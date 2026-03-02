"""Direct subprocess ProcessConnection for claudewire.

Implements ProcessConnection using a local PTY subprocess.
This is the simplest backend — spawns the Claude CLI directly as a
child process with no intermediary (no procmux, no bridge).

Usage:
    from claudewire.direct import DirectProcessConnection, find_claude
    from claudewire import BridgeTransport

    conn = DirectProcessConnection()
    transport = BridgeTransport("main", conn)
    await transport.connect()
    await transport.spawn(cli_args, env, cwd)
    await transport.write(json.dumps(msg))
    async for event in transport.read_messages():
        ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tty
from asyncio.subprocess import PIPE
from typing import Any

from claudewire.types import CommandResult, ExitEvent, StderrEvent, StdoutEvent

log = logging.getLogger(__name__)


def find_claude() -> str:
    """Find the claude CLI binary on PATH.

    Returns the path to the claude binary.
    Raises FileNotFoundError if not found.
    """
    path = shutil.which("claude")
    if path:
        return path
    raise FileNotFoundError(
        "Could not find 'claude' CLI on PATH. "
        "Install it or pass --claude-path explicitly."
    )


class _LocalProcess:
    """Manages a single PTY subprocess and feeds events into a queue."""

    def __init__(
        self,
        name: str,
        queue: asyncio.Queue[StdoutEvent | StderrEvent | ExitEvent | None],
    ) -> None:
        self._name = name
        self._queue = queue
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pty_reader: asyncio.StreamReader | None = None
        self._pty_transport: asyncio.BaseTransport | None = None

    async def start(self, cmd: list[str], env: dict[str, str], cwd: str) -> None:
        """Spawn the subprocess with a PTY for stdout."""
        master_fd, slave_fd = os.openpty()
        tty.setraw(master_fd)

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=PIPE,
            stdout=slave_fd,
            stderr=PIPE,
            env=env,
            cwd=cwd or None,
        )
        os.close(slave_fd)

        loop = asyncio.get_running_loop()
        self._pty_reader = asyncio.StreamReader()
        self._pty_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self._pty_reader),
            os.fdopen(master_fd, "rb", 0),
        )

        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def write_stdin(self, data: dict[str, Any]) -> None:
        """Write a JSON message to the subprocess stdin."""
        assert self._proc
        assert self._proc.stdin
        self._proc.stdin.write((json.dumps(data) + "\n").encode())
        await self._proc.stdin.drain()

    async def stop(self) -> None:
        """Terminate the subprocess and clean up."""
        if self._pty_transport:
            self._pty_transport.close()
            self._pty_transport = None
            self._pty_reader = None

        if self._proc:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except (ProcessLookupError, TimeoutError):
                if self._proc.returncode is None:
                    self._proc.kill()
            finally:
                self._proc = None

        for task in (self._stdout_task, self._stderr_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _read_stdout(self) -> None:
        """Read JSON lines from PTY stdout and push StdoutEvent to queue."""
        assert self._pty_reader
        try:
            while True:
                line_bytes = await self._pty_reader.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode().strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await self._queue.put(StdoutEvent(name=self._name, data=data))
        except Exception:
            log.debug("[%s] stdout reader stopped", self._name)
        finally:
            # Push exit event when stdout closes
            code = self._proc.returncode if self._proc else None
            await self._queue.put(ExitEvent(name=self._name, code=code))

    async def _read_stderr(self) -> None:
        """Read stderr lines and push StderrEvent to queue."""
        assert self._proc
        assert self._proc.stderr
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                await self._queue.put(
                    StderrEvent(name=self._name, text=line.decode())
                )
        except Exception:
            log.debug("[%s] stderr reader stopped", self._name)


class DirectProcessConnection:
    """ProcessConnection that manages local PTY subprocesses.

    Each named process gets its own subprocess with PTY stdout.
    Implements the ProcessConnection protocol from claudewire.types.
    """

    def __init__(self) -> None:
        self._processes: dict[str, _LocalProcess] = {}
        self._queues: dict[str, asyncio.Queue] = {}

    @property
    def is_alive(self) -> bool:
        return True

    def register(self, name: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[name] = queue
        return queue

    def unregister(self, name: str) -> None:
        self._queues.pop(name, None)
        self._processes.pop(name, None)

    async def spawn(
        self,
        name: str,
        *,
        cli_args: list[str],
        env: dict[str, str],
        cwd: str,
    ) -> CommandResult:
        queue = self._queues.get(name)
        if queue is None:
            return CommandResult(ok=False, error=f"No queue registered for '{name}'")

        proc = _LocalProcess(name, queue)
        try:
            await proc.start(cli_args, env, cwd)
        except Exception as e:
            return CommandResult(ok=False, error=str(e))

        self._processes[name] = proc
        return CommandResult(ok=True)

    async def subscribe(self, name: str) -> CommandResult:
        return CommandResult(ok=True)

    async def kill(self, name: str) -> CommandResult:
        proc = self._processes.get(name)
        if proc:
            await proc.stop()
        return CommandResult(ok=True)

    async def send_stdin(self, name: str, data: dict[str, Any]) -> None:
        proc = self._processes.get(name)
        if proc:
            await proc.write_stdin(data)
