"""Tests for the Frontend protocol and PlanApprovalResult."""

from __future__ import annotations

from agenthub.frontend import PlanApprovalResult


def test_plan_approval_result_defaults() -> None:
    result = PlanApprovalResult(approved=True)
    assert result.approved is True
    assert result.message == ""


def test_plan_approval_result_message() -> None:
    result = PlanApprovalResult(approved=False, message="needs changes")
    assert result.approved is False
    assert result.message == "needs changes"
