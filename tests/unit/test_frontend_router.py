"""Tests for the rewritten FrontendRouter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agenthub.frontend import PlanApprovalResult

if TYPE_CHECKING:
    from agenthub.agent_log import LogEvent
from agenthub.frontend_router import FrontendRouter
from agenthub.stream_types import TextDelta


class FakeFrontend:
    def __init__(self, frontend_name: str) -> None:
        self._name = frontend_name
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    @property
    def name(self) -> str:
        return self._name

    def _record(self, method: str, *args: Any) -> None:
        self.calls.append((method, args))

    async def start(self) -> None:
        self._record("start")

    async def stop(self) -> None:
        self._record("stop")

    async def post_message(self, agent_name: str, text: str) -> None:
        self._record("post_message", agent_name, text)

    async def post_system(self, agent_name: str, text: str) -> None:
        self._record("post_system", agent_name, text)

    async def broadcast(self, text: str) -> None:
        self._record("broadcast", text)

    async def on_wake(self, agent_name: str) -> None:
        self._record("on_wake", agent_name)

    async def on_sleep(self, agent_name: str) -> None:
        self._record("on_sleep", agent_name)

    async def on_spawn(self, agent_name: str, session: Any) -> None:
        self._record("on_spawn", agent_name)

    async def on_kill(self, agent_name: str, session_id: str | None) -> None:
        self._record("on_kill", agent_name, session_id)

    async def on_session_id(self, agent_name: str, session_id: str) -> None:
        self._record("on_session_id", agent_name, session_id)

    async def on_idle_reminder(self, agent_name: str, idle_minutes: float) -> None:
        self._record("on_idle_reminder", agent_name, idle_minutes)

    async def on_reconnect(self, agent_name: str, was_mid_task: bool) -> None:
        self._record("on_reconnect", agent_name, was_mid_task)

    async def on_stream_event(self, agent_name: str, event: Any) -> None:
        self._record("on_stream_event", agent_name, type(event).__name__)

    async def request_plan_approval(
        self, agent_name: str, plan_content: str, session: Any
    ) -> PlanApprovalResult:
        self._record("request_plan_approval", agent_name)
        return PlanApprovalResult(approved=True)

    async def ask_question(
        self, agent_name: str, questions: list[dict[str, Any]], session: Any
    ) -> dict[str, str]:
        self._record("ask_question", agent_name)
        return {"q1": "answer1"}

    async def update_todo(self, agent_name: str, todos: list[dict[str, Any]]) -> None:
        self._record("update_todo", agent_name)

    async def ensure_channel(self, agent_name: str, cwd: str | None = None) -> Any:
        self._record("ensure_channel", agent_name)
        return f"channel-{agent_name}"

    async def move_to_killed(self, agent_name: str) -> None:
        self._record("move_to_killed", agent_name)

    async def get_channel(self, agent_name: str) -> Any:
        return f"channel-{agent_name}"

    async def save_session_metadata(self, agent_name: str, session: Any) -> None:
        self._record("save_session_metadata", agent_name)

    async def reconstruct_sessions(self) -> list[dict[str, Any]]:
        return []

    async def on_log_event(self, event: LogEvent) -> None:
        self._record("on_log_event", event.kind)

    async def send_goodbye(self) -> None:
        self._record("send_goodbye")

    async def close_app(self) -> None:
        self._record("close_app")

    async def kill_process(self) -> None:
        self._record("kill_process")


class TestFrontendRouter:
    @pytest.mark.asyncio
    async def test_add_remove(self) -> None:
        router = FrontendRouter()
        fe = FakeFrontend("discord")
        router.add(fe)
        assert "discord" in router.frontends
        router.remove("discord")
        assert "discord" not in router.frontends

    @pytest.mark.asyncio
    async def test_broadcast_to_all_frontends(self) -> None:
        router = FrontendRouter()
        fe1 = FakeFrontend("discord")
        fe2 = FakeFrontend("web")
        router.add(fe1)
        router.add(fe2)

        await router.post_message("master", "hello")
        assert ("post_message", ("master", "hello")) in fe1.calls
        assert ("post_message", ("master", "hello")) in fe2.calls

    @pytest.mark.asyncio
    async def test_stream_event_broadcast(self) -> None:
        router = FrontendRouter()
        fe1 = FakeFrontend("discord")
        fe2 = FakeFrontend("web")
        router.add(fe1)
        router.add(fe2)

        await router.on_stream_event("agent-1", TextDelta(text="hi"))
        assert ("on_stream_event", ("agent-1", "TextDelta")) in fe1.calls
        assert ("on_stream_event", ("agent-1", "TextDelta")) in fe2.calls

    @pytest.mark.asyncio
    async def test_plan_approval_first_wins(self) -> None:
        router = FrontendRouter()
        fe1 = FakeFrontend("discord")
        fe2 = FakeFrontend("web")
        router.add(fe1)
        router.add(fe2)

        result = await router.request_plan_approval("agent-1", "the plan", None)
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_ask_question(self) -> None:
        router = FrontendRouter()
        fe = FakeFrontend("discord")
        router.add(fe)

        answers = await router.ask_question("agent-1", [{"question": "q1"}], None)
        assert answers == {"q1": "answer1"}

    @pytest.mark.asyncio
    async def test_get_channel(self) -> None:
        router = FrontendRouter()
        fe = FakeFrontend("discord")
        router.add(fe)

        ch = await router.get_channel("master")
        assert ch == "channel-master"

    @pytest.mark.asyncio
    async def test_ensure_channel_returns_value(self) -> None:
        router = FrontendRouter()
        fe = FakeFrontend("discord")
        router.add(fe)
        ch = await router.ensure_channel("master")
        assert ch == "channel-master"

    @pytest.mark.asyncio
    async def test_no_frontends_no_error(self) -> None:
        router = FrontendRouter()
        await router.post_message("agent", "text")
        await router.on_wake("agent")
        await router.broadcast("msg")
        result = await router.request_plan_approval("agent", "plan", None)
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_broadcast_survives_frontend_failure(self) -> None:
        router = FrontendRouter()
        bad = FakeFrontend("bad")
        good = FakeFrontend("good")

        async def boom(*args: Any) -> None:
            raise RuntimeError("boom")

        bad.post_message = boom  # type: ignore[assignment]
        router.add(bad)
        router.add(good)

        await router.post_message("master", "hello")
        assert ("post_message", ("master", "hello")) in good.calls

    @pytest.mark.asyncio
    async def test_first_response_skips_failing_frontend(self) -> None:
        router = FrontendRouter()
        bad = FakeFrontend("bad")
        good = FakeFrontend("good")

        async def boom(*args: Any) -> PlanApprovalResult:
            raise RuntimeError("boom")

        bad.request_plan_approval = boom  # type: ignore[assignment]
        router.add(bad)
        router.add(good)

        result = await router.request_plan_approval("agent", "plan", None)
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_get_channel_falls_through_when_first_frontend_returns_none(self) -> None:
        router = FrontendRouter()
        fe1 = FakeFrontend("one")
        fe2 = FakeFrontend("two")

        async def none_channel(agent_name: str) -> Any:
            return None

        fe1.get_channel = none_channel  # type: ignore[assignment]
        router.add(fe1)
        router.add(fe2)

        ch = await router.get_channel("master")
        assert ch == "channel-master"

    @pytest.mark.asyncio
    async def test_ensure_channel_falls_through_when_first_frontend_raises(self) -> None:
        router = FrontendRouter()
        fe1 = FakeFrontend("one")
        fe2 = FakeFrontend("two")

        async def boom(agent_name: str, cwd: str | None = None) -> Any:
            raise RuntimeError("boom")

        fe1.ensure_channel = boom  # type: ignore[assignment]
        router.add(fe1)
        router.add(fe2)

        ch = await router.ensure_channel("master")
        assert ch == "channel-master"

    @pytest.mark.asyncio
    async def test_kill_process_stops_after_first_success(self) -> None:
        router = FrontendRouter()
        fe1 = FakeFrontend("one")
        fe2 = FakeFrontend("two")
        called: list[str] = []

        async def kill_one() -> None:
            called.append("one")

        async def kill_two() -> None:
            called.append("two")

        fe1.kill_process = kill_one  # type: ignore[assignment]
        fe2.kill_process = kill_two  # type: ignore[assignment]
        router.add(fe1)
        router.add(fe2)

        await router.kill_process()
        assert called == ["one"]

    @pytest.mark.asyncio
    async def test_misc_broadcast_methods_are_relayed(self) -> None:
        router = FrontendRouter()
        fe = FakeFrontend("discord")
        router.add(fe)

        await router.on_idle_reminder("agent", 5.0)
        await router.on_reconnect("agent", True)
        await router.move_to_killed("agent")
        await router.send_goodbye()
        await router.close_app()

        methods = [call[0] for call in fe.calls]
        assert "on_idle_reminder" in methods
        assert "on_reconnect" in methods
        assert "move_to_killed" in methods
        assert "send_goodbye" in methods
        assert "close_app" in methods
