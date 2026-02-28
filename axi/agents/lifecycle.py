# pyright: reportPrivateUsage=false
"""Session lifecycle: wake, sleep, reset, reconstruct."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import UTC, datetime
from typing import Any

import discord
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from discord import TextChannel

from axi import channels as _channels_mod
from axi import config
from axi.agents.discord_helpers import add_reaction, send_system
from axi.agents.permissions import make_cwd_permission_callback
from axi.agents.sdk import make_stderr_callback
from axi.agents.state import _wake_lock, agents, channel_to_agent
from axi.axi_types import ActivityState, AgentSession, ConcurrencyLimitError, MessageContent
from axi.bridge import BridgeTransport
from axi.channels import (
    format_channel_topic,
    normalize_channel_name,
)
from axi.channels import (
    parse_channel_topic as _parse_channel_topic,
)
from axi.prompts import (
    compute_prompt_hash,
    make_spawned_agent_system_prompt,
    post_system_prompt_to_channel,
)
from axi.schedule_tools import make_schedule_mcp_server

log = logging.getLogger("axi")


# ---------------------------------------------------------------------------
# Handler-replacement helpers
# ---------------------------------------------------------------------------


def is_awake(session: AgentSession) -> bool:
    """Check if agent is ready to process messages."""
    if session.agent_type == "flowcoder":
        return session.flowcoder_process is not None
    return session.client is not None


def is_processing(session: AgentSession) -> bool:
    """Check if agent has active work."""
    if session.agent_type == "flowcoder":
        return session.flowcoder_process is not None and session.flowcoder_process.is_running
    return session.query_lock.locked()


def _reset_session_activity(session: AgentSession) -> None:  # pyright: ignore[reportUnusedFunction]  # called from messaging
    """Reset idle tracking and activity state for the start of a new query."""
    session.last_activity = datetime.now(UTC)
    session.last_idle_notified = None
    session.idle_reminder_count = 0
    session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Session lifecycle internals
# ---------------------------------------------------------------------------


def _get_subprocess_pid(client: ClaudeSDKClient) -> int | None:
    """Extract the PID of the underlying CLI subprocess from a ClaudeSDKClient."""
    try:
        transport = getattr(client, "_transport", None) or getattr(getattr(client, "_query", None), "transport", None)
        if transport is None:
            return None
        proc = getattr(transport, "_process", None)
        if proc is None:
            return None
        return proc.pid  # type: ignore[no-any-return]
    except Exception:
        return None


def _ensure_process_dead(pid: int | None, label: str) -> None:
    """Send SIGTERM to *pid* if it is still alive."""
    if pid is None:
        return
    try:
        os.kill(pid, 0)
    except OSError:
        return
    log.warning("Subprocess %d for '%s' survived disconnect — sending SIGTERM (SDK bug workaround)", pid, label)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


async def _create_transport(session: AgentSession, reconnecting: bool = False):  # pyright: ignore[reportUnusedFunction]  # called from bridge_conn
    """Create a transport for Claude Code agent (bridge or direct)."""
    from axi.agents.state import bridge_conn

    if bridge_conn and bridge_conn.is_alive:
        transport = BridgeTransport(
            session.name,
            bridge_conn,
            reconnecting=reconnecting,
            stderr_callback=make_stderr_callback(session),
        )
        await transport.connect()
        return transport
    else:
        return None


async def _disconnect_client(client: ClaudeSDKClient, label: str) -> None:
    """Disconnect a ClaudeSDKClient and ensure its subprocess is terminated."""
    transport = getattr(client, "_transport", None)
    if isinstance(transport, BridgeTransport):
        try:
            await asyncio.wait_for(transport.close(), timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            log.warning("'%s' bridge transport close timed out", label)
        except Exception:
            log.exception("'%s' error closing bridge transport", label)
        return

    pid = _get_subprocess_pid(client)
    try:
        await asyncio.wait_for(client.__aexit__(None, None, None), timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        log.warning("'%s' shutdown timed out or was cancelled", label)
    except RuntimeError as e:
        if "cancel scope" in str(e):
            log.debug("'%s' cross-task cleanup (expected): %s", label, e)
        else:
            raise
    _ensure_process_dead(pid, label)


# ---------------------------------------------------------------------------
# Concurrency management
# ---------------------------------------------------------------------------


def count_awake_agents() -> int:
    """Count agents that are currently awake."""
    return sum(
        1
        for s in agents.values()
        if s.client is not None or (s.flowcoder_process is not None and s.flowcoder_process.is_running)
    )


async def _evict_idle_agent(exclude: str | None = None) -> bool:
    """Sleep the most idle non-busy awake agent to free a slot."""
    candidates: list[tuple[float, str, AgentSession]] = []
    for name, s in agents.items():
        if name == exclude:
            continue
        if s.client is None:
            continue
        if s.query_lock.locked():
            continue
        if s.bridge_busy:
            continue
        idle_duration = (datetime.now(UTC) - s.last_activity).total_seconds()
        candidates.append((idle_duration, name, s))

    if not candidates:
        return False

    log.debug("Eviction candidates: %s", [(n, f"{s:.0f}s") for s, n, _ in candidates])
    candidates.sort(reverse=True, key=lambda x: x[0])
    idle_secs, evict_name, evict_session = candidates[0]
    log.info("Evicting idle agent '%s' (idle %.0fs) to free concurrency slot", evict_name, idle_secs)
    try:
        await sleep_agent(evict_session)
    except Exception:
        log.exception("Error evicting agent '%s'", evict_name)
        return False
    return True


async def _ensure_awake_slot(requesting_agent: str) -> bool:
    """Ensure there is a free awake-agent slot, evicting idle agents if needed."""
    while count_awake_agents() >= config.MAX_AWAKE_AGENTS:
        log.debug(
            "Awake slots full (%d/%d), attempting eviction for '%s'",
            count_awake_agents(),
            config.MAX_AWAKE_AGENTS,
            requesting_agent,
        )
        evicted = await _evict_idle_agent(exclude=requesting_agent)
        if not evicted:
            log.warning("Cannot free awake slot for '%s' — all %d slots busy", requesting_agent, config.MAX_AWAKE_AGENTS)
            return False
    return True


# ---------------------------------------------------------------------------
# Sleep / wake
# ---------------------------------------------------------------------------


async def sleep_agent(session: AgentSession, *, force: bool = False) -> None:
    """Shut down an agent. Keep the AgentSession in the agents dict.

    If force=False (default), skips sleeping if the agent's query_lock is held
    (i.e. the agent is actively processing a query).
    """
    if not force and session.query_lock.locked():
        log.debug("Skipping sleep for '%s' — query_lock is held", session.name)
        return

    if session.flowcoder_process:
        from axi.flowcoder import BridgeFlowcoderProcess

        proc = session.flowcoder_process
        if isinstance(proc, BridgeFlowcoderProcess) and session.query_lock.locked():
            await proc.detach()
        else:
            await proc.stop()
        session.flowcoder_process = None

    if session.agent_type == "flowcoder":
        return

    if session.client is None:
        return

    log.info("Sleeping agent '%s'", session.name)
    if session.agent_log:
        session.agent_log.info("SESSION_SLEEP")
    session.bridge_busy = False
    await _disconnect_client(session.client, session.name)
    session.client = None
    log.info("Agent '%s' is now sleeping", session.name)


def _make_agent_options(session: AgentSession, resume_id: str | None = None) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a session."""
    return ClaudeAgentOptions(
        model=config.get_model(),
        effort="high",
        thinking={"type": "enabled", "budget_tokens": 128000},
        setting_sources=["local"],
        permission_mode="plan" if session.plan_mode else "default",
        can_use_tool=make_cwd_permission_callback(session.cwd, session),
        cwd=session.cwd,
        system_prompt=session.system_prompt,
        include_partial_messages=True,
        stderr=make_stderr_callback(session),
        resume=resume_id,
        sandbox={"enabled": True, "autoAllowBashIfSandboxed": True},
        mcp_servers=session.mcp_servers or {},
        disallowed_tools=["Task"],
    )


