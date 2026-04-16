"""FrontendRouter — multiplexes frontend protocol calls without callback shims."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from agenthub.frontend import Frontend, PlanApprovalResult

if TYPE_CHECKING:
    from agenthub.agent_log import LogEvent
    from agenthub.stream_types import StreamOutput

log = logging.getLogger(__name__)


class FrontendRouter:
    def __init__(self) -> None:
        self.frontends: dict[str, Frontend] = {}

    def add(self, frontend: Frontend) -> None:
        self.frontends[frontend.name] = frontend
        log.info("Frontend '%s' registered", frontend.name)

    def remove(self, name: str) -> Frontend | None:
        return self.frontends.pop(name, None)

    def get(self, name: str) -> Frontend | None:
        return self.frontends.get(name)

    async def _broadcast(self, method: str, *args: Any, **kwargs: Any) -> None:
        for fe in self.frontends.values():
            fn = getattr(fe, method, None)
            if fn is None:
                continue
            try:
                await fn(*args, **kwargs)
            except Exception:
                log.warning("Frontend '%s'.%s failed", fe.name, method, exc_info=True)

    async def _first_response(self, method: str, *args: Any, **kwargs: Any) -> Any:
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
        done, pending = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
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

    async def post_message(self, agent_name: str, text: str) -> None:
        await self._broadcast("post_message", agent_name, text)

    async def post_system(self, agent_name: str, text: str) -> None:
        await self._broadcast("post_system", agent_name, text)

    async def broadcast(self, text: str) -> None:
        await self._broadcast("broadcast", text)

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

    async def on_stream_event(self, agent_name: str, event: StreamOutput) -> None:
        await self._broadcast("on_stream_event", agent_name, event)

    async def request_plan_approval(
        self, agent_name: str, plan_content: str, session: Any
    ) -> PlanApprovalResult:
        result = await self._first_response("request_plan_approval", agent_name, plan_content, session)
        return result if result is not None else PlanApprovalResult(approved=True)

    async def ask_question(
        self, agent_name: str, questions: list[dict[str, Any]], session: Any
    ) -> dict[str, str]:
        result = await self._first_response("ask_question", agent_name, questions, session)
        return result or {}

    async def update_todo(self, agent_name: str, todos: list[dict[str, Any]]) -> None:
        await self._broadcast("update_todo", agent_name, todos)

    async def on_log_event(self, event: LogEvent) -> None:
        await self._broadcast("on_log_event", event)

    async def start_all(self) -> None:
        for fe in self.frontends.values():
            await fe.start()

    async def stop_all(self) -> None:
        for fe in self.frontends.values():
            await fe.stop()
