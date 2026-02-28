"""FlowcoderProcess — manages a flowcoder-engine subprocess via JSON-lines on stdin/stdout.

Provides two implementations:
- FlowcoderProcess: direct subprocess (default)
- BridgeFlowcoderProcess: backed by the agent bridge for persistence across bot restarts
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from axi.bridge.client import BridgeConnection

log = logging.getLogger(__name__)

from axi.bridge.protocol import ExitMsg, StderrMsg, StdoutMsg

# ------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------


def build_engine_cmd(
    command: str,
    args: str = "",
    search_paths: list[str] | None = None,
) -> list[str]:
    """Build flowcoder-engine argv (binary resolution + flags)."""

    flowcoder_home = os.environ.get(
        "FLOWCODER_HOME",
        os.path.expanduser("~/flowcoder-rewrite"),
    )

    engine_bin = shutil.which("flowcoder-engine")
    if not engine_bin:
        engine_bin = os.path.join(
            flowcoder_home,
            "packages",
            "flowcoder-engine",
            ".venv",
            "bin",
            "flowcoder-engine",
        )

    default_search = os.path.join(flowcoder_home, "examples", "commands")

    cmd: list[str] = [engine_bin, "--command", command]
    if args:
        cmd += ["--args", args]
    for sp in [default_search] + (search_paths or []):
        cmd += ["--search-path", sp]
    return cmd


def build_engine_env() -> dict[str, str]:
    """Build clean env (strip CLAUDECODE/SDK vars)."""
    return {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT")
    }


# ------------------------------------------------------------------
# Direct subprocess implementation
# ------------------------------------------------------------------


class FlowcoderProcess:
    """Wraps a ``flowcoder-engine`` subprocess.

    Communication uses newline-delimited JSON on stdin (inbound) and stdout
    (outbound).  Stderr is forwarded to the logger.
    """

    is_bridge_backed: bool = False

    def __init__(
        self,
        command: str,
        args: str = "",
        search_paths: list[str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.command = command
        self.args = args
        self.search_paths = search_paths or []
        self.cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the flowcoder-engine subprocess."""
        cmd = build_engine_cmd(self.command, self.args, self.search_paths)
        env = build_engine_env()

        log.info("Starting flowcoder-engine: %s (cwd=%s)", " ".join(cmd), self.cwd)

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=env,
        )

        self._stderr_task = asyncio.create_task(self._forward_stderr())

    async def _forward_stderr(self) -> None:
        """Read stderr lines and forward to the logger."""
        assert self._proc
        assert self._proc.stderr
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log.debug("[flowcoder-engine stderr] %s", text)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed JSON messages from stdout, one per line."""
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("FlowcoderProcess not running — call start() first")
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                yield json.loads(text)
            except json.JSONDecodeError:
                log.warning("flowcoder: non-JSON stdout line: %s", text[:200])

    async def send(self, msg: dict[str, Any]) -> None:
        """Write a JSON-line message to the process's stdin."""
        if self._proc and self._proc.stdin and not self._proc.stdin.is_closing():
            data = json.dumps(msg, separators=(",", ":")) + "\n"
            self._proc.stdin.write(data.encode("utf-8"))
            await self._proc.stdin.drain()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Gracefully stop the subprocess (shutdown message, close stdin, terminate, kill)."""
        if self._proc is None:
            return

        # Send shutdown message so the engine exits its message loop cleanly
        try:
            await self.send({"type": "shutdown"})
        except Exception:
            pass

        # Close stdin to signal EOF
        if self._proc.stdin and not self._proc.stdin.is_closing():
            try:
                self._proc.stdin.close()
            except Exception:
                pass

        # Try graceful termination
        if self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except TimeoutError:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
                await self._proc.wait()

        # Cancel stderr forwarder
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

        log.info("flowcoder-engine stopped (returncode=%s)", self._proc.returncode)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True if the subprocess is still running."""
        return self._proc is not None and self._proc.returncode is None


# ------------------------------------------------------------------
# Bridge-backed implementation
# ------------------------------------------------------------------


