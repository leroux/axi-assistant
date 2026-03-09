"""Unit tests for agents.permissions — make_cwd_permission_callback."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from axi.agents import make_cwd_permission_callback


class TestMakeCwdPermissionCallback:
    @pytest.mark.asyncio
    async def test_allows_read(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Read", {"file_path": "/tmp/project/foo.py"}, MagicMock(spec=ToolPermissionContext))
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_allows_write_in_cwd(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Write", {"file_path": "/tmp/project/out.txt"}, MagicMock(spec=ToolPermissionContext))
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_denies_write_outside_cwd(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Write", {"file_path": "/etc/passwd"}, MagicMock(spec=ToolPermissionContext))
        assert isinstance(result, PermissionResultDeny)

    @pytest.mark.asyncio
    async def test_denies_forbidden_tool(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Skill", {}, MagicMock(spec=ToolPermissionContext))
        assert isinstance(result, PermissionResultDeny)

    @pytest.mark.asyncio
    async def test_allows_enter_plan_mode(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("EnterPlanMode", {}, MagicMock(spec=ToolPermissionContext))
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_allows_bash(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Bash", {"command": "ls"}, MagicMock(spec=ToolPermissionContext))
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_allows_edit_in_user_data(self) -> None:
        """Writes to AXI_USER_DATA are always allowed."""
        from axi import config

        user_data = os.path.realpath(config.AXI_USER_DATA)
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Edit", {"file_path": os.path.join(user_data, "notes.md")}, MagicMock(spec=ToolPermissionContext))
        assert isinstance(result, PermissionResultAllow)
