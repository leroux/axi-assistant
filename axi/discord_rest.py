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
    """Get bot token from DISCORD_TOKEN env var or test-config.json fallback."""
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
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
