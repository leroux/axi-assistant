"""Tests for receive_user_message — centralized message ingestion."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from agenthub.messaging import ReceiveResult, receive_user_message

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class FakeActivity:
    phase: str = "idle"
    query_started: datetime | None = None


@dataclass
class FakeSession:
    name: str = "test-agent"
    agent_type: str = "claude_code"
    client: Any = None
    query_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    message_queue: deque[Any] = field(default_factory=deque)
    reconnecting: bool = False
    bridge_busy: bool = False
    activity: Any = field(default_factory=FakeActivity)
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    idle_reminder_count: int = 0
    agent_log: Any = None
    plan_mode: bool = False
    session_id: str | None = None


class FakeCallbacks:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def post_system(self, agent_name: str, text: str) -> None:
        self.messages.append((agent_name, text))

    async def post_message(self, agent_name: str, text: str) -> None:
        self.messages.append((agent_name, text))


class FakeScheduler:
    def __init__(self) -> None:
        self._should_yield = False

    def mark_interactive(self, name: str) -> None:
        pass

    def should_yield(self, name: str) -> bool:
        return self._should_yield


class FakeHub:
    def __init__(self) -> None:
        self.callbacks = FakeCallbacks()
        self.scheduler = FakeScheduler()
        self.shutdown_requested = False
        self.sessions: dict[str, Any] = {}
        self.query_timeout = 300.0
        self.max_retries = 3
        self.retry_base_delay = 15.0
        self.process_conn = None
        self.wake_lock = asyncio.Lock()
        self.tasks = type("T", (), {"fire_and_forget": lambda self, c: None})()


# A stream handler that always succeeds
async def _ok_handler(session: Any) -> str | None:
    return None


# A stream handler that returns a transient error
async def _error_handler(session: Any) -> str | None:
    return "transient API error"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReceiveUserMessage:
    @pytest.mark.asyncio
    async def test_shutdown_rejected(self) -> None:
        hub = FakeHub()
        hub.shutdown_requested = True
        session = FakeSession(client=object())

        result = await receive_user_message(hub, session, "hello", _ok_handler)

        assert result.status == "shutdown"
        assert any("restarting" in text for _, text in hub.callbacks.messages)

    @pytest.mark.asyncio
    async def test_reconnecting_queued(self) -> None:
        hub = FakeHub()
        session = FakeSession(client=object(), reconnecting=True)

        result = await receive_user_message(hub, session, "hello", _ok_handler)

        assert result.status == "queued_reconnecting"
        assert len(session.message_queue) == 1
        # Default queue item is (content, None)
        assert session.message_queue[0] == ("hello", None)

    @pytest.mark.asyncio
    async def test_reconnecting_custom_queue_item(self) -> None:
        hub = FakeHub()
        session = FakeSession(client=object(), reconnecting=True)

        result = await receive_user_message(
            hub, session, "hello", _ok_handler,
            queue_item=("hello", "channel", "message"),
        )

        assert result.status == "queued_reconnecting"
        assert session.message_queue[0] == ("hello", "channel", "message")

    @pytest.mark.asyncio
    async def test_busy_queued(self) -> None:
        hub = FakeHub()
        session = FakeSession(client=object())

        # Lock the query_lock to simulate a busy agent
        await session.query_lock.acquire()
        try:
            result = await receive_user_message(hub, session, "hello", _ok_handler)
        finally:
            session.query_lock.release()

        assert result.status == "queued"
        assert len(session.message_queue) == 1
        assert any("busy" in text.lower() for _, text in hub.callbacks.messages)

    @pytest.mark.asyncio
    async def test_processed_successfully(self) -> None:
        """Agent is awake, not busy — message is processed."""
        hub = FakeHub()
        session = FakeSession(client=object())
        processed = []

        async def handler(s: Any) -> str | None:
            processed.append(s.name)
            return None

        # Mock the hub-level process_message
        import agenthub.messaging as messaging_mod

        original_process = messaging_mod.process_message

        async def mock_process(h: Any, s: Any, content: Any, sh: Any) -> None:
            await sh(s)

        messaging_mod.process_message = mock_process  # type: ignore[assignment]
        try:
            result = await receive_user_message(hub, session, "hello", handler)
        finally:
            messaging_mod.process_message = original_process

        assert result.status == "processed"
        assert processed == ["test-agent"]
        assert session.activity.phase == "idle"

    @pytest.mark.asyncio
    async def test_timeout_handled(self) -> None:
        """TimeoutError is caught and results in 'timeout' status."""
        hub = FakeHub()
        session = FakeSession(client=object())

        import agenthub.messaging as messaging_mod

        original_process = messaging_mod.process_message
        original_timeout = messaging_mod.handle_query_timeout
        timeout_handled = []

        async def mock_process(h: Any, s: Any, content: Any, sh: Any) -> None:
            raise TimeoutError

        async def mock_handle_timeout(h: Any, s: Any) -> None:
            timeout_handled.append(s.name)

        messaging_mod.process_message = mock_process  # type: ignore[assignment]
        messaging_mod.handle_query_timeout = mock_handle_timeout  # type: ignore[assignment]
        try:
            result = await receive_user_message(hub, session, "hello", _ok_handler)
        finally:
            messaging_mod.process_message = original_process
            messaging_mod.handle_query_timeout = original_timeout

        assert result.status == "timeout"
        assert timeout_handled == ["test-agent"]

    @pytest.mark.asyncio
    async def test_runtime_error_handled(self) -> None:
        """RuntimeError is caught and results in 'error' status."""
        hub = FakeHub()
        session = FakeSession(client=object())

        import agenthub.messaging as messaging_mod

        original_process = messaging_mod.process_message

        async def mock_process(h: Any, s: Any, content: Any, sh: Any) -> None:
            raise RuntimeError("test error")

        messaging_mod.process_message = mock_process  # type: ignore[assignment]
        try:
            result = await receive_user_message(hub, session, "hello", _ok_handler)
        finally:
            messaging_mod.process_message = original_process

        assert result.status == "error"
        assert result.error == "test error"
        assert any("test error" in text for _, text in hub.callbacks.messages)

    @pytest.mark.asyncio
    async def test_generic_exception_handled(self) -> None:
        """Generic Exception is caught and results in 'error' status."""
        hub = FakeHub()
        session = FakeSession(client=object())

        import agenthub.messaging as messaging_mod

        original_process = messaging_mod.process_message

        async def mock_process(h: Any, s: Any, content: Any, sh: Any) -> None:
            raise ValueError("unexpected")

        messaging_mod.process_message = mock_process  # type: ignore[assignment]
        try:
            result = await receive_user_message(hub, session, "hello", _ok_handler)
        finally:
            messaging_mod.process_message = original_process

        assert result.status == "error"
        assert "unexpected" in (result.error or "")

    @pytest.mark.asyncio
    async def test_activity_reset_on_completion(self) -> None:
        """activity is set to idle after processing, even on error."""
        hub = FakeHub()
        session = FakeSession(client=object())

        import agenthub.messaging as messaging_mod

        original_process = messaging_mod.process_message

        async def mock_process(h: Any, s: Any, content: Any, sh: Any) -> None:
            # Verify activity was set to "starting" during processing
            assert s.activity.phase == "starting"
            raise RuntimeError("boom")

        messaging_mod.process_message = mock_process  # type: ignore[assignment]
        try:
            await receive_user_message(hub, session, "hello", _ok_handler)
        finally:
            messaging_mod.process_message = original_process

        assert session.activity.phase == "idle"

    @pytest.mark.asyncio
    async def test_wake_agent_called_when_sleeping(self) -> None:
        """If agent is not awake, wake_agent is called."""
        hub = FakeHub()
        session = FakeSession(client=None)  # sleeping: client is None
        woken = []

        import agenthub.lifecycle as lifecycle_mod
        import agenthub.messaging as messaging_mod

        original_wake = lifecycle_mod.wake_agent
        original_process = messaging_mod.process_message
        original_is_awake = lifecycle_mod.is_awake

        async def mock_wake(h: Any, s: Any) -> None:
            woken.append(s.name)
            s.client = object()  # simulate waking

        def mock_is_awake(s: Any) -> bool:
            return s.client is not None

        async def mock_process(h: Any, s: Any, content: Any, sh: Any) -> None:
            pass

        lifecycle_mod.wake_agent = mock_wake  # type: ignore[assignment]
        lifecycle_mod.is_awake = mock_is_awake  # type: ignore[assignment]
        messaging_mod.process_message = mock_process  # type: ignore[assignment]
        try:
            result = await receive_user_message(hub, session, "hello", _ok_handler)
        finally:
            lifecycle_mod.wake_agent = original_wake
            lifecycle_mod.is_awake = original_is_awake
            messaging_mod.process_message = original_process

        assert result.status == "processed"
        assert woken == ["test-agent"]

    @pytest.mark.asyncio
    async def test_concurrency_limit_queues(self) -> None:
        """ConcurrencyLimitError during wake queues the message."""
        from agenthub.types import ConcurrencyLimitError

        hub = FakeHub()
        session = FakeSession(client=None)  # sleeping

        import agenthub.lifecycle as lifecycle_mod

        original_wake = lifecycle_mod.wake_agent
        original_is_awake = lifecycle_mod.is_awake

        async def mock_wake(h: Any, s: Any) -> None:
            raise ConcurrencyLimitError("no slots")

        def mock_is_awake(s: Any) -> bool:
            return s.client is not None

        lifecycle_mod.wake_agent = mock_wake  # type: ignore[assignment]
        lifecycle_mod.is_awake = mock_is_awake  # type: ignore[assignment]
        try:
            result = await receive_user_message(hub, session, "hello", _ok_handler)
        finally:
            lifecycle_mod.wake_agent = original_wake
            lifecycle_mod.is_awake = original_is_awake

        assert result.status == "queued"
        assert len(session.message_queue) == 1

    @pytest.mark.asyncio
    async def test_wake_failure_returns_error(self) -> None:
        """Exception during wake results in 'error' status."""
        hub = FakeHub()
        session = FakeSession(client=None)

        import agenthub.lifecycle as lifecycle_mod

        original_wake = lifecycle_mod.wake_agent
        original_is_awake = lifecycle_mod.is_awake

        async def mock_wake(h: Any, s: Any) -> None:
            raise RuntimeError("wake failed")

        def mock_is_awake(s: Any) -> bool:
            return s.client is not None

        lifecycle_mod.wake_agent = mock_wake  # type: ignore[assignment]
        lifecycle_mod.is_awake = mock_is_awake  # type: ignore[assignment]
        try:
            result = await receive_user_message(hub, session, "hello", _ok_handler)
        finally:
            lifecycle_mod.wake_agent = original_wake
            lifecycle_mod.is_awake = original_is_awake

        assert result.status == "error"
        assert "wake" in (result.error or "").lower()


class TestReceiveResult:
    def test_defaults(self) -> None:
        r = ReceiveResult(status="processed")
        assert r.status == "processed"
        assert r.error is None

    def test_with_error(self) -> None:
        r = ReceiveResult(status="error", error="something broke")
        assert r.status == "error"
        assert r.error == "something broke"
