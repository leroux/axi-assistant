"""FlowcoderProcess — manages a flowcoder-engine subprocess via JSON-lines on stdin/stdout."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator

log = logging.getLogger(__name__)


class FlowcoderProcess:
    """Wraps a ``flowcoder-engine`` subprocess.

    Communication uses newline-delimited JSON on stdin (inbound) and stdout
    (outbound).  Stderr is forwarded to the logger.
    """

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
        self._stderr_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the flowcoder-engine subprocess."""
        import shutil

        # Prefer the installed flowcoder-engine from PATH (pip-installed),
        # fall back to a standalone install at FLOWCODER_HOME
        engine_bin = shutil.which("flowcoder-engine")
        if not engine_bin:
            flowcoder_home = os.environ.get(
                "FLOWCODER_HOME",
                os.path.expanduser("~/flowcoder-rewrite"),
            )
            engine_bin = os.path.join(
                flowcoder_home,
                "packages", "flowcoder-engine", ".venv", "bin", "flowcoder-engine",
            )

        # Default search path for built-in commands
        flowcoder_home = os.environ.get(
            "FLOWCODER_HOME",
            os.path.expanduser("~/flowcoder-rewrite"),
        )
        default_search = os.path.join(flowcoder_home, "examples", "commands")

        cmd: list[str] = [engine_bin, "--command", self.command]
        if self.args:
            cmd += ["--args", self.args]
        for sp in [default_search] + self.search_paths:
            cmd += ["--search-path", sp]

        # Strip env vars that confuse nested Claude detection
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT")
        }

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
        assert self._proc and self._proc.stderr
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

    async def messages(self) -> AsyncIterator[dict]:
        """Yield parsed JSON messages from stdout, one per line."""
        assert self._proc and self._proc.stdout
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

    async def send(self, msg: dict) -> None:
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
            except asyncio.TimeoutError:
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
