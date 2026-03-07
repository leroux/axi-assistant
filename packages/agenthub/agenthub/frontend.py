"""Frontend protocol — the interface every frontend adapter must implement.

Each frontend (Discord, Web, Slack) implements this protocol to receive
notifications from AgentHub and render them to users. The FrontendRouter
multiplexes callbacks to all registered frontends.

This replaces the flat FrontendCallbacks dataclass with a richer protocol
that includes stream rendering, interactive gates, and channel management.
FrontendCallbacks still exists for backward compat — FrontendRouter can
generate one from registered frontends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agenthub.agent_log import LogEvent
    from agenthub.stream_types import StreamOutput

# ---------------------------------------------------------------------------
# Plan approval result
# ---------------------------------------------------------------------------


class PlanApprovalResult:
    """Result from a frontend's plan approval gate."""

    __slots__ = ("approved", "message")

    def __init__(self, approved: bool, message: str = "") -> None:
        self.approved = approved
        self.message = message


# ---------------------------------------------------------------------------
# Frontend protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Frontend(Protocol):
    """Protocol that every frontend adapter must implement.

    Methods are grouped by concern. All are async. A frontend that doesn't
    need a particular method can provide a no-op implementation.

    The `name` property identifies this frontend ("discord", "web", "slack").
    """

    @property
    def name(self) -> str:
        """Unique frontend identifier."""
        ...

    # --- Lifecycle ---

    async def start(self) -> None:
        """Start the frontend (connect to service, bind ports, etc.)."""
        ...

    async def stop(self) -> None:
        """Gracefully shut down the frontend."""
        ...

    # --- Outbound: hub -> frontend ---

    async def post_message(self, agent_name: str, text: str) -> None:
        """Send an assistant message to the user."""
        ...

    async def post_system(self, agent_name: str, text: str) -> None:
        """Send a system notification to the user."""
        ...

    async def broadcast(self, text: str) -> None:
        """Send a message to all users (e.g. rate limit announcements)."""
        ...

    # --- Agent lifecycle events ---

    async def on_wake(self, agent_name: str) -> None:
        """Agent woke up (CLI process started)."""
        ...

    async def on_sleep(self, agent_name: str) -> None:
        """Agent went to sleep (CLI process stopped)."""
        ...

    async def on_spawn(self, agent_name: str, session: Any) -> None:
        """New agent spawned."""
        ...

    async def on_kill(self, agent_name: str, session_id: str | None) -> None:
        """Agent killed."""
        ...

    async def on_session_id(self, agent_name: str, session_id: str) -> None:
        """Agent's session ID updated."""
        ...

    async def on_idle_reminder(self, agent_name: str, idle_minutes: float) -> None:
        """Agent has been idle for a while."""
        ...

    async def on_reconnect(self, agent_name: str, was_mid_task: bool) -> None:
        """Agent reconnected after a hot restart."""
        ...

    # --- Stream rendering ---

    async def on_stream_event(self, agent_name: str, event: StreamOutput) -> None:
        """Handle a normalized stream output event.

        Called for each StreamOutput yielded by the streaming engine.
        The frontend renders it appropriately for its platform.
        """
        ...

    # --- Interactive gates ---

    async def request_plan_approval(
        self, agent_name: str, plan_content: str, session: Any
    ) -> PlanApprovalResult:
        """Present a plan to the user and wait for approval/rejection."""
        ...

    async def ask_question(
        self, agent_name: str, questions: list[dict[str, Any]], session: Any
    ) -> dict[str, str]:
        """Ask the user one or more questions and return answers."""
        ...

    async def update_todo(self, agent_name: str, todos: list[dict[str, Any]]) -> None:
        """Notify the frontend of a todo list update."""
        ...

    # --- Channel/room management ---

    async def ensure_channel(self, agent_name: str, cwd: str | None = None) -> Any:
        """Ensure the agent has a channel/room in this frontend. Return channel handle."""
        ...

    async def move_to_killed(self, agent_name: str) -> None:
        """Move the agent's channel to the "killed" area."""
        ...

    async def get_channel(self, agent_name: str) -> Any:
        """Get the channel handle for an agent, or None."""
        ...

    # --- Session persistence ---

    async def save_session_metadata(self, agent_name: str, session: Any) -> None:
        """Persist session metadata (session ID, state) in frontend's storage."""
        ...

    async def reconstruct_sessions(self) -> list[dict[str, Any]]:
        """Reconstruct session metadata from frontend's storage (e.g. channel topics)."""
        ...

    # --- Event log integration ---

    async def on_log_event(self, event: LogEvent) -> None:
        """Called when a new event is appended to an agent's log.

        Frontends can use this to push real-time updates to connected clients.
        Default: no-op (frontends that don't need real-time can ignore this).
        """
        ...

    # --- Shutdown ---

    async def send_goodbye(self) -> None:
        """Send a goodbye message before shutdown."""
        ...

    async def close_app(self) -> None:
        """Close the application (bot.close(), server.shutdown(), etc.)."""
        ...

    async def kill_process(self) -> None:
        """Forcefully kill the process."""
        ...
