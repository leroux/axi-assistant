"""Stateless permission policies for Claude Code processes.

Factory functions that return CanUseTool callbacks. Compose them with
compose() to build a policy chain. Each policy returns Allow, Deny,
or None (no opinion — pass to next). First non-None result wins.

Types (CanUseTool, PermissionResultAllow, etc.) come from claude_agent_sdk.
These policies are about safely running a single Claude Code process —
multi-agent orchestration (per-session composition, interactive hooks)
lives in agenthub.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

# A policy returns Allow, Deny, or None (no opinion).
# This is looser than CanUseTool (which must return Allow|Deny).
PolicyFn = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny | None],
]

# SDK's CanUseTool type — must always return Allow or Deny (never None).
CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]

# Tools that perform file writes
_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


def cwd_policy(allowed_paths: list[str]) -> PolicyFn:
    """Restrict file writes to allowed base paths.

    Returns Allow for writes inside allowed paths, Deny for writes outside,
    None for non-write tools (no opinion).
    """
    resolved = [os.path.realpath(p) for p in allowed_paths]

    async def _check(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny | None:
        if tool_name not in _WRITE_TOOLS:
            return None
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        real = os.path.realpath(path)
        for base in resolved:
            if real == base or real.startswith(base + os.sep):
                return PermissionResultAllow()
        return PermissionResultDeny(
            message=f"Access denied: {path} is outside allowed paths"
        )

    return _check


def tool_block_policy(
    blocked: set[str],
    message: str = "Tool not available in this mode",
) -> PolicyFn:
    """Block specific tools by name."""

    async def _check(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultDeny | None:
        if tool_name in blocked:
            return PermissionResultDeny(message=f"{tool_name}: {message}")
        return None

    return _check


def tool_allow_policy(allowed: set[str]) -> PolicyFn:
    """Auto-allow specific tools by name."""

    async def _check(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | None:
        if tool_name in allowed:
            return PermissionResultAllow()
        return None

    return _check


def compose(*policies: PolicyFn) -> CanUseTool:
    """Chain policies into a single CanUseTool callback.

    Evaluates policies in order. First non-None result wins.
    If all policies return None, allows the tool call.
    """

    async def _check(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        for policy in policies:
            result = await policy(tool_name, tool_input, ctx)
            if result is not None:
                return result
        return PermissionResultAllow()

    return _check


async def allow_all(
    tool_name: str,
    tool_input: dict[str, Any],
    ctx: ToolPermissionContext,
) -> PermissionResultAllow:
    """Allow everything. Useful for testing or trusted environments."""
    return PermissionResultAllow()


async def deny_all(
    tool_name: str,
    tool_input: dict[str, Any],
    ctx: ToolPermissionContext,
) -> PermissionResultDeny:
    """Deny everything. Useful as a fallback."""
    return PermissionResultDeny(message="All tool calls denied")
