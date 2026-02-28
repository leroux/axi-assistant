"""Agent handlers - polymorphic handlers for different agent types.

Each handler manages the lifecycle and message processing for a specific agent type.
State lives in AgentSession; handlers are stateless orchestrators.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from claude_agent_sdk import ClaudeSDKClient

if TYPE_CHECKING:
    from bot import AgentSession

log = logging.getLogger("__main__")  # Use bot.py's logger for consistent log output


class AgentHandler(ABC):
    """Base class for agent type handlers.

    Handlers orchestrate agent operations without owning state.
    All state lives in AgentSession.
    """

    @abstractmethod
    def is_awake(self, session: AgentSession) -> bool:
        """Check if agent is ready to process messages."""
        pass

    @abstractmethod
    def is_processing(self, session: AgentSession) -> bool:
        """Check if agent has active work."""
        pass

    @abstractmethod
    async def wake(self, session: AgentSession) -> None:
        """Activate/initialize the agent. May raise ConcurrencyLimitError or other exceptions."""
        pass

    @abstractmethod
    async def sleep(self, session: AgentSession) -> None:
        """Deactivate/cleanup the agent."""
        pass

    @abstractmethod
    async def process_message(
        self,
        session: AgentSession,
        content: str | list,
        channel: discord.TextChannel,
    ) -> None:
        """Process a user message. Raise RuntimeError if unable."""
        pass


class ClaudeCodeHandler(AgentHandler):
    """Handler for interactive Claude Code agents with wake/sleep lifecycle."""

    def is_awake(self, session: AgentSession) -> bool:
        """Agent is awake if it has a client."""
        return session.client is not None

    def is_processing(self, session: AgentSession) -> bool:
        """Agent is processing if query lock is held."""
        return session.query_lock.locked()

    async def wake(self, session: AgentSession) -> None:
        """Create ClaudeSDKClient and connect to it.

        May raise:
            ConcurrencyLimitError: If all awake slots are in use
            Exception: If client creation/connection fails
        """
        if self.is_awake(session):
            return

        log.debug("Waking Claude Code agent '%s'", session.name)

        # Import here to avoid circular dependency
        from bot import (
            _create_transport,
            _make_agent_options,
        )

        options = _make_agent_options(session, session.session_id)

        # Create transport (bridge or direct)
        # If session_id is set, we're reconnecting/resuming an existing CLI
        is_reconnecting = session.session_id is not None
        transport = await _create_transport(session, reconnecting=is_reconnecting)

        # Create and connect client
        session.client = ClaudeSDKClient(options=options, transport=transport)
        await session.client.__aenter__()

        log.info("Claude Code agent '%s' awake", session.name)
        if session._log:
            session._log.info("SESSION_WAKE")

    async def sleep(self, session: AgentSession) -> None:
        """Disconnect ClaudeSDKClient."""
        if not self.is_awake(session):
            return

        log.debug("Sleeping Claude Code agent '%s'", session.name)

        # Import here to avoid circular dependency
        from bot import _ensure_process_dead, _get_subprocess_pid

        pid = _get_subprocess_pid(session.client)
        try:
            await asyncio.wait_for(
                session.client.__aexit__(None, None, None),
                timeout=5.0,
            )
        except (TimeoutError, asyncio.CancelledError):
            log.warning("'%s' shutdown timed out or cancelled", session.name)
        except Exception:
            log.exception("Error disconnecting agent '%s'", session.name)
        finally:
            _ensure_process_dead(pid, session.name)
            session.client = None

        session._bridge_busy = False
        log.info("Claude Code agent '%s' sleeping", session.name)
        if session._log:
            session._log.info("SESSION_SLEEP")

    async def process_message(
        self,
        session: AgentSession,
        content: str | list,
        channel: discord.TextChannel,
    ) -> None:
        """Run a query through Claude Code.

        Raises:
            RuntimeError: If agent is not awake
        """
        if not self.is_awake(session):
            raise RuntimeError(f"Claude Code agent '{session.name}' not awake")

        # Import here to avoid circular dependency
        from bot import (
            QUERY_TIMEOUT,
            _as_stream,
            _content_summary,
            _handle_query_timeout,
            _stream_with_retry,
            drain_sdk_buffer,
            drain_stderr,
        )

        session.last_activity = datetime.now(UTC)
        session.last_idle_notified = None
        session.idle_reminder_count = 0
        session._bridge_busy = False

        drain_stderr(session)
        drained = drain_sdk_buffer(session)

        if session._log:
            session._log.info("USER: %s", _content_summary(content))

        log.info("HANDLER[%s] process_message: drained=%d, calling query+stream", session.name, drained)
        try:
            async with asyncio.timeout(QUERY_TIMEOUT):
                log.info("HANDLER[%s] calling client.query()", session.name)
                await session.client.query(_as_stream(content))
                log.info("HANDLER[%s] query() returned, calling _stream_with_retry()", session.name)
                await _stream_with_retry(session, channel)
        except TimeoutError:
            await _handle_query_timeout(session, channel)
        except Exception:
            log.exception("Error querying Claude Code agent '%s'", session.name)
            raise RuntimeError(f"Query failed for agent '{session.name}'") from None


class FlowcoderHandler(AgentHandler):
    """Handler for flowcoder agents (fire-and-forget execution)."""

    def is_awake(self, session: AgentSession) -> bool:
        """Agent is awake if process is created."""
        return session.flowcoder_process is not None

    def is_processing(self, session: AgentSession) -> bool:
        """Agent is processing if process is running."""
        return session.flowcoder_process is not None and session.flowcoder_process.is_running

    async def wake(self, session: AgentSession) -> None:
        """Spawn and start flowcoder process."""
        if self.is_awake(session):
            return

        log.debug("Waking flowcoder agent '%s'", session.name)

        # Import here to avoid circular dependency
        from bot import bridge_conn
        from flowcoder import BridgeFlowcoderProcess, FlowcoderProcess

        if bridge_conn and bridge_conn.is_alive:
            proc = BridgeFlowcoderProcess(
                bridge_name=f"{session.name}:flowcoder",
                conn=bridge_conn,
                command=session.flowcoder_command,
                args=session.flowcoder_args,
                cwd=session.cwd,
            )
        else:
            proc = FlowcoderProcess(
                command=session.flowcoder_command,
                args=session.flowcoder_args,
                cwd=session.cwd,
            )

        await proc.start()
        session.flowcoder_process = proc

        log.info(
            "Flowcoder agent '%s' started (command=%s)",
            session.name,
            session.flowcoder_command,
        )

    async def sleep(self, session: AgentSession) -> None:
        """Stop flowcoder process."""
        if not self.is_awake(session):
            return

        proc = session.flowcoder_process

        if getattr(proc, "is_bridge_backed", False) and session.query_lock.locked():
            # Mid-execution: detach so bridge can buffer
            await proc.detach()
            log.info("Flowcoder '%s' detached (bridge buffering)", session.name)
        else:
            await proc.stop()
            log.info("Flowcoder '%s' stopped", session.name)

        session.flowcoder_process = None

    async def process_message(
        self,
        session: AgentSession,
        content: str | list,
        channel: discord.TextChannel,
    ) -> None:
        """Send message to flowcoder stdin.

        Raises:
            RuntimeError: If flowcoder is not running
        """
        if not self.is_processing(session):
            raise RuntimeError(f"Flowcoder '{session.name}' not running")

        user_msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": content if isinstance(content, str) else str(content),
            },
        }
        await session.flowcoder_process.send(user_msg)

        log.debug("Sent message to flowcoder '%s'", session.name)


# Singleton instances (stateless, can be reused)
_claude_code_handler = ClaudeCodeHandler()
_flowcoder_handler = FlowcoderHandler()


def get_handler(agent_type: str) -> AgentHandler:
    """Get the handler for an agent type.

    Args:
        agent_type: "claude_code" or "flowcoder"

    Returns:
        AgentHandler instance

    Raises:
        ValueError: If agent_type is unknown
    """
    if agent_type == "claude_code":
        return _claude_code_handler
    elif agent_type == "flowcoder":
        return _flowcoder_handler
    else:
        raise ValueError(f"Unknown agent type: {agent_type}")
