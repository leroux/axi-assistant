"""Runtime for the rewritten AgentHub orchestrator."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from agenthub.agent_log import make_agent_log, make_event
from agenthub.rate_limits import RateLimitTracker, handle_rate_limit, record_session_usage
from agenthub.scheduler import Scheduler
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
from agenthub.streaming import stream_response
from agenthub.tasks import BackgroundTaskSet
from agenthub.types import (
    AgentSession,
    LifecycleState,
    MessageContent,
    StopResult,
    SubmissionResult,
    TurnKind,
    TurnOutcome,
)

log = logging.getLogger(__name__)


class AgentHub:
    """Axi-less multi-agent orchestration runtime."""

    def __init__(
        self,
        *,
        frontends: list[Any] | None = None,
        create_client: Any,
        disconnect_client: Any,
        make_agent_options: Any,
        max_awake: int = 8,
        query_timeout: float = 300.0,
        usage_history_path: str | None = None,
        rate_limit_history_path: str | None = None,
        log_dir: str | None = None,
        stream_factory: Any = stream_response,
    ) -> None:
        self.frontends = list(frontends or [])
        self.sessions: dict[str, AgentSession] = {}
        self.create_client = create_client
        self.disconnect_client = disconnect_client
        self.make_agent_options = make_agent_options
        self.max_awake = max_awake
        self.query_timeout = query_timeout
        self.scheduler = Scheduler(
            max_slots=max_awake,
            protected=set(),
            get_sessions=lambda: self.sessions,
            sleep_fn=self._sleep_session,
        )
        self.rate_limits = RateLimitTracker(
            usage_history_path=usage_history_path,
            rate_limit_history_path=rate_limit_history_path,
        )
        self.tasks = BackgroundTaskSet()
        self.log_dir = log_dir
        self.stream_factory = stream_factory
        self.shutdown_requested = False

    async def spawn_agent(
        self,
        *,
        name: str,
        cwd: str,
        agent_type: str = "claude_code",
        system_prompt: Any = None,
        session_id: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
        frontend_state: Any = None,
        compact_instructions: str | None = None,
    ) -> AgentSession:
        Path(cwd).mkdir(parents=True, exist_ok=True)
        session = AgentSession(
            name=name,
            agent_type=agent_type,
            cwd=cwd,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
            frontend_state=frontend_state,
            compact_instructions=compact_instructions,
        )
        session.state = replace(session.state, session_id=session_id)
        session.agent_log = make_agent_log(name, persist_dir=self.log_dir)
        self.sessions[name] = session
        await self._emit_log(session, "system", f"spawned {name}")
        await self._frontend_broadcast("on_spawn", name, session)
        return session

    async def remove_agent(self, name: str) -> None:
        session = self.sessions.get(name)
        if session is None:
            return
        await self._sleep_session(session)
        await self._frontend_broadcast("on_kill", name, session.session_id)
        if session.agent_log is not None:
            session.agent_log.close()
        self.sessions.pop(name, None)

    def get_session(self, name: str) -> AgentSession | None:
        return self.sessions.get(name)

    def snapshot(self) -> dict[str, AgentSession]:
        return dict(self.sessions)

    async def submit_user_message(
        self,
        name: str,
        content: MessageContent,
        metadata: Any = None,
        *,
        source: str = "user",
    ) -> SubmissionResult:
        return await self._submit_turn(name, TurnKind.USER, content, metadata, source)

    async def submit_inter_agent_message(
        self,
        name: str,
        content: MessageContent,
        metadata: Any = None,
        *,
        source: str = "inter_agent",
    ) -> SubmissionResult:
        return await self._submit_turn(name, TurnKind.INTER_AGENT, content, metadata, source)

    async def request_stop(self, name: str, *, clear_queue: bool = True) -> StopResult:
        session = self.sessions[name]
        async with session.dispatch_lock:
            cleared = len(session.state.queued_turns) if clear_queue else 0
            session.state = reduce_session(session.state, StopRequested(agent_name=name, clear_queue=clear_queue))
            if session.client is not None and session.state.lifecycle is LifecycleState.STOPPING:
                await self._interrupt_session(session)
            return StopResult(status="stopping", cleared=cleared, message="stop requested")

    async def request_skip(self, name: str) -> StopResult:
        session = self.sessions[name]
        async with session.dispatch_lock:
            session.state = reduce_session(session.state, SkipRequested(agent_name=name))
            if session.client is not None and session.state.lifecycle is LifecycleState.STOPPING:
                await self._interrupt_session(session)
            return StopResult(status="stopping", message="skip requested")

    async def wake(self, name: str) -> None:
        await self._ensure_awake(self.sessions[name])

    async def sleep(self, name: str) -> None:
        await self._sleep_session(self.sessions[name])

    async def _submit_turn(
        self,
        name: str,
        kind: TurnKind,
        content: MessageContent,
        metadata: Any,
        source: str,
    ) -> SubmissionResult:
        if self.shutdown_requested:
            return SubmissionResult(status="shutdown", message="hub is shutting down")
        session = self.sessions[name]
        turn_id = uuid.uuid4().hex[:12]
        payload = metadata if isinstance(metadata, dict) else {"data": metadata}
        payload = dict(payload)
        payload["turn_id"] = turn_id
        async with session.dispatch_lock:
            had_current = session.state.current_turn is not None
            old_queue_len = len(session.state.queued_turns)
            session.state = reduce_session(
                session.state,
                SubmitTurn(
                    agent_name=name,
                    kind=kind,
                    content=content,
                    metadata=payload,
                    source=source,
                ),
            )
            if not had_current and session.state.current_turn and session.state.current_turn.turn_id == turn_id:
                if kind is TurnKind.USER:
                    self.scheduler.mark_interactive(name)
                else:
                    self.scheduler.mark_background(name)
                session.query_task = self.tasks.fire_and_forget(self._drive_current_turn(session))
                return SubmissionResult(status="started", turn_id=turn_id)
            return SubmissionResult(
                status="queued",
                turn_id=turn_id,
                position=len(session.state.queued_turns),
                message=f"queued at position {len(session.state.queued_turns)} (was {old_queue_len})",
            )

    async def _drive_current_turn(self, session: AgentSession) -> None:
        turn = session.state.current_turn
        if turn is None:
            return
        async with session.query_lock:
            try:
                await self._ensure_awake(session)
            except Exception as exc:
                session.query_task = None
                async with session.dispatch_lock:
                    session.state = reduce_session(
                        session.state,
                        WakeFailed(agent_name=session.name, error=str(exc)),
                    )
                await self._emit_log(session, "error", f"wake failed: {exc}")
                return

            session.state.last_activity = turn.submitted_at
            session.activity = type(session.activity)(phase="starting", query_started=turn.submitted_at)
            await self._emit_log(session, "user", str(turn.content), source=turn.source, turn_kind=turn.kind.value)
            try:
                outcome = await self._run_turn_with_timeout(session, turn)
            except TimeoutError:
                await self._handle_turn_timeout(session)
                outcome = TurnOutcome.TIMEOUT
            except Exception as exc:
                await self._emit_log(session, "error", f"query failed: {exc}")
                outcome = TurnOutcome.ERROR
            finally:
                session.activity = type(session.activity)()

        async with session.dispatch_lock:
            session.state = reduce_session(
                session.state,
                TurnFinished(agent_name=session.name, turn_id=turn.turn_id, outcome=outcome),
            )
            if session.state.current_turn is not None:
                if self.scheduler.should_yield(session.name):
                    await self._sleep_session(session)
                    session.query_task = None
                else:
                    session.query_task = self.tasks.fire_and_forget(self._drive_current_turn(session))
            else:
                session.query_task = None
                await self._sleep_session(session)

    async def _run_turn_with_timeout(self, session: AgentSession, turn: Any) -> TurnOutcome:
        async with asyncio.timeout(self.query_timeout):
            await session.client.query(turn.content)
            return await self._consume_stream(session, turn.turn_id)

    async def _handle_turn_timeout(self, session: AgentSession) -> None:
        await self._emit_log(session, "error", f"turn timed out after {self.query_timeout}s")
        await self._interrupt_session(session)
        await self._sleep_session(session)

    async def _consume_stream(self, session: AgentSession, turn_id: str) -> TurnOutcome:
        outcome = TurnOutcome.COMPLETED
        async for event in self.stream_factory(
            session,
            set_session_id_fn=self._set_session_id_from_stream,
            record_usage_fn=lambda agent_name, msg: record_session_usage(self.rate_limits, agent_name, msg),
        ):
            async with session.dispatch_lock:
                session.state = reduce_session(
                    session.state,
                    StreamEventReceived(agent_name=session.name, turn_id=turn_id, event=event),
                )
            await self._frontend_broadcast("on_stream_event", session.name, event)
            await self._emit_log(session, "stream_event", type(event).__name__)
            from agenthub.stream_types import QueryResult, RateLimitHit, StreamKilled, TransientError

            if isinstance(event, StreamKilled):
                outcome = TurnOutcome.KILLED
            elif isinstance(event, RateLimitHit):
                await handle_rate_limit(
                    self.rate_limits,
                    event.error_text,
                    lambda text: self._frontend_broadcast("broadcast", text),
                    lambda _seconds: None,
                )
                outcome = TurnOutcome.RATE_LIMIT
            elif isinstance(event, TransientError):
                outcome = TurnOutcome.RETRY_EXHAUSTED
            elif isinstance(event, QueryResult) and event.is_error:
                outcome = TurnOutcome.ERROR
        if session.state.stop_requested:
            return TurnOutcome.INTERRUPTED
        return outcome

    async def _set_session_id_from_stream(self, session: AgentSession, msg: Any) -> None:
        session_id = getattr(msg, "session_id", None)
        if not session_id:
            return
        session.session_id = session_id
        await self._frontend_broadcast("on_session_id", session.name, session_id)

    async def _ensure_awake(self, session: AgentSession) -> None:
        if session.client is not None:
            if session.state.lifecycle is LifecycleState.WAKING:
                async with session.dispatch_lock:
                    session.state = reduce_session(session.state, WakeCompleted(agent_name=session.name))
            return
        await self.scheduler.request_slot(session.name)
        session.state.lifecycle = LifecycleState.WAKING
        try:
            options = self.make_agent_options(session, session.session_id)
            session.client = await self.create_client(session, options)
        except Exception:
            self.scheduler.release_slot(session.name)
            raise
        async with session.dispatch_lock:
            session.state = reduce_session(session.state, WakeCompleted(agent_name=session.name))
        await self._frontend_broadcast("on_wake", session.name)

    async def _sleep_session(self, session: AgentSession) -> None:
        if session.client is None:
            session.state.lifecycle = LifecycleState.SLEEPING
            return
        await self.disconnect_client(session.client, session.name)
        session.client = None
        session.transport = None
        session.query_task = None
        self.scheduler.release_slot(session.name)
        session.state.lifecycle = LifecycleState.SLEEPING
        await self._frontend_broadcast("on_sleep", session.name)

    async def _interrupt_session(self, session: AgentSession) -> None:
        if session.client is None:
            return
        try:
            await session.client.interrupt()
        except Exception:
            log.warning("Interrupt failed for '%s'", session.name, exc_info=True)

    async def _frontend_broadcast(self, method: str, *args: Any) -> None:
        for frontend in self.frontends:
            fn = getattr(frontend, method, None)
            if fn is None:
                continue
            await fn(*args)

    async def _emit_log(self, session: AgentSession, kind: str, text: str, **data: Any) -> None:
        if session.agent_log is None:
            return
        await session.agent_log.append(make_event(kind, session.name, text=text, **data))
