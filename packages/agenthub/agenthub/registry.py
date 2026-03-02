"""Agent session registry — create, destroy, and look up sessions.

Module-level functions that take (hub, ...) as arguments.
Session dict (hub.sessions) is public — these functions mutate it directly.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

if TYPE_CHECKING:
    from agenthub.hub import AgentHub
    from agenthub.types import AgentSession

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


def register_session(hub: AgentHub, session: AgentSession) -> None:
    """Add a session to the hub's registry."""
    hub.sessions[session.name] = session
    log.info("Session '%s' registered", session.name)


def unregister_session(hub: AgentHub, name: str) -> AgentSession | None:
    """Remove and return a session. Returns None if not found."""
    session = hub.sessions.pop(name, None)
    if session:
        log.info("Session '%s' unregistered", name)
    return session


def get_session(hub: AgentHub, name: str) -> AgentSession | None:
    """Look up a session by name."""
    return hub.sessions.get(name)


# ---------------------------------------------------------------------------
# End session
# ---------------------------------------------------------------------------


async def end_session(hub: AgentHub, name: str) -> None:
    """End a named session: disconnect client, release slot, remove from registry."""
    with _tracer.start_as_current_span("end_session", attributes={"agent.name": name}):
        session = hub.sessions.get(name)
        if session is None:
            return
        if session.client is not None:
            await hub.disconnect_client(session.client, name)
            session.client = None
            hub.scheduler.release_slot(name)
        if session.agent_log:
            for handler in list(session.agent_log.handlers):
                handler.close()
                session.agent_log.removeHandler(handler)
        hub.sessions.pop(name, None)
        log.info("Session '%s' ended", name)


# ---------------------------------------------------------------------------
# Rebuild / reset
# ---------------------------------------------------------------------------


async def rebuild_session(
    hub: AgentHub,
    name: str,
    *,
    cwd: str | None = None,
    session_id: str | None = None,
    system_prompt: Any = None,
    mcp_servers: dict[str, Any] | None = None,
) -> AgentSession:
    """End an existing session and create a fresh sleeping AgentSession.

    Preserves system prompt, cwd, and MCP servers from the old session
    unless overrides are provided.
    """
    from agenthub.types import AgentSession as SessionType

    old = hub.sessions.get(name)
    resolved_cwd = cwd or (old.cwd if old else "")
    resolved_prompt = system_prompt or (old.system_prompt if old else None)
    resolved_mcp = mcp_servers if mcp_servers is not None else (old.mcp_servers if old else None)
    old_frontend = old.frontend_state if old else None

    await end_session(hub, name)

    new_session = SessionType(
        name=name,
        cwd=resolved_cwd,
        system_prompt=resolved_prompt,
        client=None,
        session_id=session_id,
        mcp_servers=resolved_mcp,
        frontend_state=old_frontend,
    )
    hub.sessions[name] = new_session
    return new_session


async def reset_session(
    hub: AgentHub, name: str, cwd: str | None = None
) -> AgentSession:
    """Reset a named session. Preserves system prompt and MCP servers."""
    new_session = await rebuild_session(hub, name, cwd=cwd)
    log.info("Session '%s' reset (sleeping, cwd=%s)", name, new_session.cwd)
    return new_session


# ---------------------------------------------------------------------------
# Reclaim
# ---------------------------------------------------------------------------


async def reclaim_agent_name(hub: AgentHub, name: str) -> None:
    """If an agent with *name* already exists, kill it to free the name."""
    from agenthub import lifecycle

    if name not in hub.sessions:
        return
    _tracer.start_span(
        "reclaim_agent_name", attributes={"agent.name": name}
    ).end()
    log.info("Reclaiming agent name '%s' — terminating existing session", name)
    session = hub.sessions[name]
    await lifecycle.sleep_agent(hub, session, force=True)
    hub.sessions.pop(name, None)


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


async def spawn_agent(
    hub: AgentHub,
    *,
    name: str,
    cwd: str,
    agent_type: str = "claude_code",
    initial_prompt: str = "",
    resume: str | None = None,
    system_prompt: Any = None,
    mcp_servers: dict[str, Any] | None = None,
    frontend_state: Any = None,
) -> AgentSession:
    """Create a new agent session and register it.

    Does NOT run the initial prompt — caller is responsible for that
    (typically via messaging.run_initial_prompt or a frontend wrapper).
    This keeps spawn_agent a pure registry operation.
    """
    from agenthub.types import AgentSession as SessionType

    with _tracer.start_as_current_span(
        "spawn_agent",
        attributes={
            "agent.name": name,
            "agent.type": agent_type,
            "agent.cwd": cwd,
            "agent.resumed": bool(resume),
        },
    ):
        os.makedirs(cwd, exist_ok=True)

        session = SessionType(
            name=name,
            agent_type=agent_type,
            cwd=cwd,
            system_prompt=system_prompt,
            client=None,
            session_id=resume,
            mcp_servers=mcp_servers,
            frontend_state=frontend_state,
        )

        hub.sessions[name] = session
        log.info(
            "Agent '%s' registered (type=%s, cwd=%s, resume=%s)",
            name,
            agent_type,
            cwd,
            resume,
        )

        await hub.callbacks.on_spawn(session)
        return session
