//! `flowcoder-engine` — headless proxy binary wrapping Claude CLI.
//!
//! Transparent proxy with flowchart command interception. Sits between
//! the outer client (axi, TUI, etc.) and inner Claude CLI subprocess.
//!
//! - Proxy mode: forwards client ↔ Claude messages unchanged
//! - Flowchart mode: intercepts `/command` messages, executes flowcharts
//!
//! Stdin/stdout protocol: NDJSON, superset of claudewire stream-json.

use std::path::PathBuf;
use std::time::Duration;

use anyhow::Result;
use clap::Parser;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use tracing::debug;

use flowchart::{resolve_command, validate};
use flowchart_runner::Session;
use flowchart_runner::executor::{ExecutorConfig, run_flowchart};

mod control;
mod engine_protocol;
mod engine_session;
mod events;
mod proxy;
mod router;

use engine_protocol::EngineProtocol;
use engine_session::EngineSession;
use events::EngineEvent;

#[derive(clap::Parser)]
#[command(
    name = "flowcoder-engine",
    about = "Headless Claude CLI proxy with flowchart support"
)]
struct Cli {
    /// Additional search paths for flowchart command files
    #[arg(long = "search-path", action = clap::ArgAction::Append)]
    search_paths: Vec<String>,

    /// Maximum blocks before safety halt
    #[arg(long, default_value_t = 1000)]
    max_blocks: usize,

    /// Enable debug logging to stderr
    #[arg(long)]
    debug: bool,

    /// Arguments passed through to the inner Claude CLI process.
    /// Provide after `--` separator.
    #[arg(last = true, allow_hyphen_values = true)]
    claude_args: Vec<String>,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    // Initialize tracing to stderr
    let level = if cli.debug {
        tracing::Level::DEBUG
    } else {
        tracing::Level::WARN
    };
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env().add_directive(level.into()),
        )
        .with_writer(std::io::stderr)
        .init();

    // Build search paths
    let search_paths: Vec<PathBuf> = cli.search_paths.iter().map(PathBuf::from).collect();

    // Build Claude CLI args: engine ensures --print + stream-json + replay
    let claude_args = build_claude_args(&cli.claude_args);

    // Build executor config
    let exec_config = ExecutorConfig {
        max_blocks: cli.max_blocks,
        search_paths: search_paths.clone(),
        ..Default::default()
    };

    // Start stdin router
    let router::RouterChannels {
        control_response_rx,
        message_rx,
    } = router::spawn_stdin_router();

    // Create engine session (spawns inner Claude)
    let cancel = CancellationToken::new();
    let mut session =
        EngineSession::new(claude_args, "engine".into(), control_response_rx, cancel.clone())?;
    let mut protocol = EngineProtocol::new();
    let mut message_rx = message_rx;

    debug!("Engine started");

    // Drain startup control_requests (SDK MCP initialization handshake).
    // The inner Claude CLI may send control_request messages during MCP server
    // initialization before it's ready for user input. We must handle these
    // before entering the main loop, otherwise the inner CLI blocks waiting
    // for control_responses that never come (deadlock).
    drain_startup_control_requests(&mut session).await;

    debug!("Startup drain complete, entering main loop");

    // Install SIGINT handler — trap and forward as cancel (don't die)
    let sigint_cancel = cancel.clone();
    tokio::spawn(async move {
        loop {
            if tokio::signal::ctrl_c().await.is_ok() {
                debug!("SIGINT received — forwarding as cancel");
                sigint_cancel.cancel();
            }
        }
    });

    // Main loop: read messages, dispatch proxy or flowchart
    loop {
        let msg = if let Some(m) = message_rx.recv().await { m } else {
            debug!("Message channel closed, shutting down");
            break;
        };

        let msg_type = msg
            .get("type")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("");

        match msg_type {
            "user" => {
                // Check if this is a flowchart command
                if let Some((name, args)) = proxy::extract_command_name(&msg) {
                    let fc_result = try_run_flowchart(
                        &name,
                        &args,
                        &search_paths,
                        &exec_config,
                        &mut session,
                        &mut protocol,
                        message_rx,
                    )
                    .await;
                    message_rx = fc_result.message_rx;

                    // Process buffered messages (non-control messages received during flowchart)
                    for buffered_msg in fc_result.buffered {
                        proxy::proxy_query_session(&mut session, &buffered_msg).await;
                    }

                    if fc_result.was_command {
                        continue;
                    }
                    // Not a valid flowchart command — fall through to proxy
                }

                // Proxy mode: forward to inner Claude
                proxy::proxy_query_session(&mut session, &msg).await;
            }

            "engine_control" => {
                let command = msg
                    .get("command")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("");
                match command {
                    "status" => {
                        events::emit(&EngineEvent::EngineStatus {
                            mode: "proxy".into(),
                            current_block: None,
                            blocks_done: 0,
                            total_blocks: 0,
                            paused: false,
                        });
                    }
                    "interrupt" => {
                        debug!("Engine control: interrupt in proxy mode");
                        if let Some(cli) = session.cli_mut() {
                            cli.send_signal(nix::sys::signal::Signal::SIGINT);
                        }
                    }
                    _ => {
                        debug!("Engine control '{command}' ignored in proxy mode");
                    }
                }
            }

            _ => {
                debug!("Unexpected message type in main loop: {msg_type}");
            }
        }
    }

    session.stop().await;
    Ok(())
}

