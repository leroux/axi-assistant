"""Pure utilities for Discord data manipulation.

Snowflake/datetime conversion, message formatting, and message splitting.
No I/O, no network calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

DISCORD_EPOCH_MS = 1420070400000


def datetime_to_snowflake(dt: datetime) -> int:
    """Convert a datetime to a Discord snowflake ID."""
    ms = int(dt.timestamp() * 1000)
    return (ms - DISCORD_EPOCH_MS) << 22


def resolve_snowflake(value: str) -> int:
    """Parse a value as either a snowflake ID or ISO datetime, returning a snowflake.

    Raises ValueError if the value cannot be parsed.
    """
    if value.isdigit():
        return int(value)
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return datetime_to_snowflake(dt)
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as a snowflake ID or ISO datetime.") from None


def format_message(msg: dict[str, Any], fmt: str = "jsonl") -> str:
    """Format a Discord API message object for output.

    Args:
        msg: Raw Discord message object from the API.
        fmt: "jsonl" for JSON lines or "text" for human-readable.
    """
    ts = msg.get("timestamp", "")
    author = msg.get("author", {})
    author_name = author.get("username", "unknown")
    content = msg.get("content", "")
    attachments = len(msg.get("attachments", []))
    embeds = len(msg.get("embeds", []))

    if fmt == "text":
        try:
            dt = datetime.fromisoformat(ts)
            ts_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        except (ValueError, TypeError):
            ts_str = ts
        line = f"[{ts_str}] {author_name}: {content}"
        extras: list[str] = []
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


def split_message(text: str, limit: int = 2000) -> list[str]:
    """Split text into chunks that fit within Discord's message limit.

    Splits at newline boundaries when possible, falling back to hard
    splits at the limit.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks
