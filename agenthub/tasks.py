"""Background task management with GC protection.

In Python 3.12+ the event loop only keeps weak references to tasks,
so untracked fire-and-forget tasks may be collected before completion.
BackgroundTaskSet prevents this by holding strong references until done.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class BackgroundTaskSet:
    """Holds strong references to fire-and-forget asyncio tasks.

    Prevents garbage collection of tasks before completion and logs
    any unhandled exceptions.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def fire_and_forget(self, coro: Any) -> asyncio.Task[None]:
        """Schedule a coroutine as a background task, preventing GC before completion."""
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[None]) -> None:
        """Remove finished task and log any unhandled exception."""
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error(
                "Background task %s failed: %s",
                task.get_name(),
                task.exception(),
                exc_info=task.exception(),
            )

    def __len__(self) -> int:
        return len(self._tasks)
