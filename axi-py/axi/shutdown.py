"""Graceful shutdown coordinator for Axi bot.

Extracted from bot.py for testability. All side effects (sleeping agents,
closing the Discord bot, killing the supervisor) are injected as callbacks
so the core logic can be tested with pure mocks.

Design:
  - graceful_shutdown(): waits *indefinitely* for busy agents to finish,
    then sleeps everyone and exits. No hard timeout — the user can trigger
    force_shutdown() if they need to bail out.
  - force_shutdown(): skips the wait and exits immediately.
  - A safety-deadline thread guarantees os._exit(42) fires even if
    bot.close() or sleep hangs.
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
SHUTDOWN_DEADLINE = 30  # safety deadline for the exit phase (after agents finish)
BOT_CLOSE_TIMEOUT = 10  # max seconds to wait for bot.close()


# ---------------------------------------------------------------------------
# Protocols — the coordinator only depends on these interfaces
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
SleepFn = Callable[[Any], Awaitable[None]]  # sleep one agent
CloseBotFn = Callable[[], Awaitable[None]]  # close discord bot
NotifyFn = Callable[[str, str], Awaitable[None]]  # (agent_name, message) → send status
GoodbyeFn = Callable[[], Awaitable[None]]  # send goodbye message to master channel


# ---------------------------------------------------------------------------
# Safety deadline — guarantees the process exits
# ---------------------------------------------------------------------------


def _start_deadline_thread(timeout: float) -> threading.Thread:
    """Spawn a daemon thread that calls os._exit(42) after *timeout* seconds.

    This is the last-resort safety net: if bot.close() or sleep_agent() hangs,
    the process will still exit and the supervisor will restart it.
    """

    def _deadline():
        time.sleep(timeout)
        log.warning("Shutdown safety deadline reached (%ds) — forcing os._exit(%d)", timeout, RESTART_EXIT_CODE)
        os._exit(RESTART_EXIT_CODE)

    t = threading.Thread(target=_deadline, daemon=True, name="shutdown-deadline")
    t.start()
    return t


def kill_supervisor() -> None:
    """Send SIGTERM to the parent supervisor process, then os._exit(42).

    This is intentionally synchronous and runs as the very last step.
    The supervisor (or systemd) will relaunch us.
    """
    ppid = os.getppid()
    log.info("Sending SIGTERM to supervisor (pid=%d)", ppid)
    try:
        os.kill(ppid, signal.SIGTERM)
    except OSError:
        log.warning("Failed to kill supervisor (pid=%d)", ppid)
    # Brief pause so the SIGTERM is delivered before we exit.
    # This blocks the event loop, but it's the very last thing before os._exit.
    time.sleep(0.5)
    os._exit(RESTART_EXIT_CODE)


def exit_for_restart() -> None:
    """Exit with code 42 without killing the supervisor.

    Used for bridge-mode graceful restarts: the bridge process and its CLI
    subprocesses keep running, bot.py exits, supervisor relaunches bot.py,
    and it reconnects to the bridge.
    """
    log.info("Exiting for restart (bridge mode) — bridge stays alive")
    os._exit(RESTART_EXIT_CODE)


# ---------------------------------------------------------------------------
# ShutdownCoordinator
# ---------------------------------------------------------------------------


class ShutdownCoordinator:
    """Coordinates graceful (and forced) shutdown of agent sessions.

    Usage in bot.py::

        coordinator = ShutdownCoordinator(
            agents=agents,                          # dict[str, AgentSession]
            sleep_fn=sleep_agent,                   # async (session) → None
            close_bot_fn=bot.close,                 # async () → None
            kill_fn=kill_supervisor,                 # () → NoReturn
            notify_fn=_notify_agent_channel,        # async (name, msg) → None
        )

    Then replace the old ``_graceful_shutdown`` call with::

        asyncio.create_task(coordinator.graceful_shutdown("MCP tool", skip_agent="axi-master"))

    And ``_shutdown_requested`` checks with ``coordinator.requested``.
    """

    def __init__(
        self,
        agents: Mapping[str, Sleepable],
        sleep_fn: SleepFn,
        close_bot_fn: CloseBotFn,
        kill_fn: Callable[[], None] = kill_supervisor,
        notify_fn: NotifyFn | None = None,
        goodbye_fn: GoodbyeFn | None = None,
        deadline_timeout: float = SHUTDOWN_DEADLINE,
        bot_close_timeout: float = BOT_CLOSE_TIMEOUT,
        bridge_mode: bool = False,
    ):
        self._agents = agents
        self._sleep_fn = sleep_fn
        self._close_bot_fn = close_bot_fn
        self._kill_fn = kill_fn
        self._notify_fn = notify_fn
        self._goodbye_fn = goodbye_fn
        self._deadline_timeout = deadline_timeout
        self._bot_close_timeout = bot_close_timeout
        self._bridge_mode = bridge_mode
        self._requested = False

    # -- Public state -------------------------------------------------------

    @property
    def requested(self) -> bool:
        return self._requested

    # -- Busy-agent helpers -------------------------------------------------

    def get_busy_agents(self, skip: str | None = None) -> dict[str, Sleepable]:
        """Return agents whose query_lock is held, excluding *skip*."""
        return {name: s for name, s in self._agents.items() if s.query_lock.locked() and name != skip}

    # -- Sleep all (with skip) ----------------------------------------------

    async def sleep_all(self, skip: str | None = None) -> None:
        """Sleep every awake agent, optionally skipping *skip*.

        Exceptions from individual agents are logged and swallowed so one
        broken agent doesn't prevent the rest from being cleaned up.
        """
        for name, session in list(self._agents.items()):
            if name == skip:
                continue
            if session.client is not None:
                try:
                    await self._sleep_fn(session)
                except Exception:
                    log.exception("Error sleeping agent '%s' during shutdown", name)

    # -- Notify helper ------------------------------------------------------

    async def _notify(self, agent_name: str, message: str) -> None:
        if self._notify_fn is not None:
            try:
                await self._notify_fn(agent_name, message)
            except Exception:
                log.exception("Failed to notify agent '%s'", agent_name)

    # -- Exit phase (sleep → close → kill) ----------------------------------

    async def _execute_exit(self, skip_agent: str | None = None) -> None:
        """Sleep agents, close the bot, and kill the supervisor.

        A safety-deadline thread is started first so that os._exit(42) fires
        even if any step below hangs.

        In bridge mode, agents are NOT slept — they keep running in the bridge
        process and will be reconnected after restart.

        Note: spans are fire-and-forget here because os._exit() prevents normal
        cleanup. shutdown_tracing() is called inside _close_bot_fn to flush
        pending spans before exit.
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
            await asyncio.wait_for(self._close_bot_fn(), timeout=self._bot_close_timeout)
        except TimeoutError:
            log.warning("bot.close() timed out after %ds — proceeding to kill", self._bot_close_timeout)
        except Exception:
            log.exception("bot.close() raised — proceeding to kill")

        self._kill_fn()

    # -- Public API ---------------------------------------------------------

    async def graceful_shutdown(self, source: str, skip_agent: str | None = None) -> None:
        """Wait for all busy agents to finish, then exit with code 42.

        *skip_agent* is excluded from the busy-wait and from the sleep phase
        (used when an agent triggers its own restart to avoid deadlocking on
        itself).

        There is a 5-minute hard timeout on the wait. If agents are still
        busy after that, shutdown proceeds anyway. The user can also call
        force_shutdown() to bail out sooner.

        In bridge mode, busy agents are not waited for — they keep running
        in the bridge process and will be reconnected after restart.
        """
        if self._requested:
            log.info("Graceful shutdown already in progress (ignoring duplicate from %s)", source)
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

        # In bridge mode, agents keep running — no need to wait or sleep
        if self._bridge_mode:
            log.info("Bridge mode — skipping agent wait, agents keep running")
            await self._execute_exit(skip_agent=skip_agent)
            return  # unreachable (os._exit) but makes intent clear

        if not busy:
            log.info("No agents busy — exiting immediately")
            await self._execute_exit(skip_agent=skip_agent)
            return  # unreachable (os._exit) but makes intent clear

        # Notify each busy agent's channel
        for name in busy:
            await self._notify(name, f"Restart pending — waiting for **{name}** to finish current task...")

        # Wait indefinitely for agents to finish
        start = time.monotonic()
        last_status = 0.0

        shutdown_timeout = 300  # 5 minutes

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

            # Periodic status updates
            if elapsed - last_status >= STATUS_INTERVAL:
                last_status = elapsed
                for name in still_busy:
                    await self._notify(name, f"Still waiting for **{name}** to finish... ({int(elapsed)}s)")

        await self._execute_exit(skip_agent=skip_agent)

    async def force_shutdown(self, source: str = "force") -> None:
        """Skip the wait and exit immediately."""
        if self._requested:
            log.info("Shutdown already in progress — escalating to force (from %s)", source)
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
