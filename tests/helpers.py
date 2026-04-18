"""Discord API helpers for smoke tests."""

from discord_e2e import DiscordChannel, DiscordE2EClient


class Discord(DiscordE2EClient):
    """Axi-specific test adapter built on the generic Discord E2E client."""

    def __init__(self, bot_token: str, sender_token: str, guild_id: str):
        super().__init__(reader_token=bot_token, sender_token=sender_token, guild_id=guild_id)
        self._bot = self._reader
        self._sender = self._sender_client

    def require_channel(self, name: str) -> DiscordChannel:
        return super().require_channel(name)

    def wait_for_bot(
        self,
        channel_id: str,
        after: str,
        timeout: float = 120.0,
        poll_interval: float = 2.0,
        sentinel: bool = True,
        check: str | None = None,
    ) -> list[dict]:
        result = self.wait_for_bot_response(
            channel_id,
            after=after,
            timeout=timeout,
            poll_interval=poll_interval,
            sentinel="awaiting input" if sentinel else None,
            check=check,
        )
        return result.messages

    def send_and_wait(
        self,
        channel_id: str,
        content: str,
        timeout: float = 120.0,
        sentinel: bool = True,
    ) -> list[dict]:
        result = super().send_and_wait(
            channel_id,
            content,
            timeout=timeout,
            sentinel="awaiting input" if sentinel else None,
        )
        return result.messages

    def bot_response_text(self, messages: list[dict]) -> str:
        return "\n".join(message.get("content", "") for message in messages)

    def poll_history(
        self,
        channel_id: str,
        after: str,
        check: str,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
    ) -> str:
        result = self.wait_for_bot_response(
            channel_id,
            after=after,
            timeout=timeout,
            poll_interval=poll_interval,
            sentinel=None,
            check=check,
        )
        return result.text
