//! Wire protocol crate for the Claude CLI's `stream-json` interface.
//!
//! claudewire is the Rust implementation of the protocol that Claude Code's CLI
//! speaks over stdin/stdout when invoked with `--input-format stream-json
//! --output-format stream-json`. It handles everything needed to host a Claude
//! CLI subprocess: configuration, serialization, session lifecycle, and message
//! routing.
//!
//! This crate is intentionally free of application logic (no Discord, no bot
//! concepts). Any program that needs to drive a Claude CLI process — a Discord
//! bot, a CLI wrapper, a test harness — can depend on claudewire directly.
//!
//! See `PROTOCOL.md` in this crate for the full wire protocol reference
//! (message types, session lifecycle, MCP handshake, dual emission, etc.).
//!
//! # Modules
//!
//! - [`config`] — `Config` struct that maps to CLI flags and env vars.
//!   Build one, call `to_cli_args()` and `to_env()`.
//! - [`schema`] — Serde types for every message in the stream-json protocol:
//!   `InboundMsg` (CLI → host), `OutboundMsg` (host → CLI), stream events,
//!   control requests/responses, rate limits.
//! - [`session`] — `CliSession` manages a Claude CLI subprocess. Spawn one
//!   from a `Config` or a raw `tokio::process::Command`, then call
//!   `read_message()` / `write()` / `stop()`.
//! - [`types`] — Low-level process IO types (`ProcessEvent`, `StdoutEvent`,
//!   `StderrEvent`, `ExitEvent`) used by `CliSession` internally and by
//!   external backends like procmux.
//!
//! # Usage
//!
//! ```no_run
//! use claudewire::config::Config;
//! use claudewire::session::CliSession;
//!
//! # #[tokio::main]
//! # async fn main() -> anyhow::Result<()> {
//! // 1. Build a config
//! let config = Config {
//!     model: "sonnet".into(),
//!     permission_mode: "plan".into(),
//!     ..Default::default()
//! };
//!
//! // 2. Spawn a CLI session
//! let mut session = CliSession::spawn(&config, "my-agent".into(), None)?;
//!
//! // 3. Wait for system.init
//! let init = session.read_message().await.expect("CLI should send system.init");
//! assert_eq!(init["type"], "system");
//!
//! // 4. Send a query
//! let user_msg = serde_json::json!({
//!     "type": "user",
//!     "session_id": "",
//!     "message": {"role": "user", "content": "Say hello"},
//!     "parent_tool_use_id": null,
//! });
//! session.write(&user_msg.to_string()).await?;
//!
//! // 5. Read messages until result
//! while let Some(msg) = session.read_message().await {
//!     match msg["type"].as_str().unwrap_or("") {
//!         "result" => break,
//!         _ => { /* handle stream_event, assistant, control_request, etc. */ }
//!     }
//! }
//!
//! session.stop().await;
//! # Ok(())
//! # }
//! ```

pub mod config;
pub mod schema;
pub mod session;
pub mod types;
