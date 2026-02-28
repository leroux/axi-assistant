"""Permission callbacks for agent tool use."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

import config

if TYPE_CHECKING:
    from axi_types import AgentSession

log = logging.getLogger("axi")


def make_cwd_permission_callback(allowed_cwd: str, session: AgentSession | None = None):
    """Create a can_use_tool callback that restricts file writes to allowed_cwd and AXI_USER_DATA."""
    allowed = os.path.realpath(allowed_cwd)
    user_data = os.path.realpath(config.AXI_USER_DATA)
    worktrees = os.path.realpath(config.BOT_WORKTREES_DIR)
    bot_dir = os.path.realpath(config.BOT_DIR)

    is_code_agent = (
        allowed in (bot_dir, worktrees)
        or allowed.startswith((bot_dir + os.sep, worktrees + os.sep))
    )
    bases = [allowed, user_data]
    if is_code_agent:
        bases.append(worktrees)
        bases.extend(config.ADMIN_ALLOWED_CWDS)

    async def _check_permission(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        forbidden_tools = {"AskUserQuestion", "TodoWrite", "Skill", "EnterWorktree", "Task"}
        if tool_name in forbidden_tools:
            return PermissionResultDeny(
                message=f"{tool_name} is not compatible with Discord-based agent mode. Use text messages to communicate instead."
            )

        if tool_name == "EnterPlanMode":
            return PermissionResultAllow()

        if tool_name == "ExitPlanMode":
            return await _handle_exit_plan_mode(session, tool_input)

        if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            resolved = os.path.realpath(path)
            for base in bases:
                if resolved == base or resolved.startswith(base + os.sep):
                    return PermissionResultAllow()
            return PermissionResultDeny(
                message=f"Access denied: {path} is outside working directory {allowed} and user data {user_data}"
            )
        return PermissionResultAllow()

    return _check_permission


async def _handle_exit_plan_mode(
    session: AgentSession | None,
    tool_input: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    """Handle ExitPlanMode by posting the plan to Discord and waiting for user approval."""
    if session is None or session.discord_channel_id is None:
        return PermissionResultAllow()

    channel_id = session.discord_channel_id

    async def _send_plan_msg(content: str) -> None:
        await config.discord_request("POST", f"/channels/{channel_id}/messages", json={"content": content})

    plan_content = (tool_input.get("plan") or "").strip() or None

    header = f"📋 **Plan from {session.name}** — waiting for approval"
    try:
        if plan_content:
            plan_bytes = plan_content.encode("utf-8")
            await config.discord_request(
                "POST", f"/channels/{channel_id}/messages",
                data={"content": header},
                files={"files[0]": ("plan.txt", plan_bytes)},
            )
        else:
            await _send_plan_msg(
                f"{header}\n\n*(Plan file not found — the agent should have described the plan in its messages above.)*"
            )

        await _send_plan_msg(
            "Reply with **approve** to proceed, **reject** to cancel, or type feedback to revise the plan."
        )
    except Exception:
        log.exception("_handle_exit_plan_mode: failed to post plan to Discord — auto-approving")
        return PermissionResultAllow()

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    session.plan_approval_future = future  # type: ignore[assignment]

    log.info("Agent '%s' paused waiting for plan approval", session.name)

    try:
        result = await future
    finally:
        session.plan_approval_future = None

    if result.get("approved"):
        log.info("Agent '%s' plan approved by user", session.name)
        if session.plan_mode:
            session.plan_mode = False
            if session.client:
                try:
                    await session.client.set_permission_mode("default")
                    log.info("Agent '%s' permission mode reset to default after plan approval", session.name)
                except Exception:
                    log.exception("Failed to reset permission mode for '%s'", session.name)
        return PermissionResultAllow()
    else:
        message = result.get("message", "User rejected the plan.")
        log.info("Agent '%s' plan rejected: %s", session.name, message)
        return PermissionResultDeny(message=json.dumps(message) if not isinstance(message, str) else message)
