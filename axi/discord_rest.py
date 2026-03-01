"""Shared Discord REST helpers for CLI utilities."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

API_BASE = "https://discord.com/api/v10"
MAX_RETRIES = 3


def get_token() -> str:
    """Get Discord bot token for CLI scripts.

    Resolution order:
    1. Slot-based: derive instance name from this file's path, look up reserved
       bot token in ~/.config/axi/.test-slots.json + test-config.json.
       This takes priority so agents in test worktrees use the test instance's
       token, not the inherited DISCORD_TOKEN from the parent process.
    2. DISCORD_TOKEN env var (set by prime's .env, or explicitly).
    3. sender_token from test-config.json defaults (last resort).
    """
    config_dir = os.path.expanduser("~/.config/axi")
    config_path = os.path.join(config_dir, "test-config.json")

    # Try slot-based resolution first: instance name = basename of repo/worktree root
    slots_path = os.path.join(config_dir, ".test-slots.json")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    instance_name = os.path.basename(repo_root)

    try:
        with open(slots_path) as f:
            slots = json.load(f)
        slot = slots.get(instance_name)
        if slot:
            with open(config_path) as f:
                config = json.load(f)
            return config["bots"][slot["token_id"]]["token"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    # No slot — use env var (works for prime and explicit overrides)
    token = os.environ.get("DISCORD_TOKEN")
    if token:
        return token

    # Last resort: sender_token (works for guilds the sender bot is a member of)
    try:
        with open(config_path) as f:
            config = json.load(f)
        token = config.get("defaults", {}).get("sender_token")
        if token:
            return token
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    print("Error: No Discord token found (no slot, no DISCORD_TOKEN, no sender_token).", file=sys.stderr)
    sys.exit(1)


def make_client(token: str, *, timeout: float = 30.0) -> httpx.Client:
    """Create an httpx Client configured for the Discord API."""
    return httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bot {token}"},
        timeout=httpx.Timeout(timeout),
    )


def api_get(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Any:
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
