//! Procmux server — runs as a separate process, managing subprocesses.
//!
//! Spawns named OS subprocesses with stdin/stdout/stderr pipes, multiplexes
//! them over one Unix socket connection, and buffers output when the client
//! is disconnected. Zero intelligence — no knowledge of Claude, agents,
//! sessions, or any semantic layer.

use std::collections::HashMap;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use nix::sys::signal::{self, Signal};
use nix::unistd::Pid;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::process::{Child, Command};
use tokio::sync::{mpsc, Notify};
use tracing::{debug, error, info, warn};

use crate::protocol::{ServerMsg, ClientMsg, StdoutMsg, StderrMsg, ExitMsg, CmdMsg, ResultMsg, StdinMsg};

/// A subprocess managed by procmux.
struct ManagedProcess {
    _name: String,
    child: Child,
    status: ProcessStatus,
    exit_code: Option<i32>,
    buffer: Vec<ServerMsg>,
    subscribed: bool,
    last_stdin_at: Instant,
    last_stdout_at: Instant,
    /// Handles for the stdout/stderr relay tasks.
    stdout_task: Option<tokio::task::JoinHandle<()>>,
    stderr_task: Option<tokio::task::JoinHandle<()>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ProcessStatus {
    Running,
    Exited,
}

impl ManagedProcess {
    fn idle(&self) -> bool {
        self.last_stdout_at >= self.last_stdin_at
    }
}

/// Messages sent from relay tasks to the server's main loop.
enum RelayMsg {
    Stdout { name: String, data: serde_json::Value },
    Stderr { name: String, text: String },
    Exit { name: String, code: Option<i32> },
}

pub struct ProcmuxServer {
    socket_path: PathBuf,
    procs: HashMap<String, ManagedProcess>,
    /// Channel for relay tasks to send messages back to the server.
    relay_tx: mpsc::UnboundedSender<RelayMsg>,
    relay_rx: mpsc::UnboundedReceiver<RelayMsg>,
    /// The currently connected client's write half.
    client_tx: Option<mpsc::UnboundedSender<Vec<u8>>>,
    start_time: Instant,
    stdio_log_dir: PathBuf,
}

impl ProcmuxServer {
    pub fn new(socket_path: impl Into<PathBuf>) -> Self {
        let socket_path = socket_path.into();
        let stdio_log_dir = std::env::var("BRIDGE_STDIO_LOG_DIR").map_or_else(|_| {
                socket_path
                    .parent()
                    .unwrap_or_else(|| Path::new("."))
                    .join("logs")
            }, PathBuf::from);
        std::fs::create_dir_all(&stdio_log_dir).ok();

        let (relay_tx, relay_rx) = mpsc::unbounded_channel();

        Self {
            socket_path,
            procs: HashMap::new(),
            relay_tx,
            relay_rx,
            client_tx: None,
            start_time: Instant::now(),
            stdio_log_dir,
        }
    }

    pub async fn run(mut self) -> anyhow::Result<()> {
        // Clean up stale socket
        if self.socket_path.exists() {
            std::fs::remove_file(&self.socket_path)?;
        }

        let listener = UnixListener::bind(&self.socket_path)?;
        info!("Procmux listening on {}", self.socket_path.display());

        let shutdown = Arc::new(Notify::new());
        let shutdown_clone = shutdown.clone();

        // Signal handler
        tokio::spawn(async move {
            let mut sigterm =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                    .expect("failed to register SIGTERM");
            let mut sigint =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())
                    .expect("failed to register SIGINT");
            tokio::select! {
                _ = sigterm.recv() => info!("Received SIGTERM"),
                _ = sigint.recv() => info!("Received SIGINT"),
            }
            shutdown_clone.notify_one();
        });

