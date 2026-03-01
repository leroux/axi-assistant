"""Frontend callback protocol for Agent Hub.

Defines the interface that a frontend (Discord, CLI, etc.) implements
to receive notifications from the agent orchestration layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class PostMessageFn(Protocol):
    async def __call__(self, agent_name: str, text: str) -> None: ...


class PostSystemFn(Protocol):
    async def __call__(self, agent_name: str, text: str) -> None: ...


class OnLifecycleFn(Protocol):
    async def __call__(self, agent_name: str) -> None: ...


class OnSessionIdFn(Protocol):
    async def __call__(self, agent_name: str, session_id: str) -> None: ...


class GetChannelFn(Protocol):
    async def __call__(self, agent_name: str) -> Any: ...


@dataclass
class FrontendCallbacks:
    """Callbacks from Agent Hub to the frontend layer.

    All callbacks are async. The hub calls these to notify the frontend
    about agent lifecycle events and to send messages to users.
    """

    post_message: PostMessageFn  # Send a message to the agent's channel
    post_system: PostSystemFn  # Send a system notification
    on_wake: OnLifecycleFn  # Agent woke up
    on_sleep: OnLifecycleFn  # Agent went to sleep
    on_session_id: OnSessionIdFn  # Persist session ID
    get_channel: GetChannelFn  # Get frontend channel for an agent