/// Result of `try_run_flowchart` — returns ownership of `message_rx`.
struct FlowchartAttempt {
    was_command: bool,
    message_rx: mpsc::UnboundedReceiver<serde_json::Value>,
    /// Messages buffered by the control reader during flowchart execution.
    buffered: Vec<serde_json::Value>,
}

/// Try to run a flowchart command. Returns the `message_rx` (ownership transfer)
/// and whether the name resolved as a flowchart command.
async fn try_run_flowchart(
    name: &str,
    args: &str,
    search_paths: &[PathBuf],
    exec_config: &ExecutorConfig,
    session: &mut EngineSession,
    protocol: &mut EngineProtocol,
    message_rx: mpsc::UnboundedReceiver<serde_json::Value>,
) -> FlowchartAttempt {
    // Resolve command from search paths
    let command = match resolve_command(name, search_paths) {
        Ok(cmd) => cmd,
        Err(_) => {
            return FlowchartAttempt {
                was_command: false,
                message_rx,
                buffered: Vec::new(),
            };
        }
    };

    // Validate
    if let Err(errors) = validate(&command.flowchart) {
        for e in &errors {
            events::emit(&EngineEvent::EngineLog {
                message: format!("Validation error in '{name}': {e}"),
            });
        }
        return FlowchartAttempt {
            was_command: true,
            message_rx,
            buffered: Vec::new(),
        };
    }

    // Set up cancellation and control for this flowchart
    let cancel = CancellationToken::new();
    session.set_cancel(cancel.clone());

    let control_state = control::ControlState::new(cancel.clone());

    let fc_config = ExecutorConfig {
        max_blocks: exec_config.max_blocks,
        max_depth: exec_config.max_depth,
        search_paths: exec_config.search_paths.clone(),
        soft_timeout_secs: exec_config.soft_timeout_secs,
        pause_flag: Some(control_state.pause_flag.clone()),
        pause_signal: Some(control_state.pause_signal.clone()),
    };

    // Spawn background control reader — takes ownership of message_rx
    let done = CancellationToken::new();
    let control_handle =
        control::spawn_control_reader(message_rx, &control_state, protocol, done.clone());

    // Run the flowchart
    let result = run_flowchart(session, protocol, &command, args, &fc_config, cancel).await;

    // Stop control reader, recover message_rx and buffered messages
    done.cancel();
    let cr_result = control_handle.await.unwrap_or_else(|_| {
        control::ControlReaderResult {
            buffered: Vec::new(),
            message_rx: mpsc::unbounded_channel().1,
        }
    });

    match &result {
        Ok(r) => {
            debug!(
                "Flowchart '{}' completed: {}",
                name,
                events::format_status(&r.status)
            );
        }
        Err(e) => {
            events::emit(&EngineEvent::EngineLog {
                message: format!("Flowchart '{name}' error: {e}"),
            });
        }
    }

    FlowchartAttempt {
        was_command: true,
        message_rx: cr_result.message_rx,
        buffered: cr_result.buffered,
    }
}

