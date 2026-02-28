# pyright: reportPrivateUsage=false
"""Bridge connection and reconnection logic."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

import config
from agents.discord_helpers import send_system
from agents.flowcoder import _stream_flowcoder_to_channel
from agents.lifecycle import _create_transport
from agents.messaging import process_message_queue
from agents.permissions import make_cwd_permission_callback
from agents.sdk import make_stderr_callback
from agents.state import agents
from agents.streaming import stream_response_to_channel
from axi_types import ActivityState, AgentSession
from bridge import ensure_bridge
from channels import get_agent_channel

if TYPE_CHECKING:
    from flowcoder import BridgeFlowcoderProcess

if config.FLOWCODER_ENABLED:
    from flowcoder import BridgeFlowcoderProcess

log = logging.getLogger("axi")


async def connect_bridge() -> None:
    """Connect to the agent bridge and schedule reconnections for running agents."""
    from agents import state

    try:
        state.bridge_conn = await ensure_bridge(config.BRIDGE_SOCKET_PATH, timeout=10.0)
        log.info("Bridge connection established")
    except Exception:
        log.exception("Failed to connect to bridge — agents will use direct subprocess mode")
        state.bridge_conn = None
        return

    bridge_conn = state.bridge_conn

    try:
        result = await bridge_conn.send_command("list")
        bridge_agents = result.agents or {}
        log.info("Bridge reports %d agent(s): %s", len(bridge_agents), list(bridge_agents.keys()))
    except Exception:
        log.exception("Failed to list bridge agents")
        return

    if not bridge_agents:
        return

    for agent_name, info in bridge_agents.items():
        if agent_name.endswith(":flowcoder"):
            if not config.FLOWCODER_ENABLED:
                log.info("Flowcoder disabled — killing bridge agent '%s'", agent_name)
                try:
                    await bridge_conn.send_command("kill", name=agent_name)
                except Exception:
                    log.exception("Failed to kill bridge flowcoder '%s' (disabled)", agent_name)
                continue
            base_name = agent_name.removesuffix(":flowcoder")
            session = agents.get(base_name)
            if session is None:
                log.warning("Bridge has flowcoder '%s' but no matching session — killing", agent_name)
                try:
                    await bridge_conn.send_command("kill", name=agent_name)
                except Exception:
                    log.exception("Failed to kill orphan bridge flowcoder '%s'", agent_name)
                continue
            if info.get("status") == "exited":
                log.info("Bridge flowcoder '%s' already exited — cleaning up", agent_name)
                try:
                    await bridge_conn.send_command("kill", name=agent_name)
                except Exception:
                    pass
                continue
            session.reconnecting = True
            asyncio.create_task(_reconnect_flowcoder(session, agent_name, info))
            continue

        session = agents.get(agent_name)
        if session is None:
            log.warning("Bridge has agent '%s' but no matching session — killing", agent_name)
            try:
                await bridge_conn.send_command("kill", name=agent_name)
            except Exception:
                log.exception("Failed to kill orphan bridge agent '%s'", agent_name)
            continue

        status = info.get("status", "unknown")
        buffered = info.get("buffered_msgs", 0)
        log.info(
            "Reconnecting agent '%s' (status=%s, buffered=%d)",
            agent_name,
            status,
            buffered,
        )

        session.reconnecting = True
        asyncio.create_task(_reconnect_and_drain(session, info))


async def _reconnect_and_drain(session: AgentSession, bridge_info: dict[str, Any]) -> None:
    """Reconnect a single agent to the bridge and drain any buffered output."""
    from agents.state import bridge_conn

    try:
        async with session.query_lock:
            if bridge_conn is None or not bridge_conn.is_alive:
                log.warning("Bridge connection lost during reconnect of '%s'", session.name)
                session.reconnecting = False
                return

            transport = await _create_transport(session, reconnecting=True)
            assert transport is not None

            sub_result = await transport.subscribe()
            replayed = sub_result.replayed or 0
            cli_status = sub_result.status or "unknown"
            cli_idle = sub_result.idle if sub_result.idle is not None else True
            log.info(
                "Subscribed to '%s' (replayed=%d, status=%s, idle=%s)",
                session.name,
                replayed,
                cli_status,
                cli_idle,
            )

            options = ClaudeAgentOptions(
                can_use_tool=make_cwd_permission_callback(session.cwd, session),
                mcp_servers=session.mcp_servers or {},
                permission_mode="plan" if session.plan_mode else "default",
                cwd=session.cwd,
                include_partial_messages=True,
                stderr=make_stderr_callback(session),
                disallowed_tools=["Task"],
            )

            client = ClaudeSDKClient(options=options, transport=transport)  # pyright: ignore[reportArgumentType]
            await client.__aenter__()
            session.client = client
            session.last_activity = datetime.now(UTC)

            if session.agent_log:
                session.agent_log.info(
                    "SESSION_RECONNECT via bridge (replayed=%d, idle=%s)",
                    replayed,
                    cli_idle,
                )

            if cli_status == "exited":
                log.info("Agent '%s' CLI exited while we were down", session.name)
                session.reconnecting = False

            session.reconnecting = False

            if cli_status == "running" and not cli_idle:
                session.bridge_busy = True
                channel = await get_agent_channel(session.name)
                if replayed > 0 and channel:
                    log.info("RECONNECT_DRAIN[%s] draining buffered output (replayed=%d)", session.name, replayed)
                    await send_system(channel, "*(reconnected after restart — resuming output)*")
                    try:
                        async with asyncio.timeout(config.QUERY_TIMEOUT):
                            await stream_response_to_channel(session, channel)
                    except TimeoutError:
                        log.warning("Drain timeout for '%s' — continuing", session.name)
                    except Exception:
                        log.exception("Error draining buffered output for '%s'", session.name)
                    session.bridge_busy = False
                    session.last_activity = datetime.now(UTC)
                elif channel:
                    await send_system(channel, "*(reconnected after restart — task still running)*")
                log.info(
                    "Agent '%s' reconnected mid-task (idle=False, replayed=%d, bridge_busy=%s)",
                    session.name,
                    replayed,
                    session.bridge_busy,
                )
            elif cli_status == "running":
                channel = await get_agent_channel(session.name)
                if channel:
                    await send_system(channel, "*(reconnected after restart)*")
                log.info("Agent '%s' reconnected idle (between turns)", session.name)

            log.info("Reconnect complete for '%s'", session.name)

    except Exception:
        log.exception("Failed to reconnect agent '%s'", session.name)
        session.reconnecting = False

    await process_message_queue(session)


async def _reconnect_flowcoder(session: AgentSession, bridge_name: str, bridge_info: dict[str, Any]) -> None:
    """Reconnect to a flowcoder engine that survived bot.py restart."""
    from agents.state import bridge_conn

    log.info("Reconnecting flowcoder '%s' for session '%s'", bridge_name, session.name)
    try:
        async with session.query_lock:
            if bridge_conn is None or not bridge_conn.is_alive:
                log.warning("Bridge connection lost during flowcoder reconnect of '%s'", bridge_name)
                session.reconnecting = False
                return

            proc = BridgeFlowcoderProcess(
                bridge_name=bridge_name,
                conn=bridge_conn,
                command=session.flowcoder_command,
                args=session.flowcoder_args,
                cwd=session.cwd,
            )
            sub_result = await proc.subscribe()
            session.flowcoder_process = proc
            session.reconnecting = False

            replayed = sub_result.replayed or 0
            log.info(
                "Flowcoder '%s' subscribed (replayed=%d, status=%s)",
                bridge_name,
                replayed,
                sub_result.status,
            )

            channel = await get_agent_channel(session.name)

            await proc.send({"type": "status_request"})

            if channel:
                if replayed > 0:
                    await send_system(channel, "*(reconnected — resuming flowchart)*")
                await _stream_flowcoder_to_channel(session, channel)

            await proc.stop()
            session.flowcoder_process = None
            session.activity = ActivityState(phase="idle")
            log.info("Flowcoder '%s' reconnect complete — cleaned up", bridge_name)

    except Exception:
        log.exception("Failed to reconnect flowcoder '%s'", bridge_name)
        session.reconnecting = False

    await process_message_queue(session)
