#!/usr/bin/env python3
"""Query Discord server message history via the REST API.

Usage:
    discord_query.py guilds
    discord_query.py channels <guild_id>
    discord_query.py history <channel> [options]
    discord_query.py search <guild_id> <query> [options]
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime

import httpx
from dotenv import load_dotenv

# Load .env from cwd if present (supports both main and test instances)
load_dotenv()

API_BASE = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
MAX_PER_REQUEST = 100
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
DEFAULT_MAX_SCAN = 500
MAX_RETRIES = 3


def get_token() -> str:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        # Fall back to sender_token from test config
        config_path = os.path.expanduser("~/.config/axi/test-config.json")
        try:
            with open(config_path) as f:
                config = json.load(f)
            token = config.get("defaults", {}).get("sender_token")
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    if not token:
        print("Error: DISCORD_TOKEN not set and no sender_token in test config.", file=sys.stderr)
        sys.exit(1)
    return token


def get_bot_guild_ids(client: httpx.Client) -> set[int]:
    """Fetch the list of guilds the bot is a member of."""
    guilds = api_get(client, "/users/@me/guilds")
    return {int(g["id"]) for g in guilds}


def validate_guild(client: httpx.Client, guild_id: int) -> None:
    """Validate that the bot is a member of the given guild."""
    bot_guilds = get_bot_guild_ids(client)
    if guild_id not in bot_guilds:
        print(
            f"Error: Bot is not a member of guild {guild_id}.",
            file=sys.stderr,
        )
        sys.exit(1)


# --- HTTP client ---


def make_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bot {token}"},
        timeout=httpx.Timeout(30.0),
    )


def api_get(client: httpx.Client, path: str, params: dict | None = None) -> list | dict:
    """GET request with rate-limit and retry handling."""
    for attempt in range(MAX_RETRIES + 1):
        resp = client.get(path, params=params)

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429:
            retry_after = float(resp.json().get("retry_after", 1.0))
            print(f"Rate limited, waiting {retry_after:.1f}s...", file=sys.stderr)
            time.sleep(retry_after)
            continue

        if resp.status_code >= 500:
            if attempt < MAX_RETRIES:
                wait = 2**attempt
                print(f"Server error {resp.status_code}, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue

        # 4xx or exhausted retries
        print(
            f"Error: Discord API returned {resp.status_code}: {resp.text}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Should not reach here, but just in case
    print("Error: Exhausted retries.", file=sys.stderr)
    sys.exit(1)


# --- Helpers ---


def datetime_to_snowflake(dt: datetime) -> int:
    """Convert a datetime to a Discord snowflake ID."""
    ms = int(dt.timestamp() * 1000)
    return (ms - DISCORD_EPOCH_MS) << 22


def resolve_snowflake(value: str) -> int:
    """Parse a value as either a snowflake ID or ISO datetime, returning a snowflake."""
    if value.isdigit():
        return int(value)
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return datetime_to_snowflake(dt)
    except ValueError:
        print(f"Error: Cannot parse '{value}' as a snowflake ID or ISO datetime.", file=sys.stderr)
        sys.exit(1)


def resolve_channel(client: httpx.Client, channel_arg: str) -> str:
    """Resolve a channel argument to a channel ID.

    Accepts either a raw channel ID or guild_id:channel_name syntax.
    """
    if channel_arg.isdigit():
        return channel_arg

    if ":" not in channel_arg:
        print(
            f"Error: '{channel_arg}' is not a valid channel ID or guild_id:channel_name pair.",
            file=sys.stderr,
        )
        sys.exit(1)

    guild_id_str, channel_name = channel_arg.split(":", 1)
    if not guild_id_str.isdigit():
        print(f"Error: '{guild_id_str}' is not a valid guild ID.", file=sys.stderr)
        sys.exit(1)

    guild_id = int(guild_id_str)
    validate_guild(client, guild_id)

    channels = api_get(client, f"/guilds/{guild_id}/channels")
    text_channels = [
        c
        for c in channels
        if c["type"] in (0, 5)  # text and announcement
        and c["name"].lower() == channel_name.lower()
    ]

    if not text_channels:
        available = sorted([c["name"] for c in channels if c["type"] in (0, 5)])
        print(
            f"Error: No text channel named '{channel_name}' in guild {guild_id}.",
            file=sys.stderr,
        )
        if available:
            print(f"Available channels: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)

    if len(text_channels) > 1:
        print(
            f"Warning: Multiple channels named '{channel_name}', using first match.",
            file=sys.stderr,
        )

    return str(text_channels[0]["id"])


def format_message(msg: dict, fmt: str) -> str:
    """Format a Discord API message object for output."""
    ts = msg.get("timestamp", "")
    author = msg.get("author", {})
    author_name = author.get("username", "unknown")
    content = msg.get("content", "")
    attachments = len(msg.get("attachments", []))
    embeds = len(msg.get("embeds", []))

    if fmt == "text":
        # Parse and reformat timestamp
        try:
            dt = datetime.fromisoformat(ts)
            ts_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, TypeError):
            ts_str = ts
        line = f"[{ts_str}] {author_name}: {content}"
        extras = []
        if attachments:
            extras.append(f"{attachments} attachment(s)")
        if embeds:
            extras.append(f"{embeds} embed(s)")
        if extras:
            line += f"  [{', '.join(extras)}]"
        return line

    # JSONL
    return json.dumps(
        {
            "id": msg.get("id"),
            "ts": ts,
            "author": author_name,
            "author_id": author.get("id"),
            "content": content,
            "attachments": attachments,
            "embeds": embeds,
        }
    )


# --- Subcommands ---


def cmd_guilds(args, client: httpx.Client) -> None:
    """List guilds (servers) the bot is a member of."""
    guilds = api_get(client, "/users/@me/guilds")
    for g in guilds:
        entry = {
            "id": str(g["id"]),
            "name": g["name"],
        }
        print(json.dumps(entry))


def cmd_channels(args, client: httpx.Client) -> None:
    """List text channels in a guild."""
    guild_id = int(args.guild_id)
    validate_guild(client, guild_id)

    channels = api_get(client, f"/guilds/{guild_id}/channels")

    # Build category name map
    categories = {c["id"]: c["name"] for c in channels if c["type"] == 4}

    # Filter to text-like channels and sort by position
    text_channels = sorted(
        [c for c in channels if c["type"] in (0, 5)],
        key=lambda c: c.get("position", 0),
    )

    for ch in text_channels:
        ch_type = "announcement" if ch["type"] == 5 else "text"
        category = categories.get(ch.get("parent_id"))
        entry = {
            "id": str(ch["id"]),
            "name": ch["name"],
            "type": ch_type,
            "category": category,
            "position": ch.get("position", 0),
        }
        print(json.dumps(entry))


def cmd_history(args, client: httpx.Client) -> None:
    """Fetch message history from a channel."""
    channel_id = resolve_channel(client, args.channel)
    limit = min(args.limit, MAX_LIMIT)
    fmt = args.format

    collected = 0
    params: dict = {}

    if args.before:
        params["before"] = resolve_snowflake(args.before)
    if args.after:
        params["after"] = resolve_snowflake(args.after)

    # When using 'after', Discord returns oldest-first, which is natural
    # for "what happened after X?" queries. No reversal needed.
    use_after = "after" in params

    messages = []

    while collected < limit:
        batch_size = min(MAX_PER_REQUEST, limit - collected)
        params["limit"] = batch_size

        batch = api_get(client, f"/channels/{channel_id}/messages", params)

        if not batch:
            break

        messages.extend(batch)
        collected += len(batch)

        if len(batch) < batch_size:
            break  # No more messages

        # Set cursor for next page
        if use_after:
            # Discord returns oldest-first with 'after', so next cursor is the last (newest) ID
            params["after"] = batch[-1]["id"]
        else:
            # Default newest-first, so next cursor is the last (oldest) ID
            params["before"] = batch[-1]["id"]

    for msg in messages:
        print(format_message(msg, fmt))


def cmd_search(args, client: httpx.Client) -> None:
    """Search messages by content substring."""
    guild_id = int(args.guild_id)
    validate_guild(client, guild_id)

    query = args.query.lower()
    limit = min(args.limit, MAX_LIMIT)
    max_scan = args.max_scan
    fmt = args.format
    author_filter = args.author.lower() if args.author else None

    # Determine which channels to scan
    if args.channel:
        channel_ids = [resolve_channel(client, args.channel)]
    else:
        channels = api_get(client, f"/guilds/{guild_id}/channels")
        channel_ids = [
            str(c["id"]) for c in sorted(channels, key=lambda c: c.get("position", 0)) if c["type"] in (0, 5)
        ]

    found = 0

    for channel_id in channel_ids:
        if found >= limit:
            break

        scanned = 0
        params: dict = {}

        while scanned < max_scan and found < limit:
            batch_size = min(MAX_PER_REQUEST, max_scan - scanned)
            params["limit"] = batch_size

            try:
                batch = api_get(client, f"/channels/{channel_id}/messages", params)
            except SystemExit:
                # Permission error on this channel, skip it
                break

            if not batch:
                break

            for msg in batch:
                content = msg.get("content", "").lower()
                author_name = msg.get("author", {}).get("username", "").lower()

                if query in content:
                    if author_filter and author_filter not in author_name:
                        continue
                    print(format_message(msg, fmt))
                    found += 1
                    if found >= limit:
                        break

            scanned += len(batch)

            if len(batch) < batch_size:
                break

            params["before"] = batch[-1]["id"]

    if found == 0:
        print("No messages found.", file=sys.stderr)


# --- Main ---


def main():
    parser = argparse.ArgumentParser(
        description="Query Discord server message history via the REST API.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # guilds
    subparsers.add_parser("guilds", help="List guilds (servers) the bot is a member of.")

    # channels
    p_channels = subparsers.add_parser("channels", help="List text channels in a guild.")
    p_channels.add_argument("guild_id", help="Discord guild (server) ID.")

    # history
    p_history = subparsers.add_parser("history", help="Fetch message history from a channel.")
    p_history.add_argument(
        "channel",
        help="Channel ID, or guild_id:channel_name (e.g. 123456789:general).",
    )
    p_history.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Number of messages to fetch (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
    )
    p_history.add_argument(
        "--before",
        help="Fetch messages before this point (ISO datetime or snowflake ID).",
    )
    p_history.add_argument(
        "--after",
        help="Fetch messages after this point (ISO datetime or snowflake ID).",
    )
    p_history.add_argument(
        "--format",
        choices=["jsonl", "text"],
        default="jsonl",
        help="Output format (default: jsonl).",
    )

    # search
    p_search = subparsers.add_parser("search", help="Search messages by content substring.")
    p_search.add_argument("guild_id", help="Discord guild (server) ID.")
    p_search.add_argument("query", help="Search term (case-insensitive substring match).")
    p_search.add_argument(
        "--channel",
        help="Limit search to this channel (ID or guild_id:channel_name).",
    )
    p_search.add_argument(
        "--author",
        help="Filter by author username (case-insensitive substring match).",
    )
    p_search.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max results to return (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
    )
    p_search.add_argument(
        "--max-scan",
        type=int,
        default=DEFAULT_MAX_SCAN,
        help=f"Max messages to scan per channel (default {DEFAULT_MAX_SCAN}).",
    )
    p_search.add_argument(
        "--format",
        choices=["jsonl", "text"],
        default="jsonl",
        help="Output format (default: jsonl).",
    )

    args = parser.parse_args()
    token = get_token()

    with make_client(token) as client:
        if args.command == "guilds":
            cmd_guilds(args, client)
        elif args.command == "channels":
            cmd_channels(args, client)
        elif args.command == "history":
            cmd_history(args, client)
        elif args.command == "search":
            cmd_search(args, client)


if __name__ == "__main__":
    main()
