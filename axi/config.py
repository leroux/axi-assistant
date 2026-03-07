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
    "AXI_CATEGORY_NAME",
    "AXI_USER_DATA",
    "BOT_DIR",
    "BOT_WORKTREES_DIR",
    "BRIDGE_SOCKET_PATH",
    "CHANNEL_STATUS_ENABLED",
    "CLEAN_TOOL_MESSAGES",
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
    "MCP_SERVERS_PATH",
    "QUERY_TIMEOUT",
    "RATE_LIMIT_HISTORY_PATH",
    "README_CONTENT_PATH",
    "ROLLBACK_MARKER_PATH",
    "SCHEDULES_PATH",
    "SCHEDULE_TIMEZONE",
    "SHOW_AWAITING_INPUT",
    "STREAMING_DISCORD",
    "STREAMING_EDIT_INTERVAL",
    "USAGE_HISTORY_PATH",
    "VALID_MODELS",
    "WEB_ENABLED",
    "WEB_PORT",
    "discord_client",
    "get_model",
    "intents",
    "load_mcp_servers",
    "log",
    "set_model",
]

import json
import logging
import os
import threading
import time
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from typing import Any, ClassVar
from zoneinfo import ZoneInfo


class _ColorFormatter(logging.Formatter):
    """Formatter that adds ANSI color codes to log lines based on level."""

    LEVEL_COLORS: ClassVar[dict[int, str]] = {
        logging.DEBUG: "\033[2m",       # dim
        logging.INFO: "\033[32m",       # green
        logging.WARNING: "\033[33m",    # yellow
        logging.ERROR: "\033[31m",      # red
        logging.CRITICAL: "\033[1;31m",  # bold red
    }
    RESET: ClassVar[str] = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        color = self.LEVEL_COLORS.get(record.levelno)
        if color:
            return f"{color}{msg}{self.RESET}"
        return msg

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

from axi.log_context import StructuredContextFilter

log = logging.getLogger("axi")
log.setLevel(logging.DEBUG)

# Structured context filter — injects ctx_prefix, ctx_agent, etc. into all records.
# Installed on handlers (not the logger) so it applies to child logger records too
# (e.g. axi.channels, axi.shutdown) that propagate to these handlers.
_ctx_filter = StructuredContextFilter()

# Console handler: configurable via LOG_LEVEL env var (default INFO)
_console_handler = logging.StreamHandler()
_console_handler.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
_console_handler.addFilter(_ctx_filter)
_console_fmt = _ColorFormatter("%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] [%(ctx_prefix)s] %(message)s")
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
_file_handler.addFilter(_ctx_filter)
_file_fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(funcName)s:%(lineno)d] [%(ctx_prefix)s] %(message)s")
_file_fmt.converter = time.gmtime
_file_handler.setFormatter(_file_fmt)
log.addHandler(_file_handler)

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

FLOWCODER_ENABLED = os.environ.get("FLOWCODER_ENABLED", "1").lower() in ("1", "true", "yes")
STREAMING_DISCORD = os.environ.get("STREAMING_DISCORD", "").lower() in ("1", "true", "yes")
CHANNEL_STATUS_ENABLED = os.environ.get("CHANNEL_STATUS_ENABLED", "").lower() in ("1", "true", "yes")
CLEAN_TOOL_MESSAGES = os.environ.get("CLEAN_TOOL_MESSAGES", "").lower() in ("1", "true", "yes")
WEB_ENABLED = os.environ.get("WEB_ENABLED", "").lower() in ("1", "true", "yes")
WEB_PORT = int(os.environ.get("WEB_PORT", "8420"))

# Context compaction threshold — fraction of context window that triggers auto-compact.
# Default 0.80 (80%). Set lower to compact earlier, higher to use more context before compacting.
COMPACT_THRESHOLD = float(os.environ.get("COMPACT_THRESHOLD", "0.80"))

# Streaming edit interval in seconds — how often to edit the Discord message with new content.
# Must stay well under Discord's per-channel rate limit (~5 req/5s).
STREAMING_EDIT_INTERVAL = float(os.environ.get("STREAMING_EDIT_INTERVAL", "1.5"))

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
    guild_reactions=True,
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
MCP_SERVERS_PATH = os.path.join(AXI_USER_DATA, "mcp_servers.json")

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
    """Get the current model preference.

    The AXI_MODEL env var takes precedence over the config file.
    This is used by test instances to force haiku.
    """
    env_override = os.environ.get("AXI_MODEL", "").lower()
    if env_override and env_override in VALID_MODELS:
        return env_override
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
# Custom MCP server registry
# ---------------------------------------------------------------------------


def load_mcp_servers(names: list[str]) -> dict[str, dict[str, Any]]:
    """Load named MCP server configs from mcp_servers.json.

    Returns a dict of {name: McpStdioServerConfig-compatible dict} for each
    requested name that exists in the config file.  Unknown names are logged
    and skipped.
    """
    if not os.path.exists(MCP_SERVERS_PATH):
        if names:
            log.warning("mcp_servers.json not found at %s", MCP_SERVERS_PATH)
        return {}
    try:
        with open(MCP_SERVERS_PATH) as f:
            registry: dict[str, Any] = json.load(f)
    except Exception as e:
        log.error("Failed to load mcp_servers.json: %s", e)
        return {}

    result: dict[str, dict[str, Any]] = {}
    for name in names:
        if name not in registry:
            log.warning("MCP server '%s' not found in mcp_servers.json", name)
            continue
        result[name] = registry[name]
    return result


# ---------------------------------------------------------------------------
# Numeric constants
# ---------------------------------------------------------------------------

MASTER_AGENT_NAME = "axi-master"
MAX_AWAKE_AGENTS = int(os.environ.get("MAX_AWAKE_AGENTS", "7"))
IDLE_REMINDER_THRESHOLDS = [timedelta(minutes=30), timedelta(hours=3), timedelta(hours=48)]
QUERY_TIMEOUT = 43200  # 12 hours
INTERRUPT_TIMEOUT = 15  # seconds to wait after interrupt
API_ERROR_MAX_RETRIES = 3
API_ERROR_BASE_DELAY = 5  # seconds, doubles each retry

ACTIVE_CATEGORY_NAME = "Active"
AXI_CATEGORY_NAME = "Axi"
KILLED_CATEGORY_NAME = "Killed"

# ---------------------------------------------------------------------------
# Discord REST API client
# ---------------------------------------------------------------------------

from discordquery import AsyncDiscordClient

discord_client = AsyncDiscordClient(DISCORD_TOKEN)