        loop {
            tokio::select! {
                accept_result = listener.accept() => {
                    match accept_result {
                        Ok((stream, _)) => self.handle_client(stream).await,
                        Err(e) => error!("Accept error: {}", e),
                    }
                }
                Some(relay) = self.relay_rx.recv() => {
                    self.handle_relay(relay).await;
                }
                () = shutdown.notified() => {
                    info!("Shutting down...");
                    break;
                }
            }
        }

        self.shutdown().await;
        Ok(())
    }

    async fn shutdown(&mut self) {
        info!(
            "Procmux shutting down, killing {} process(es)",
            self.procs.len()
        );
        let names: Vec<String> = self.procs.keys().cloned().collect();
        for name in names {
            self.kill_process(&name).await;
        }
        self.client_tx = None;
        if self.socket_path.exists() {
            std::fs::remove_file(&self.socket_path).ok();
        }
        info!("Procmux shutdown complete");
    }

    async fn handle_client(&mut self, stream: UnixStream) {
        // Drop old client
        if let Some(old_tx) = self.client_tx.take() {
            warn!("New client connected -- dropping previous connection");
            drop(old_tx);
        }
        for mp in self.procs.values_mut() {
            mp.subscribed = false;
        }

        info!("Client connected");

        let (read_half, write_half) = stream.into_split();
        let (tx, mut rx) = mpsc::unbounded_channel::<Vec<u8>>();
        self.client_tx = Some(tx);

        // Writer task: drains the channel and writes to the socket
        tokio::spawn(async move {
            let mut writer = write_half;
            while let Some(data) = rx.recv().await {
                if writer.write_all(&data).await.is_err() {
                    break;
                }
            }
        });

        // Reader: read lines from client
        let reader = BufReader::new(read_half);
        let mut lines = reader.lines();

        // We need to process client lines in the main loop, so we'll
        // forward them via the relay channel using a special variant.
        // But to keep it simpler, we'll use a separate channel for client messages.
        let (client_msg_tx, mut client_msg_rx) = mpsc::unbounded_channel::<String>();

        tokio::spawn(async move {
            while let Ok(Some(line)) = lines.next_line().await {
                if client_msg_tx.send(line).is_err() {
                    break;
                }
            }
        });

        // Process client messages until disconnection
        loop {
            tokio::select! {
                msg = client_msg_rx.recv() => {
                    if let Some(line) = msg { self.handle_client_line(&line).await } else {
                        // Client disconnected
                        self.client_tx = None;
                        for mp in self.procs.values_mut() {
                            mp.subscribed = false;
                        }
                        info!("Client disconnected -- buffering all output");
                        return;
                    }
                }
                Some(relay) = self.relay_rx.recv() => {
                    self.handle_relay(relay).await;
                }
            }
        }
    }

    async fn handle_client_line(&mut self, line: &str) {
        let msg: ClientMsg = match serde_json::from_str(line) {
            Ok(m) => m,
            Err(e) => {
                warn!("Invalid message from client: {:?} -- {}", &line[..200.min(line.len())], e);
                return;
            }
        };

        match msg {
            ClientMsg::Cmd(cmd) => self.handle_command(cmd).await,
            ClientMsg::Stdin(stdin) => self.handle_stdin(stdin).await,
        }
    }

