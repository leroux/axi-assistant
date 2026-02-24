#!/usr/bin/env python3
"""Process supervisor for bot.py — replaces run.sh."""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

RESTART_EXIT_CODE = 42
CRASH_THRESHOLD = 60
ROLLBACK_MARKER = ".rollback_performed"
CRASH_ANALYSIS_MARKER = ".crash_analysis"
LOG_FILE = ".bot_output.log"
MAX_RUNTIME_CRASHES = 3

DIR = Path(__file__).resolve().parent


def ensure_default_files():
    if not (DIR / "USER_PROFILE.md").exists():
        (DIR / "USER_PROFILE.md").write_text(
            "# User Profile\n\nThis is a currently blank user profile. It will be updated over time.\n"
        )
    if not (DIR / "schedules.json").exists():
        (DIR / "schedules.json").write_text("[]\n")
    if not (DIR / "schedule_history.json").exists():
        (DIR / "schedule_history.json").write_text("[]\n")


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
    log_path = DIR / LOG_FILE
    proc = subprocess.Popen(
        ["uv", "run", "python", "bot.py"],
        cwd=DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def stream(pipe, log_file):
        for line in iter(pipe.readline, b""):
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            log_file.write(line)
            log_file.flush()

    with open(log_path, "ab") as lf:
        t = threading.Thread(target=stream, args=(proc.stdout, lf), daemon=True)
        t.start()
        proc.wait()
        t.join(timeout=5)

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
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
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
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
        "crash_log": crash_log,
    }
    (DIR / ROLLBACK_MARKER).write_text(json.dumps(marker, indent=4) + "\n")


def main():
    os.chdir(DIR)
    ensure_default_files()

    rollback_attempted = False
    runtime_crash_count = 0

    while True:
        start_time = time.time()
        pre_launch_commit = get_head()

        code = run_bot()

        if code == RESTART_EXIT_CODE:
            print("Restart requested, relaunching...")
            rollback_attempted = False
            runtime_crash_count = 0
            continue

        if code == 0:
            print("Clean exit, stopping.")
            sys.exit(0)

        elapsed = int(time.time() - start_time)
        print(f"Bot exited with code {code} after {elapsed}s.")

        # --- Runtime crash (ran long enough) ---
        if elapsed >= CRASH_THRESHOLD:
            runtime_crash_count += 1
            print(
                f"Runtime crash detected ({elapsed}s >= {CRASH_THRESHOLD}s threshold). "
                f"Consecutive count: {runtime_crash_count}/{MAX_RUNTIME_CRASHES}."
            )

            if runtime_crash_count >= MAX_RUNTIME_CRASHES:
                print(f"Max consecutive runtime crashes ({MAX_RUNTIME_CRASHES}) reached. Stopping.")
                sys.exit(code)

            crash_log = tail_log()
            write_crash_marker(code, elapsed, crash_log)

            print("Crash analysis marker written. Relaunching for runtime crash recovery...")
            rollback_attempted = False
            continue

        # --- Startup crash (quick failure) ---
        print(f"Quick crash detected ({elapsed}s < {CRASH_THRESHOLD}s threshold).")

        crash_log = tail_log()

        if rollback_attempted:
            print("Rollback already attempted. Stopping to prevent infinite loop.")
            sys.exit(code)

        if not is_git_repo():
            print("Not a git repository. Cannot rollback. Stopping.")
            sys.exit(code)

        current_commit = get_head()
        uncommitted = has_uncommitted_changes()

        if current_commit == pre_launch_commit and not uncommitted:
            print("No changes (committed or uncommitted) to roll back. Stopping.")
            sys.exit(code)

        rollback_details = ""
        stash_output = ""

        # Stash uncommitted changes
        if uncommitted:
            print("Stashing uncommitted changes...")
            r = git("stash", "push", "--include-untracked", "-m",
                     f"auto-rollback: crash with exit code {code}")
            stash_output = (r.stdout + r.stderr).strip()
            print(stash_output)
            rollback_details = "uncommitted changes stashed"

        # Revert committed changes if HEAD moved
        if pre_launch_commit and current_commit != pre_launch_commit:
            r = git("rev-list", "--count", f"{pre_launch_commit}..{current_commit}")
            new_commits = r.stdout.strip() if r.returncode == 0 else "?"
            print(
                f"HEAD moved from {pre_launch_commit[:7]} to {current_commit[:7]} "
                f"({new_commits} new commit(s)). Resetting..."
            )
            git("reset", "--hard", pre_launch_commit)
            detail = f"{new_commits} commit(s) reverted"
            rollback_details = f"{rollback_details} + {detail}" if rollback_details else detail

        write_rollback_marker(
            code, elapsed, stash_output, rollback_details,
            pre_launch_commit, current_commit, crash_log,
        )

        print(f"Rollback marker written. Relaunching with pre-launch code ({pre_launch_commit[:7]})...")
        rollback_attempted = True


if __name__ == "__main__":
    main()
