"""Centralized configuration for the Axi bot.

Leaf module — no project imports. All env vars, paths, constants, logging setup,
Discord REST client, and user config management live here.
"""

from __future__ import annotations

__all__ = [
    "ACTIVE_CATEGORY_NAME",
    "ADMIN_ALLOWED_CWDS",
    "ALLOWED_CWDS",
    "ALLOWED_USER_IDS",
    "API_ERROR_BASE_DELAY",
    "API_ERROR_MAX_RETRIES",
    "AXI_USER_DATA",
    "BOT_DIR",
    "BOT_WORKTREES_DIR",
    "BRIDGE_SOCKET_PATH",
    "CONFIG_PATH",
    "CRASH_ANALYSIS_MARKER_PATH",
    "DAY_BOUNDARY_HOUR",
    "DEFAULT_CWD",
    "DISCORD_GUILD_ID",
    "DISCORD_TOKEN",
    "ENABLE_CRASH_HANDLER",
    "FLOWCODER_ENABLED",
    "HISTORY_PATH",
    "IDLE_REMINDER_THRESHOLDS",
    "INTERRUPT_TIMEOUT",
    "KILLED_CATEGORY_NAME",
    "LOG_DIR",
    "MASTER_AGENT_NAME",
    "MASTER_SESSION_PATH",
    "MAX_AWAKE_AGENTS",
    "QUERY_TIMEOUT",
    "RATE_LIMIT_HISTORY_PATH",
    "README_CONTENT_PATH",
    "ROLLBACK_MARKER_PATH",
    "SCHEDULES_PATH",
    "SCHEDULE_TIMEZONE",
    "SHOW_AWAITING_INPUT",
    "USAGE_HISTORY_PATH",
    "VALID_MODELS",
    "discord_request",
    "get_model",
    "intents",
    "log",
    "set_model",
]

import asyncio
import json
import logging
import os
import threading
import time
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

log = logging.getLogger("axi")
log.setLevel(logging.DEBUG)

# Console handler: configurable via LOG_LEVEL env var (default INFO)
_console_handler = logging.StreamHandler()
_console_handler.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
_console_fmt = logging.Formatter("%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s")
_console_fmt.converter = time.gmtime
_console_handler.setFormatter(_console_fmt)
log.addHandler(_console_handler)

# File handler: DEBUG level, rotating 10MB x 3 backups
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "orchestrator.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=3,
)
_file_handler.setLevel(logging.DEBUG)
_file_fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(funcName)s:%(lineno)d] %(message)s")
_file_fmt.converter = time.gmtime
_file_handler.setFormatter(_file_fmt)
log.addHandler(_file_handler)

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

FLOWCODER_ENABLED = os.environ.get("FLOWCODER_ENABLED", "").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Discord token resolution
# ---------------------------------------------------------------------------


def _resolve_discord_token() -> str:
    """Resolve Discord token from env or test slot reservation.

    For prime: reads DISCORD_TOKEN from .env as usual.
    For test instances: derives instance name from the bot directory,
    looks up the reserved token from ~/.config/axi/.test-slots.json
    and ~/.config/axi/test-config.json. No token in .env needed.
    """
    token = os.environ.get("DISCORD_TOKEN")
    if token:
        return token

    bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    instance_name = os.path.basename(bot_dir)
    config_dir = os.path.expanduser("~/.config/axi")
    slots_path = os.path.join(config_dir, ".test-slots.json")
    config_path = os.path.join(config_dir, "test-config.json")

    try:
        with open(slots_path) as f:
            slots = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"DISCORD_TOKEN not set and cannot read {slots_path}: {e}\n"
            f"Set DISCORD_TOKEN in .env or reserve a slot: axi-test up {instance_name}"
        ) from None

    slot = slots.get(instance_name)
    if not slot:
        raise RuntimeError(
            f"DISCORD_TOKEN not set and no slot for '{instance_name}' in {slots_path}\n"
            f"Reserve a slot: axi-test up {instance_name}"
        )

    try:
        with open(config_path) as f:
            config = json.load(f)
        return config["bots"][slot["token_id"]]["token"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"Cannot resolve token for bot '{slot.get('token_id')}': {e}") from None


DISCORD_TOKEN = _resolve_discord_token()

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

ALLOWED_USER_IDS = {int(uid.strip()) for uid in os.environ["ALLOWED_USER_IDS"].split(",")}
DEFAULT_CWD = os.environ.get("DEFAULT_CWD", os.getcwd())
AXI_USER_DATA = os.environ.get("AXI_USER_DATA", os.path.expanduser("~/axi-user-data"))
SCHEDULE_TIMEZONE = ZoneInfo(os.environ.get("SCHEDULE_TIMEZONE", "UTC"))
DISCORD_GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
DAY_BOUNDARY_HOUR = int(os.environ.get("DAY_BOUNDARY_HOUR", "0"))
ENABLE_CRASH_HANDLER = os.environ.get("ENABLE_CRASH_HANDLER", "").lower() in ("1", "true", "yes")
SHOW_AWAITING_INPUT = os.environ.get("SHOW_AWAITING_INPUT", "").lower() in ("1", "true", "yes")
README_CONTENT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "readme_content.md")