    async fn handle_relay(&mut self, relay: RelayMsg) {
        match relay {
            RelayMsg::Stdout { name, data } => {
                if let Some(mp) = self.procs.get_mut(&name) {
                    mp.last_stdout_at = Instant::now();
                    let msg = ServerMsg::Stdout(StdoutMsg {
                        r#type: None,
                        name: name.clone(),
                        data,
                    });
                    self.relay_or_buffer(&name, msg);
                }
            }
            RelayMsg::Stderr { name, text } => {
                let msg = ServerMsg::Stderr(StderrMsg {
                    r#type: None,
                    name: name.clone(),
                    text,
                });
                self.relay_or_buffer(&name, msg);
            }
            RelayMsg::Exit { name, code } => {
                // Reap the child process to prevent zombies, and get the real exit code.
                let final_code = if let Some(mp) = self.procs.get_mut(&name) {
                    let real_code = if code.is_none() {
                        // stdout relay sent Exit with no code — wait() to reap and get it
                        match mp.child.try_wait() {
                            Ok(Some(status)) => status.code(),
                            Ok(None) => {
                                // Still running — wait with timeout
                                match tokio::time::timeout(
                                    std::time::Duration::from_secs(5),
                                    mp.child.wait(),
                                ).await {
                                    Ok(Ok(status)) => status.code(),
                                    _ => code,
                                }
                            }
                            Err(_) => code,
                        }
                    } else {
                        code
                    };
                    mp.status = ProcessStatus::Exited;
                    mp.exit_code = real_code;
                    info!("Process '{}' exited (code={:?})", name, real_code);
                    real_code
                } else {
                    code
                };
                let msg = ServerMsg::Exit(ExitMsg {
                    r#type: None,
                    name: name.clone(),
                    code: final_code,
                });
                self.relay_or_buffer(&name, msg);
            }
        }
    }

    fn relay_or_buffer(&mut self, name: &str, msg: ServerMsg) {
        let Some(mp) = self.procs.get_mut(name) else {
            return;
        };
        if mp.subscribed {
            // Drop the mutable borrow before calling send_to_client
            let sent = send_to_client_tx(self.client_tx.as_ref(), &msg);
            if !sent {
                let mp = self.procs.get_mut(name).unwrap();
                mp.subscribed = false;
                mp.buffer.push(msg);
            }
        } else {
            debug!("[relay][{}] buffering (buffer_size={})", name, mp.buffer.len() + 1);
            mp.buffer.push(msg);
        }
    }

    // -- Command handling --

    async fn handle_command(&mut self, msg: CmdMsg) {
        match msg.cmd.as_str() {
            "spawn" => self.cmd_spawn(msg).await,
            "kill" => self.cmd_kill(&msg.name).await,
            "interrupt" => self.cmd_interrupt(&msg.name).await,
            "subscribe" => self.cmd_subscribe(&msg.name).await,
            "unsubscribe" => self.cmd_unsubscribe(&msg.name).await,
            "list" => self.cmd_list().await,
            "status" => self.cmd_status().await,
            other => {
                self.send_result(ResultMsg::err("", format!("unknown command: {other}")));
            }
        }
    }

