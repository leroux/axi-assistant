from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any

from discordquery import DiscordClient
from discordquery.wait import DEFAULT_STABLE_POLLS, wait_for_messages


@dataclass(slots=True)
class WaitResult:
    messages: list[dict[str, Any]]
    cursor: str

    @property
    def text(self) -> str:
        return "\n".join(message.get("content", "") for message in self.messages)


@dataclass(slots=True)
class DiscordChannel:
    client: DiscordE2EClient
    channel_id: str
    name: str | None = None

    def send(self, content: str) -> str:
        return self.client.send(self.channel_id, content)

    def history(self, limit: int = 50, after: str | None = None) -> list[dict[str, Any]]:
        return self.client.history(self.channel_id, limit=limit, after=after)

    def latest_message_id(self) -> str | None:
        return self.client.latest_message_id(self.channel_id)

    def wait_for_messages(
        self,
        *,
        after: str,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
        ignore_system: bool = True,
        substring: str | None = None,
        stable_polls: int | None = None,
    ) -> WaitResult:
        return self.client.wait_for_messages(
            self.channel_id,
            after=after,
            timeout=timeout,
            poll_interval=poll_interval,
            ignore_system=ignore_system,
            substring=substring,
            stable_polls=stable_polls,
        )

    def wait_for_bot_response(
        self,
        *,
        after: str,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
        sentinel: str | None = "awaiting input",
        check: str | None = None,
    ) -> WaitResult:
        return self.client.wait_for_bot_response(
            self.channel_id,
            after=after,
            timeout=timeout,
            poll_interval=poll_interval,
            sentinel=sentinel,
            check=check,
        )

    def send_and_wait(
        self,
        content: str,
        *,
        timeout: float = 120.0,
        sentinel: str | None = "awaiting input",
    ) -> WaitResult:
        return self.client.send_and_wait(self.channel_id, content, timeout=timeout, sentinel=sentinel)


class DiscordE2EClient:
    def __init__(self, *, reader_token: str, sender_token: str, guild_id: str) -> None:
        self.guild_id = guild_id
        self.reader_token = reader_token
        self.sender_token = sender_token
        self._reader = DiscordClient(reader_token, timeout=15.0)
        self._sender_client = DiscordClient(sender_token, timeout=15.0)

    def close(self) -> None:
        self._reader.close()
        self._sender_client.close()

    def list_channels(self) -> list[dict[str, Any]]:
        return self._reader.list_channels(self.guild_id)

    def channel(self, channel_id: str, *, name: str | None = None) -> DiscordChannel:
        return DiscordChannel(client=self, channel_id=channel_id, name=name)

    def find_channel(self, name: str) -> str | None:
        channel = self._reader.find_channel(self.guild_id, name)
        return str(channel["id"]) if channel else None

    def require_channel(self, name: str) -> DiscordChannel:
        channel_id = self.find_channel(name)
        if channel_id is None:
            raise AssertionError(f"Channel '{name}' not found")
        return self.channel(channel_id, name=name)

    def find_channel_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        for channel in self._reader.list_channels(self.guild_id):
            if channel.get("name", "").startswith(prefix):
                return channel
        return None

    def wait_for_channel(
        self,
        prefix: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 2.0,
    ) -> DiscordChannel:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            channel = self.find_channel_by_prefix(prefix)
            if channel is not None:
                return self.channel(str(channel["id"]), name=channel.get("name"))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
        raise AssertionError(f"No channel with prefix '{prefix}' appeared within {timeout}s")

    def find_category(self, name: str) -> str | None:
        channels = self._reader.get(f"/guilds/{self.guild_id}/channels")
        for channel in channels:
            if channel["type"] == 4 and channel["name"].lower() == name.lower():
                return str(channel["id"])
        return None

    def create_channel(self, name: str, parent_id: str | None = None) -> str:
        channel = self._reader.create_channel(self.guild_id, name, parent_id=parent_id)
        return str(channel["id"])

    def delete_channel(self, channel_id: str) -> None:
        self._reader.delete_channel(channel_id)

    def channel_info(self, channel_id: str) -> dict[str, Any]:
        return self._reader.get_channel(channel_id)

    def history(self, channel_id: str, limit: int = 50, after: str | None = None) -> list[dict[str, Any]]:
        return self._reader.get_messages(channel_id, limit=limit, after=after)

    def latest_message_id(self, channel_id: str) -> str | None:
        messages = self.history(channel_id, limit=1)
        return str(messages[0]["id"]) if messages else None

    def send(self, channel_id: str, content: str) -> str:
        message = self._sender_client.send_message(channel_id, content)
        return str(message["id"])

    def wait_for_messages(
        self,
        channel_id: str,
        *,
        after: str,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
        ignore_system: bool = True,
        substring: str | None = None,
        stable_polls: int | None = None,
    ) -> WaitResult:
        messages, cursor = wait_for_messages(
            self._reader,
            channel_id,
            after,
            timeout,
            ignore_author_ids={self._sender_user_id()},
            ignore_system=ignore_system,
            poll_interval=poll_interval,
            substring=substring,
            stable_polls=DEFAULT_STABLE_POLLS if stable_polls is None else stable_polls,
        )
        return WaitResult(messages=messages, cursor=cursor)

    def wait_for_bot_response(
        self,
        channel_id: str,
        *,
        after: str,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
        sentinel: str | None = "awaiting input",
        check: str | None = None,
        ignore_system: bool | None = None,
    ) -> WaitResult:
        # When no sentinel is provided, the caller wants the raw bot reply —
        # system-prefixed messages ARE the reply in that mode (e.g. "/status",
        # killed-agent notices), so don't drop them by default.
        effective_ignore_system = (sentinel is not None) if ignore_system is None else ignore_system
        if sentinel is None:
            return self.wait_for_messages(
                channel_id,
                after=after,
                timeout=timeout,
                poll_interval=poll_interval,
                substring=check,
                stable_polls=DEFAULT_STABLE_POLLS if check is None else 0,
                ignore_system=effective_ignore_system,
            )

        deadline = time.monotonic() + timeout
        last_seen_id = after
        collected: list[dict[str, Any]] = []
        sender_user_id = self._sender_user_id()

        while time.monotonic() < deadline:
            messages = self.history(channel_id, limit=100, after=after)
            if messages:
                for message in reversed(messages):
                    message_id = str(message["id"])
                    author_id = str(message["author"]["id"])
                    if author_id == sender_user_id or int(message_id) <= int(last_seen_id):
                        continue
                    collected.append(message)
                    last_seen_id = message_id
                    if sentinel in message.get("content", ""):
                        filtered = [
                            entry for entry in collected if sentinel not in entry.get("content", "")
                        ]
                        return WaitResult(messages=filtered, cursor=last_seen_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))

        return WaitResult(messages=collected, cursor=last_seen_id)

    def send_and_wait(
        self,
        channel_id: str,
        content: str,
        *,
        timeout: float = 120.0,
        sentinel: str | None = "awaiting input",
    ) -> WaitResult:
        message_id = self.send(channel_id, content)
        return self.wait_for_bot_response(channel_id, after=message_id, timeout=timeout, sentinel=sentinel)

    def has_attachment(self, messages: list[dict[str, Any]]) -> bool:
        return any(bool(message.get("attachments")) for message in messages)

    def _sender_user_id(self) -> str:
        token_part = self.sender_token.split(".", 1)[0]
        padded = token_part + "=" * (-len(token_part) % 4)
        return base64.b64decode(padded).decode("utf-8")
