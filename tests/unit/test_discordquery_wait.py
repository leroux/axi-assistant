from __future__ import annotations

from discordquery.wait import wait_for_messages


class FakeClient:
    def __init__(self, responses: list[list[dict]]):
        self._responses = responses
        self._index = 0

    def get_messages(self, channel_id: str, limit: int = 100, after: str | None = None):
        if self._index >= len(self._responses):
            return []
        response = self._responses[self._index]
        self._index += 1
        return response


def _msg(message_id: str, content: str, author_id: str = "bot") -> dict:
    return {
        "id": message_id,
        "content": content,
        "author": {"id": author_id, "username": author_id},
        "timestamp": "2026-04-17T00:00:00+00:00",
    }


def test_wait_for_messages_returns_first_batch_when_stable_polls_zero() -> None:
    client = FakeClient([[ _msg("2", "hello") ]])
    messages, cursor = wait_for_messages(
        client,
        "chan",
        "1",
        timeout=1.0,
        ignore_author_ids=set(),
        stable_polls=0,
        poll_interval=0.0,
    )
    assert [m["content"] for m in messages] == ["hello"]
    assert cursor == "2"


def test_wait_for_messages_returns_on_substring_match() -> None:
    client = FakeClient([
        [_msg("2", "still waiting")],
        [_msg("3", "target acquired")],
    ])
    messages, cursor = wait_for_messages(
        client,
        "chan",
        "1",
        timeout=1.0,
        ignore_author_ids=set(),
        substring="target",
        stable_polls=0,
        poll_interval=0.0,
    )
    assert [m["content"] for m in messages] == ["target acquired"]
    assert cursor == "3"


def test_wait_for_messages_returns_after_stability_window() -> None:
    client = FakeClient([
        [_msg("2", "first")],
        [],
        [],
    ])
    messages, cursor = wait_for_messages(
        client,
        "chan",
        "1",
        timeout=1.0,
        ignore_author_ids=set(),
        stable_polls=2,
        poll_interval=0.0,
    )
    assert [m["content"] for m in messages] == ["first"]
    assert cursor == "2"


def test_wait_for_messages_advances_cursor_past_filtered_messages() -> None:
    client = FakeClient([[ _msg("5", "ignored", author_id="me") ]])
    messages, cursor = wait_for_messages(
        client,
        "chan",
        "1",
        timeout=1.0,
        ignore_author_ids={"me"},
        stable_polls=0,
        poll_interval=0.0,
    )
    assert messages == []
    assert cursor == "5"
