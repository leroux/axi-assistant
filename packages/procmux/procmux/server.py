"""Procmux server — runs as a separate process, managing subprocesses.

Spawns named OS subprocesses with stdin/stdout/stderr pipes, multiplexes
them over one Unix socket connection, and buffers output when the client
is disconnected.  Zero intelligence — no knowledge of Claude, agents,
sessions, or any semantic layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Any, cast

from .protocol import (
    CmdMsg,
    ExitMsg,
    ResultMsg,
    StderrMsg,
    StdinMsg,
    StdoutMsg,
    parse_client_msg,
)

log = logging.getLogger(__name__)


@dataclass
class ManagedProcess:
    """A subprocess managed by procmux."""

    name: str
    proc: asyncio.subprocess.Process
    status: str = "running"  # "running" | "exited"
    exit_code: int | None = None
    buffer: list[StdoutMsg | StderrMsg | ExitMsg] = field(default_factory=lambda: list[StdoutMsg | StderrMsg | ExitMsg]())
    subscribed: bool = False  # whether the client is receiving output
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    last_stdin_at: float = 0.0  # monotonic timestamp of last stdin write
    last_stdout_at: float = 0.0  # monotonic timestamp of last stdout message

    @property
    def idle(self) -> bool:
        """True if the process is between turns (not mid-task).

        Inferred from stdin/stdout timing:
          last_stdout >= last_stdin → finished responding → idle
          last_stdin > last_stdout  → given work, no response yet → busy
          no stdin ever (0.0)       → never queried → idle
        """
        return self.last_stdout_at >= self.last_stdin_at


class ProcmuxServer:
    """The procmux relay process. Manages subprocesses and relays I/O to the client."""

    def __init__(self, socket_path: str):
        self._socket_path = socket_path
        self._procs: dict[str, ManagedProcess] = {}
        self._client_writer: asyncio.StreamWriter | None = None
        self._client_lock = asyncio.Lock()  # protects _client_writer
        self._server: asyncio.Server | None = None
        self._shutdown_event = asyncio.Event()
        self._start_time = time.monotonic()
        self._stdio_loggers: dict[str, logging.Logger] = {}
        # Derive log dir from socket path (sibling "logs/" directory)
        self._stdio_log_dir = os.environ.get(
            "BRIDGE_STDIO_LOG_DIR",
            os.path.join(os.path.dirname(os.path.abspath(socket_path)), "logs"),
        )
        os.makedirs(self._stdio_log_dir, exist_ok=True)

    def _get_stdio_logger(self, name: str) -> logging.Logger:
        """Get or create a per-process stdio logger with rotating file handler."""
        if name in self._stdio_loggers:
            return self._stdio_loggers[name]
        logger = logging.getLogger(f"procmux.stdio.{name}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        if not logger.handlers:
            fh = RotatingFileHandler(
                os.path.join(self._stdio_log_dir, f"procmux-stdio-{name}.log"),
                maxBytes=5 * 1024 * 1024,  # 5 MB
                backupCount=2,
            )
            fh.setLevel(logging.DEBUG)
            fmt = logging.Formatter("%(asctime)s %(message)s")
            fmt.converter = time.gmtime
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        self._stdio_loggers[name] = logger
        return logger

    def _close_stdio_logger(self, name: str) -> None:
        """Close and remove a per-process stdio logger."""
        logger = self._stdio_loggers.pop(name, None)
        if logger:
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)

    async def start(self):
        """Start listening on the Unix socket."""
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self._socket_path,
            limit=10 * 1024 * 1024,  # 10 MB
        )
        log.info("Procmux listening on %s", self._socket_path)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self._shutdown_event.set())

        await self._shutdown_event.wait()
        await self._shutdown()

    async def _shutdown(self):
        """Gracefully shut down: kill all processes, close socket."""
        log.info("Procmux shutting down, killing %d process(es)", len(self._procs))
        for mp in list(self._procs.values()):
            await self._kill_process(mp)
        if self._server:
            self._server.close()
            # Force-close the client writer so _handle_client exits promptly
            async with self._client_lock:
                writer = self._client_writer
                self._client_writer = None
            if writer is not None:
                try:
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=3.0)
            except TimeoutError:
                log.warning("wait_closed() timed out — proceeding with shutdown")
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        log.info("Procmux shutdown complete")

    # -- Client connection handling --

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a client connection. Only one client at a time."""
        async with self._client_lock:
            old_writer = self._client_writer
            self._client_writer = writer
            for mp in self._procs.values():
                mp.subscribed = False

        if old_writer is not None:
            log.warning("New client connected — dropping previous connection")
            try:
                old_writer.close()
                await old_writer.wait_closed()
            except Exception:
                pass

        log.info("Client connected")

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg: CmdMsg | StdinMsg = parse_client_msg(line)
                except Exception:
                    log.warning("Invalid message from client: %r", line[:200], exc_info=True)
                    continue

                if isinstance(msg, CmdMsg):
                    await self._handle_command(msg)
                else:
                    await self._handle_stdin(msg)
        except (ConnectionError, OSError) as e:
            log.info("Client disconnected: %s", e)
        except Exception:
            log.exception("Error handling client")
        finally:
            async with self._client_lock:
                if self._client_writer is writer:
                    self._client_writer = None
                    for mp in self._procs.values():
                        mp.subscribed = False
            log.info("Client disconnected — buffering all output")

    # -- Command handling --

    async def _handle_command(self, msg: CmdMsg):
        cmd = msg.cmd
        name = msg.name

        if cmd == "spawn":
            await self._cmd_spawn(msg)
        elif cmd == "kill":
            await self._cmd_kill(name)
        elif cmd == "interrupt":
            await self._cmd_interrupt(name)
        elif cmd == "subscribe":
            await self._cmd_subscribe(name)
        elif cmd == "unsubscribe":
            await self._cmd_unsubscribe(name)
        elif cmd == "list":
            await self._cmd_list()
        elif cmd == "status":
            await self._cmd_status()
        else:
            await self._send_result(ResultMsg(ok=False, error=f"unknown command: {cmd}"))

    async def _cmd_spawn(self, msg: CmdMsg):
        name = msg.name

        if name in self._procs and self._procs[name].status == "running":
            mp = self._procs[name]
            await self._send_result(
                ResultMsg(
                    ok=True,
                    name=name,
                    pid=mp.proc.pid,
                    already_running=True,
                )
            )
            return

        self._procs.pop(name, None)

        try:
            proc = await asyncio.create_subprocess_exec(
                *msg.cli_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=msg.cwd,
                env=msg.env,
                limit=10 * 1024 * 1024,  # 10 MB
                start_new_session=True,
            )
        except Exception as e:
            await self._send_result(ResultMsg(ok=False, name=name, error=str(e)))
            return

        mp = ManagedProcess(name=name, proc=proc)
        mp.stdout_task = asyncio.create_task(self._relay_stdout(mp))
        mp.stderr_task = asyncio.create_task(self._relay_stderr(mp))
        self._procs[name] = mp

        log.info("Spawned process '%s' (pid=%d)", name, proc.pid)
        await self._send_result(ResultMsg(ok=True, name=name, pid=proc.pid))

    async def _cmd_kill(self, name: str):
        mp = self._procs.get(name)
        if mp is None:
            await self._send_result(ResultMsg(ok=False, name=name, error="not found"))
            return
        await self._kill_process(mp)
        self._procs.pop(name, None)
        await self._send_result(ResultMsg(ok=True, name=name))

    async def _cmd_interrupt(self, name: str):
        mp = self._procs.get(name)
        if mp is None or mp.status != "running":
            await self._send_result(ResultMsg(ok=False, name=name, error="not running"))
            return
        try:
            os.killpg(os.getpgid(mp.proc.pid), signal.SIGINT)
            await self._send_result(ResultMsg(ok=True, name=name))
        except Exception as e:
            await self._send_result(ResultMsg(ok=False, name=name, error=str(e)))

    async def _cmd_subscribe(self, name: str):
        mp = self._procs.get(name)
        if mp is None:
            await self._send_result(ResultMsg(ok=False, name=name, error="not found"))
            return

        buffered_count = len(mp.buffer)
        log.debug("[subscribe][%s] replaying %d buffered messages", name, buffered_count)

        # CRITICAL: Write all buffered messages synchronously (no await between writes)
        # to prevent interleaving with live relay messages.
        writer = self._client_writer
        if writer is not None:
            for msg in mp.buffer:
                payload = msg.model_dump_json().encode() + b"\n"
                log.debug("[subscribe][%s] replay-write %s (%d bytes)", name, type(msg).__name__, len(payload))
                writer.write(payload)
        mp.buffer.clear()
        mp.subscribed = True

        if writer is not None:
            try:
                await writer.drain()
            except (ConnectionError, BrokenPipeError, OSError):
                log.warning("[subscribe][%s] drain failed — client disconnected", name)
                async with self._client_lock:
                    if self._client_writer is writer:
                        self._client_writer = None
                        for mp2 in self._procs.values():
                            mp2.subscribed = False
                return

        await self._send_result(
            ResultMsg(
                ok=True,
                name=name,
                replayed=buffered_count,
                status=mp.status,
                exit_code=mp.exit_code,
                idle=mp.idle,
            )
        )

    async def _cmd_unsubscribe(self, name: str):
        mp = self._procs.get(name)
        if mp is None:
            await self._send_result(ResultMsg(ok=False, name=name, error="not found"))
            return
        mp.subscribed = False
        await self._send_result(ResultMsg(ok=True, name=name))

    async def _cmd_list(self):
        procs: dict[str, Any] = {}
        for name, mp in self._procs.items():
            procs[name] = {
                "pid": mp.proc.pid,
                "status": mp.status,
                "exit_code": mp.exit_code,
                "buffered_msgs": len(mp.buffer),
                "subscribed": mp.subscribed,
                "idle": mp.idle,
            }
        await self._send_result(ResultMsg(ok=True, agents=procs))

    async def _cmd_status(self):
        uptime = time.monotonic() - self._start_time
        await self._send_result(ResultMsg(ok=True, uptime_seconds=int(uptime)))

    # -- stdin forwarding --

    async def _handle_stdin(self, msg: StdinMsg):
        mp = self._procs.get(msg.name)
        if mp is None or mp.status != "running" or mp.proc.stdin is None:
            return
        try:
            line = json.dumps(msg.data) + "\n"
            log.debug("[stdin][%s] forwarding %d bytes: %.200s", msg.name, len(line), line.rstrip())
            self._get_stdio_logger(msg.name).debug(">>> STDIN  %s", line.rstrip())
            mp.proc.stdin.write(line.encode())
            await mp.proc.stdin.drain()
            mp.last_stdin_at = asyncio.get_running_loop().time()
        except (ConnectionError, OSError):
            log.warning("Failed to write to process '%s' stdin", msg.name)

    # -- stdout/stderr relay --

    async def _relay_or_buffer(self, mp: ManagedProcess, msg: StdoutMsg | StderrMsg | ExitMsg):
        """Send to client if subscribed, otherwise buffer."""
        if mp.subscribed:
            log.debug("[relay][%s] relaying %s to client", mp.name, type(msg).__name__)
            await self._send_to_client(msg)
        else:
            mp.buffer.append(msg)
            log.debug("[relay][%s] buffering %s (buffer_size=%d)", mp.name, type(msg).__name__, len(mp.buffer))

    async def _relay_stdout(self, mp: ManagedProcess):
        """Read JSON lines from process stdout, relay or buffer."""
        assert mp.proc.stdout is not None
        normal_eof = False
        stdio_log = self._get_stdio_logger(mp.name)
        try:
            while True:
                line = await mp.proc.stdout.readline()
                if not line:
                    normal_eof = True
                    break
                raw = line.decode().strip()
                log.debug("[stdout][%s] raw line (%d bytes): %.200s", mp.name, len(line), raw)
                stdio_log.debug("<<< STDOUT %s", raw)
                try:
                    data: Any = json.loads(raw)
                except json.JSONDecodeError:
                    log.debug("[stdout][%s] json.loads FAILED on: %.200s", mp.name, raw)
                    if raw:
                        await self._relay_or_buffer(mp, StderrMsg(name=mp.name, text=raw))
                    continue

                log.debug("[stdout][%s] parsed msg", mp.name)
                mp.last_stdout_at = asyncio.get_running_loop().time()
                stdout_data = cast("dict[str, Any]", data) if isinstance(data, dict) else {"raw": data}
                await self._relay_or_buffer(mp, StdoutMsg(name=mp.name, data=stdout_data))
        except Exception:
            log.exception("Error relaying stdout for '%s'", mp.name)
        finally:
            if normal_eof:
                try:
                    await asyncio.wait_for(mp.proc.wait(), timeout=10.0)
                except (TimeoutError, Exception):
                    pass
                mp.status = "exited"
                mp.exit_code = mp.proc.returncode
                await self._relay_or_buffer(mp, ExitMsg(name=mp.name, code=mp.exit_code))
                log.info("Process '%s' exited (code=%s)", mp.name, mp.exit_code)
            else:
                log.error(
                    "stdout relay for '%s' failed — process still running (pid=%s), "
                    "relay is dead. Kill and respawn to recover.",
                    mp.name,
                    mp.proc.pid,
                )

    async def _relay_stderr(self, mp: ManagedProcess):
        """Read lines from process stderr, relay or buffer."""
        assert mp.proc.stderr is not None
        stdio_log = self._get_stdio_logger(mp.name)
        try:
            while True:
                line = await mp.proc.stderr.readline()
                if not line:
                    break
                text = line.decode().strip()
                if not text:
                    continue
                stdio_log.debug("<<< STDERR %s", text)
                await self._relay_or_buffer(mp, StderrMsg(name=mp.name, text=text))
        except Exception:
            log.exception("Error relaying stderr for '%s'", mp.name)

    # -- Helpers --

    async def _kill_process(self, mp: ManagedProcess):
        """Terminate a managed process and its entire process group.

        Sends SIGTERM to the process group first (graceful), then escalates
        to SIGKILL after 5 seconds.  Uses os.killpg() so child processes
        (e.g. inner Claude CLI spawned by the flowcoder engine) are killed too.
        """
        if mp.status != "running":
            return
        try:
            pgid = os.getpgid(mp.proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(mp.proc.wait(), timeout=5.0)
            except TimeoutError:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await mp.proc.wait()
        except ProcessLookupError:
            pass
        except Exception:
            log.exception("Error killing process '%s'", mp.name)
        self._get_stdio_logger(mp.name).debug("--- KILLED (exit_code=%s)", mp.proc.returncode)
        self._close_stdio_logger(mp.name)
        mp.status = "exited"
        mp.exit_code = mp.proc.returncode
        if mp.stdout_task:
            mp.stdout_task.cancel()
        if mp.stderr_task:
            mp.stderr_task.cancel()

    async def _send_to_client(self, msg: ResultMsg | StdoutMsg | StderrMsg | ExitMsg) -> bool:
        """Send a message to the connected client. Returns False if no client."""
        writer = self._client_writer
        if writer is None:
            return False
        try:
            payload = msg.model_dump_json().encode() + b"\n"
            name = getattr(msg, "name", None)
            log.debug("[send][%s] %s (%d bytes)", name or "cmd", type(msg).__name__, len(payload))
            writer.write(payload)
            await writer.drain()
            return True
        except (ConnectionError, BrokenPipeError, OSError):
            async with self._client_lock:
                if self._client_writer is writer:
                    self._client_writer = None
                    for mp in self._procs.values():
                        mp.subscribed = False
            return False

    async def _send_result(self, result: ResultMsg):
        """Send a command result to the client."""
        await self._send_to_client(result)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <socket_path>", file=sys.stderr)
        sys.exit(1)

    socket_path = sys.argv[1]

    log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s [procmux] %(message)s",
        stream=sys.stderr,
    )

    server = ProcmuxServer(socket_path)
    asyncio.run(server.start())
