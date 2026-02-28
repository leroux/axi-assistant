"""MCP tools for per-agent schedule management.

Each spawned agent gets its own MCP server instance with its identity
captured via closures (mirrors the make_cwd_permission_callback pattern).
The module is self-contained — no imports from bot.py.

Exports:
    make_schedule_mcp_server  — factory returning a per-agent McpSdkServerConfig
    schedule_key              — composite key helper for schedule_last_fired / check_skip
    schedules_lock            — shared asyncio.Lock for schedules.json access
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
from croniter import croniter

from axi import config

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

#: Shared lock for schedules.json read-modify-write cycles.
#: Must be acquired by both MCP tools and check_schedules when writing.
schedules_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SCHEDULES_PER_AGENT = 20
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
_MAX_NAME_LEN = 50
_MAX_PROMPT_LEN = 2000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def schedule_key(entry: dict[str, Any]) -> str:
    """Compute a globally-unique key for schedule_last_fired / check_skip.

    Entries with an ``owner`` field produce ``"{owner}/{name}"``.
    Legacy entries (no owner) produce just ``"{name}"``.
    This is never stored — only used at runtime for lookups.
    """
    owner = entry.get("owner") or entry.get("session")
    return f"{owner}/{entry['name']}" if owner else entry["name"]


def _load(path: str) -> list[dict[str, Any]]:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(path: str, entries: list[dict[str, Any]]) -> None:
    with open(path, "w") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Schedule persistence (moved from agents.py — pure file I/O, no agent state)
# ---------------------------------------------------------------------------


def load_schedules() -> list[dict[str, Any]]:
    return _load(config.SCHEDULES_PATH)


def save_schedules(entries: list[dict[str, Any]]) -> None:
    _save(config.SCHEDULES_PATH, entries)


def load_history() -> list[dict[str, Any]]:
    return _load(config.HISTORY_PATH)


def append_history(entry: dict[str, Any], fired_at: datetime) -> None:
    history = load_history()
    history.append(
        {
            "name": entry["name"],
            "prompt": entry["prompt"],
            "fired_at": fired_at.isoformat(),
        }
    )
    _save(config.HISTORY_PATH, history)


def prune_history() -> None:
    history = load_history()
    cutoff = datetime.now(UTC) - timedelta(days=7)
    pruned = [h for h in history if datetime.fromisoformat(h["fired_at"]) > cutoff]
    if len(pruned) != len(history):
        _save(config.HISTORY_PATH, pruned)


def load_skips() -> list[dict[str, Any]]:
    return _load(config.SKIPS_PATH)


def save_skips(skips: list[dict[str, Any]]) -> None:
    _save(config.SKIPS_PATH, skips)


def prune_skips() -> None:
    """Remove skip entries whose date has passed."""
    skips = load_skips()
    today = datetime.now(config.SCHEDULE_TIMEZONE).date()
    pruned = [s for s in skips if datetime.strptime(s["skip_date"], "%Y-%m-%d").replace(tzinfo=UTC).date() >= today]
    if len(pruned) != len(skips):
        save_skips(pruned)


def check_skip(name: str) -> bool:
    """Check if a recurring event should be skipped today."""
    skips = load_skips()
    today = datetime.now(config.SCHEDULE_TIMEZONE).strftime("%Y-%m-%d")
    for skip in skips:
        if skip.get("name") == name and skip.get("skip_date") == today:
            skips.remove(skip)
            save_skips(skips)
            return True
    return False


def _text(msg: str) -> dict[str, Any]:
    """Return a successful MCP tool response with a single text block."""
    return {"content": [{"type": "text", "text": msg}]}


def _error(msg: str) -> dict[str, Any]:
    """Return an MCP tool error response."""
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_schedule_mcp_server(agent_name: str, schedules_path: str):
    """Create a per-agent schedule MCP server.

    Args:
        agent_name: The owning agent's name (captured in tool closures).
        schedules_path: Absolute path to schedules.json.

    Returns:
        McpSdkServerConfig with schedule_list, schedule_create, and
        schedule_delete tools, all scoped to *agent_name*.
    """

    # -- Closures over agent_name -----------------------------------------

    def _my_schedules(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [e for e in entries if (e.get("owner") or e.get("session")) == agent_name]

    # -- Tool handlers ----------------------------------------------------

    async def handle_schedule_list(args: dict[str, Any]) -> dict[str, Any]:
        async with schedules_lock:
            entries = _load(schedules_path)

        mine = _my_schedules(entries)
        if not mine:
            return _text("You have no scheduled tasks.")

        result: list[dict[str, Any]] = []
        for e in mine:
            item: dict[str, Any] = {"name": e["name"], "prompt": e["prompt"]}
            if "schedule" in e:
                item["type"] = "recurring"
                item["schedule"] = e["schedule"]
            elif "at" in e:
                item["type"] = "one_off"
                item["at"] = e["at"]
            if e.get("reset_context"):
                item["reset_context"] = True
            result.append(item)

        return _text(json.dumps(result, indent=2))

    async def handle_schedule_create(args: dict[str, Any]) -> dict[str, Any]:
        name = (args.get("name") or "").strip()
        prompt = (args.get("prompt") or "").strip()
        stype = (args.get("schedule_type") or "").strip()
        cron_expr = (args.get("cron") or "").strip()
        at_str = (args.get("at") or "").strip()
        reset_context = bool(args.get("reset_context", False))

        # --- Validate name ---
        if not name or len(name) > _MAX_NAME_LEN or not _NAME_RE.match(name):
            return _error(
                f"Invalid name: must be 1-{_MAX_NAME_LEN} chars, lowercase "
                "alphanumeric and hyphens, starting with a letter or digit."
            )

        # --- Validate prompt ---
        if not prompt:
            return _error("Prompt is required and cannot be empty.")
        if len(prompt) > _MAX_PROMPT_LEN:
            return _error(f"Prompt too long ({len(prompt)} chars). Max is {_MAX_PROMPT_LEN}.")

        # --- Validate schedule type ---
        if stype not in ("recurring", "one_off"):
            return _error("schedule_type must be 'recurring' or 'one_off'.")

        # --- Validate type-specific fields ---
        if stype == "recurring":
            if not cron_expr:
                return _error("Missing 'cron' parameter (required for recurring schedules).")
            if not croniter.is_valid(cron_expr):
                return _error(f"Invalid cron expression: '{cron_expr}'.")
        else:
            if not at_str:
                return _error("Missing 'at' parameter (required for one-off schedules).")
            try:
                fire_at = datetime.fromisoformat(at_str)
            except (ValueError, TypeError):
                return _error(
                    f"Cannot parse datetime: '{at_str}'. Use ISO 8601 format (e.g. '2026-03-01T14:00:00+00:00')."
                )
            if fire_at.tzinfo is None:
                return _error("Datetime must include timezone information (e.g. +00:00 or Z at the end).")
            if fire_at <= datetime.now(UTC):
                return _error("Datetime is in the past. Provide a future time.")

        # --- Acquire lock and write ---
        async with schedules_lock:
            entries = _load(schedules_path)
            mine = _my_schedules(entries)

            # Per-agent uniqueness
            if any(e["name"] == name for e in mine):
                return _error(
                    f"A schedule named '{name}' already exists. Use a different name or delete the existing one first."
                )

            # Per-agent limit
            if len(mine) >= MAX_SCHEDULES_PER_AGENT:
                return _error(f"Schedule limit reached ({MAX_SCHEDULES_PER_AGENT}). Delete an existing schedule first.")

            # Build entry
            entry: dict[str, Any] = {
                "name": name,
                "prompt": prompt,
                "owner": agent_name,
            }
            if stype == "recurring":
                entry["schedule"] = cron_expr
            else:
                entry["at"] = at_str
            if reset_context:
                entry["reset_context"] = True

            entries.append(entry)
            _save(schedules_path, entries)

        # --- Success ---
        tz_note = os.environ.get("SCHEDULE_TIMEZONE", "UTC")
        if stype == "recurring":
            return _text(
                f"Created recurring schedule '{name}' with cron '{cron_expr}'. Cron is evaluated in {tz_note} timezone."
            )
        else:
            return _text(f"Created one-off schedule '{name}' firing at {at_str}.")

    async def handle_schedule_delete(args: dict[str, Any]) -> dict[str, Any]:
        name = (args.get("name") or "").strip()
        if not name:
            return _error("Name is required.")

        async with schedules_lock:
            entries = _load(schedules_path)

            # Find the entry — must match BOTH name and owner
            idx = None
            for i, e in enumerate(entries):
                if e.get("name") == name and e.get("owner") == agent_name:
                    idx = i
                    break

            if idx is None:
                return _error(f"Schedule '{name}' not found.")

            entries.pop(idx)
            _save(schedules_path, entries)

        return _text(f"Deleted schedule '{name}'.")

    # -- Build SdkMcpTool instances ---------------------------------------

    list_tool = SdkMcpTool(
        name="schedule_list",
        description=("List all of your scheduled tasks (one-off and recurring)."),
        input_schema={"type": "object", "properties": {}},
        handler=handle_schedule_list,
    )

    create_tool = SdkMcpTool(
        name="schedule_create",
        description=(
            "Create a new scheduled task. Use schedule_type 'recurring' with "
            "a cron expression for repeating schedules, or 'one_off' with an "
            "ISO 8601 datetime for a single future event."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Short identifier (lowercase letters, numbers, hyphens). Must be unique among your schedules."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": ("The message that will be sent to you when this schedule fires."),
                },
                "schedule_type": {
                    "type": "string",
                    "enum": ["recurring", "one_off"],
                    "description": ("Whether this repeats on a cron schedule or fires once."),
                },
                "cron": {
                    "type": "string",
                    "description": (
                        "Cron expression (e.g. '0 9 * * *' for daily at 9am). Required if schedule_type is 'recurring'."
                    ),
                },
                "at": {
                    "type": "string",
                    "description": (
                        "ISO 8601 datetime with timezone "
                        "(e.g. '2026-03-01T14:00:00+00:00'). "
                        "Required if schedule_type is 'one_off'. "
                        "Must be in the future."
                    ),
                },
                "reset_context": {
                    "type": "boolean",
                    "description": (
                        "If true, resets your conversation context when this schedule fires. Default: false."
                    ),
                },
            },
            "required": ["name", "prompt", "schedule_type"],
        },
        handler=handle_schedule_create,
    )

    delete_tool = SdkMcpTool(
        name="schedule_delete",
        description="Delete one of your scheduled tasks by name.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": ("The name of the schedule to delete (as shown in schedule_list)."),
                },
            },
            "required": ["name"],
        },
        handler=handle_schedule_delete,
    )

    return create_sdk_mcp_server(
        name="schedule",
        version="1.0.0",
        tools=[list_tool, create_tool, delete_tool],
    )