/// Drain `control_request` messages from the inner CLI during startup.
///
/// When the inner Claude CLI has SDK MCP servers (`{"type":"sdk"}`), it sends
/// `control_request` messages during MCP initialization (initialize, tools/list)
/// before accepting any user input. These must be forwarded to the outer client
/// and responses relayed back before the first user message is sent.
///
/// Without this drain, the engine's main loop blocks waiting for user messages
/// while the inner CLI blocks waiting for `control_responses` — a deadlock.
///
/// With `--resume`, session loading can take 10–30 seconds before MCP init
/// starts, so the first-message timeout must be generous.
async fn drain_startup_control_requests(session: &mut EngineSession) {
    let mut count = 0u32;
    let mut got_any = false;

    loop {
        // Before any message: wait up to 60s for the inner CLI to start
        // producing output (session loading with --resume can be slow).
        // After the first message: 2s timeout between messages.
        // After a control_request: 2s to allow for follow-up MCP handshake.
        let timeout = if got_any {
            Duration::from_secs(2)
        } else {
            Duration::from_secs(60)
        };

        let cli = match session.cli_mut() {
            Some(c) => c,
            None => break,
        };

        let result = tokio::time::timeout(timeout, cli.read_message()).await;

        match result {
            Ok(Some(msg)) => {
                got_any = true;
                let msg_type = msg
                    .get("type")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("");

                if msg_type == "control_request" {
                    count += 1;
                    debug!("Startup: control_request #{count}");
                    events::emit_raw(&msg);

                    // Wait for the outer client to send back a control_response
                    if let Some(response) = session.control_response_rx_mut().recv().await {
                        if let Some(cli) = session.cli_mut() {
                            let _ = cli.write(&response.to_string()).await;
                        }
                    } else {
                        debug!("Startup: control_response channel closed");
                        break;
                    }
                } else {
                    debug!("Startup: forwarding {msg_type} message");
                    events::emit_raw(&msg);
                }
            }
            Ok(None) => {
                debug!("Startup: inner CLI exited during drain");
                break;
            }
            Err(_) => {
                // Timeout — no more startup messages, initialization complete
                debug!("Startup drain: handled {count} control_requests");
                break;
            }
        }
    }
}

/// Build the inner Claude CLI argv from passthrough args.
///
/// Ensures `--print`, `--output-format stream-json`, `--input-format stream-json`,
/// and `--replay-user-messages` are present regardless of what the caller passes.
fn build_claude_args(passthrough: &[String]) -> Vec<String> {
    let mut args = vec![
        "claude".to_string(),
        "--print".to_string(),
        "--output-format".to_string(),
        "stream-json".to_string(),
        "--input-format".to_string(),
        "stream-json".to_string(),
        "--replay-user-messages".to_string(),
    ];

    // Append passthrough args, skipping any that duplicate what we already set
    let skip_flags = [
        "--print",
        "--output-format",
        "--input-format",
        "--replay-user-messages",
    ];
    let mut skip_next = false;
    for arg in passthrough {
        if skip_next {
            skip_next = false;
            continue;
        }
        if skip_flags.contains(&arg.as_str()) {
            // Skip flags that take a value argument
            if arg == "--output-format" || arg == "--input-format" {
                skip_next = true;
            }
            continue;
        }
        args.push(arg.clone());
    }

    args
}
