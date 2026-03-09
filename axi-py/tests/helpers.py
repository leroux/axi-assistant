"""Discord API helpers for smoke tests."""

import time

import httpx

API_BASE = "https://discord.com/api/v10"


class Discord:
    """Thin wrapper around Discord REST API for test interactions."""

    def __init__(self, bot_token: str, sender_token: str, guild_id: str):
        self.bot_token = bot_token
        self.sender_token = sender_token
        self.guild_id = guild_id
        # Sender client — simulates a user sending messages
        self._sender = httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bot {sender_token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        )
        # Bot client — reads messages as the bot itself
        self._bot = httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(15.0),
        )

    def close(self):
        self._sender.close()
        self._bot.close()

    # --- Channel discovery ---

    def list_channels(self) -> list[dict]:
        """List all text channels in the guild."""
        resp = self._bot.get(f"/guilds/{self.guild_id}/channels")
        resp.raise_for_status()
        channels = resp.json()
        return [
            {"id": c["id"], "name": c["name"], "category_id": c.get("parent_id")}
            for c in channels
            if c["type"] == 0  # text channels only
        ]

    def find_channel(self, name: str) -> str | None:
        """Find a channel by name, return its ID or None."""
        for ch in self.list_channels():
            if ch["name"] == name:
                return ch["id"]
        return None

    # --- Sending ---

    def send(self, channel_id: str, content: str) -> str:
        """Send a message as the sender bot. Returns the message ID."""
        resp = self._sender.post(
            f"/channels/{channel_id}/messages",
            json={"content": content},
        )
        resp.raise_for_status()
        return resp.json()["id"]

    # --- Reading ---

    def history(
        self, channel_id: str, limit: int = 50, after: str | None = None
    ) -> list[dict]:
        """Fetch recent messages from a channel (newest first unless after is set)."""
        params: dict = {"limit": limit}
        if after:
            params["after"] = after
        resp = self._bot.get(f"/channels/{channel_id}/messages", params=params)
        resp.raise_for_status()
        return resp.json()

    def latest_message_id(self, channel_id: str) -> str | None:
        """Get the ID of the most recent message in a channel."""
        msgs = self.history(channel_id, limit=1)
        return msgs[0]["id"] if msgs else None

    # --- Waiting ---

    def wait_for_bot(
        self,
        channel_id: str,
        after: str,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
        sentinel: bool = True,
    ) -> list[dict]:
        """Wait for the bot to respond after a given message ID.

        If sentinel=True (default), waits for the "awaiting input" sentinel.
        If sentinel=False, uses stability-based detection (no new messages for
        `poll_interval * 2` seconds).

        Returns all bot messages after `after`, in chronological order.
        """
        deadline = time.monotonic() + timeout
        sender_user_id = self._get_sender_user_id()
        last_seen_id = after
        collected: list[dict] = []
        stable_since: float | None = None

        while time.monotonic() < deadline:
            msgs = self.history(channel_id, limit=100, after=after)
            if msgs:
                # Discord returns newest-first with no `after`, but oldest-first with `after`
                # Actually, Discord always returns newest-first. We reverse for chronological.
                msgs_chrono = list(reversed(msgs))
                new_bot_msgs = []

                for m in msgs_chrono:
                    mid = m["id"]
                    author_id = m["author"]["id"]

                    # Skip sender's own messages
                    if author_id == sender_user_id:
                        continue

                    # Check if this message is new
                    if int(mid) > int(last_seen_id):
                        new_bot_msgs.append(m)
                        last_seen_id = mid

                if new_bot_msgs:
                    collected.extend(new_bot_msgs)
                    stable_since = None  # Reset stability timer

                    if sentinel:
                        # Check if any message contains the sentinel
                        for m in new_bot_msgs:
                            content = m.get("content", "")
                            if "awaiting input" in content:
                                # Filter out the sentinel message itself
                                return [
                                    m
                                    for m in collected
                                    if "awaiting input" not in m.get("content", "")
                                ]
                else:
                    # No new messages — track stability
                    if stable_since is None:
                        stable_since = time.monotonic()
                    elif not sentinel and (time.monotonic() - stable_since) > (poll_interval * 3):
                        return collected
            else:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif not sentinel and (time.monotonic() - stable_since) > (poll_interval * 3):
                    return collected

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))

        # Timed out — return whatever we collected
        return collected

    def send_and_wait(
        self,
        channel_id: str,
        content: str,
        timeout: float = 120.0,
        sentinel: bool = True,
    ) -> list[dict]:
        """Send a message and wait for the bot's response.

        Returns the bot's response messages in chronological order.
        """
        msg_id = self.send(channel_id, content)
        return self.wait_for_bot(
            channel_id, after=msg_id, timeout=timeout, sentinel=sentinel
        )

    # --- Internal ---

    def _get_sender_user_id(self) -> str:
        """Extract user ID from the sender token (base64 decode first segment)."""
        import base64

        token_part = self.sender_token.split(".")[0]
        # Add padding if needed
        padded = token_part + "=" * (4 - len(token_part) % 4)
        return base64.b64decode(padded).decode("utf-8")

    # --- Convenience ---

    def bot_response_text(self, messages: list[dict]) -> str:
        """Join all bot response messages into a single string."""
        return "\n".join(m.get("content", "") for m in messages)

    def create_channel(self, name: str, parent_id: str | None = None) -> str:
        """Create a text channel in the guild. Returns channel ID."""
        payload: dict = {"name": name, "type": 0}
        if parent_id:
            payload["parent_id"] = parent_id
        resp = self._bot.post(
            f"/guilds/{self.guild_id}/channels",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def delete_channel(self, channel_id: str):
        """Delete a channel."""
        resp = self._bot.delete(f"/channels/{channel_id}")
        resp.raise_for_status()

    def find_category(self, name: str) -> str | None:
        """Find a category channel by name, return its ID or None."""
        resp = self._bot.get(f"/guilds/{self.guild_id}/channels")
        resp.raise_for_status()
        for c in resp.json():
            if c["type"] == 4 and c["name"] == name:  # category type
                return c["id"]
        return None

    def channel_info(self, channel_id: str) -> dict:
        """Get full channel info including parent_id."""
        resp = self._bot.get(f"/channels/{channel_id}")
        resp.raise_for_status()
        return resp.json()

    def has_attachment(self, messages: list[dict]) -> bool:
        """Check if any message in the list has an attachment."""
        return any(len(m.get("attachments", [])) > 0 for m in messages)

    def poll_history(
        self,
        channel_id: str,
        after: str,
        check: str,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
    ) -> str:
        """Poll channel history until `check` substring appears. Returns full text."""
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            msgs = self.history(channel_id, limit=30, after=after)
            text = "\n".join(m.get("content", "") for m in msgs)
            if check.lower() in text.lower():
                return text
            time.sleep(poll_interval)
        return text
