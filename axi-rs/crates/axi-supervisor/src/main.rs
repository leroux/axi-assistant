//! Process supervisor for the Axi bot — Rust replacement for supervisor.py.
//!
//! Signal semantics:
//!   SIGTERM/SIGINT  — full stop: kill bot AND bridge, then exit.
//!   SIGHUP          — hot restart: kill only bot, leave bridge running.

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Instant;

use nix::sys::signal::{self, Signal};
use nix::unistd::Pid;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

const RESTART_EXIT_CODE: i32 = 42;
const CRASH_THRESHOLD_SECS: u64 = 60;
const MAX_RUNTIME_CRASHES: u32 = 3;
const LOG_FILE: &str = ".bot_output.log";
const BRIDGE_SOCKET: &str = ".bridge.sock";
const PROCMUX_BINARY: &str = "procmux";
const BOT_BINARY: &str = "axi-bot";

/// Strip ANSI escape codes from bytes.
fn strip_ansi(input: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(input.len());
    let mut i = 0;
    while i < input.len() {
        if input[i] == 0x1b && i + 1 < input.len() && input[i + 1] == b'[' {
            // Skip escape sequence
            i += 2;
            while i < input.len() && !(input[i].is_ascii_alphabetic() || input[i] == b'm') {
                i += 1;
            }
            if i < input.len() {
                i += 1; // skip the terminating character
            }
        } else {
            out.push(input[i]);
            i += 1;
        }
    }
    out
}

fn pid_alive(pid: i32) -> bool {
    signal::kill(Pid::from_raw(pid), None).is_ok()
}

fn kill_bridge(dir: &Path) {
    let sock_path = dir.join(BRIDGE_SOCKET);
    let sock_str = sock_path.to_string_lossy();

    let result = Command::new("pgrep")
        .args(["-f", &sock_str])
        .output();

    let mut killed_pids = Vec::new();
    if let Ok(output) = result {
        let stdout = String::from_utf8_lossy(&output.stdout);
        let my_pid = std::process::id().cast_signed();
        for pid_str in stdout.split_whitespace() {
            if let Ok(pid) = pid_str.parse::<i32>() {
                if pid == my_pid {
                    continue;
                }
                info!("Sending SIGTERM to bridge process (pid={})", pid);
                signal::kill(Pid::from_raw(pid), Signal::SIGTERM).ok();
                killed_pids.push(pid);
            }
        }
    }

    // Wait up to 5s for bridge processes to die
    if !killed_pids.is_empty() {
        let deadline = Instant::now() + std::time::Duration::from_secs(5);
        loop {
            killed_pids.retain(|&pid| pid_alive(pid));
            if killed_pids.is_empty() || Instant::now() >= deadline {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        for &pid in &killed_pids {
            warn!(
                "Bridge process (pid={}) did not exit after SIGTERM, sending SIGKILL",
                pid
            );
            signal::kill(Pid::from_raw(pid), Signal::SIGKILL).ok();
        }
    }

    // Clean up stale socket
    if sock_path.exists() {
        fs::remove_file(&sock_path).ok();
    }
}

fn ensure_default_files(dir: &Path) {
    let user_data = std::env::var("AXI_USER_DATA")
        .map_or_else(|_| dirs_home().join("axi-user-data"), PathBuf::from);
    fs::create_dir_all(&user_data).ok();

    let profile = dir.join("USER_PROFILE.md");
    if !profile.exists() {
        fs::write(
            &profile,
            "# User Profile\n\nThis is a currently blank user profile. It will be updated over time.\n",
        )
        .ok();
    }
    let schedules = user_data.join("schedules.json");
    if !schedules.exists() {
        fs::write(&schedules, "[]\n").ok();
    }
    let history = user_data.join("schedule_history.json");
    if !history.exists() {
        fs::write(&history, "[]\n").ok();
    }
}

fn dirs_home() -> PathBuf {
    std::env::var("HOME").map_or_else(|_| PathBuf::from("/tmp"), PathBuf::from)
}

/// Find a sibling binary (same directory as ourselves).
fn find_sibling_binary(name: &str) -> PathBuf {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let candidate = parent.join(name);
            if candidate.exists() {
                return candidate;
            }
        }
    }
    // Fallback: assume it's on PATH
    PathBuf::from(name)
}

