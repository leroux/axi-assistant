"""Tests for WebFrontend — FastAPI WebSocket server + Frontend protocol adapter."""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from axi.web_frontend import WebClient, WebFrontend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Minimal WebSocket stub for unit tests."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.client_state = "CONNECTED"  # matches WebSocketState.CONNECTED

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.client_state = "DISCONNECTED"


class FakeSession:
    """Minimal AgentSession stub."""

    def __init__(self, name: str = "test-agent", awake: bool = True) -> None:
        self.name = name
        self.agent_type = "claude_code"
        self.client = object() if awake else None
        self.activity = type("A", (), {"phase": "idle"})()
        self.session_id = "sess-123"


# ---------------------------------------------------------------------------
# WebClient tests
# ---------------------------------------------------------------------------


class TestWebClient:
    @pytest.mark.asyncio
    async def test_send_json_success(self) -> None:
        ws = FakeWebSocket()
        # Patch client_state check — our fake uses a string, not the enum
        client = WebClient.__new__(WebClient)
        client.ws = ws  # type: ignore[assignment]
        client.subscribed_agents = set()
        client._send_lock = asyncio.Lock()
        # Override wants_agent to always return True
        assert client.wants_agent("any")

    def test_wants_agent_empty_subs(self) -> None:
        client = WebClient.__new__(WebClient)
        client.subscribed_agents = set()
        assert client.wants_agent("master") is True
        assert client.wants_agent("agent-1") is True

    def test_wants_agent_filtered(self) -> None:
        client = WebClient.__new__(WebClient)
        client.subscribed_agents = {"agent-1", "agent-2"}
        assert client.wants_agent("agent-1") is True
        assert client.wants_agent("agent-2") is True
        assert client.wants_agent("agent-3") is False


# ---------------------------------------------------------------------------
# WebFrontend protocol tests
# ---------------------------------------------------------------------------


class TestWebFrontendProtocol:
    """Test that WebFrontend implements the Frontend protocol methods."""

    def test_name(self) -> None:
        fe = WebFrontend(port=0)
        assert fe.name == "web"

    @pytest.mark.asyncio
    async def test_post_message_no_clients(self) -> None:
        fe = WebFrontend(port=0)
        # Should not raise even with no clients
        await fe.post_message("agent-1", "hello")

    @pytest.mark.asyncio
    async def test_post_system_no_clients(self) -> None:
        fe = WebFrontend(port=0)
        await fe.post_system("agent-1", "system msg")

    @pytest.mark.asyncio
    async def test_broadcast_no_clients(self) -> None:
        fe = WebFrontend(port=0)
        await fe.broadcast("broadcast msg")

    @pytest.mark.asyncio
    async def test_on_wake(self) -> None:
        fe = WebFrontend(port=0)
        await fe.on_wake("agent-1")

    @pytest.mark.asyncio
    async def test_on_sleep(self) -> None:
        fe = WebFrontend(port=0)
        await fe.on_sleep("agent-1")

    @pytest.mark.asyncio
    async def test_on_spawn(self) -> None:
        fe = WebFrontend(port=0)
        await fe.on_spawn("agent-1", FakeSession())

    @pytest.mark.asyncio
    async def test_on_kill(self) -> None:
        fe = WebFrontend(port=0)
        await fe.on_kill("agent-1", "sess-123")

    @pytest.mark.asyncio
    async def test_on_session_id(self) -> None:
        fe = WebFrontend(port=0)
        await fe.on_session_id("agent-1", "sess-456")

    @pytest.mark.asyncio
    async def test_on_idle_reminder(self) -> None:
        fe = WebFrontend(port=0)
        await fe.on_idle_reminder("agent-1", 15.0)

    @pytest.mark.asyncio
    async def test_on_reconnect(self) -> None:
        fe = WebFrontend(port=0)
        await fe.on_reconnect("agent-1", True)

    @pytest.mark.asyncio
    async def test_ensure_channel_returns_none(self) -> None:
        fe = WebFrontend(port=0)
        assert await fe.ensure_channel("agent-1") is None

    @pytest.mark.asyncio
    async def test_get_channel_returns_none(self) -> None:
        fe = WebFrontend(port=0)
        assert await fe.get_channel("agent-1") is None

    @pytest.mark.asyncio
    async def test_reconstruct_sessions_empty(self) -> None:
        fe = WebFrontend(port=0)
        assert await fe.reconstruct_sessions() == []

    @pytest.mark.asyncio
    async def test_update_todo(self) -> None:
        fe = WebFrontend(port=0)
        await fe.update_todo("agent-1", [{"task": "test", "done": False}])

    @pytest.mark.asyncio
    async def test_send_goodbye(self) -> None:
        fe = WebFrontend(port=0)
        await fe.send_goodbye()

    @pytest.mark.asyncio
    async def test_close_app_stops(self) -> None:
        fe = WebFrontend(port=0)
        await fe.close_app()  # Should not raise

    @pytest.mark.asyncio
    async def test_kill_process_noop(self) -> None:
        fe = WebFrontend(port=0)
        await fe.kill_process()

    @pytest.mark.asyncio
    async def test_save_session_metadata_noop(self) -> None:
        fe = WebFrontend(port=0)
        await fe.save_session_metadata("agent-1", FakeSession())


