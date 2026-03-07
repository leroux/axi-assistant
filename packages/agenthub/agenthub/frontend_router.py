"""FrontendRouter — multiplexes hub callbacks to all registered frontends.

When AgentHub emits a notification (message, lifecycle event, stream output),
the router broadcasts it to every registered frontend. Each frontend renders
it in its own way (Discord edits messages, web pushes WebSocket events, etc.).

For interactive gates (plan approval, questions), the router races all
frontends and returns the first response.

The router also generates a FrontendCallbacks instance for backward
compatibility with existing AgentHub code.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from agenthub.callbacks import FrontendCallbacks
from agenthub.frontend import Frontend, PlanApprovalResult

if TYPE_CHECKING:
    from agenthub.agent_log import LogEvent
    from agenthub.stream_types import StreamOutput

log = logging.getLogger(__name__)


class FrontendRouter:
    """Multiplexes hub events to N registered frontends.

    Usage:
        router = FrontendRouter()
        router.add(discord_frontend)
        router.add(web_frontend)

        # Get FrontendCallbacks for AgentHub (backward compat)
        callbacks = router.as_callbacks()
        hub = AgentHub(callbacks=callbacks, ...)

        # Or call methods directly
        await router.post_message("master", "hello")
    """

    def __init__(self) -> None:
        self.frontends: dict[str, Frontend] = {}

    def add(self, frontend: Frontend) -> None:
        """Register a frontend adapter."""
        self.frontends[frontend.name] = frontend
        log.info("Frontend '%s' registered", frontend.name)

    def remove(self, name: str) -> Frontend | None:
        """Unregister a frontend adapter. Returns it or None."""
        fe = self.frontends.pop(name, None)
        if fe:
            log.info("Frontend '%s' unregistered", name)
        return fe

    def get(self, name: str) -> Frontend | None:
        """Get a frontend by name."""
        return self.frontends.get(name)

    # ------------------------------------------------------------------
    # Broadcast helpers
    # ------------------------------------------------------------------

    async def _broadcast(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Call a method on all frontends, logging errors but not raising."""
        for fe in self.frontends.values():
            fn = getattr(fe, method, None)
            if fn is None:
                continue
            try:
                await fn(*args, **kwargs)
            except Exception:
                log.warning(
                    "Frontend '%s'.%s failed", fe.name, method, exc_info=True
                )

    async def _first_response(
        self, method: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Race all frontends, return the first response.

        Used for interactive gates (plan approval, questions) where the
        first user response wins.
        """
        if not self.frontends:
            return None

        tasks: dict[asyncio.Task[Any], str] = {}
        for fe in self.frontends.values():
            fn = getattr(fe, method, None)
            if fn is None:
                continue
            task = asyncio.create_task(fn(*args, **kwargs))
            tasks[task] = fe.name

        if not tasks:
            return None

        done, pending = await asyncio.wait(
            tasks.keys(), return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel losers
        for task in pending:
            task.cancel()

        # Return first successful result
        for task in done:
            try:
                return task.result()
            except Exception:
                log.warning(
                    "Frontend '%s'.%s failed during race",
                    tasks[task],
                    method,
                    exc_info=True,
                )
        return None

    # ------------------------------------------------------------------
    # Outbound: hub -> frontend
    # ------------------------------------------------------------------

    async def post_message(self, agent_name: str, text: str) -> None:
        await self._broadcast("post_message", agent_name, text)

    async def post_system(self, agent_name: str, text: str) -> None:
        await self._broadcast("post_system", agent_name, text)

    async def broadcast(self, text: str) -> None:
        await self._broadcast("broadcast", text)

    # ------------------------------------------------------------------
    # Agent lifecycle events
    # ------------------------------------------------------------------

    async def on_wake(self, agent_name: str) -> None:
        await self._broadcast("on_wake", agent_name)

    async def on_sleep(self, agent_name: str) -> None:
        await self._broadcast("on_sleep", agent_name)

    async def on_spawn(self, agent_name: str, session: Any) -> None:
        await self._broadcast("on_spawn", agent_name, session)

    async def on_kill(self, agent_name: str, session_id: str | None) -> None:
        await self._broadcast("on_kill", agent_name, session_id)

    async def on_session_id(self, agent_name: str, session_id: str) -> None:
        await self._broadcast("on_session_id", agent_name, session_id)

    async def on_idle_reminder(self, agent_name: str, idle_minutes: float) -> None:
        await self._broadcast("on_idle_reminder", agent_name, idle_minutes)

    async def on_reconnect(self, agent_name: str, was_mid_task: bool) -> None:
        await self._broadcast("on_reconnect", agent_name, was_mid_task)

    # ------------------------------------------------------------------
    # Stream rendering
    # ------------------------------------------------------------------

    async def on_stream_event(self, agent_name: str, event: StreamOutput) -> None:
        await self._broadcast("on_stream_event", agent_name, event)

    # ------------------------------------------------------------------
    # Interactive gates (first response wins)
    # ------------------------------------------------------------------

    async def request_plan_approval(
        self, agent_name: str, plan_content: str, session: Any
    ) -> PlanApprovalResult:
        result = await self._first_response(
            "request_plan_approval", agent_name, plan_content, session
        )
        if result is None:
            return PlanApprovalResult(approved=True)  # no frontend = auto-approve
        return result

    async def ask_question(
        self, agent_name: str, questions: list[dict[str, Any]], session: Any
    ) -> dict[str, str]:
        result = await self._first_response(
            "ask_question", agent_name, questions, session
        )
        return result or {}

    async def update_todo(self, agent_name: str, todos: list[dict[str, Any]]) -> None:
        await self._broadcast("update_todo", agent_name, todos)

    # ------------------------------------------------------------------
    # Channel management (delegates to first frontend that has a channel)
    # ------------------------------------------------------------------

    async def get_channel(self, agent_name: str) -> Any:
        for fe in self.frontends.values():
            try:
                ch = await fe.get_channel(agent_name)
                if ch is not None:
                    return ch
            except Exception:
                continue
        return None

    async def ensure_channel(self, agent_name: str, cwd: str | None = None) -> Any:
        await self._broadcast("ensure_channel", agent_name, cwd)

    async def move_to_killed(self, agent_name: str) -> None:
        await self._broadcast("move_to_killed", agent_name)

    # ------------------------------------------------------------------
    # Event log integration
    # ------------------------------------------------------------------

    async def on_log_event(self, event: LogEvent) -> None:
        await self._broadcast("on_log_event", event)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def send_goodbye(self) -> None:
        await self._broadcast("send_goodbye")

    async def close_app(self) -> None:
        await self._broadcast("close_app")

    async def kill_process(self) -> None:
        # Only call on first frontend (usually Discord — there's only one process to kill)
        for fe in self.frontends.values():
            try:
                await fe.kill_process()
                return
            except Exception:
                log.warning("Frontend '%s'.kill_process failed", fe.name, exc_info=True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        """Start all registered frontends."""
        for fe in self.frontends.values():
            try:
                await fe.start()
                log.info("Frontend '%s' started", fe.name)
            except Exception:
                log.exception("Failed to start frontend '%s'", fe.name)

    async def stop_all(self) -> None:
        """Stop all registered frontends."""
        for fe in self.frontends.values():
            try:
                await fe.stop()
                log.info("Frontend '%s' stopped", fe.name)
            except Exception:
                log.warning("Failed to stop frontend '%s'", fe.name, exc_info=True)

    # ------------------------------------------------------------------
    # Backward compat: generate FrontendCallbacks
    # ------------------------------------------------------------------

    def as_callbacks(self) -> FrontendCallbacks:
        """Build a FrontendCallbacks that delegates to this router.

        This lets the router integrate with existing AgentHub code that
        expects FrontendCallbacks, while routing all events through the
        new Frontend protocol.
        """
        router = self

        async def post_message(agent_name: str, text: str) -> None:
            await router.post_message(agent_name, text)

        async def post_system(agent_name: str, text: str) -> None:
            await router.post_system(agent_name, text)

        async def on_wake(agent_name: str) -> None:
            await router.on_wake(agent_name)

        async def on_sleep(agent_name: str) -> None:
            await router.on_sleep(agent_name)

        async def on_session_id(agent_name: str, session_id: str) -> None:
            await router.on_session_id(agent_name, session_id)

        async def get_channel(agent_name: str) -> Any:
            return await router.get_channel(agent_name)

        async def on_spawn(session: Any) -> None:
            await router.on_spawn(session.name, session)

        async def on_kill(agent_name: str, session_id: str | None) -> None:
            await router.on_kill(agent_name, session_id)

        async def broadcast(text: str) -> None:
            await router.broadcast(text)

        async def schedule_rate_limit_expiry(seconds: float) -> None:
            pass  # Handled by rate limit subsystem

        async def on_idle_reminder(agent_name: str, idle_minutes: float) -> None:
            await router.on_idle_reminder(agent_name, idle_minutes)

        async def on_reconnect(agent_name: str, was_mid_task: bool) -> None:
            await router.on_reconnect(agent_name, was_mid_task)

        async def close_app() -> None:
            await router.close_app()

        async def kill_process() -> None:
            await router.kill_process()

        async def send_goodbye() -> None:
            await router.send_goodbye()

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
            send_goodbye=send_goodbye,
        )
