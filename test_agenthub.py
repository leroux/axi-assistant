"""Integration test for AgentHub library.

Exercises the core async code paths with mock SDK factories and callbacks.
Validates lifecycle, registry, messaging, scheduler, and queue processing.

Run: PYTHONPATH=packages/agenthub:packages/procmux python test_agenthub.py
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

# --- AgentHub imports ---
from agenthub import (
    AgentHub,
    AgentSession,
    FrontendCallbacks,
    lifecycle,
    messaging,
    registry,
)

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("test")

# ---------------------------------------------------------------------------
# Mock SDK
# ---------------------------------------------------------------------------

class MockClient:
    """Simulates a ClaudeSDKClient."""

    def __init__(self, session_name: str):
        self.name = session_name
        self.queries: list[Any] = []
        self._interrupted = False

    async def query(self, content: Any) -> None:
        self.queries.append(content)

    async def interrupt(self) -> None:
        self._interrupted = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object):
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_system_messages: list[tuple[str, str]] = []
_wake_events: list[str] = []
_sleep_events: list[str] = []
_spawn_events: list[str] = []
_reconnect_events: list[tuple[str, bool]] = []


def make_callbacks() -> FrontendCallbacks:
    """Build FrontendCallbacks with simple recorders."""

    async def post_message(name: str, text: str) -> None:
        log.info("POST[%s]: %s", name, text[:80])

    async def post_system(name: str, text: str) -> None:
        _system_messages.append((name, text))
        log.info("SYSTEM[%s]: %s", name, text[:80])

    async def on_wake(name: str) -> None:
        _wake_events.append(name)

    async def on_sleep(name: str) -> None:
        _sleep_events.append(name)

    async def on_session_id(name: str, sid: str) -> None:
        pass

    async def get_channel(name: str) -> Any:
        return None

    async def on_spawn(session: Any) -> None:
        _spawn_events.append(session.name)

    async def on_kill(name: str, sid: str | None) -> None:
        pass

    async def broadcast(text: str) -> None:
        pass

    async def schedule_rate_limit_expiry(seconds: float) -> None:
        pass

    async def on_idle_reminder(name: str, idle_minutes: float) -> None:
        pass

    async def on_reconnect(name: str, was_mid_task: bool) -> None:
        _reconnect_events.append((name, was_mid_task))

    async def close_app() -> None:
        pass

    async def kill_process() -> None:
        pass

    return FrontendCallbacks(
        post_message=post_message,
        post_system=post_system,
        on_wake=on_wake,
        on_sleep=on_sleep,
        on_session_id=on_session_id,
        get_channel=get_channel,
        on_spawn=on_spawn,
        on_kill=on_kill,
        broadcast=broadcast,
        schedule_rate_limit_expiry=schedule_rate_limit_expiry,
        on_idle_reminder=on_idle_reminder,
        on_reconnect=on_reconnect,
        close_app=close_app,
        kill_process=kill_process,
    )


def make_hub(max_awake: int = 2) -> AgentHub:
    """Create a test AgentHub with mock SDK factories."""

    def make_options(session: AgentSession, resume_id: str | None) -> dict[str, Any]:
        return {"session": session.name, "resume": resume_id}

    async def create_client(session: AgentSession, options: Any) -> MockClient:
        return MockClient(session.name)

    async def disconnect_client(client: Any, name: str) -> None:
        log.info("DISCONNECT[%s]", name)

    return AgentHub(
        max_awake=max_awake,
        protected={"master"},
        callbacks=make_callbacks(),
        make_agent_options=make_options,
        create_client=create_client,
        disconnect_client=disconnect_client,
        query_timeout=10.0,
        max_retries=2,
        retry_base_delay=0.1,
        slot_timeout=1.0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        log.info("PASS  %s", name)
    else:
        failed += 1
        log.error("FAIL  %s %s", name, detail)


async def test_spawn_and_lifecycle() -> None:
    """Test: spawn -> wake -> sleep -> end."""
    hub = make_hub()
    _spawn_events.clear()
    _wake_events.clear()

    session = await registry.spawn_agent(hub, name="agent-1", cwd="/tmp/test")
    check("spawn creates session", "agent-1" in hub.sessions)
    check("spawn fires callback", "agent-1" in _spawn_events)
    check("session starts sleeping", session.client is None)
    check("is_awake=False initially", not lifecycle.is_awake(session))

    await lifecycle.wake_agent(hub, session)
    check("wake sets client", session.client is not None)
    check("is_awake=True after wake", lifecycle.is_awake(session))
    check("wake fires callback", "agent-1" in _wake_events)
    check("scheduler has slot", hub.scheduler.slot_count() == 1)

    # Waking again is a no-op
    old_client = session.client
    await lifecycle.wake_agent(hub, session)
    check("double wake is no-op", session.client is old_client)

    await lifecycle.sleep_agent(hub, session)
    check("sleep clears client", session.client is None)
    check("scheduler released slot", hub.scheduler.slot_count() == 0)

    await registry.end_session(hub, "agent-1")
    check("end removes session", "agent-1" not in hub.sessions)


async def test_wake_or_queue() -> None:
    """Test: wake_or_queue falls back to queuing on concurrency limit."""
    hub = make_hub(max_awake=1)

    s1 = await registry.spawn_agent(hub, name="master", cwd="/tmp/m")
    s2 = await registry.spawn_agent(hub, name="worker", cwd="/tmp/w")

    await lifecycle.wake_agent(hub, s1)
    check("master awake", lifecycle.is_awake(s1))

    # Lock master's query_lock so scheduler can't evict it
    async with s1.query_lock:
        result = await lifecycle.wake_or_queue(hub, s2, "hello", metadata={"test": True})
        check("wake_or_queue returns False (queued)", result is False)
        check("message queued", len(s2.message_queue) == 1)
        content, meta = s2.message_queue[0]
        check("queue content correct", content == "hello")
        check("queue metadata correct", meta == {"test": True})

    # Clean up
    await lifecycle.sleep_agent(hub, s1, force=True)
    await registry.end_session(hub, "master")
    await registry.end_session(hub, "worker")


async def test_scheduler_eviction() -> None:
    """Test: scheduler evicts idle agents when slots are full."""
    hub = make_hub(max_awake=1)

    s1 = await registry.spawn_agent(hub, name="idle-agent", cwd="/tmp/i")
    s2 = await registry.spawn_agent(hub, name="new-agent", cwd="/tmp/n")

    await lifecycle.wake_agent(hub, s1)
    check("idle-agent awake", lifecycle.is_awake(s1))

    # Wake s2 — should evict s1 (idle, not busy, not protected)
    await lifecycle.wake_agent(hub, s2)
    check("new-agent awake after eviction", lifecycle.is_awake(s2))
    check("idle-agent was evicted", not lifecycle.is_awake(s1))

    await lifecycle.sleep_agent(hub, s2)
    await registry.end_session(hub, "idle-agent")
    await registry.end_session(hub, "new-agent")


async def test_rebuild_and_reset() -> None:
    """Test: rebuild preserves frontend_state, reset works."""
    hub = make_hub()

    s = await registry.spawn_agent(
        hub, name="r-agent", cwd="/tmp/r", frontend_state={"channel_id": 123}
    )
    s.session_id = "old-sid"

    new_s = await registry.rebuild_session(hub, "r-agent", session_id="new-sid")
    check("rebuild preserves frontend_state", new_s.frontend_state == {"channel_id": 123})
    check("rebuild sets new session_id", new_s.session_id == "new-sid")
    check("rebuild preserves cwd", new_s.cwd == "/tmp/r")
    check("rebuild is sleeping", new_s.client is None)

    reset_s = await registry.reset_session(hub, "r-agent", cwd="/tmp/r2")
    check("reset changes cwd", reset_s.cwd == "/tmp/r2")

    await registry.end_session(hub, "r-agent")


async def test_reclaim() -> None:
    """Test: reclaim_agent_name kills existing session."""
    hub = make_hub()

    s = await registry.spawn_agent(hub, name="to-reclaim", cwd="/tmp/rc")
    await lifecycle.wake_agent(hub, s)
    check("pre-reclaim awake", lifecycle.is_awake(s))

    await registry.reclaim_agent_name(hub, "to-reclaim")
    check("reclaim removes session", "to-reclaim" not in hub.sessions)


async def test_process_message() -> None:
    """Test: process_message dispatches query and calls stream handler."""
    hub = make_hub()

    s = await registry.spawn_agent(hub, name="msg-agent", cwd="/tmp/msg")
    await lifecycle.wake_agent(hub, s)

    stream_calls: list[str] = []

    async def mock_stream_handler(session: AgentSession) -> str | None:
        stream_calls.append(session.name)
        return None  # success

    await messaging.process_message(hub, s, "Hello world", mock_stream_handler)
    check("query was sent", len(s.client.queries) == 1)
    check("stream handler was called", stream_calls == ["msg-agent"])

    await lifecycle.sleep_agent(hub, s)
    await registry.end_session(hub, "msg-agent")


async def test_process_message_retry() -> None:
    """Test: process_message retries on transient error from stream handler."""
    hub = make_hub()

    s = await registry.spawn_agent(hub, name="retry-agent", cwd="/tmp/retry")
    await lifecycle.wake_agent(hub, s)

    call_count = 0
    _system_messages.clear()

    async def flaky_handler(session: AgentSession) -> str | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "overloaded_error"  # transient
        return None  # success on retry

    await messaging.process_message(hub, s, "Retry me", flaky_handler)
    check("handler called twice (1 initial + 1 retry)", call_count == 2)
    check("retry query sent", len(s.client.queries) == 2)  # initial + retry
    check("retry notification sent", any("retrying" in m[1].lower() for m in _system_messages))

    await lifecycle.sleep_agent(hub, s)
    await registry.end_session(hub, "retry-agent")


async def test_process_message_timeout() -> None:
    """Test: process_message raises TimeoutError on timeout."""
    hub = make_hub()
    hub.query_timeout = 0.05  # 50ms

    s = await registry.spawn_agent(hub, name="timeout-agent", cwd="/tmp/to")
    await lifecycle.wake_agent(hub, s)

    async def slow_handler(session: AgentSession) -> str | None:
        await asyncio.sleep(5)  # way over timeout
        return None

    timed_out = False
    try:
        await messaging.process_message(hub, s, "Slow query", slow_handler)
    except TimeoutError:
        timed_out = True

    check("timeout raised", timed_out)

    # Clean up (session may be in bad state)
    await lifecycle.sleep_agent(hub, s, force=True)
    await registry.end_session(hub, "timeout-agent")


async def test_interrupt() -> None:
    """Test: interrupt_session calls client.interrupt when no bridge."""
    hub = make_hub()

    s = await registry.spawn_agent(hub, name="int-agent", cwd="/tmp/int")
    await lifecycle.wake_agent(hub, s)
    client = s.client

    await messaging.interrupt_session(hub, s)
    check("interrupt called on client", client._interrupted)

    await lifecycle.sleep_agent(hub, s)
    await registry.end_session(hub, "int-agent")


async def test_message_queue_processing() -> None:
    """Test: process_message_queue drains queued messages."""
    hub = make_hub()

    s = await registry.spawn_agent(hub, name="q-agent", cwd="/tmp/q")
    await lifecycle.wake_agent(hub, s)

    # Queue two messages
    s.message_queue.append(("msg-1", None))
    s.message_queue.append(("msg-2", None))

    processed: list[str] = []

    async def tracking_handler(session: AgentSession) -> str | None:
        # The last query content tells us which message was processed
        processed.append(session.name)
        return None

    _system_messages.clear()
    await messaging.process_message_queue(hub, s, tracking_handler)

    check("both messages processed", len(processed) == 2)
    check("queue is empty", len(s.message_queue) == 0)
    check("status messages sent", len(_system_messages) > 0)

    await lifecycle.sleep_agent(hub, s)
    await registry.end_session(hub, "q-agent")


async def test_shutdown_flag_stops_queue() -> None:
    """Test: setting shutdown_requested stops queue processing."""
    hub = make_hub()

    s = await registry.spawn_agent(hub, name="sd-agent", cwd="/tmp/sd")
    await lifecycle.wake_agent(hub, s)

    s.message_queue.append(("msg-1", None))
    s.message_queue.append(("msg-2", None))

    hub.shutdown_requested = True

    async def no_op_handler(session: AgentSession) -> str | None:
        return None

    await messaging.process_message_queue(hub, s, no_op_handler)

    check("queue not drained on shutdown", len(s.message_queue) == 2)

    hub.shutdown_requested = False
    await lifecycle.sleep_agent(hub, s)
    await registry.end_session(hub, "sd-agent")


async def test_count_awake() -> None:
    """Test: count_awake counts correctly."""
    hub = make_hub(max_awake=3)

    s1 = await registry.spawn_agent(hub, name="c1", cwd="/tmp/c1")
    s2 = await registry.spawn_agent(hub, name="c2", cwd="/tmp/c2")
    s3 = await registry.spawn_agent(hub, name="c3", cwd="/tmp/c3")

    check("0 awake initially", lifecycle.count_awake(hub.sessions) == 0)

    await lifecycle.wake_agent(hub, s1)
    check("1 awake", lifecycle.count_awake(hub.sessions) == 1)

    await lifecycle.wake_agent(hub, s2)
    check("2 awake", lifecycle.count_awake(hub.sessions) == 2)

    await lifecycle.sleep_agent(hub, s1)
    check("1 awake after sleep", lifecycle.count_awake(hub.sessions) == 1)

    await lifecycle.sleep_agent(hub, s2)
    await registry.end_session(hub, "c1")
    await registry.end_session(hub, "c2")
    await registry.end_session(hub, "c3")


async def test_hub_thin_delegation() -> None:
    """Test: hub.wake, hub.sleep, hub.spawn, hub.kill, hub.get work."""
    hub = make_hub()

    s = await hub.spawn(name="del-agent", cwd="/tmp/del")
    check("hub.spawn works", hub.get("del-agent") is s)

    await hub.wake("del-agent")
    check("hub.wake works", lifecycle.is_awake(s))

    await hub.sleep("del-agent")
    check("hub.sleep works", not lifecycle.is_awake(s))

    await hub.kill("del-agent")
    check("hub.kill works", hub.get("del-agent") is None)


async def test_deliver_inter_agent_message() -> None:
    """Test: inter-agent message delivery to idle agent."""
    hub = make_hub()

    target = await registry.spawn_agent(hub, name="target", cwd="/tmp/t")
    _system_messages.clear()

    delivered: list[str] = []

    async def handler(session: AgentSession) -> str | None:
        delivered.append(session.name)
        return None

    result = await messaging.deliver_inter_agent_message(
        hub, "sender", target, "Hello from sender", handler
    )
    check("delivery result has target name", "target" in result)

    # Give the fire-and-forget task a moment to run
    await asyncio.sleep(0.2)

    check("message was delivered", len(delivered) == 1)
    check("system message sent", any("sender" in m[1] for m in _system_messages))

    await lifecycle.sleep_agent(hub, target, force=True)
    await registry.end_session(hub, "target")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main() -> None:
    tests = [
        test_spawn_and_lifecycle,
        test_wake_or_queue,
        test_scheduler_eviction,
        test_rebuild_and_reset,
        test_reclaim,
        test_process_message,
        test_process_message_retry,
        test_process_message_timeout,
        test_interrupt,
        test_message_queue_processing,
        test_shutdown_flag_stops_queue,
        test_count_awake,
        test_hub_thin_delegation,
        test_deliver_inter_agent_message,
    ]

    for test_fn in tests:
        log.info("--- %s ---", test_fn.__name__)
        try:
            await test_fn()
        except Exception:
            log.exception("CRASH in %s", test_fn.__name__)
            global failed
            failed += 1

    log.info("=" * 50)
    log.info("Results: %d passed, %d failed", passed, failed)
    if failed > 0:
        raise SystemExit(1)
    log.info("All tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
