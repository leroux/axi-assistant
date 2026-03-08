//! Stdin message router — classifies incoming JSON into two channels.
//!
//! `control_response` messages go to a dedicated channel so they can reach
//! inner Claude immediately during `EngineSession::query()` without blocking
//! behind queued user messages or `engine_control` commands.
//!
//! Everything else (user messages, `engine_control`) goes to the main channel.

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::mpsc;
use tracing::{debug, warn};

/// Handle for the two channels that the router feeds.
pub struct RouterChannels {
    /// Control responses destined for inner Claude during query.
    pub control_response_rx: mpsc::UnboundedReceiver<serde_json::Value>,
    /// Everything else: user messages, `engine_control` commands.
    pub message_rx: mpsc::UnboundedReceiver<serde_json::Value>,
}

/// Spawn a background task that reads NDJSON from stdin and routes messages.
///
/// Returns the channel handles. The task runs until stdin closes.
pub fn spawn_stdin_router() -> RouterChannels {
    let (control_response_tx, control_response_rx) = mpsc::unbounded_channel();
    let (message_tx, message_rx) = mpsc::unbounded_channel();

    tokio::spawn(async move {
        let stdin = tokio::io::stdin();
        let reader = BufReader::new(stdin);
        let mut lines = reader.lines();

        while let Ok(Some(line)) = lines.next_line().await {
            if line.is_empty() {
                continue;
            }

            let msg: serde_json::Value = match serde_json::from_str(&line) {
                Ok(v) => v,
                Err(e) => {
                    warn!("Ignoring non-JSON stdin: {:.100} ({e})", line);
                    continue;
                }
            };

            let msg_type = msg
                .get("type")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("");

            if msg_type == "control_response" {
                debug!("Router: control_response → control channel");
                let _ = control_response_tx.send(msg);
            } else {
                debug!("Router: {msg_type} → message channel");
                let _ = message_tx.send(msg);
            }
        }

        debug!("Stdin router: EOF, shutting down");
    });

    RouterChannels {
        control_response_rx,
        message_rx,
    }
}
