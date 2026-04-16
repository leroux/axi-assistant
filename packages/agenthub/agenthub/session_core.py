"""Pure reducer for per-session control-plane state."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Any

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
    TurnRequest,
)


def reduce_session(
    state: SessionState,
    event: Any,
) -> SessionState:
    """Return the next pure control-plane state for one session."""
    new_state = replace(
        state,
        queued_turns=deque(state.queued_turns),
    )

    if isinstance(event, SubmitTurn):
        turn = TurnRequest(
            turn_id=event.metadata.get("turn_id") if isinstance(event.metadata, dict) and event.metadata.get("turn_id") else "",
            kind=event.kind,
            content=event.content,
            metadata=event.metadata,
            source=event.source,
        )
        if not turn.turn_id:
            raise ValueError("SubmitTurn requires metadata['turn_id']")
        if new_state.current_turn is None and new_state.lifecycle in {
            LifecycleState.SLEEPING,
            LifecycleState.IDLE,
        }:
            new_state.current_turn = turn
            new_state.lifecycle = LifecycleState.WAKING if state.lifecycle == LifecycleState.SLEEPING else LifecycleState.RUNNING
            return new_state
        new_state.queued_turns.append(turn)
        return new_state

    if isinstance(event, WakeCompleted):
        if new_state.current_turn is not None:
            new_state.lifecycle = LifecycleState.RUNNING
        else:
            new_state.lifecycle = LifecycleState.IDLE
        return new_state

    if isinstance(event, WakeFailed):
        new_state.current_turn = None
        new_state.lifecycle = LifecycleState.SLEEPING
        return new_state

    if isinstance(event, StopRequested):
        new_state.stop_requested = True
        new_state.skip_requested = False
        if new_state.current_turn is not None:
            new_state.current_turn.interrupt_requested = True
            new_state.lifecycle = LifecycleState.STOPPING
        if event.clear_queue:
            new_state.queued_turns.clear()
        return new_state

    if isinstance(event, SkipRequested):
        new_state.skip_requested = True
        if new_state.current_turn is not None:
            new_state.current_turn.interrupt_requested = True
            new_state.lifecycle = LifecycleState.STOPPING
        return new_state

    if isinstance(event, StreamEventReceived):
        stream_event = event.event
        if isinstance(stream_event, StreamKilled):
            new_state.lifecycle = LifecycleState.STOPPING
        elif isinstance(stream_event, QueryResult):
            new_state.session_id = stream_event.session_id or new_state.session_id
        elif isinstance(stream_event, RateLimitHit):
            new_state.lifecycle = LifecycleState.IDLE
        return new_state

    if isinstance(event, TurnFinished):
        new_state.current_turn = None

        if new_state.stop_requested:
            new_state.lifecycle = LifecycleState.SLEEPING
            new_state.stop_requested = False
            new_state.skip_requested = False
            return new_state

        if new_state.queued_turns:
            next_turn = new_state.queued_turns.popleft()
            new_state.current_turn = next_turn
            new_state.lifecycle = LifecycleState.WAKING
            return new_state

        new_state.lifecycle = LifecycleState.IDLE
        new_state.skip_requested = False
        return new_state

    return new_state
