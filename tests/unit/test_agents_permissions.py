"""Unit tests for agents.permissions — make_cwd_permission_callback."""

from __future__ import annotations

import os

import pytest
from claudewire.permissions import Allow, Deny

from axi.agents import make_cwd_permission_callback


class TestMakeCwdPermissionCallback:
    @pytest.mark.asyncio
    async def test_allows_read(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Read", {"file_path": "/tmp/project/foo.py"})
        assert isinstance(result, Allow)

    @pytest.mark.asyncio
    async def test_allows_write_in_cwd(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Write", {"file_path": "/tmp/project/out.txt"})
        assert isinstance(result, Allow)

    @pytest.mark.asyncio
    async def test_denies_write_outside_cwd(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Write", {"file_path": "/etc/passwd"})
        assert isinstance(result, Deny)

    @pytest.mark.asyncio
    async def test_allows_skill(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Skill", {})
        assert isinstance(result, Allow)

    @pytest.mark.asyncio
    async def test_allows_enter_plan_mode(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("EnterPlanMode", {})
        assert isinstance(result, Allow)

    @pytest.mark.asyncio
    async def test_allows_bash(self) -> None:
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Bash", {"command": "ls"})
        assert isinstance(result, Allow)

    @pytest.mark.asyncio
    async def test_allows_edit_in_user_data(self) -> None:
        """Writes to AXI_USER_DATA are always allowed."""
        from axi import config

        user_data = os.path.realpath(config.AXI_USER_DATA)
        cb = make_cwd_permission_callback("/tmp/project")
        result = await cb("Edit", {"file_path": os.path.join(user_data, "notes.md")})
        assert isinstance(result, Allow)
