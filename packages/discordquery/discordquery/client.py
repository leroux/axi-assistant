"""Sync and async Discord REST API clients with rate-limit and retry handling.

Both clients wrap httpx and provide:
- Automatic rate-limit retry (429 responses)
- Server error retry with exponential backoff (5xx)
- High-level convenience methods for common operations

Usage::

    # Sync (for CLI tools and scripts)
    with DiscordClient(token) as client:
        guilds = client.list_guilds()
        messages = client.get_messages(channel_id, limit=50)

    # Async (for bots and async applications)
    async with AsyncDiscordClient(token) as client:
        channels = await client.list_channels(guild_id)
        await client.send_message(channel_id, "Hello!")
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
MAX_RETRIES = 3
MAX_RATELIMIT_RETRIES = 10

DiscordRestObserver = Callable[[str, str, int | str, float], None]


def _record_discord_rest_attempt(
    observer: DiscordRestObserver | None,
    method: str,
    path: str,
    started_at: float,
    status: int | str,
) -> None:
    if observer is None:
        return
    observer(method, path, status, time.monotonic() - started_at)


# ---------------------------------------------------------------------------
# Sync client
# ---------------------------------------------------------------------------


class DiscordClient:
    """Synchronous Discord REST client for CLI tools and scripts."""

    def __init__(self, token: str, *, base_url: str = API_BASE, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bot {token}"},
            timeout=httpx.Timeout(timeout),
        )

    def __enter__(self) -> DiscordClient:
        return self

    def __exit__(self, *args: object) -> None:
        self._client.close()

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make a Discord API request with rate-limit and retry handling.

        Rate-limit retries (429) do not count against MAX_RETRIES.
        Raises httpx.HTTPStatusError on non-retriable failures.
        """
        failures = 0
        ratelimit_retries = 0
        while True:
            resp = self._client.request(method, path, **kwargs)

            if resp.status_code in (200, 201, 204):
                return resp

            if resp.status_code == 429:
                ratelimit_retries += 1
                if ratelimit_retries > MAX_RATELIMIT_RETRIES:
                    log.error("Rate limit retries exhausted (%d) on %s %s", MAX_RATELIMIT_RETRIES, method, path)
                    resp.raise_for_status()
                retry_after = float(resp.json().get("retry_after", 1.0))
                log.warning("Rate limited, waiting %.1fs (attempt %d/%d)...", retry_after, ratelimit_retries, MAX_RATELIMIT_RETRIES)
                time.sleep(retry_after)
                continue

            if resp.status_code >= 500 and failures < MAX_RETRIES:
                wait = 2**failures
                failures += 1
                log.warning("Server error %d, retrying in %ds...", resp.status_code, wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET request, returning parsed JSON."""
        return self.request("GET", path, params=params).json()

    def post(self, path: str, **kwargs: Any) -> Any:
        """POST request, returning parsed JSON."""
        return self.request("POST", path, **kwargs).json()

    # -- High-level methods -------------------------------------------------

    def list_guilds(self) -> list[dict[str, Any]]:
        """List guilds (servers) the bot is a member of."""
        return self.get("/users/@me/guilds")

    def list_channels(self, guild_id: str | int) -> list[dict[str, Any]]:
        """List text channels in a guild with category names resolved.

        Returns dicts with keys: id, name, type ("text"/"announcement"), category, position.
        """
        channels: list[dict[str, Any]] = self.get(f"/guilds/{guild_id}/channels")
        categories = {c["id"]: c["name"] for c in channels if c["type"] == 4}
        return [
            {
                "id": str(ch["id"]),
                "name": ch["name"],
                "type": "announcement" if ch["type"] == 5 else "text",
                "category": categories.get(ch.get("parent_id")),
                "position": ch.get("position", 0),
            }
            for ch in sorted(channels, key=lambda c: c.get("position", 0))
            if ch["type"] in (0, 5)
        ]

    def get_messages(
        self,
        channel_id: str | int,
        *,
        limit: int = 50,
        before: str | int | None = None,
        after: str | int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch messages from a channel.

        Discord returns newest-first by default.  When using ``after``,
        messages come oldest-first.
        """
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        return self.get(f"/channels/{channel_id}/messages", params)
    def find_channel(self, guild_id: str | int, name: str) -> dict[str, Any] | None:
        """Find a text channel by name in a guild. Returns None if not found."""
        channels: list[dict[str, Any]] = self.get(f"/guilds/{guild_id}/channels")
        for ch in channels:
            if ch["type"] in (0, 5) and ch["name"].lower() == name.lower():
                return ch
        return None

    def create_channel(
        self,
        guild_id: str | int,
        name: str,
        *,
        channel_type: int = 0,
        parent_id: str | int | None = None,
    ) -> dict[str, Any]:
        """Create a channel in a guild."""
        payload: dict[str, Any] = {"name": name, "type": channel_type}
        if parent_id is not None:
            payload["parent_id"] = str(parent_id)
        return self.post(f"/guilds/{guild_id}/channels", json=payload)

    def get_channel(self, channel_id: str | int) -> dict[str, Any]:
        """Fetch channel metadata."""
        return self.get(f"/channels/{channel_id}")

    def send_message(self, channel_id: str | int, content: str) -> dict[str, Any]:
        """Send a text message to a channel. Returns the message object."""
        return self.post(f"/channels/{channel_id}/messages", json={"content": content})

    def delete_channel(self, channel_id: str | int) -> None:
        """Delete a channel."""
        self.request("DELETE", f"/channels/{channel_id}")


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------


class AsyncDiscordClient:
    """Asynchronous Discord REST client for bots and async applications."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE,
        timeout: float = 15.0,
        on_request_observer: DiscordRestObserver | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bot {token}"},
            timeout=timeout,
        )
        self.on_request_observer = on_request_observer
        # Optional content filter — called on outgoing message text before send/edit.
        # Set to a callable(str) -> str to scrub content (e.g. secret redaction).
        self.content_filter: Callable[[str], str] | None = None
        # Optional audit hook — called with request/response metadata for outbound REST traffic.
        self.audit_hook: Callable[[dict[str, Any]], None] | None = None

    async def __aenter__(self) -> AsyncDiscordClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make a Discord API request with rate-limit and retry handling.

        Rate-limit retries (429) do not count against MAX_RETRIES.
        Raises httpx.HTTPStatusError on non-retriable failures.
        """
        failures = 0
        ratelimit_retries = 0
        audit_base = {
            "method": method,
            "path": path,
            "params": kwargs.get("params"),
            "json": kwargs.get("json"),
            "data": kwargs.get("data"),
            "files": kwargs.get("files"),
        }
        while True:
            started_at = time.monotonic()
            try:
                resp = await self._client.request(method, path, **kwargs)
            except Exception:
                _record_discord_rest_attempt(self.on_request_observer, method, path, started_at, "exception")
                raise

            _record_discord_rest_attempt(self.on_request_observer, method, path, started_at, resp.status_code)

            if resp.status_code in (200, 201, 204):
                if self.audit_hook:
                    response_json = None
                    content_type = resp.headers.get("content-type", "")
                    if content_type.startswith("application/json"):
                        try:
                            response_json = resp.json()
                        except ValueError:
                            response_json = None
                    self.audit_hook(
                        {
                            **audit_base,
                            "outcome": "success",
                            "status_code": resp.status_code,
                            "response_json": response_json,
                            "ratelimit_retries": ratelimit_retries,
                            "server_error_retries": failures,
                        }
                    )
                return resp

            if resp.status_code == 429:
                ratelimit_retries += 1
                if ratelimit_retries > MAX_RATELIMIT_RETRIES:
                    log.error("Rate limit retries exhausted (%d) on %s %s", MAX_RATELIMIT_RETRIES, method, path)
                    if self.audit_hook:
                        self.audit_hook(
                            {
                                **audit_base,
                                "outcome": "error",
                                "status_code": resp.status_code,
                                "error": f"HTTPStatusError: {resp.status_code}",
                                "ratelimit_retries": ratelimit_retries,
                                "server_error_retries": failures,
                            }
                        )
                    resp.raise_for_status()
                retry_after = float(resp.json().get("retry_after", 1.0))
                log.warning("Rate limited on %s %s, waiting %.1fs (attempt %d/%d)...", method, path, retry_after, ratelimit_retries, MAX_RATELIMIT_RETRIES)
                await asyncio.sleep(retry_after)
                continue

            if resp.status_code >= 500 and failures < MAX_RETRIES:
                wait = 2**failures
                failures += 1
                log.warning("Server error %d on %s %s, retrying in %ds...", resp.status_code, method, path, wait)
                await asyncio.sleep(wait)
                continue

            if self.audit_hook:
                response_json = None
                content_type = resp.headers.get("content-type", "")
                if content_type.startswith("application/json"):
                    try:
                        response_json = resp.json()
                    except ValueError:
                        response_json = None
                self.audit_hook(
                    {
                        **audit_base,
                        "outcome": "error",
                        "status_code": resp.status_code,
                        "response_json": response_json,
                        "error": f"HTTPStatusError: {resp.status_code}",
                        "ratelimit_retries": ratelimit_retries,
                        "server_error_retries": failures,
                    }
                )
            resp.raise_for_status()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET request, returning parsed JSON."""
        resp = await self.request("GET", path, params=params)
        return resp.json()

    async def post(self, path: str, **kwargs: Any) -> Any:
        """POST request, returning parsed JSON."""
        resp = await self.request("POST", path, **kwargs)
        return resp.json()

    # -- High-level methods: Guilds -----------------------------------------

    async def list_guilds(self) -> list[dict[str, Any]]:
        """List guilds (servers) the bot is a member of."""
        return await self.get("/users/@me/guilds")

    async def list_channels(self, guild_id: str | int) -> list[dict[str, Any]]:
        """List text channels in a guild with category names resolved.

        Returns dicts with keys: id, name, type, category, position.
        """
        channels: list[dict[str, Any]] = await self.get(f"/guilds/{guild_id}/channels")
        categories = {c["id"]: c["name"] for c in channels if c["type"] == 4}
        return [
            {
                "id": str(ch["id"]),
                "name": ch["name"],
                "type": "announcement" if ch["type"] == 5 else "text",
                "category": categories.get(ch.get("parent_id")),
                "position": ch.get("position", 0),
            }
            for ch in sorted(channels, key=lambda c: c.get("position", 0))
            if ch["type"] in (0, 5)
        ]

    async def find_channel(self, guild_id: str | int, name: str) -> dict[str, Any] | None:
        """Find a text channel by name in a guild. Returns None if not found."""
        channels: list[dict[str, Any]] = await self.get(f"/guilds/{guild_id}/channels")
        for ch in channels:
            if ch["type"] in (0, 5) and ch["name"].lower() == name.lower():
                return ch
        return None

    async def create_channel(self, guild_id: str | int, name: str, *, channel_type: int = 0) -> dict[str, Any]:
        """Create a channel in a guild."""
        return await self.post(f"/guilds/{guild_id}/channels", json={"name": name, "type": channel_type})

    # -- High-level methods: Messages ---------------------------------------

    async def get_messages(
        self,
        channel_id: str | int,
        *,
        limit: int = 50,
        before: str | int | None = None,
        after: str | int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch messages from a channel.

        Discord returns newest-first by default.  When using ``after``,
        messages come oldest-first.
        """
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        return await self.get(f"/channels/{channel_id}/messages", params)

    async def send_message(self, channel_id: str | int, content: str) -> dict[str, Any]:
        """Send a text message to a channel. Returns the message object."""
        if self.content_filter:
            content = self.content_filter(content)
        return await self.post(f"/channels/{channel_id}/messages", json={"content": content})

    async def send_file(
        self,
        channel_id: str | int,
        filename: str,
        file_data: bytes,
        content: str | None = None,
    ) -> dict[str, Any]:
        """Send a file attachment to a channel. Returns the message object."""
        data: dict[str, str] = {}
        if content:
            if self.content_filter:
                content = self.content_filter(content)
            data["content"] = content
        files = {"files[0]": (filename, file_data)}
        resp = await self.request(
            "POST",
            f"/channels/{channel_id}/messages",
            data=data,
            files=files,
        )
        return resp.json()

    async def edit_message(self, channel_id: str | int, message_id: str | int, content: str) -> dict[str, Any]:
        """Edit an existing message. Returns the updated message object."""
        if self.content_filter:
            content = self.content_filter(content)
        resp = await self.request(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            json={"content": content},
        )
        return resp.json()

    async def delete_message(self, channel_id: str | int, message_id: str | int) -> None:
        """Delete a message."""
        await self.request("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    # -- High-level methods: Reactions --------------------------------------

    async def add_reaction(self, channel_id: str | int, message_id: str | int, emoji: str) -> None:
        """Add a reaction to a message."""
        encoded = urllib.parse.quote(emoji)
        await self.request(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
        )

    async def remove_reaction(self, channel_id: str | int, message_id: str | int, emoji: str) -> None:
        """Remove the bot's own reaction from a message."""
        encoded = urllib.parse.quote(emoji)
        await self.request(
            "DELETE",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
        )
