use std::io::Write as _;
use std::path::PathBuf;

use tokio_util::sync::CancellationToken;

use flowchart::{CommandInfo, resolve_command, validate};
use flowchart_runner::error::ExecutionError;
use flowchart_runner::executor::{ExecutorConfig, run_flowchart};
use flowchart_runner::session::Session;
use flowchart_runner::variables::build_variables;

use crate::tui_protocol::TuiProtocol;

/// Interactive REPL for flowcoder.
pub struct Repl {
    search_paths: Vec<PathBuf>,
    config: ExecutorConfig,
}

impl Repl {
    pub const fn new(search_paths: Vec<PathBuf>, config: ExecutorConfig) -> Self {
        Self {
            search_paths,
            config,
        }
    }

    /// Run the interactive REPL loop.
    pub async fn run<S: Session>(
        &self,
        session: &mut S,
        protocol: &mut TuiProtocol,
    ) -> anyhow::Result<()> {
        eprintln!("flowcoder REPL — type /help for commands, /quit to exit\n");

        loop {
            let input = match read_input().await {
                Some(line) => line,
                None => break, // EOF
            };

            let trimmed = input.trim();
            if trimmed.is_empty() {
                continue;
            }

            if trimmed.starts_with('/') {
                let should_exit = self
                    .handle_command(trimmed, session, protocol)
                    .await?;
                if should_exit {
                    break;
                }
            } else {
                // Chat mode — direct query without flowchart
                self.handle_chat(trimmed, session, protocol).await;
            }
        }

        Ok(())
    }

    /// Handle a slash command. Returns true if REPL should exit.
    async fn handle_command<S: Session>(
        &self,
        input: &str,
        session: &mut S,
        protocol: &mut TuiProtocol,
    ) -> anyhow::Result<bool> {
        let parts: Vec<&str> = input.splitn(2, ' ').collect();
        let cmd = parts[0];
        let args = parts.get(1).copied().unwrap_or("");

        match cmd {
            "/help" => {
                eprintln!("Commands:");
                eprintln!("  /help           Show this help");
                eprintln!("  /list           List available flowchart commands");
                eprintln!("  /cost           Show accumulated cost");
                eprintln!("  /clear          Clear conversation context");
                eprintln!("  /model [name]   Show or change model");
                eprintln!("  /quit, /exit    Exit REPL");
                eprintln!("  /<command> args  Run a flowchart command");
                eprintln!("  (plain text)    Chat directly with Claude");
            }

            "/quit" | "/exit" => {
                return Ok(true);
            }

            "/list" => {
                self.cmd_list();
            }

            "/cost" => {
                let cost = session.total_cost();
                eprintln!("Total cost: ${cost:.4}");
            }

            "/clear" => {
                session
                    .clear()
                    .await
                    .map_err(|e| anyhow::anyhow!("Clear failed: {e}"))?;
                eprintln!("Session cleared.");
            }

            "/model" => {
                if args.is_empty() {
                    eprintln!("Usage: /model <name>");
                    eprintln!("Examples: /model sonnet, /model haiku, /model opus");
                } else {
                    eprintln!(
                        "Model switching requires restarting the session. \
                         Use --model flag when starting flowcoder."
                    );
                }
            }

            _ => {
                // Try as flowchart command (strip leading /)
                let name = &cmd[1..];
                self.run_command(name, args, session, protocol).await;
            }
        }

        Ok(false)
    }

    fn cmd_list(&self) {
        let commands = flowchart::resolve::list_commands(&self.search_paths);
        if commands.is_empty() {
            eprintln!("No commands found in search paths.");
            return;
        }

        eprintln!("Available commands:");
        for info in &commands {
            self.print_command_info(info);
        }
    }

    fn print_command_info(&self, info: &CommandInfo) {
        let args_display = match resolve_command(&info.name, &self.search_paths) {
            Ok(cmd) => {
                if cmd.arguments.is_empty() {
                    String::new()
                } else {
                    let args: Vec<String> = cmd
                        .arguments
                        .iter()
                        .map(|a| {
                            if a.required.unwrap_or(true) {
                                format!("<{}>", a.name)
                            } else if let Some(default) = &a.default {
                                format!("[{}={}]", a.name, default)
                            } else {
                                format!("[{}]", a.name)
                            }
                        })
                        .collect();
                    format!(" {}", args.join(" "))
                }
            }
            Err(_) => String::new(),
        };

        let desc = info.description.as_deref().unwrap_or("");
        eprintln!("  /{}{args_display}  {desc}", info.name);
    }

    async fn run_command<S: Session>(
        &self,
        name: &str,
        args: &str,
        session: &mut S,
        protocol: &mut TuiProtocol,
    ) {
        let command = match resolve_command(name, &self.search_paths) {
            Ok(cmd) => cmd,
            Err(e) => {
                eprintln!("Error: {e}");
                return;
            }
        };

        if let Err(errors) = validate(&command.flowchart) {
            eprintln!("Validation errors in '{name}':");
            for e in &errors {
                eprintln!("  - {e}");
            }
            return;
        }

        // Check required args before starting
        if let Err(e) = build_variables(args, &command.arguments) {
            match &e {
                ExecutionError::MissingArgument { name: arg_name, .. } => {
                    eprintln!("Missing required argument: {arg_name}");
                    let usage: Vec<String> = command
                        .arguments
                        .iter()
                        .map(|a| {
                            if a.required.unwrap_or(true) {
                                format!("<{}>", a.name)
                            } else {
                                format!("[{}]", a.name)
                            }
                        })
                        .collect();
                    eprintln!("Usage: /{name} {}", usage.join(" "));
                }
                _ => eprintln!("Error: {e}"),
            }
            return;
        }

        let cancel = CancellationToken::new();
        protocol.reset();

        let result = run_flowchart(session, protocol, &command, args, &self.config, cancel).await;

        match result {
            Ok(_) => {
                // Output already displayed via streaming protocol
            }
            Err(e) => {
                eprintln!("Execution error: {e}");
            }
        }
    }

    async fn handle_chat<S: Session>(
        &self,
        input: &str,
        session: &mut S,
        protocol: &mut TuiProtocol,
    ) {
        match session.query(input, "", "chat", protocol).await {
            Ok(_) => {
                // Stream text already displayed via protocol.on_stream_text
                eprintln!();
            }
            Err(e) => {
                eprintln!("\nError: {e}");
            }
        }
    }
}

/// Read a line of input without blocking the async runtime.
async fn read_input() -> Option<String> {
    eprint!("> ");
    let _ = std::io::stderr().flush();
    tokio::task::spawn_blocking(|| {
        let mut line = String::new();
        match std::io::stdin().read_line(&mut line) {
            Ok(0) | Err(_) => None,
            Ok(_) => Some(line),
        }
    })
    .await
    .ok()
    .flatten()
}
