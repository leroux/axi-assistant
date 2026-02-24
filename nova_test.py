#!/usr/bin/env python3
"""Send a message to Nova and wait for a response. Fast feedback loop for testing.

Usage:
    nova_test.py "your message here"
    nova_test.py "your message here" --timeout 60
    nova_test.py "your message here" --channel 1475637434736840877
    nova_test.py --poll-after 1475639782599037128   # just poll for response after a known msg ID
"""
import argparse
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

API_BASE = "https://discord.com/api/v10"
NOVA_GUILD_ID = "1475631458243710977"
DEFAULT_CHANNEL = "1475637434736840877"  # axi-master in Nova's server
POLL_INTERVAL = 3  # seconds
FINISHED_SENTINEL = "Bot has finished responding"


def get_token() -> str:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("Error: DISCORD_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    return token


def make_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bot {token}"},
        timeout=httpx.Timeout(30.0),
    )


def send_message(client: httpx.Client, channel_id: str, content: str) -> str:
    """Send a message, return the message ID."""
    resp = client.post(f"/channels/{channel_id}/messages", json={"content": content})
    if resp.status_code not in (200, 201):
        print(f"Error sending message: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)
    msg_id = resp.json()["id"]
    return msg_id


def poll_response(client: httpx.Client, channel_id: str, after_id: str, timeout: int) -> list[dict]:
    """Poll for messages after `after_id` until we see the finished sentinel or timeout."""
    start = time.time()
    last_count = 0

    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        elapsed = int(time.time() - start)

        resp = client.get(f"/channels/{channel_id}/messages", params={
            "after": after_id,
            "limit": 50,
        })
        if resp.status_code != 200:
            print(f"  [{elapsed}s] Poll error: {resp.status_code}", file=sys.stderr)
            continue

        messages = resp.json()
        # Discord returns newest-first with 'after', reverse for chronological
        messages.reverse()

        if len(messages) != last_count:
            last_count = len(messages)
            # Print new messages as they arrive
            for msg in messages:
                author = msg["author"]["username"]
                content = msg["content"]
                if FINISHED_SENTINEL not in content:
                    preview = content[:120] + ("..." if len(content) > 120 else "")
                    print(f"  [{elapsed}s] {author}: {preview}")

        # Check if Nova is done
        for msg in messages:
            if FINISHED_SENTINEL in msg.get("content", ""):
                # Return all non-sentinel messages
                return [m for m in messages if FINISHED_SENTINEL not in m.get("content", "")]

    print(f"\n⏱ Timeout after {timeout}s — Nova may still be working.", file=sys.stderr)
    # Return whatever we have
    resp = client.get(f"/channels/{channel_id}/messages", params={"after": after_id, "limit": 50})
    if resp.status_code == 200:
        messages = resp.json()
        messages.reverse()
        return [m for m in messages if FINISHED_SENTINEL not in m.get("content", "")]
    return []


def print_response(messages: list[dict]) -> None:
    """Pretty-print the response messages."""
    if not messages:
        print("\n(no response)")
        return

    print(f"\n{'─' * 60}")
    for msg in messages:
        author = msg["author"]["username"]
        content = msg["content"]
        print(f"  {author}: {content}")
    print(f"{'─' * 60}")


def main():
    parser = argparse.ArgumentParser(description="Send a message to Nova and wait for response.")
    parser.add_argument("message", nargs="?", help="Message to send to Nova")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="Channel ID to use")
    parser.add_argument("--timeout", type=int, default=120, help="Max seconds to wait (default: 120)")
    parser.add_argument("--poll-after", help="Skip sending, just poll for response after this message ID")
    args = parser.parse_args()

    if not args.message and not args.poll_after:
        parser.error("Either provide a message or --poll-after")

    token = get_token()

    with make_client(token) as client:
        if args.poll_after:
            after_id = args.poll_after
            print(f"Polling for response after message {after_id}...")
        else:
            print(f"→ Sending: {args.message}")
            after_id = send_message(client, args.channel, args.message)
            print(f"  Sent (id: {after_id}). Waiting for Nova...")

        responses = poll_response(client, args.channel, after_id, args.timeout)
        print_response(responses)


if __name__ == "__main__":
    main()
