"""Agent Hub -- multi-agent orchestration primitives.

Provides the core abstractions for managing multiple Claude Code agents:
- BackgroundTaskSet: GC-safe fire-and-forget async task management
- ConcurrencyLimitError: raised when awake-agent slots are exhausted
- FrontendCallbacks: protocol for frontend integration (Discord, etc.)
"""

from agenthub.callbacks import FrontendCallbacks
from agenthub.tasks import BackgroundTaskSet
from agenthub.types import ConcurrencyLimitError

__all__ = [
    "BackgroundTaskSet",
    "ConcurrencyLimitError",
    "FrontendCallbacks",
]
