"""Frontend callback protocol for Agent Hub.

Defines the interface that a frontend (Discord, CLI, etc.) implements
to receive notifications from the agent orchestration layer.

All callbacks are async. The hub calls these to notify the frontend
about agent lifecycle events and to send messages to users.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# Callback type aliases — plain callables, no Protocol classes needed.
# Each documents its signature in the FrontendCallbacks docstring.
PostMessageFn = Callable[[str, str], Awaitable[None]]  # (agent_name, text)
PostSystemFn = Callable[[str, str], Awaitable[None]]  # (agent_name, text)
OnLifecycleFn = Callable[[str], Awaitable[None]]  # (agent_name)
OnSessionIdFn = Callable[[str, str], Awaitable[None]]  # (agent_name, session_id)
GetChannelFn = Callable[[str], Awaitable[Any]]  # (agent_name) -> channel
OnSpawnFn = Callable[[Any], Awaitable[None]]  # (session)
OnKillFn = Callable[[str, str | None], Awaitable[None]]  # (agent_name, session_id)
BroadcastFn = Callable[[str], Awaitable[None]]  # (message)
ScheduleExpiryFn = Callable[[float], Awaitable[None]]  # (delay_secs)
OnIdleFn = Callable[[str, float], Awaitable[None]]  # (agent_name, idle_minutes)
OnReconnectFn = Callable[[str, bool], Awaitable[None]]  # (agent_name, was_mid_task)
CloseAppFn = Callable[[], Awaitable[None]]
KillProcessFn = Callable[[], Awaitable[None]]
GoodbyeFn = Callable[[], Awaitable[None]]


@dataclass
class FrontendCallbacks:
    """Callbacks from Agent Hub to the frontend layer.

    Required callbacks must be provided at init. Optional callbacks
    default to no-ops. Add new callbacks as needed when extracting
    modules — start minimal.
    """

    # Core messaging
    post_message: PostMessageFn
    post_system: PostSystemFn

    # Lifecycle events
    on_wake: OnLifecycleFn
    on_sleep: OnLifecycleFn
    on_session_id: OnSessionIdFn
    get_channel: GetChannelFn

    # Session events
    on_spawn: OnSpawnFn
    on_kill: OnKillFn

    # Broadcast (rate limits, system announcements)
    broadcast: BroadcastFn
    schedule_rate_limit_expiry: ScheduleExpiryFn

    # Idle monitoring
    on_idle_reminder: OnIdleFn

    # Hot restart
    on_reconnect: OnReconnectFn

    # Shutdown
    close_app: CloseAppFn
    kill_process: KillProcessFn
    send_goodbye: GoodbyeFn | None = None
