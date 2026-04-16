"""Legacy messaging module removed.

The rewritten package uses agenthub.runtime.AgentHub submission methods and the
session reducer directly.
"""

from __future__ import annotations


def __getattr__(name: str):  # pragma: no cover - explicit failure surface
    raise RuntimeError(
        "agenthub.messaging has been removed. Use agenthub.runtime.AgentHub methods instead."
    )