async def wake_agent(session: AgentSession) -> None:
    """Wake a sleeping agent. Enforces concurrency limit. Posts system prompt on first wake."""
    from axi.agents.state import _bot

    assert _bot is not None

    if is_awake(session):
        return

    async with _wake_lock:
        if is_awake(session):
            return

        log.debug(
            "Wake lock acquired for '%s', awake_count=%d/%d",
            session.name,
            count_awake_agents(),
            config.MAX_AWAKE_AGENTS,
        )

        slot_available = await _ensure_awake_slot(session.name)
        if not slot_available:
            raise ConcurrencyLimitError(
                f"Cannot wake agent '{session.name}': all {config.MAX_AWAKE_AGENTS} awake slots are busy. "
                f"Message will be queued and processed when a slot opens."
            )

        log.debug("Awake slot secured for '%s'", session.name)
        log.info("Waking agent '%s' (session_id=%s)", session.name, session.session_id)

        resume_id = session.session_id
        options = _make_agent_options(session, resume_id)

        log.debug("Waking '%s' (resume=%s)", session.name, resume_id)
        try:
            client = ClaudeSDKClient(options=options)
            await client.__aenter__()
            session.client = client
            log.info("Agent '%s' is now awake (resumed=%s)", session.name, resume_id)
            if session.agent_log:
                session.agent_log.info("SESSION_WAKE (resumed=%s)", bool(resume_id))
        except Exception:
            log.warning("Failed to resume agent '%s' with session_id=%s, retrying fresh", session.name, resume_id)
            options = _make_agent_options(session, resume_id=None)
            client = ClaudeSDKClient(options=options)
            await client.__aenter__()
            session.client = client
            session.session_id = None
            if session.name == config.MASTER_AGENT_NAME:
                try:
                    os.remove(config.MASTER_SESSION_PATH)
                except OSError:
                    pass
            log.warning("Agent '%s' woke with fresh session (previous context lost)", session.name)
            if session.agent_log:
                session.agent_log.info("SESSION_WAKE (resumed=False, fresh after resume failure)")

        prompt_changed = False
        if resume_id and session.system_prompt is not None:
            current_hash = compute_prompt_hash(session.system_prompt)
            if session.system_prompt_hash is not None and current_hash != session.system_prompt_hash:
                prompt_changed = True
                log.info(
                    "System prompt changed for '%s' (old=%s, new=%s)",
                    session.name, session.system_prompt_hash, current_hash,
                )
            session.system_prompt_hash = current_hash

        if not session.system_prompt_posted and session.discord_channel_id:
            session.system_prompt_posted = True
            channel = _bot.get_channel(session.discord_channel_id)
            if channel and isinstance(channel, TextChannel):
                try:
                    await post_system_prompt_to_channel(
                        channel,
                        session.system_prompt,
                        is_resume=bool(resume_id),
                        prompt_changed=prompt_changed,
                        session_id=session.session_id or resume_id,
                    )
                except Exception:
                    log.warning(
                        "Failed to post system prompt to Discord for '%s'",
                        session.name,
                        exc_info=True,
                    )

        await _post_model_warning(session)


