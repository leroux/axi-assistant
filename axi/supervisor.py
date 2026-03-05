#!/usr/bin/env python3
"""Process supervisor for bot.py — replaces run.sh.

Signal semantics:
  SIGTERM/SIGINT  — full stop: kill bot.py AND bridge, then exit.
                    This is what systemctl stop/restart sends.
  SIGHUP          — hot restart: kill only bot.py, leave bridge running.
                    Supervisor relaunches bot.py which reconnects to the bridge.
"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import IO

_ANSI_RE = re.compile(rb"\033\[[0-9;]*m")

LOG_LEVEL = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s [supervisor] %(message)s",
)
log = logging.getLogger(__name__)

RESTART_EXIT_CODE = 42
CRASH_THRESHOLD = 60
ROLLBACK_MARKER = ".rollback_performed"
CRASH_ANALYSIS_MARKER = ".crash_analysis"
LOG_FILE = ".bot_output.log"
BRIDGE_SOCKET = ".bridge.sock"
MAX_RUNTIME_CRASHES = 3
ENABLE_ROLLBACK = os.environ.get("ENABLE_ROLLBACK", "").lower() in ("1", "true", "yes")

DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Signal handling
#   SIGTERM/SIGINT → full stop (kill bridge too)
#   SIGHUP         → hot restart (bridge stays)
# ---------------------------------------------------------------------------

_bot_proc: subprocess.Popen[bytes] | None = None
_stopping = False  # full stop requested (SIGTERM/SIGINT)
_hot_restart = False  # hot restart requested (SIGHUP)


def _stop_handler(signum: int, _frame: FrameType | None) -> None:
    """SIGTERM/SIGINT: forward to bot.py, flag for full stop."""
    global _stopping
    _stopping = True
    if _bot_proc and _bot_proc.poll() is None:
        _bot_proc.send_signal(signum)


def _hup_handler(signum: int, _frame: FrameType | None) -> None:
    """SIGHUP: forward SIGTERM to bot.py to trigger hot restart."""
    global _hot_restart
    _hot_restart = True
    if _bot_proc and _bot_proc.poll() is None:
        _bot_proc.send_signal(signal.SIGTERM)


def _kill_bridge():
    """Find and kill the bridge process, waiting for it to actually exit."""
    sock_path = DIR / BRIDGE_SOCKET
    killed_pids: list[int] = []
    try:
        result = subprocess.run(
            ["pgrep", "-f", str(sock_path)],
            capture_output=True,
            text=True,
        )
        pids = result.stdout.strip().split()
        for pid_str in pids:
            pid = int(pid_str)
            if pid == os.getpid():
                continue
            log.info("Sending SIGTERM to bridge process (pid=%d)", pid)
            try:
                os.kill(pid, signal.SIGTERM)
                killed_pids.append(pid)
            except ProcessLookupError:
                pass
    except Exception as e:
        log.warning("Failed to find/kill bridge: %s", e)

    # Wait up to 5s for bridge to die, then SIGKILL
    if killed_pids:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            alive = [p for p in killed_pids if _pid_alive(p)]
            if not alive:
                break
            time.sleep(0.1)
        else:
            alive = [p for p in killed_pids if _pid_alive(p)]

        for pid in alive:
            log.warning("Bridge process (pid=%d) did not exit after SIGTERM, sending SIGKILL", pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # Clean up stale socket
    if sock_path.exists():
        try:
            sock_path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def ensure_default_files():
    user_data = Path(os.environ.get("AXI_USER_DATA", Path.home() / "axi-user-data"))
    user_data.mkdir(parents=True, exist_ok=True)

    if not (DIR / "USER_PROFILE.md").exists():
        (DIR / "USER_PROFILE.md").write_text(
            "# User Profile\n\nThis is a currently blank user profile. It will be updated over time.\n"
        )
    if not (user_data / "schedules.json").exists():
        (user_data / "schedules.json").write_text("[]\n")
    if not (user_data / "schedule_history.json").exists():
        (user_data / "schedule_history.json").write_text("[]\n")


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=DIR,
        capture_output=True,
        text=True,
    )


def get_head() -> str:
    r = git("rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def has_uncommitted_changes() -> bool:
    r1 = git("diff", "--quiet", "HEAD")
    r2 = git("diff", "--cached", "--quiet")
    return r1.returncode != 0 or r2.returncode != 0


def is_git_repo() -> bool:
    r = git("rev-parse", "--is-inside-work-tree")
    return r.returncode == 0


def run_bot() -> int:
    """Launch bot.py, tee output to LOG_FILE, return exit code."""
    global _bot_proc
    log_path = DIR / LOG_FILE
    proc = subprocess.Popen(
        ["uv", "run", "python", "-m", "axi.main"],
        cwd=DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _bot_proc = proc

    def stream(pipe: IO[bytes], log_file: IO[bytes]) -> None:
        for line in iter(pipe.readline, b""):
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            log_file.write(_ANSI_RE.sub(b"", line))
            log_file.flush()

    with open(log_path, "ab") as lf:
        t = threading.Thread(target=stream, args=(proc.stdout, lf), daemon=True)
        t.start()
        proc.wait()
        t.join(timeout=5)

    _bot_proc = None
    return proc.returncode


def tail_log(n: int = 200) -> str:
    log_path = DIR / LOG_FILE
    try:
        lines = log_path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except FileNotFoundError:
        return ""


def write_crash_marker(code: int, elapsed: int, crash_log: str):
    marker = {
        "exit_code": code,
        "uptime_seconds": elapsed,
        "timestamp": datetime.now(UTC).astimezone().isoformat(),
        "crash_log": crash_log,
    }
    (DIR / CRASH_ANALYSIS_MARKER).write_text(json.dumps(marker, indent=4) + "\n")


def write_rollback_marker(
    code: int,
    elapsed: int,
    stash_output: str,
    rollback_details: str,
    pre_launch_commit: str,
    crashed_commit: str,
    crash_log: str,
):
    marker = {
        "exit_code": code,
        "uptime_seconds": elapsed,
        "stash_output": stash_output,
        "rollback_details": rollback_details,
        "pre_launch_commit": pre_launch_commit,
        "crashed_commit": crashed_commit,
        "timestamp": datetime.now(UTC).astimezone().isoformat(),
        "crash_log": crash_log,
    }
    (DIR / ROLLBACK_MARKER).write_text(json.dumps(marker, indent=4) + "\n")


def main():
    os.chdir(DIR)
    ensure_default_files()

    signal.signal(signal.SIGTERM, _stop_handler)  # pyright: ignore[reportArgumentType]
    signal.signal(signal.SIGINT, _stop_handler)  # pyright: ignore[reportArgumentType]
    signal.signal(signal.SIGHUP, _hup_handler)  # pyright: ignore[reportArgumentType]

    global _hot_restart
    rollback_attempted = False
    runtime_crash_count = 0

    while True:
        start_time = time.time()
        pre_launch_commit = get_head()

        code = run_bot()

        # SIGTERM/SIGINT — full stop: kill bridge and exit
        if _stopping:
            log.info("Received stop signal, bot exited with code %d. Killing bridge and stopping.", code)
            _kill_bridge()
            sys.exit(0)

        # SIGHUP — hot restart: just relaunch bot.py, bridge stays
        if _hot_restart:
            log.info("Received SIGHUP, bot exited with code %d. Hot-restarting (bridge stays alive)...", code)
            _hot_restart = False
            rollback_attempted = False
            runtime_crash_count = 0
            continue

        if code == RESTART_EXIT_CODE:
            log.info("Restart requested, relaunching...")
            rollback_attempted = False
            runtime_crash_count = 0
            continue

        if code == 0:
            log.info("Clean exit, stopping.")
            sys.exit(0)

        # Killed by signal (e.g. SIGTERM) — treat as intentional stop, not a crash
        if code < 0 or code == 128 + 15:  # negative = signal on Popen, 143 = SIGTERM
            log.info("Killed by signal (exit code %d), stopping.", code)
            sys.exit(0)

        elapsed = int(time.time() - start_time)
        log.info("Bot exited with code %d after %ds.", code, elapsed)

        # --- Runtime crash (ran long enough) ---
        if elapsed >= CRASH_THRESHOLD:
            runtime_crash_count += 1
            log.warning(
                "Runtime crash detected (%ds >= %ds threshold). Consecutive count: %d/%d.",
                elapsed,
                CRASH_THRESHOLD,
                runtime_crash_count,
                MAX_RUNTIME_CRASHES,
            )

            if runtime_crash_count >= MAX_RUNTIME_CRASHES:
                log.error("Max consecutive runtime crashes (%d) reached. Stopping.", MAX_RUNTIME_CRASHES)
                sys.exit(code)

            crash_log = tail_log()
            write_crash_marker(code, elapsed, crash_log)

            log.info("Crash analysis marker written. Relaunching for runtime crash recovery...")
            rollback_attempted = False
            continue

        # --- Startup crash (quick failure) ---
        log.warning("Quick crash detected (%ds < %ds threshold).", elapsed, CRASH_THRESHOLD)

        crash_log = tail_log()

        if not ENABLE_ROLLBACK:
            log.info("Rollback disabled. Writing crash marker and relaunching...")
            write_crash_marker(code, elapsed, crash_log)
            runtime_crash_count += 1
            if runtime_crash_count >= MAX_RUNTIME_CRASHES:
                log.error("Max consecutive crashes (%d) reached. Stopping.", MAX_RUNTIME_CRASHES)
                sys.exit(code)
            continue

        if rollback_attempted:
            log.error("Rollback already attempted. Stopping to prevent infinite loop.")
            sys.exit(code)

        if not is_git_repo():
            log.error("Not a git repository. Cannot rollback. Stopping.")
            sys.exit(code)

        current_commit = get_head()
        uncommitted = has_uncommitted_changes()

        if current_commit == pre_launch_commit and not uncommitted:
            log.error("No changes (committed or uncommitted) to roll back. Stopping.")
            sys.exit(code)

        rollback_details = ""
        stash_output = ""

        # Stash uncommitted changes
        if uncommitted:
            log.info("Stashing uncommitted changes...")
            r = git("stash", "push", "--include-untracked", "-m", f"auto-rollback: crash with exit code {code}")
            stash_output = (r.stdout + r.stderr).strip()
            log.info("%s", stash_output)
            rollback_details = "uncommitted changes stashed"

        # Revert committed changes if HEAD moved
        if pre_launch_commit and current_commit != pre_launch_commit:
            r = git("rev-list", "--count", f"{pre_launch_commit}..{current_commit}")
            new_commits = r.stdout.strip() if r.returncode == 0 else "?"
            log.warning(
                "HEAD moved from %s to %s (%s new commit(s)). Resetting...",
                pre_launch_commit[:7],
                current_commit[:7],
                new_commits,
            )
            git("reset", "--hard", pre_launch_commit)
            detail = f"{new_commits} commit(s) reverted"
            rollback_details = f"{rollback_details} + {detail}" if rollback_details else detail

        write_rollback_marker(
            code,
            elapsed,
            stash_output,
            rollback_details,
            pre_launch_commit,
            current_commit,
            crash_log,
        )

        log.info("Rollback marker written. Relaunching with pre-launch code (%s)...", pre_launch_commit[:7])
        rollback_attempted = True


if __name__ == "__main__":
    main()