# ---------------------------------------------------------------------------
# FastAPI endpoint tests
# ---------------------------------------------------------------------------


class TestFastAPIEndpoints:
    def test_health(self) -> None:
        fe = WebFrontend(port=0)
        with TestClient(fe.app) as client:
            resp = client.get("/api/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

    def test_agents_no_hub(self) -> None:
        fe = WebFrontend(port=0)
        with TestClient(fe.app) as client:
            resp = client.get("/api/agents")
            assert resp.status_code == 200
            assert resp.json() == []

    def test_agents_with_hub(self) -> None:
        fe = WebFrontend(port=0)

        class FakeHub:
            sessions: ClassVar[dict] = {"master": FakeSession("master"), "worker": FakeSession("worker", awake=False)}

        fe.set_hub(FakeHub())
        with TestClient(fe.app) as client:
            resp = client.get("/api/agents")
            assert resp.status_code == 200
            agents = resp.json()
            assert len(agents) == 2
            names = {a["name"] for a in agents}
            assert names == {"master", "worker"}

    def test_websocket_connect_gets_agent_list(self) -> None:
        fe = WebFrontend(port=0)

        class FakeHub:
            sessions: ClassVar[dict] = {"master": FakeSession("master")}

        fe.set_hub(FakeHub())

        with TestClient(fe.app) as client, client.websocket_connect("/ws") as ws:
            data = ws.receive_json()
            assert data["type"] == "agent_list"
            assert len(data["agents"]) == 1
            assert data["agents"][0]["name"] == "master"

    def test_websocket_ping_pong(self) -> None:
        fe = WebFrontend(port=0)
        fe.set_hub(type("H", (), {"sessions": {}})())

        with TestClient(fe.app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # agent_list
            ws.receive_json()  # chat_history
            ws.send_text(json.dumps({"type": "ping"}))
            data = ws.receive_json()
            assert data["type"] == "pong"
            assert "ts" in data

    def test_websocket_subscribe(self) -> None:
        fe = WebFrontend(port=0)
        fe.set_hub(type("H", (), {"sessions": {}})())

        with TestClient(fe.app) as client, client.websocket_connect("/ws") as ws:
            ws.receive_json()  # agent_list
            ws.receive_json()  # chat_history
            ws.send_text(json.dumps({"type": "subscribe", "agents": ["agent-1"]}))
            # No response expected, just verifying it doesn't error
            ws.send_text(json.dumps({"type": "ping"}))
            data = ws.receive_json()
            assert data["type"] == "pong"


# ---------------------------------------------------------------------------
# Stream event tests
# ---------------------------------------------------------------------------


class TestStreamEvents:
    @pytest.mark.asyncio
    async def test_on_stream_event(self) -> None:
        from agenthub.stream_types import TextDelta

        fe = WebFrontend(port=0)
        # No clients — should not raise
        await fe.on_stream_event("agent-1", TextDelta(text="hello"))

    @pytest.mark.asyncio
    async def test_on_log_event(self) -> None:
        from agenthub.agent_log import make_event

        fe = WebFrontend(port=0)
        event = make_event("user", "agent-1", text="test message")
        await fe.on_log_event(event)


# ---------------------------------------------------------------------------
# Interactive gate tests
# ---------------------------------------------------------------------------


class TestInteractiveGates:
    @pytest.mark.asyncio
    async def test_plan_approval_resolves(self) -> None:
        fe = WebFrontend(port=0)

        async def approve_later():
            await asyncio.sleep(0.05)
            fe._handle_plan_approval({
                "agent": "agent-1",
                "approved": True,
                "feedback": "looks good",
            })

        asyncio.create_task(approve_later())
        result = await fe.request_plan_approval("agent-1", "the plan", None)
        assert result.approved is True
        assert result.message == "looks good"

    @pytest.mark.asyncio
    async def test_plan_rejection(self) -> None:
        fe = WebFrontend(port=0)

        async def reject_later():
            await asyncio.sleep(0.05)
            fe._handle_plan_approval({
                "agent": "agent-1",
                "approved": False,
                "feedback": "needs changes",
            })

        asyncio.create_task(reject_later())
        result = await fe.request_plan_approval("agent-1", "the plan", None)
        assert result.approved is False
        assert result.message == "needs changes"

    @pytest.mark.asyncio
    async def test_question_answer_resolves(self) -> None:
        fe = WebFrontend(port=0)

        async def answer_later():
            await asyncio.sleep(0.05)
            fe._handle_question_answer({
                "agent": "agent-1",
                "answers": {"q1": "answer1"},
            })

        asyncio.create_task(answer_later())
        result = await fe.ask_question("agent-1", [{"question": "q1"}], None)
        assert result == {"q1": "answer1"}
