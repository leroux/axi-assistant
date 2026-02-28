"""Minimal conftest for unit tests — no Discord, no external services."""

import pytest


@pytest.fixture(autouse=True)
def _ensure_warm():
    """Override the E2E autouse fixture so unit tests skip Discord warmup."""