class BridgeFlowcoderProcess:
    """Flowcoder engine backed by the agent bridge.

    Same interface as FlowcoderProcess but the subprocess lives in the bridge,
    surviving bot.py restarts. Uses bridge spawn/subscribe/kill commands.
    """

    is_bridge_backed: bool = True

    def __init__(
        self,
        bridge_name: str,
        conn: BridgeConnection,
        command: str,
        args: str = "",
        search_paths: list[str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.bridge_name = bridge_name
        self.command = command
        self.args = args
        self.search_paths = search_paths or []
        self.cwd = cwd
        self._conn = conn
        self._queue: asyncio.Queue[StdoutMsg | StderrMsg | ExitMsg | None] | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the engine in the bridge and subscribe to its output."""
        cmd = build_engine_cmd(self.command, self.args, self.search_paths)
        env = build_engine_env()

        log.info(
            "Starting bridge flowcoder '%s': %s (cwd=%s)",
            self.bridge_name,
            " ".join(cmd),
            self.cwd,
        )

        # Register queue before spawn so we don't miss early output
        self._queue = self._conn.register_agent(self.bridge_name)

        result = await self._conn.send_command(
            "spawn",
            name=self.bridge_name,
            cli_args=cmd,
            env=env,
            cwd=self.cwd,
        )
        if not result.ok and not result.already_running:
            self._conn.unregister_agent(self.bridge_name)
            self._queue = None
            raise RuntimeError(f"Bridge spawn failed for '{self.bridge_name}': {result.error}")

        sub_result = await self._conn.send_command("subscribe", name=self.bridge_name)
        if not sub_result.ok:
            log.warning("Subscribe warning for '%s': %s", self.bridge_name, sub_result.error)

        self._running = True

    async def subscribe(self) -> Any:
        """Reconnect — register + subscribe without spawning.

        Returns the subscribe ResultMsg (has .replayed, .status, .idle fields).
        """
        self._queue = self._conn.register_agent(self.bridge_name)
        result = await self._conn.send_command("subscribe", name=self.bridge_name)
        if not result.ok:
            self._conn.unregister_agent(self.bridge_name)
            self._queue = None
            raise RuntimeError(f"Bridge subscribe failed for '{self.bridge_name}': {result.error}")
        self._running = True
        return result

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Async iterator reading from bridge agent queue.

        Yields StdoutMsg.data dicts, logs StderrMsg, stops on ExitMsg/None.
        """
        if not self._queue:
            return

        while True:
            msg = await self._queue.get()
            if msg is None:
                # Connection lost sentinel
                self._running = False
                break
            if isinstance(msg, StdoutMsg):
                yield msg.data
            elif isinstance(msg, StderrMsg):
                log.debug("[flowcoder-engine bridge stderr] %s", msg.text)
            else:  # ExitMsg
                self._running = False
                log.info(
                    "Bridge flowcoder '%s' exited (code=%s)",
                    self.bridge_name,
                    msg.code,
                )
                break

    async def send(self, msg: dict[str, Any]) -> None:
        """Send a JSON message to the engine's stdin via the bridge."""
        await self._conn.send_stdin(self.bridge_name, msg)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Send shutdown message, then kill via bridge, then unregister."""
        try:
            await self.send({"type": "shutdown"})
        except Exception:
            pass
        try:
            await self._conn.send_command("kill", name=self.bridge_name)
        except Exception:
            pass
        self._conn.unregister_agent(self.bridge_name)
        self._queue = None
        self._running = False
        log.info("Bridge flowcoder '%s' stopped", self.bridge_name)

    async def detach(self) -> None:
        """Unsubscribe and unregister without killing — bridge buffers output.

        Used during sleep/shutdown when the engine is mid-execution.
        """
        try:
            await self._conn.send_command("unsubscribe", name=self.bridge_name)
        except Exception:
            pass
        self._conn.unregister_agent(self.bridge_name)
        self._queue = None
        self._running = False
        log.info("Bridge flowcoder '%s' detached (bridge buffering)", self.bridge_name)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True if the engine is believed to be running in the bridge."""
        return self._running
