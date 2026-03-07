# pyright: reportUnusedFunction=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""WebFrontend — FastAPI + WebSocket frontend adapter for the web UI.

Runs an HTTP/WebSocket server in the same process as the Discord bot.
Connected web clients receive real-time agent events over WebSocket and
can send messages, plan approvals, and question answers back.

Architecture:
    FastAPI app (uvicorn) <--WebSocket--> React SPA
    WebFrontend implements the Frontend protocol so FrontendRouter
    broadcasts events to all connected web clients alongside Discord.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse
from starlette.websockets import WebSocketState

from agenthub.frontend import PlanApprovalResult

if TYPE_CHECKING:
    from agenthub.agent_log import LogEvent
    from agenthub.stream_types import StreamOutput

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connected client tracking
# ---------------------------------------------------------------------------


class WebClient:
    """One connected WebSocket client."""

    __slots__ = ("_send_lock", "subscribed_agents", "ws")

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.subscribed_agents: set[str] = set()  # empty = all agents
        self._send_lock = asyncio.Lock()

    async def send_json(self, data: dict[str, Any]) -> bool:
        """Send JSON to client. Returns False if the connection is dead."""
        if self.ws.client_state != WebSocketState.CONNECTED:
            return False
        try:
            async with self._send_lock:
                await self.ws.send_json(data)
            return True
        except Exception:
            return False

    def wants_agent(self, agent_name: str) -> bool:
        """Check if this client is subscribed to events from this agent."""
        return not self.subscribed_agents or agent_name in self.subscribed_agents


# ---------------------------------------------------------------------------
# Interactive gate futures
# ---------------------------------------------------------------------------


class _PlanGate:
    """Pending plan approval waiting for a web client response."""

    __slots__ = ("agent_name", "future")

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self.future: asyncio.Future[PlanApprovalResult] = asyncio.get_event_loop().create_future()


class _QuestionGate:
    """Pending question waiting for a web client response."""

    __slots__ = ("agent_name", "future")

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self.future: asyncio.Future[dict[str, str]] = asyncio.get_event_loop().create_future()


# ---------------------------------------------------------------------------
# WebFrontend
# ---------------------------------------------------------------------------


