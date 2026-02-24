#!/usr/bin/env python3
"""Wait for new messages in a Discord channel.

Polls the Discord API and returns as soon as new messages appear.
Designed for fast cross-bot communication (e.g., Prime <-> Nova).

Outputs matching messages as JSONL, followed by a cursor line.
Use the cursor as --after on the next call to avoid missing messages.

Usage:
    wait_for_message.py <channel_id> [options]

Examples:
    # Wait for any new message after a specific message ID
    wait_for_message.py 123456789 --after 987654321

    # Chain calls with cursor (never miss messages):
    #   msg=$(python wait_for_message.py 123 --after 456)
    #   cursor=$(echo "$msg" | tail -1 | jq -r .cursor)
    #   python wait_for_message.py 123 --after $cursor

    # Wait for next message (auto-detects latest as baseline)
    wait_for_message.py 123456789
"""
import argparse
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

API_BASE = "https://discord.com/api/v10"
DEFAULT_TIMEOUT = 120
POLL_INTERVAL = 2.0  # seconds between polls


def get_token() -> str:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("Error: DISCORD_TOKEN not set in environment.", file=sys.stderr)
        sys.exit(1)
    return token


def api_get(client: httpx.Client, path: str, params: dict | None = None) -> list | dict:
    """GET with rate-limit retry."""
    for attempt in range(3):
        resp = client.get(path, params=params)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            retry_after = float(resp.json().get("retry_after", 1.0))
            time.sleep(retry_after)
            continue
        if resp.status_code >= 500 and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        print(f"Error: Discord API {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)
    print("Error: Exhausted retries.", file=sys.stderr)
    sys.exit(1)


def get_latest_message_id(client: httpx.Client, channel_id: str) -> str | None:
    """Get the ID of the most recent message in a channel."""
    messages = api_get(client, f"/channels/{channel_id}/messages", {"limit": 1})
    if messages:
        return messages[0]["id"]
    return None


def format_message(msg: dict) -> str:
    """Format a message for output."""
    ts = msg.get("timestamp", "")
    author = msg.get("author", {})
    return json.dumps({
        "id": msg.get("id"),
        "ts": ts,
        "author": author.get("username", "unknown"),
        "author_id": author.get("id"),
        "content": msg.get("content", ""),
    })


def is_system_message(msg: dict) -> bool:
    """Check if a message is a bot system message."""
    content = msg.get("content", "")
    return content.startswith("*System:*")


def wait_for_messages(
    client: httpx.Client,
    channel_id: str,
    after_id: str,
    timeout: float,
    ignore_author_ids: set[str],
    ignore_system: bool = True,
    poll_interval: float = POLL_INTERVAL,
) -> tuple[list[dict], str]:
    """Poll until new messages appear after after_id.

    Returns (matching_messages, cursor) where cursor is the highest
    message ID seen (including filtered messages), so the next call
    with --after cursor won't re-process anything.
    """
    deadline = time.monotonic() + timeout
    cursor = after_id

    while time.monotonic() < deadline:
        messages = api_get(
            client,
            f"/channels/{channel_id}/messages",
            {"after": after_id, "limit": 100},
        )

        if messages:
            # Track the highest ID seen (Discord returns newest-first)
            cursor = messages[0]["id"]

            # Filter and collect in chronological order
            matching = []
            for msg in reversed(messages):
                author_id = msg.get("author", {}).get("id", "")

                if author_id in ignore_author_ids:
                    continue

                if ignore_system and is_system_message(msg):
                    continue

                matching.append(msg)

            if matching:
                return matching, cursor

            # Messages exist but all filtered — advance baseline so we
            # don't re-fetch the same filtered messages every poll
            after_id = cursor

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    return [], cursor


def main():
    parser = argparse.ArgumentParser(
        description="Wait for new messages in a Discord channel.",
    )
    parser.add_argument("channel_id", help="Discord channel ID to watch.")
    parser.add_argument(
        "--after",
        help="Wait for messages after this message ID. If omitted, uses the latest message.",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help=f"Max seconds to wait (default {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--ignore-author-id", action="append", default=[],
        help="Ignore messages from this author ID (can be repeated).",
    )
    parser.add_argument(
        "--include-system", action="store_true",
        help="Include system messages (default: skip *System:* messages).",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=POLL_INTERVAL,
        help=f"Seconds between polls (default {POLL_INTERVAL}).",
    )
    parser.add_argument(
        "--no-cursor", action="store_true",
        help="Don't emit cursor line at end of output.",
    )

    args = parser.parse_args()
    token = get_token()
    ignore_ids = set(args.ignore_author_id)

    with httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bot {token}"},
        timeout=httpx.Timeout(10.0),
    ) as client:
        # Determine baseline message ID
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
        )

        if messages:
            for msg in messages:
                print(format_message(msg))
            if not args.no_cursor:
                print(json.dumps({"cursor": cursor}))
        else:
            if not args.no_cursor:
                print(json.dumps({"cursor": cursor}))
            print("Error: Timed out waiting for message.", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
