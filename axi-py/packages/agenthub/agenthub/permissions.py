"""Session-aware permission composition for agent sessions.

Composes Claude Wire's stateless policies (cwd_policy, tool_block_policy)
with session context and optional interactive hooks (plan approval, user
questions) provided by the frontend.

The interactive hooks are plain callbacks — no Protocol class needed.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from claudewire.permissions import (
    CanUseTool,
    PolicyFn,
    compose,
    cwd_policy,
    tool_allow_policy,
    tool_block_policy,
)

if TYPE_CHECKING:
    from agenthub.types import AgentSession

# Interactive hooks: (session, tool_input) -> Allow | Deny
PlanApprovalHook = Callable[
    ["AgentSession", dict[str, Any]],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]
QuestionHook = Callable[
    ["AgentSession", dict[str, Any]],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]

# Default tool sets
DEFAULT_BLOCKED_TOOLS = frozenset({"Skill", "EnterWorktree", "Task"})
DEFAULT_AUTO_ALLOW_TOOLS = frozenset({"TodoWrite", "EnterPlanMode"})


def build_permission_callback(
    session: AgentSession,
    *,
    allowed_paths: list[str],
    blocked_tools: set[str] = DEFAULT_BLOCKED_TOOLS,
    auto_allow_tools: set[str] = DEFAULT_AUTO_ALLOW_TOOLS,
    plan_approval_hook: PlanApprovalHook | None = None,
    question_hook: QuestionHook | None = None,
) -> CanUseTool:
    """Build a composed permission callback for one agent session.

    Chains policies in order:
    1. Block forbidden tools (Skill, EnterWorktree, Task)
    2. Auto-allow safe tools (TodoWrite, EnterPlanMode)
    3. Interactive hooks (plan approval, user questions) if provided
    4. CWD restriction (file writes only inside allowed paths)
    5. Default: allow everything else
    """
    policies: list[PolicyFn] = [
        tool_block_policy(blocked_tools, message="Not available in agent mode"),
        tool_allow_policy(auto_allow_tools),
    ]

    if plan_approval_hook:
        # Capture hook in closure for this session
        hook = plan_approval_hook

        async def _plan_policy(
            tool_name: str,
            tool_input: dict[str, Any],
            ctx: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny | None:
            if tool_name == "ExitPlanMode":
                return await hook(session, tool_input)
            return None

        policies.append(_plan_policy)

    if question_hook:
        hook_q = question_hook

        async def _question_policy(
            tool_name: str,
            tool_input: dict[str, Any],
            ctx: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny | None:
            if tool_name == "AskUserQuestion":
                return await hook_q(session, tool_input)
            return None

        policies.append(_question_policy)

    policies.append(cwd_policy(allowed_paths))

    return compose(*policies)


def compute_allowed_paths(
    session_cwd: str,
    *,
    user_data_path: str,
    bot_dir: str | None = None,
    worktrees_dir: str | None = None,
    admin_allowed_cwds: list[str] | None = None,
) -> list[str]:
    """Compute the allowed write paths for an agent session.

    Agents can always write to their cwd and the user data directory.
    Code agents (cwd inside bot_dir or worktrees_dir) additionally get
    access to the worktrees directory and admin-allowed paths.
    """
    allowed_cwd = os.path.realpath(session_cwd)
    user_data = os.path.realpath(user_data_path)
    paths = [allowed_cwd, user_data]

    if bot_dir and worktrees_dir:
        real_bot = os.path.realpath(bot_dir)
        real_worktrees = os.path.realpath(worktrees_dir)
        is_code_agent = allowed_cwd in (real_bot, real_worktrees) or allowed_cwd.startswith(
            (real_bot + os.sep, real_worktrees + os.sep)
        )
        if is_code_agent:
            paths.append(real_worktrees)
            if admin_allowed_cwds:
                paths.extend(admin_allowed_cwds)

    return paths
