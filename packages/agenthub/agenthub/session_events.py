"""Core control-plane events for the rewritten AgentHub runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agenthub.stream_types import StreamOutput
    from agenthub.types import TurnKind, TurnOutcome


@dataclass(slots=True)
class SubmitTurn:
    agent_name: str
    kind: TurnKind
    content: Any
    metadata: Any = None
    source: str = ""


@dataclass(slots=True)
class WakeCompleted:
    agent_name: str


@dataclass(slots=True)
class WakeFailed:
    agent_name: str
    error: str


@dataclass(slots=True)
class StopRequested:
    agent_name: str
    clear_queue: bool = True


@dataclass(slots=True)
class SkipRequested:
    agent_name: str


@dataclass(slots=True)
class StreamEventReceived:
    agent_name: str
    turn_id: str
    event: StreamOutput


@dataclass(slots=True)
class TurnFinished:
    agent_name: str
    turn_id: str
    outcome: TurnOutcome
    error: str = ""


SessionEvent = (
    SubmitTurn
    | WakeCompleted
    | WakeFailed
    | StopRequested
    | SkipRequested
    | StreamEventReceived
    | TurnFinished
)
