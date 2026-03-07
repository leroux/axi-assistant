//! `BridgeTransport` — routes messages through a process event queue.
//!
//! Implements the transport interface for Claude CLI communication.
//! For reconnecting agents, intercepts the initialize `control_request` and
//! fakes a success response — the CLI is already initialized.
//!
//! This module has NO dependency on procmux. It works with any backend that
//! sends `ProcessEvents` through a tokio mpsc channel.

use tokio::sync::mpsc;
use tracing::debug;

use crate::schema::is_bare_stream_type;
use crate::types::{ProcessEvent, StdoutEvent, ExitEvent};

/// Callback type for stderr output.
pub type StderrCallback = Box<dyn Fn(&str) + Send + Sync>;

/// Function to send stdin to a process.
pub type SendStdinFn =
    Box<dyn Fn(String, serde_json::Value) -> std::pin::Pin<Box<dyn Future<Output = anyhow::Result<()>> + Send>> + Send + Sync>;

/// Function to kill a process.
pub type KillFn =
    Box<dyn Fn(String) -> std::pin::Pin<Box<dyn Future<Output = anyhow::Result<()>> + Send>> + Send + Sync>;

/// `BridgeTransport` routes Claude CLI messages through a process connection.
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
    #[allow(clippy::too_many_arguments)]
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
        if self.reconnecting
            && msg.get("type").and_then(|v| v.as_str()) == Some("control_request")
            && msg
                .get("request")
                .and_then(|r| r.get("subtype"))
                .and_then(|v| v.as_str())
                == Some("initialize")
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

    pub const fn is_ready(&self) -> bool {
        self.ready
    }

    pub const fn cli_exited(&self) -> bool {
        self.cli_exited
    }

    pub const fn exit_code(&self) -> Option<i32> {
        self.exit_code
    }

    pub fn name(&self) -> &str {
        &self.name
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tokio::sync::mpsc;

    fn make_transport(reconnecting: bool) -> (BridgeTransport, mpsc::UnboundedReceiver<(String, serde_json::Value)>) {
        let (event_tx, event_rx) = mpsc::unbounded_channel();
        let (stdin_tx, stdin_rx) = mpsc::unbounded_channel::<(String, serde_json::Value)>();
        let alive = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
        let alive_clone = alive.clone();

        let send_stdin: SendStdinFn = Box::new(move |name, data| {
            let tx = stdin_tx.clone();
            Box::pin(async move {
                tx.send((name, data)).map_err(|_| anyhow::anyhow!("send failed"))?;
                Ok(())
            })
        });

        let kill: KillFn = Box::new(|_name| Box::pin(async { Ok(()) }));
        let is_alive = Box::new(move || alive_clone.load(std::sync::atomic::Ordering::SeqCst));

        let transport = BridgeTransport::new(
            "test-agent".to_string(),
            event_rx,
            event_tx,
            send_stdin,
            kill,
            is_alive,
            reconnecting,
            None,
        );

        (transport, stdin_rx)
    }

    #[tokio::test]
    async fn initialize_interception_for_reconnecting() {
        let (mut transport, mut stdin_rx) = make_transport(true);

        // Send an initialize control_request
        let init_msg = json!({
            "type": "control_request",
            "request_id": "req-123",
            "request": {
                "subtype": "initialize",
                "data": {}
            }
        });

        transport.write(&init_msg.to_string()).await.unwrap();

        // Should NOT have been forwarded to stdin
        assert!(stdin_rx.try_recv().is_err());

        // Should have injected a fake control_response into the event queue
        let response = transport.read_message().await.unwrap();
        assert_eq!(response["type"], "control_response");
        assert_eq!(response["response"]["subtype"], "success");
        assert_eq!(response["response"]["request_id"], "req-123");

        // After interception, reconnecting should be false
        // Subsequent writes should go through normally
        let normal_msg = json!({"type": "user", "content": "hello"});
        transport.write(&normal_msg.to_string()).await.unwrap();

        let (name, data) = stdin_rx.try_recv().unwrap();
        assert_eq!(name, "test-agent");
        assert_eq!(data["type"], "user");
    }

    #[tokio::test]
    async fn normal_write_forwards_to_stdin() {
        let (mut transport, mut stdin_rx) = make_transport(false);

        let msg = json!({"type": "user", "content": "test"});
        transport.write(&msg.to_string()).await.unwrap();

        let (name, data) = stdin_rx.try_recv().unwrap();
        assert_eq!(name, "test-agent");
        assert_eq!(data["content"], "test");
    }

    #[tokio::test]
    async fn read_message_yields_stdout() {
        let (event_tx, event_rx) = mpsc::unbounded_channel();
        let alive = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
        let alive_clone = alive.clone();

        let transport_tx = event_tx.clone();
        let send_stdin: SendStdinFn = Box::new(|_name, _data| Box::pin(async { Ok(()) }));
        let kill: KillFn = Box::new(|_name| Box::pin(async { Ok(()) }));
        let is_alive = Box::new(move || alive_clone.load(std::sync::atomic::Ordering::SeqCst));

        let mut transport = BridgeTransport::new(
            "test".to_string(), event_rx, event_tx, send_stdin, kill, is_alive, false, None,
        );

        // Send a stdout event (use "result" type — bare stream types are deduped/dropped)
        transport_tx.send(ProcessEvent::Stdout(StdoutEvent {
            name: "test".to_string(),
            data: json!({"type": "result", "cost_usd": 0.01}),
        })).ok();

        let msg = transport.read_message().await.unwrap();
        assert_eq!(msg["type"], "result");
        assert_eq!(msg["cost_usd"], 0.01);
    }

    #[tokio::test]
    async fn read_message_returns_none_on_exit() {
        let (event_tx, event_rx) = mpsc::unbounded_channel();
        let alive = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
        let alive_clone = alive.clone();

        let send_stdin: SendStdinFn = Box::new(|_name, _data| Box::pin(async { Ok(()) }));
        let kill: KillFn = Box::new(|_name| Box::pin(async { Ok(()) }));
        let is_alive = Box::new(move || alive_clone.load(std::sync::atomic::Ordering::SeqCst));

        let mut transport = BridgeTransport::new(
            "test".to_string(), event_rx, event_tx.clone(), send_stdin, kill, is_alive, false, None,
        );

        // Send exit event
        event_tx.send(ProcessEvent::Exit(ExitEvent {
            name: "test".to_string(),
            code: Some(0),
        })).ok();

        let msg = transport.read_message().await;
        assert!(msg.is_none());
        assert!(transport.cli_exited());
        assert_eq!(transport.exit_code(), Some(0));
    }

    #[tokio::test]
    async fn stop_kills_and_marks_exited() {
        let (event_tx, event_rx) = mpsc::unbounded_channel();
        let alive = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
        let alive_clone = alive.clone();

        let send_stdin: SendStdinFn = Box::new(|_name, _data| Box::pin(async { Ok(()) }));
        let kill: KillFn = Box::new(|_name| Box::pin(async { Ok(()) }));
        let is_alive = Box::new(move || alive_clone.load(std::sync::atomic::Ordering::SeqCst));

        let mut transport = BridgeTransport::new(
            "test".to_string(), event_rx, event_tx, send_stdin, kill, is_alive, false, None,
        );

        transport.stop().await;
        assert!(transport.cli_exited());

        // Second stop is a no-op
        transport.stop().await;
    }
}