async def wake_or_queue(
    session: AgentSession,
    content: MessageContent,
    channel: TextChannel,
    orig_message: discord.Message | None,
) -> bool:
    """Try to wake agent, return True if successful, False if queued."""
    try:
        await wake_agent(session)
        return True
    except ConcurrencyLimitError:
        session.message_queue.append((content, channel, orig_message))
        position = len(session.message_queue)
        awake = count_awake_agents()
        log.debug("Concurrency limit hit for '%s', queuing message (position %d)", session.name, position)
        await add_reaction(orig_message, "📨")
        await send_system(channel, f"⏳ All {awake} agent slots busy. Message queued (position {position}).")
        return False
    except Exception:
        log.exception("Failed to wake agent '%s'", session.name)
        await add_reaction(orig_message, "❌")
        await send_system(
            channel, f"Failed to wake agent **{session.name}**. Try `/kill-agent {session.name}` and respawn."
        )
        return False


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


async def end_session(name: str) -> None:
    """End a named Claude session and remove it from the registry."""
    session = agents.get(name)
    if session is None:
        return
    if session.flowcoder_process:
        await session.flowcoder_process.stop()
        session.flowcoder_process = None
    if session.client is not None:
        await _disconnect_client(session.client, name)
        session.client = None
    session.close_log()
    agents.pop(name, None)
    log.info("Session '%s' ended", name)


async def _rebuild_session(
    name: str, *, cwd: str | None = None, session_id: str | None = None
) -> AgentSession:
    """End an existing session and create a fresh sleeping AgentSession.

    Preserves system prompt, channel mapping, and MCP servers from the old session.
    """
    session = agents.get(name)
    old_cwd = session.cwd if session else config.DEFAULT_CWD
    old_channel_id = session.discord_channel_id if session else None
    old_mcp = getattr(session, "mcp_servers", None)
    resolved_cwd = cwd or old_cwd
    prompt = session.system_prompt if session and session.system_prompt else make_spawned_agent_system_prompt(resolved_cwd)
    prompt_hash = session.system_prompt_hash if session and session.system_prompt_hash else compute_prompt_hash(prompt)
    await end_session(name)
    new_session = AgentSession(
        name=name,
        cwd=resolved_cwd,
        system_prompt=prompt,
        system_prompt_hash=prompt_hash,
        client=None,
        session_id=session_id,
        discord_channel_id=old_channel_id,
        mcp_servers=old_mcp,
    )
    agents[name] = new_session
    return new_session


