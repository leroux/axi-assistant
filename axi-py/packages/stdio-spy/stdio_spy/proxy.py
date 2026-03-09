"""stdio-spy: bidirectional stdio proxy with logging.

Sits between a parent and child process, forwarding stdin/stdout/stderr
while logging all traffic to a file with timestamps and direction markers.

Uses a PTY for stdin/stdout (so TUI apps like Claude CLI see a real terminal)
and a separate pipe for stderr (so stderr is logged distinctly).
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import pty
import select
import signal
import sys
import time
from logging.handlers import RotatingFileHandler


def _setup_logger(log_path: str) -> logging.Logger:
    """Create a logger matching procmux stdio log format."""
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    logger = logging.getLogger("stdio_spy")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in logger.handlers[:]:
        logger.removeHandler(h)
    fh = RotatingFileHandler(log_path, maxBytes=50 * 1024 * 1024, backupCount=2)
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fmt.converter = time.gmtime
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def _log_bytes(logger: logging.Logger, direction: str, data: bytes) -> None:
    """Log raw bytes, splitting on newlines so each JSON message gets its own line."""
    text = data.decode("utf-8", errors="replace")
    for line in text.split("\n"):
        if line:
            logger.debug("%s %s", direction, line)


def _set_raw_mode(fd: int) -> list[int] | None:
    """Put a TTY fd into raw mode, return old settings for restore."""
    import termios
    import tty

    try:
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        return old
    except termios.error:
        return None


def _restore_mode(fd: int, old: list[int] | None) -> None:
    """Restore terminal settings."""
    if old is None:
        return
    import termios

    try:
        termios.tcsetattr(fd, termios.TCSAFLUSH, old)
    except termios.error:
        pass


def _copy_terminal_size(src_fd: int, dst_fd: int) -> None:
    """Copy terminal window size from src to dst."""
    import fcntl
    import termios

    try:
        size = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _set_nonblock(fd: int) -> None:
    """Set a file descriptor to non-blocking mode."""
    import fcntl

    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def run_proxy(child_args: list[str], log_path: str) -> int:
    """Spawn child with PTY for stdin/stdout and pipe for stderr.

    Returns the child's exit code.
    """
    import termios

    logger = _setup_logger(log_path)
    logger.debug("--- START pid=%d cmd=%s", os.getpid(), child_args)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    stderr_fd = sys.stderr.fileno()
    stdin_is_tty = os.isatty(stdin_fd)

    # Create PTY for child's stdin/stdout
    master_fd, slave_fd = pty.openpty()

    # Create pipe for child's stderr
    stderr_r, stderr_w = os.pipe()

    child_pid = os.fork()

    if child_pid == 0:
        # Child process
        os.close(master_fd)
        os.close(stderr_r)

        # New session + set slave PTY as controlling terminal
        os.setsid()
        import fcntl

        TIOCSCTTY = getattr(termios, "TIOCSCTTY", 0x540E)
        fcntl.ioctl(slave_fd, TIOCSCTTY, 0)

        # Redirect child's stdin/stdout to PTY slave, stderr to pipe
        os.dup2(slave_fd, 0)  # stdin
        os.dup2(slave_fd, 1)  # stdout
        os.dup2(stderr_w, 2)  # stderr
        if slave_fd > 2:
            os.close(slave_fd)
        if stderr_w > 2:
            os.close(stderr_w)

        os.execvp(child_args[0], child_args)
        os.write(2, f"stdio-spy: exec failed: {child_args[0]}\n".encode())
        os._exit(127)

    # Parent process
    os.close(slave_fd)
    os.close(stderr_w)

    if stdin_is_tty:
        _copy_terminal_size(stdin_fd, master_fd)
    else:
        # When stdin is a pipe, disable PTY echo so input isn't doubled
        try:
            attrs = termios.tcgetattr(master_fd)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(master_fd, termios.TCSANOW, attrs)
        except termios.error:
            pass

    old_stdin_settings = _set_raw_mode(stdin_fd) if stdin_is_tty else None

    _set_nonblock(master_fd)
    _set_nonblock(stderr_r)

    child_exited = False
    exit_status = 0
    stderr_open = True
    original_stdin_fd = stdin_fd  # keep for restore

    def forward_signal(signum: int, _frame: object) -> None:
        try:
            os.kill(child_pid, signum)
        except ProcessLookupError:
            pass

    def handle_sigwinch(_signum: int, _frame: object) -> None:
        if stdin_is_tty:
            _copy_terminal_size(original_stdin_fd, master_fd)
            try:
                os.kill(child_pid, signal.SIGWINCH)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGINT, forward_signal)
    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGHUP, forward_signal)
    if hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, handle_sigwinch)

    def drain_fds() -> None:
        """Drain remaining output from master_fd and stderr_r."""
        for fd, label, dest in [(master_fd, "<<< STDOUT", stdout_fd), (stderr_r, "<<< STDERR", stderr_fd)]:
            try:
                while True:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    os.write(dest, data)
                    _log_bytes(logger, label, data)
            except OSError:
                pass

    def reap_child() -> bool:
        """Check if child has exited. Returns True if reaped."""
        nonlocal child_exited, exit_status
        if child_exited:
            return True
        try:
            pid, status = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            child_exited = True
            return True
        if pid == 0:
            return False
        if os.WIFEXITED(status):
            exit_status = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            exit_status = 128 + os.WTERMSIG(status)
        child_exited = True
        return True

    try:
        while True:
            rfds = [master_fd]
            if stderr_open:
                rfds.append(stderr_r)
            if stdin_fd >= 0:
                rfds.append(stdin_fd)

            try:
                readable, _, _ = select.select(rfds, [], [], 0.1)
            except (OSError, ValueError):
                if reap_child():
                    drain_fds()
                    break
                continue

            if master_fd in readable:
                try:
                    data = os.read(master_fd, 65536)
                    if not data:
                        break
                    os.write(stdout_fd, data)
                    _log_bytes(logger, "<<< STDOUT", data)
                except OSError as e:
                    if e.errno == errno.EIO:
                        # PTY closed (child exited)
                        break
                    raise

            if stderr_r in readable:
                try:
                    data = os.read(stderr_r, 65536)
                    if not data:
                        stderr_open = False
                    else:
                        os.write(stderr_fd, data)
                        _log_bytes(logger, "<<< STDERR", data)
                except OSError as e:
                    if e.errno == errno.EIO:
                        stderr_open = False
                    else:
                        raise

            if stdin_fd >= 0 and stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 65536)
                    if not data:
                        # EOF on stdin — send EOT to PTY, stop polling
                        try:
                            os.write(master_fd, b"\x04")
                        except OSError:
                            pass
                        stdin_fd = -1
                        continue
                    os.write(master_fd, data)
                    _log_bytes(logger, ">>> STDIN ", data)
                except OSError as e:
                    if e.errno in (errno.EIO, errno.EBADF):
                        stdin_fd = -1
                    else:
                        raise

            if reap_child():
                # Child exited — drain remaining output then stop
                drain_fds()
                break

    except Exception:
        import traceback

        logger.debug("--- ERROR %s", traceback.format_exc())
        raise
    finally:
        _restore_mode(original_stdin_fd, old_stdin_settings)

        if not child_exited:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                _, status = os.waitpid(child_pid, 0)
                if os.WIFEXITED(status):
                    exit_status = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    exit_status = 128 + os.WTERMSIG(status)
            except ChildProcessError:
                pass

        for fd in (master_fd, stderr_r):
            try:
                os.close(fd)
            except OSError:
                pass

        logger.debug("--- EXIT code=%d", exit_status)

    return exit_status


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="stdio-spy",
        description="Bidirectional stdio proxy/logger. Forwards stdin/stdout/stderr "
        "while logging all traffic to a file.",
    )
    parser.add_argument(
        "--log",
        required=True,
        help="Path to the log file",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run (use -- before the command)",
    )
    args = parser.parse_args()

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if not cmd:
        parser.error("No command specified. Usage: stdio-spy --log FILE -- COMMAND [ARGS...]")

    sys.exit(run_proxy(cmd, args.log))
