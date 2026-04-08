"""Worktree management and merge queue infrastructure.

Extracted from axi_test.py and axi/main.py to share between the bot runtime
and the CLI.  The bot uses create_worktree(); the CLI uses merge/queue helpers.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from axi import config

log = logging.getLogger("axi")


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------


@contextmanager
def flock(path: str) -> Generator[None, None, None]:
    """Acquire exclusive file lock, release on exit."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# Branch helpers
# ---------------------------------------------------------------------------


def get_worktree_branch(worktree_path: str) -> str:
    """Get the branch name for a worktree."""
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    branch = result.stdout.strip()
    if branch:
        return branch
    # Detached HEAD — show short hash
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or "unknown"


def is_git_repo(path: str) -> bool:
    """Check if *path* is inside a git working tree."""
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


# ---------------------------------------------------------------------------
# Worktree creation (from axi/main.py)
# ---------------------------------------------------------------------------


def create_worktree(name: str) -> str | None:
    """Create a git worktree for an axi-dev agent. Returns worktree path or None on failure."""
    worktree_path = os.path.join(config.BOT_WORKTREES_DIR, name)

    if os.path.isdir(worktree_path):
        # Check if it's already a valid git worktree
        git_marker = os.path.join(worktree_path, ".git")
        if os.path.exists(git_marker):
            log.info("Reusing existing worktree for '%s' at %s", name, worktree_path)
            return worktree_path
        # Directory exists but isn't a worktree — conflict
        log.warning("Directory exists at %s but is not a git worktree", worktree_path)
        return None

    branch = f"feature/{name}"
    result = subprocess.run(
        ["git", "-C", config.BOT_DIR, "worktree", "add", worktree_path, "-b", branch],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Branch might already exist, try without -b
        result = subprocess.run(
            ["git", "-C", config.BOT_DIR, "worktree", "add", worktree_path, branch],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning("Failed to create worktree for '%s': %s", name, result.stderr.strip())
            return None
        log.info("Created worktree for '%s' at %s (existing branch)", name, worktree_path)
    else:
        log.info("Created worktree for '%s' at %s", name, worktree_path)

    return worktree_path


# ---------------------------------------------------------------------------
# Merge queue infrastructure (from axi_test.py)
# ---------------------------------------------------------------------------


def find_main_repo() -> str:
    """Find the main repo path via git's common dir."""
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("Error: Not inside a git repository")
        raise SystemExit(1)
    return os.path.dirname(result.stdout.strip())


def queue_file(main_repo: str) -> str:
    return os.path.join(main_repo, ".merge-queue.json")


def queue_lock(main_repo: str) -> str:
    return os.path.join(main_repo, ".merge-queue.lock")


def merge_lock_file(main_repo: str) -> str:
    return os.path.join(main_repo, ".merge-exec.lock")


def read_queue(main_repo: str) -> list[dict[str, Any]]:
    """Read queue file. Caller must hold queue lock."""
    path = queue_file(main_repo)
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def write_queue(main_repo: str, entries: list[dict[str, Any]]) -> None:
    """Write queue file. Caller must hold queue lock."""
    with open(queue_file(main_repo), "w") as f:
        json.dump(entries, f, indent=2)


def cleanup_stale(entries: list[dict[str, Any]]) -> None:
    """Remove entries with dead processes. Modifies list in place."""
    now = datetime.now(UTC)
    to_remove: list[int] = []
    for i, entry in enumerate(entries):
        pid = entry.get("pid")
        if not pid:
            continue
        try:
            os.kill(pid, 0)
            # Process alive — check heartbeat staleness as backup
            heartbeat_s = entry.get("heartbeat", "")
            submitted_s = entry.get("submitted_at", "")
            if heartbeat_s and submitted_s:
                heartbeat = datetime.fromisoformat(heartbeat_s)
                submitted = datetime.fromisoformat(submitted_s)
                if (now - heartbeat).total_seconds() > 60 and (now - submitted).total_seconds() > 600:
                    to_remove.append(i)
        except ProcessLookupError:
            to_remove.append(i)
        except PermissionError:
            pass  # Process exists but we can't signal it
    for i in reversed(to_remove):
        removed = entries.pop(i)
        print(f"Removed stale queue entry: {removed.get('branch', '?')} (pid {removed.get('pid', '?')})")


def remove_from_queue(main_repo: str, branch: str) -> None:
    """Remove a branch from the queue."""
    with flock(queue_lock(main_repo)):
        entries = read_queue(main_repo)
        entries = [e for e in entries if e["branch"] != branch]
        write_queue(main_repo, entries)


def git(main_repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in the main repo."""
    return subprocess.run(
        ["git", "-C", main_repo, *args],
        capture_output=True,
        text=True,
    )


def find_git_deps(main_repo: str) -> list[str]:
    """Parse pyproject.toml to find git-sourced dependency package names."""
    pyproject_path = os.path.join(main_repo, "pyproject.toml")
    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return []
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    return [name for name, spec in sources.items() if isinstance(spec, dict) and "git" in spec]


def upgrade_git_deps(main_repo: str) -> None:
    """Upgrade git-sourced deps and auto-commit uv.lock if changed."""
    pkgs = find_git_deps(main_repo)
    if not pkgs:
        print("No git-sourced dependencies found — skipping upgrade")
        return

    lock_args = []
    for pkg in pkgs:
        lock_args.extend(["--upgrade-package", pkg])

    print(f"Upgrading git deps: {', '.join(pkgs)}...")
    lock_result = subprocess.run(
        ["uv", "lock", *lock_args],
        cwd=main_repo,
        capture_output=True,
        text=True,
    )
    if lock_result.returncode != 0:
        print(f"Note: uv lock failed — {lock_result.stderr.strip()}")
        return

    sync_result = subprocess.run(
        ["uv", "sync"],
        cwd=main_repo,
        capture_output=True,
        text=True,
    )
    if sync_result.returncode != 0:
        print(f"Note: uv sync failed — {sync_result.stderr.strip()}")

    # Check if lock file changed
    diff = git(main_repo, "diff", "--quiet", "uv.lock")
    if diff.returncode != 0:
        git(main_repo, "add", "uv.lock")
        pkg_list = ", ".join(pkgs)
        git(main_repo, "commit", "-m", f"Update git deps ({pkg_list})")
        print(f"Updated git deps: uv.lock")
    else:
        print("Git deps already up to date")


def execute_merge(main_repo: str, branch: str, message: str | None = None) -> tuple[str, str]:
    """Execute squash merge. Returns (status, detail).

    status: "merged", "needs_rebase", or "error"
    detail: commit SHA on success, or error message on failure.
    """
    # Verify main repo is on 'main' branch
    current = git(main_repo, "branch", "--show-current")
    if current.stdout.strip() != "main":
        return ("error", f"main repo is on branch '{current.stdout.strip()}', expected 'main'")

    # Pre-merge cleanup: if index is dirty from interrupted merge, reset
    if git(main_repo, "diff", "--cached", "--quiet").returncode != 0:
        print("Cleaning up dirty index from interrupted merge...")
        r = git(main_repo, "reset", "--hard", "HEAD")
        if r.returncode != 0:
            return ("error", f"failed to clean dirty index: {r.stderr.strip()}")

    # Fast-forward check: merge-base must equal main HEAD
    merge_base_r = git(main_repo, "merge-base", "main", branch)
    if merge_base_r.returncode != 0:
        return ("error", f"failed to compute merge-base: {merge_base_r.stderr.strip()}")

    main_head_r = git(main_repo, "rev-parse", "main")
    if main_head_r.returncode != 0:
        return ("error", f"failed to get main HEAD: {main_head_r.stderr.strip()}")

    merge_base = merge_base_r.stdout.strip()
    main_head = main_head_r.stdout.strip()

    if merge_base != main_head:
        return ("needs_rebase", f"merge-base {merge_base[:8]} != main HEAD {main_head[:8]}")

    # Check branch has commits beyond main
    log_r = git(main_repo, "log", "--oneline", f"main..{branch}")
    if not log_r.stdout.strip():
        return ("error", "no commits to merge — branch is identical to main")

    # Collect full commit messages (subject + body) before squashing
    msg_r = git(main_repo, "log", "--format=%B---", f"main..{branch}")
    raw_msgs = msg_r.stdout.strip()
    # Split by separator, clean up, format as bullet list
    commits = [m.strip() for m in raw_msgs.split("---") if m.strip()]
    if len(commits) == 1:
        commit_log = commits[0]
    else:
        commit_log = "\n\n".join(f"- {c}" for c in commits)

    # Squash merge
    merge_r = git(main_repo, "merge", "--squash", branch)
    if merge_r.returncode != 0:
        git(main_repo, "reset", "--hard", "HEAD")
        return ("error", f"squash merge failed: {merge_r.stderr.strip()}")

    # Build commit message: custom message as title, always include full commit log
    if message:
        commit_msg = message
        if commit_log:
            commit_msg += f"\n\n{commit_log}"
    else:
        if commit_log:
            commit_msg = commit_log
        else:
            commit_msg = branch

    # Commit
    commit_r = git(main_repo, "commit", "-m", commit_msg)
    if commit_r.returncode != 0:
        git(main_repo, "reset", "--hard", "HEAD")
        return ("error", f"commit failed: {commit_r.stderr.strip()}")

    sha_r = git(main_repo, "rev-parse", "--short", "HEAD")
    return ("merged", sha_r.stdout.strip())


# ---------------------------------------------------------------------------
# Auto-merge on agent completion
# ---------------------------------------------------------------------------


def is_auto_worktree(cwd: str) -> bool:
    """Check if cwd is an auto-created worktree under BOT_WORKTREES_DIR."""
    try:
        return os.path.realpath(cwd).startswith(os.path.realpath(config.BOT_WORKTREES_DIR) + os.sep)
    except (OSError, ValueError):
        return False


def has_commits_beyond_main(worktree_path: str) -> bool:
    """Check if the worktree branch has commits beyond main."""
    branch = get_worktree_branch(worktree_path)
    if not branch or branch == "unknown":
        return False
    result = subprocess.run(
        ["git", "-C", worktree_path, "log", "--oneline", f"main..{branch}"],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def try_merge_and_cleanup(worktree_path: str) -> tuple[str, str]:
    """Attempt to merge a worktree branch into main and clean up.

    Returns (status, detail) where status is one of:
    - "merged": squash-merged successfully, worktree+branch cleaned up
    - "no_commits": branch had no new commits, worktree cleaned up
    - "needs_rebase": branch needs rebase, worktree kept
    - "conflict": merge had conflicts, worktree kept
    - "error": unexpected error, worktree kept
    """
    branch = get_worktree_branch(worktree_path)
    if not branch or branch == "unknown":
        return ("error", "could not determine branch")

    # Find main repo from the worktree
    result = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ("error", "could not find main repo")
    main_repo = os.path.dirname(result.stdout.strip())

    # Check for commits
    if not has_commits_beyond_main(worktree_path):
        _remove_worktree(main_repo, worktree_path, branch)
        return ("no_commits", "no new commits — cleaned up worktree")

    # Attempt merge with merge lock
    with flock(merge_lock_file(main_repo)):
        status, detail = execute_merge(main_repo, branch)

    if status == "merged":
        _remove_worktree(main_repo, worktree_path, branch)
        return ("merged", detail)

    if status == "needs_rebase":
        # Attempt auto-rebase
        rebase_r = subprocess.run(
            ["git", "-C", worktree_path, "rebase", "main"],
            capture_output=True,
            text=True,
        )
        if rebase_r.returncode != 0:
            # Abort failed rebase
            subprocess.run(
                ["git", "-C", worktree_path, "rebase", "--abort"],
                capture_output=True,
                text=True,
            )
            return ("conflict", f"rebase conflicts: {rebase_r.stderr.strip()}")

        # Retry merge after rebase
        with flock(merge_lock_file(main_repo)):
            status2, detail2 = execute_merge(main_repo, branch)

        if status2 == "merged":
            _remove_worktree(main_repo, worktree_path, branch)
            return ("merged", detail2)
        return (status2, detail2)

    return (status, detail)


def _remove_worktree(main_repo: str, worktree_path: str, branch: str) -> None:
    """Remove a worktree and delete the branch if it was a feature branch."""
    subprocess.run(
        ["git", "-C", main_repo, "worktree", "remove", worktree_path, "--force"],
        capture_output=True,
        text=True,
    )
    if branch.startswith("feature/"):
        subprocess.run(
            ["git", "-C", main_repo, "branch", "-D", branch],
            capture_output=True,
            text=True,
        )
    log.info("Cleaned up worktree %s (branch %s)", worktree_path, branch)
