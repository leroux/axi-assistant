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
    "FC_WRAP",
    "FLOWCODER_ENABLED",
    "HISTORY_PATH",
    "HTTP_API_HOST",
    "HTTP_API_PORT",
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
    "SCHEDULES_PATH",
    "SCHEDULE_TIMEZONE",
    "SKIPS_PATH",
    "STREAMING_DISCORD",
    "STREAMING_EDIT_INTERVAL",
    "USAGE_HISTORY_PATH",
    "VALID_HARNESSES",
    "VALID_MODELS",
    "discord_client",
    "get_default_agent_type",
    "get_effort",
    "get_fc_wrap",
    "get_harness",
    "get_model",
    "get_model_runtime",
    "get_resolved_model",
    "intents",
    "load_mcp_servers",
    "log",
    "normalize_model",
    "set_model",
    "uses_chatgpt_proxy",
    "validate_model",
]

import json
import logging
import os
import re
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

from axi.discord_wire import emit_rest_audit_event
from axi.egress_filter import scrub_secrets
from axi.log_context import StructuredContextFilter

load_dotenv()

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

# Route discord.py's internal logger to our handlers so slash command errors
# (autocomplete failures, command invocation errors) aren't silently dropped.
_discord_logger = logging.getLogger("discord")
_discord_logger.setLevel(logging.WARNING)
_discord_logger.addHandler(_console_handler)
_discord_logger.addHandler(_file_handler)

# Route FlowCoder library loggers so execution errors aren't silently dropped.
for _fc_logger_name in ("src.controllers", "src.services", "src.embedding"):
    _fc_logger = logging.getLogger(_fc_logger_name)
    _fc_logger.setLevel(logging.WARNING)
    _fc_logger.addHandler(_console_handler)
    _fc_logger.addHandler(_file_handler)

# Route agenthub and claudewire loggers so wake/transport errors are visible.
for _pkg_logger_name in ("agenthub", "claudewire"):
    _pkg_logger = logging.getLogger(_pkg_logger_name)
    _pkg_logger.setLevel(logging.DEBUG)
    _pkg_logger.addHandler(_console_handler)
    _pkg_logger.addHandler(_file_handler)

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

VALID_HARNESSES = {"claude_code", "flowcoder"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _normalize_harness(value: str | None) -> str | None:
    normalized = (value or "").strip().lower().replace("-", "_")
    aliases = {
        "claude": "claude_code",
        "claudecode": "claude_code",
        "claude_code": "claude_code",
        "flow_coder": "flowcoder",
        "flowcoder": "flowcoder",
    }
    return aliases.get(normalized)


def get_harness() -> str:
    """Return the configured agent harness: ``claude_code`` or ``flowcoder``."""
    harness = _normalize_harness(os.environ.get("AXI_HARNESS"))
    if harness:
        return harness

    # Backwards compatibility for older installs. AXI_HARNESS is the preferred
    # single knob; FLOWCODER_ENABLED only matters when the new knob is absent.
    legacy_flowcoder = os.environ.get("FLOWCODER_ENABLED")
    if legacy_flowcoder is not None and legacy_flowcoder.strip().lower() in _FALSE_VALUES:
        return "claude_code"
    if legacy_flowcoder is not None and legacy_flowcoder.strip().lower() in _TRUE_VALUES:
        return "flowcoder"
    return "flowcoder"


def get_default_agent_type() -> str:
    """Return the default AgentSession type for newly registered agents."""
    return get_harness()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def get_fc_wrap() -> str | None:
    """Return the FlowCoder auto-wrap flowchart name, or None when disabled."""
    raw_value = os.environ.get("AXI_FC_WRAP")
    if raw_value is None:
        legacy_value = os.environ.get("AXI_SOUL_WRAP_ENABLED")
        if legacy_value is not None:
            return "soul" if _env_bool("AXI_SOUL_WRAP_ENABLED", default=True) else None
        raw_value = "soul"

    raw = raw_value.strip()
    normalized = raw.lower().replace("_", "-")
    if normalized in {"", "0", "false", "no", "off", "none"}:
        return None
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", raw):
        return None
    return raw


FLOWCODER_ENABLED = get_harness() == "flowcoder"
FC_WRAP = get_fc_wrap()
STREAMING_DISCORD = os.environ.get("STREAMING_DISCORD", "").lower() in ("1", "true", "yes")
CHANNEL_STATUS_ENABLED = os.environ.get("CHANNEL_STATUS_ENABLED", "").lower() in ("1", "true", "yes")
CHANNEL_SORT_BY_RECENCY = os.environ.get("CHANNEL_SORT_BY_RECENCY", "").lower() in ("1", "true", "yes")
CLEAN_TOOL_MESSAGES = os.environ.get("CLEAN_TOOL_MESSAGES", "").lower() in ("1", "true", "yes")

# Context compaction threshold — fraction of context window that triggers auto-compact.
# Default 0.80 (80%). Set lower to compact earlier, higher to use more context before compacting.
COMPACT_THRESHOLD = float(os.environ.get("COMPACT_THRESHOLD", "0.80"))

# Idle sleep threshold — seconds of inactivity before auto-sleeping an awake agent.
IDLE_SLEEP_SECONDS = int(os.environ.get("IDLE_SLEEP_SECONDS", "60"))

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
AXI_USER_DATA = os.environ.get("AXI_USER_DATA", os.path.expanduser("~/app-user-data/axi-assistant"))
SCHEDULE_TIMEZONE = ZoneInfo(os.environ.get("SCHEDULE_TIMEZONE", "UTC"))
DISCORD_GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
DAY_BOUNDARY_HOUR = int(os.environ.get("DAY_BOUNDARY_HOUR", "0"))
ENABLE_CRASH_HANDLER = os.environ.get("ENABLE_CRASH_HANDLER", "").lower() in ("1", "true", "yes")
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
    voice_states=True,
)

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_WORKTREES_DIR = os.environ.get("AXI_WORKTREES_DIR", os.path.join(os.path.expanduser("~"), "axi-tests"))
SCHEDULES_PATH = os.path.join(AXI_USER_DATA, "schedules.json")
SKIPS_PATH = os.path.join(AXI_USER_DATA, "schedule_skips.json")
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
LEGACY_MODEL_ALIASES = {
    "codex": "gpt-5.4",
}
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
_CHATGPT_PROXY_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "o5")

