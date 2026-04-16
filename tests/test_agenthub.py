"""Headless integration tests for the rewritten AgentHub runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agenthub import AgentHub, FrontendRouter, StopResult, TurnKind, TurnOutcome
from agenthub.stream_types import QueryResult, RateLimitHit, StreamEnd, StreamKilled, StreamStart, TransientError
from agenthub.streaming import stream_response


async def result_stream(session: Any, **kwargs: Any):
    yield StreamStart()
    yield QueryResult(session_id=f"sid-{session.name}", cost_usd=0.01, num_turns=1, duration_ms=25)
    yield StreamEnd(elapsed_s=0.01, msg_count=1, flush_count=0)


async def killed_stream(session: Any, **kwargs: Any):
    yield StreamStart()
    yield StreamKilled()
    yield StreamEnd(elapsed_s=0.01, msg_count=0, flush_count=0)


@pytest.fixture(scope="session")
def warmup():
    return None


@pytest.fixture(autouse=True)
def _recover_after_failure():
    return None


class FakeClient:
    def __init__(self, name: str, mode: str = "result") -> None:
        self.name = name
        self.mode = mode
        self.queries: list[Any] = []
        self._interrupted = False
        self._messages: asyncio.Queue[Any] = asyncio.Queue()
        self._query = self

    async def query(self, content: Any) -> None:
        self.queries.append(content)
        if self.mode == "result":
            await self._messages.put(type("Result", (), {
                "session_id": f"sid-{self.name}",
                "total_cost_usd": 0.01,
                "num_turns": 1,
                "duration_ms": 25,
                "duration_api_ms": 25,
                "is_error": False,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })())
        elif self.mode in {"killed", "wait"}:
            return

    async def interrupt(self) -> None:
        self._interrupted = True

    async def receive_messages(self):
        while not self._messages.empty():
            yield await self._messages.get()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object):
        return None


@dataclass
class FakeFrontend:
    frontend_name: str
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.frontend_name

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

    async def request_plan_approval(self, agent_name: str, plan_content: str, session: Any):
        self._record("request_plan_approval", agent_name)
        from agenthub.frontend import PlanApprovalResult
        return PlanApprovalResult(approved=True)

    async def ask_question(self, agent_name: str, questions: list[dict[str, Any]], session: Any) -> dict[str, str]:
        self._record("ask_question", agent_name)
        return {"ok": "yes"}

    async def update_todo(self, agent_name: str, todos: list[dict[str, Any]]) -> None:
        self._record("update_todo", agent_name)

    async def on_log_event(self, event: Any) -> None:
        self._record("on_log_event", event.kind)


@pytest.fixture
def frontend() -> FakeFrontend:
    return FakeFrontend("fake")


@pytest.fixture
def hub(frontend: FakeFrontend) -> AgentHub:
    router = FrontendRouter()
    router.add(frontend)

    async def create_client(session: Any, options: Any) -> FakeClient:
        return FakeClient(session.name)

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    def make_agent_options(session: Any, session_id: str | None) -> dict[str, Any]:
        return {"session": session.name, "resume": session_id}

    return AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=make_agent_options,
        max_awake=3,
        query_timeout=1.0,
        stream_factory=result_stream,
    )


@pytest.mark.asyncio
async def test_spawn_and_remove_agent(hub: AgentHub, frontend: FakeFrontend) -> None:
    session = await hub.spawn_agent(name="agent-1", cwd="/tmp/agent-1")
    assert session.name == "agent-1"
    assert hub.get_session("agent-1") is session
    assert ("on_spawn", ("agent-1",)) in frontend.calls

    await hub.remove_agent("agent-1")
    assert hub.get_session("agent-1") is None
    assert any(call[0] == "on_kill" for call in frontend.calls)


@pytest.mark.asyncio
async def test_submit_user_message_runs_turn(hub: AgentHub, frontend: FakeFrontend) -> None:
    await hub.spawn_agent(name="agent-run", cwd="/tmp/agent-run")
    result = await hub.submit_user_message("agent-run", "hello")
    assert result.status == "started"
    await asyncio.sleep(0.05)

    session = hub.get_session("agent-run")
    assert session is not None
    assert session.state.lifecycle in {session.state.lifecycle.IDLE, session.state.lifecycle.SLEEPING}
    assert session.state.session_id == "sid-agent-run"
    assert any(call[0] == "on_wake" for call in frontend.calls)
    assert any(call[0] == "on_stream_event" and call[1][1] == "StreamStart" for call in frontend.calls)


@pytest.mark.asyncio
async def test_killed_turn_does_not_post_special_completion(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)

    async def create_client(session: Any, options: Any) -> FakeClient:
        return FakeClient(session.name, mode="killed")

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
        stream_factory=killed_stream,
    )
    await hub.spawn_agent(name="killed-turn", cwd="/tmp/killed-turn")
    result = await hub.submit_user_message("killed-turn", "boot")
    assert result.status == "started"
    await asyncio.sleep(0.05)

    session = hub.get_session("killed-turn")
    assert session is not None
    assert session.state.lifecycle in {session.state.lifecycle.IDLE, session.state.lifecycle.SLEEPING}
    assert not any(
        call[0] == "post_system" and "finished initial task" in call[1][1].lower()
        for call in frontend.calls
    )


@pytest.mark.asyncio
async def test_stop_clears_queue(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)
    gate = asyncio.Event()

    class WaitingClient(FakeClient):
        async def query(self, content: Any) -> None:
            self.queries.append(content)
            await gate.wait()

    async def create_client(session: Any, options: Any) -> FakeClient:
        return WaitingClient(session.name, mode="wait")

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
    )
    await hub.spawn_agent(name="stop-agent", cwd="/tmp/stop-agent")
    start = await hub.submit_user_message("stop-agent", "first")
    assert start.status == "started"
    queued = await hub.submit_user_message("stop-agent", "after")
    assert queued.status == "queued"

    stopped = await hub.request_stop("stop-agent")
    assert isinstance(stopped, StopResult)
    assert stopped.cleared == 1
    gate.set()
    await asyncio.sleep(0.05)

    session = hub.get_session("stop-agent")
    assert session is not None
    assert len(session.state.queued_turns) == 0


@pytest.mark.asyncio
async def test_max_awake_limit_queues_new_wake_until_slot_frees(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)
    gate = asyncio.Event()

    class WaitingClient(FakeClient):
        async def query(self, content: Any) -> None:
            self.queries.append(content)
            await gate.wait()

    async def create_client(session: Any, options: Any) -> FakeClient:
        return WaitingClient(session.name, mode="wait")

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
        max_awake=1,
    )
    await hub.spawn_agent(name="a1", cwd="/tmp/a1")
    await hub.spawn_agent(name="a2", cwd="/tmp/a2")

    first = await hub.submit_user_message("a1", "hold")
    assert first.status == "started"
    second = await hub.submit_user_message("a2", "blocked")
    assert second.status == "started"
    await asyncio.sleep(0.05)

    session = hub.get_session("a2")
    assert session is not None
    assert session.state.lifecycle is session.state.lifecycle.WAKING
    assert session.client is None
    assert session.state.current_turn is not None

    gate.set()
    await asyncio.sleep(0.1)

    assert any(call == ("on_wake", ("a2",)) for call in frontend.calls)
    assert session.state.lifecycle in {session.state.lifecycle.IDLE, session.state.lifecycle.SLEEPING}


@pytest.mark.asyncio
async def test_query_timeout_bounds_entire_turn(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)
    stream_entered = asyncio.Event()
    hold_stream = asyncio.Event()

    class HangingStreamClient(FakeClient):
        def __init__(self, name: str, mode: str = "wait") -> None:
            super().__init__(name, mode)
            self.interrupt = AsyncMock(side_effect=self._interrupt_impl)

        async def _interrupt_impl(self) -> None:
            self._interrupted = True
            hold_stream.set()

        async def query(self, content: Any) -> None:
            self.queries.append(content)

        async def receive_messages(self):
            stream_entered.set()
            await hold_stream.wait()
            if False:
                yield None

    client_ref: HangingStreamClient | None = None

    async def create_client(session: Any, options: Any) -> FakeClient:
        nonlocal client_ref
        client_ref = HangingStreamClient(session.name, mode="wait")
        return client_ref

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
        query_timeout=0.05,
        stream_factory=stream_response,
    )
    await hub.spawn_agent(name="hang-agent", cwd="/tmp/hang-agent")

    result = await hub.submit_user_message("hang-agent", "hello")
    assert result.status == "started"
    await asyncio.wait_for(stream_entered.wait(), timeout=1.0)
    await asyncio.sleep(0.2)

    session = hub.get_session("hang-agent")
    assert session is not None
    assert client_ref is not None
    assert session.query_task is None
    assert session.state.current_turn is None
    assert session.state.lifecycle is session.state.lifecycle.SLEEPING
    assert session.client is None
    client_ref.interrupt.assert_awaited_once()
    assert any(call == ("on_stream_event", ("hang-agent", "StreamStart")) for call in frontend.calls)
    assert any(call == ("on_sleep", ("hang-agent",)) for call in frontend.calls)
    assert not any(
        call[0] == "on_stream_event" and call[1][0] == "hang-agent" and call[1][1] == "StreamEnd"
        for call in frontend.calls
    )


@pytest.mark.asyncio
async def test_submit_inter_agent_message_marks_background(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)

    async def create_client(session: Any, options: Any) -> FakeClient:
        return FakeClient(session.name)

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
        stream_factory=result_stream,
    )
    await hub.spawn_agent(name="worker", cwd="/tmp/worker")

    result = await hub.submit_inter_agent_message("worker", "hello from master")
    assert result.status == "started"
    await asyncio.sleep(0.05)

    assert "worker" not in hub.scheduler._interactive
    session = hub.get_session("worker")
    assert session is not None
    assert session.state.lifecycle in {session.state.lifecycle.IDLE, session.state.lifecycle.SLEEPING}


@pytest.mark.asyncio
async def test_request_skip_interrupts_and_runs_queued_followup(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)
    gate = asyncio.Event()

    class WaitingClient(FakeClient):
        async def query(self, content: Any) -> None:
            self.queries.append(content)
            await gate.wait()

    client_ref: WaitingClient | None = None

    async def create_client(session: Any, options: Any) -> FakeClient:
        nonlocal client_ref
        client_ref = WaitingClient(session.name, mode="wait")
        return client_ref

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
    )
    await hub.spawn_agent(name="skip-agent", cwd="/tmp/skip-agent")
    start = await hub.submit_user_message("skip-agent", "first")
    assert start.status == "started"
    queued = await hub.submit_user_message("skip-agent", "second")
    assert queued.status == "queued"

    skipped = await hub.request_skip("skip-agent")
    assert skipped.status == "stopping"
    gate.set()
    await asyncio.sleep(0.05)

    session = hub.get_session("skip-agent")
    assert session is not None
    assert client_ref is not None
    assert client_ref.queries == ["first", "second"]
    assert session.state.current_turn is None
    assert len(session.state.queued_turns) == 0
    assert session.state.lifecycle in {session.state.lifecycle.IDLE, session.state.lifecycle.SLEEPING}
    assert session.state.skip_requested is False


@pytest.mark.asyncio
async def test_request_stop_without_clearing_queue_preserves_followup(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)
    gate = asyncio.Event()

    class WaitingClient(FakeClient):
        async def query(self, content: Any) -> None:
            self.queries.append(content)
            await gate.wait()

    client_ref: WaitingClient | None = None

    async def create_client(session: Any, options: Any) -> FakeClient:
        nonlocal client_ref
        client_ref = WaitingClient(session.name, mode="wait")
        return client_ref

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
        stream_factory=result_stream,
    )
    await hub.spawn_agent(name="stop-keep", cwd="/tmp/stop-keep")
    start = await hub.submit_user_message("stop-keep", "first")
    assert start.status == "started"
    queued = await hub.submit_user_message("stop-keep", "second")
    assert queued.status == "queued"

    stopped = await hub.request_stop("stop-keep", clear_queue=False)
    assert stopped.status == "stopping"
    assert stopped.cleared == 0
    gate.set()
    await asyncio.sleep(0.1)

    session = hub.get_session("stop-keep")
    assert session is not None
    assert client_ref is not None
    assert client_ref.queries == ["first"]
    assert session.state.current_turn is None
    assert len(session.state.queued_turns) == 1
    assert session.state.queued_turns[0].content == "second"
    assert session.state.lifecycle is session.state.lifecycle.SLEEPING


@pytest.mark.asyncio
async def test_shutdown_requested_short_circuits_submission(hub: AgentHub) -> None:
    await hub.spawn_agent(name="shutdown-agent", cwd="/tmp/shutdown-agent")
    hub.shutdown_requested = True

    result = await hub.submit_user_message("shutdown-agent", "hello")

    assert result.status == "shutdown"
    session = hub.get_session("shutdown-agent")
    assert session is not None
    assert session.query_task is None
    assert session.state.current_turn is None


@pytest.mark.asyncio
async def test_wake_failure_clears_current_turn_and_releases_slot(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)

    async def create_client(session: Any, options: Any) -> FakeClient:
        raise RuntimeError("boom")

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
    )
    await hub.spawn_agent(name="wake-fail", cwd="/tmp/wake-fail")

    result = await hub.submit_user_message("wake-fail", "hello")
    assert result.status == "started"
    await asyncio.sleep(0.05)

    session = hub.get_session("wake-fail")
    assert session is not None
    assert session.state.current_turn is None
    assert session.state.lifecycle is session.state.lifecycle.SLEEPING
    assert session.query_task is None
    assert hub.scheduler.slot_count() == 0


@pytest.mark.asyncio
async def test_query_result_error_marks_turn_error(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)

    async def error_result_stream(session: Any, **kwargs: Any):
        yield StreamStart()
        yield QueryResult(session_id=f"sid-{session.name}", cost_usd=0.01, num_turns=1, duration_ms=25, is_error=True)
        yield StreamEnd(elapsed_s=0.01, msg_count=1, flush_count=0)

    hub = AgentHub(
        frontends=[router],
        create_client=lambda session, options: FakeClient(session.name),
        disconnect_client=lambda client, name: asyncio.sleep(0),
        make_agent_options=lambda session, sid: {},
        stream_factory=error_result_stream,
    )
    await hub.spawn_agent(name="error-agent", cwd="/tmp/error-agent")

    result = await hub.submit_user_message("error-agent", "hello")
    assert result.status == "started"
    await asyncio.sleep(0.05)

    session = hub.get_session("error-agent")
    assert session is not None
    assert session.state.lifecycle in {session.state.lifecycle.IDLE, session.state.lifecycle.SLEEPING}
    assert session.state.current_turn is None


@pytest.mark.asyncio
async def test_transient_error_marks_retry_exhausted(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)

    async def transient_stream(session: Any, **kwargs: Any):
        yield StreamStart()
        yield TransientError(error_type="overloaded", error_text="retry later")
        yield StreamEnd(elapsed_s=0.01, msg_count=1, flush_count=0)

    hub = AgentHub(
        frontends=[router],
        create_client=lambda session, options: FakeClient(session.name),
        disconnect_client=lambda client, name: asyncio.sleep(0),
        make_agent_options=lambda session, sid: {},
        stream_factory=transient_stream,
    )
    await hub.spawn_agent(name="transient-agent", cwd="/tmp/transient-agent")

    result = await hub.submit_user_message("transient-agent", "hello")
    assert result.status == "started"
    await asyncio.sleep(0.05)

    session = hub.get_session("transient-agent")
    assert session is not None
    assert session.state.lifecycle in {session.state.lifecycle.IDLE, session.state.lifecycle.SLEEPING}
    assert session.state.current_turn is None


@pytest.mark.asyncio
async def test_rate_limit_hit_updates_tracker_state(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)

    async def create_client(session: Any, options: Any) -> FakeClient:
        return FakeClient(session.name)

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    async def rate_limit_stream(session: Any, **kwargs: Any):
        yield StreamStart()
        yield RateLimitHit(error_type="rate_limit", error_text="retry after 12 seconds")
        yield StreamEnd(elapsed_s=0.01, msg_count=1, flush_count=0)

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
        stream_factory=rate_limit_stream,
    )
    await hub.spawn_agent(name="rl-agent", cwd="/tmp/rl-agent")

    result = await hub.submit_user_message("rl-agent", "hello")
    assert result.status == "started"
    await asyncio.sleep(0.05)

    assert hub.rate_limits.rate_limited_until is not None
    session = hub.get_session("rl-agent")
    assert session is not None
    assert session.state.lifecycle in {session.state.lifecycle.IDLE, session.state.lifecycle.SLEEPING}


@pytest.mark.asyncio
async def test_on_session_id_is_forwarded_to_frontend(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)
    hub = AgentHub(
        frontends=[router],
        create_client=lambda session, options: FakeClient(session.name),
        disconnect_client=lambda client, name: asyncio.sleep(0),
        make_agent_options=lambda session, sid: {},
    )
    session = await hub.spawn_agent(name="sid-agent", cwd="/tmp/sid-agent")

    await hub._set_session_id_from_stream(session, SimpleNamespace(session_id="sid-123"))

    assert session.session_id == "sid-123"
    assert ("on_session_id", ("sid-agent", "sid-123")) in frontend.calls


@pytest.mark.asyncio
async def test_wake_sleep_and_snapshot_public_methods(frontend: FakeFrontend) -> None:
    router = FrontendRouter()
    router.add(frontend)

    async def create_client(session: Any, options: Any) -> FakeClient:
        return FakeClient(session.name)

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
    )
    session = await hub.spawn_agent(name="public-agent", cwd="/tmp/public-agent")

    snapshot = hub.snapshot()
    assert snapshot["public-agent"] is session

    await hub.wake("public-agent")
    assert session.client is not None
    assert ("on_wake", ("public-agent",)) in frontend.calls

    await hub.sleep("public-agent")
    assert session.client is None
    assert session.state.lifecycle is session.state.lifecycle.SLEEPING
    assert ("on_sleep", ("public-agent",)) in frontend.calls


@given(st.lists(st.sampled_from(["one", "two", "three"]), min_size=1, max_size=6))
@pytest.mark.asyncio
async def test_submit_sequence_keeps_fifo_order_under_runtime(turns: list[str]) -> None:
    router = FrontendRouter()
    frontend = FakeFrontend("fake")
    router.add(frontend)
    gate = asyncio.Event()

    class WaitingClient(FakeClient):
        async def query(self, content: Any) -> None:
            self.queries.append(content)
            await gate.wait()

    client_ref: WaitingClient | None = None

    async def create_client(session: Any, options: Any) -> FakeClient:
        nonlocal client_ref
        client_ref = WaitingClient(session.name, mode="wait")
        return client_ref

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
        stream_factory=result_stream,
    )
    await hub.spawn_agent(name="fifo-agent", cwd="/tmp/fifo-agent")

    results = [await hub.submit_user_message("fifo-agent", turn) for turn in turns]

    assert results[0].status == "started"
    session = hub.get_session("fifo-agent")
    assert session is not None
    assert [queued.content for queued in session.state.queued_turns] == turns[1:]

    gate.set()
    await asyncio.sleep(0.15)

    assert client_ref is not None
    assert client_ref.queries == turns
    assert session.state.current_turn is None
    assert len(session.state.queued_turns) == 0


@given(st.lists(st.sampled_from(["a", "b", "c", "d"]), min_size=2, max_size=5))
@pytest.mark.asyncio
async def test_skip_preserves_followup_progression_under_runtime(turns: list[str]) -> None:
    router = FrontendRouter()
    frontend = FakeFrontend("fake")
    router.add(frontend)
    gate = asyncio.Event()

    class WaitingClient(FakeClient):
        async def query(self, content: Any) -> None:
            self.queries.append(content)
            await gate.wait()

    client_ref: WaitingClient | None = None

    async def create_client(session: Any, options: Any) -> FakeClient:
        nonlocal client_ref
        client_ref = WaitingClient(session.name, mode="wait")
        return client_ref

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
        stream_factory=result_stream,
    )
    await hub.spawn_agent(name="skip-fifo-agent", cwd="/tmp/skip-fifo-agent")

    for turn in turns:
        await hub.submit_user_message("skip-fifo-agent", turn)

    skipped = await hub.request_skip("skip-fifo-agent")
    assert skipped.status == "stopping"
    gate.set()
    await asyncio.sleep(0.15)

    session = hub.get_session("skip-fifo-agent")
    assert session is not None
    assert client_ref is not None
    assert client_ref.queries == turns
    assert session.state.current_turn is None
    assert len(session.state.queued_turns) == 0
    assert session.state.skip_requested is False
    assert TurnKind.USER.value == "user"
    assert TurnOutcome.COMPLETED.value == "completed"
    assert isinstance(StreamStart(), StreamStart)
    assert isinstance(StreamKilled(), StreamKilled)
    assert isinstance(QueryResult(session_id="s", cost_usd=0.0, num_turns=1, duration_ms=1), QueryResult)


_RUNTIME_CONTROL_ACTIONS = st.lists(
    st.sampled_from(["stop_clear", "stop_keep", "skip"]),
    min_size=1,
    max_size=4,
)


@given(actions=_RUNTIME_CONTROL_ACTIONS)
@pytest.mark.asyncio
async def test_runtime_control_requests_leave_consistent_settled_state(
    actions: list[str],
) -> None:
    router = FrontendRouter()
    frontend = FakeFrontend("fake")
    router.add(frontend)
    gate = asyncio.Event()

    class WaitingClient(FakeClient):
        async def query(self, content: Any) -> None:
            self.queries.append(content)
            await gate.wait()

    client_ref: WaitingClient | None = None

    async def create_client(session: Any, options: Any) -> FakeClient:
        nonlocal client_ref
        client_ref = WaitingClient(session.name, mode="wait")
        return client_ref

    async def disconnect_client(client: Any, name: str) -> None:
        return None

    hub = AgentHub(
        frontends=[router],
        create_client=create_client,
        disconnect_client=disconnect_client,
        make_agent_options=lambda session, sid: {},
        stream_factory=result_stream,
    )
    await hub.spawn_agent(name="control-agent", cwd="/tmp/control-agent")

    started = await hub.submit_user_message("control-agent", "first")
    queued = await hub.submit_user_message("control-agent", "second")
    assert started.status == "started"
    assert queued.status == "queued"

    results: list[StopResult] = []
    for action in actions:
        if action == "stop_clear":
            results.append(await hub.request_stop("control-agent", clear_queue=True))
        elif action == "stop_keep":
            results.append(await hub.request_stop("control-agent", clear_queue=False))
        else:
            results.append(await hub.request_skip("control-agent"))

    gate.set()
    await asyncio.sleep(0.15)

    session = hub.get_session("control-agent")
    assert session is not None
    assert client_ref is not None
    assert results
    assert all(result.status == "stopping" for result in results)
    assert session.state.current_turn is None
    assert session.query_task is None
    assert session.client is None
    assert session.state.lifecycle is session.state.lifecycle.SLEEPING
    assert session.state.stop_requested is False
    assert session.state.skip_requested is False

    all_skips = all(action == "skip" for action in actions)
    expected_queries = ["first", "second"] if all_skips else ["first"]
    assert client_ref.queries == expected_queries

    expected_queue = ["second"] if (not all_skips and "stop_keep" in actions and "stop_clear" not in actions) else []
    assert [turn.content for turn in session.state.queued_turns] == expected_queue
    assert max(result.cleared for result in results) <= 1

    followup_clear = await hub.request_stop("control-agent", clear_queue=True)
    assert followup_clear.status == "stopping"
    assert followup_clear.cleared == len(expected_queue)


@given(actions=_RUNTIME_CONTROL_ACTIONS)
@pytest.mark.asyncio
async def test_runtime_control_requests_after_terminal_turn_are_idempotent(actions: list[str]) -> None:
    router = FrontendRouter()
    frontend = FakeFrontend("fake")
    router.add(frontend)

    hub = AgentHub(
        frontends=[router],
        create_client=lambda session, options: FakeClient(session.name),
        disconnect_client=lambda client, name: asyncio.sleep(0),
        make_agent_options=lambda session, sid: {},
        stream_factory=result_stream,
    )
    await hub.spawn_agent(name="post-terminal-agent", cwd="/tmp/post-terminal-agent")

    started = await hub.submit_user_message("post-terminal-agent", "done")
    assert started.status == "started"
    await asyncio.sleep(0.1)

    session = hub.get_session("post-terminal-agent")
    assert session is not None
    assert session.state.current_turn is None
    assert len(session.state.queued_turns) == 0

    results: list[StopResult] = []
    for action in actions:
        if action == "stop_clear":
            results.append(await hub.request_stop("post-terminal-agent", clear_queue=True))
        elif action == "stop_keep":
            results.append(await hub.request_stop("post-terminal-agent", clear_queue=False))
        else:
            results.append(await hub.request_skip("post-terminal-agent"))

    assert results
    assert all(result.status == "stopping" for result in results)
    assert all(result.cleared == 0 for result in results)
    assert session.state.current_turn is None
    assert len(session.state.queued_turns) == 0
    assert session.query_task is None
    assert session.client is None
    assert session.state.lifecycle is session.state.lifecycle.SLEEPING
