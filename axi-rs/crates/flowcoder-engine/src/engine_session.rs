//! `EngineSession` — implements `flowchart_runner::Session` around `CliSession`.
//!
//! Mirrors `flowcoder/src/claude_session.rs` with one key difference:
//! `control_requests` are relayed to the engine's stdout (for the outer client
//! to handle), and responses are read from the router's `control_response` channel.

use std::process::Stdio;
use std::time::Instant;

use tokio::process::Command;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use tracing::debug;

use claudewire::session::CliSession;
use flowchart_runner::error::ExecutionError;
use flowchart_runner::protocol::Protocol;
use flowchart_runner::session::{QueryResult, Session};

use crate::events;

/// Session backed by an inner Claude CLI subprocess.
///
/// Relays `control_requests` to engine stdout (the outer client) and reads
/// `control_responses` from the router's dedicated channel.
pub struct EngineSession {
    /// Raw CLI args for the inner Claude process (for respawning on clear).
    claude_args: Vec<String>,
    name: String,
    cli: Option<CliSession>,
    total_cost: f64,
    session_id: Option<String>,
    control_response_rx: mpsc::UnboundedReceiver<serde_json::Value>,
    cancel: CancellationToken,
}

impl EngineSession {
    /// Spawn the inner Claude CLI from raw CLI args.
    ///
    /// `claude_args` are the full argv for the inner process (e.g. `["claude", "--print", ...]`).
    /// The engine adds `--print`, `--output-format stream-json`, etc. if not already present.
    pub fn new(
        claude_args: Vec<String>,
        name: String,
        control_response_rx: mpsc::UnboundedReceiver<serde_json::Value>,
        cancel: CancellationToken,
    ) -> anyhow::Result<Self> {
        let cli = Self::spawn_claude(&claude_args, &name)?;
        Ok(Self {
            claude_args,
            name,
            cli: Some(cli),
            total_cost: 0.0,
            session_id: None,
            control_response_rx,
            cancel,
        })
    }

    fn spawn_claude(args: &[String], name: &str) -> anyhow::Result<CliSession> {
        let binary = args.first().map_or("claude", |s| s.as_str());
        let mut cmd = Command::new(binary);
        if args.len() > 1 {
            cmd.args(&args[1..]);
        }
        cmd.stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        // Prevent nested session detection
        cmd.env_remove("CLAUDECODE");
        CliSession::from_command(cmd, name.to_string(), None)
    }

    /// Borrow the inner `CliSession` mutably.
    pub const fn cli_mut(&mut self) -> Option<&mut CliSession> {
        self.cli.as_mut()
    }

    /// Current session ID from the last result message.
    #[allow(dead_code)]
    pub fn session_id(&self) -> Option<&str> {
        self.session_id.as_deref()
    }

    /// Borrow the `control_response` receiver mutably (for proxy mode).
    pub const fn control_response_rx_mut(
        &mut self,
    ) -> &mut mpsc::UnboundedReceiver<serde_json::Value> {
        &mut self.control_response_rx
    }

    /// Replace the cancellation token (used when starting a new flowchart).
    pub fn set_cancel(&mut self, cancel: CancellationToken) {
        self.cancel = cancel;
    }
}

impl Session for EngineSession {
    async fn query(
        &mut self,
        prompt: &str,
        block_id: &str,
        block_name: &str,
        protocol: &mut dyn Protocol,
    ) -> Result<QueryResult, ExecutionError> {
        let cli = self
            .cli
            .as_mut()
            .ok_or_else(|| ExecutionError::Session("Session not started".into()))?;

        let start = Instant::now();

        // Send user message to inner Claude
        let user_msg = serde_json::json!({
            "type": "user",
            "message": {
                "role": "user",
                "content": prompt
            }
        });
        cli.write(&user_msg.to_string())
            .await
            .map_err(|e| ExecutionError::Session(e.to_string()))?;

        let mut response_parts: Vec<String> = Vec::new();
        let mut cost_usd = 0.0;
        let mut duration_ms = 0u64;
        let mut session_id = None;

        loop {
            // Race: cancel token vs reading next message
            let msg = tokio::select! {
                () = self.cancel.cancelled() => {
                    return Err(ExecutionError::Session("Cancelled".into()));
                }
                msg = cli.read_message() => msg,
            };

            let msg = match msg {
                Some(m) => m,
                None => {
                    return Err(ExecutionError::Session(
                        "CLI process exited during query".into(),
                    ));
                }
            };

            let msg_type = msg
                .get("type")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("");

            match msg_type {
                "system" | "rate_limit_event" | "keep_alive" | "tool_progress"
                | "tool_use_summary" | "auth_status" | "prompt_suggestion"
                | "user" => {
                    debug!("{msg_type}: {}", summarize_msg(&msg));
                }

                "assistant" => {
                    if let Some(text) = extract_assistant_text(&msg) {
                        response_parts.push(text);
                    }
                    protocol.on_forwarded_message(&msg, block_id, block_name);
                }

                "stream_event" => {
                    if let Some(text) = extract_stream_text(&msg) {
                        protocol.on_stream_text(&text);
                    }
                    protocol.on_forwarded_message(&msg, block_id, block_name);
                }

                "control_request" => {
                    // Relay to engine stdout for the outer client
                    events::emit_raw(&msg);

                    // Wait for response from the router's control_response channel
                    let response = tokio::select! {
                        () = self.cancel.cancelled() => {
                            return Err(ExecutionError::Session("Cancelled during control_request".into()));
                        }
                        resp = self.control_response_rx.recv() => {
                            if let Some(r) = resp { r } else {
                                // Channel closed — construct a deny response
                                let request_id = msg
                                    .get("request_id")
                                    .and_then(serde_json::Value::as_str)
                                    .unwrap_or("");
                                serde_json::json!({
                                    "type": "control_response",
                                    "response": {
                                        "subtype": "permissions_response",
                                        "request_id": request_id,
                                        "response": {"allowed": false}
                                    }
                                })
                            }
                        }
                    };

                    let _ = cli.write(&response.to_string()).await;
                }

                "result" => {
                    if let Some(c) =
                        msg.get("total_cost_usd").and_then(serde_json::Value::as_f64)
                    {
                        cost_usd = c;
                        self.total_cost = c; // cumulative
                    }
                    if let Some(d) = msg.get("duration_ms").and_then(serde_json::Value::as_i64) {
                        duration_ms = d as u64;
                    }
                    if let Some(s) = msg.get("session_id").and_then(serde_json::Value::as_str) {
                        session_id = Some(s.to_owned());
                        self.session_id = Some(s.to_owned());
                    }

                    if response_parts.is_empty()
                        && let Some(r) = msg.get("result").and_then(serde_json::Value::as_str)
                    {
                        response_parts.push(r.to_owned());
                    }

                    if msg.get("is_error").and_then(serde_json::Value::as_bool) == Some(true) {
                        let err_text = response_parts.join("");
                        return Err(ExecutionError::Session(err_text));
                    }

                    break;
                }

                _ => {
                    debug!("UNHANDLED {msg_type}: {}", summarize_msg(&msg));
                }
            }
        }

        let response_text = response_parts.join("");
        let elapsed = start.elapsed().as_millis() as u64;

        Ok(QueryResult {
            response_text,
            cost_usd,
            duration_ms: if duration_ms > 0 { duration_ms } else { elapsed },
            session_id,
        })
    }

