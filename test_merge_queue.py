"""Tests for the merge queue in axi_test.py.

Covers: queue file I/O, stale cleanup, locking, and merge execution.
All git tests use throwaway repos in tmp_path — never touches the real repo.
"""

import os
import subprocess
from datetime import UTC

from axi_test import (
    _cleanup_stale,
    _execute_merge,
    _flock,
    _queue_file,
    _queue_lock,
    _read_queue,
    _remove_from_queue,
    _write_queue,
)

# --- Helpers ---


def git(repo, *args):
    """Run git command in a repo, assert success."""
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr}"
    return r.stdout.strip()


def make_repo(tmp_path, name="main_repo"):
    """Create a git repo with an initial commit on 'main'."""
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@test.com")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "Initial commit")
    return repo


def make_feature_branch(repo, branch_name, files):
    """Create a feature branch with commits. files: {name: content}."""
    git(repo, "checkout", "-b", branch_name)
    for name, content in files.items():
        (repo / name).write_text(content)
        git(repo, "add", name)
        git(repo, "commit", "-m", f"Add {name}")
    git(repo, "checkout", "main")


def advance_main(repo, filename="main_advance.txt", content="advance"):
    """Add a commit to main (to create divergence)."""
    (repo / filename).write_text(content)
    git(repo, "add", filename)
    git(repo, "commit", "-m", f"Advance main: {filename}")


# --- Queue File I/O ---


class TestQueueReadWrite:
    def test_read_empty_queue_no_file(self, tmp_path):
        entries = _read_queue(str(tmp_path))
        assert entries == []

    def test_write_and_read(self, tmp_path):
        repo = str(tmp_path)
        entries = [{"branch": "feature/a", "pid": 1}]
        with _flock(_queue_lock(repo)):
            _write_queue(repo, entries)
        with _flock(_queue_lock(repo)):
            result = _read_queue(repo)
        assert result == entries

    def test_read_corrupted_json(self, tmp_path):
        path = _queue_file(str(tmp_path))
        with open(path, "w") as f:
            f.write("{broken json!!")
        result = _read_queue(str(tmp_path))
        assert result == []

    def test_write_overwrites(self, tmp_path):
        repo = str(tmp_path)
        with _flock(_queue_lock(repo)):
            _write_queue(repo, [{"branch": "a"}])
            _write_queue(repo, [{"branch": "b"}])
            result = _read_queue(repo)
        assert len(result) == 1
        assert result[0]["branch"] == "b"

    def test_multiple_entries_preserve_order(self, tmp_path):
        repo = str(tmp_path)
        entries = [
            {"branch": "feature/first", "pid": 1},
            {"branch": "feature/second", "pid": 2},
            {"branch": "feature/third", "pid": 3},
        ]
        with _flock(_queue_lock(repo)):
            _write_queue(repo, entries)
            result = _read_queue(repo)
        assert [e["branch"] for e in result] == ["feature/first", "feature/second", "feature/third"]


# --- Stale Cleanup ---


class TestStaleCleanup:
    def test_dead_pid_removed(self):
        entries = [
            {
                "branch": "feature/dead",
                "pid": 99999,  # Almost certainly not running
                "submitted_at": "2026-01-01T00:00:00+00:00",
                "heartbeat": "2026-01-01T00:00:00+00:00",
            }
        ]
        _cleanup_stale(entries)
        assert len(entries) == 0

    def test_alive_pid_kept(self):
        entries = [
            {
                "branch": "feature/alive",
                "pid": os.getpid(),  # This process is alive
                "submitted_at": "2099-01-01T00:00:00+00:00",
                "heartbeat": "2099-01-01T00:00:00+00:00",
            }
        ]
        _cleanup_stale(entries)
        assert len(entries) == 1

    def test_alive_but_stale_heartbeat_removed(self):
        """Process alive but heartbeat old + submitted long ago → stale."""
        entries = [
            {
                "branch": "feature/stale",
                "pid": os.getpid(),
                "submitted_at": "2020-01-01T00:00:00+00:00",
                "heartbeat": "2020-01-01T00:00:00+00:00",
            }
        ]
        _cleanup_stale(entries)
        assert len(entries) == 0

    def test_alive_recent_heartbeat_kept(self):
        """Process alive with recent heartbeat → keep."""
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        entries = [
            {
                "branch": "feature/fresh",
                "pid": os.getpid(),
                "submitted_at": now,
                "heartbeat": now,
            }
        ]
        _cleanup_stale(entries)
        assert len(entries) == 1

    def test_no_pid_skipped(self):
        entries = [{"branch": "feature/nopid"}]
        _cleanup_stale(entries)
        assert len(entries) == 1

    def test_mixed_stale_and_alive(self):
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        entries = [
            {"branch": "dead", "pid": 99999, "submitted_at": now, "heartbeat": now},
            {"branch": "alive", "pid": os.getpid(), "submitted_at": now, "heartbeat": now},
            {"branch": "also-dead", "pid": 99998, "submitted_at": now, "heartbeat": now},
        ]
        _cleanup_stale(entries)
        assert len(entries) == 1
        assert entries[0]["branch"] == "alive"


