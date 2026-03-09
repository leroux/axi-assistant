//! Proxy mode — transparent forwarding between outer client and inner Claude.
//!
//! In proxy mode, user messages are forwarded to inner Claude, and Claude's
//! responses pass through unchanged to stdout. Control requests are relayed.

use tracing::debug;

use crate::engine_session::EngineSession;
use crate::events;

/// Forward a user message to inner Claude and stream responses to stdout.
///
/// Takes the full `EngineSession` to avoid split-borrow issues (needs both
/// `cli` and `control_response_rx`).
///
/// Returns the result event value, or None if the stream ends unexpectedly.
pub async fn proxy_query_session(
    session: &mut EngineSession,
    user_msg: &serde_json::Value,
) -> Option<serde_json::Value> {
    // Drain any pending control_requests before writing the user message.
    // This prevents the inner CLI from reading a user message on stdin when
    // it's still expecting a control_response during MCP initialization.
    drain_pending_messages(session).await;

    // Send user message to inner Claude
    let cli = session.cli_mut()?;
    if let Err(e) = cli.write(&user_msg.to_string()).await {
        tracing::warn!("Failed to write to inner Claude: {e}");
        return None;
    }

    // Read and forward until result
    loop {
        let msg = session.cli_mut()?.read_message().await?;

        let msg_type = msg
            .get("type")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("");

        match msg_type {
            "control_request" => {
                // Relay control_request to outer client via stdout
                events::emit_raw(&msg);

                // Wait for the outer client to send a control_response
                let cr_rx = session.control_response_rx_mut();
                if let Some(response) = cr_rx.recv().await {
                    if let Some(cli) = session.cli_mut() {
                        let _ = cli.write(&response.to_string()).await;
                    }
                } else {
                    // Channel closed — deny
                    let request_id = msg
                        .get("request_id")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or("");
                    let deny = serde_json::json!({
                        "type": "control_response",
                        "response": {
                            "subtype": "permissions_response",
                            "request_id": request_id,
                            "response": {"allowed": false}
                        }
                    });
                    if let Some(cli) = session.cli_mut() {
                        let _ = cli.write(&deny.to_string()).await;
                    }
                }
            }

            "result" => {
                events::emit_raw(&msg);
                return Some(msg);
            }

            _ => {
                events::emit_raw(&msg);
            }
        }
    }
}

/// Check if a user message content starts with `/` followed by a potential
/// flowchart command name.
pub fn extract_command_name(msg: &serde_json::Value) -> Option<(String, String)> {
    let content = msg
        .get("message")
        .and_then(|m| m.get("content"))
        .and_then(serde_json::Value::as_str)?;

    let trimmed = content.trim();
    if !trimmed.starts_with('/') {
        return None;
    }

    let parts: Vec<&str> = trimmed[1..].splitn(2, ' ').collect();
    let name = parts[0].to_string();
    let args = parts.get(1).copied().unwrap_or("").to_string();

    if name.is_empty() {
        return None;
    }

    debug!("Detected potential flowchart command: /{name} {args}");
    Some((name, args))
}

/// Drain any buffered messages from the inner CLI, handling `control_request`s.
///
/// Uses a short timeout (100ms) — only drains what's already in the buffer.
/// This is a safety net for `control_request`s that arrived after the startup
/// drain completed but before the first user message.
async fn drain_pending_messages(session: &mut EngineSession) {
    while let Some(cli) = session.cli_mut() {
        let result = tokio::time::timeout(
            std::time::Duration::from_millis(100),
            cli.read_message(),
        )
        .await;

        match result {
            Ok(Some(msg)) => {
                let msg_type = msg
                    .get("type")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or("");

                if msg_type == "control_request" {
                    debug!("Pre-query drain: control_request");
                    events::emit_raw(&msg);

                    if let Some(response) = session.control_response_rx_mut().recv().await
                        && let Some(cli) = session.cli_mut()
                    {
                        let _ = cli.write(&response.to_string()).await;
                    }
                } else {
                    debug!("Pre-query drain: forwarding {msg_type}");
                    events::emit_raw(&msg);
                }
            }
            Ok(None) | Err(_) => break,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn user_msg(content: &str) -> serde_json::Value {
        serde_json::json!({
            "type": "user",
            "message": {
                "role": "user",
                "content": content
            }
        })
    }

    #[test]
    fn extract_command_basic() {
        let msg = user_msg("/story dragons");
        let (name, args) = extract_command_name(&msg).unwrap();
        assert_eq!(name, "story");
        assert_eq!(args, "dragons");
    }

    #[test]
    fn extract_command_no_args() {
        let msg = user_msg("/help");
        let (name, args) = extract_command_name(&msg).unwrap();
        assert_eq!(name, "help");
        assert_eq!(args, "");
    }

    #[test]
    fn extract_command_multiple_args() {
        let msg = user_msg("/story dragons in space");
        let (name, args) = extract_command_name(&msg).unwrap();
        assert_eq!(name, "story");
        assert_eq!(args, "dragons in space");
    }

    #[test]
    fn extract_command_with_leading_whitespace() {
        let msg = user_msg("  /story dragons");
        let (name, args) = extract_command_name(&msg).unwrap();
        assert_eq!(name, "story");
        assert_eq!(args, "dragons");
    }

    #[test]
    fn extract_command_plain_text_returns_none() {
        let msg = user_msg("tell me about dragons");
        assert!(extract_command_name(&msg).is_none());
    }

    #[test]
    fn extract_command_just_slash_returns_none() {
        let msg = user_msg("/");
        assert!(extract_command_name(&msg).is_none());
    }

    #[test]
    fn extract_command_no_content_returns_none() {
        let msg = serde_json::json!({"type": "user"});
        assert!(extract_command_name(&msg).is_none());
    }

    #[test]
    fn extract_command_non_string_content_returns_none() {
        let msg = serde_json::json!({
            "type": "user",
            "message": {"content": 42}
        });
        assert!(extract_command_name(&msg).is_none());
    }
}
