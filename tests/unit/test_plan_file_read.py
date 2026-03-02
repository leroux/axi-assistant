"""Unit tests for _read_latest_plan_file — plan file discovery logic."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from axi.agents import _read_latest_plan_file

if TYPE_CHECKING:
    import pytest


class TestReadLatestPlanFile:
    def test_finds_plan_md_in_cwd(self, tmp_path: pytest.TempPathFactory) -> None:
        plan = tmp_path / "PLAN.md"
        plan.write_text("# My Plan\nDo the thing.")
        result = _read_latest_plan_file(cwd=str(tmp_path))
        assert result == "# My Plan\nDo the thing."

    def test_finds_lowercase_plan_md_in_cwd(self, tmp_path: pytest.TempPathFactory) -> None:
        plan = tmp_path / "plan.md"
        plan.write_text("lowercase plan")
        result = _read_latest_plan_file(cwd=str(tmp_path))
        assert result == "lowercase plan"

    def test_finds_plan_in_claude_plans_dir(self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        plan = plans_dir / "fuzzy-dancing-cat.md"
        plan.write_text("plan from claude dir")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = _read_latest_plan_file()
        assert result == "plan from claude dir"

    def test_prefers_newer_cwd_over_claude_plans(self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
        # Create older file in ~/.claude/plans/
        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        old_plan = plans_dir / "old-plan.md"
        old_plan.write_text("old plan")
        # Backdate it
        import os
        old_time = time.time() - 60
        os.utime(old_plan, (old_time, old_time))

        # Create newer file in CWD
        cwd = tmp_path / "project"
        cwd.mkdir()
        new_plan = cwd / "PLAN.md"
        new_plan.write_text("new plan in cwd")

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = _read_latest_plan_file(cwd=str(cwd))
        assert result == "new plan in cwd"

    def test_prefers_newer_claude_plans_over_cwd(self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
        # Create older PLAN.md in CWD
        cwd = tmp_path / "project"
        cwd.mkdir()
        old_plan = cwd / "PLAN.md"
        old_plan.write_text("old plan in cwd")
        import os
        old_time = time.time() - 60
        os.utime(old_plan, (old_time, old_time))

        # Create newer file in ~/.claude/plans/
        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        new_plan = plans_dir / "new-plan.md"
        new_plan.write_text("new plan from claude")

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = _read_latest_plan_file(cwd=str(cwd))
        assert result == "new plan from claude"

    def test_returns_none_when_too_old(self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = tmp_path / "PLAN.md"
        plan.write_text("stale plan")
        import os
        old_time = time.time() - 600  # 10 minutes ago
        os.utime(plan, (old_time, old_time))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = _read_latest_plan_file(cwd=str(tmp_path))
        assert result is None

    def test_returns_none_when_no_files(self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = _read_latest_plan_file(cwd=str(tmp_path))
        assert result is None

    def test_returns_none_with_no_cwd(self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        result = _read_latest_plan_file()
        assert result is None

    def test_skips_empty_plan_file(self, tmp_path: pytest.TempPathFactory) -> None:
        plan = tmp_path / "PLAN.md"
        plan.write_text("   \n  \n  ")
        result = _read_latest_plan_file(cwd=str(tmp_path))
        assert result is None
