"""Legacy lifecycle module removed.

The rewritten package uses agenthub.runtime.AgentHub as the execution boundary.
"""

from __future__ import annotations


def __getattr__(name: str):  # pragma: no cover - explicit failure surface
    raise RuntimeError(
        "agenthub.lifecycle has been removed. Use agenthub.runtime.AgentHub methods instead."
    )
