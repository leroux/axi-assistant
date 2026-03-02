"""discordquery — lightweight Discord REST client and CLI tools.

Provides sync and async httpx-based Discord API clients with rate-limit
retry handling, plus CLI tools for querying message history and waiting
for new messages. No discord.py dependency — pure REST.

Client usage::

    from discordquery import DiscordClient, AsyncDiscordClient

    # Sync
    with DiscordClient(token) as client:
        guilds = client.list_guilds()

    # Async
    async with AsyncDiscordClient(token) as client:
        await client.send_message(channel_id, "Hello!")

CLI usage::

    python -m discordquery query guilds
    python -m discordquery wait <channel_id> --timeout 30
"""

from discordquery.client import AsyncDiscordClient, DiscordClient
from discordquery.helpers import (
    DISCORD_EPOCH_MS,
    datetime_to_snowflake,
    format_message,
    resolve_snowflake,
    split_message,
)

__all__ = [
    "DISCORD_EPOCH_MS",
    "AsyncDiscordClient",
    "DiscordClient",
    "datetime_to_snowflake",
    "format_message",
    "resolve_snowflake",
    "split_message",
]
