"""Session lifecycle helpers for Claude CLI processes.

Functions for managing ClaudeSDKClient subprocess lifecycle:
disconnect, PID extraction, process cleanup, and stdio logging.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

log = logging.getLogger(__name__)


def get_subprocess_pid(client: object) -> int | None:
    """Extract the PID of the underlying CLI subprocess from a ClaudeSDKClient."""
    try:
        transport = getattr(client, "_transport", None) or getattr(getattr(client, "_query", None), "transport", None)
        if transport is None:
            return None
        proc = getattr(transport, "_process", None)
        if proc is None:
            return None
        return proc.pid  # type: ignore[no-any-return]
    except Exception:
        return None


def ensure_process_dead(pid: int | None, label: str) -> None:
    """Send SIGTERM to *pid* if it is still alive."""
    if pid is None:
        return
    try:
        os.kill(pid, 0)
    except OSError:
        return
    log.warning("Subprocess %d for '%s' survived disconnect \u2014 sending SIGTERM (SDK bug workaround)", pid, label)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


async def disconnect_client(client: object, label: str) -> None:
    """Disconnect a ClaudeSDKClient and ensure its subprocess is terminated.

    If the client's transport has an async close() method (e.g. BridgeTransport),
    uses that. Otherwise falls back to __aexit__ + process cleanup.
    """
    transport = getattr(client, "_transport", None)
    # Duck-type check: any transport with an async close() method
    if hasattr(transport, "close") and asyncio.iscoroutinefunction(getattr(transport, "close", None)):
        try:
            await asyncio.wait_for(transport.close(), timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            log.warning("'%s' transport close timed out", label)
        except Exception:
            log.exception("'%s' error closing transport", label)
        return

    pid = get_subprocess_pid(client)
    try:
        await asyncio.wait_for(client.__aexit__(None, None, None), timeout=5.0)  # type: ignore[union-attr]
    except (TimeoutError, asyncio.CancelledError):
        log.warning("'%s' shutdown timed out or was cancelled", label)
    except RuntimeError as e:
        if "cancel scope" in str(e):
            log.debug("'%s' cross-task cleanup (expected): %s", label, e)
        else:
            raise
    ensure_process_dead(pid, label)


def get_stdio_logger(agent_name: str, log_dir: str) -> logging.Logger:
    """Get or create a per-agent bridge stdio logger."""
    from logging.handlers import RotatingFileHandler

    name = f"bridge.stdio.{agent_name}"
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        fh = RotatingFileHandler(
            os.path.join(log_dir, f"bridge-stdio-{agent_name}.log"),
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=2,
        )
        fh.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s %(message)s")
        fmt.converter = time.gmtime
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger
