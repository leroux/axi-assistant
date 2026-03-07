"""AgentHub — multi-agent session orchestrator.

Thin state holder. Logic lives in lifecycle.py, registry.py, messaging.py,
reconnect.py, etc. Methods are thin delegation to module functions.
State is public — module functions access it directly.

Per CODE-PHILOSOPHY.md: no god objects, no hidden state, explicit data flow.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from agenthub import lifecycle, messaging, reconnect, registry
from agenthub.rate_limits import RateLimitTracker
from agenthub.scheduler import Scheduler
from agenthub.tasks import BackgroundTaskSet
from agenthub.types import AgentSession

if TYPE_CHECKING:
    from agenthub.callbacks import FrontendCallbacks

log = logging.getLogger(__name__)

# SDK factory types — injected by the frontend at construction.
# AgentHub doesn't import ClaudeAgentOptions or ClaudeSDKClient directly.
MakeOptionsFn = Callable[[AgentSession, str | None], Any]  # (session, resume_id) -> options
CreateClientFn = Callable[[AgentSession, Any], Awaitable[Any]]  # (session, options) -> client
DisconnectClientFn = Callable[[Any, str], Awaitable[None]]  # (client, name) -> None


class AgentHub:
    """Multi-agent session orchestrator.

    Owns the state. Logic lives in peer modules (lifecycle, registry,
    messaging, reconnect). Methods are thin delegation — the class
    exists to hold state that multiple modules need to share.
    """

    def __init__(
        self,
        *,
        # Concurrency
        max_awake: int,
        protected: set[str],
        # Frontend
        callbacks: FrontendCallbacks,
        # SDK factories
        make_agent_options: MakeOptionsFn,
        create_client: CreateClientFn,
        disconnect_client: DisconnectClientFn,
        # Timeouts and retry
        query_timeout: float = 300.0,
        max_retries: int = 3,
        retry_base_delay: float = 15.0,
        slot_timeout: float = 120.0,
        # Paths (for rate limit / usage history)
        usage_history_path: str | None = None,
        rate_limit_history_path: str | None = None,
    ) -> None:
        # Session state — public, module functions access directly
        self.sessions: dict[str, AgentSession] = {}
        self.callbacks = callbacks

        # SDK factories
        self.make_agent_options = make_agent_options
        self.create_client = create_client
        self.disconnect_client = disconnect_client

        # Subsystems
        self.scheduler = Scheduler(
            max_slots=max_awake,
            protected=protected,
            get_sessions=lambda: self.sessions,
            sleep_fn=lambda s: lifecycle.sleep_agent(self, s),
        )
        self.rate_limits = RateLimitTracker(
            usage_history_path=usage_history_path,
            rate_limit_history_path=rate_limit_history_path,
        )
        self.tasks = BackgroundTaskSet()

        # Concurrency
        self.wake_lock = asyncio.Lock()

        # Bridge connection (set by reconnect.connect_procmux)
        self.process_conn: Any = None  # ProcmuxProcessConnection | None
        self.raw_procmux_conn: Any = None  # ProcmuxConnection | None

        # Config
        self.query_timeout = query_timeout
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.slot_timeout = slot_timeout
        self.protected = protected

        # Shutdown flag — set by ShutdownCoordinator
        self.shutdown_requested = False

    # ------------------------------------------------------------------
    # Thin delegation — keeps imports clean for callers
    # ------------------------------------------------------------------

    # Lifecycle
    async def wake(self, name: str) -> None:
        await lifecycle.wake_agent(self, self.sessions[name])

    async def sleep(self, name: str) -> None:
        await lifecycle.sleep_agent(self, self.sessions[name])

    # Registry
    async def spawn(self, **kw: Any) -> AgentSession:
        return await registry.spawn_agent(self, **kw)

    async def kill(self, name: str) -> None:
        await registry.end_session(self, name)

    def get(self, name: str) -> AgentSession | None:
        return self.sessions.get(name)

    # Messaging
    async def send(
        self,
        name: str,
        content: Any,
        stream_handler: messaging.StreamHandlerFn,
    ) -> None:
        await messaging.process_message(
            self, self.sessions[name], content, stream_handler
        )

    async def receive_user_message(
        self,
        name: str,
        content: Any,
        stream_handler: messaging.StreamHandlerFn,
        *,
        queue_item: Any = None,
    ) -> messaging.ReceiveResult:
        return await messaging.receive_user_message(
            self, self.sessions[name], content, stream_handler,
            queue_item=queue_item,
        )

    # Reconnect
    async def connect_bridge(self, socket_path: str) -> None:
        await reconnect.connect_procmux(self, socket_path)
