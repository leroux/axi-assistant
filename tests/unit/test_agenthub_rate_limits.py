"""Unit tests for AgentHub rate limit helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agenthub.rate_limits import (
    RateLimitTracker,
    format_time_remaining,
    handle_rate_limit,
    is_rate_limited,
    parse_rate_limit_seconds,
    rate_limit_remaining_seconds,
    record_session_usage,
    update_rate_limit_quota,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_parse_rate_limit_seconds_examples() -> None:
    assert parse_rate_limit_seconds("Rate limited, try again in 30 seconds") == 30
    assert parse_rate_limit_seconds("Try again after 5 minutes") == 300
    assert parse_rate_limit_seconds("Retry after 2 mins") == 120
    assert parse_rate_limit_seconds("Rate limit expires in 1 hour") == 3600
    assert parse_rate_limit_seconds("Please wait, retry in 2 hrs") == 7200
    assert parse_rate_limit_seconds("something went wrong") == 300


@given(
    value=st.integers(min_value=1, max_value=500),
    unit=st.sampled_from([
        ("second", 1),
        ("seconds", 1),
        ("minute", 60),
        ("minutes", 60),
        ("min", 60),
        ("mins", 60),
        ("hour", 3600),
        ("hours", 3600),
        ("hr", 3600),
        ("hrs", 3600),
    ]),
    prefix=st.sampled_from(["retry after", "try again in", "rate limit expires in"]),
)
def test_parse_rate_limit_seconds_property(value: int, unit: tuple[str, int], prefix: str) -> None:
    unit_text, multiplier = unit
    text = f"{prefix} {value} {unit_text}"
    assert parse_rate_limit_seconds(text) == value * multiplier


def test_format_time_remaining_examples() -> None:
    assert format_time_remaining(30) == "30s"
    assert format_time_remaining(120) == "2m"
    assert format_time_remaining(125) == "2m 5s"
    assert format_time_remaining(3600) == "1h"
    assert format_time_remaining(3660) == "1h 1m"


def test_is_rate_limited_and_remaining_seconds() -> None:
    tracker = RateLimitTracker()
    assert is_rate_limited(tracker) is False
    assert rate_limit_remaining_seconds(tracker) == 0

    tracker.rate_limited_until = datetime.now(UTC) + timedelta(seconds=30)
    assert is_rate_limited(tracker) is True
    assert 0 < rate_limit_remaining_seconds(tracker) <= 30

    tracker.rate_limited_until = datetime.now(UTC) - timedelta(seconds=1)
    assert is_rate_limited(tracker) is False
    assert tracker.rate_limited_until is None


def test_record_session_usage_aggregates_and_persists(tmp_path: Path) -> None:
    history_path = tmp_path / "usage.jsonl"
    tracker = RateLimitTracker(usage_history_path=str(history_path))
    msg = SimpleNamespace(
        session_id="sid-1",
        total_cost_usd=0.2,
        num_turns=2,
        duration_ms=150,
        duration_api_ms=140,
        is_error=False,
        usage={"input_tokens": 10, "output_tokens": 20},
    )

    record_session_usage(tracker, "agent-1", msg)
    record_session_usage(tracker, "agent-1", msg)

    usage = tracker.session_usage["sid-1"]
    assert usage.agent_name == "agent-1"
    assert usage.queries == 2
    assert usage.total_cost_usd == 0.4
    assert usage.total_turns == 4
    assert usage.total_duration_ms == 300
    assert usage.total_input_tokens == 20
    assert usage.total_output_tokens == 40

    lines = history_path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["session_id"] == "sid-1"


def test_update_rate_limit_quota_preserves_utilization_when_missing(tmp_path: Path) -> None:
    history_path = tmp_path / "quotas.jsonl"
    tracker = RateLimitTracker(rate_limit_history_path=str(history_path))
    resets_at = datetime.now(UTC) + timedelta(minutes=10)

    update_rate_limit_quota(
        tracker,
        SimpleNamespace(rate_limit_type="tokens", status="limited", utilization=0.8, resets_at=resets_at),
    )
    update_rate_limit_quota(
        tracker,
        SimpleNamespace(rate_limit_type="tokens", status="limited", utilization=None, resets_at=resets_at),
    )

    quota = tracker.rate_limit_quotas["tokens"]
    assert quota.status == "limited"
    assert quota.utilization == 0.8
    assert len(history_path.read_text().splitlines()) == 2


@pytest.mark.asyncio
async def test_handle_rate_limit_sets_state_and_broadcasts_once() -> None:
    tracker = RateLimitTracker()
    broadcasts: list[str] = []
    expiries: list[float] = []

    async def broadcast(text: str) -> None:
        broadcasts.append(text)

    def schedule_expiry(seconds: float) -> None:
        expiries.append(seconds)

    await handle_rate_limit(tracker, "retry after 12 seconds", broadcast, schedule_expiry)
    first_limit = tracker.rate_limited_until
    await handle_rate_limit(tracker, "retry after 5 seconds", broadcast, schedule_expiry)

    assert first_limit is not None
    assert tracker.rate_limited_until is not None
    assert tracker.rate_limited_until >= first_limit
    assert len(broadcasts) == 1
    assert "Rate limited by Claude API" in broadcasts[0]
    assert expiries == [12]


@given(st.lists(st.integers(min_value=1, max_value=600), min_size=1, max_size=8))
@pytest.mark.asyncio
async def test_handle_rate_limit_sequence_is_monotonic_and_notifies_once(wait_seconds: list[int]) -> None:
    tracker = RateLimitTracker()
    broadcasts: list[str] = []
    expiries: list[float] = []
    seen_limits: list[datetime] = []

    async def broadcast(text: str) -> None:
        broadcasts.append(text)

    def schedule_expiry(seconds: float) -> None:
        expiries.append(seconds)

    running_max = 0
    for seconds in wait_seconds:
        running_max = max(running_max, seconds)
        await handle_rate_limit(tracker, f"retry after {seconds} seconds", broadcast, schedule_expiry)
        assert tracker.rate_limited_until is not None
        seen_limits.append(tracker.rate_limited_until)

    assert seen_limits == sorted(seen_limits)
    assert len(broadcasts) == 1
    assert len(expiries) == 1
    assert expiries[0] == wait_seconds[0]
    assert "Rate limited by Claude API" in broadcasts[0]
    remaining = rate_limit_remaining_seconds(tracker)
    assert max(0, running_max - 2) <= remaining <= running_max
