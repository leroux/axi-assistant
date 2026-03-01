"""Core types for agent orchestration."""

from __future__ import annotations


class ConcurrencyLimitError(Exception):
    """Raised when the awake-agent concurrency limit is reached and no slots can be freed."""
