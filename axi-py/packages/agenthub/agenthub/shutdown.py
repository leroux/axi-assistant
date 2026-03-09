"""Graceful shutdown coordinator for agent sessions.

All side effects (sleeping agents, closing the app, killing the process)
are injected as callbacks so the core logic can be tested with pure mocks.

Design:
  - graceful_shutdown(): waits for busy agents to finish, then sleeps
    everyone and exits. 5-minute hard timeout.
  - force_shutdown(): skips the wait and exits immediately.
  - A safety-deadline thread guarantees os._exit(42) fires even if
    close or sleep hangs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from opentelemetry import trace

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

RESTART_EXIT_CODE = 42
STATUS_INTERVAL = 30  # seconds between "still waiting" messages
POLL_INTERVAL = 5  # seconds between busy-agent polls
SHUTDOWN_DEADLINE = 30  # safety deadline for the exit phase
CLOSE_APP_TIMEOUT = 10  # max seconds to wait for close_app_fn


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class Sleepable(Protocol):
    """Minimal interface the coordinator needs from an agent session."""

    @property
    def name(self) -> str: ...

    @property
    def query_lock(self) -> asyncio.Lock: ...

    @property
    def client(self) -> Any: ...


# Callback types
SleepFn = Callable[[Any], Awaitable[None]]
CloseAppFn = Callable[[], Awaitable[None]]
NotifyFn = Callable[[str, str], Awaitable[None]]
GoodbyeFn = Callable[[], Awaitable[None]]


# ---------------------------------------------------------------------------
# Safety deadline — guarantees the process exits
# ---------------------------------------------------------------------------


def _start_deadline_thread(timeout: float) -> threading.Thread:
    """Spawn a daemon thread that calls os._exit(42) after *timeout* seconds."""

    def _deadline():
        time.sleep(timeout)
        log.warning(
            "Shutdown safety deadline reached (%ds) — forcing os._exit(%d)",
            timeout,
            RESTART_EXIT_CODE,
        )
        os._exit(RESTART_EXIT_CODE)

    t = threading.Thread(target=_deadline, daemon=True, name="shutdown-deadline")
    t.start()
    return t


def kill_supervisor() -> None:
    """Send SIGTERM to the parent supervisor process, then os._exit(42)."""
    ppid = os.getppid()
    log.info("Sending SIGTERM to supervisor (pid=%d)", ppid)
    try:
        os.kill(ppid, signal.SIGTERM)
    except OSError:
        log.warning("Failed to kill supervisor (pid=%d)", ppid)
    time.sleep(0.5)
    os._exit(RESTART_EXIT_CODE)


def exit_for_restart() -> None:
    """Exit with code 42 without killing the supervisor.

    Used for bridge-mode graceful restarts: the bridge process and its CLI
    subprocesses keep running, the app exits, supervisor relaunches it,
    and it reconnects to the bridge.
    """
    log.info("Exiting for restart (bridge mode) — bridge stays alive")
    os._exit(RESTART_EXIT_CODE)


# ---------------------------------------------------------------------------
# ShutdownCoordinator
# ---------------------------------------------------------------------------


class ShutdownCoordinator:
    """Coordinates graceful (and forced) shutdown of agent sessions.

    All side effects are injected — the coordinator has no opinion about
    what "close the app" or "kill the process" means.
    """

    def __init__(
        self,
        agents: Mapping[str, Sleepable],
        sleep_fn: SleepFn,
        close_app_fn: CloseAppFn,
        kill_fn: Callable[[], None] = kill_supervisor,
        notify_fn: NotifyFn | None = None,
        goodbye_fn: GoodbyeFn | None = None,
        deadline_timeout: float = SHUTDOWN_DEADLINE,
        close_app_timeout: float = CLOSE_APP_TIMEOUT,
        bridge_mode: bool = False,
    ):
        self._agents = agents
        self._sleep_fn = sleep_fn
        self._close_app_fn = close_app_fn
        self._kill_fn = kill_fn
        self._notify_fn = notify_fn
        self._goodbye_fn = goodbye_fn
        self._deadline_timeout = deadline_timeout
        self._close_app_timeout = close_app_timeout
        self._bridge_mode = bridge_mode
        self._requested = False

    @property
    def requested(self) -> bool:
        return self._requested

    def get_busy_agents(self, skip: str | None = None) -> dict[str, Sleepable]:
        """Return agents whose query_lock is held, excluding *skip*."""
        return {
            name: s
            for name, s in self._agents.items()
            if s.query_lock.locked() and name != skip
        }

    async def sleep_all(self, skip: str | None = None) -> None:
        """Sleep every awake agent, optionally skipping *skip*."""
        for name, session in list(self._agents.items()):
            if name == skip:
                continue
            if session.client is not None:
                try:
                    await self._sleep_fn(session)
                except Exception:
                    log.exception(
                        "Error sleeping agent '%s' during shutdown", name
                    )

    async def _notify(self, agent_name: str, message: str) -> None:
        if self._notify_fn is not None:
            try:
                await self._notify_fn(agent_name, message)
            except Exception:
                log.exception("Failed to notify agent '%s'", agent_name)

    async def _execute_exit(self, skip_agent: str | None = None) -> None:
        """Sleep agents, close app, kill process.

        Safety-deadline thread ensures os._exit fires even if anything hangs.
        In bridge mode, agents are NOT slept — they keep running.

        Note: spans are fire-and-forget here because os._exit() prevents normal
        cleanup. The close_app_fn should flush pending spans before exit.
        """
        _tracer.start_span(
            "shutdown.execute_exit",
            attributes={
                "shutdown.bridge_mode": self._bridge_mode,
                "shutdown.skip_agent": skip_agent or "",
            },
        ).end()
        _start_deadline_thread(self._deadline_timeout)

        if self._goodbye_fn is not None:
            try:
                await self._goodbye_fn()
            except Exception:
                log.exception("Failed to send goodbye message")

        if not self._bridge_mode:
            await self.sleep_all(skip=skip_agent)

        try:
            await asyncio.wait_for(
                self._close_app_fn(), timeout=self._close_app_timeout
            )
        except TimeoutError:
            log.warning(
                "close_app timed out after %ds — proceeding to kill",
                self._close_app_timeout,
            )
        except Exception:
            log.exception("close_app raised — proceeding to kill")

        self._kill_fn()

    async def graceful_shutdown(
        self, source: str, skip_agent: str | None = None
    ) -> None:
        """Wait for busy agents to finish, then exit.

        5-minute hard timeout. In bridge mode, busy agents are not waited for.
        """
        if self._requested:
            log.info(
                "Graceful shutdown already in progress (ignoring duplicate from %s)",
                source,
            )
            return
        self._requested = True
        busy = self.get_busy_agents(skip=skip_agent)
        _tracer.start_span(
            "shutdown.graceful",
            attributes={
                "shutdown.source": source,
                "shutdown.bridge_mode": self._bridge_mode,
                "shutdown.busy_agents": list(busy.keys()),
                "shutdown.skip_agent": skip_agent or "",
            },
        ).end()
        log.info("Graceful shutdown initiated from %s", source)

        if self._bridge_mode:
            log.info("Bridge mode — skipping agent wait, agents keep running")
            await self._execute_exit(skip_agent=skip_agent)
            return

        if not busy:
            log.info("No agents busy — exiting immediately")
            await self._execute_exit(skip_agent=skip_agent)
            return

        for name in busy:
            await self._notify(
                name,
                f"Restart pending — waiting for **{name}** to finish current task...",
            )

        start = time.monotonic()
        last_status = 0.0
        shutdown_timeout = 300

        while True:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed = time.monotonic() - start

            still_busy = self.get_busy_agents(skip=skip_agent)
            if not still_busy:
                log.info("All agents finished after %ds — exiting", int(elapsed))
                break

            if elapsed > shutdown_timeout:
                log.warning(
                    "Shutdown timeout after %ds — agents still busy: %s",
                    int(elapsed),
                    list(still_busy.keys()),
                )
                break

            if elapsed - last_status >= STATUS_INTERVAL:
                last_status = elapsed
                for name in still_busy:
                    await self._notify(
                        name,
                        f"Still waiting for **{name}** to finish... ({int(elapsed)}s)",
                    )

        await self._execute_exit(skip_agent=skip_agent)

    async def force_shutdown(self, source: str = "force") -> None:
        """Skip the wait and exit immediately."""
        if self._requested:
            log.info(
                "Shutdown already in progress — escalating to force (from %s)", source
            )
        self._requested = True
        busy = self.get_busy_agents()
        _tracer.start_span(
            "shutdown.force",
            attributes={
                "shutdown.source": source,
                "shutdown.busy_agents": list(busy.keys()),
            },
        ).end()
        log.info("Force shutdown initiated from %s", source)
        await self._execute_exit(skip_agent=None)
