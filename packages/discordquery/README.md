# discordquery

Lightweight Discord REST API client and CLI query tools. No discord.py — pure httpx.

## Purpose

Provides sync and async Discord API clients with automatic rate-limit retry and exponential backoff for server errors. Includes CLI tools for querying message history and polling for new messages.

## Usage

### Library

```python
from discordquery import DiscordClient, AsyncDiscordClient

# Sync (for scripts/CLI)
with DiscordClient(token) as client:
    guilds = client.list_guilds()
    messages = client.get_messages(channel_id, limit=50)

# Async (for bots)
async with AsyncDiscordClient(token) as client:
    await client.send_message(channel_id, "Hello!")
    await client.send_file(channel_id, "log.txt", data, content="Here's the log")
```

### CLI

```bash
# List guilds
python -m discordquery query guilds

# List channels
python -m discordquery query channels <guild_id>

# Fetch message history
python -m discordquery query history <channel_id> --limit 100 --format text

# Search messages
python -m discordquery query search <guild_id> "keyword" --author alice --limit 20

# Wait for new messages (polling)
python -m discordquery wait <channel_id> --timeout 60
```

The `wait` command outputs a cursor for chaining:

```bash
cursor=$(python -m discordquery wait 123 --after $last_cursor | tail -1 | jq -r .cursor)
```

## API

### Clients

| Export | Description |
|---|---|
| `DiscordClient` | Sync httpx client (context manager) |
| `AsyncDiscordClient` | Async httpx client (context manager) |

Both support: `list_guilds()`, `list_channels()`, `get_messages()`, `send_message()`, `send_file()`, `edit_message()`, `add_reaction()`, `remove_reaction()`. The async client also has `find_channel()` and `create_channel()`.

Retry logic: 429 rate limits (wait `retry_after`), 5xx errors (exponential backoff, 3 retries).

### Helpers

| Export | Description |
|---|---|
| `split_message(text, limit=2000)` | Split text for Discord's character limit |
| `format_message(msg, fmt)` | Format message as `"jsonl"` or `"text"` |
| `datetime_to_snowflake(dt)` | Convert datetime to Discord snowflake ID |
| `resolve_snowflake(value)` | Parse snowflake ID or ISO datetime string |
| `DISCORD_EPOCH_MS` | Discord epoch constant (1420070400000) |

## Dependencies

- `httpx`

Requires Python 3.12+.
