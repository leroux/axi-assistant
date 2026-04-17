"""Construct the rewritten AgentHub for Axi.

Called once from agents.init() to create the hub. The hub shares the same
sessions dict as agents.py's `agents` — both reference the same objects.
This allows gradual migration while Axi keeps its own Discord-specific
lifecycle and queue handling on top of the standalone runtime core.

The FrontendRouter multiplexes events to all registered frontends. Currently
only DiscordFrontend is registered; future frontends (web, Slack) will be
added via router.add().
"""

from __future__ import annotations

import logging
import os
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
    """Build ClaudeAgentOptions from config and session state.

    Note: can_use_tool is NOT set here — it's passed to BridgeTransport
    instead (which handles control_request.can_use_tool internally).
    """
    from axi.agents import make_stderr_callback

    selected_model = session.model or config.get_model()
    resolved_model, resolved_env = config.get_model_runtime(selected_model)
    base_env = {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "100",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "PATH": os.path.join(config.BOT_DIR, "bin") + ":" + os.environ.get("PATH", ""),
    }
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
        base_env.pop(key, None)
    base_env.update(resolved_env)

    return ClaudeAgentOptions(
        model=resolved_model,
        effort=config.get_effort(),
        thinking={"type": "adaptive"},
        setting_sources=["user", "project", "local"],
        permission_mode="plan" if session.plan_mode else "default",
        permission_prompt_tool_name="stdio",
        cwd=session.cwd,
        system_prompt=session.system_prompt,
        include_partial_messages=True,
        stderr=make_stderr_callback(session),
        resume=resume_id,
        sandbox={
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "excludedCommands": ["git", "systemctl", "uv", "ts-ssh", "ts-curl", *session.extra_excluded_commands],
            "network": {
                "allowAllUnixSockets": True,
                "allowUnixSockets": [
                    str(config.BRIDGE_SOCKET_PATH),
                ],
            },
        },
        add_dirs=[
            config.AXI_USER_DATA,
            config.BOT_WORKTREES_DIR,
            os.path.expanduser("~/.config/axi"),
            os.path.expanduser("~/.config/minflow"),
            os.path.expanduser("~/.cache/uv"),
            *session.extra_write_dirs,
        ],
        mcp_servers=session.mcp_servers or {},
        disallowed_tools=[],
        extra_args={"debug-to-stderr": None},
        env=base_env,
    )


async def _create_client(session: AgentSession, options: Any) -> Any:
    """Create a ClaudeSDKClient for a session."""
    from axi.agents import create_transport, make_cwd_permission_callback

    if session.agent_type == "flowcoder" and config.FLOWCODER_ENABLED:
        from axi.flowcoder import build_engine_cli_args, build_engine_env

        permission_cb = make_cwd_permission_callback(session.cwd, session)
        transport = await create_transport(session, can_use_tool=permission_cb)
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
    """Create and configure the rewritten AgentHub.

    Registers DiscordFrontend directly with the runtime's frontend list and
    then shares the same sessions dict with legacy Axi code during migration.
    """
    global router

    router = FrontendRouter()
    discord_fe = DiscordFrontend(bot)
    router.add(discord_fe)

    hub = AgentHub(
        frontends=[router],
        max_awake=config.MAX_AWAKE_AGENTS,
        make_agent_options=_make_agent_options,
        create_client=_create_client,
        disconnect_client=_disconnect_client,
        query_timeout=config.QUERY_TIMEOUT,
        usage_history_path=config.USAGE_HISTORY_PATH,
        rate_limit_history_path=config.RATE_LIMIT_HISTORY_PATH,
    )

    # Share the same sessions dict — gradual migration
    hub.sessions = sessions  # type: ignore[assignment]

    return hub
