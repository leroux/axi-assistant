"""Unit tests for the new session reducer."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from agenthub.session_core import reduce_session
from agenthub.session_events import (
    SkipRequested,
    StopRequested,
    StreamEventReceived,
    SubmitTurn,
    TurnFinished,
    WakeCompleted,
    WakeFailed,
)
from agenthub.stream_types import QueryResult, RateLimitHit, StreamKilled
from agenthub.types import (
    LifecycleState,
    SessionState,
    TurnKind,
    TurnOutcome,
)


def _submit(kind: TurnKind, turn_id: str = "t1") -> SubmitTurn:
    return SubmitTurn(
        agent_name="agent",
        kind=kind,
        content="hello",
        metadata={"turn_id": turn_id},
        source="test",
    )


def test_submit_first_turn_starts_running_path() -> None:
    state = SessionState(lifecycle=LifecycleState.SLEEPING)
    new_state = reduce_session(state, _submit(TurnKind.USER))
    assert new_state.current_turn is not None
    assert new_state.current_turn.turn_id == "t1"
    assert new_state.lifecycle is LifecycleState.WAKING


def test_submit_second_turn_queues() -> None:
    state = SessionState(
        lifecycle=LifecycleState.RUNNING,
        current_turn=reduce_session(SessionState(), _submit(TurnKind.USER)).current_turn,
    )
    new_state = reduce_session(state, _submit(TurnKind.USER, turn_id="t2"))
    assert len(new_state.queued_turns) == 1
    assert new_state.queued_turns[0].turn_id == "t2"


def test_wake_completed_sets_running_when_turn_present() -> None:
    state = reduce_session(SessionState(lifecycle=LifecycleState.SLEEPING), _submit(TurnKind.USER))
    new_state = reduce_session(state, WakeCompleted(agent_name="agent"))
    assert new_state.lifecycle is LifecycleState.RUNNING


def test_wake_failed_clears_current_turn() -> None:
    state = reduce_session(SessionState(lifecycle=LifecycleState.SLEEPING), _submit(TurnKind.USER))
    new_state = reduce_session(state, WakeFailed(agent_name="agent", error="boom"))
    assert new_state.lifecycle is LifecycleState.SLEEPING
    assert new_state.current_turn is None


def test_stop_clears_queue() -> None:
    state = reduce_session(SessionState(lifecycle=LifecycleState.SLEEPING), _submit(TurnKind.USER))
    state.queued_turns.append(state.current_turn)  # type: ignore[arg-type]
    new_state = reduce_session(state, StopRequested(agent_name="agent", clear_queue=True))
    assert new_state.lifecycle is LifecycleState.STOPPING
    assert len(new_state.queued_turns) == 0
    assert new_state.current_turn is not None
    assert new_state.current_turn.interrupt_requested is True


def test_skip_sets_stopping_without_clearing_queue() -> None:
    state = reduce_session(SessionState(lifecycle=LifecycleState.SLEEPING), _submit(TurnKind.USER))
    queued = _submit(TurnKind.USER, turn_id="t2")
    state.queued_turns.append(
        type(state.current_turn)(
            turn_id=queued.metadata["turn_id"],
            kind=queued.kind,
            content=queued.content,
            metadata=queued.metadata,
            source=queued.source,
        )
    )
    new_state = reduce_session(state, SkipRequested(agent_name="agent"))
    assert new_state.lifecycle is LifecycleState.STOPPING
    assert len(new_state.queued_turns) == 1
    assert new_state.skip_requested is True


def test_stream_killed_moves_to_stopping() -> None:
    state = reduce_session(SessionState(lifecycle=LifecycleState.SLEEPING), _submit(TurnKind.USER))
    new_state = reduce_session(
        state,
        StreamEventReceived(agent_name="agent", turn_id="t1", event=StreamKilled()),
    )
    assert new_state.lifecycle is LifecycleState.STOPPING


def test_query_result_updates_session_id() -> None:
    state = reduce_session(SessionState(lifecycle=LifecycleState.SLEEPING), _submit(TurnKind.USER))
    new_state = reduce_session(
        state,
        StreamEventReceived(
            agent_name="agent",
            turn_id="t1",
            event=QueryResult(session_id="sid-1", cost_usd=0.1, num_turns=1, duration_ms=10),
        ),
    )
    assert new_state.session_id == "sid-1"


def test_rate_limit_hits_idle() -> None:
    state = SessionState(lifecycle=LifecycleState.RUNNING)
    new_state = reduce_session(
        state,
        StreamEventReceived(
            agent_name="agent",
            turn_id="t1",
            event=RateLimitHit(error_type="rate_limit", error_text="wait"),
        ),
    )
    assert new_state.lifecycle is LifecycleState.IDLE


def test_turn_finished_goes_idle_when_no_queue() -> None:
    state = reduce_session(SessionState(lifecycle=LifecycleState.SLEEPING), _submit(TurnKind.USER, "t1"))
    state = reduce_session(state, WakeCompleted(agent_name="agent"))
    new_state = reduce_session(
        state,
        TurnFinished(agent_name="agent", turn_id="t1", outcome=TurnOutcome.COMPLETED),
    )
    assert new_state.current_turn is None
    assert new_state.lifecycle is LifecycleState.IDLE


def test_turn_finished_advances_next_queued_turn() -> None:
    state = reduce_session(SessionState(lifecycle=LifecycleState.SLEEPING), _submit(TurnKind.USER, "t1"))
    state = reduce_session(state, WakeCompleted(agent_name="agent"))
    state = reduce_session(state, _submit(TurnKind.USER, "t2"))
    new_state = reduce_session(
        state,
        TurnFinished(agent_name="agent", turn_id="t1", outcome=TurnOutcome.COMPLETED),
    )
    assert new_state.current_turn is not None
    assert new_state.current_turn.turn_id == "t2"
    assert new_state.lifecycle is LifecycleState.WAKING


def test_turn_finished_after_stop_goes_sleeping() -> None:
    state = reduce_session(SessionState(lifecycle=LifecycleState.SLEEPING), _submit(TurnKind.USER, "t1"))
    state = reduce_session(state, StopRequested(agent_name="agent", clear_queue=True))
    new_state = reduce_session(
        state,
        TurnFinished(agent_name="agent", turn_id="t1", outcome=TurnOutcome.INTERRUPTED),
    )
    assert new_state.lifecycle is LifecycleState.SLEEPING
    assert new_state.stop_requested is False
    assert new_state.skip_requested is False


@given(st.lists(st.sampled_from(["t1", "t2", "t3"]), min_size=1, max_size=10))
def test_submit_only_sequences_preserve_fifo_order(turn_ids: list[str]) -> None:
    state = SessionState(lifecycle=LifecycleState.SLEEPING)
    for turn_id in turn_ids:
        state = reduce_session(state, _submit(TurnKind.USER, turn_id))

    if turn_ids:
        assert state.current_turn is not None
        assert state.current_turn.turn_id == turn_ids[0]
        assert [turn.turn_id for turn in state.queued_turns] == turn_ids[1:]


@given(st.sampled_from(["t1", "t2", "t3"]))
def test_query_result_event_is_only_source_of_session_id(turn_id: str) -> None:
    state = SessionState(lifecycle=LifecycleState.SLEEPING)
    state = reduce_session(state, _submit(TurnKind.USER, turn_id))
    assert state.session_id is None

    state = reduce_session(
        state,
        StreamEventReceived(
            agent_name="agent",
            turn_id=turn_id,
            event=QueryResult(session_id=f"sid-{turn_id}", cost_usd=0.1, num_turns=1, duration_ms=10),
        ),
    )
    assert state.session_id == f"sid-{turn_id}"


@given(st.sampled_from(["t1", "t2", "t3"]))
def test_stop_clear_with_current_turn_marks_interrupt_and_clears_queue(turn_id: str) -> None:
    state = SessionState(lifecycle=LifecycleState.SLEEPING)
    state = reduce_session(state, _submit(TurnKind.USER, turn_id))
    state = reduce_session(state, _submit(TurnKind.USER, "t2" if turn_id != "t2" else "t3"))

    state = reduce_session(state, StopRequested(agent_name="agent", clear_queue=True))

    assert state.stop_requested is True
    assert state.current_turn is not None
    assert state.current_turn.interrupt_requested is True
    assert len(state.queued_turns) == 0


@given(st.sampled_from(["t1", "t2", "t3"]))
def test_skip_with_current_turn_marks_interrupt_without_clearing_queue(turn_id: str) -> None:
    queued_id = "t2" if turn_id != "t2" else "t3"
    state = SessionState(lifecycle=LifecycleState.SLEEPING)
    state = reduce_session(state, _submit(TurnKind.USER, turn_id))
    state = reduce_session(state, _submit(TurnKind.USER, queued_id))

    state = reduce_session(state, SkipRequested(agent_name="agent"))

    assert state.skip_requested is True
    assert state.current_turn is not None
    assert state.current_turn.interrupt_requested is True
    assert [turn.turn_id for turn in state.queued_turns] == [queued_id]
