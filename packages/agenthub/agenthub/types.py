"""Core data models for the rewritten AgentHub runtime."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from claudewire.events import ActivityState

MessageContent = str | list[dict[str, Any]]


class LifecycleState(StrEnum):
    SLEEPING = "sleeping"
    WAKING = "waking"
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    RECONNECTING = "reconnecting"


class TurnKind(StrEnum):
    USER = "user"
    INTER_AGENT = "inter_agent"


class TurnOutcome(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    KILLED = "killed"
    TIMEOUT = "timeout"
    ERROR = "error"
    RATE_LIMIT = "rate_limit"
    RETRY_EXHAUSTED = "retry_exhausted"


@dataclass(slots=True)
class TurnRequest:
    turn_id: str
    kind: TurnKind
    content: MessageContent
    metadata: Any = None
    source: str = ""
    submitted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    interrupt_requested: bool = False


@dataclass(slots=True)
class SessionState:
    lifecycle: LifecycleState = LifecycleState.SLEEPING
    current_turn: TurnRequest | None = None
    queued_turns: deque[TurnRequest] = field(default_factory=deque)
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    idle_reminder_count: int = 0
    session_id: str | None = None
    last_failed_resume_id: str | None = None
    activity: ActivityState = field(default_factory=ActivityState)
    stop_requested: bool = False
    skip_requested: bool = False
    compacting: bool = False
    bridge_busy: bool = False
    reconnecting: bool = False
    context_tokens: int = 0
    context_window: int = 0


@dataclass(slots=True)
class AgentSession:
    """One agent session managed by AgentHub.

    `state` is the authoritative synchronous control-plane state.
    `client` / `transport` / `query_task` are async runtime resources owned by
    the effect layer.

    `message_queue` and `plan_mode` are retained for Axi compatibility while
    the frontend layer is still mid-migration.
    """

    name: str
    agent_type: str = "claude_code"
    cwd: str = ""
    system_prompt: Any = None
    system_prompt_hash: str | None = None
    mcp_servers: dict[str, Any] | None = None
    mcp_server_names: list[str] | None = None
    frontend_state: Any = None
    compact_instructions: str | None = None
    startup_command: str | None = None
    startup_command_args: str = ""
    extra_excluded_commands: list[str] = field(default_factory=list)
    extra_write_dirs: list[str] = field(default_factory=list)
    model: str | None = None
    message_queue: deque[Any] = field(default_factory=deque)
    plan_mode: bool = False
    state: SessionState = field(default_factory=SessionState)
    client: Any = None
    transport: Any = None
    agent_log: Any = None
    query_task: asyncio.Task[None] | None = None
    dispatch_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    query_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def last_activity(self) -> datetime:
        return self.state.last_activity

    @last_activity.setter
    def last_activity(self, value: datetime) -> None:
        self.state.last_activity = value

    @property
    def idle_reminder_count(self) -> int:
        return self.state.idle_reminder_count

    @idle_reminder_count.setter
    def idle_reminder_count(self, value: int) -> None:
        self.state.idle_reminder_count = value

    @property
    def session_id(self) -> str | None:
        return self.state.session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        self.state.session_id = value

    @property
    def last_failed_resume_id(self) -> str | None:
        return self.state.last_failed_resume_id

    @last_failed_resume_id.setter
    def last_failed_resume_id(self, value: str | None) -> None:
        self.state.last_failed_resume_id = value

    @property
    def activity(self) -> ActivityState:
        return self.state.activity

    @activity.setter
    def activity(self, value: ActivityState) -> None:
        self.state.activity = value

    @property
    def compacting(self) -> bool:
        return self.state.compacting

    @compacting.setter
    def compacting(self, value: bool) -> None:
        self.state.compacting = value

    @property
    def bridge_busy(self) -> bool:
        return self.state.bridge_busy

    @bridge_busy.setter
    def bridge_busy(self, value: bool) -> None:
        self.state.bridge_busy = value

    @property
    def reconnecting(self) -> bool:
        return self.state.reconnecting

    @reconnecting.setter
    def reconnecting(self, value: bool) -> None:
        self.state.reconnecting = value

    @property
    def context_tokens(self) -> int:
        return self.state.context_tokens

    @context_tokens.setter
    def context_tokens(self, value: int) -> None:
        self.state.context_tokens = value

    @property
    def context_window(self) -> int:
        return self.state.context_window

    @context_window.setter
    def context_window(self, value: int) -> None:
        self.state.context_window = value


@dataclass(slots=True)
class SubmissionResult:
    status: str
    turn_id: str | None = None
    position: int | None = None
    message: str = ""


@dataclass(slots=True)
class StopResult:
    status: str
    cleared: int = 0
    message: str = ""


@dataclass(slots=True)
class SessionUsage:
    agent_name: str
    queries: int = 0
    total_cost_usd: float = 0.0
    total_turns: int = 0
    total_duration_ms: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    first_query: datetime | None = None
    last_query: datetime | None = None


@dataclass(slots=True)
class RateLimitQuota:
    status: str
    resets_at: datetime
    rate_limit_type: str
    utilization: float | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ConcurrencyLimitError(Exception):
    """Raised when the awake-agent concurrency limit cannot be satisfied."""
