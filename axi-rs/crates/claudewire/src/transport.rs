//! BridgeTransport — routes messages through a process event queue.
//!
//! Implements the transport interface for Claude CLI communication.
//! For reconnecting agents, intercepts the initialize control_request and
//! fakes a success response — the CLI is already initialized.
//!
//! This module has NO dependency on procmux. It works with any backend that
//! sends ProcessEvents through a tokio mpsc channel.

use tokio::sync::mpsc;
use tracing::debug;

use crate::schema::is_bare_stream_type;
use crate::types::*;

/// Callback type for stderr output.
pub type StderrCallback = Box<dyn Fn(&str) + Send + Sync>;

/// Function to send stdin to a process.
pub type SendStdinFn =
    Box<dyn Fn(String, serde_json::Value) -> std::pin::Pin<Box<dyn std::future::Future<Output = anyhow::Result<()>> + Send>> + Send + Sync>;

/// Function to kill a process.
pub type KillFn =
    Box<dyn Fn(String) -> std::pin::Pin<Box<dyn std::future::Future<Output = anyhow::Result<()>> + Send>> + Send + Sync>;

/// BridgeTransport routes Claude CLI messages through a process connection.
pub struct BridgeTransport {
    name: String,
    reconnecting: bool,
    stderr_callback: Option<StderrCallback>,
    rx: Option<mpsc::UnboundedReceiver<ProcessEvent>>,
    tx: Option<mpsc::UnboundedSender<ProcessEvent>>,
    send_stdin: SendStdinFn,
    kill: KillFn,
    is_alive: Box<dyn Fn() -> bool + Send + Sync>,
    ready: bool,
    cli_exited: bool,
    exit_code: Option<i32>,
}

impl BridgeTransport {
    pub fn new(
        name: String,
        rx: mpsc::UnboundedReceiver<ProcessEvent>,
        tx: mpsc::UnboundedSender<ProcessEvent>,
        send_stdin: SendStdinFn,
        kill: KillFn,
        is_alive: Box<dyn Fn() -> bool + Send + Sync>,
        reconnecting: bool,
        stderr_callback: Option<StderrCallback>,
    ) -> Self {
        Self {
            name,
            reconnecting,
            stderr_callback,
            rx: Some(rx),
            tx: Some(tx),
            send_stdin,
            kill,
            is_alive,
            ready: true,
            cli_exited: false,
            exit_code: None,
        }
    }

    /// Write data to the CLI's stdin.
    pub async fn write(&mut self, data: &str) -> anyhow::Result<()> {
        if !(self.is_alive)() {
            anyhow::bail!("Process connection is dead");
        }

        let msg: serde_json::Value = serde_json::from_str(data)?;

        // Intercept initialize for reconnecting agents — fake success
        if self.reconnecting {
            if let Some("control_request") = msg.get("type").and_then(|v| v.as_str()) {
                if let Some("initialize") = msg
                    .get("request")
                    .and_then(|r| r.get("subtype"))
                    .and_then(|v| v.as_str())
                {
                    let request_id = msg
                        .get("request_id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let fake_response = serde_json::json!({
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": request_id,
                            "response": {},
                        },
                    });
                    if let Some(tx) = &self.tx {
                        tx.send(ProcessEvent::Stdout(StdoutEvent {
                            name: self.name.clone(),
                            data: fake_response,
                        }))
                        .ok();
                    }
                    self.reconnecting = false;
                    return Ok(());
                }
            }
        }

        // Inject OTel trace context (placeholder — will wire up later)
        // For now, just send the message as-is
        (self.send_stdin)(self.name.clone(), msg).await
    }

    /// Async iterator yielding parsed JSON dicts from CLI stdout.
    /// Returns None when the stream ends.
    pub async fn read_message(&mut self) -> Option<serde_json::Value> {
        let rx = self.rx.as_mut()?;
        if !(self.is_alive)() && !self.cli_exited {
            return None;
        }

        loop {
            let event = rx.recv().await?;

            // stop() was called — discard buffered messages
            if self.cli_exited {
                return None;
            }

            match event {
                ProcessEvent::Stdout(stdout) => {
                    let msg_type = stdout
                        .data
                        .get("type")
                        .and_then(|v| v.as_str())
                        .unwrap_or("?");

                    // Drop bare duplicate stream events
                    if is_bare_stream_type(msg_type) {
                        debug!(
                            "[read][{}] dropping bare duplicate type={}",
                            self.name, msg_type
                        );
                        continue;
                    }

                    debug!("[read][{}] yielding stdout type={}", self.name, msg_type);
                    return Some(stdout.data);
                }
                ProcessEvent::Stderr(stderr) => {
                    debug!("[read][{}] stderr: {:.200}", self.name, stderr.text);
                    if let Some(cb) = &self.stderr_callback {
                        cb(&stderr.text);
                    }
                }
                ProcessEvent::Exit(exit) => {
                    debug!("[read][{}] exit code={:?}", self.name, exit.code);
                    self.cli_exited = true;
                    self.exit_code = exit.code;
                    return None;
                }
            }
        }
    }

    /// Immediately terminate the read stream and kill the CLI process.
    pub async fn stop(&mut self) {
        if self.cli_exited {
            return;
        }
        self.cli_exited = true;
        debug!(
            "[stop][{}] injecting ExitEvent and scheduling background kill",
            self.name
        );

        // Inject ExitEvent to unblock read_message()
        if let Some(tx) = &self.tx {
            tx.send(ProcessEvent::Exit(ExitEvent {
                name: self.name.clone(),
                code: None,
            }))
            .ok();
        }

        // Kill the process (best effort)
        let name = self.name.clone();
        if let Err(e) = (self.kill)(name).await {
            debug!(
                "[stop][{}] kill failed (process may already be dead): {}",
                self.name, e
            );
        }
    }

    /// Kill the CLI process and clean up.
    pub async fn close(&mut self) {
        if self.ready && !self.cli_exited {
            let name = self.name.clone();
            (self.kill)(name).await.ok();
        }
        self.ready = false;
    }

    pub fn is_ready(&self) -> bool {
        self.ready
    }

    pub fn cli_exited(&self) -> bool {
        self.cli_exited
    }

    pub fn exit_code(&self) -> Option<i32> {
        self.exit_code
    }

    pub fn name(&self) -> &str {
        &self.name
    }
}
