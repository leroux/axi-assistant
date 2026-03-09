"""Hot restart — bridge connection and agent reconnection.

Handles reconnecting to agents that survived a bot restart via the
procmux bridge. Each agent's CLI process keeps running; we reconnect
by subscribing to their stdout/stderr streams and replaying buffered output.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opentelemetry import context as otel_context
from opentelemetry import trace

if TYPE_CHECKING:
    from agenthub.hub import AgentHub
    from agenthub.types import AgentSession

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)


async def connect_procmux(hub: AgentHub, socket_path: str) -> None:
    """Connect to the procmux bridge and schedule reconnections for running agents.

    Sets hub.process_conn on success. On failure, logs a warning and
    agents will use direct subprocess mode.
    """
    from agenthub.procmux_wire import ProcmuxProcessConnection

    # Lazy import — procmux may not be installed in all environments
    try:
        from procmux import ensure_running as ensure_bridge
    except ImportError:
        log.warning("procmux not available — agents will use direct subprocess mode")
        return

    with _tracer.start_as_current_span("connect_procmux") as span:
        try:
            conn = await ensure_bridge(socket_path, timeout=10.0)
            hub.process_conn = ProcmuxProcessConnection(conn)
            hub.raw_procmux_conn = conn
            log.info("Bridge connection established")
            span.set_attribute("procmux.connected", True)
        except Exception:
            log.exception(
                "Failed to connect to bridge — agents will use direct subprocess mode"
            )
            hub.process_conn = None
            hub.raw_procmux_conn = None
            span.set_attribute("procmux.connected", False)
            return

        try:
            result = await conn.send_command("list")
            bridge_agents = result.agents or {}
            log.info(
                "Bridge reports %d agent(s): %s",
                len(bridge_agents),
                list(bridge_agents.keys()),
            )
            span.set_attribute("procmux.agents_found", len(bridge_agents))
        except Exception:
            log.exception("Failed to list bridge agents")
            return

        if not bridge_agents:
            return

        for agent_name, info in bridge_agents.items():
            session = hub.sessions.get(agent_name)
            if session is None:
                log.warning(
                    "Bridge has agent '%s' but no matching session — killing",
                    agent_name,
                )
                try:
                    await conn.send_command("kill", name=agent_name)
                except Exception:
                    log.exception(
                        "Failed to kill orphan bridge agent '%s'", agent_name
                    )
                continue

            session.reconnecting = True
            hub.tasks.fire_and_forget(
                reconnect_single(hub, session, info)
            )


async def reconnect_single(
    hub: AgentHub,
    session: AgentSession,
    bridge_info: dict[str, Any],
) -> None:
    """Reconnect a single agent to the bridge and drain any buffered output.

    Creates a BridgeTransport, initializes the SDK client, THEN subscribes
    to the agent's streams. This order is critical: subscribe replays buffered
    messages into the transport queue, which would corrupt the SDK's initialize
    handshake if they arrive before the control_response.
    """
    from claudewire import BridgeTransport

    span = _tracer.start_span(
        "reconnect_single",
        attributes={
            "agent.name": session.name,
            "procmux.buffered_msgs": bridge_info.get("buffered_msgs", 0),
        },
    )
    ctx_token = otel_context.attach(trace.set_span_in_context(span))
    try:
        async with session.query_lock:
            if hub.process_conn is None:
                log.warning(
                    "Bridge connection lost during reconnect of '%s'",
                    session.name,
                )
                session.reconnecting = False
                return

            transport = BridgeTransport(
                session.name,
                hub.process_conn,
                reconnecting=True,
            )
            await transport.connect()

            # Create and initialize SDK client FIRST — the queue is empty so
            # the initialize handshake completes cleanly.
            options = hub.make_agent_options(session, session.session_id)

            from claude_agent_sdk import ClaudeSDKClient

            client = ClaudeSDKClient(options=options, transport=transport)  # pyright: ignore[reportArgumentType]
            await client.__aenter__()

            # NOW subscribe — replayed messages flow into the queue after init.
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

            # Handle exited processes — clean up and leave agent sleeping
            if cli_status == "exited":
                log.info("Agent '%s' CLI exited while we were down — cleaning up", session.name)
                await hub.disconnect_client(client, session.name)
                session.transport = None
                session.reconnecting = False
                if session.agent_log:
                    session.agent_log.info("SESSION_RECONNECT aborted — CLI exited")
                return

            session.client = client
            hub.scheduler.restore_slot(session.name)
            session.last_activity = datetime.now(UTC)

            if session.agent_log:
                session.agent_log.info(
                    "SESSION_RECONNECT via bridge (replayed=%d, idle=%s)",
                    replayed,
                    cli_idle,
                )

            session.reconnecting = False

            was_mid_task = cli_status == "running" and not cli_idle
            await hub.callbacks.on_reconnect(session.name, was_mid_task)

            if was_mid_task:
                session.bridge_busy = True
                log.info(
                    "Agent '%s' reconnected mid-task (idle=False, replayed=%d)",
                    session.name,
                    replayed,
                )
            elif cli_status == "running":
                log.info(
                    "Agent '%s' reconnected idle (between turns)", session.name
                )

            log.info("Reconnect complete for '%s'", session.name)

    except Exception:
        log.exception("Failed to reconnect agent '%s'", session.name)
        span.set_status(trace.StatusCode.ERROR, "reconnect failed")
        session.reconnecting = False
    finally:
        otel_context.detach(ctx_token)
        span.end()
