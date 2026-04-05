"""Construct the AgentHub with FrontendRouter and SDK factories.

Called once from agents.init() to create the hub. The hub shares the same
sessions dict as agents.py's `agents` — both reference the same objects.
This allows gradual migration: existing code works alongside hub calls.

The FrontendRouter multiplexes events to all registered frontends. Currently
only DiscordFrontend is registered; future frontends (web, Slack) will be
added via router.add().
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from agenthub import AgentHub
from agenthub.frontend_router import FrontendRouter
from axi import config
from axi.discord_frontend import DiscordFrontend

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from agenthub.types import AgentSession

log = logging.getLogger("axi")


# ---------------------------------------------------------------------------
# SDK factories — build options and clients for the hub
# ---------------------------------------------------------------------------


def _make_agent_options(session: AgentSession, resume_id: str | None) -> Any:
    """Build ClaudeAgentOptions from config and session state."""
    from axi.agents import make_cwd_permission_callback, make_stderr_callback

    return ClaudeAgentOptions(
        model=config.get_model(),
        effort="max",
        thinking={"type": "adaptive"},
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
        disallowed_tools=[],
        extra_args={"debug-to-stderr": None},
        env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "100"},
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
        env = build_engine_env(options)
        log.info(
            "Spawning flowcoder engine for '%s': %s",
            session.name,
            " ".join(cli_args[:6]) + "...",
        )
        await transport.spawn(cli_args, env, session.cwd)
        await transport.subscribe()
        session.transport = transport
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

# Module-level router — accessible for adding more frontends later
router: FrontendRouter | None = None


def create_hub(
    bot: Bot,
    sessions: dict[str, Any],
) -> AgentHub:
    """Create and configure the AgentHub with FrontendRouter.

    Creates a FrontendRouter, registers DiscordFrontend, and uses
    router.as_callbacks() to generate the FrontendCallbacks that AgentHub
    expects. The router is stored module-level so other code can add
    frontends later (e.g. web, Slack).
    """
    global router

    # Create router and register Discord as the first frontend
    router = FrontendRouter()
    discord_fe = DiscordFrontend(bot)
    router.add(discord_fe)

    # Generate backward-compatible callbacks from the router
    callbacks = router.as_callbacks()

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
