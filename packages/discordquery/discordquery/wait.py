"""Wait for new messages in a Discord channel.

Polls the Discord API and returns as soon as new messages appear.
Designed for fast cross-bot communication and integration testing.

Outputs matching messages as JSONL, followed by a cursor line.
Use the cursor as --after on the next call to avoid missing messages.

Usage:
    python -m discordquery wait <channel_id> [options]

Examples:
    # Wait for any new message after a specific message ID
    python -m discordquery wait 123456789 --after 987654321

    # Chain calls with cursor:
    #   msg=$(python -m discordquery wait 123 --after 456)
    #   cursor=$(echo "$msg" | tail -1 | jq -r .cursor)
    #   python -m discordquery wait 123 --after $cursor

    # Wait for next message (auto-detects latest as baseline)
    python -m discordquery wait 123456789
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from discordquery.client import DiscordClient

DEFAULT_TIMEOUT = 120
POLL_INTERVAL = 2.0
DEFAULT_STABLE_POLLS = 3


def _message_matches(msg: dict[str, Any], substring: str | None) -> bool:
    if substring is None:
        return True
    return substring.lower() in msg.get("content", "").lower()


def get_latest_message_id(client: DiscordClient, channel_id: str) -> str | None:
    """Get the ID of the most recent message in a channel."""
    messages = client.get_messages(channel_id, limit=1)
    if messages:
        return messages[0]["id"]
    return None


def format_wait_message(msg: dict[str, Any]) -> str:
    """Format a message for wait output."""
    ts = msg.get("timestamp", "")
    author = msg.get("author", {})
    return json.dumps(
        {
            "id": msg.get("id"),
            "ts": ts,
            "author": author.get("username", "unknown"),
            "author_id": author.get("id"),
            "content": msg.get("content", ""),
        }
    )


def is_system_message(msg: dict[str, Any]) -> bool:
    """Check if a message is a bot system message (prefixed with *System:*)."""
    content = msg.get("content", "")
    return content.startswith("*System:*")


def wait_for_messages(
    client: DiscordClient,
    channel_id: str,
    after_id: str,
    timeout: float,
    ignore_author_ids: set[str],
    ignore_system: bool = True,
    poll_interval: float = POLL_INTERVAL,
    substring: str | None = None,
    stable_polls: int = DEFAULT_STABLE_POLLS,
) -> tuple[list[dict[str, Any]], str]:
    """Poll until new messages appear after after_id.

    Returns (matching_messages, cursor) where cursor is the highest
    message ID seen (including filtered messages), so the next call
    with --after cursor won't re-process anything.

    If substring is provided, only returns once a matching message appears.
    If stable_polls > 0, returns accumulated messages after that many polls
    with no new matching messages once at least one matching message has been seen.
    """
    deadline = time.monotonic() + timeout
    cursor = after_id
    collected: list[dict[str, Any]] = []
    idle_polls = 0

    while time.monotonic() < deadline:
        messages = client.get_messages(channel_id, limit=100, after=after_id)

        if messages:
            # Track the highest ID seen (Discord returns newest-first)
            cursor = messages[0]["id"]

            # Filter and collect in chronological order
            matching: list[dict[str, Any]] = []
            for msg in reversed(messages):
                author_id = msg.get("author", {}).get("id", "")

                if author_id in ignore_author_ids:
                    continue

                if ignore_system and is_system_message(msg):
                    continue

                if _message_matches(msg, substring):
                    matching.append(msg)

            after_id = cursor

            if matching:
                collected.extend(matching)
                if substring is not None:
                    return collected, cursor
                idle_polls = 0
                if stable_polls <= 0:
                    return collected, cursor
            elif collected and stable_polls > 0:
                idle_polls += 1
                if idle_polls >= stable_polls:
                    return collected, cursor
            elif stable_polls <= 0 and substring is None and collected:
                return collected, cursor
        elif collected and stable_polls > 0:
            idle_polls += 1
            if idle_polls >= stable_polls:
                return collected, cursor

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    return collected, cursor


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the wait CLI."""
    parser = argparse.ArgumentParser(
        description="Wait for new messages in a Discord channel.",
    )
    parser.add_argument("channel_id", help="Discord channel ID to watch.")
    parser.add_argument("--after", help="Wait for messages after this message ID.")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"Max seconds to wait (default {DEFAULT_TIMEOUT})."
    )
    parser.add_argument(
        "--ignore-author-id", action="append", default=[], help="Ignore messages from this author ID (repeatable)."
    )
    parser.add_argument(
        "--include-system", action="store_true", help="Include system messages (default: skip *System:* messages)."
    )
    parser.add_argument(
        "--poll-interval", type=float, default=POLL_INTERVAL, help=f"Seconds between polls (default {POLL_INTERVAL})."
    )
    parser.add_argument(
        "--check", help="Wait until a message contains this case-insensitive substring."
    )
    parser.add_argument(
        "--stable-polls",
        type=int,
        default=DEFAULT_STABLE_POLLS,
        help=f"Number of idle polls before returning accumulated messages (default {DEFAULT_STABLE_POLLS}). Use 0 to return on first match.",
    )
    parser.add_argument("--no-cursor", action="store_true", help="Don't emit cursor line at end of output.")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    import os

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("Error: DISCORD_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)

    ignore_ids = set(args.ignore_author_id)

    with DiscordClient(token, timeout=10.0) as client:
        after_id = args.after
        if not after_id:
            after_id = get_latest_message_id(client, args.channel_id)
            if not after_id:
                print("Error: Channel has no messages.", file=sys.stderr)
                sys.exit(1)

        messages, cursor = wait_for_messages(
            client,
            args.channel_id,
            after_id,
            args.timeout,
            ignore_ids,
            ignore_system=not args.include_system,
            poll_interval=args.poll_interval,
            substring=args.check,
            stable_polls=args.stable_polls,
        )

        if messages:
            for msg in messages:
                print(format_wait_message(msg))
            if not args.no_cursor:
                print(json.dumps({"cursor": cursor}))
        else:
            if not args.no_cursor:
                print(json.dumps({"cursor": cursor}))
            print("Error: Timed out waiting for message.", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
