"""Agent scheduler — slot management and priority-based eviction.

Manages which agents hold awake slots. When slots are full:
1. Evicts the longest-idle non-busy agent (background before interactive).
2. If all agents are busy, queues the request and marks an agent for
   deferred eviction (sleep after its current turn completes).

Priority classes:
- Protected (master): never evicted
- Interactive: user recently sent a message — higher eviction resistance
- Background: everything else — evicted first

Same algorithm as the original axi/scheduler.py. Converted from module-level
state to a class so AgentHub can own its scheduler instance.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agenthub.types import AgentSession

log = logging.getLogger(__name__)


class Scheduler:
    """Slot management and priority-based eviction."""

    def __init__(
        self,
        *,
        max_slots: int,
        protected: set[str],
        get_sessions: Callable[[], dict[str, AgentSession]],
        sleep_fn: Callable[[AgentSession], Awaitable[None]],
    ) -> None:
        self._max_slots = max_slots
        self._protected = set(protected)
        self._get_sessions = get_sessions
        self._sleep_fn = sleep_fn
        self._lock = asyncio.Lock()
        self._slots: set[str] = set()
        self._waiters: deque[tuple[str, asyncio.Event]] = deque()
        self._yield_set: set[str] = set()
        self._interactive: set[str] = set()
        log.info(
            "Scheduler initialized: max_slots=%d, protected=%s",
            max_slots,
            protected,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def request_slot(self, agent_name: str, *, timeout: float = 120.0) -> None:
        """Acquire an awake slot. Blocks until one is available.

        1. Already has slot -> return.
        2. Slot available -> grant.
        3. Idle agents exist -> evict (background first), grant.
        4. All busy -> queue, mark a yield target, block until slot freed.

        Raises ConcurrencyLimitError on timeout.
        """
        if agent_name in self._slots:
            return

        async with self._lock:
            if agent_name in self._slots:
                return

            # Fast path: slot available
            if len(self._slots) < self._max_slots:
                self._slots.add(agent_name)
                log.debug(
                    "Slot granted to '%s' (%d/%d)",
                    agent_name,
                    len(self._slots),
                    self._max_slots,
                )
                return

            # Try evicting idle agents
            while len(self._slots) >= self._max_slots:
                evicted = await self._evict_idle(exclude=agent_name)
                if not evicted:
                    break

            if len(self._slots) < self._max_slots:
                self._slots.add(agent_name)
                log.debug(
                    "Slot granted to '%s' after eviction (%d/%d)",
                    agent_name,
                    len(self._slots),
                    self._max_slots,
                )
                return

            # All agents busy — queue and mark an eviction target
            event = asyncio.Event()
            self._waiters.append((agent_name, event))
            self._select_yield_target(exclude=agent_name)
            log.info(
                "All %d slots busy, '%s' queued (position %d), yield targets: %s",
                self._max_slots,
                agent_name,
                len(self._waiters),
                self._yield_set,
            )

        # Wait outside lock for a slot to be freed
        try:
            async with asyncio.timeout(timeout):
                await event.wait()
        except TimeoutError:
            if agent_name in self._slots:
                return
            for i, (name, _) in enumerate(self._waiters):
                if name == agent_name:
                    del self._waiters[i]
                    break
            from agenthub.types import ConcurrencyLimitError

            raise ConcurrencyLimitError(
                f"Cannot wake agent '{agent_name}': all {self._max_slots} slots busy "
                f"after {timeout:.0f}s wait. Message will be queued and processed "
                f"when a slot opens."
            ) from None

        log.info(
            "Slot granted to '%s' from wait queue (%d/%d)",
            agent_name,
            len(self._slots),
            self._max_slots,
        )

    def release_slot(self, agent_name: str) -> None:
        """Release a slot. Called when an agent sleeps.

        Sync — no lock needed (single-threaded asyncio event loop).
        Safe to call from inside _lock (eviction path).
        """
        was_held = agent_name in self._slots
        self._slots.discard(agent_name)
        self._yield_set.discard(agent_name)
        self._interactive.discard(agent_name)

        if not was_held:
            return

        log.debug(
            "Slot released by '%s' (%d/%d)",
            agent_name,
            len(self._slots),
            self._max_slots,
        )

        # Grant slot to next waiter
        while self._waiters:
            waiter_name, event = self._waiters[0]
            if event.is_set():
                self._waiters.popleft()
                continue
            self._waiters.popleft()
            self._slots.add(waiter_name)
            event.set()
            log.info(
                "Slot granted to waiter '%s' (freed by '%s')",
                waiter_name,
                agent_name,
            )
            break

    def should_yield(self, agent_name: str) -> bool:
        """Check if this agent should sleep after finishing its current turn."""
        return agent_name in self._yield_set

    def restore_slot(self, agent_name: str) -> None:
        """Register an agent that's already awake (e.g. reconnected from bridge).

        Unlike request_slot, this doesn't evict or queue — just records
        that the agent holds a slot. Used during startup/reconnect.
        """
        self._slots.add(agent_name)
        log.debug(
            "Slot restored for '%s' (%d/%d)",
            agent_name,
            len(self._slots),
            self._max_slots,
        )

    def mark_interactive(self, agent_name: str) -> None:
        """Mark agent as interactive (user sent a message). Higher eviction resistance."""
        self._interactive.add(agent_name)

    def mark_background(self, agent_name: str) -> None:
        """Mark agent as background. Lower eviction resistance."""
        self._interactive.discard(agent_name)

    def has_waiters(self) -> bool:
        """True if agents are queued waiting for slots."""
        return bool(self._waiters)

    def slot_count(self) -> int:
        """Current number of occupied slots."""
        return len(self._slots)

    def status(self) -> dict[str, Any]:
        """Return scheduler state for diagnostics."""
        return {
            "max_slots": self._max_slots,
            "slots": sorted(self._slots),
            "slot_count": len(self._slots),
            "waiters": [name for name, _ in self._waiters],
            "yield_targets": sorted(self._yield_set),
            "interactive": sorted(self._interactive),
            "protected": sorted(self._protected),
        }

    # ------------------------------------------------------------------
    # Internal: eviction
    # ------------------------------------------------------------------

    async def _evict_idle(self, exclude: str) -> bool:
        """Evict the longest-idle, non-busy, non-protected agent.

        Prefers background agents over interactive ones.
        Returns True if an agent was evicted.
        """
        sessions = self._get_sessions()

        background: list[tuple[float, str, AgentSession]] = []
        interactive: list[tuple[float, str, AgentSession]] = []

        for name, session in sessions.items():
            if name == exclude or name in self._protected:
                continue
            if session.client is None:
                continue
            if session.query_lock.locked():
                continue
            if session.bridge_busy:
                continue
            idle_secs = (datetime.now(UTC) - session.last_activity).total_seconds()
            bucket = interactive if name in self._interactive else background
            bucket.append((idle_secs, name, session))

        # Try background first, then interactive
        for bucket_name, candidates in [
            ("background", background),
            ("interactive", interactive),
        ]:
            if not candidates:
                continue
            candidates.sort(reverse=True, key=lambda x: x[0])
            idle_secs, evict_name, evict_session = candidates[0]
            log.info(
                "Evicting idle %s agent '%s' (idle %.0fs) to free slot",
                bucket_name,
                evict_name,
                idle_secs,
            )
            try:
                await self._sleep_fn(evict_session)
                return True
            except Exception:
                log.exception("Failed to evict agent '%s'", evict_name)
                return False

        return False

    def _select_yield_target(self, exclude: str) -> None:
        """Pick the best busy agent to evict when it finishes its current turn.

        Prefers background over interactive. Among same priority, picks the agent
        whose current query started earliest (running longest, most likely to finish soon).
        """
        sessions = self._get_sessions()

        background: list[tuple[float, str]] = []
        interactive: list[tuple[float, str]] = []

        for name, session in sessions.items():
            if name == exclude or name in self._protected:
                continue
            if name in self._yield_set:
                continue
            if session.client is None:
                continue
            started = session.activity.query_started if session.activity else None
            busy_secs = (
                (datetime.now(UTC) - started).total_seconds() if started else 0.0
            )
            bucket = interactive if name in self._interactive else background
            bucket.append((busy_secs, name))

        for candidates in [background, interactive]:
            if not candidates:
                continue
            candidates.sort(reverse=True, key=lambda x: x[0])
            target = candidates[0][1]
            self._yield_set.add(target)
            log.info(
                "Marked '%s' for yield after current turn (busy %.0fs)",
                target,
                candidates[0][0],
            )
            return

        log.warning(
            "No yield target available for waiter '%s' — all agents are protected",
            exclude,
        )
