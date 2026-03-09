"""Message processing — query dispatch, retry, timeout, interrupt.

Drives the SDK interaction for conversation turns. Rendering is not
done here — the frontend provides a stream_handler callback that
consumes the SDK stream and renders to the user.

StreamHandlerFn: async (session) -> str | None
  Returns None on success, or an error string for transient errors (retry).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from agenthub import lifecycle
from claudewire.events import ActivityState, as_stream, tool_display

if TYPE_CHECKING:
    from agenthub.hub import AgentHub
    from agenthub.types import AgentSession

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

# Callback: consume the SDK stream, render to user, return error or None.
StreamHandlerFn = Callable[["AgentSession"], Awaitable[str | None]]


# ---------------------------------------------------------------------------
# Result of receive_user_message
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReceiveResult:
    """Outcome of receive_user_message.

    status values:
        "processed"            — message was sent and response streamed
        "queued"               — agent busy, message appended to queue
        "queued_reconnecting"  — agent reconnecting, message queued
        "shutdown"             — hub is shutting down, message rejected
        "error"                — unrecoverable error during processing
        "timeout"              — query timed out, session was recovered
    """

    status: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Interrupt
# ---------------------------------------------------------------------------


async def interrupt_session(hub: AgentHub, session: AgentSession) -> None:
    """Gracefully interrupt the current turn for an agent session.

    Sends SIGINT via procmux "interrupt" command if connected, otherwise
    falls back to the SDK's interrupt method.  The CLI process stays alive
    with conversation context preserved.
    """
    if hub.process_conn is not None:
        try:
            result = await hub.process_conn.send_command("interrupt", name=session.name)
            if not result.ok:
                log.warning(
                    "Bridge interrupt for '%s' failed: %s", session.name, result.error
                )
        except Exception:
            log.exception("Bridge interrupt for '%s' raised", session.name)
        return

    # Fallback: SDK interrupt
    if session.client is not None:
        try:
            async with asyncio.timeout(5):
                await session.client.interrupt()
        except (TimeoutError, Exception):
            pass


async def graceful_interrupt(session: AgentSession) -> bool:
    """Gracefully interrupt the current turn via the SDK control protocol.

    Sends control_request.interrupt to the CLI, which aborts the current
    API call and emits a result.  The CLI stays alive with full conversation
    context — ready for the next user message.

    Returns True if the interrupt was acknowledged, False on failure.
    On failure the queued message will process after the current turn finishes.
    """
    if session.client is None:
        log.debug("graceful_interrupt: no client for '%s'", session.name)
        return False

    try:
        async with asyncio.timeout(5):
            await session.client.interrupt()
        log.info("INTERRUPT[%s] graceful interrupt sent", session.name)
        return True
    except TimeoutError:
        log.warning("INTERRUPT[%s] graceful interrupt timed out", session.name)
        return False
    except Exception:
        log.warning("INTERRUPT[%s] graceful interrupt failed", session.name, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Core: process one conversation turn
# ---------------------------------------------------------------------------


async def process_message(
    hub: AgentHub,
    session: AgentSession,
    content: Any,
    stream_handler: StreamHandlerFn,
) -> None:
    """Drive one conversation turn: send query, stream response with retry.

    The stream_handler (provided by frontend) iterates the SDK stream,
    renders to the user, and returns None on success or an error string
    for transient errors that should be retried.

    Raises RuntimeError on unrecoverable query failure.
    Raises TimeoutError if the query exceeds hub.query_timeout.
    """
    if session.client is None:
        raise RuntimeError(f"Agent '{session.name}' not awake")

    lifecycle.reset_activity(session)
    session.bridge_busy = False

    if session.agent_log:
        preview = content[:200] if isinstance(content, str) else str(content)[:200]
        session.agent_log.info("USER: %s", preview)

    with _tracer.start_as_current_span(
        "process_message",
        attributes={
            "agent.name": session.name,
            "agent.type": session.agent_type or "claude_code",
            "message.length": len(content) if isinstance(content, str) else -1,
        },
    ):
        try:
            async with asyncio.timeout(hub.query_timeout):
                await session.client.query(as_stream(content))
                await _stream_with_retry(hub, session, stream_handler)
        except TimeoutError:
            raise
        except Exception:
            log.exception("Error querying agent '%s'", session.name)
            raise RuntimeError(f"Query failed for agent '{session.name}'") from None


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


async def _stream_with_retry(
    hub: AgentHub,
    session: AgentSession,
    stream_handler: StreamHandlerFn,
) -> bool:
    """Stream response with retry on transient API errors. Returns True on success."""
    with _tracer.start_as_current_span(
        "stream_with_retry", attributes={"agent.name": session.name}
    ) as span:
        error = await stream_handler(session)
        if error is None:
            span.set_attribute("retry.attempts", 1)
            return True

        log.warning("Transient error for '%s': %s — will retry", session.name, error)
        for attempt in range(2, hub.max_retries + 1):
            delay = hub.retry_base_delay * (2 ** (attempt - 2))
            log.warning(
                "Agent '%s' retrying in %ds (attempt %d/%d)",
                session.name,
                delay,
                attempt,
                hub.max_retries,
            )
            await hub.callbacks.post_system(
                session.name,
                f"API error, retrying in {delay}s... (attempt {attempt}/{hub.max_retries})",
            )
            await asyncio.sleep(delay)

            try:
                assert session.client is not None
                await session.client.query(
                    as_stream("Continue from where you left off.")
                )
            except Exception:
                log.exception("Agent '%s' retry query failed", session.name)
                continue

            error = await stream_handler(session)
            if error is None:
                span.set_attribute("retry.attempts", attempt)
                return True

        log.error(
            "Agent '%s' transient error persisted after %d retries",
            session.name,
            hub.max_retries,
        )
        await hub.callbacks.post_system(
            session.name,
            f"API error persisted after {hub.max_retries} retries. Try again later.",
        )
        span.set_attribute("retry.exhausted", True)
        span.set_status(trace.StatusCode.ERROR, "retries exhausted")
        return False


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------


async def handle_query_timeout(hub: AgentHub, session: AgentSession) -> None:
    """Handle a query timeout: interrupt the CLI, rebuild the session."""
    from agenthub import registry

    log.warning("Query timeout for agent '%s', killing session", session.name)

    try:
        await interrupt_session(hub, session)
    except Exception:
        log.exception("interrupt_session failed for '%s'", session.name)

    old_session_id = session.session_id
    new_session = await registry.rebuild_session(
        hub, session.name, session_id=old_session_id
    )

    if old_session_id:
        await hub.callbacks.post_system(
            new_session.name,
            f"Agent **{new_session.name}** timed out and was recovered (sleeping). Context preserved.",
        )
    else:
        await hub.callbacks.post_system(
            new_session.name,
            f"Agent **{new_session.name}** timed out and was reset (sleeping). Context lost.",
        )


# ---------------------------------------------------------------------------
# User message ingestion (single entry point for all frontends)
# ---------------------------------------------------------------------------


async def receive_user_message(
    hub: AgentHub,
    session: AgentSession,
    content: Any,
    stream_handler: StreamHandlerFn,
    *,
    queue_item: Any = None,
) -> ReceiveResult:
    """Receive a user message for an agent — single entry point for all frontends.

    Handles:
    - Shutdown rejection
    - Reconnecting → queue
    - Busy → queue
    - Lock acquisition
    - Wake-if-sleeping (with ConcurrencyLimitError handling)
    - Process message + stream with retry
    - Error/timeout handling

    Does NOT drain the queue or check scheduler yield — the caller handles
    post-processing since queue item format is frontend-specific.

    Args:
        queue_item: What to append to message_queue when queuing is needed.
            Defaults to ``(content, None)``. Frontends pass their own format
            (e.g. Discord uses ``(content, channel, orig_message)``).

    Returns:
        ReceiveResult with status and optional error description.
    """
    from agenthub.types import ConcurrencyLimitError

    if queue_item is None:
        queue_item = (content, None)

    if hub.shutdown_requested:
        await hub.callbacks.post_system(
            session.name, "Bot is restarting — not accepting new messages."
        )
        return ReceiveResult(status="shutdown")

    # Reconnecting: queue
    if session.reconnecting:
        session.message_queue.append(queue_item)
        position = len(session.message_queue)
        await hub.callbacks.post_system(
            session.name,
            f"Agent **{session.name}** is reconnecting — message queued (position {position}).",
        )
        return ReceiveResult(status="queued_reconnecting")

    # Busy: queue and interrupt current turn for early processing
    if session.query_lock.locked():
        session.message_queue.append(queue_item)
        position = len(session.message_queue)
        if session.compacting:
            # Don't interrupt during compaction
            await hub.callbacks.post_system(
                session.name,
                f"\U0001f504 Agent **{session.name}** is compacting context — message queued (position {position}). "
                "Will process after compaction completes.",
            )
        else:
            # Describe what the agent is currently doing
            activity = session.activity
            tool_suffix = ""
            if activity.phase == "waiting" and activity.tool_name:
                tool_suffix = f" (currently {tool_display(activity.tool_name)})"

            interrupted = await graceful_interrupt(session)
            if interrupted:
                await hub.callbacks.post_system(
                    session.name,
                    f"Agent **{session.name}** is busy — message queued (position {position}). "
                    f"Interrupting current task.{tool_suffix}",
                )
            else:
                await hub.callbacks.post_system(
                    session.name,
                    f"Agent **{session.name}** is busy — message queued (position {position}). "
                    f"Will process after current turn.{tool_suffix}",
                )
        return ReceiveResult(status="queued")

    # Normal processing path
    hub.scheduler.mark_interactive(session.name)
    lifecycle.reset_activity(session)
    session.bridge_busy = False

    if session.agent_log:
        preview = content[:200] if isinstance(content, str) else str(content)[:200]
        session.agent_log.info("USER: %s", preview)

    result = ReceiveResult(status="processed")

    with _tracer.start_as_current_span(
        "receive_user_message",
        attributes={
            "agent.name": session.name,
            "agent.type": session.agent_type or "claude_code",
            "message.length": len(content) if isinstance(content, str) else -1,
        },
    ):
        async with session.query_lock:
            if not lifecycle.is_awake(session):
                try:
                    await lifecycle.wake_agent(hub, session)
                except ConcurrencyLimitError:
                    session.message_queue.append(queue_item)
                    await hub.callbacks.post_system(
                        session.name,
                        "All agent slots busy. Message queued — will run when a slot opens.",
                    )
                    return ReceiveResult(status="queued")
                except Exception:
                    log.exception(
                        "Failed to wake agent '%s' for user message", session.name
                    )
                    await hub.callbacks.post_system(
                        session.name,
                        f"Failed to wake agent **{session.name}**.",
                    )
                    return ReceiveResult(status="error", error="Failed to wake agent")

            session.activity = ActivityState(
                phase="starting", query_started=datetime.now(UTC)
            )

            try:
                await process_message(hub, session, content, stream_handler)
            except TimeoutError:
                await handle_query_timeout(hub, session)
                result = ReceiveResult(status="timeout")
            except RuntimeError as e:
                log.warning(
                    "Runtime error for '%s': %s", session.name, e
                )
                await hub.callbacks.post_system(session.name, str(e))
                result = ReceiveResult(status="error", error=str(e))
            except Exception as e:
                log.exception(
                    "Error processing message for '%s'", session.name
                )
                await hub.callbacks.post_system(
                    session.name,
                    f"Error communicating with agent **{session.name}**.",
                )
                result = ReceiveResult(status="error", error=str(e))
            finally:
                session.activity = ActivityState(phase="idle")

    return result


# ---------------------------------------------------------------------------
# Initial prompt
# ---------------------------------------------------------------------------


async def run_initial_prompt(
    hub: AgentHub,
    session: AgentSession,
    prompt: Any,
    stream_handler: StreamHandlerFn,
    metadata: Any = None,
) -> None:
    """Run the initial prompt for a spawned agent.

    Acquires the query_lock, wakes the agent, processes the prompt,
    then yields/queues/sleeps as appropriate.
    """
    from agenthub.types import ConcurrencyLimitError

    try:
        async with session.query_lock:
            if not lifecycle.is_awake(session):
                try:
                    await lifecycle.wake_agent(hub, session)
                except ConcurrencyLimitError:
                    log.info(
                        "Concurrency limit hit for '%s' initial prompt — queuing",
                        session.name,
                    )
                    session.message_queue.append((prompt, metadata))
                    await hub.callbacks.post_system(
                        session.name,
                        "All agent slots busy. Initial prompt queued — will run when a slot opens.",
                    )
                    return
                except Exception:
                    log.exception(
                        "Failed to wake agent '%s' for initial prompt", session.name
                    )
                    await hub.callbacks.post_system(
                        session.name,
                        f"Failed to wake agent **{session.name}**.",
                    )
                    return

            session.last_activity = datetime.now(UTC)
            session.activity = ActivityState(
                phase="starting", query_started=datetime.now(UTC)
            )

            try:
                await process_message(hub, session, prompt, stream_handler)
                session.last_activity = datetime.now(UTC)
            except TimeoutError:
                await handle_query_timeout(hub, session)
            except RuntimeError as e:
                log.warning(
                    "Handler error for '%s' initial prompt: %s", session.name, e
                )
                await hub.callbacks.post_system(session.name, f"Error: {e}")
            finally:
                session.activity = ActivityState(phase="idle")

        log.debug("Initial prompt completed for '%s'", session.name)
        await hub.callbacks.post_system(
            session.name,
            f"Agent **{session.name}** finished initial task.",
        )

    except Exception:
        log.exception(
            "Error running initial prompt for agent '%s'", session.name
        )
        await hub.callbacks.post_system(
            session.name,
            f"Agent **{session.name}** encountered an error during initial task.",
        )

    if hub.scheduler.should_yield(session.name):
        log.info(
            "Scheduler yield: '%s' sleeping after initial prompt (skipping queue)",
            session.name,
        )
    else:
        await process_message_queue(hub, session, stream_handler)

    try:
        await lifecycle.sleep_agent(hub, session)
    except Exception:
        log.exception(
            "Error sleeping agent '%s' after initial prompt", session.name
        )


# ---------------------------------------------------------------------------
# Message queue
# ---------------------------------------------------------------------------


async def process_message_queue(
    hub: AgentHub,
    session: AgentSession,
    stream_handler: StreamHandlerFn,
) -> None:
    """Process queued messages for an agent after the current query finishes."""
    if session.message_queue:
        log.info(
            "QUEUE[%s] processing %d queued messages",
            session.name,
            len(session.message_queue),
        )
        _tracer.start_span(
            "process_message_queue",
            attributes={
                "agent.name": session.name,
                "queue.size": len(session.message_queue),
            },
        ).end()

    while session.message_queue:
        if hub.shutdown_requested:
            log.info(
                "Shutdown requested — not processing further queued messages for '%s'",
                session.name,
            )
            break
        if hub.scheduler.should_yield(session.name):
            log.info(
                "Scheduler yield: '%s' deferring %d queued messages",
                session.name,
                len(session.message_queue),
            )
            await lifecycle.sleep_agent(hub, session)
            return

        content, metadata = session.message_queue.popleft()
        remaining = len(session.message_queue)
        log.debug(
            "Processing queued message for '%s' (%d remaining)",
            session.name,
            remaining,
        )

        preview = content[:200] if isinstance(content, str) else str(content)[:200]
        remaining_str = f" ({remaining} more in queue)" if remaining > 0 else ""
        await hub.callbacks.post_system(
            session.name,
            f"Processing queued message{remaining_str}:\n> {preview}",
        )

        async with session.query_lock:
            if not lifecycle.is_awake(session):
                try:
                    await lifecycle.wake_agent(hub, session)
                except Exception:
                    log.exception(
                        "Failed to wake agent '%s' for queued message",
                        session.name,
                    )
                    await hub.callbacks.post_system(
                        session.name,
                        f"Failed to wake agent **{session.name}** — dropping queued messages.",
                    )
                    session.message_queue.clear()
                    return

            lifecycle.reset_activity(session)
            try:
                await process_message(hub, session, content, stream_handler)
            except TimeoutError:
                await handle_query_timeout(hub, session)
            except RuntimeError as e:
                log.warning(
                    "Runtime error processing queued message for '%s': %s",
                    session.name,
                    e,
                )
                await hub.callbacks.post_system(session.name, str(e))
            except Exception:
                log.exception(
                    "Error processing queued message for '%s'", session.name
                )
                await hub.callbacks.post_system(
                    session.name,
                    f"Error processing queued message for **{session.name}**.",
                )
            finally:
                session.activity = ActivityState(phase="idle")


# ---------------------------------------------------------------------------
# Inter-agent messaging
# ---------------------------------------------------------------------------


async def deliver_inter_agent_message(
    hub: AgentHub,
    sender_name: str,
    target_session: AgentSession,
    content: str,
    stream_handler: StreamHandlerFn,
) -> str:
    """Deliver a message from one agent to another.

    If the target is busy, interrupts it and queues the message.
    If idle, fires a background task to wake and process.
    Returns a status string.
    """
    _tracer.start_span(
        "deliver_inter_agent_message",
        attributes={
            "agent.sender": sender_name,
            "agent.target": target_session.name,
            "message.length": len(content),
        },
    ).end()

    await hub.callbacks.post_system(
        target_session.name,
        f"Message from {sender_name}:\n> {content}",
    )

    ts_prefix = datetime.now(UTC).strftime("[%Y-%m-%d %H:%M:%S UTC] ")
    prompt = ts_prefix + f"[Inter-agent message from {sender_name}] {content}"

    if target_session.query_lock.locked():
        target_session.message_queue.appendleft((prompt, None))
        if target_session.compacting:
            # Don't interrupt during compaction — message is queued, will process after
            log.info(
                "Inter-agent message from '%s' to compacting agent '%s' — queued (no interrupt)",
                sender_name,
                target_session.name,
            )
            return f"delivered to compacting agent '{target_session.name}' (queued, will process after compaction)"
        log.info(
            "Inter-agent message from '%s' to busy agent '%s' — interrupting",
            sender_name,
            target_session.name,
        )
        interrupted = await graceful_interrupt(target_session)
        if not interrupted:
            log.warning(
                "Graceful interrupt failed for '%s' inter-agent message (message still queued)",
                target_session.name,
            )
        return f"delivered to busy agent '{target_session.name}' (interrupted, will process next)"
    else:
        hub.tasks.fire_and_forget(
            _process_inter_agent_prompt(
                hub, target_session, prompt, stream_handler
            )
        )
        return f"delivered to agent '{target_session.name}'"


async def _process_inter_agent_prompt(
    hub: AgentHub,
    session: AgentSession,
    content: str,
    stream_handler: StreamHandlerFn,
) -> None:
    """Background task: wake (if needed) and process an inter-agent message."""
    from agenthub.types import ConcurrencyLimitError

    try:
        async with session.query_lock:
            if not lifecycle.is_awake(session):
                try:
                    await lifecycle.wake_agent(hub, session)
                except ConcurrencyLimitError:
                    session.message_queue.append((content, None))
                    log.info(
                        "Concurrency limit hit for '%s' inter-agent message — queuing",
                        session.name,
                    )
                    await hub.callbacks.post_system(
                        session.name,
                        "All agent slots busy. Inter-agent message queued.",
                    )
                    return
                except Exception:
                    log.exception(
                        "Failed to wake agent '%s' for inter-agent message",
                        session.name,
                    )
                    await hub.callbacks.post_system(
                        session.name,
                        f"Failed to wake agent **{session.name}** for inter-agent message.",
                    )
                    return

            lifecycle.reset_activity(session)
            try:
                await process_message(hub, session, content, stream_handler)
            except TimeoutError:
                await handle_query_timeout(hub, session)
            except RuntimeError as e:
                log.warning(
                    "Runtime error processing inter-agent message for '%s': %s",
                    session.name,
                    e,
                )
                await hub.callbacks.post_system(session.name, str(e))
            except Exception:
                log.exception(
                    "Error processing inter-agent message for '%s'",
                    session.name,
                )
                await hub.callbacks.post_system(
                    session.name,
                    f"Error processing inter-agent message for **{session.name}**.",
                )
            finally:
                session.activity = ActivityState(phase="idle")

        if hub.scheduler.should_yield(session.name):
            log.info(
                "Scheduler yield: '%s' sleeping after inter-agent message",
                session.name,
            )
            await lifecycle.sleep_agent(hub, session)
        else:
            await process_message_queue(hub, session, stream_handler)
    except Exception:
        log.exception(
            "Unhandled error in _process_inter_agent_prompt for '%s'",
            session.name,
        )