class WebFrontend:
    """Frontend adapter for the web UI.

    Implements the Frontend protocol. Manages WebSocket connections,
    broadcasts events to connected clients, and handles incoming
    messages from the web UI.
    """

    def __init__(self, port: int = 8420, host: str = "0.0.0.0") -> None:
        self._port = port
        self._host = host
        self._clients: list[WebClient] = []
        self._plan_gates: dict[str, _PlanGate] = {}
        self._question_gates: dict[str, _QuestionGate] = {}
        self._server_task: asyncio.Task[None] | None = None
        self._hub: Any = None  # Set after wiring

        # Chat history store
        import os

        from axi import config
        from axi.web_chat_store import ChatStore

        db_path = os.path.join(config.AXI_USER_DATA, "web_chat_history.db")
        self._chat_store = ChatStore(db_path)

        # FastAPI app
        self.app = FastAPI(title="Axi Web API", docs_url=None, redoc_url=None)
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self._register_routes()
        self._mount_static()

    @property
    def name(self) -> str:
        return "web"

    def set_hub(self, hub: Any) -> None:
        """Set the AgentHub reference for message routing."""
        self._hub = hub

    def _mount_static(self) -> None:
        """Mount the React build output if available."""
        import os

        bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dist_dir = os.path.join(bot_dir, "web", "dist")
        if os.path.isdir(dist_dir):
            index_path = os.path.join(dist_dir, "index.html")

            # Serve static assets (JS, CSS, images)
            assets_dir = os.path.join(dist_dir, "assets")
            if os.path.isdir(assets_dir):
                self.app.mount("/assets", StaticFiles(directory=assets_dir), name="static")

            # SPA fallback: serve index.html for all non-API routes
            @self.app.get("/{path:path}")
            async def spa_fallback(path: str) -> FileResponse:
                # Serve actual files if they exist in dist
                file_path = os.path.join(dist_dir, path)
                if os.path.isfile(file_path):
                    return FileResponse(file_path)
                return FileResponse(index_path)

    # ------------------------------------------------------------------
    # FastAPI routes
    # ------------------------------------------------------------------

    def _register_routes(self) -> None:
        app = self.app

        @app.get("/api/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/api/agents")
        async def list_agents() -> list[dict[str, Any]]:
            if not self._hub:
                return []
            agents = []
            for name, session in self._hub.sessions.items():
                agents.append({
                    "name": name,
                    "agent_type": session.agent_type,
                    "awake": session.client is not None,
                    "activity_phase": session.activity.phase if session.activity else "idle",
                    "session_id": session.session_id,
                })
            return agents

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            client = WebClient(ws)
            self._clients.append(client)
            log.info("Web client connected (%d total)", len(self._clients))

            try:
                # Send initial agent list
                await client.send_json({
                    "type": "agent_list",
                    "agents": await list_agents(),
                })

                # Send chat history
                history = self._chat_store.get_all_agents_history(limit_per_agent=100)
                if history:
                    await client.send_json({
                        "type": "chat_history",
                        "history": history,
                    })

                # Process incoming messages
                while True:
                    raw = await ws.receive_text()
                    await self._handle_client_message(client, raw)
            except WebSocketDisconnect:
                pass
            except Exception:
                log.warning("WebSocket error", exc_info=True)
            finally:
                self._clients.remove(client)
                log.info("Web client disconnected (%d remaining)", len(self._clients))

    # ------------------------------------------------------------------
    # Client message handling
    # ------------------------------------------------------------------

    async def _handle_client_message(self, client: WebClient, raw: str) -> None:
        """Handle an incoming message from a web client."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Invalid JSON from web client: %s", raw[:100])
            return

        msg_type = msg.get("type", "")

        if msg_type == "message":
            await self._handle_user_message(msg)
        elif msg_type == "plan_approval":
            self._handle_plan_approval(msg)
        elif msg_type == "question_answer":
            self._handle_question_answer(msg)
        elif msg_type == "subscribe":
            agents = msg.get("agents", [])
            client.subscribed_agents = set(agents) if agents else set()
        elif msg_type == "ping":
            await client.send_json({"type": "pong", "ts": time.time()})
        else:
            log.warning("Unknown web message type: %s", msg_type)

    async def _handle_user_message(self, msg: dict[str, Any]) -> None:
        """Route a user message from the web UI to the agent."""
        agent_name = msg.get("agent", "")
        content = msg.get("content", "")
        if not agent_name or not content:
            return

        if not self._hub:
            log.warning("WebFrontend: no hub set, cannot route message")
            return

        session = self._hub.sessions.get(agent_name)
        if not session:
            await self._broadcast_to_clients({
                "type": "error",
                "agent": agent_name,
                "text": f"Agent '{agent_name}' not found",
            })
            return

        # Broadcast the user message to all web clients (echo back)
        ts = time.time()
        await self._broadcast_to_clients({
            "type": "user_message",
            "agent": agent_name,
            "content": content,
            "source": "web",
            "ts": ts,
        })
        self._chat_store.add_message(agent_name, "user", content, ts, source="web")

        # Process via hub's centralized message ingestion
        asyncio.create_task(self._process_web_message(session, content))

    async def _process_web_message(self, session: Any, content: str) -> None:
        """Process a message from web UI through the hub."""
        from agenthub import lifecycle
        from agenthub.messaging import process_message_queue, receive_user_message

        agent_name = session.name
        handler = self._make_web_stream_handler(agent_name)

        result = await receive_user_message(
            self._hub, session, content, handler,
        )

        log.info("Web message for '%s': %s", agent_name, result.status)

        # Post-processing: yield check + queue drain (using hub's 2-tuple queue)
        if result.status == "processed":
            if self._hub.scheduler.should_yield(session.name):
                await lifecycle.sleep_agent(self._hub, session)
            else:
                await process_message_queue(self._hub, session, handler)

    def _make_web_stream_handler(self, agent_name: str) -> Any:
        """Create a StreamHandlerFn that iterates SDK events and pushes to web clients."""
        async def handler(session: Any) -> str | None:
            from agenthub.stream_types import RateLimitHit, TransientError
            from agenthub.streaming import stream_response

            error_text: str | None = None
            async for event in stream_response(session):
                await self.on_stream_event(agent_name, event)
                if isinstance(event, (TransientError, RateLimitHit)):
                    error_text = event.error_text or event.error_type
            return error_text
        return handler

    def _handle_plan_approval(self, msg: dict[str, Any]) -> None:
        """Resolve a pending plan approval from the web UI."""
        agent_name = msg.get("agent", "")
        gate = self._plan_gates.get(agent_name)
        if gate and not gate.future.done():
            approved = msg.get("approved", False)
            feedback = msg.get("feedback", "")
            gate.future.set_result(PlanApprovalResult(
                approved=approved,
                message=feedback,
            ))

    def _handle_question_answer(self, msg: dict[str, Any]) -> None:
        """Resolve a pending question from the web UI."""
        agent_name = msg.get("agent", "")
        gate = self._question_gates.get(agent_name)
        if gate and not gate.future.done():
            answers = msg.get("answers", {})
            gate.future.set_result(answers)

    # ------------------------------------------------------------------
    # Broadcast to connected web clients
    # ------------------------------------------------------------------

    async def _broadcast_to_clients(
        self, data: dict[str, Any], agent_name: str | None = None
    ) -> None:
        """Send a JSON message to all connected (and subscribed) clients."""
        dead: list[WebClient] = []
        for client in self._clients:
            if agent_name and not client.wants_agent(agent_name):
                continue
            if not await client.send_json(data):
                dead.append(client)
        for client in dead:
            try:
                self._clients.remove(client)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Frontend protocol: Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the uvicorn server as a background task."""
        import uvicorn

        uvi_config = uvicorn.Config(
            app=self.app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(uvi_config)
        self._server_task = asyncio.create_task(server.serve())
        log.info("Web frontend started on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        """Stop the web server."""
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
            self._server_task = None
        # Close all WebSocket connections
        for client in list(self._clients):
            try:
                await client.ws.close()
            except Exception:
                pass
        self._clients.clear()
        log.info("Web frontend stopped")

    # ------------------------------------------------------------------
    # Frontend protocol: Outbound (hub -> web clients)
    # ------------------------------------------------------------------

    async def post_message(self, agent_name: str, text: str) -> None:
        ts = time.time()
        await self._broadcast_to_clients({
            "type": "assistant_message",
            "agent": agent_name,
            "text": text,
            "ts": ts,
        }, agent_name)
        self._chat_store.add_message(agent_name, "assistant", text, ts)

    async def post_system(self, agent_name: str, text: str) -> None:
        ts = time.time()
        await self._broadcast_to_clients({
            "type": "system",
            "agent": agent_name,
            "text": text,
            "ts": ts,
        }, agent_name)
        self._chat_store.add_message(agent_name, "system", text, ts)

    async def broadcast(self, text: str) -> None:
        await self._broadcast_to_clients({
            "type": "broadcast",
            "text": text,
            "ts": time.time(),
        })

    # ------------------------------------------------------------------
    # Frontend protocol: Agent lifecycle events
    # ------------------------------------------------------------------

    async def on_wake(self, agent_name: str) -> None:
        ts = time.time()
        await self._broadcast_to_clients({
            "type": "agent_wake",
            "agent": agent_name,
            "ts": ts,
        }, agent_name)
        self._chat_store.add_message(agent_name, "system", f"Agent **{agent_name}** woke up", ts, stream_event_type="lifecycle")

    async def on_sleep(self, agent_name: str) -> None:
        ts = time.time()
        await self._broadcast_to_clients({
            "type": "agent_sleep",
            "agent": agent_name,
            "ts": ts,
        }, agent_name)
        self._chat_store.add_message(agent_name, "system", f"Agent **{agent_name}** went to sleep", ts, stream_event_type="lifecycle")

    async def on_spawn(self, agent_name: str, session: Any) -> None:
        await self._broadcast_to_clients({
            "type": "agent_spawn",
            "agent": agent_name,
            "agent_type": getattr(session, "agent_type", "claude_code"),
            "ts": time.time(),
        })

    async def on_kill(self, agent_name: str, session_id: str | None) -> None:
        await self._broadcast_to_clients({
            "type": "agent_kill",
            "agent": agent_name,
            "session_id": session_id,
            "ts": time.time(),
        })

    async def on_session_id(self, agent_name: str, session_id: str) -> None:
        await self._broadcast_to_clients({
            "type": "session_id",
            "agent": agent_name,
            "session_id": session_id,
            "ts": time.time(),
        }, agent_name)

    async def on_idle_reminder(self, agent_name: str, idle_minutes: float) -> None:
        await self._broadcast_to_clients({
            "type": "idle_reminder",
            "agent": agent_name,
            "idle_minutes": idle_minutes,
            "ts": time.time(),
        }, agent_name)

    async def on_reconnect(self, agent_name: str, was_mid_task: bool) -> None:
        await self._broadcast_to_clients({
            "type": "agent_reconnect",
            "agent": agent_name,
            "was_mid_task": was_mid_task,
            "ts": time.time(),
        }, agent_name)

    # ------------------------------------------------------------------
    # Frontend protocol: Stream rendering
    # ------------------------------------------------------------------

    async def on_stream_event(self, agent_name: str, event: StreamOutput) -> None:
        """Push a stream output event to web clients as JSON."""
        event_type = type(event).__name__
        # Convert dataclass to dict for JSON serialization
        try:
            data = asdict(event)  # type: ignore[arg-type]
        except Exception:
            data = {}

        ts = time.time()
        await self._broadcast_to_clients({
            "type": "stream_event",
            "event_type": event_type,
            "agent": agent_name,
            "data": data,
            "ts": ts,
        }, agent_name)

        # Persist stream events that produce visible messages
        if event_type == "TextFlush" and data.get("text"):
            self._chat_store.add_message(agent_name, "assistant", data["text"], ts)
        elif event_type == "StreamEnd" and data.get("elapsed_s"):
            self._chat_store.add_message(
                agent_name, "system", f"Completed in {data['elapsed_s']:.1f}s", ts,
                stream_event_type="timing",
            )
        elif event_type == "ThinkingStart":
            self._chat_store.add_message(agent_name, "system", "Thinking...", ts, stream_event_type="thinking")
        elif event_type in ("ToolUseStart", "ToolUseEnd"):
            tool_text = f"Using tool: {data.get('tool_name', '?')}" if event_type == "ToolUseStart" else (
                f"{data.get('tool_name', '?')}: {data.get('preview', '')}" if data.get("preview") else None
            )
            if tool_text:
                self._chat_store.add_message(agent_name, "system", tool_text, ts, stream_event_type="tool")

    # ------------------------------------------------------------------
    # Frontend protocol: Interactive gates
    # ------------------------------------------------------------------

    async def request_plan_approval(
        self, agent_name: str, plan_content: str, session: Any
    ) -> PlanApprovalResult:
        """Send plan to web clients and wait for approval."""
        gate = _PlanGate(agent_name)
        self._plan_gates[agent_name] = gate

        await self._broadcast_to_clients({
            "type": "plan_request",
            "agent": agent_name,
            "plan": plan_content,
            "ts": time.time(),
        }, agent_name)

        try:
            return await gate.future
        finally:
            self._plan_gates.pop(agent_name, None)

    async def ask_question(
        self, agent_name: str, questions: list[dict[str, Any]], session: Any
    ) -> dict[str, str]:
        """Send questions to web clients and wait for answers."""
        gate = _QuestionGate(agent_name)
        self._question_gates[agent_name] = gate

        await self._broadcast_to_clients({
            "type": "question",
            "agent": agent_name,
            "questions": questions,
            "ts": time.time(),
        }, agent_name)

        try:
            return await gate.future
        finally:
            self._question_gates.pop(agent_name, None)

    async def update_todo(self, agent_name: str, todos: list[dict[str, Any]]) -> None:
        await self._broadcast_to_clients({
            "type": "todo_update",
            "agent": agent_name,
            "todos": todos,
            "ts": time.time(),
        }, agent_name)

    # ------------------------------------------------------------------
    # Frontend protocol: Channel management (no-op for web)
    # ------------------------------------------------------------------

    async def ensure_channel(self, agent_name: str, cwd: str | None = None) -> Any:
        return None  # Web doesn't have "channels" — agents are virtual rooms

    async def move_to_killed(self, agent_name: str) -> None:
        await self._broadcast_to_clients({
            "type": "agent_kill",
            "agent": agent_name,
            "ts": time.time(),
        })

    async def get_channel(self, agent_name: str) -> Any:
        return None  # Web doesn't use Discord channels

    # ------------------------------------------------------------------
    # Frontend protocol: Session persistence (no-op for web)
    # ------------------------------------------------------------------

    async def save_session_metadata(self, agent_name: str, session: Any) -> None:
        pass  # Web frontend doesn't persist session metadata

    async def reconstruct_sessions(self) -> list[dict[str, Any]]:
        return []  # Web frontend doesn't reconstruct sessions

    # ------------------------------------------------------------------
    # Frontend protocol: Event log integration
    # ------------------------------------------------------------------

    async def on_log_event(self, event: LogEvent) -> None:
        """Push log events to web clients for real-time updates."""
        try:
            data = asdict(event)  # type: ignore[arg-type]
            data["ts"] = event.ts.isoformat()
        except Exception:
            data = {"kind": event.kind, "agent": event.agent, "text": event.text}

        await self._broadcast_to_clients({
            "type": "log_event",
            "event": data,
        }, event.agent)

    # ------------------------------------------------------------------
    # Frontend protocol: Shutdown
    # ------------------------------------------------------------------

    async def send_goodbye(self) -> None:
        await self._broadcast_to_clients({
            "type": "system",
            "text": "Server shutting down...",
            "ts": time.time(),
        })

    async def close_app(self) -> None:
        await self.stop()

    async def kill_process(self) -> None:
        pass  # Web frontend doesn't kill the process