    async fn cmd_spawn(&mut self, msg: CmdMsg) {
        let name = &msg.name;

        // Already running?
        if let Some(mp) = self.procs.get(name)
            && mp.status == ProcessStatus::Running
        {
            let pid = mp.child.id().unwrap_or(0);
            let mut result = ResultMsg::ok(name);
            result.pid = Some(pid);
            result.already_running = Some(true);
            self.send_result(result);
            return;
        }

        // Remove old entry
        self.procs.remove(name);

        // Build environment
        let env: HashMap<String, String> = msg.env;

        // Spawn the process
        let mut cmd = Command::new(&msg.cli_args[0]);
        if msg.cli_args.len() > 1 {
            cmd.args(&msg.cli_args[1..]);
        }
        if let Some(cwd) = &msg.cwd {
            cmd.current_dir(cwd);
        }
        cmd.env_clear();
        cmd.envs(&env);
        cmd.stdin(std::process::Stdio::piped());
        cmd.stdout(std::process::Stdio::piped());
        cmd.stderr(std::process::Stdio::piped());
        // Start new session so we can kill the process group
        // SAFETY: setsid() is safe to call in pre_exec — it only affects the child process.
        #[allow(unsafe_code)]
        unsafe {
            cmd.pre_exec(|| {
                nix::unistd::setsid().map_err(io::Error::other)?;
                Ok(())
            });
        }

        let child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                self.send_result(ResultMsg::err(name, e.to_string()));
                return;
            }
        };

        let pid = child.id().unwrap_or(0);
        info!("Spawned process '{}' (pid={})", name, pid);

        let mut mp = ManagedProcess {
            _name: name.clone(),
            child,
            status: ProcessStatus::Running,
            exit_code: None,
            buffer: Vec::new(),
            subscribed: false,
            last_stdin_at: Instant::now(),
            last_stdout_at: Instant::now(),
            stdout_task: None,
            stderr_task: None,
        };

        // Open log files for stdio capture
        let stdout_log = open_log_file(&self.stdio_log_dir, name, "stdout");
        let stderr_log = open_log_file(&self.stdio_log_dir, name, "stderr");

        // Spawn stdout relay task
        let stdout = mp.child.stdout.take().expect("stdout pipe");
        let relay_tx = self.relay_tx.clone();
        let proc_name = name.clone();
        mp.stdout_task = Some(tokio::spawn(async move {
            relay_stdout(proc_name, stdout, relay_tx, stdout_log).await;
        }));

        // Spawn stderr relay task
        let stderr = mp.child.stderr.take().expect("stderr pipe");
        let relay_tx = self.relay_tx.clone();
        let proc_name = name.clone();
        mp.stderr_task = Some(tokio::spawn(async move {
            relay_stderr(proc_name, stderr, relay_tx, stderr_log).await;
        }));

        self.procs.insert(name.clone(), mp);
        let mut result = ResultMsg::ok(name);
        result.pid = Some(pid);
        self.send_result(result);
    }

    async fn cmd_kill(&mut self, name: &str) {
        if !self.procs.contains_key(name) {
            self.send_result(ResultMsg::err(name, "not found"));
            return;
        }
        self.kill_process(name).await;
        self.procs.remove(name);
        self.send_result(ResultMsg::ok(name));
    }

    async fn cmd_interrupt(&self, name: &str) {
        let Some(mp) = self.procs.get(name) else {
            self.send_result(ResultMsg::err(name, "not running"));
            return;
        };
        if mp.status != ProcessStatus::Running {
            self.send_result(ResultMsg::err(name, "not running"));
            return;
        }
        if let Some(pid) = mp.child.id() {
            match nix::unistd::getpgid(Some(Pid::from_raw(pid.cast_signed()))) {
                Ok(pgid) => {
                    if let Err(e) = signal::killpg(pgid, Signal::SIGINT) {
                        self.send_result(ResultMsg::err(name, e.to_string()));
                        return;
                    }
                    self.send_result(ResultMsg::ok(name));
                }
                Err(e) => {
                    self.send_result(ResultMsg::err(name, e.to_string()));
                }
            }
        } else {
            self.send_result(ResultMsg::err(name, "no pid"));
        }
    }

    async fn cmd_subscribe(&mut self, name: &str) {
        let Some(mp) = self.procs.get_mut(name) else {
            self.send_result(ResultMsg::err(name, "not found"));
            return;
        };

        let buffered_count = mp.buffer.len();
        debug!("[subscribe][{}] replaying {} buffered messages", name, buffered_count);

        // Drain buffer and collect info before releasing mutable borrow
        let buffered: Vec<ServerMsg> = mp.buffer.drain(..).collect();
        let status = match mp.status {
            ProcessStatus::Running => "running".to_string(),
            ProcessStatus::Exited => "exited".to_string(),
        };
        let exit_code = mp.exit_code;
        let idle = mp.idle();
        mp.subscribed = true;

        // Replay buffered messages (no mutable borrow on procs needed)
        for msg in &buffered {
            send_to_client_tx(self.client_tx.as_ref(), msg);
        }

        let mut result = ResultMsg::ok(name);
        result.replayed = Some(buffered_count);
        result.status = Some(status);
        result.exit_code = exit_code;
        result.idle = Some(idle);
        self.send_result(result);
    }

    async fn cmd_unsubscribe(&mut self, name: &str) {
        let Some(mp) = self.procs.get_mut(name) else {
            self.send_result(ResultMsg::err(name, "not found"));
            return;
        };
        mp.subscribed = false;
        self.send_result(ResultMsg::ok(name));
    }

    async fn cmd_list(&self) {
        let mut agents = serde_json::Map::new();
        for (name, mp) in &self.procs {
            agents.insert(
                name.clone(),
                serde_json::json!({
                    "pid": mp.child.id().unwrap_or(0),
                    "status": match mp.status {
                        ProcessStatus::Running => "running",
                        ProcessStatus::Exited => "exited",
                    },
                    "exit_code": mp.exit_code,
                    "buffered_msgs": mp.buffer.len(),
                    "subscribed": mp.subscribed,
                    "idle": mp.idle(),
                }),
            );
        }
        let mut result = ResultMsg::ok("");
        result.agents = Some(serde_json::Value::Object(agents));
        self.send_result(result);
    }

    async fn cmd_status(&self) {
        let uptime = self.start_time.elapsed().as_secs();
        let mut result = ResultMsg::ok("");
        result.uptime_seconds = Some(uptime);
        self.send_result(result);
    }

    // -- stdin forwarding --

    async fn handle_stdin(&mut self, msg: StdinMsg) {
        let Some(mp) = self.procs.get_mut(&msg.name) else {
            return;
        };
        if mp.status != ProcessStatus::Running {
            return;
        }
        let Some(stdin) = mp.child.stdin.as_mut() else {
            return;
        };

        let line = match serde_json::to_string(&msg.data) {
            Ok(s) => s + "\n",
            Err(_) => return,
        };

        debug!("[stdin][{}] forwarding {} bytes", msg.name, line.len());

        if let Err(e) = stdin.write_all(line.as_bytes()).await {
            warn!("Failed to write to process '{}' stdin: {}", msg.name, e);
            return;
        }
        if let Err(e) = stdin.flush().await {
            warn!("Failed to flush process '{}' stdin: {}", msg.name, e);
            return;
        }
        mp.last_stdin_at = Instant::now();
    }

    // -- Helpers --

    async fn kill_process(&mut self, name: &str) {
        let Some(mp) = self.procs.get_mut(name) else {
            return;
        };
        if mp.status != ProcessStatus::Running {
            return;
        }

        let Some(pid) = mp.child.id() else {
            return;
        };

        // Try SIGTERM to process group first
        let pgid = nix::unistd::getpgid(Some(Pid::from_raw(pid.cast_signed())));
        if let Ok(pgid) = pgid {
            signal::killpg(pgid, Signal::SIGTERM).ok();

            // Wait up to 5 seconds
            if let Ok(Ok(status)) = tokio::time::timeout(
                std::time::Duration::from_secs(5),
                mp.child.wait(),
            )
            .await {
                mp.status = ProcessStatus::Exited;
                mp.exit_code = status.code();
            } else {
                // Escalate to SIGKILL
                signal::killpg(pgid, Signal::SIGKILL).ok();
                mp.child.wait().await.ok();
                mp.status = ProcessStatus::Exited;
                mp.exit_code = mp.child.try_wait().ok().flatten().and_then(|s| s.code());
            }
        }

        // Cancel relay tasks
        if let Some(task) = mp.stdout_task.take() {
            task.abort();
        }
        if let Some(task) = mp.stderr_task.take() {
            task.abort();
        }
    }

    fn send_result(&self, result: ResultMsg) {
        send_to_client_tx(self.client_tx.as_ref(), &ServerMsg::Result(result));
    }
}