CHATGPT_PROXY_DEFAULT_ENV = {
    "ANTHROPIC_API_KEY": "test",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:3000",
    "ANTHROPIC_MODEL": "gpt-5.4",
}
# Backwards-compatible import name for older tests/extensions.
CODEX_PROXY_ENV = dict(CHATGPT_PROXY_DEFAULT_ENV)


_config_lock = threading.Lock()


def get_effort() -> str:
    """Get the effort level for Claude Code agent sessions."""
    effort = os.environ.get("AXI_EFFORT", "max").strip().lower()
    aliases = {
        "xhigh": "max",
        "maximum": "max",
    }
    effort = aliases.get(effort, effort)
    if effort not in {"low", "medium", "high", "max"}:
        return "max"
    return effort


def _normalize_model_selector(model: str) -> str:
    model = model.strip()
    lower = model.lower()
    if lower in VALID_MODELS:
        return lower
    return LEGACY_MODEL_ALIASES.get(lower, model)


def normalize_model(model: str) -> str:
    return _normalize_model_selector(model)


def validate_model(model: str) -> str:
    normalized = _normalize_model_selector(model)
    if not _is_valid_model_name(normalized):
        return (
            f"Invalid model '{model}'. Use a Claude alias like "
            f"{', '.join(sorted(VALID_MODELS))} or a provider model ID like gpt-5.4."
        )
    return ""


def _is_valid_model_name(model: str) -> bool:
    return bool(model and _MODEL_NAME_RE.fullmatch(model))


def uses_chatgpt_proxy(model: str) -> bool:
    """Return whether a model name should be routed through the ChatGPT proxy."""
    normalized = _normalize_model_selector(model).lower()
    return normalized.startswith(_CHATGPT_PROXY_MODEL_PREFIXES)


def _chatgpt_proxy_env(model: str) -> dict[str, str]:
    proxy_base_url = (
        os.environ.get("AXI_CHATGPT_PROXY_BASE_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or CHATGPT_PROXY_DEFAULT_ENV["ANTHROPIC_BASE_URL"]
    )
    proxy_api_key = (
        os.environ.get("AXI_CHATGPT_PROXY_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or CHATGPT_PROXY_DEFAULT_ENV["ANTHROPIC_API_KEY"]
    )
    return {
        "ANTHROPIC_API_KEY": proxy_api_key,
        "ANTHROPIC_BASE_URL": proxy_base_url,
        "ANTHROPIC_MODEL": model,
    }


def get_model_runtime(model: str) -> tuple[str | None, dict[str, str]]:
    """Resolve an Axi model selector into Claude model args and env vars."""
    resolved = _normalize_model_selector(model)
    if uses_chatgpt_proxy(resolved):
        return None, _chatgpt_proxy_env(resolved)
    return resolved, {}


def get_resolved_model(model: str | None = None) -> tuple[str, str | None, dict[str, str]]:
    """Return an Axi model along with resolved Claude runtime settings."""
    resolved_input = get_model() if model is None else _normalize_model_selector(model)
    resolved_model, env = get_model_runtime(resolved_input)
    return resolved_input, resolved_model, env


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
    Native Claude aliases and provider model IDs are both accepted.
    """
    env_override = os.environ.get("AXI_MODEL", "").strip()
    if env_override:
        return _normalize_model_selector(env_override)
    with _config_lock:
        config = _load_config()
    return _normalize_model_selector(str(config.get("model", "opus")))


def set_model(model: str) -> str:
    """Set the model preference. Returns validation error string or empty string on success."""
    normalized = _normalize_model_selector(model)
    error = validate_model(normalized)
    if error:
        return error
    with _config_lock:
        config = _load_config()
        config["model"] = normalized
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
HTTP_API_PORT = int(os.environ.get("HTTP_API_PORT", "0"))
HTTP_API_HOST = os.environ.get("HTTP_API_HOST", "127.0.0.1")
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

from axi.metrics import observe_discord_rest_request
from discordquery import AsyncDiscordClient

discord_client = AsyncDiscordClient(
    DISCORD_TOKEN,
    on_request_observer=lambda method, path, status, duration: observe_discord_rest_request(
        "discordquery",
        method,
        path,
        status,
        duration,
    ),
)

discord_client.content_filter = scrub_secrets
discord_client.audit_hook = emit_rest_audit_event
