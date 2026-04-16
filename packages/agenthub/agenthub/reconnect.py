"""Bridge reconnect support is not yet ported to the rewritten AgentHub runtime."""

from __future__ import annotations


async def connect_procmux(*args, **kwargs):  # pragma: no cover - explicit failure surface
    raise RuntimeError(
        "agenthub.reconnect is not yet ported to the rewritten runtime. "
        "Reconnect must be reimplemented against agenthub.runtime.AgentHub before use."
    )


async def reconnect_single(*args, **kwargs):  # pragma: no cover - explicit failure surface
    raise RuntimeError(
        "agenthub.reconnect is not yet ported to the rewritten runtime."
    )