/// Send a message to the client via the channel. Returns false if no client.
fn send_to_client_tx(
    client_tx: Option<&mpsc::UnboundedSender<Vec<u8>>>,
    msg: &ServerMsg,
) -> bool {
    let Some(tx) = client_tx else {
        return false;
    };
    let Ok(mut payload) = serde_json::to_vec(msg) else {
        return false;
    };
    payload.push(b'\n');
    tx.send(payload).is_ok()
}

/// Open a log file for stdio capture, rotating if the current file exceeds 10 MB.
fn open_log_file(log_dir: &Path, name: &str, stream: &str) -> Option<std::fs::File> {
    const MAX_LOG_SIZE: u64 = 10 * 1024 * 1024;
    const MAX_ROTATIONS: u32 = 3;

    let path = log_dir.join(format!("{name}.{stream}.log"));

    if let Ok(meta) = std::fs::metadata(&path)
        && meta.len() > MAX_LOG_SIZE
    {
        for i in (1..MAX_ROTATIONS).rev() {
            let from = log_dir.join(format!("{name}.{stream}.log.{i}"));
            let to = log_dir.join(format!("{}.{}.log.{}", name, stream, i + 1));
            std::fs::rename(&from, &to).ok();
        }
        let rotated = log_dir.join(format!("{name}.{stream}.log.1"));
        std::fs::rename(&path, &rotated).ok();
    }

    match std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
    {
        Ok(f) => Some(f),
        Err(e) => {
            warn!("Failed to open log file {}: {}", path.display(), e);
            None
        }
    }
}

