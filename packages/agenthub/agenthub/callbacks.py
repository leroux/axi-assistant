"""Legacy callback shim retained only as a hard failure.

The rewritten package uses the Frontend protocol directly; FrontendCallbacks is removed.
"""

from __future__ import annotations


class FrontendCallbacks:  # pragma: no cover - explicit failure surface
    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError(
            "FrontendCallbacks has been removed. Use agenthub.frontend.Frontend and "
            "agenthub.runtime.AgentHub with direct frontends instead."
        )
