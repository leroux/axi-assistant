use std::path::PathBuf;

use anyhow::Result;
use clap::Parser;
use tokio_util::sync::CancellationToken;

use claudewire::config::Config;
use flowchart::{resolve_command, validate};
use flowchart_runner::Session;
use flowchart_runner::executor::{ExecutorConfig, run_flowchart};
use flowchart_runner::variables::build_variables;

mod claude_session;
mod repl;
mod tui_protocol;

use claude_session::{ClaudeSession, ControlCallback};
use repl::Repl;
use tui_protocol::TuiProtocol;

#[derive(clap::Parser)]
#[command(name = "flowcoder", about = "Flowchart-driven Claude CLI runner")]
struct Cli {
    /// Claude model to use
    #[arg(long, default_value = "sonnet")]
    model: String,

    /// Additional search paths for flowchart command files
    #[arg(long = "search-path", action = clap::ArgAction::Append)]
    search_paths: Vec<String>,

    /// Claude permission mode (plan, edit, etc)
    #[arg(long, default_value = "plan")]
    permission_mode: String,

    /// Auto-allow all tool permissions (no prompting)
    #[arg(long)]
    skip_permissions: bool,

    /// Maximum blocks before safety halt
    #[arg(long, default_value_t = 1000)]
    max_blocks: usize,

    /// Resume an existing Claude session
    #[arg(long)]
    resume: Option<String>,

    /// Enable verbose output (show stream text after blocks)
    #[arg(long)]
    verbose: bool,

    /// Print all unhandled JSON-RPC messages from Claude CLI to stderr
    #[arg(long)]
    debug: bool,

    /// Flowchart command to run (omit for REPL mode)
    command: Option<String>,

    /// Arguments for the flowchart command
    args: Vec<String>,
}

#[tokio::main]
async fn main() -> Result<()> {
    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive(tracing::Level::WARN.into()),
        )
        .with_writer(std::io::stderr)
        .init();

    let cli = Cli::parse();

    // Build search paths
    let mut search_paths: Vec<PathBuf> = cli.search_paths.iter().map(PathBuf::from).collect();
    // Add current directory
    if let Ok(cwd) = std::env::current_dir() {
        search_paths.push(cwd);
    }

    // Build claudewire config
    let config = Config {
        model: cli.model.clone(),
        permission_mode: if cli.skip_permissions {
            "bypassPermissions".into()
        } else {
            cli.permission_mode.clone()
        },
        resume: cli.resume.clone(),
        verbose: cli.verbose,
        ..Config::default()
    };

    // Build executor config
    let exec_config = ExecutorConfig {
        max_blocks: cli.max_blocks,
        search_paths: search_paths.clone(),
        ..Default::default()
    };

    // Build control callback
    let control_callback: Option<ControlCallback> = if cli.skip_permissions {
        // Auto-allow everything
        Some(Box::new(|msg: &serde_json::Value| {
            let request_id = msg
                .get("request_id")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("");
            serde_json::json!({
                "type": "control_response",
                "response": {
                    "subtype": "permissions_response",
                    "request_id": request_id,
                    "response": {"allowed": true}
                }
            })
        }))
    } else {
        // Default: deny (TODO: add interactive terminal prompting)
        None
    };

    // Create session
    let mut session = ClaudeSession::new(config, "flowcoder".into(), control_callback, cli.debug)?;
    let mut protocol = TuiProtocol::new(cli.verbose);

    if let Some(command_name) = &cli.command {
        // Single-command mode: resolve, validate, run, exit
        run_single_command(
            command_name,
            &cli.args,
            &search_paths,
            &exec_config,
            &mut session,
            &mut protocol,
        )
        .await
    } else {
        // REPL mode
        let repl = Repl::new(search_paths, exec_config);
        repl.run(&mut session, &mut protocol).await
    }
}

async fn run_single_command(
    name: &str,
    args: &[String],
    search_paths: &[PathBuf],
    config: &ExecutorConfig,
    session: &mut ClaudeSession,
    protocol: &mut TuiProtocol,
) -> Result<()> {
    // Resolve command
    let command = resolve_command(name, search_paths)?;

    // Validate
    if let Err(errors) = validate(&command.flowchart) {
        for e in &errors {
            eprintln!("Validation error: {e}");
        }
        anyhow::bail!("Flowchart validation failed");
    }

    let args_str = args.join(" ");

    // Validate required args
    build_variables(&args_str, &command.arguments)?;

    let cancel = CancellationToken::new();

    // Set up signal handler for cancellation
    let cancel_clone = cancel.clone();
    tokio::spawn(async move {
        if matches!(tokio::signal::ctrl_c().await, Ok(())) {
            cancel_clone.cancel();
        }
    });

    let result = run_flowchart(session, protocol, &command, &args_str, config, cancel).await?;

    // Exit with the flowchart's exit code if it halted
    match result.status {
        flowchart_runner::ExecutionStatus::Halted { exit_code } => {
            std::process::exit(exit_code);
        }
        flowchart_runner::ExecutionStatus::Error(ref msg) => {
            eprintln!("Error: {msg}");
            std::process::exit(1);
        }
        flowchart_runner::ExecutionStatus::Interrupted => {
            std::process::exit(130); // Standard SIGINT exit code
        }
        flowchart_runner::ExecutionStatus::Completed => {}
    }

    session.stop().await;
    Ok(())
}