async def reset_session(name: str, cwd: str | None = None) -> AgentSession:
    """Reset a named session. Preserves system prompt, channel mapping, and MCP servers."""
    new_session = await _rebuild_session(name, cwd=cwd)
    log.info("Session '%s' reset (sleeping, cwd=%s)", name, new_session.cwd)
    return new_session


def get_master_session() -> AgentSession | None:
    """Get the axi-master session."""
    return agents.get(config.MASTER_AGENT_NAME)


async def reconstruct_agents_from_channels() -> int:
    """Reconstruct sleeping AgentSession entries from existing Discord channels."""
    from axi.agents.state import _utils_mcp_server

    reconstructed = 0
    if not _channels_mod.active_category:
        return reconstructed

    for cat in [_channels_mod.active_category]:
        for ch in cat.text_channels:
            agent_name = ch.name

            if agent_name == normalize_channel_name(config.MASTER_AGENT_NAME):
                channel_to_agent[ch.id] = config.MASTER_AGENT_NAME
                continue

            if agent_name in agents:
                channel_to_agent[ch.id] = agent_name
                continue

            cwd, session_id, old_prompt_hash = _parse_channel_topic(ch.topic)
            if cwd is None:
                log.debug("No cwd in topic for channel #%s, skipping", agent_name)
                continue

            prompt = make_spawned_agent_system_prompt(cwd)
            mcp_servers: dict[str, Any] = {}
            if _utils_mcp_server is not None:
                mcp_servers["utils"] = _utils_mcp_server
            mcp_servers["schedule"] = make_schedule_mcp_server(agent_name, config.SCHEDULES_PATH)

            session = AgentSession(
                name=agent_name,
                client=None,
                cwd=cwd,
                system_prompt=prompt,
                system_prompt_hash=old_prompt_hash,
                session_id=session_id,
                discord_channel_id=ch.id,
                mcp_servers=mcp_servers,
            )
            agents[agent_name] = session
            channel_to_agent[ch.id] = agent_name
            reconstructed += 1
            log.info(
                "Reconstructed agent '%s' from #%s (category=%s, session_id=%s, prompt_hash=%s)",
                agent_name,
                ch.name,
                cat.name,
                session_id,
                old_prompt_hash,
            )

    log.info("Reconstructed %d agent(s) from channels", reconstructed)
    return reconstructed


# ---------------------------------------------------------------------------
# Session ID persistence
# ---------------------------------------------------------------------------


async def _set_session_id(session: AgentSession, msg_or_sid: Any, channel: TextChannel | None = None) -> None:  # pyright: ignore[reportUnusedFunction]  # called from streaming
    """Update session's session_id and persist it (topic or file)."""
    from axi.agents.state import _bot

    assert _bot is not None
    sid: str | None = msg_or_sid if isinstance(msg_or_sid, str) else getattr(msg_or_sid, "session_id", None)
    if sid and sid != session.session_id:
        session.session_id = sid
        if session.name == config.MASTER_AGENT_NAME:
            try:
                data = {"session_id": sid}
                if session.system_prompt_hash:
                    data["prompt_hash"] = session.system_prompt_hash
                with open(config.MASTER_SESSION_PATH, "w") as f:
                    json.dump(data, f)
                log.info("Saved master session data to %s", config.MASTER_SESSION_PATH)
            except OSError:
                log.warning("Failed to save master session data", exc_info=True)
        elif session.discord_channel_id:
            ch = channel or _bot.get_channel(session.discord_channel_id)
            if ch and isinstance(ch, TextChannel):
                desired_topic = format_channel_topic(session.cwd, sid, session.system_prompt_hash)
                if ch.topic != desired_topic:
                    log.info("Updating topic on #%s: %r -> %r", ch.name, ch.topic, desired_topic)
                    await ch.edit(topic=desired_topic)
    else:
        session.session_id = sid


# ---------------------------------------------------------------------------
# Model warning
# ---------------------------------------------------------------------------


async def _post_model_warning(session: AgentSession) -> None:
    """Post a warning to Discord if the agent is running on a non-opus model."""
    from axi.agents.state import _bot

    assert _bot is not None
    model = config.get_model()
    if model == "opus" or not session.discord_channel_id:
        return
    channel = _bot.get_channel(session.discord_channel_id)
    if channel and isinstance(channel, TextChannel):
        try:
            await channel.send(f"⚠️ Running on **{model}** — switch to opus with `/model opus` for best results.")
        except Exception:
            log.warning("Failed to post model warning for '%s'", session.name, exc_info=True)
