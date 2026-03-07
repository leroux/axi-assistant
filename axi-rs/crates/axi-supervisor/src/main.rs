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

use chrono::Utc;
use nix::sys::signal::{self, Signal};
use nix::unistd::Pid;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

const RESTART_EXIT_CODE: i32 = 42;
const CRASH_THRESHOLD_SECS: u64 = 60;
const MAX_RUNTIME_CRASHES: u32 = 3;
const LOG_FILE: &str = ".bot_output.log";
const BRIDGE_SOCKET: &str = ".bridge.sock";
const CRASH_ANALYSIS_MARKER: &str = ".crash_analysis";
const ROLLBACK_MARKER: &str = ".rollback_performed";

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
        let my_pid = std::process::id() as i32;
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

fn git(dir: &Path, args: &[&str]) -> std::process::Output {
    Command::new("git")
        .args(args)
        .current_dir(dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .unwrap_or_else(|_| {
            panic!("Failed to run git {:?}", args)
        })
}

fn get_head(dir: &Path) -> String {
    let output = git(dir, &["rev-parse", "HEAD"]);
    if output.status.success() {
        String::from_utf8_lossy(&output.stdout).trim().to_string()
    } else {
        String::new()
    }
}

fn has_uncommitted_changes(dir: &Path) -> bool {
    let r1 = git(dir, &["diff", "--quiet", "HEAD"]);
    let r2 = git(dir, &["diff", "--cached", "--quiet"]);
    !r1.status.success() || !r2.status.success()
}

fn is_git_repo(dir: &Path) -> bool {
    let r = git(dir, &["rev-parse", "--is-inside-work-tree"]);
    r.status.success()
}

fn ensure_default_files(dir: &Path) {
    let user_data = std::env::var("AXI_USER_DATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| dirs_home().join("axi-user-data"));
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
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/tmp"))
}

fn tail_log(dir: &Path, n: usize) -> String {
    let log_path = dir.join(LOG_FILE);
    match fs::read_to_string(&log_path) {
        Ok(content) => {
            let lines: Vec<&str> = content.lines().collect();
            let start = lines.len().saturating_sub(n);
            lines[start..].join("\n")
        }
        Err(_) => String::new(),
    }
}

fn write_crash_marker(dir: &Path, code: i32, elapsed: u64, crash_log: &str) {
    let marker = serde_json::json!({
        "exit_code": code,
        "uptime_seconds": elapsed,
        "timestamp": Utc::now().to_rfc3339(),
        "crash_log": crash_log,
    });
    fs::write(
        dir.join(CRASH_ANALYSIS_MARKER),
        serde_json::to_string_pretty(&marker).unwrap() + "\n",
    )
    .ok();
}

fn write_rollback_marker(
    dir: &Path,
    code: i32,
    elapsed: u64,
    stash_output: &str,
    rollback_details: &str,
    pre_launch_commit: &str,
    crashed_commit: &str,
    crash_log: &str,
) {
    let marker = serde_json::json!({
        "exit_code": code,
        "uptime_seconds": elapsed,
        "stash_output": stash_output,
        "rollback_details": rollback_details,
        "pre_launch_commit": pre_launch_commit,
        "crashed_commit": crashed_commit,
        "timestamp": Utc::now().to_rfc3339(),
        "crash_log": crash_log,
    });
    fs::write(
        dir.join(ROLLBACK_MARKER),
        serde_json::to_string_pretty(&marker).unwrap() + "\n",
    )
    .ok();
}

/// Run the bot process, teeing output to LOG_FILE. Returns exit code.
fn run_bot(dir: &Path) -> i32 {
    let mut child = Command::new("uv")
        .args(["run", "python", "-m", "axi.main"])
        .current_dir(dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("Failed to spawn bot process");

    // Store child PID for signal forwarding
    let child_pid = child.id() as i32;

    // Register the child PID so signal handlers can forward
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
                    // Write to stdout
                    let mut stdout = std::io::stdout().lock();
                    stdout.write_all(&raw_line).ok();
                    stdout.write_all(b"\n").ok();
                    stdout.flush().ok();

                    // Write stripped to log file
                    let stripped = strip_ansi(&raw_line);
                    log_file.write_all(&stripped).ok();
                    log_file.write_all(b"\n").ok();
                    log_file.flush().ok();
                }
                Err(_) => break,
            }
        }
    });

    // Tee stderr too
    if let Some(stderr) = stderr {
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.split(b'\n') {
                if let Ok(raw_line) = line {
                    let mut stderr_out = std::io::stderr().lock();
                    stderr_out.write_all(&raw_line).ok();
                    stderr_out.write_all(b"\n").ok();
                }
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

    // Determine working directory
    let dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));

    // Use BOT_DIR env var if set, otherwise current directory
    let dir = std::env::var("BOT_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| std::env::current_dir().unwrap_or(dir));

    std::env::set_current_dir(&dir).ok();
    ensure_default_files(&dir);

    let enable_rollback = std::env::var("ENABLE_ROLLBACK")
        .map(|v| matches!(v.to_lowercase().as_str(), "1" | "true" | "yes"))
        .unwrap_or(false);

    // Register signal handlers
    unsafe {
        nix::libc::signal(nix::libc::SIGTERM, stop_handler as *const () as nix::libc::sighandler_t);
        nix::libc::signal(nix::libc::SIGINT, stop_handler as *const () as nix::libc::sighandler_t);
        nix::libc::signal(nix::libc::SIGHUP, hup_handler as *const () as nix::libc::sighandler_t);
    }

    let mut rollback_attempted = false;
    let mut runtime_crash_count: u32 = 0;

    loop {
        let start = Instant::now();
        let pre_launch_commit = get_head(&dir);

        let code = run_bot(&dir);

        // SIGTERM/SIGINT — full stop
        if STOPPING.load(Ordering::SeqCst) {
            info!(
                "Received stop signal, bot exited with code {}. Killing bridge and stopping.",
                code
            );
            kill_bridge(&dir);
            std::process::exit(0);
        }

        // SIGHUP — hot restart
        if HOT_RESTART.load(Ordering::SeqCst) {
            info!(
                "Received SIGHUP, bot exited with code {}. Hot-restarting (bridge stays alive)...",
                code
            );
            HOT_RESTART.store(false, Ordering::SeqCst);
            rollback_attempted = false;
            runtime_crash_count = 0;
            continue;
        }

        // Clean restart
        if code == RESTART_EXIT_CODE {
            info!("Restart requested, relaunching...");
            rollback_attempted = false;
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

        // Runtime crash (ran long enough)
        if elapsed >= CRASH_THRESHOLD_SECS {
            runtime_crash_count += 1;
            warn!(
                "Runtime crash detected ({}s >= {}s threshold). Consecutive count: {}/{}.",
                elapsed, CRASH_THRESHOLD_SECS, runtime_crash_count, MAX_RUNTIME_CRASHES
            );

            if runtime_crash_count >= MAX_RUNTIME_CRASHES {
                error!(
                    "Max consecutive runtime crashes ({}) reached. Stopping.",
                    MAX_RUNTIME_CRASHES
                );
                std::process::exit(code);
            }

            let crash_log = tail_log(&dir, 200);
            write_crash_marker(&dir, code, elapsed, &crash_log);
            info!("Crash analysis marker written. Relaunching for runtime crash recovery...");
            rollback_attempted = false;
            continue;
        }

        // Startup crash (quick failure)
        warn!(
            "Quick crash detected ({}s < {}s threshold).",
            elapsed, CRASH_THRESHOLD_SECS
        );

        let crash_log = tail_log(&dir, 200);

        if !enable_rollback {
            info!("Rollback disabled. Writing crash marker and relaunching...");
            write_crash_marker(&dir, code, elapsed, &crash_log);
            runtime_crash_count += 1;
            if runtime_crash_count >= MAX_RUNTIME_CRASHES {
                error!(
                    "Max consecutive crashes ({}) reached. Stopping.",
                    MAX_RUNTIME_CRASHES
                );
                std::process::exit(code);
            }
            continue;
        }

        if rollback_attempted {
            error!("Rollback already attempted. Stopping to prevent infinite loop.");
            std::process::exit(code);
        }

        if !is_git_repo(&dir) {
            error!("Not a git repository. Cannot rollback. Stopping.");
            std::process::exit(code);
        }

        let current_commit = get_head(&dir);
        let uncommitted = has_uncommitted_changes(&dir);

        if current_commit == pre_launch_commit && !uncommitted {
            error!("No changes (committed or uncommitted) to roll back. Stopping.");
            std::process::exit(code);
        }

        let mut rollback_details = String::new();
        let mut stash_output = String::new();

        // Stash uncommitted changes
        if uncommitted {
            info!("Stashing uncommitted changes...");
            let r = git(
                &dir,
                &[
                    "stash",
                    "push",
                    "--include-untracked",
                    "-m",
                    &format!("auto-rollback: crash with exit code {}", code),
                ],
            );
            stash_output = String::from_utf8_lossy(&r.stdout).to_string()
                + &String::from_utf8_lossy(&r.stderr);
            stash_output = stash_output.trim().to_string();
            info!("{}", stash_output);
            rollback_details = "uncommitted changes stashed".to_string();
        }

        // Revert committed changes
        if !pre_launch_commit.is_empty() && current_commit != pre_launch_commit {
            let r = git(
                &dir,
                &[
                    "rev-list",
                    "--count",
                    &format!("{}..{}", pre_launch_commit, current_commit),
                ],
            );
            let new_commits = if r.status.success() {
                String::from_utf8_lossy(&r.stdout).trim().to_string()
            } else {
                "?".to_string()
            };
            warn!(
                "HEAD moved from {} to {} ({} new commit(s)). Resetting...",
                &pre_launch_commit[..7.min(pre_launch_commit.len())],
                &current_commit[..7.min(current_commit.len())],
                new_commits
            );
            git(&dir, &["reset", "--hard", &pre_launch_commit]);
            let detail = format!("{} commit(s) reverted", new_commits);
            if rollback_details.is_empty() {
                rollback_details = detail;
            } else {
                rollback_details = format!("{} + {}", rollback_details, detail);
            }
        }

        write_rollback_marker(
            &dir,
            code,
            elapsed,
            &stash_output,
            &rollback_details,
            &pre_launch_commit,
            &current_commit,
            &crash_log,
        );

        info!(
            "Rollback marker written. Relaunching with pre-launch code ({})...",
            &pre_launch_commit[..7.min(pre_launch_commit.len())]
        );
        rollback_attempted = true;
    }
}
