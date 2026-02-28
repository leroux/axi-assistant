"""Bridge server — runs as a separate process, managing CLI subprocesses."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
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
class CliProcess:
    """A CLI subprocess managed by the bridge."""

    name: str
    proc: asyncio.subprocess.Process
    status: str = "running"  # "running" | "exited"
    exit_code: int | None = None
    buffer: list[StdoutMsg | StderrMsg | ExitMsg] = field(default_factory=lambda: list[StdoutMsg | StderrMsg | ExitMsg]())
    subscribed: bool = False  # whether bot.py is receiving output
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    last_stdin_at: float = 0.0  # monotonic timestamp of last stdin write
    last_stdout_at: float = 0.0  # monotonic timestamp of last stdout message

    @property
    def idle(self) -> bool:
        """True if the agent is between turns (not mid-task).

        Inferred from stdin/stdout timing:
          last_stdout >= last_stdin → finished responding → idle
          last_stdin > last_stdout  → given work, no response yet → busy
          no stdin ever (0.0)       → never queried → idle
        """
        return self.last_stdout_at >= self.last_stdin_at


class BridgeServer:
    """The bridge relay process. Manages CLI subprocesses and relays to bot.py."""

    def __init__(self, socket_path: str):
        self._socket_path = socket_path
        self._cli_procs: dict[str, CliProcess] = {}
        self._client_writer: asyncio.StreamWriter | None = None
        self._client_lock = asyncio.Lock()  # protects _client_writer
        self._server: asyncio.Server | None = None
        self._shutdown_event = asyncio.Event()
        self._start_time = time.monotonic()

    async def start(self):
        """Start listening on the Unix socket."""
        # Clean up stale socket
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self._socket_path,
            limit=10 * 1024 * 1024,  # 10 MB — match CLI subprocess limit
        )
        log.info("Bridge listening on %s", self._socket_path)

        # Install signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self._shutdown_event.set())

        await self._shutdown_event.wait()
        await self._shutdown()

    async def _shutdown(self):
        """Gracefully shut down: kill all CLIs, close socket."""
        log.info("Bridge shutting down, killing %d CLI(s)", len(self._cli_procs))
        for cp in list(self._cli_procs.values()):
            await self._kill_cli(cp)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        log.info("Bridge shutdown complete")

    # -- Client connection handling --

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a bot.py connection. Only one client at a time."""
        async with self._client_lock:
            old_writer = self._client_writer
            self._client_writer = writer
            # Unsubscribe all agents (new client must re-subscribe)
            for cp in self._cli_procs.values():
                cp.subscribed = False

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
                    break  # Client disconnected
                try:
                    msg: CmdMsg | StdinMsg = parse_client_msg(line)
                except Exception:
                    log.warning("Invalid message from client: %r", line[:200], exc_info=True)
                    continue

                if isinstance(msg, CmdMsg):
                    await self._handle_command(msg)
                else:  # StdinMsg
                    await self._handle_stdin(msg)
        except (ConnectionError, OSError) as e:
            log.info("Client disconnected: %s", e)
        except Exception:
            log.exception("Error handling client")
        finally:
            async with self._client_lock:
                if self._client_writer is writer:
                    self._client_writer = None
                    for cp in self._cli_procs.values():
                        cp.subscribed = False
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

        if name in self._cli_procs and self._cli_procs[name].status == "running":
            cp = self._cli_procs[name]
            await self._send_result(
                ResultMsg(
                    ok=True,
                    name=name,
                    pid=cp.proc.pid,
                    already_running=True,
                )
            )
            return

        # Clean up dead entry if exists
        self._cli_procs.pop(name, None)

        try:
            proc = await asyncio.create_subprocess_exec(
                *msg.cli_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=msg.cwd,
                env=msg.env,
                limit=10 * 1024 * 1024,  # 10 MB — Claude SDK can emit large JSON lines
                start_new_session=True,  # own process group so SIGINT reaches Task subagents
            )
        except Exception as e:
            await self._send_result(ResultMsg(ok=False, name=name, error=str(e)))
            return

        cp = CliProcess(name=name, proc=proc)
        cp.stdout_task = asyncio.create_task(self._relay_stdout(cp))
        cp.stderr_task = asyncio.create_task(self._relay_stderr(cp))
        self._cli_procs[name] = cp

        log.info("Spawned CLI '%s' (pid=%d)", name, proc.pid)
        await self._send_result(ResultMsg(ok=True, name=name, pid=proc.pid))

    async def _cmd_kill(self, name: str):
        cp = self._cli_procs.get(name)
        if cp is None:
            await self._send_result(ResultMsg(ok=False, name=name, error="not found"))
            return
        await self._kill_cli(cp)
        self._cli_procs.pop(name, None)
        await self._send_result(ResultMsg(ok=True, name=name))

    async def _cmd_interrupt(self, name: str):
        cp = self._cli_procs.get(name)
        if cp is None or cp.status != "running":
            await self._send_result(ResultMsg(ok=False, name=name, error="not running"))
            return
        try:
            os.killpg(os.getpgid(cp.proc.pid), signal.SIGINT)
            await self._send_result(ResultMsg(ok=True, name=name))
        except Exception as e:
            await self._send_result(ResultMsg(ok=False, name=name, error=str(e)))

    async def _cmd_subscribe(self, name: str):
        cp = self._cli_procs.get(name)
        if cp is None:
            await self._send_result(ResultMsg(ok=False, name=name, error="not found"))
            return

        buffered_count = len(cp.buffer)
        log.debug("[subscribe][%s] replaying %d buffered messages", name, buffered_count)

        # CRITICAL: Write all buffered messages to the socket buffer synchronously
        # (no await between writes) to prevent interleaving with live relay messages.
        # writer.write() is sync — the event loop cannot yield between calls —
        # so all buffered payloads land in the socket buffer before any new messages
        # from _relay_stdout can be written.
        writer = self._client_writer
        if writer is not None:
            for msg in cp.buffer:
                payload = msg.model_dump_json().encode() + b"\n"
                log.debug("[subscribe][%s] replay-write %s (%d bytes)", name, type(msg).__name__, len(payload))
                writer.write(payload)
        cp.buffer.clear()
        cp.subscribed = True  # NOW relay sends directly — after all buffered writes

        # Safe to yield — buffered bytes are already ahead of any new relay bytes
        if writer is not None:
            try:
                await writer.drain()
            except (ConnectionError, BrokenPipeError, OSError):
                log.warning("[subscribe][%s] drain failed — client disconnected", name)
                async with self._client_lock:
                    if self._client_writer is writer:
                        self._client_writer = None
                        for cp2 in self._cli_procs.values():
                            cp2.subscribed = False
                return

        await self._send_result(
            ResultMsg(
                ok=True,
                name=name,
                replayed=buffered_count,
                status=cp.status,
                exit_code=cp.exit_code,
                idle=cp.idle,
            )
        )

    async def _cmd_unsubscribe(self, name: str):
        cp = self._cli_procs.get(name)
        if cp is None:
            await self._send_result(ResultMsg(ok=False, name=name, error="not found"))
            return
        cp.subscribed = False
        await self._send_result(ResultMsg(ok=True, name=name))

    async def _cmd_list(self):
        agents: dict[str, Any] = {}
        for name, cp in self._cli_procs.items():
            agents[name] = {
                "pid": cp.proc.pid,
                "status": cp.status,
                "exit_code": cp.exit_code,
                "buffered_msgs": len(cp.buffer),
                "subscribed": cp.subscribed,
                "idle": cp.idle,
            }
        await self._send_result(ResultMsg(ok=True, agents=agents))

    async def _cmd_status(self):
        uptime = time.monotonic() - self._start_time
        await self._send_result(ResultMsg(ok=True, uptime_seconds=int(uptime)))

    # -- stdin forwarding --

    async def _handle_stdin(self, msg: StdinMsg):
        cp = self._cli_procs.get(msg.name)
        if cp is None or cp.status != "running" or cp.proc.stdin is None:
            return
        try:
            line = json.dumps(msg.data) + "\n"
            log.debug("[stdin][%s] forwarding %d bytes: %.200s", msg.name, len(line), line.rstrip())
            cp.proc.stdin.write(line.encode())
            await cp.proc.stdin.drain()
            cp.last_stdin_at = asyncio.get_running_loop().time()
        except (ConnectionError, OSError):
            log.warning("Failed to write to CLI '%s' stdin", msg.name)

    # -- stdout/stderr relay --

    async def _relay_or_buffer(self, cp: CliProcess, msg: StdoutMsg | StderrMsg | ExitMsg):
        """Send to client if subscribed, otherwise buffer."""
        if cp.subscribed:
            log.debug("[relay][%s] relaying %s to client", cp.name, type(msg).__name__)
            await self._send_to_client(msg)
        else:
            cp.buffer.append(msg)
            log.debug("[relay][%s] buffering %s (buffer_size=%d)", cp.name, type(msg).__name__, len(cp.buffer))

    async def _relay_stdout(self, cp: CliProcess):
        """Read JSON lines from CLI stdout, relay or buffer."""
        assert cp.proc.stdout is not None
        normal_eof = False
        try:
            while True:
                line = await cp.proc.stdout.readline()
                if not line:
                    normal_eof = True
                    break  # EOF
                raw = line.decode().strip()
                log.debug("[stdout][%s] raw line (%d bytes): %.200s", cp.name, len(line), raw)
                try:
                    data: Any = json.loads(raw)
                except json.JSONDecodeError:
                    log.debug("[stdout][%s] json.loads FAILED on: %.200s", cp.name, raw)
                    # Non-JSON output — treat as stderr
                    if raw:
                        await self._relay_or_buffer(cp, StderrMsg(name=cp.name, text=raw))
                    continue

                log.debug("[stdout][%s] parsed msg", cp.name)
                cp.last_stdout_at = asyncio.get_running_loop().time()
                stdout_data = cast("dict[str, Any]", data) if isinstance(data, dict) else {"raw": data}
                await self._relay_or_buffer(cp, StdoutMsg(name=cp.name, data=stdout_data))
        except Exception:
            log.exception("Error relaying stdout for '%s'", cp.name)
        finally:
            if normal_eof:
                # Process closed stdout normally — wait for exit (should be immediate)
                try:
                    await asyncio.wait_for(cp.proc.wait(), timeout=10.0)
                except (TimeoutError, Exception):
                    pass
                cp.status = "exited"
                cp.exit_code = cp.proc.returncode
                await self._relay_or_buffer(cp, ExitMsg(name=cp.name, code=cp.exit_code))
                log.info("CLI '%s' exited (code=%s)", cp.name, cp.exit_code)
            else:
                # Relay crashed (e.g. LimitOverrunError) — process may still be alive.
                # Do NOT await proc.wait() as it could block forever.
                # Log the error state but leave the process running so it can be
                # re-subscribed or killed explicitly.
                log.error(
                    "stdout relay for '%s' failed — process still running (pid=%s), "
                    "relay is dead. New queries will not receive responses until "
                    "the agent is killed and respawned.",
                    cp.name,
                    cp.proc.pid,
                )

    async def _relay_stderr(self, cp: CliProcess):
        """Read lines from CLI stderr, relay or buffer."""
        assert cp.proc.stderr is not None
        try:
            while True:
                line = await cp.proc.stderr.readline()
                if not line:
                    break  # EOF
                text = line.decode().strip()
                if not text:
                    continue
                await self._relay_or_buffer(cp, StderrMsg(name=cp.name, text=text))
        except Exception:
            log.exception("Error relaying stderr for '%s'", cp.name)

    # -- Helpers --

    async def _kill_cli(self, cp: CliProcess):
        """Terminate a CLI process."""
        if cp.status != "running":
            return
        try:
            cp.proc.terminate()
            try:
                await asyncio.wait_for(cp.proc.wait(), timeout=5.0)
            except TimeoutError:
                cp.proc.kill()
                await cp.proc.wait()
        except ProcessLookupError:
            pass  # Already dead
        except Exception:
            log.exception("Error killing CLI '%s'", cp.name)
        cp.status = "exited"
        cp.exit_code = cp.proc.returncode
        if cp.stdout_task:
            cp.stdout_task.cancel()
        if cp.stderr_task:
            cp.stderr_task.cancel()

    async def _send_to_client(self, msg: ResultMsg | StdoutMsg | StderrMsg | ExitMsg) -> bool:
        """Send a message to the connected bot.py client. Returns False if no client."""
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
            # Client disconnected mid-write — mark as gone
            async with self._client_lock:
                if self._client_writer is writer:
                    self._client_writer = None
                    for cp in self._cli_procs.values():
                        cp.subscribed = False
            return False

    async def _send_result(self, result: ResultMsg):
        """Send a command result to the client."""
        await self._send_to_client(result)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <socket_path>", file=sys.stderr)
        sys.exit(1)

    socket_path = sys.argv[1]

    # Set up logging for the bridge process
    log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s [bridge] %(message)s",
        stream=sys.stderr,
    )

    server = BridgeServer(socket_path)
    asyncio.run(server.start())
