//! `CliSession` — manages a Claude CLI subprocess and its IO channels.
//!
//! Two construction paths:
//! - `CliSession::spawn(config)` — builds CLI args from a `Config`, spawns the process
//! - `CliSession::from_command(cmd)` — takes a pre-built `tokio::process::Command`
//!
//! Both wire stdin/stdout/stderr through the same channel-based read/write API.
//! For reconnecting agents, `CliSession::new()` accepts raw channels (used by procmux).

use std::process::Stdio;

use nix::sys::signal::{self, Signal};
use nix::unistd::Pid;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc;
use tracing::debug;

use crate::config::Config;
use crate::schema::is_bare_stream_type;
use crate::types::{ExitEvent, ProcessEvent, StdoutEvent};

/// Callback type for stderr output.
pub type StderrCallback = Box<dyn Fn(&str) + Send + Sync>;

/// Function to send stdin to a process.
pub type SendStdinFn =
    Box<dyn Fn(String, serde_json::Value) -> std::pin::Pin<Box<dyn Future<Output = anyhow::Result<()>> + Send>> + Send + Sync>;

/// Function to kill a process.
pub type KillFn =
    Box<dyn Fn(String) -> std::pin::Pin<Box<dyn Future<Output = anyhow::Result<()>> + Send>> + Send + Sync>;

/// `CliSession` manages a Claude CLI process and routes messages through channels.
/// Function to send a signal to a process.
pub type SignalFn =
    Box<dyn Fn(Signal) -> bool + Send + Sync>;

pub struct CliSession {
    name: String,
    reconnecting: bool,
    stderr_callback: Option<StderrCallback>,
    rx: Option<mpsc::UnboundedReceiver<ProcessEvent>>,
    tx: Option<mpsc::UnboundedSender<ProcessEvent>>,
    send_stdin: SendStdinFn,
    kill: KillFn,
    signal: SignalFn,
    is_alive: Box<dyn Fn() -> bool + Send + Sync>,
    ready: bool,
    cli_exited: bool,
    exit_code: Option<i32>,
}

