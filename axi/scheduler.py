"""Centralized agent scheduler — slot management and eviction.

Manages which agents hold awake slots. When slots are full:
1. Evicts the longest-idle non-busy agent (background before interactive).
2. If all agents are busy, queues the request and marks an agent for
   deferred eviction (sleep after its current turn completes).

Priority classes:
- Protected (master): never evicted
- Interactive: user recently sent a message — higher eviction resistance
- Background: everything else — evicted first
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from axi.axi_types import AgentSession

log = logging.getLogger("axi")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_max_slots: int = 10
_protected: set[str] = set()
_lock = asyncio.Lock()
_slots: set[str] = set()
_waiters: deque[tuple[str, asyncio.Event]] = deque()
_yield_set: set[str] = set()
_interactive: set[str] = set()

# Callbacks (set via init)
_get_agents: Callable[[], dict[str, AgentSession]] | None = None
_sleep_fn: Callable[[AgentSession], Awaitable[None]] | None = None


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


def init(
    *,
    max_slots: int,
    protected: set[str],
    get_agents: Callable[[], dict[str, AgentSession]],
    sleep_fn: Callable[[AgentSession], Awaitable[None]],
) -> None:
    """Initialize the scheduler. Called once at startup."""
    global _max_slots, _protected, _get_agents, _sleep_fn
    _max_slots = max_slots
    _protected = set(protected)
    _get_agents = get_agents
    _sleep_fn = sleep_fn
    _slots.clear()
    _waiters.clear()
    _yield_set.clear()
    _interactive.clear()
    log.info("Scheduler initialized: max_slots=%d, protected=%s", max_slots, protected)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def request_slot(agent_name: str, *, timeout: float = 120.0) -> None:
    """Acquire an awake slot. Blocks until one is available.

    1. Already has slot -> return.
    2. Slot available -> grant.
    3. Idle agents exist -> evict (background first), grant.
    4. All busy -> queue, mark a yield target, block until slot freed.

    Raises ConcurrencyLimitError on timeout.
    """
    if agent_name in _slots:
        return

    async with _lock:
        # Re-check after acquiring lock
        if agent_name in _slots:
            return

        # Fast path: slot available
        if len(_slots) < _max_slots:
            _slots.add(agent_name)
            log.debug("Slot granted to '%s' (%d/%d)", agent_name, len(_slots), _max_slots)
            return

        # Try evicting idle agents
        while len(_slots) >= _max_slots:
            evicted = await _evict_idle(exclude=agent_name)
            if not evicted:
                break

        if len(_slots) < _max_slots:
            _slots.add(agent_name)
            log.debug("Slot granted to '%s' after eviction (%d/%d)", agent_name, len(_slots), _max_slots)
            return

        # All agents busy — queue and mark an eviction target
        event = asyncio.Event()
        _waiters.append((agent_name, event))
        _select_yield_target(exclude=agent_name)
        log.info(
            "All %d slots busy, '%s' queued (position %d), yield targets: %s",
            _max_slots,
            agent_name,
            len(_waiters),
            _yield_set,
        )

    # Wait outside lock for a slot to be freed
    try:
        async with asyncio.timeout(timeout):
            await event.wait()
    except TimeoutError:
        # Race: slot might have been granted just as we timed out
        if agent_name in _slots:
            return
        # Clean up our waiter entry
        for i, (name, _) in enumerate(_waiters):
            if name == agent_name:
                del _waiters[i]
                break
        from axi.axi_types import ConcurrencyLimitError

        raise ConcurrencyLimitError(
            f"Cannot wake agent '{agent_name}': all {_max_slots} slots busy after {timeout:.0f}s wait. "
            f"Message will be queued and processed when a slot opens."
        ) from None

    # Slot was reserved for us by release_slot
    log.info("Slot granted to '%s' from wait queue (%d/%d)", agent_name, len(_slots), _max_slots)


def release_slot(agent_name: str) -> None:
    """Release a slot. Called when an agent sleeps.

    Sync — no lock needed (single-threaded asyncio event loop).
    Safe to call from inside _lock (eviction path).
    """
    was_held = agent_name in _slots
    _slots.discard(agent_name)
    _yield_set.discard(agent_name)
    _interactive.discard(agent_name)

    if not was_held:
        return

    log.debug("Slot released by '%s' (%d/%d)", agent_name, len(_slots), _max_slots)

    # Grant slot to next waiter
    while _waiters:
        waiter_name, event = _waiters[0]
        if event.is_set():
            _waiters.popleft()
            continue
        _waiters.popleft()
        _slots.add(waiter_name)
        event.set()
        log.info("Slot granted to waiter '%s' (freed by '%s')", waiter_name, agent_name)
        break


def should_yield(agent_name: str) -> bool:
    """Check if this agent should sleep after finishing its current turn."""
    return agent_name in _yield_set


def restore_slot(agent_name: str) -> None:
    """Register an agent that's already awake (e.g. reconnected from bridge).

    Unlike request_slot, this doesn't evict or queue — just records
    that the agent holds a slot. Used during startup/reconnect.
    """
    _slots.add(agent_name)
    log.debug("Slot restored for '%s' (%d/%d)", agent_name, len(_slots), _max_slots)


def mark_interactive(agent_name: str) -> None:
    """Mark agent as interactive (user sent a message). Higher eviction resistance."""
    _interactive.add(agent_name)


def mark_background(agent_name: str) -> None:
    """Mark agent as background (no pending user interaction). Lower eviction resistance."""
    _interactive.discard(agent_name)


def has_waiters() -> bool:
    """True if agents are queued waiting for slots."""
    return bool(_waiters)


def slot_count() -> int:
    """Current number of occupied slots."""
    return len(_slots)


def slots_full() -> bool:
    """True if all slots are occupied (wake will need eviction or queuing)."""
    return len(_slots) >= _max_slots


def status() -> dict[str, Any]:
    """Return scheduler state for diagnostics."""
    return {
        "max_slots": _max_slots,
        "slots": sorted(_slots),
        "slot_count": len(_slots),
        "waiters": [name for name, _ in _waiters],
        "yield_targets": sorted(_yield_set),
        "interactive": sorted(_interactive),
        "protected": sorted(_protected),
    }


# ---------------------------------------------------------------------------
# Internal: eviction
# ---------------------------------------------------------------------------


async def _evict_idle(exclude: str) -> bool:
    """Evict the longest-idle, non-busy, non-protected agent.

    Prefers background agents over interactive ones.
    Returns True if an agent was evicted.
    """
    assert _get_agents is not None
    assert _sleep_fn is not None
    agents = _get_agents()

    background: list[tuple[float, str, AgentSession]] = []
    interactive: list[tuple[float, str, AgentSession]] = []

    for name, session in agents.items():
        if name == exclude or name in _protected:
            continue
        if session.client is None:
            continue
        if session.query_lock.locked():
            continue
        if session.bridge_busy:
            continue
        idle_secs = (datetime.now(UTC) - session.last_activity).total_seconds()
        bucket = interactive if name in _interactive else background
        bucket.append((idle_secs, name, session))

    # Try background first, then interactive
    for bucket_name, candidates in [("background", background), ("interactive", interactive)]:
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
            await _sleep_fn(evict_session)
            return True
        except Exception:
            log.exception("Failed to evict agent '%s'", evict_name)
            return False

    return False


def _select_yield_target(exclude: str) -> None:
    """Pick the best busy agent to evict when it finishes its current turn.

    Prefers background over interactive. Among same priority, picks the agent
    whose current query started earliest (running longest, most likely to finish soon).
    """
    assert _get_agents is not None
    agents = _get_agents()

    background: list[tuple[float, str]] = []
    interactive: list[tuple[float, str]] = []

    for name, session in agents.items():
        if name == exclude or name in _protected:
            continue
        if name in _yield_set:
            continue
        if session.client is None:
            continue
        started = session.activity.query_started if session.activity else None
        busy_secs = (datetime.now(UTC) - started).total_seconds() if started else 0.0
        bucket = interactive if name in _interactive else background
        bucket.append((busy_secs, name))

    for candidates in [background, interactive]:
        if not candidates:
            continue
        candidates.sort(reverse=True, key=lambda x: x[0])
        target = candidates[0][1]
        _yield_set.add(target)
        log.info("Marked '%s' for yield after current turn (busy %.0fs)", target, candidates[0][0])
        return

    log.warning("No yield target available for waiter '%s' — all agents are protected", exclude)
