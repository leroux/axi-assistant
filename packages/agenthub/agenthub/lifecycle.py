"""Agent lifecycle — wake, sleep, and transport management.

Module-level functions that take (hub, session) as arguments.
AgentHub delegates to these — not a god object with methods.

The hub injects SDK factories (make_agent_options, create_client,
disconnect_client) at construction. Lifecycle functions call them
without knowing the concrete SDK types.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from claudewire.events import ActivityState

if TYPE_CHECKING:
    from agenthub.hub import AgentHub
    from agenthub.types import AgentSession

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)


# ---------------------------------------------------------------------------
# Pure helpers — no hub dependency
# ---------------------------------------------------------------------------


def is_awake(session: AgentSession) -> bool:
    """True if the session has a connected client."""
    return session.client is not None


def is_processing(session: AgentSession) -> bool:
    """True if the agent's query_lock is currently held."""
    return session.query_lock.locked()


def count_awake(sessions: dict[str, AgentSession]) -> int:
    """Count sessions that have a connected client."""
    return sum(1 for s in sessions.values() if s.client is not None)


def reset_activity(session: AgentSession) -> None:
    """Reset idle tracking and activity state for a new query."""
    session.last_activity = datetime.now(UTC)
    session.idle_reminder_count = 0
    session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------


async def sleep_agent(
    hub: AgentHub, session: AgentSession, *, force: bool = False
) -> None:
    """Disconnect an agent's client and release its scheduler slot.

    If force=False (default), skips if the agent's query_lock is held.
    """
    if not force and session.query_lock.locked():
        log.debug("Skipping sleep for '%s' — query_lock is held", session.name)
        return

    if session.client is None:
        return

    with _tracer.start_as_current_span(
        "sleep_agent",
        attributes={"agent.name": session.name, "agent.force": force},
    ):
        log.info("Sleeping agent '%s'", session.name)
        if session.agent_log:
            session.agent_log.info("SESSION_SLEEP")
        session.bridge_busy = False
        session.transport = None
        await hub.disconnect_client(session.client, session.name)
        session.client = None
        hub.scheduler.release_slot(session.name)
        log.info("Agent '%s' is now sleeping", session.name)


# ---------------------------------------------------------------------------
# Wake
# ---------------------------------------------------------------------------


async def wake_agent(hub: AgentHub, session: AgentSession) -> None:
    """Wake a sleeping agent. Requests a scheduler slot and creates an SDK client.

    On resume failure, retries with a fresh session (no resume).
    Raises ConcurrencyLimitError if no slots are available.
    """
    if is_awake(session):
        return

    # Fail fast if the working directory no longer exists — avoids requesting
    # a scheduler slot and two SDK subprocess attempts that will both fail.
    if session.cwd and not os.path.isdir(session.cwd):
        raise RuntimeError(
            f"Working directory does not exist: {session.cwd}"
        )

    async with hub.wake_lock:
        if is_awake(session):
            return

        await hub.scheduler.request_slot(session.name, timeout=hub.slot_timeout)

        log.info("Waking agent '%s' (session_id=%s)", session.name, session.session_id)

        resume_id = session.session_id
        options = hub.make_agent_options(session, resume_id)

        with _tracer.start_as_current_span(
            "wake_agent",
            attributes={
                "agent.name": session.name,
                "agent.type": session.agent_type or "",
                "agent.resumed": bool(resume_id),
                "agent.cwd": session.cwd or "",
            },
        ):
            try:
                client = await hub.create_client(session, options)
                session.client = client
                log.info(
                    "Agent '%s' is now awake (resumed=%s)", session.name, resume_id
                )
                if session.agent_log:
                    session.agent_log.info(
                        "SESSION_WAKE (resumed=%s)", bool(resume_id)
                    )
                # Successful resume — clear any previous failure marker
                session.last_failed_resume_id = None
            except Exception:
                if resume_id:
                    log.warning(
                        "Failed to resume agent '%s' with session_id=%s, retrying fresh",
                        session.name,
                        resume_id,
                    )
                    options = hub.make_agent_options(session, None)
                    try:
                        client = await hub.create_client(session, options)
                    except Exception:
                        hub.scheduler.release_slot(session.name)
                        raise
                    session.client = client
                    session.session_id = None
                    # Remember the failed session_id so we don't save the same
                    # stale ID when the fresh session returns it in result messages.
                    session.last_failed_resume_id = resume_id
                    log.warning(
                        "Agent '%s' woke with fresh session (previous context lost)",
                        session.name,
                    )
                    if session.agent_log:
                        session.agent_log.info(
                            "SESSION_WAKE (resumed=False, fresh after resume failure)"
                        )
                else:
                    hub.scheduler.release_slot(session.name)
                    raise

        await hub.callbacks.on_wake(session.name)


# ---------------------------------------------------------------------------
# Wake-or-queue
# ---------------------------------------------------------------------------


async def wake_or_queue(
    hub: AgentHub,
    session: AgentSession,
    content: Any,
    metadata: Any = None,
) -> bool:
    """Try to wake an agent. On ConcurrencyLimitError, queue the message.

    Returns True if woken successfully, False if queued or failed.
    """
    from agenthub.types import ConcurrencyLimitError

    try:
        await wake_agent(hub, session)
        return True
    except ConcurrencyLimitError:
        session.message_queue.append((content, metadata))
        position = len(session.message_queue)
        awake = count_awake(hub.sessions)
        log.debug(
            "Concurrency limit hit for '%s', queuing message (position %d, %d awake)",
            session.name,
            position,
            awake,
        )
        return False
    except Exception:
        log.exception("Failed to wake agent '%s'", session.name)
        return False
