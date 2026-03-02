"""Query Discord server message history via the REST API.

Usage:
    python -m discordquery query guilds
    python -m discordquery query channels <guild_id>
    python -m discordquery query history <channel> [options]
    python -m discordquery query search <guild_id> <query> [options]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from discordquery.client import DiscordClient
from discordquery.helpers import format_message, resolve_snowflake

MAX_PER_REQUEST = 100
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
DEFAULT_MAX_SCAN = 500


def get_bot_guild_ids(client: DiscordClient) -> set[int]:
    """Fetch the list of guilds the bot is a member of."""
    guilds = client.list_guilds()
    return {int(g["id"]) for g in guilds}


def validate_guild(client: DiscordClient, guild_id: int) -> None:
    """Validate that the bot is a member of the given guild."""
    bot_guilds = get_bot_guild_ids(client)
    if guild_id not in bot_guilds:
        print(f"Error: Bot is not a member of guild {guild_id}.", file=sys.stderr)
        sys.exit(1)


def resolve_channel(client: DiscordClient, channel_arg: str) -> str:
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

    channels = client.get(f"/guilds/{guild_id}/channels")
    text_channels = [c for c in channels if c["type"] in (0, 5) and c["name"].lower() == channel_name.lower()]

    if not text_channels:
        available = sorted([c["name"] for c in channels if c["type"] in (0, 5)])
        print(f"Error: No text channel named '{channel_name}' in guild {guild_id}.", file=sys.stderr)
        if available:
            print(f"Available channels: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)

    if len(text_channels) > 1:
        print(f"Warning: Multiple channels named '{channel_name}', using first match.", file=sys.stderr)

    return str(text_channels[0]["id"])


# --- Subcommands ---


def cmd_guilds(args: argparse.Namespace, client: DiscordClient) -> None:
    """List guilds (servers) the bot is a member of."""
    guilds = client.list_guilds()
    for g in guilds:
        print(json.dumps({"id": str(g["id"]), "name": g["name"]}))


def cmd_channels(args: argparse.Namespace, client: DiscordClient) -> None:
    """List text channels in a guild."""
    guild_id = int(args.guild_id)
    validate_guild(client, guild_id)
    channels = client.list_channels(guild_id)
    for ch in channels:
        print(json.dumps(ch))


def cmd_history(args: argparse.Namespace, client: DiscordClient) -> None:
    """Fetch message history from a channel."""
    channel_id = resolve_channel(client, args.channel)
    limit = min(args.limit, MAX_LIMIT)
    fmt = args.format

    collected = 0
    params: dict[str, Any] = {}

    if args.before:
        params["before"] = resolve_snowflake(args.before)
    if args.after:
        params["after"] = resolve_snowflake(args.after)

    use_after = "after" in params
    messages: list[dict[str, Any]] = []

    while collected < limit:
        batch_size = min(MAX_PER_REQUEST, limit - collected)
        params["limit"] = batch_size

        batch = client.get(f"/channels/{channel_id}/messages", params)

        if not batch:
            break

        messages.extend(batch)
        collected += len(batch)

        if len(batch) < batch_size:
            break

        if use_after:
            params["after"] = batch[-1]["id"]
        else:
            params["before"] = batch[-1]["id"]

    for msg in messages:
        print(format_message(msg, fmt))


def cmd_search(args: argparse.Namespace, client: DiscordClient) -> None:
    """Search messages by content substring."""
    guild_id = int(args.guild_id)
    validate_guild(client, guild_id)

    query = args.query.lower()
    limit = min(args.limit, MAX_LIMIT)
    max_scan = args.max_scan
    fmt = args.format
    author_filter = args.author.lower() if args.author else None

    if args.channel:
        channel_ids = [resolve_channel(client, args.channel)]
    else:
        channels = client.list_channels(guild_id)
        channel_ids = [ch["id"] for ch in channels]

    found = 0

    for channel_id in channel_ids:
        if found >= limit:
            break

        scanned = 0
        params: dict[str, Any] = {}

        while scanned < max_scan and found < limit:
            batch_size = min(MAX_PER_REQUEST, max_scan - scanned)
            params["limit"] = batch_size

            try:
                batch = client.get(f"/channels/{channel_id}/messages", params)
            except Exception:
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


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the query CLI."""
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
    p_history.add_argument("channel", help="Channel ID, or guild_id:channel_name.")
    p_history.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Number of messages (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
    )
    p_history.add_argument("--before", help="Fetch messages before this point (ISO datetime or snowflake ID).")
    p_history.add_argument("--after", help="Fetch messages after this point (ISO datetime or snowflake ID).")
    p_history.add_argument(
        "--format", choices=["jsonl", "text"], default="jsonl", help="Output format (default: jsonl)."
    )

    # search
    p_search = subparsers.add_parser("search", help="Search messages by content substring.")
    p_search.add_argument("guild_id", help="Discord guild (server) ID.")
    p_search.add_argument("query", help="Search term (case-insensitive substring match).")
    p_search.add_argument("--channel", help="Limit search to this channel (ID or guild_id:channel_name).")
    p_search.add_argument("--author", help="Filter by author username (case-insensitive substring match).")
    p_search.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help=f"Max results (default {DEFAULT_LIMIT}, max {MAX_LIMIT})."
    )
    p_search.add_argument(
        "--max-scan",
        type=int,
        default=DEFAULT_MAX_SCAN,
        help=f"Max messages to scan per channel (default {DEFAULT_MAX_SCAN}).",
    )
    p_search.add_argument(
        "--format", choices=["jsonl", "text"], default="jsonl", help="Output format (default: jsonl)."
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    import os

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("Error: DISCORD_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)

    with DiscordClient(token) as client:
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