# --- Remove from Queue ---


class TestRemoveFromQueue:
    def test_remove_existing(self, tmp_path):
        repo = str(tmp_path)
        with _flock(_queue_lock(repo)):
            _write_queue(
                repo,
                [
                    {"branch": "feature/a"},
                    {"branch": "feature/b"},
                ],
            )
        _remove_from_queue(repo, "feature/a")
        with _flock(_queue_lock(repo)):
            result = _read_queue(repo)
        assert len(result) == 1
        assert result[0]["branch"] == "feature/b"

    def test_remove_nonexistent_noop(self, tmp_path):
        repo = str(tmp_path)
        with _flock(_queue_lock(repo)):
            _write_queue(repo, [{"branch": "feature/a"}])
        _remove_from_queue(repo, "feature/nonexistent")
        with _flock(_queue_lock(repo)):
            result = _read_queue(repo)
        assert len(result) == 1

    def test_remove_from_empty(self, tmp_path):
        repo = str(tmp_path)
        _remove_from_queue(repo, "feature/anything")
        with _flock(_queue_lock(repo)):
            result = _read_queue(repo)
        assert result == []


# --- File Locking ---


class TestFlock:
    def test_lock_creates_file(self, tmp_path):
        lock_path = str(tmp_path / "test.lock")
        assert not os.path.exists(lock_path)
        with _flock(lock_path):
            assert os.path.exists(lock_path)

    def test_lock_reentrant_same_process(self, tmp_path):
        """flock is per-process, so same process can re-acquire."""
        lock_path = str(tmp_path / "test.lock")
        with _flock(lock_path), _flock(lock_path):
            pass  # Should not deadlock


# --- Merge Execution (real git repos) ---


