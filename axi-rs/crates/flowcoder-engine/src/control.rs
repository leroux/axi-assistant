//! Background control reader — processes `engine_control` commands during flowchart execution.
//!
//! Spawned when a flowchart starts, reads from the main message channel,
//! and sets pause/cancel flags. Non-control messages are buffered for
//! processing after the flowchart completes.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use tracing::debug;

use crate::engine_protocol::EngineProtocol;
use crate::events::{self, EngineEvent};

/// State shared between the control reader task and the executor.
pub struct ControlState {
    pub pause_flag: Arc<AtomicBool>,
    pub pause_signal: Arc<tokio::sync::Notify>,
    pub cancel: CancellationToken,
}

impl ControlState {
    pub fn new(cancel: CancellationToken) -> Self {
        Self {
            pause_flag: Arc::new(AtomicBool::new(false)),
            pause_signal: Arc::new(tokio::sync::Notify::new()),
            cancel,
        }
    }
}

/// Result of the control reader task: buffered messages + the original receiver.
pub struct ControlReaderResult {
    pub buffered: Vec<serde_json::Value>,
    pub message_rx: mpsc::UnboundedReceiver<serde_json::Value>,
}

/// Spawn a background task that reads `engine_control` commands from the message
/// channel and sets the appropriate flags.
///
/// The task owns `message_rx` during execution and returns it when `done` fires.
/// Non-control messages are buffered and returned.
pub fn spawn_control_reader(
    mut message_rx: mpsc::UnboundedReceiver<serde_json::Value>,
    control: &ControlState,
    protocol: &EngineProtocol,
    done: CancellationToken,
) -> tokio::task::JoinHandle<ControlReaderResult> {
    let pause_flag = control.pause_flag.clone();
    let pause_signal = control.pause_signal.clone();
    let cancel = control.cancel.clone();

    // Snapshot protocol state for status queries
    let total_blocks = protocol.total_blocks();

    tokio::spawn(async move {
        let mut buffered = Vec::new();

        loop {
            tokio::select! {
                () = done.cancelled() => break,
                msg = message_rx.recv() => {
                    let msg = match msg {
                        Some(m) => m,
                        None => break,
                    };

                    let msg_type = msg
                        .get("type")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or("");

                    if msg_type == "engine_control" {
                        let command = msg
                            .get("command")
                            .and_then(serde_json::Value::as_str)
                            .unwrap_or("");

                        match command {
                            "pause" => {
                                debug!("Control: pause");
                                pause_flag.store(true, Ordering::Relaxed);
                            }
                            "resume" => {
                                debug!("Control: resume");
                                pause_flag.store(false, Ordering::Relaxed);
                                pause_signal.notify_one();
                            }
                            "cancel" => {
                                debug!("Control: cancel");
                                cancel.cancel();
                            }
                            "status" => {
                                let paused = pause_flag.load(Ordering::Relaxed);
                                events::emit(&EngineEvent::EngineStatus {
                                    mode: "flowchart".into(),
                                    current_block: None,
                                    blocks_done: 0,
                                    total_blocks,
                                    paused,
                                });
                            }
                            _ => {
                                debug!("Unknown engine_control command: {command}");
                            }
                        }
                    } else {
                        // Non-control message — buffer for after flowchart completes
                        buffered.push(msg);
                    }
                }
            }
        }

        ControlReaderResult {
            buffered,
            message_rx,
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn pause_sets_flag() {
        let cancel = CancellationToken::new();
        let control = ControlState::new(cancel);
        let protocol = EngineProtocol::new();
        let done = CancellationToken::new();

        let (tx, rx) = mpsc::unbounded_channel();
        let handle = spawn_control_reader(rx, &control, &protocol, done.clone());

        // Send pause command
        tx.send(serde_json::json!({"type": "engine_control", "command": "pause"}))
            .unwrap();

        // Give the task time to process
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;

        assert!(control.pause_flag.load(Ordering::Relaxed));

        // Send resume
        tx.send(serde_json::json!({"type": "engine_control", "command": "resume"}))
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;

        assert!(!control.pause_flag.load(Ordering::Relaxed));

        done.cancel();
        let result = handle.await.unwrap();
        assert!(result.buffered.is_empty());
    }

    #[tokio::test]
    async fn cancel_fires_token() {
        let cancel = CancellationToken::new();
        let control = ControlState::new(cancel.clone());
        let protocol = EngineProtocol::new();
        let done = CancellationToken::new();

        let (tx, rx) = mpsc::unbounded_channel();
        let handle = spawn_control_reader(rx, &control, &protocol, done.clone());

        tx.send(serde_json::json!({"type": "engine_control", "command": "cancel"}))
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;

        assert!(cancel.is_cancelled());

        done.cancel();
        handle.await.unwrap();
    }

    #[tokio::test]
    async fn non_control_messages_buffered() {
        let cancel = CancellationToken::new();
        let control = ControlState::new(cancel);
        let protocol = EngineProtocol::new();
        let done = CancellationToken::new();

        let (tx, rx) = mpsc::unbounded_channel();
        let handle = spawn_control_reader(rx, &control, &protocol, done.clone());

        // Send a mix of control and non-control messages
        tx.send(serde_json::json!({"type": "user", "message": {"content": "hello"}}))
            .unwrap();
        tx.send(serde_json::json!({"type": "engine_control", "command": "pause"}))
            .unwrap();
        tx.send(serde_json::json!({"type": "user", "message": {"content": "world"}}))
            .unwrap();

        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        done.cancel();

        let result = handle.await.unwrap();
        assert_eq!(result.buffered.len(), 2);
        assert_eq!(result.buffered[0]["message"]["content"], "hello");
        assert_eq!(result.buffered[1]["message"]["content"], "world");
        assert!(control.pause_flag.load(Ordering::Relaxed));
    }

    #[tokio::test]
    async fn receiver_returned_after_done() {
        let cancel = CancellationToken::new();
        let control = ControlState::new(cancel);
        let protocol = EngineProtocol::new();
        let done = CancellationToken::new();

        let (tx, rx) = mpsc::unbounded_channel();
        let handle = spawn_control_reader(rx, &control, &protocol, done.clone());

        // Send a message, then signal done
        tx.send(serde_json::json!({"type": "user", "message": {"content": "buffered"}}))
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;

        // Send another message that won't be read by the control reader
        // (it will be in the returned receiver)
        done.cancel();
        // Small delay to let task finish before sending
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        let _ = tx.send(serde_json::json!({"type": "user", "message": {"content": "after_done"}}));

        let result = handle.await.unwrap();
        assert_eq!(result.buffered.len(), 1);

        // The receiver should still be usable
        let mut rx = result.message_rx;
        // Messages sent after done.cancel() might or might not be in the receiver
        // depending on timing, so just verify it doesn't panic
        let _ = rx.try_recv();
    }
}