impl CliSession {
    /// Create a `CliSession` from raw channels (for procmux or other backends).
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
        // Procmux-backed sessions have no direct PID — signal is a no-op
        let signal: SignalFn = Box::new(|_| false);
        Self {
            name,
            reconnecting,
            stderr_callback,
            rx: Some(rx),
            tx: Some(tx),
            send_stdin,
            kill,
            signal,
            is_alive,
            ready: true,
            cli_exited: false,
            exit_code: None,
        }
    }

    /// Spawn a Claude CLI process from a `Config`.
    ///
    /// Builds CLI args and env vars from the config, then delegates to `from_command`.
    pub fn spawn(
        config: &Config,
        name: String,
        stderr_callback: Option<StderrCallback>,
    ) -> anyhow::Result<Self> {
        let args = config.to_cli_args();
        let env = config.to_env();

        let mut cmd = Command::new(&args[0]);
        cmd.args(&args[1..]);
        cmd.envs(env);
        // Prevent Claude CLI from detecting nested sessions
        cmd.env_remove("CLAUDECODE");

        Self::from_command(cmd, name, stderr_callback)
    }

    /// Create a `CliSession` from a pre-built `tokio::process::Command`.
    ///
    /// Spawns the process, wires stdin/stdout/stderr to channels.
    pub fn from_command(
        mut cmd: Command,
        name: String,
        stderr_callback: Option<StderrCallback>,
    ) -> anyhow::Result<Self> {
        cmd.stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        let mut child = cmd.spawn()?;

        let child_stdin = child.stdin.take().expect("stdin was piped");
        let child_stdout = child.stdout.take().expect("stdout was piped");
        let child_stderr = child.stderr.take().expect("stderr was piped");

        let (event_tx, event_rx) = mpsc::unbounded_channel();
        let (stdin_tx, mut stdin_rx) = mpsc::unbounded_channel::<Vec<u8>>();

        // Stdout reader task — parse NDJSON lines into ProcessEvent::Stdout
        let stdout_tx = event_tx.clone();
        let stdout_name = name.clone();
        tokio::spawn(async move {
            let reader = BufReader::new(child_stdout);
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if line.is_empty() {
                    continue;
                }
                match serde_json::from_str::<serde_json::Value>(&line) {
                    Ok(data) => {
                        stdout_tx
                            .send(ProcessEvent::Stdout(StdoutEvent {
                                name: stdout_name.clone(),
                                data,
                            }))
                            .ok();
                    }
                    Err(e) => {
                        debug!(
                            "[session][{}] ignoring non-JSON stdout: {:.100} ({})",
                            stdout_name, line, e
                        );
                    }
                }
            }
        });

        // Stderr reader task — send ProcessEvent::Stderr
        let stderr_tx = event_tx.clone();
        let stderr_name = name.clone();
        tokio::spawn(async move {
            let reader = BufReader::new(child_stderr);
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                stderr_tx
                    .send(ProcessEvent::Stderr(crate::types::StderrEvent {
                        name: stderr_name.clone(),
                        text: line,
                    }))
                    .ok();
            }
        });

        // Stdin writer task — forward bytes from channel to process stdin
        tokio::spawn(async move {
            let mut writer = child_stdin;
            while let Some(data) = stdin_rx.recv().await {
                if writer.write_all(&data).await.is_err() {
                    break;
                }
                if writer.flush().await.is_err() {
                    break;
                }
            }
        });

        // Build the pid-based is_alive check
        let pid = child.id();
        let is_alive: Box<dyn Fn() -> bool + Send + Sync> = Box::new(move || {
            if let Some(pid) = pid {
                // Signal 0 checks if process exists without sending a signal
                signal::kill(Pid::from_raw(pid.cast_signed()), None).is_ok()
            } else {
                false
            }
        });

        // Build the stdin sender
        let send_stdin: SendStdinFn = Box::new(move |_name, data| {
            let tx = stdin_tx.clone();
            Box::pin(async move {
                let mut bytes = serde_json::to_vec(&data)?;
                bytes.push(b'\n');
                tx.send(bytes).map_err(|_| anyhow::anyhow!("stdin channel closed"))?;
                Ok(())
            })
        });

        // Build the signal function — send any signal to the process
        let signal_pid = pid;
        let signal_fn: SignalFn = Box::new(move |sig| {
            if let Some(pid) = signal_pid {
                signal::kill(Pid::from_raw(pid.cast_signed()), sig).is_ok()
            } else {
                false
            }
        });

        // Build the kill function
        let kill_pid = pid;
        let kill: KillFn = Box::new(move |_name| {
            Box::pin(async move {
                if let Some(pid) = kill_pid {
                    signal::kill(Pid::from_raw(pid.cast_signed()), Signal::SIGTERM).ok();
                }
                Ok(())
            })
        });

        // Spawn a wait task that owns the child and sends ExitEvent
        let wait_tx = event_tx.clone();
        let wait_name = name.clone();
        let mut owned_child = child;
        tokio::spawn(async move {
            let status = owned_child.wait().await;
            let code = status.ok().and_then(|s| s.code());
            wait_tx
                .send(ProcessEvent::Exit(ExitEvent {
                    name: wait_name,
                    code,
                }))
                .ok();
        });

        Ok(Self {
            name,
            reconnecting: false,
            stderr_callback,
            rx: Some(event_rx),
            tx: Some(event_tx),
            send_stdin,
            kill,
            signal: signal_fn,
            is_alive,
            ready: true,
            cli_exited: false,
            exit_code: None,
        })
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

        (self.send_stdin)(self.name.clone(), msg).await
    }

    /// Async iterator yielding parsed JSON dicts from CLI stdout.
    /// Returns None when the stream ends.
    pub async fn read_message(&mut self) -> Option<serde_json::Value> {
        let rx = self.rx.as_mut()?;

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

    /// Send a signal to the underlying process. Returns `true` if the signal was sent.
    /// For procmux-backed sessions this is a no-op (returns `false`).
    pub fn send_signal(&self, sig: Signal) -> bool {
        (self.signal)(sig)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tokio::sync::mpsc;

    fn make_session(reconnecting: bool) -> (CliSession, mpsc::UnboundedReceiver<(String, serde_json::Value)>) {
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

        let session = CliSession::new(
            "test-agent".to_string(),
            event_rx,
            event_tx,
            send_stdin,
            kill,
            is_alive,
            reconnecting,
            None,
        );

        (session, stdin_rx)
    }

    #[tokio::test]
    async fn initialize_interception_for_reconnecting() {
        let (mut session, mut stdin_rx) = make_session(true);

        // Send an initialize control_request
        let init_msg = json!({
            "type": "control_request",
            "request_id": "req-123",
            "request": {
                "subtype": "initialize",
                "data": {}
            }
        });

        session.write(&init_msg.to_string()).await.unwrap();

        // Should NOT have been forwarded to stdin
        assert!(stdin_rx.try_recv().is_err());

        // Should have injected a fake control_response into the event queue
        let response = session.read_message().await.unwrap();
        assert_eq!(response["type"], "control_response");
        assert_eq!(response["response"]["subtype"], "success");
        assert_eq!(response["response"]["request_id"], "req-123");

        // After interception, reconnecting should be false
        // Subsequent writes should go through normally
        let normal_msg = json!({"type": "user", "content": "hello"});
        session.write(&normal_msg.to_string()).await.unwrap();

        let (name, data) = stdin_rx.try_recv().unwrap();
        assert_eq!(name, "test-agent");
        assert_eq!(data["type"], "user");
    }

    #[tokio::test]
    async fn normal_write_forwards_to_stdin() {
        let (mut session, mut stdin_rx) = make_session(false);

        let msg = json!({"type": "user", "content": "test"});
        session.write(&msg.to_string()).await.unwrap();

        let (name, data) = stdin_rx.try_recv().unwrap();
        assert_eq!(name, "test-agent");
        assert_eq!(data["content"], "test");
    }

    #[tokio::test]
    async fn read_message_yields_stdout() {
        let (event_tx, event_rx) = mpsc::unbounded_channel();
        let alive = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
        let alive_clone = alive.clone();

        let session_tx = event_tx.clone();
        let send_stdin: SendStdinFn = Box::new(|_name, _data| Box::pin(async { Ok(()) }));
        let kill: KillFn = Box::new(|_name| Box::pin(async { Ok(()) }));
        let is_alive = Box::new(move || alive_clone.load(std::sync::atomic::Ordering::SeqCst));

        let mut session = CliSession::new(
            "test".to_string(), event_rx, event_tx, send_stdin, kill, is_alive, false, None,
        );

        // Send a stdout event (use "result" type — bare stream types are deduped/dropped)
        session_tx.send(ProcessEvent::Stdout(StdoutEvent {
            name: "test".to_string(),
            data: json!({"type": "result", "cost_usd": 0.01}),
        })).ok();

        let msg = session.read_message().await.unwrap();
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

        let mut session = CliSession::new(
            "test".to_string(), event_rx, event_tx.clone(), send_stdin, kill, is_alive, false, None,
        );

        // Send exit event
        event_tx.send(ProcessEvent::Exit(ExitEvent {
            name: "test".to_string(),
            code: Some(0),
        })).ok();

        let msg = session.read_message().await;
        assert!(msg.is_none());
        assert!(session.cli_exited());
        assert_eq!(session.exit_code(), Some(0));
    }

    #[tokio::test]
    async fn stop_kills_and_marks_exited() {
        let (event_tx, event_rx) = mpsc::unbounded_channel();
        let alive = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
        let alive_clone = alive.clone();

        let send_stdin: SendStdinFn = Box::new(|_name, _data| Box::pin(async { Ok(()) }));
        let kill: KillFn = Box::new(|_name| Box::pin(async { Ok(()) }));
        let is_alive = Box::new(move || alive_clone.load(std::sync::atomic::Ordering::SeqCst));

        let mut session = CliSession::new(
            "test".to_string(), event_rx, event_tx, send_stdin, kill, is_alive, false, None,
        );

        session.stop().await;
        assert!(session.cli_exited());

        // Second stop is a no-op
        session.stop().await;
    }
}