class TestExecuteMerge:
    def test_clean_squash_merge(self, tmp_path):
        repo = make_repo(tmp_path)
        make_feature_branch(repo, "feature/test", {"new.txt": "content"})

        status, detail = _execute_merge(str(repo), "feature/test")

        assert status == "merged"
        assert len(detail) > 0  # short SHA
        # Verify squash: single parent commit (not a merge commit)
        parents = git(repo, "log", "-1", "--format=%P", "main")
        assert len(parents.split()) == 1
        # Verify file exists on main
        assert (repo / "new.txt").exists()

    def test_needs_rebase_when_main_moved(self, tmp_path):
        repo = make_repo(tmp_path)
        make_feature_branch(repo, "feature/test", {"new.txt": "content"})
        advance_main(repo)

        status, detail = _execute_merge(str(repo), "feature/test")

        assert status == "needs_rebase"
        assert "merge-base" in detail
        # Verify main unchanged
        assert not (repo / "new.txt").exists()

    def test_no_commits_to_merge(self, tmp_path):
        repo = make_repo(tmp_path)
        # Create branch at same commit as main (no new commits)
        git(repo, "branch", "feature/empty")

        status, detail = _execute_merge(str(repo), "feature/empty")

        assert status == "error"
        assert "no commits to merge" in detail

    def test_wrong_branch_on_main_repo(self, tmp_path):
        repo = make_repo(tmp_path)
        make_feature_branch(repo, "feature/test", {"new.txt": "content"})
        git(repo, "checkout", "feature/test")  # Switch away from main

        status, detail = _execute_merge(str(repo), "feature/test")

        assert status == "error"
        assert "expected 'main'" in detail

    def test_custom_commit_message(self, tmp_path):
        repo = make_repo(tmp_path)
        make_feature_branch(repo, "feature/test", {"new.txt": "content"})

        status, _ = _execute_merge(str(repo), "feature/test", message="Custom msg")

        assert status == "merged"
        msg = git(repo, "log", "-1", "--format=%s", "main")
        assert msg == "Custom msg"

    def test_default_commit_message_includes_branch_and_commits(self, tmp_path):
        repo = make_repo(tmp_path)
        make_feature_branch(
            repo,
            "feature/test",
            {
                "a.txt": "a",
                "b.txt": "b",
            },
        )

        status, _ = _execute_merge(str(repo), "feature/test")

        assert status == "merged"
        full_msg = git(repo, "log", "-1", "--format=%B", "main")
        assert "feature/test" in full_msg
        assert "Add a.txt" in full_msg
        assert "Add b.txt" in full_msg

    def test_squash_produces_single_commit(self, tmp_path):
        """Multiple commits on feature branch → one squash commit on main."""
        repo = make_repo(tmp_path)
        make_feature_branch(
            repo,
            "feature/multi",
            {
                "a.txt": "a",
                "b.txt": "b",
                "c.txt": "c",
            },
        )
        main_head_before = git(repo, "rev-parse", "main")

        status, _ = _execute_merge(str(repo), "feature/multi")

        assert status == "merged"
        # Count commits between old main and new main
        count = git(repo, "rev-list", "--count", f"{main_head_before}..main")
        assert count == "1"
        # All files present
        assert (repo / "a.txt").exists()
        assert (repo / "b.txt").exists()
        assert (repo / "c.txt").exists()

    def test_linear_history_after_merge(self, tmp_path):
        """Verify no merge commits — linear history."""
        repo = make_repo(tmp_path)
        make_feature_branch(repo, "feature/test", {"new.txt": "content"})

        _execute_merge(str(repo), "feature/test")

        # Check all commits have exactly one parent (linear)
        parents_list = git(repo, "log", "--format=%P", "main")
        for line in parents_list.strip().split("\n"):
            if line.strip():  # Skip root commit (empty parents)
                assert len(line.split()) == 1, f"Merge commit found: {line}"

    def test_dirty_index_cleanup(self, tmp_path):
        """If index is dirty from interrupted merge, it should be cleaned up."""
        repo = make_repo(tmp_path)
        make_feature_branch(repo, "feature/test", {"new.txt": "content"})

        # Dirty the index (simulating interrupted squash merge)
        (repo / "dirty.txt").write_text("dirty")
        git(repo, "add", "dirty.txt")

        status, _ = _execute_merge(str(repo), "feature/test")

        assert status == "merged"
        # dirty.txt should NOT be on main (it was cleaned up)
        assert not (repo / "dirty.txt").exists()

    def test_main_unchanged_on_needs_rebase(self, tmp_path):
        """When merge fails with needs_rebase, main HEAD must not move."""
        repo = make_repo(tmp_path)
        make_feature_branch(repo, "feature/test", {"new.txt": "content"})
        advance_main(repo)
        head_before = git(repo, "rev-parse", "main")

        status, _ = _execute_merge(str(repo), "feature/test")

        assert status == "needs_rebase"
        head_after = git(repo, "rev-parse", "main")
        assert head_before == head_after

    def test_rebase_then_merge_succeeds(self, tmp_path):
        """Full cycle: merge fails, rebase, merge succeeds."""
        repo = make_repo(tmp_path)
        make_feature_branch(repo, "feature/test", {"new.txt": "content"})
        advance_main(repo)

        # First attempt fails
        status, _ = _execute_merge(str(repo), "feature/test")
        assert status == "needs_rebase"

        # Rebase the feature branch
        git(repo, "checkout", "feature/test")
        git(repo, "rebase", "main")
        git(repo, "checkout", "main")

        # Second attempt succeeds
        status, detail = _execute_merge(str(repo), "feature/test")
        assert status == "merged"
        assert (repo / "new.txt").exists()

    def test_nonexistent_branch(self, tmp_path):
        repo = make_repo(tmp_path)

        status, detail = _execute_merge(str(repo), "feature/nonexistent")

        assert status == "error"
        assert "merge-base" in detail or "not" in detail.lower()

    def test_sequential_merges(self, tmp_path):
        """Two branches merged sequentially — second needs rebase after first."""
        repo = make_repo(tmp_path)
        make_feature_branch(repo, "feature/first", {"first.txt": "1"})
        make_feature_branch(repo, "feature/second", {"second.txt": "2"})

        # First merge succeeds
        status, _ = _execute_merge(str(repo), "feature/first")
        assert status == "merged"

        # Second should need rebase (main moved from first merge)
        status, _ = _execute_merge(str(repo), "feature/second")
        assert status == "needs_rebase"

        # Rebase and retry
        git(repo, "checkout", "feature/second")
        git(repo, "rebase", "main")
        git(repo, "checkout", "main")

        status, _ = _execute_merge(str(repo), "feature/second")
        assert status == "merged"

        # Both files present, linear history
        assert (repo / "first.txt").exists()
        assert (repo / "second.txt").exists()
        count = git(repo, "rev-list", "--count", "main")
        assert int(count) == 3  # initial + 2 squash commits
