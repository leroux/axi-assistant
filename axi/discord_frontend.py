"""DiscordFrontend — wraps existing Discord code into the Frontend protocol.

Thin adapter: delegates to agents.py, channels.py, discord_stream.py,
discord_ui.py. Allows the FrontendRouter to multiplex Discord alongside
other frontends without changing existing behavior.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agenthub.frontend import PlanApprovalResult

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from agenthub.agent_log import LogEvent
    from agenthub.stream_types import StreamOutput

log = logging.getLogger(__name__)


class DiscordFrontend:
    """Frontend adapter for Discord.

    Wraps existing module-level functions (agents.py, channels.py, etc.)
    into the Frontend protocol. This is the first step toward a fully
    self-contained Discord frontend class — for now it delegates everything.
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    @property
    def name(self) -> str:
        return "discord"

    # --- Lifecycle ---

    async def start(self) -> None:
        pass  # Discord bot lifecycle is managed externally

    async def stop(self) -> None:
        pass  # Discord bot lifecycle is managed externally

    # --- Outbound: hub -> frontend ---

    async def post_message(self, agent_name: str, text: str) -> None:
        from axi.agents import send_long
        from axi.channels import get_agent_channel

        channel = await get_agent_channel(agent_name)
        if channel:
            await send_long(channel, text)

    async def post_system(self, agent_name: str, text: str) -> None:
        from axi.agents import send_system
        from axi.channels import get_agent_channel

        channel = await get_agent_channel(agent_name)
        if channel:
            await send_system(channel, text)

    async def broadcast(self, text: str) -> None:
        from axi.channels import get_master_channel

        master_ch = await get_master_channel()
        if master_ch:
            await master_ch.send(text)

    # --- Agent lifecycle events ---

    async def on_wake(self, agent_name: str) -> None:
        log.debug("Discord: agent '%s' woke", agent_name)

    async def on_sleep(self, agent_name: str) -> None:
        log.debug("Discord: agent '%s' slept", agent_name)

    async def on_spawn(self, agent_name: str, session: Any) -> None:
        log.info("Discord: agent '%s' spawned", agent_name)

    async def on_kill(self, agent_name: str, session_id: str | None) -> None:
        log.info("Discord: agent '%s' killed", agent_name)

    async def on_session_id(self, agent_name: str, session_id: str) -> None:
        log.debug("Discord: agent '%s' session_id=%s", agent_name, session_id)

    async def on_idle_reminder(self, agent_name: str, idle_minutes: float) -> None:
        pass  # Handled by existing idle check code

    async def on_reconnect(self, agent_name: str, was_mid_task: bool) -> None:
        from axi.channels import get_agent_channel

        channel = await get_agent_channel(agent_name)
        if channel:
            if was_mid_task:
                await channel.send("*(reconnected after restart — resuming output)*")
            else:
                await channel.send("*(reconnected after restart)*")

    # --- Stream rendering ---

    async def on_stream_event(self, agent_name: str, event: StreamOutput) -> None:
        pass  # Stream rendering uses existing discord_stream.py path for now

    # --- Interactive gates ---
    # Not yet called through the FrontendRouter — plan approval and questions
    # still flow through the permission callback system in agents.py.
    # These stubs exist for protocol compliance; they'll be wired when
    # permission handling migrates to the frontend.

    async def request_plan_approval(
        self, agent_name: str, plan_content: str, session: Any
    ) -> PlanApprovalResult:
        return PlanApprovalResult(approved=True)

    async def ask_question(
        self, agent_name: str, questions: list[dict[str, Any]], session: Any
    ) -> dict[str, str]:
        return {}

    async def update_todo(self, agent_name: str, todos: list[dict[str, Any]]) -> None:
        pass

    # --- Channel management ---

    async def ensure_channel(self, agent_name: str, cwd: str | None = None) -> Any:
        from axi.channels import ensure_agent_channel

        return await ensure_agent_channel(agent_name, cwd=cwd)

    async def move_to_killed(self, agent_name: str) -> None:
        from axi.channels import move_channel_to_killed

        await move_channel_to_killed(agent_name)

    async def get_channel(self, agent_name: str) -> Any:
        from axi.channels import get_agent_channel

        return await get_agent_channel(agent_name)

    # --- Session persistence ---

    async def save_session_metadata(self, agent_name: str, session: Any) -> None:
        from axi.agents import _set_session_id  # pyright: ignore[reportPrivateUsage]

        if session.session_id:
            await _set_session_id(session, session.session_id)

    async def reconstruct_sessions(self) -> list[dict[str, Any]]:
        from axi.agents import reconstruct_agents_from_channels

        await reconstruct_agents_from_channels()
        return []  # Reconstruction populates agents dict directly

    # --- Event log integration ---

    async def on_log_event(self, event: LogEvent) -> None:
        pass  # Discord doesn't use the event log (yet)

    # --- Shutdown ---

    async def send_goodbye(self) -> None:
        from axi.channels import get_master_channel

        master_ch = await get_master_channel()
        if master_ch:
            await master_ch.send("*System:* Shutting down — see you soon!")

    async def close_app(self) -> None:
        from axi.tracing import shutdown_tracing

        shutdown_tracing()
        await self._bot.close()

    async def kill_process(self) -> None:
        from agenthub.shutdown import kill_supervisor

        kill_supervisor()
