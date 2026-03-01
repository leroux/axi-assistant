"""Helper for sending Discord messages to test channels and collecting responses."""
import json
import os
import sys
import time

import httpx

with open(os.path.expanduser("~/.config/axi/test-config.json")) as f:
    _cfg = json.load(f)
TOKEN = _cfg["defaults"]["sender_token"]
GUILD_ID = "1475631458243710977"


def get_client():
    return httpx.Client(
        base_url="https://discord.com/api/v10",
        headers={"Authorization": f"Bot {TOKEN}"},
        timeout=httpx.Timeout(10.0),
    )


def find_channel(client, name):
    channels = client.get(f"/guilds/{GUILD_ID}/channels").json()
    for c in channels:
        if c["name"] == name and c["type"] == 0:
            return c["id"]
    return None


def send_and_wait(client, channel_id, msg, timeout=120, verbose=True):
    sent = client.post(f"/channels/{channel_id}/messages", json={"content": msg}).json()
    sent_id = sent["id"]
    if verbose:
        print(f"\n>>> {msg}")

    deadline = time.monotonic() + timeout
    after_id = sent_id
    responses = []
    while time.monotonic() < deadline:
        time.sleep(3)
        msgs = client.get(
            f"/channels/{channel_id}/messages",
            params={"after": after_id, "limit": 10},
        ).json()
        if msgs:
            after_id = msgs[0]["id"]
            for m in reversed(msgs):
                if m["id"] == sent_id:
                    continue
                content = m.get("content", "")
                author = m.get("author", {}).get("username", "?")
                if "awaiting input" in content.lower():
                    if verbose:
                        print("[sentinel]")
                    return responses
                if content:
                    responses.append(content)
                    if verbose:
                        print(f"<<< [{author}] {content[:600]}")
    if verbose:
        print("[timeout]")
    return responses


def main():
    if len(sys.argv) < 3:
        print("Usage: python test_discord.py <channel_name> <message>")
        sys.exit(1)
    channel_name = sys.argv[1]
    message = " ".join(sys.argv[2:])

    with get_client() as client:
        ch_id = find_channel(client, channel_name)
        if not ch_id:
            print(f"Channel #{channel_name} not found")
            sys.exit(1)
        send_and_wait(client, ch_id, message)


if __name__ == "__main__":
    main()
