"""Construct the AgentHub with Discord-specific callbacks and SDK factories.

Called once from agents.init() to create the hub. The hub shares the same
sessions dict as agents.py's `agents` — both reference the same objects.
This allows gradual migration: existing code works alongside hub calls.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from agenthub import AgentHub, FrontendCallbacks
from axi import config

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from agenthub.types import AgentSession

log = logging.getLogger("axi")


# ---------------------------------------------------------------------------
# Frontend callbacks — map hub events to Discord operations
# ---------------------------------------------------------------------------


def _build_callbacks(bot: Bot) -> FrontendCallbacks:
    """Build FrontendCallbacks that use the Discord bot for notifications."""
    from axi.channels import get_agent_channel, get_master_channel

    async def post_message(agent_name: str, text: str) -> None:
        channel = await get_agent_channel(agent_name)
        if channel:
            from axi.agents import send_long

            await send_long(channel, text)

    async def post_system(agent_name: str, text: str) -> None:
        channel = await get_agent_channel(agent_name)
        if channel:
            from axi.agents import send_system

            await send_system(channel, text)

    async def on_wake(agent_name: str) -> None:
        log.debug("Hub: agent '%s' woke", agent_name)

    async def on_sleep(agent_name: str) -> None:
        log.debug("Hub: agent '%s' slept", agent_name)

    async def on_session_id(agent_name: str, session_id: str) -> None:
        log.debug("Hub: agent '%s' session_id=%s", agent_name, session_id)

    async def get_channel(agent_name: str) -> Any:
        return await get_agent_channel(agent_name)

    async def on_spawn(session: Any) -> None:
        log.info("Hub: agent '%s' spawned", session.name)

    async def on_kill(agent_name: str, session_id: str | None) -> None:
        log.info("Hub: agent '%s' killed", agent_name)

    async def broadcast(text: str) -> None:
        master_ch = await get_master_channel()
        if master_ch:
            await master_ch.send(text)

    async def schedule_rate_limit_expiry(seconds: float) -> None:
        pass  # Handled by existing rate limit code for now

    async def on_idle_reminder(agent_name: str, idle_minutes: float) -> None:
        pass  # Handled by existing idle check code for now

    async def on_reconnect(agent_name: str, was_mid_task: bool) -> None:
        channel = await get_agent_channel(agent_name)
        if channel:
            if was_mid_task:
                await channel.send("*(reconnected after restart — resuming output)*")
            else:
                await channel.send("*(reconnected after restart)*")

    async def close_app() -> None:
        from axi.tracing import shutdown_tracing

        shutdown_tracing()
        await bot.close()

    async def kill_process() -> None:
        from agenthub.shutdown import kill_supervisor

        kill_supervisor()

    async def send_goodbye() -> None:
        master_ch = await get_master_channel()
        if master_ch:
            await master_ch.send("*System:* Shutting down — see you soon!")

    return FrontendCallbacks(
        post_message=post_message,
        post_system=post_system,
        on_wake=on_wake,
        on_sleep=on_sleep,
        on_session_id=on_session_id,
        get_channel=get_channel,
        on_spawn=on_spawn,
        on_kill=on_kill,
        broadcast=broadcast,
        schedule_rate_limit_expiry=schedule_rate_limit_expiry,
        on_idle_reminder=on_idle_reminder,
        on_reconnect=on_reconnect,
        close_app=close_app,
        kill_process=kill_process,
        send_goodbye=send_goodbye,
    )


# ---------------------------------------------------------------------------
# SDK factories — build options and clients for the hub
# ---------------------------------------------------------------------------


def _make_agent_options(session: AgentSession, resume_id: str | None) -> Any:
    """Build ClaudeAgentOptions from config and session state."""
    from axi.agents import make_cwd_permission_callback, make_stderr_callback

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


async def _create_client(session: AgentSession, options: Any) -> Any:
    """Create a ClaudeSDKClient for a session."""
    from axi.agents import create_transport

    if session.agent_type == "flowcoder" and config.FLOWCODER_ENABLED:
        from axi.flowcoder import build_engine_cli_args, build_engine_env

        transport = await create_transport(session)
        if not transport:
            raise RuntimeError(
                f"Procmux required for flowcoder agent '{session.name}'"
            )
        cli_args = build_engine_cli_args(options)
        env = build_engine_env()
        log.info(
            "Spawning flowcoder engine for '%s': %s",
            session.name,
            " ".join(cli_args[:6]) + "...",
        )
        await transport.spawn(cli_args, env, session.cwd)
        await transport.subscribe()
        client = ClaudeSDKClient(options=options, transport=transport)  # pyright: ignore[reportArgumentType]
        await client.__aenter__()
        return client

    client = ClaudeSDKClient(options=options)
    await client.__aenter__()
    return client


async def _disconnect_client(client: Any, name: str) -> None:
    """Disconnect an SDK client."""
    from claudewire.session import disconnect_client

    await disconnect_client(client, name)


# ---------------------------------------------------------------------------
# Hub construction
# ---------------------------------------------------------------------------


def create_hub(
    bot: Bot,
    sessions: dict[str, Any],
) -> AgentHub:
    """Create and configure the AgentHub.

    The hub shares the same sessions dict as agents.py — both point to the
    same session objects. This allows gradual migration: existing code that
    accesses `agents["name"]` works alongside hub.sessions["name"].
    """
    callbacks = _build_callbacks(bot)

    hub = AgentHub(
        max_awake=config.MAX_AWAKE_AGENTS,
        protected={config.MASTER_AGENT_NAME},
        callbacks=callbacks,
        make_agent_options=_make_agent_options,
        create_client=_create_client,
        disconnect_client=_disconnect_client,
        query_timeout=config.QUERY_TIMEOUT,
        max_retries=config.API_ERROR_MAX_RETRIES,
        retry_base_delay=config.API_ERROR_BASE_DELAY,
        usage_history_path=config.USAGE_HISTORY_PATH,
        rate_limit_history_path=config.RATE_LIMIT_HISTORY_PATH,
    )

    # Share the same sessions dict — gradual migration
    hub.sessions = sessions  # type: ignore[assignment]

    return hub