# ---------------------------------------------------------------------------
# Discord intents
# ---------------------------------------------------------------------------

from discord import Intents

intents = Intents(
    guilds=True,
    guild_messages=True,
    message_content=True,
    dm_messages=True,
)

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_WORKTREES_DIR = os.path.join(os.path.dirname(BOT_DIR), "axi-tests")
SCHEDULES_PATH = os.path.join(AXI_USER_DATA, "schedules.json")
HISTORY_PATH = os.path.join(AXI_USER_DATA, "schedule_history.json")
ROLLBACK_MARKER_PATH = os.path.join(BOT_DIR, ".rollback_performed")
CRASH_ANALYSIS_MARKER_PATH = os.path.join(BOT_DIR, ".crash_analysis")
BRIDGE_SOCKET_PATH = os.path.join(BOT_DIR, ".bridge.sock")
MASTER_SESSION_PATH = os.path.join(BOT_DIR, ".master_session_id")
CONFIG_PATH = os.path.join(BOT_DIR, "config.json")
RATE_LIMIT_HISTORY_PATH = os.path.join(LOG_DIR, "rate_limit_history.jsonl")
USAGE_HISTORY_PATH = os.path.join(AXI_USER_DATA, "usage_history.jsonl")

# Directories agents are allowed to use as cwd (configurable via .env)
_allowed_cwds_env = os.environ.get("ALLOWED_CWDS", "")
_base_cwds: list[str] = [
    os.path.realpath(os.path.expanduser(p)) for p in (_allowed_cwds_env.split(":") if _allowed_cwds_env else [])
] + [os.path.realpath(AXI_USER_DATA), os.path.realpath(BOT_DIR), os.path.realpath(BOT_WORKTREES_DIR)]

# Extra directories that admin agents (rooted in BOT_DIR) can spawn into and write to
_admin_cwds_env = os.environ.get("ADMIN_ALLOWED_CWDS", "")
ADMIN_ALLOWED_CWDS: list[str] = [
    os.path.realpath(os.path.expanduser(p)) for p in (_admin_cwds_env.split(":") if _admin_cwds_env else [])
]
ALLOWED_CWDS: list[str] = _base_cwds + ADMIN_ALLOWED_CWDS

# ---------------------------------------------------------------------------
# User configuration management
# ---------------------------------------------------------------------------

VALID_MODELS = {"haiku", "sonnet", "opus"}
_config_lock = threading.Lock()


def _load_config() -> dict[str, Any]:
    """Load user configuration from file. Caller must hold _config_lock if consistency matters."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                data: dict[str, Any] = json.load(f)
                return data
        except Exception as e:
            log.warning("Failed to load config: %s", e)
    return {}


def _save_config(config: dict[str, Any]) -> None:
    """Save user configuration to file. Caller must hold _config_lock."""
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        log.error("Failed to save config: %s", e)


def get_model() -> str:
    """Get the current model preference."""
    with _config_lock:
        config = _load_config()
    return config.get("model", "opus")


def set_model(model: str) -> str:
    """Set the model preference. Returns validation error string or empty string on success."""
    if model.lower() not in VALID_MODELS:
        return f"Invalid model '{model}'. Valid options: {', '.join(sorted(VALID_MODELS))}"
    with _config_lock:
        config = _load_config()
        config["model"] = model.lower()
        _save_config(config)
    return ""


# ---------------------------------------------------------------------------
# Numeric constants
# ---------------------------------------------------------------------------

MASTER_AGENT_NAME = "axi-master"
MAX_AWAKE_AGENTS = 3  # max concurrent awake agents (each ~280MB); MemoryMax=2G on service
IDLE_REMINDER_THRESHOLDS = [timedelta(minutes=30), timedelta(hours=3), timedelta(hours=48)]
QUERY_TIMEOUT = 43200  # 12 hours
INTERRUPT_TIMEOUT = 15  # seconds to wait after interrupt
API_ERROR_MAX_RETRIES = 3
API_ERROR_BASE_DELAY = 5  # seconds, doubles each retry

ACTIVE_CATEGORY_NAME = "Active"
KILLED_CATEGORY_NAME = "Killed"

# ---------------------------------------------------------------------------
# Discord REST API client
# ---------------------------------------------------------------------------

_discord_api = httpx.AsyncClient(
    base_url="https://discord.com/api/v10",
    headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
    timeout=15.0,
)


async def discord_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Make a Discord API request with rate-limit retry."""
    resp: httpx.Response | None = None
    for _attempt in range(3):
        resp = await _discord_api.request(method, path, **kwargs)
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1.0)
            log.warning("Discord API rate limited on %s %s, retrying after %.1fs", method, path, retry_after)
            await asyncio.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp
    assert resp is not None
    resp.raise_for_status()
    return resp
