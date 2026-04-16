"""Tests for agenthub types and top-level exports."""

from __future__ import annotations

import pytest

from agenthub import (
    AgentHub,
    BackgroundTaskSet,
    ConcurrencyLimitError,
    FrontendRouter,
    LifecycleState,
    ProcmuxProcessConnection,
    TurnKind,
    TurnOutcome,
)
from agenthub.types import ConcurrencyLimitError as DirectImport


class TestConcurrencyLimitError:
    def test_is_exception(self) -> None:
        err = ConcurrencyLimitError("max 3 agents")
        assert isinstance(err, Exception)
        assert str(err) == "max 3 agents"

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(ConcurrencyLimitError, match="slots full"):
            raise ConcurrencyLimitError("slots full")

    def test_re_exported_from_package(self) -> None:
        assert ConcurrencyLimitError is DirectImport


class TestPackageExports:
    def test_all_exports_importable(self) -> None:
        assert AgentHub is not None
        assert BackgroundTaskSet is not None
        assert ConcurrencyLimitError is not None
        assert FrontendRouter is not None
        assert LifecycleState is not None
        assert TurnKind is not None
        assert TurnOutcome is not None
        assert ProcmuxProcessConnection is not None
