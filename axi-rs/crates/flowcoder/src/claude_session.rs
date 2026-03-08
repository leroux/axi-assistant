use flowchart_runner::error::ExecutionError;
use flowchart_runner::protocol::Protocol;
use flowchart_runner::session::{QueryResult, Session};

use claudewire::config::Config;
use claudewire::session::CliSession;

use std::time::Instant;

/// Callback for handling control requests during a query.
/// Takes a `control_request` JSON, returns a `control_response` JSON.
pub type ControlCallback = Box<dyn Fn(&serde_json::Value) -> serde_json::Value + Send + Sync>;

/// Session implementation backed by Claude CLI via `claudewire::CliSession`.
pub struct ClaudeSession {
    config: Config,
    name: String,
    cli: Option<CliSession>,
    total_cost: f64,
    session_id: Option<String>,
    control_callback: Option<ControlCallback>,
    debug: bool,
}

impl ClaudeSession {
    /// Create a new Claude session. Spawns the CLI process immediately.
    pub fn new(
        config: Config,
        name: String,
        control_callback: Option<ControlCallback>,
        debug: bool,
    ) -> anyhow::Result<Self> {
        let cli = CliSession::spawn(&config, name.clone(), None)?;
        Ok(Self {
            config,
            name,
            cli: Some(cli),
            total_cost: 0.0,
            session_id: None,
            control_callback,
            debug,
        })
    }

    /// Get the current session ID (from the last result message).
    #[allow(dead_code)]
    pub fn session_id(&self) -> Option<&str> {
        self.session_id.as_deref()
    }
}

impl Session for ClaudeSession {
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

        // Send user message
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

        // Read messages until result
        let mut response_parts: Vec<String> = Vec::new();
        let mut cost_usd = 0.0;
        let mut duration_ms = 0u64;
        let mut session_id = None;

        loop {
            let msg = cli.read_message().await;
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
                | "tool_use_summary" | "auth_status" | "prompt_suggestion" => {
                    if self.debug {
                        eprintln!("[debug] {msg_type}: {}", summarize_msg(&msg));
                    }
                }

                "assistant" => {
                    // Extract text from assistant message
                    if let Some(text) = extract_assistant_text(&msg) {
                        response_parts.push(text);
                    }
                    // Forward to protocol
                    protocol.on_forwarded_message(&msg, block_id, block_name);
                }

                "stream_event" => {
                    // Extract text_delta for streaming display
                    if let Some(text) = extract_stream_text(&msg) {
                        protocol.on_stream_text(&text);
                    }
                    protocol.on_forwarded_message(&msg, block_id, block_name);
                }

                "control_request" => {
                    // Handle control request via callback
                    let response = if let Some(cb) = &self.control_callback {
                        cb(&msg)
                    } else {
                        // Default: deny
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
                    };
                    let _ = cli.write(&response.to_string()).await;
                }

                "result" => {
                    // Extract final data
                    if let Some(c) = msg.get("total_cost_usd").and_then(serde_json::Value::as_f64)
                    {
                        cost_usd = c;
                        self.total_cost = c; // total_cost_usd is cumulative
                    }
                    if let Some(d) = msg.get("duration_ms").and_then(serde_json::Value::as_i64) {
                        duration_ms = d as u64;
                    }
                    if let Some(s) = msg.get("session_id").and_then(serde_json::Value::as_str) {
                        session_id = Some(s.to_owned());
                        self.session_id = Some(s.to_owned());
                    }

                    // Use result text if no assistant parts collected
                    if response_parts.is_empty()
                        && let Some(r) = msg.get("result").and_then(serde_json::Value::as_str) {
                            response_parts.push(r.to_owned());
                        }

                    // Check for error
                    if msg.get("is_error").and_then(serde_json::Value::as_bool) == Some(true) {
                        let err_text = response_parts.join("");
                        return Err(ExecutionError::Session(err_text));
                    }

                    break;
                }

                _ => {
                    if self.debug {
                        eprintln!("[debug] UNHANDLED {msg_type}: {msg}");
                    }
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
        // Stop existing session
        if let Some(mut cli) = self.cli.take() {
            cli.stop().await;
        }

        // Spawn new session with same config (cost survives)
        let cli = CliSession::spawn(&self.config, self.name.clone(), None)
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
        // Send SIGINT to the process group to abort the current API call.
        // The CLI will emit a result message and stay alive.
        // CliSession doesn't expose PID directly, so we stop and let
        // the consumer respawn if needed.
        // TODO: Add PID access to claudewire::CliSession for proper SIGINT support
        tracing::debug!("Interrupt requested — stopping session");
        if let Some(mut cli) = self.cli.take() {
            cli.stop().await;
        }
        Ok(())
    }

    fn total_cost(&self) -> f64 {
        self.total_cost
    }
}

/// Extract text content from an assistant message.
fn extract_assistant_text(msg: &serde_json::Value) -> Option<String> {
    // Try message.content (list of content blocks)
    if let Some(content) = msg
        .get("message")
        .and_then(|m| m.get("content"))
        .and_then(serde_json::Value::as_array)
    {
        let mut parts = Vec::new();
        for block in content {
            if block.get("type").and_then(serde_json::Value::as_str) == Some("text")
                && let Some(text) = block.get("text").and_then(serde_json::Value::as_str) {
                    parts.push(text.to_owned());
                }
        }
        if !parts.is_empty() {
            return Some(parts.join(""));
        }
    }

    // Try message.content as string
    if let Some(text) = msg
        .get("message")
        .and_then(|m| m.get("content"))
        .and_then(serde_json::Value::as_str)
    {
        return Some(text.to_owned());
    }

    // Try top-level content
    if let Some(text) = msg.get("content").and_then(serde_json::Value::as_str) {
        return Some(text.to_owned());
    }

    None
}

/// One-line summary of a message for debug output.
/// Prints key fields without dumping the entire JSON.
fn summarize_msg(msg: &serde_json::Value) -> String {
    let mut parts = Vec::new();
    if let Some(subtype) = msg.get("subtype").and_then(serde_json::Value::as_str) {
        parts.push(format!("subtype={subtype}"));
    }
    if let Some(sid) = msg.get("session_id").and_then(serde_json::Value::as_str) {
        parts.push(format!("session={}", &sid[..sid.len().min(12)]));
    }
    if let Some(info) = msg.get("rate_limit_info")
        && let Some(remaining) = info.get("requests_remaining")
    {
        parts.push(format!("remaining={remaining}"));
    }
    if parts.is_empty() {
        // Fall back to compact JSON, truncated
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

    delta.get("text").and_then(serde_json::Value::as_str).map(str::to_owned)
}
