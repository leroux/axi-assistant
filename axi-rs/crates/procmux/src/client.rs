//! Procmux client — connects to the procmux server over a Unix socket.
//!
//! Runs a demux loop that routes incoming messages to per-process queues
//! and command responses to a dedicated queue.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;

use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::sync::{mpsc, Mutex};
use tracing::{debug, info, warn};

use crate::protocol::{StdoutMsg, StderrMsg, ExitMsg, ResultMsg, ServerMsg, ClientMsg, CmdMsg, StdinMsg};

/// Message types that flow through per-process queues.
#[derive(Debug)]
pub enum ProcessMsg {
    Stdout(StdoutMsg),
    Stderr(StderrMsg),
    Exit(ExitMsg),
    /// Connection lost sentinel.
    ConnectionLost,
}

pub struct ProcmuxConnection {
    writer: Arc<Mutex<tokio::net::unix::OwnedWriteHalf>>,
    process_queues: Arc<Mutex<HashMap<String, mpsc::UnboundedSender<ProcessMsg>>>>,
    _cmd_response_tx: mpsc::UnboundedSender<ResultMsg>,
    cmd_response_rx: Arc<Mutex<mpsc::UnboundedReceiver<ResultMsg>>>,
    cmd_lock: Arc<Mutex<()>>,
    demux_task: tokio::task::JoinHandle<()>,
    closed: Arc<std::sync::atomic::AtomicBool>,
}

impl ProcmuxConnection {
    /// Connect to a procmux server at the given Unix socket path.
    pub async fn connect(path: impl AsRef<Path>) -> anyhow::Result<Self> {
        let stream = UnixStream::connect(path.as_ref()).await?;
        let (read_half, write_half) = stream.into_split();
        let writer = Arc::new(Mutex::new(write_half));
        let process_queues: Arc<Mutex<HashMap<String, mpsc::UnboundedSender<ProcessMsg>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let (cmd_response_tx, cmd_response_rx) = mpsc::unbounded_channel();
        let cmd_response_rx = Arc::new(Mutex::new(cmd_response_rx));
        let closed = Arc::new(std::sync::atomic::AtomicBool::new(false));

        let demux_queues = process_queues.clone();
        let demux_cmd_tx = cmd_response_tx.clone();
        let demux_closed = closed.clone();

        let demux_task = tokio::spawn(async move {
            let reader = BufReader::new(read_half);
            let mut lines = reader.lines();

            loop {
                match lines.next_line().await {
                    Ok(Some(line)) => {
                        let msg: ServerMsg = match serde_json::from_str(&line) {
                            Ok(m) => m,
                            Err(e) => {
                                debug!("[demux] parse failed: {}", e);
                                continue;
                            }
                        };

                        match msg {
                            ServerMsg::Result(result) => {
                                debug!("[demux] routed ResultMsg (ok={})", result.ok);
                                demux_cmd_tx.send(result).ok();
                            }
                            ServerMsg::Stdout(stdout) => {
                                let queues = demux_queues.lock().await;
                                if let Some(tx) = queues.get(&stdout.name) {
                                    tx.send(ProcessMsg::Stdout(stdout)).ok();
                                } else {
                                    warn!(
                                        "[demux] dropped stdout for unregistered process '{}'",
                                        stdout.name
                                    );
                                }
                            }
                            ServerMsg::Stderr(stderr) => {
                                let queues = demux_queues.lock().await;
                                if let Some(tx) = queues.get(&stderr.name) {
                                    tx.send(ProcessMsg::Stderr(stderr)).ok();
                                } else {
                                    warn!(
                                        "[demux] dropped stderr for unregistered process '{}'",
                                        stderr.name
                                    );
                                }
                            }
                            ServerMsg::Exit(exit) => {
                                let queues = demux_queues.lock().await;
                                if let Some(tx) = queues.get(&exit.name) {
                                    tx.send(ProcessMsg::Exit(exit)).ok();
                                }
                            }
                        }
                    }
                    Ok(None) => {
                        info!("Procmux connection lost (EOF)");
                        break;
                    }
                    Err(e) => {
                        info!("Procmux connection error: {}", e);
                        break;
                    }
                }
            }

            // Signal all process queues that the connection is lost
            demux_closed.store(true, std::sync::atomic::Ordering::SeqCst);
            let queues = demux_queues.lock().await;
            for tx in queues.values() {
                tx.send(ProcessMsg::ConnectionLost).ok();
            }
        });

        Ok(Self {
            writer,
            process_queues,
            _cmd_response_tx: cmd_response_tx,
            cmd_response_rx,
            cmd_lock: Arc::new(Mutex::new(())),
            demux_task,
            closed,
        })
    }

    pub fn is_alive(&self) -> bool {
        !self.closed.load(std::sync::atomic::Ordering::SeqCst)
    }

    /// Send a command to procmux and wait for the result.
    pub async fn send_command(
        &self,
        cmd: &str,
        name: &str,
        cli_args: Vec<String>,
        env: HashMap<String, String>,
        cwd: Option<String>,
    ) -> anyhow::Result<ResultMsg> {
        let _lock = self.cmd_lock.lock().await;

        let msg = ClientMsg::Cmd(CmdMsg {
            r#type: None,
            cmd: cmd.to_string(),
            name: name.to_string(),
            cli_args,
            env,
            cwd,
        });

        let payload = serde_json::to_string(&msg)? + "\n";
        {
            let mut writer = self.writer.lock().await;
            writer.write_all(payload.as_bytes()).await?;
            writer.flush().await?;
        }

        let mut rx = self.cmd_response_rx.lock().await;
        match tokio::time::timeout(std::time::Duration::from_secs(30), rx.recv()).await {
            Ok(Some(result)) => Ok(result),
            Ok(None) => anyhow::bail!("command response channel closed"),
            Err(_) => anyhow::bail!("command response timed out"),
        }
    }

    /// Send a simple command (no `cli_args/env/cwd`).
    pub async fn send_simple_command(
        &self,
        cmd: &str,
        name: &str,
    ) -> anyhow::Result<ResultMsg> {
        self.send_command(cmd, name, vec![], HashMap::new(), None)
            .await
    }

    /// Send data to a process's stdin via procmux.
    pub async fn send_stdin(
        &self,
        name: &str,
        data: serde_json::Value,
    ) -> anyhow::Result<()> {
        let msg = ClientMsg::Stdin(StdinMsg {
            r#type: None,
            name: name.to_string(),
            data,
        });
        let payload = serde_json::to_string(&msg)? + "\n";
        let mut writer = self.writer.lock().await;
        writer.write_all(payload.as_bytes()).await?;
        writer.flush().await?;
        Ok(())
    }

    /// Register a process and return a receiver for its messages.
    pub async fn register_process(
        &self,
        name: &str,
    ) -> mpsc::UnboundedReceiver<ProcessMsg> {
        let (tx, rx) = mpsc::unbounded_channel();
        let mut queues = self.process_queues.lock().await;
        queues.insert(name.to_string(), tx);
        rx
    }

    /// Unregister a process's message queue.
    pub async fn unregister_process(&self, name: &str) {
        let mut queues = self.process_queues.lock().await;
        queues.remove(name);
    }

    /// Close the connection to procmux.
    pub fn close(self) {
        self.closed
            .store(true, std::sync::atomic::Ordering::SeqCst);
        self.demux_task.abort();
    }
}
