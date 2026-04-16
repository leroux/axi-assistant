"""Legacy registry module removed.

The rewritten package uses agenthub.runtime.AgentHub to own session lifecycle.
"""

from __future__ import annotations


def __getattr__(name: str):  # pragma: no cover - explicit failure surface
    raise RuntimeError(
        "agenthub.registry has been removed. Use agenthub.runtime.AgentHub methods instead."
    )