/// Write a line to the log file with a timestamp prefix.
fn log_line(file: &mut std::fs::File, line: &str) {
    use std::io::Write;
    let now = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S%.3fZ");
    let _ = writeln!(file, "[{now}] {line}");
}

/// Relay stdout from a child process. Reads JSON lines and sends them as `RelayMsg`.
async fn relay_stdout(
    name: String,
    stdout: tokio::process::ChildStdout,
    tx: mpsc::UnboundedSender<RelayMsg>,
    mut log_file: Option<std::fs::File>,
) {
    let reader = BufReader::new(stdout);
    let mut lines = reader.lines();
    let mut normal_eof = false;

    loop {
        match lines.next_line().await {
            Ok(Some(line)) => {
                let line = line.trim().to_string();
                if line.is_empty() {
                    continue;
                }
                if let Some(f) = log_file.as_mut() {
                    log_line(f, &line);
                }
                match serde_json::from_str::<serde_json::Value>(&line) {
                    Ok(data) => {
                        let data = if data.is_object() {
                            data
                        } else {
                            serde_json::json!({"raw": data})
                        };
                        tx.send(RelayMsg::Stdout {
                            name: name.clone(),
                            data,
                        })
                        .ok();
                    }
                    Err(_) => {
                        // Non-JSON output goes to stderr
                        if !line.is_empty() {
                            tx.send(RelayMsg::Stderr {
                                name: name.clone(),
                                text: line,
                            })
                            .ok();
                        }
                    }
                }
            }
            Ok(None) => {
                normal_eof = true;
                break;
            }
            Err(e) => {
                debug!("[stdout][{}] read error: {}", name, e);
                break;
            }
        }
    }

    if normal_eof {
        // Wait for the exit code — we don't have direct access to the child here,
        // so the exit event will be sent when the main loop detects the child has exited.
        // We send Exit with code=None; the main loop can update it.
        tx.send(RelayMsg::Exit {
            name,
            code: None,
        })
        .ok();
    }
}

/// Relay stderr from a child process.
async fn relay_stderr(
    name: String,
    stderr: tokio::process::ChildStderr,
    tx: mpsc::UnboundedSender<RelayMsg>,
    mut log_file: Option<std::fs::File>,
) {
    let reader = BufReader::new(stderr);
    let mut lines = reader.lines();

    while let Ok(Some(line)) = lines.next_line().await {
        let text = line.trim().to_string();
        if text.is_empty() {
            continue;
        }
        if let Some(f) = log_file.as_mut() {
            log_line(f, &text);
        }
        tx.send(RelayMsg::Stderr {
            name: name.clone(),
            text,
        })
        .ok();
    }
}
