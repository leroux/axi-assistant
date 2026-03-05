"""Unit tests for supervisor bridge-killing and process lifecycle helpers."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

from axi.supervisor import _kill_bridge, _pid_alive


class TestPidAlive:
    def test_returns_true_for_self(self) -> None:
        assert _pid_alive(os.getpid()) is True

    def test_returns_false_for_dead_pid(self) -> None:
        # Spawn and immediately kill a process to get a dead PID
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        # PID should be dead now (or recycled, but very unlikely in test)
        # Give OS a moment to clean up
        time.sleep(0.1)
        # Note: _pid_alive uses ProcessLookupError, zombie processes
        # still exist until waited. Since we called wait(), it's reaped.
        assert _pid_alive(proc.pid) is False

    def test_returns_false_for_nonexistent_pid(self) -> None:
        # Use a very high PID that almost certainly doesn't exist
        assert _pid_alive(4_000_000) is False


class TestKillBridge:
    def test_no_processes_found(self) -> None:
        """_kill_bridge doesn't crash when pgrep finds nothing."""
        with patch("axi.supervisor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=1)
            with patch("axi.supervisor.DIR") as mock_dir:
                mock_dir.__truediv__ = lambda self, x: MagicMock(
                    __str__=lambda s: "/fake/.bridge.sock",
                    exists=lambda: False,
                )
                _kill_bridge()
            # Should not raise

    def test_sigkill_escalation(self) -> None:
        """If SIGTERM doesn't kill the bridge within timeout, SIGKILL is sent."""
        # Start a process that ignores SIGTERM
        proc = subprocess.Popen(
            [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
        )
        try:
            pid = proc.pid

            # Patch pgrep to return our process PID and DIR to match
            with (
                patch("axi.supervisor.subprocess.run") as mock_pgrep,
                patch("axi.supervisor.DIR", new=MagicMock()),
            ):
                mock_pgrep.return_value = MagicMock(stdout=str(pid), returncode=0)

                # Patch the socket path existence check
                sock_mock = MagicMock()
                sock_mock.exists.return_value = False
                type(mock_pgrep.return_value).stdout = property(lambda s: str(pid))

                # Override DIR / BRIDGE_SOCKET to return our mock
                import axi.supervisor as sup
                orig_dir = sup.DIR
                try:
                    sup.DIR = MagicMock()
                    sup.DIR.__truediv__ = lambda self, x: sock_mock
                    sock_mock.__str__ = lambda s: "/fake/.bridge.sock"

                    # Re-patch pgrep since DIR changed
                    with patch("axi.supervisor.subprocess.run") as mock_run2:
                        mock_run2.return_value = MagicMock(
                            stdout=str(pid), returncode=0
                        )
                        mock_run2.return_value.stdout = str(pid)

                        t0 = time.monotonic()
                        _kill_bridge()
                        elapsed = time.monotonic() - t0

                        # Should have waited ~5s then SIGKILL'd
                        assert elapsed >= 4.5, f"Should wait before SIGKILL, took {elapsed:.1f}s"
                        assert elapsed < 7.0, f"Shouldn't wait too long, took {elapsed:.1f}s"
                finally:
                    sup.DIR = orig_dir

            # Process should be dead (killed by SIGKILL)
            proc.wait(timeout=2)
            assert proc.returncode is not None
        finally:
            # Safety cleanup
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass

    def test_clean_sigterm_exit(self) -> None:
        """Bridge that responds to SIGTERM exits quickly, no SIGKILL needed.

        In production, the bridge is NOT a child of the supervisor (it's
        reparented to init after bot.py exits), so zombies don't apply.
        We simulate this by reaping the child in a background thread.
        """
        import threading

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
        )
        try:
            pid = proc.pid
            # Reap the child in background so it doesn't become a zombie
            # (simulates the real scenario where init reaps the orphaned bridge)
            threading.Thread(target=proc.wait, daemon=True).start()

            import axi.supervisor as sup
            orig_dir = sup.DIR
            try:
                sock_mock = MagicMock()
                sock_mock.exists.return_value = False
                sock_mock.__str__ = lambda s: "/fake/.bridge.sock"
                sup.DIR = MagicMock()
                sup.DIR.__truediv__ = lambda self, x: sock_mock

                with patch("axi.supervisor.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(
                        stdout=str(pid), returncode=0
                    )
                    mock_run.return_value.stdout = str(pid)

                    t0 = time.monotonic()
                    _kill_bridge()
                    elapsed = time.monotonic() - t0

                    # Should exit quickly (process responds to SIGTERM)
                    assert elapsed < 3.0, f"Clean SIGTERM exit took {elapsed:.1f}s — too slow"
            finally:
                sup.DIR = orig_dir
        finally:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
