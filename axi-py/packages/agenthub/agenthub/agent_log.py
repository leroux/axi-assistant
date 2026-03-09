"""Per-agent append-only event log — source of truth for all frontends.

Every significant event (user message, assistant response, tool use, system
notification, plan approval, etc.) is recorded as a LogEvent. Frontends
subscribe for real-time push and can replay history for catch-up.

No frontend-specific code here. LogEvent.data carries structured metadata
that each frontend interprets in its own way.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LogEvent — one entry in the agent's event stream
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LogEvent:
    """A single event in an agent's message log.

    kind values:
        user            — user message (text or content blocks)
        assistant       — assistant text (complete, post-stream)
        text_delta      — streaming text fragment
        system          — system notification (spawn, wake, sleep, error, etc.)
        tool_use        — tool invocation (name + preview in data)
        thinking_start  — model started extended thinking
        thinking_end    — model finished extended thinking
        stream_start    — response stream opened
        stream_end      — response stream closed (duration/cost in data)
        plan_request    — agent requested plan approval
        plan_result     — user approved/rejected plan
        question        — agent asked user a question
        answer          — user answered a question
        todo_update     — todo list changed
        error           — error during processing
        rate_limit      — rate limit event
    """

    ts: datetime
    kind: str
    agent: str
    text: str = ""
    source: str = ""  # which frontend originated it ("discord", "web", "")
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Subscriber protocol
# ---------------------------------------------------------------------------


class LogSubscriber(Protocol):
    """Callback invoked for each new LogEvent."""

    async def __call__(self, event: LogEvent) -> None: ...


# ---------------------------------------------------------------------------
# AgentLog — per-agent event store
# ---------------------------------------------------------------------------


class AgentLog:
    """Append-only event log for one agent. Source of truth for all frontends.

    Thread-safe: only accessed from the asyncio event loop (single-threaded).
    Persistence is optional JSONL append.
    """

    __slots__ = ("_persist_fd", "_persist_path", "agent_name", "events", "subscribers")

    def __init__(self, agent_name: str, persist_dir: str | None = None) -> None:
        self.agent_name = agent_name
        self.events: list[LogEvent] = []
        self.subscribers: list[LogSubscriber] = []
        self._persist_path: str | None = None
        self._persist_fd: Any = None

        if persist_dir:
            os.makedirs(persist_dir, exist_ok=True)
            self._persist_path = os.path.join(persist_dir, f"{agent_name}.events.jsonl")

    async def append(self, event: LogEvent) -> None:
        """Record an event and notify all subscribers."""
        self.events.append(event)
        self._persist(event)
        for sub in self.subscribers:
            try:
                await sub(event)
            except Exception:
                log.warning(
                    "Subscriber error for %s: %s",
                    self.agent_name,
                    type(sub).__name__,
                    exc_info=True,
                )

    def subscribe(self, callback: LogSubscriber) -> None:
        """Register a subscriber for new events."""
        if callback not in self.subscribers:
            self.subscribers.append(callback)

    def unsubscribe(self, callback: LogSubscriber) -> None:
        """Remove a subscriber."""
        try:
            self.subscribers.remove(callback)
        except ValueError:
            pass

    def replay(self, since: datetime | None = None) -> list[LogEvent]:
        """Return events for catch-up (e.g. web UI connecting to a running agent)."""
        if since is None:
            return list(self.events)
        return [e for e in self.events if e.ts >= since]

    def clear(self) -> None:
        """Clear in-memory events (persistence file is not truncated)."""
        self.events.clear()

    def _persist(self, event: LogEvent) -> None:
        """Append one event to the JSONL file."""
        if self._persist_path is None:
            return
        try:
            d = asdict(event)
            d["ts"] = event.ts.isoformat()
            with open(self._persist_path, "a") as f:
                f.write(json.dumps(d, default=str) + "\n")
        except Exception:
            log.warning("Failed to persist event for %s", self.agent_name, exc_info=True)

    def close(self) -> None:
        """Clean up resources."""
        self.subscribers.clear()


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------


def make_agent_log(agent_name: str, persist_dir: str | None = None) -> AgentLog:
    """Create an AgentLog, optionally with JSONL persistence."""
    return AgentLog(agent_name, persist_dir=persist_dir)


def make_event(
    kind: str,
    agent: str,
    text: str = "",
    source: str = "",
    **data: Any,
) -> LogEvent:
    """Convenience factory for LogEvent with auto-timestamping."""
    return LogEvent(
        ts=datetime.now(UTC),
        kind=kind,
        agent=agent,
        text=text,
        source=source,
        data=data or {},
    )
