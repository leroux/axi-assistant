# pyright: reportPrivateUsage=false
"""Message processing, spawning, and inter-agent delivery."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import channels as _channels_mod
import config
from agents._discord import add_reaction, content_summary, remove_reaction, send_long, send_system
from agents._lifecycle import (
    _post_model_warning,
    _reset_session_activity,
    count_awake_agents,
    is_awake,
    sleep_agent,
    wake_agent,
)
from agents._sdk import as_stream, drain_sdk_buffer, drain_stderr
from agents._state import agents, channel_to_agent
from agents._streaming import handle_query_timeout, stream_with_retry
from axi_types import ActivityState, AgentSession, ConcurrencyLimitError, MessageContent
from channels import ensure_agent_channel, format_channel_topic, get_agent_channel, normalize_channel_name
from prompts import compute_prompt_hash, make_spawned_agent_system_prompt
from schedule_tools import make_schedule_mcp_server

if TYPE_CHECKING:
    from discord import TextChannel

log = logging.getLogger("axi")


# ---------------------------------------------------------------------------
# process_message — unified handler replacement
# ---------------------------------------------------------------------------


async def process_message(session: AgentSession, content: MessageContent, channel: TextChannel) -> None:
    """Process a user message through the appropriate agent type.

    Replaces the former AgentHandler.process_message() delegation.
    """
    if session.agent_type == "flowcoder":
        if session.flowcoder_process is None:
            raise RuntimeError(f"Flowcoder '{session.name}' not running")
        if isinstance(content, str):
            text = content
        else:
            # Extract text blocks only — flowcoder doesn't support images/attachments
            text_parts = [b["text"] for b in content if b.get("type") == "text"]
            if not text_parts:
                await send_system(channel, "Flowcoder agents don't support image-only messages.")
                return
            text = "\n".join(text_parts)
        user_msg = {
            "type": "user",
            "message": {"role": "user", "content": text},
        }
        await session.flowcoder_process.send(user_msg)
        log.debug("Sent message to flowcoder '%s'", session.name)
        return

    # Claude Code agent
    if session.client is None:
        raise RuntimeError(f"Claude Code agent '{session.name}' not awake")

    _reset_session_activity(session)
    session.bridge_busy = False
    drain_stderr(session)
    drained = drain_sdk_buffer(session)

    if session.agent_log:
        session.agent_log.info("USER: %s", content_summary(content))
    log.info("PROCESS[%s] drained=%d, calling query+stream", session.name, drained)
    try:
        async with asyncio.timeout(config.QUERY_TIMEOUT):
            await session.client.query(as_stream(content))
            await stream_with_retry(session, channel)
    except TimeoutError:
        await handle_query_timeout(session, channel)
    except Exception:
        log.exception("Error querying Claude Code agent '%s'", session.name)
        raise RuntimeError(f"Query failed for agent '{session.name}'") from None


# ---------------------------------------------------------------------------
# Agent spawning
# ---------------------------------------------------------------------------


async def reclaim_agent_name(name: str) -> None:
    """If an agent with *name* already exists, kill it silently to free the name."""
    if name not in agents:
        return
    log.info("Reclaiming agent name '%s' — terminating existing session", name)
    session = agents[name]
    await sleep_agent(session, force=True)
    agents.pop(name, None)
    channel = await get_agent_channel(name)
    if channel:
        await send_system(channel, f"Recycled previous **{name}** session for new scheduled run.")


async def spawn_agent(
    name: str,
    cwd: str,
    initial_prompt: str,
    resume: str | None = None,
    agent_type: str = "claude_code",
    command: str = "",
    command_args: str = "",
    packs: list[str] | None = None,
) -> None:
    """Spawn a new agent session and run its initial prompt in the background."""
    from agents._flowcoder import _run_flowcoder
    from agents._state import _utils_mcp_server

    if not os.path.isdir(cwd):
        os.makedirs(cwd, exist_ok=True)
        log.info("Auto-created working directory: %s", cwd)

    normalized = normalize_channel_name(name)
    _channels_mod.bot_creating_channels.add(normalized)
    channel = await ensure_agent_channel(name)

    if agent_type == "flowcoder":
        await send_system(channel, f"Spawning flowcoder agent **{name}** — command `{command}` in `{cwd}`...")
    elif resume:
        await send_system(channel, f"Resuming agent **{name}** (session `{resume[:8]}…`) in `{cwd}`...")
    else:
        await send_system(channel, f"Spawning agent **{name}** in `{cwd}`...")

    if agent_type == "flowcoder":
        session = AgentSession(
            name=name,
            agent_type="flowcoder",
            cwd=cwd,
            session_id=None,
            discord_channel_id=channel.id,
            mcp_servers=None,
            flowcoder_command=command,
            flowcoder_args=command_args,
        )
    else:
        prompt = make_spawned_agent_system_prompt(cwd, packs=packs)
        mcp_servers: dict[str, Any] = {}
        if _utils_mcp_server is not None:
            mcp_servers["utils"] = _utils_mcp_server
        mcp_servers["schedule"] = make_schedule_mcp_server(name, config.SCHEDULES_PATH)

        session = AgentSession(
            name=name,
            cwd=cwd,
            system_prompt=prompt,
            system_prompt_hash=compute_prompt_hash(prompt),
            client=None,
            session_id=resume,
            discord_channel_id=channel.id,
            mcp_servers=mcp_servers,
        )

    agents[name] = session
    channel_to_agent[channel.id] = name
    _channels_mod.bot_creating_channels.discard(normalized)
    log.info("Agent '%s' registered (type=%s, cwd=%s, resume=%s)", name, agent_type, cwd, resume)

    desired_topic = format_channel_topic(cwd, resume, session.system_prompt_hash)
    if channel.topic != desired_topic:
        log.info("Updating topic on #%s: %r -> %r", channel.name, channel.topic, desired_topic)
        await channel.edit(topic=desired_topic)

    if agent_type == "flowcoder":
        asyncio.create_task(_run_flowcoder(session, channel))
        return

    if not initial_prompt:
        await send_system(channel, f"Agent **{name}** is ready (sleeping).")
        return

    asyncio.create_task(run_initial_prompt(session, initial_prompt, channel))


async def send_prompt_to_agent(agent_name: str, prompt: str) -> None:
    """Send a prompt to an existing agent session in the background."""
    session = agents.get(agent_name)
    if session is None:
        log.warning("send_prompt_to_agent: agent '%s' not found", agent_name)
        return

    channel = await get_agent_channel(agent_name)
    if channel is None:
        log.warning("send_prompt_to_agent: no channel for agent '%s'", agent_name)
        return

    ts_prefix = datetime.now(UTC).strftime("[%Y-%m-%d %H:%M:%S UTC] ")
    prompt = ts_prefix + prompt

    asyncio.create_task(run_initial_prompt(session, prompt, channel))


# ---------------------------------------------------------------------------
# Initial prompt / message queue
# ---------------------------------------------------------------------------


async def run_initial_prompt(session: AgentSession, prompt: MessageContent, channel: TextChannel) -> None:
    """Run the initial prompt for a spawned agent."""
    try:
        async with session.query_lock:
            if not is_awake(session):
                try:
                    await wake_agent(session)
                except ConcurrencyLimitError:
                    log.info("Concurrency limit hit for '%s' initial prompt — queuing", session.name)
                    session.message_queue.append((prompt, channel, None))
                    awake = count_awake_agents()
                    await send_system(
                        channel,
                        f"⏳ All {awake} agent slots are busy. Initial prompt queued — will run when a slot opens.",
                    )
                    return
                except Exception:
                    log.exception("Failed to wake agent '%s' for initial prompt", session.name)
                    await send_system(channel, f"Failed to wake agent **{session.name}**.")
                    return

            session.last_activity = datetime.now(UTC)
            drain_stderr(session)
            drain_sdk_buffer(session)

            prompt_text = prompt if isinstance(prompt, str) else str(prompt)
            await send_long(channel, f"*System:* 📝 **Initial prompt:**\n{prompt_text}")

            if session.agent_log:
                session.agent_log.info("PROMPT: %s", content_summary(prompt))
            log.info("INITIAL_PROMPT[%s] running initial prompt: %s", session.name, content_summary(prompt))
            session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))
            try:
                await process_message(session, prompt, channel)
                session.last_activity = datetime.now(UTC)
            except RuntimeError as e:
                log.warning("Handler error for '%s' initial prompt: %s", session.name, e)
                await send_system(channel, f"Error: {e}")
            finally:
                session.activity = ActivityState(phase="idle")

        log.debug("Initial prompt completed for '%s'", session.name)
        await send_system(channel, f"Agent **{session.name}** finished initial task.")

    except Exception:
        log.exception("Error running initial prompt for agent '%s'", session.name)
        await send_system(channel, f"Agent **{session.name}** encountered an error during initial task.")

    await process_message_queue(session)

    try:
        await sleep_agent(session)
    except Exception:
        log.exception("Error sleeping agent '%s' after initial prompt", session.name)


async def process_message_queue(session: AgentSession) -> None:
    """Process any queued messages for an agent after the current query finishes."""
    from agents._state import shutdown_coordinator

    if session.message_queue:
        log.info("QUEUE[%s] processing %d queued messages", session.name, len(session.message_queue))
    while session.message_queue:
        if shutdown_coordinator and shutdown_coordinator.requested:
            log.info("Shutdown requested — not processing further queued messages for '%s'", session.name)
            break
        content, channel, orig_message = session.message_queue.popleft()

        remaining = len(session.message_queue)
        log.debug("Processing queued message for '%s' (%d remaining)", session.name, remaining)
        if session.agent_log:
            session.agent_log.info("QUEUED_MSG: %s", content_summary(content))
        await remove_reaction(orig_message, "📨")
        preview = content_summary(content)
        remaining_str = f" ({remaining} more in queue)" if remaining > 0 else ""
        await send_system(channel, f"Processing queued message{remaining_str}:\n> {preview}")

        async with session.query_lock:
            if not is_awake(session):
                try:
                    await wake_agent(session)
                    await _post_model_warning(session)
                except Exception:
                    log.exception("Failed to wake agent '%s' for queued message", session.name)
                    await add_reaction(orig_message, "❌")
                    await send_system(
                        channel,
                        f"Failed to wake agent **{session.name}** — dropping queued message.",
                    )
                    while session.message_queue:
                        _, ch, dropped_msg = session.message_queue.popleft()
                        await remove_reaction(dropped_msg, "📨")
                        await add_reaction(dropped_msg, "❌")
                        await send_system(
                            ch,
                            f"Failed to wake agent **{session.name}** — dropping queued message.",
                        )
                    return

            _reset_session_activity(session)
            try:
                await process_message(session, content, channel)
                await add_reaction(orig_message, "✅")
            except TimeoutError:
                await add_reaction(orig_message, "⏳")
                await handle_query_timeout(session, channel)
            except RuntimeError as e:
                log.warning(
                    "Runtime error processing queued message for '%s': %s",
                    session.name,
                    e,
                )
                await add_reaction(orig_message, "❌")
                await send_system(channel, str(e))
            except Exception:
                log.exception("Error processing queued message for '%s'", session.name)
                await add_reaction(orig_message, "❌")
                await send_system(
                    channel,
                    f"Error processing queued message for **{session.name}**.",
                )
            finally:
                session.activity = ActivityState(phase="idle")


# ---------------------------------------------------------------------------
# Inter-agent messaging
# ---------------------------------------------------------------------------


async def deliver_inter_agent_message(
    sender_name: str,
    target_session: AgentSession,
    content: str,
) -> str:
    """Deliver a message from one agent to another."""
    from agents._state import bridge_conn

    channel = await get_agent_channel(target_session.name)
    if channel is None:
        return f"No Discord channel found for agent '{target_session.name}'"

    await send_system(
        channel,
        f"📨 **Message from {sender_name}:**\n> {content}",
    )

    ts_prefix = datetime.now(UTC).strftime("[%Y-%m-%d %H:%M:%S UTC] ")
    prompt = ts_prefix + f"[Inter-agent message from {sender_name}] {content}"

    if target_session.query_lock.locked():
        target_session.message_queue.appendleft((prompt, channel, None))
        log.info(
            "Inter-agent message from '%s' to busy agent '%s' — interrupting",
            sender_name,
            target_session.name,
        )
        try:
            if bridge_conn and bridge_conn.is_alive:
                await bridge_conn.send_command("interrupt", name=target_session.name)
            if target_session.client:
                try:
                    await target_session.client.interrupt()
                except Exception:
                    pass
        except Exception:
            log.exception(
                "Failed to interrupt '%s' for inter-agent message (message still queued)",
                target_session.name,
            )
        return f"delivered to busy agent '{target_session.name}' (interrupted, will process next)"
    else:
        asyncio.create_task(_process_inter_agent_prompt(target_session, prompt, channel))
        return f"delivered to agent '{target_session.name}'"


async def _process_inter_agent_prompt(
    session: AgentSession,
    content: str,
    channel: TextChannel,
) -> None:
    """Background task to wake (if needed) and process an inter-agent message."""
    try:
        async with session.query_lock:
            if not is_awake(session):
                try:
                    await wake_agent(session)
                    await _post_model_warning(session)
                except ConcurrencyLimitError:
                    session.message_queue.append((content, channel, None))
                    awake = count_awake_agents()
                    log.info(
                        "Concurrency limit hit for '%s' inter-agent message — queuing",
                        session.name,
                    )
                    await send_system(
                        channel,
                        f"⏳ All {awake} agent slots busy. Inter-agent message queued.",
                    )
                    return
                except Exception:
                    log.exception(
                        "Failed to wake agent '%s' for inter-agent message",
                        session.name,
                    )
                    await send_system(
                        channel,
                        f"Failed to wake agent **{session.name}** for inter-agent message.",
                    )
                    return

            _reset_session_activity(session)
            try:
                await process_message(session, content, channel)
            except TimeoutError:
                await handle_query_timeout(session, channel)
            except RuntimeError as e:
                log.warning(
                    "Runtime error processing inter-agent message for '%s': %s",
                    session.name,
                    e,
                )
                await send_system(channel, str(e))
            except Exception:
                log.exception(
                    "Error processing inter-agent message for '%s'",
                    session.name,
                )
                await send_system(
                    channel,
                    f"Error processing inter-agent message for **{session.name}**.",
                )
            finally:
                session.activity = ActivityState(phase="idle")

        await process_message_queue(session)
    except Exception:
        log.exception(
            "Unhandled error in _process_inter_agent_prompt for '%s'",
            session.name,
        )