/// Start the procmux bridge process. Returns the child process.
fn start_bridge(dir: &Path) -> Option<std::process::Child> {
    let sock_path = dir.join(BRIDGE_SOCKET);

    // Clean up stale socket
    if sock_path.exists() {
        fs::remove_file(&sock_path).ok();
    }

    let procmux_bin = find_sibling_binary(PROCMUX_BINARY);
    info!("Starting bridge: {} {}", procmux_bin.display(), sock_path.display());

    match Command::new(&procmux_bin)
        .arg(&sock_path)
        .current_dir(dir)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(child) => {
            info!("Bridge started (pid={})", child.id());
            // Give it a moment to create the socket
            std::thread::sleep(std::time::Duration::from_millis(500));
            Some(child)
        }
        Err(e) => {
            error!("Failed to start bridge: {}", e);
            None
        }
    }
}

/// Run the bot process, teeing output to `LOG_FILE`. Returns exit code.
fn run_bot(dir: &Path) -> i32 {
    let bot_bin = find_sibling_binary(BOT_BINARY);
    let mut child = Command::new(&bot_bin)
        .current_dir(dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("Failed to spawn bot process");

    // Store child PID for signal forwarding
    let child_pid = child.id().cast_signed();
    CHILD_PID.store(child_pid, Ordering::SeqCst);

    // Read stdout in a thread, tee to log file
    let stdout = child.stdout.take().expect("stdout pipe");
    let log_path = dir.join(LOG_FILE);
    let stderr = child.stderr.take();

    let log_thread = std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        let mut log_file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .expect("Failed to open log file");

        for line in reader.split(b'\n') {
            match line {
                Ok(raw_line) => {
                    let mut stdout = std::io::stdout().lock();
                    stdout.write_all(&raw_line).ok();
                    stdout.write_all(b"\n").ok();
                    stdout.flush().ok();

                    let stripped = strip_ansi(&raw_line);
                    log_file.write_all(&stripped).ok();
                    log_file.write_all(b"\n").ok();
                    log_file.flush().ok();
                }
                Err(_) => break,
            }
        }
    });

    if let Some(stderr) = stderr {
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for raw_line in reader.split(b'\n').flatten() {
                let mut stderr_out = std::io::stderr().lock();
                stderr_out.write_all(&raw_line).ok();
                stderr_out.write_all(b"\n").ok();
            }
        });
    }

    let status = child.wait().expect("Failed to wait for bot process");
    log_thread.join().ok();
    CHILD_PID.store(0, Ordering::SeqCst);

    status.code().unwrap_or(-1)
}

// Global state for signal handling
static STOPPING: std::sync::LazyLock<Arc<AtomicBool>> =
    std::sync::LazyLock::new(|| Arc::new(AtomicBool::new(false)));
static HOT_RESTART: std::sync::LazyLock<Arc<AtomicBool>> =
    std::sync::LazyLock::new(|| Arc::new(AtomicBool::new(false)));
static CHILD_PID: std::sync::atomic::AtomicI32 = std::sync::atomic::AtomicI32::new(0);

extern "C" fn stop_handler(_sig: nix::libc::c_int) {
    STOPPING.store(true, Ordering::SeqCst);
    let pid = CHILD_PID.load(Ordering::SeqCst);
    if pid > 0 {
        signal::kill(Pid::from_raw(pid), Signal::SIGTERM).ok();
    }
}

extern "C" fn hup_handler(_sig: nix::libc::c_int) {
    HOT_RESTART.store(true, Ordering::SeqCst);
    let pid = CHILD_PID.load(Ordering::SeqCst);
    if pid > 0 {
        signal::kill(Pid::from_raw(pid), Signal::SIGTERM).ok();
    }
}