    async fn clear(&mut self) -> Result<(), ExecutionError> {
        if let Some(mut cli) = self.cli.take() {
            cli.stop().await;
        }

        let cli = Self::spawn_claude(&self.claude_args, &self.name)
            .map_err(|e| ExecutionError::Session(e.to_string()))?;
        self.cli = Some(cli);
        Ok(())
    }

    async fn stop(&mut self) {
        if let Some(mut cli) = self.cli.take() {
            cli.stop().await;
        }
    }

    async fn interrupt(&mut self) -> Result<(), ExecutionError> {
        debug!("Interrupt requested — sending SIGINT to inner Claude");
        if let Some(cli) = self.cli.as_ref()
            && !cli.send_signal(nix::sys::signal::Signal::SIGINT) {
                debug!("SIGINT not supported on this session, falling back to stop");
                if let Some(mut cli) = self.cli.take() {
                    cli.stop().await;
                }
            }
        Ok(())
    }

    fn total_cost(&self) -> f64 {
        self.total_cost
    }
}

/// Extract text content from an assistant message.
fn extract_assistant_text(msg: &serde_json::Value) -> Option<String> {
    if let Some(content) = msg
        .get("message")
        .and_then(|m| m.get("content"))
        .and_then(serde_json::Value::as_array)
    {
        let mut parts = Vec::new();
        for block in content {
            if block.get("type").and_then(serde_json::Value::as_str) == Some("text")
                && let Some(text) = block.get("text").and_then(serde_json::Value::as_str)
            {
                parts.push(text.to_owned());
            }
        }
        if !parts.is_empty() {
            return Some(parts.join(""));
        }
    }

    if let Some(text) = msg
        .get("message")
        .and_then(|m| m.get("content"))
        .and_then(serde_json::Value::as_str)
    {
        return Some(text.to_owned());
    }

    msg.get("content")
        .and_then(serde_json::Value::as_str)
        .map(str::to_owned)
}

/// Extract streaming text delta from a `stream_event` message.
fn extract_stream_text(msg: &serde_json::Value) -> Option<String> {
    let event = msg.get("event")?;
    let event_type = event.get("type").and_then(serde_json::Value::as_str)?;
    if event_type != "content_block_delta" {
        return None;
    }

    let delta = event.get("delta")?;
    let delta_type = delta.get("type").and_then(serde_json::Value::as_str)?;
    if delta_type != "text_delta" {
        return None;
    }

    delta
        .get("text")
        .and_then(serde_json::Value::as_str)
        .map(str::to_owned)
}

/// One-line summary of a message for debug output.
fn summarize_msg(msg: &serde_json::Value) -> String {
    let mut parts = Vec::new();
    if let Some(subtype) = msg.get("subtype").and_then(serde_json::Value::as_str) {
        parts.push(format!("subtype={subtype}"));
    }
    if let Some(sid) = msg.get("session_id").and_then(serde_json::Value::as_str) {
        parts.push(format!("session={}", &sid[..sid.len().min(12)]));
    }
    if parts.is_empty() {
        let s = msg.to_string();
        if s.len() > 200 {
            format!("{}...", &s[..200])
        } else {
            s
        }
    } else {
        parts.join(" ")
    }
}
