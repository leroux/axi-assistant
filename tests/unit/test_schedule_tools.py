"""Unit tests for schedule_tools.schedule_key()."""

from axi.schedule_tools import schedule_key


class TestScheduleKey:
    def test_with_owner(self) -> None:
        entry = {"name": "daily-check", "owner": "axi-master"}
        assert schedule_key(entry) == "axi-master/daily-check"

    def test_without_owner(self) -> None:
        entry = {"name": "legacy-task"}
        assert schedule_key(entry) == "legacy-task"

    def test_different_owners_different_keys(self) -> None:
        a = {"name": "task", "owner": "agent-a"}
        b = {"name": "task", "owner": "agent-b"}
        assert schedule_key(a) != schedule_key(b)

    def test_same_owner_different_names(self) -> None:
        a = {"name": "task-1", "owner": "agent"}
        b = {"name": "task-2", "owner": "agent"}
        assert schedule_key(a) != schedule_key(b)