fn main() {
    let log_level = std::env::var("LOG_LEVEL").unwrap_or_else(|_| "info".to_string());
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_new(&log_level).unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(false)
        .init();

    let dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."));

    let dir = std::env::var("BOT_DIR")
        .map_or_else(|_| std::env::current_dir().unwrap_or(dir), PathBuf::from);

    std::env::set_current_dir(&dir).ok();
    ensure_default_files(&dir);

    // SAFETY: Signal handlers only set atomic flags (AtomicBool), which is
    // async-signal-safe. No heap allocation or complex logic in handlers.
    #[allow(unsafe_code)]
    unsafe {
        nix::libc::signal(
            nix::libc::SIGTERM,
            stop_handler as *const () as nix::libc::sighandler_t,
        );
        nix::libc::signal(
            nix::libc::SIGINT,
            stop_handler as *const () as nix::libc::sighandler_t,
        );
        nix::libc::signal(
            nix::libc::SIGHUP,
            hup_handler as *const () as nix::libc::sighandler_t,
        );
    }

    // Start the bridge (procmux) — stays alive across bot restarts
    let mut bridge_child = start_bridge(&dir);

    let mut runtime_crash_count: u32 = 0;

    loop {
        let start = Instant::now();
        let code = run_bot(&dir);

        // SIGTERM/SIGINT — full stop
        if STOPPING.load(Ordering::SeqCst) {
            info!(
                "Received stop signal, bot exited with code {}. Killing bridge and stopping.",
                code
            );
            if let Some(ref mut child) = bridge_child {
                info!("Stopping bridge child (pid={})", child.id());
                signal::kill(Pid::from_raw(child.id().cast_signed()), Signal::SIGTERM).ok();
                let deadline = Instant::now() + std::time::Duration::from_secs(5);
                loop {
                    match child.try_wait() {
                        Ok(Some(_)) => break,
                        Ok(None) if Instant::now() >= deadline => {
                            warn!("Bridge did not exit after SIGTERM, sending SIGKILL");
                            child.kill().ok();
                            child.wait().ok();
                            break;
                        }
                        _ => std::thread::sleep(std::time::Duration::from_millis(100)),
                    }
                }
            }
            kill_bridge(&dir); // Also clean up any other bridge processes
            std::process::exit(0);
        }

        // SIGHUP — hot restart
        if HOT_RESTART.load(Ordering::SeqCst) {
            info!(
                "Received SIGHUP, bot exited with code {}. Hot-restarting (bridge stays alive)...",
                code
            );
            HOT_RESTART.store(false, Ordering::SeqCst);
            runtime_crash_count = 0;
            continue;
        }

        // Clean restart (exit code 42)
        if code == RESTART_EXIT_CODE {
            info!("Restart requested, relaunching...");
            runtime_crash_count = 0;
            continue;
        }

        // Clean exit
        if code == 0 {
            info!("Clean exit, stopping.");
            std::process::exit(0);
        }

        // Killed by signal — treat as intentional
        if code < 0 || code == 128 + 15 {
            info!("Killed by signal (exit code {}), stopping.", code);
            std::process::exit(0);
        }

        let elapsed = start.elapsed().as_secs();
        info!("Bot exited with code {} after {}s.", code, elapsed);

        runtime_crash_count += 1;

        if runtime_crash_count >= MAX_RUNTIME_CRASHES {
            error!(
                "Max consecutive crashes ({}) reached. Stopping.",
                MAX_RUNTIME_CRASHES
            );
            std::process::exit(code);
        }

        if elapsed >= CRASH_THRESHOLD_SECS {
            warn!(
                "Runtime crash ({}s >= {}s threshold). Count: {}/{}.",
                elapsed, CRASH_THRESHOLD_SECS, runtime_crash_count, MAX_RUNTIME_CRASHES
            );
        } else {
            warn!(
                "Quick crash ({}s < {}s threshold). Count: {}/{}.",
                elapsed, CRASH_THRESHOLD_SECS, runtime_crash_count, MAX_RUNTIME_CRASHES
            );
        }

        info!("Relaunching...");
    }
}
