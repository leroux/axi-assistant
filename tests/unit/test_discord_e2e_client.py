from __future__ import annotations

from discord_e2e import DiscordChannel, DiscordE2EClient


class StubWaitClient:
    def __init__(self) -> None:
        self.channel_requests: list[tuple[str | int, str]] = []

    def close(self) -> None:
        return None

    def list_channels(self, guild_id: str) -> list[dict[str, str | int | None]]:
        self.channel_requests.append((guild_id, "list"))
        return [{"id": "123", "name": "general", "type": "text", "category": None, "position": 0}]

    def find_channel(self, guild_id: str, name: str) -> dict[str, str] | None:
        self.channel_requests.append((guild_id, name))
        if name == "general":
            return {"id": "123", "name": "general"}
        return None

    def get(self, path: str) -> list[dict[str, str | int]]:
        assert path == "/guilds/guild/channels"
        return [{"id": "9", "name": "bots", "type": 4}]

    def create_channel(self, guild_id: str, name: str, *, parent_id: str | None = None) -> dict[str, str]:
        assert guild_id == "guild"
        assert name == "tests"
        assert parent_id == "9"
        return {"id": "456"}

    def delete_channel(self, channel_id: str) -> None:
        assert channel_id == "456"

    def get_channel(self, channel_id: str) -> dict[str, str]:
        return {"id": channel_id, "name": "tests"}

    def get_messages(self, channel_id: str, *, limit: int = 50, after: str | None = None) -> list[dict]:
        assert channel_id == "123"
        return [{"id": "222", "content": "hello", "author": {"id": "bot"}}]


class StubSenderClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def close(self) -> None:
        return None

    def send_message(self, channel_id: str, content: str) -> dict[str, str]:
        self.sent.append((channel_id, content))
        return {"id": "111"}


class StubFrameworkClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def send(self, channel_id: str, content: str) -> str:
        self.calls.append(("send", (channel_id, content), {}))
        return "101"

    def history(self, channel_id: str, limit: int = 50, after: str | None = None) -> list[dict[str, str]]:
        self.calls.append(("history", (channel_id,), {"limit": limit, "after": after}))
        return [{"id": "102", "content": "ok"}]

    def latest_message_id(self, channel_id: str) -> str | None:
        self.calls.append(("latest_message_id", (channel_id,), {}))
        return "103"

    def wait_for_messages(self, channel_id: str, **kwargs: object):
        self.calls.append(("wait_for_messages", (channel_id,), kwargs))
        return object()

    def wait_for_bot_response(self, channel_id: str, **kwargs: object):
        self.calls.append(("wait_for_bot_response", (channel_id,), kwargs))
        return object()

    def send_and_wait(self, channel_id: str, content: str, **kwargs: object):
        self.calls.append(("send_and_wait", (channel_id, content), kwargs))
        return object()


def test_discord_e2e_client_uses_reusable_transport() -> None:
    client = DiscordE2EClient(reader_token="bot", sender_token="ZmFrZQ==.rest", guild_id="guild")
    client._reader = StubWaitClient()  # type: ignore[assignment]
    client._sender_client = StubSenderClient()  # type: ignore[assignment]

    assert client.find_channel("general") == "123"
    assert client.require_channel("general").channel_id == "123"
    assert client.find_category("bots") == "9"
    assert client.create_channel("tests", parent_id="9") == "456"
    assert client.channel_info("456")["name"] == "tests"
    assert client.latest_message_id("123") == "222"
    assert client.send("123", "ping") == "111"
    assert client.history("123")[0]["content"] == "hello"
    client.delete_channel("456")


def test_find_channel_by_prefix_matches_first_hit() -> None:
    class PrefixStub:
        def close(self) -> None:
            return None

        def list_channels(self, guild_id: str) -> list[dict[str, object]]:
            return [
                {"id": "1", "name": "general"},
                {"id": "2", "name": "smoke-probe-abc"},
                {"id": "3", "name": "smoke-probe-xyz"},
            ]

    client = DiscordE2EClient(reader_token="bot", sender_token="ZmFrZQ==.rest", guild_id="guild")
    client._reader = PrefixStub()  # type: ignore[assignment]

    hit = client.find_channel_by_prefix("smoke-probe")
    assert hit is not None
    assert hit["id"] == "2"
    assert client.find_channel_by_prefix("no-such-") is None


def test_wait_for_channel_returns_immediately_when_present() -> None:
    class PrefixStub:
        def close(self) -> None:
            return None

        def list_channels(self, guild_id: str) -> list[dict[str, object]]:
            return [{"id": "42", "name": "smoke-probe-now"}]

    client = DiscordE2EClient(reader_token="bot", sender_token="ZmFrZQ==.rest", guild_id="guild")
    client._reader = PrefixStub()  # type: ignore[assignment]

    channel = client.wait_for_channel("smoke-probe-", timeout=1.0, poll_interval=0.1)
    assert channel.channel_id == "42"
    assert channel.name == "smoke-probe-now"


def test_wait_for_channel_raises_on_timeout() -> None:
    import pytest

    class EmptyStub:
        def close(self) -> None:
            return None

        def list_channels(self, guild_id: str) -> list[dict[str, object]]:
            return []

    client = DiscordE2EClient(reader_token="bot", sender_token="ZmFrZQ==.rest", guild_id="guild")
    client._reader = EmptyStub()  # type: ignore[assignment]

    with pytest.raises(AssertionError, match="smoke-probe-"):
        client.wait_for_channel("smoke-probe-", timeout=0.1, poll_interval=0.05)


def test_discord_channel_delegates_to_client() -> None:
    framework = StubFrameworkClient()
    channel = DiscordChannel(client=framework, channel_id="123", name="general")  # type: ignore[arg-type]

    assert channel.send("hello") == "101"
    assert channel.history(limit=5, after="99")[0]["content"] == "ok"
    assert channel.latest_message_id() == "103"
    channel.wait_for_messages(after="1", timeout=5.0)
    channel.wait_for_bot_response(after="2", check="OK")
    channel.send_and_wait("ping", timeout=9.0)

    assert framework.calls == [
        ("send", ("123", "hello"), {}),
        ("history", ("123",), {"limit": 5, "after": "99"}),
        ("latest_message_id", ("123",), {}),
        ("wait_for_messages", ("123",), {"after": "1", "timeout": 5.0, "poll_interval": 2.0, "ignore_system": True, "substring": None, "stable_polls": None}),
        ("wait_for_bot_response", ("123",), {"after": "2", "timeout": 120.0, "poll_interval": 2.0, "sentinel": "awaiting input", "check": "OK"}),
        ("send_and_wait", ("123", "ping"), {"timeout": 9.0, "sentinel": "awaiting input"}),
    ]
