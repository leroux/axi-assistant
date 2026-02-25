import os
import re
import json
import time
import signal
import asyncio
import threading
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import base64

import discord
from dotenv import load_dotenv
from discord import Intents, app_commands, CategoryChannel, TextChannel
from discord.ext.commands import Bot
from discord.ext import tasks
from discord.enums import ChannelType
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, create_sdk_mcp_server, tool
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolPermissionContext,
    PermissionResultAllow,
    PermissionResultDeny,
)
from croniter import croniter
from shutdown import ShutdownCoordinator, kill_supervisor, exit_for_restart
from bridge import (
    BridgeConnection, BridgeTransport, ensure_bridge, build_cli_spawn_args,
    connect_to_bridge,
)
from schedule_tools import make_schedule_mcp_server, schedule_key, schedules_lock

load_dotenv()

# --- Logging setup ---
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

# Console handler: configurable via LOG_LEVEL env var (default INFO)
_console_handler = logging.StreamHandler()
_console_handler.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))
_console_fmt = logging.Formatter("%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s")
_console_fmt.converter = time.gmtime
_console_handler.setFormatter(_console_fmt)
log.addHandler(_console_handler)

# File handler: DEBUG level, rotating 10MB x 3 backups
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)
_file_handler = RotatingFileHandler(
    os.path.join(_log_dir, "orchestrator.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=3,
)
_file_handler.setLevel(logging.DEBUG)
_file_fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(funcName)s:%(lineno)d] %(message)s")
_file_fmt.converter = time.gmtime
_file_handler.setFormatter(_file_fmt)
log.addHandler(_file_handler)

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ALLOWED_USER_IDS = {int(uid.strip()) for uid in os.environ["ALLOWED_USER_IDS"].split(",")}
DEFAULT_CWD = os.environ.get("DEFAULT_CWD", os.getcwd())
AXI_USER_DATA = os.environ.get("AXI_USER_DATA", os.path.expanduser("~/axi-user-data"))
SCHEDULE_TIMEZONE = ZoneInfo(os.environ.get("SCHEDULE_TIMEZONE", "UTC"))
DISCORD_GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
DAY_BOUNDARY_HOUR = int(os.environ.get("DAY_BOUNDARY_HOUR", "0"))
ENABLE_CRASH_HANDLER = os.environ.get("ENABLE_CRASH_HANDLER", "").lower() in ("1", "true", "yes")
SHOW_AWAITING_INPUT = os.environ.get("SHOW_AWAITING_INPUT", "").lower() in ("1", "true", "yes")
README_CONTENT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "readme_content.md")

# --- Discord bot setup ---

intents = Intents(
    guilds=True,
    guild_messages=True,
    message_content=True,
    dm_messages=True,
)
bot = Bot(command_prefix="!", intents=intents)

# --- Scheduler state ---

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_WORKTREES_DIR = os.path.join(os.path.dirname(BOT_DIR), "axi-tests")
SCHEDULES_PATH = os.path.join(BOT_DIR, "schedules.json")
HISTORY_PATH = os.path.join(BOT_DIR, "schedule_history.json")
SKIPS_PATH = os.path.join(BOT_DIR, "schedule_skips.json")
ROLLBACK_MARKER_PATH = os.path.join(BOT_DIR, ".rollback_performed")
CRASH_ANALYSIS_MARKER_PATH = os.path.join(BOT_DIR, ".crash_analysis")
BRIDGE_SOCKET_PATH = os.path.join(BOT_DIR, ".bridge.sock")
schedule_last_fired: dict[str, datetime] = {}
_bot_start_time: datetime | None = None

# --- Agent session management ---

MASTER_AGENT_NAME = "axi-master"
MAX_AWAKE_AGENTS = 5  # max concurrent awake agents (each ~280MB); set based on available RAM
IDLE_REMINDER_THRESHOLDS = [timedelta(minutes=30), timedelta(hours=3), timedelta(hours=48)]
QUERY_TIMEOUT = 43200  # 12 hours
INTERRUPT_TIMEOUT = 15  # seconds to wait after interrupt
API_ERROR_MAX_RETRIES = 3
API_ERROR_BASE_DELAY = 5  # seconds, doubles each retry

ACTIVE_CATEGORY_NAME = "Active"
KILLED_CATEGORY_NAME = "Killed"


@dataclass
class ActivityState:
    """Real-time activity tracking for an agent during a query."""
    phase: str = "idle"           # "thinking", "writing", "tool_use", "waiting", "starting", "idle"
    tool_name: str | None = None  # Current tool being called (e.g. "Bash", "Read")
    tool_input_preview: str = ""  # First ~200 chars of tool input JSON
    turn_count: int = 0           # Number of API turns in current query
    query_started: datetime | None = None  # When the current query began
    last_event: datetime | None = None     # When the last stream event arrived
    text_chars: int = 0           # Characters of text generated in current turn


TOOL_DISPLAY_NAMES = {
    "Bash": "running bash command",
    "Read": "reading file",
    "Write": "writing file",
    "Edit": "editing file",
    "MultiEdit": "editing file",
    "Glob": "searching for files",
    "Grep": "searching code",
    "WebSearch": "searching the web",
    "WebFetch": "fetching web page",
    "Task": "running subagent",
    "NotebookEdit": "editing notebook",
    "TodoWrite": "updating tasks",
}


def _tool_display(name: str) -> str:
    """Human-readable description of a tool call."""
    if name in TOOL_DISPLAY_NAMES:
        return TOOL_DISPLAY_NAMES[name]
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return f"{parts[1]}: {parts[2]}"
    return f"using {name}"


@dataclass
class AgentSession:
    name: str
    client: ClaudeSDKClient | None = None
    cwd: str = ""
    query_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stderr_buffer: list[str] = field(default_factory=list)
    stderr_lock: threading.Lock = field(default_factory=threading.Lock)
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    system_prompt: dict | str | None = None
    _system_prompt_posted: bool = False  # Set True after posting system prompt to Discord
    last_idle_notified: datetime | None = None
    idle_reminder_count: int = 0
    session_id: str | None = None
    discord_channel_id: int | None = None
    message_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    mcp_servers: dict | None = None
    _reconnecting: bool = False  # True during bridge reconnect (blocks on_message from waking)
    _bridge_busy: bool = False   # True when reconnected to a mid-task CLI (bridge idle=False)
    activity: ActivityState = field(default_factory=ActivityState)
    _log: logging.Logger | None = None

    def __post_init__(self):
        """Set up per-agent logger writing to <assistant_dir>/logs/<name>.log."""
        os.makedirs(_log_dir, exist_ok=True)
        logger = logging.getLogger(f"agent.{self.name}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        if not logger.handlers:  # Avoid duplicate handlers on re-creation
            fh = RotatingFileHandler(
                os.path.join(_log_dir, f"{self.name}.log"),
                maxBytes=5 * 1024 * 1024,
                backupCount=2,
            )
            fh.setLevel(logging.DEBUG)
            _agent_fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
            _agent_fmt.converter = time.gmtime
            fh.setFormatter(_agent_fmt)
            logger.addHandler(fh)
        self._log = logger

    def close_log(self):
        """Remove all handlers from the per-agent logger."""
        if self._log:
            for handler in self._log.handlers[:]:
                handler.close()
                self._log.removeHandler(handler)


agents: dict[str, AgentSession] = {}
_wake_lock = asyncio.Lock()  # Serializes wake_agent calls to prevent TOCTOU races on concurrency limit

# Bridge connection — initialized in on_ready(), used by wake_agent/sleep_agent
bridge_conn: BridgeConnection | None = None

# Shutdown coordinator — initialized with a placeholder notify_fn because
# send_system/get_agent_channel aren't defined yet at import time.
# The real notify_fn is wired up in _init_shutdown_coordinator() called from on_ready.
shutdown_coordinator: ShutdownCoordinator | None = None

# Global rate limit state (all agents share the same API account)
_rate_limited_until: datetime | None = None
_rate_limit_retry_task: asyncio.Task | None = None

@dataclass
class SessionUsage:
    agent_name: str
    queries: int = 0
    total_cost_usd: float = 0.0
    total_turns: int = 0
    total_duration_ms: int = 0
    first_query: datetime | None = None
    last_query: datetime | None = None

_session_usage: dict[str, SessionUsage] = {}  # keyed by session_id

@dataclass
class RateLimitQuota:
    status: str              # "allowed", "allowed_warning", "rejected"
    resets_at: datetime      # from resetsAt unix timestamp
    rate_limit_type: str     # "five_hour"
    utilization: float | None = None  # 0.0-1.0, only present on warnings
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

_rate_limit_quota: RateLimitQuota | None = None

# Guild infrastructure (populated in on_ready)
target_guild: discord.Guild | None = None
active_category: CategoryChannel | None = None
killed_category: CategoryChannel | None = None
channel_to_agent: dict[int, str] = {}  # channel_id -> agent_name
_bot_creating_channels: set[str] = set()  # channel names currently being created by the bot


def make_stderr_callback(session: AgentSession):
    """Create a stderr callback bound to a specific agent session."""
    def callback(text: str) -> None:
        with session.stderr_lock:
            session.stderr_buffer.append(text)
    return callback


def drain_stderr(session: AgentSession) -> list[str]:
    """Drain stderr buffer for a specific agent session."""
    with session.stderr_lock:
        msgs = list(session.stderr_buffer)
        session.stderr_buffer.clear()
    return msgs


async def _as_stream(content: str | list):
    """Wrap a prompt as an AsyncIterable for streaming mode (required by can_use_tool).

    ``content`` may be a plain string or a list of content blocks (text + image)
    for multi-modal messages.
    """
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


# --- Emoji reaction helpers ---

async def _add_reaction(message: discord.Message | None, emoji: str) -> None:
    """Add a reaction to a message, silently ignoring errors."""
    if message is None:
        return
    try:
        await message.add_reaction(emoji)
        log.info("Reaction +%s on message %s", emoji, message.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Reaction +%s failed on message %s: %s", emoji, message.id, exc)


async def _remove_reaction(message: discord.Message | None, emoji: str) -> None:
    """Remove the bot's own reaction from a message, silently ignoring errors."""
    if message is None:
        return
    try:
        await message.remove_reaction(emoji, bot.user)
        log.info("Reaction -%s on message %s", emoji, message.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Reaction -%s failed on message %s: %s", emoji, message.id, exc)


# --- Image attachment support ---

_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB per image


async def _extract_message_content(message: discord.Message) -> str | list:
    """Extract text and image content from a Discord message.

    Returns a plain string if there are no image attachments, or a list of
    content blocks ``[{"type": "text", ...}, {"type": "image", ...}, ...]``
    when images are present.
    """
    image_attachments = [
        a for a in message.attachments
        if a.content_type
        and a.content_type.split(";")[0].strip() in _SUPPORTED_IMAGE_TYPES
        and a.size <= _MAX_IMAGE_SIZE
    ]

    if not image_attachments:
        return message.content

    blocks: list[dict] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})

    for attachment in image_attachments:
        try:
            data = await attachment.read()
            b64 = base64.b64encode(data).decode("utf-8")
            mime = attachment.content_type.split(";")[0].strip()
            blocks.append({"type": "image", "data": b64, "mimeType": mime})
            log.debug("Attached image: %s (%s, %d bytes)", attachment.filename, mime, len(data))
        except Exception:
            log.warning("Failed to download attachment %s", attachment.filename, exc_info=True)

    return blocks if blocks else message.content


def _content_summary(content: str | list) -> str:
    """Short text summary of message content for logging."""
    if isinstance(content, str):
        return content[:200]
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block["text"][:100])
        elif block.get("type") == "image":
            parts.append(f"[image:{block.get('mimeType', '?')}]")
    return " ".join(parts)[:200]


def drain_sdk_buffer(session: AgentSession) -> int:
    """Drain any stale messages from the SDK message buffer before sending a new query.

    The SDK's internal message buffer (_message_receive) is a FIFO queue shared
    across all query/response cycles.  If a previous response left unconsumed
    messages (e.g. post-ResultMessage system messages from the CLI), they would
    be read by the *next* stream_response_to_channel call, causing the agent to
    appear to replay old content instead of responding to the new message.

    Call this right before query() to flush any such stale data.
    Returns the number of messages drained.
    """
    if session.client is None or getattr(session.client, "_query", None) is None:
        return 0

    import anyio

    receive_stream = session.client._query._message_receive
    drained: list[dict] = []
    while True:
        try:
            msg = receive_stream.receive_nowait()
            drained.append(msg)
        except anyio.WouldBlock:
            break
        except Exception:
            break

    if drained:
        for msg in drained:
            msg_type = msg.get("type", "?")
            msg_role = msg.get("message", {}).get("role", "") if isinstance(msg.get("message"), dict) else ""
            log.warning(
                "Drained stale SDK message from '%s': type=%s role=%s",
                session.name, msg_type, msg_role,
            )
        log.warning("Total drained from '%s': %d stale messages", session.name, len(drained))

    return len(drained)


def make_cwd_permission_callback(allowed_cwd: str):
    """Create a can_use_tool callback that restricts file writes to allowed_cwd and AXI_USER_DATA."""
    allowed = os.path.realpath(allowed_cwd)
    user_data = os.path.realpath(AXI_USER_DATA)
    worktrees = os.path.realpath(BOT_WORKTREES_DIR)
    bot_dir = os.path.realpath(BOT_DIR)

    # Agents rooted in bot code or worktree dirs also get worktree write access
    is_code_agent = (allowed == bot_dir or allowed.startswith(bot_dir + os.sep) or
                     allowed == worktrees or allowed.startswith(worktrees + os.sep))
    bases = [allowed, user_data]
    if is_code_agent:
        bases.append(worktrees)

    async def _check_permission(
        tool_name: str, tool_input: dict, ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        # File-writing tools — check path is within allowed bases
        if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            resolved = os.path.realpath(path)
            for base in bases:
                if resolved == base or resolved.startswith(base + os.sep):
                    return PermissionResultAllow()
            return PermissionResultDeny(
                message=f"Access denied: {path} is outside working directory {allowed} and user data {user_data}"
            )
        # Everything else (Bash handled by sandbox, reads allowed everywhere)
        return PermissionResultAllow()

    return _check_permission


# --- Schedule helpers ---

def load_schedules() -> list[dict]:
    try:
        with open(SCHEDULES_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_schedules(entries: list[dict]) -> None:
    with open(SCHEDULES_PATH, "w") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")


def load_history() -> list[dict]:
    try:
        with open(HISTORY_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def append_history(entry: dict, fired_at: datetime) -> None:
    history = load_history()
    history.append({
        "name": entry["name"],
        "prompt": entry["prompt"],
        "fired_at": fired_at.isoformat(),
    })
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)
        f.write("\n")


def prune_history() -> None:
    history = load_history()
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    pruned = [h for h in history if datetime.fromisoformat(h["fired_at"]) > cutoff]
    if len(pruned) != len(history):
        with open(HISTORY_PATH, "w") as f:
            json.dump(pruned, f, indent=2)
            f.write("\n")


def load_skips() -> list[dict]:
    try:
        with open(SKIPS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_skips(skips: list[dict]) -> None:
    with open(SKIPS_PATH, "w") as f:
        json.dump(skips, f, indent=2)
        f.write("\n")


def prune_skips() -> None:
    """Remove skip entries whose date has passed."""
    skips = load_skips()
    today = datetime.now(SCHEDULE_TIMEZONE).date()
    pruned = [s for s in skips if datetime.strptime(s["skip_date"], "%Y-%m-%d").date() >= today]
    if len(pruned) != len(skips):
        save_skips(pruned)


def check_skip(name: str) -> bool:
    """Check if a recurring event should be skipped today. Returns True if skipped (and removes the entry)."""
    skips = load_skips()
    today = datetime.now(SCHEDULE_TIMEZONE).strftime("%Y-%m-%d")
    for skip in skips:
        if skip.get("name") == name and skip.get("skip_date") == today:
            skips.remove(skip)
            save_skips(skips)
            return True
    return False


# --- Channel topic helpers ---

def _format_channel_topic(cwd: str, session_id: str | None = None) -> str:
    """Format agent metadata for a Discord channel topic."""
    parts = [f"cwd: {cwd}"]
    if session_id:
        parts.append(f"session: {session_id}")
    return " | ".join(parts)


def _parse_channel_topic(topic: str | None) -> tuple[str | None, str | None]:
    """Parse cwd and session_id from a channel topic. Returns (cwd, session_id)."""
    if not topic:
        return None, None
    cwd = None
    session_id = None
    for part in topic.split("|"):
        part = part.strip()
        if part.startswith("cwd: "):
            cwd = part[5:].strip()
        elif part.startswith("session: "):
            session_id = part[9:].strip()
    return cwd, session_id


async def _set_session_id(session: AgentSession, msg: ResultMessage) -> None:
    """Extract session_id from a ResultMessage. Updates channel topic on first change."""
    sid = getattr(msg, "session_id", None)
    if sid and sid != session.session_id:
        session.session_id = sid
        # Only persist session_id to channel topic for non-master agents
        # (master gets a new session every restart so the topic would always churn)
        if session.name != MASTER_AGENT_NAME and session.discord_channel_id:
            ch = bot.get_channel(session.discord_channel_id)
            if ch:
                desired_topic = _format_channel_topic(session.cwd, sid)
                if ch.topic != desired_topic:
                    log.info("Updating topic on #%s: %r -> %r", ch.name, ch.topic, desired_topic)
                    await ch.edit(topic=desired_topic)
    else:
        session.session_id = sid


# --- System prompt construction from layered .md files ---
# SOUL.md: shared personality for ALL agents
# dev_context.md: axi-assistant development context (architecture, safety, test workflow)
# Content uses %(var)s interpolation — literal % must be escaped as %% in prompt files.


def _load_prompt_file(path: str, variables: dict[str, str] | None = None) -> str:
    """Load a prompt .md file, optionally expanding %(var)s placeholders."""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    if variables:
        content = content % variables
    return content


_PROMPT_VARS = {"axi_user_data": AXI_USER_DATA, "bot_dir": BOT_DIR}

_SOUL = _load_prompt_file(os.path.join(BOT_DIR, "SOUL.md"), _PROMPT_VARS)
_DEV_CONTEXT = _load_prompt_file(os.path.join(BOT_DIR, "dev_context.md"), _PROMPT_VARS)


def _is_axi_dev_cwd(cwd: str) -> bool:
    """Check if a working directory is within the axi-assistant codebase."""
    return cwd.startswith(BOT_DIR) or (
        BOT_WORKTREES_DIR and cwd.startswith(BOT_WORKTREES_DIR)
    )


# Master agent: soul + dev context (master is always an axi dev agent too)
MASTER_SYSTEM_PROMPT: dict = {
    "type": "preset",
    "preset": "claude_code",
    "append": _SOUL + "\n\n" + _DEV_CONTEXT,
}


def _make_spawned_agent_system_prompt(cwd: str) -> dict:
    """Build system prompt for a spawned agent based on its working directory."""
    if _is_axi_dev_cwd(cwd):
        # Axi dev agent — soul + dev context
        append = _SOUL + "\n\n" + _DEV_CONTEXT
    else:
        # General claw — soul only
        append = _SOUL
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": append,
    }


# --- Discord visibility for system prompts ---

import io as _io


async def _post_system_prompt_to_channel(
    channel: TextChannel,
    system_prompt: dict | str | None,
    *,
    is_resume: bool = False,
    session_id: str | None = None,
) -> None:
    """Post the system prompt as a file attachment to the agent's Discord channel.

    On resume, posts a brief note instead of the full prompt.
    On new sessions, posts the appended system prompt as an .md file attachment.
    """
    if is_resume:
        sid_display = f"`{session_id[:8]}…`" if session_id else "unknown"
        await channel.send(f"*System:* 📋 Resumed session {sid_display}")
        return

    if isinstance(system_prompt, dict):
        prompt_text = system_prompt.get("append", "")
        label = "claude_code preset + appended instructions"
    elif isinstance(system_prompt, str):
        prompt_text = system_prompt
        label = "custom system prompt (full replacement)"
    else:
        return

    line_count = len(prompt_text.splitlines())
    file = discord.File(
        _io.BytesIO(prompt_text.encode("utf-8")),
        filename="system-prompt.md",
    )
    await channel.send(f"*System:* 📋 {label} ({line_count} lines)", file=file)


# --- MCP tools for master agent ---

@tool(
    "axi_spawn_agent",
    "Spawn a new Axi agent session with its own Discord channel. "
    "Returns immediately with success/error message.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Unique short name, no spaces (e.g. 'feature-auth', 'fix-bug-123')"},
            "cwd": {"type": "string", "description": "Absolute path to the working directory for the agent. Defaults to a per-agent subdirectory under user data (agents/<name>/)."},
            "prompt": {"type": "string", "description": "Initial task instructions for the agent"},
            "resume": {"type": "string", "description": "Optional session ID to resume a previous agent session"},
        },
        "required": ["name", "prompt"],
    },
)
async def axi_spawn_agent(args):
    agent_name = args.get("name", "").strip()
    default_cwd = os.path.join(AXI_USER_DATA, "agents", agent_name) if agent_name else AXI_USER_DATA
    agent_cwd = os.path.realpath(os.path.expanduser(args.get("cwd", default_cwd)))
    agent_prompt = args.get("prompt", "")
    agent_resume = args.get("resume")

    ALLOWED_CWDS = (os.path.realpath(AXI_USER_DATA), os.path.realpath(BOT_DIR), os.path.realpath(BOT_WORKTREES_DIR))
    if not any(agent_cwd == d or agent_cwd.startswith(d + os.sep) for d in ALLOWED_CWDS):
        return {"content": [{"type": "text", "text": f"Error: cwd must be under {AXI_USER_DATA}, {BOT_DIR}, or {BOT_WORKTREES_DIR}."}], "is_error": True}

    if not agent_name:
        return {"content": [{"type": "text", "text": "Error: 'name' is required and cannot be empty."}], "is_error": True}
    if agent_name == MASTER_AGENT_NAME:
        return {"content": [{"type": "text", "text": f"Error: cannot spawn agent with reserved name '{MASTER_AGENT_NAME}'."}], "is_error": True}
    if agent_name in agents and not agent_resume:
        return {"content": [{"type": "text", "text": f"Error: agent '{agent_name}' already exists. Kill it first or use 'resume' to replace it."}], "is_error": True}

    async def _do_spawn():
        try:
            if agent_name in agents and agent_resume:
                await reclaim_agent_name(agent_name)
            await spawn_agent(agent_name, agent_cwd, agent_prompt, resume=agent_resume)
        except Exception:
            log.exception("Error in background spawn of agent '%s'", agent_name)
            try:
                channel = await get_agent_channel(agent_name)
                if channel:
                    await send_system(channel, f"Failed to spawn agent **{agent_name}**. Check logs for details.")
            except Exception:
                pass

    log.info("Spawning agent '%s' via MCP tool (cwd=%s, resume=%s)", agent_name, agent_cwd, agent_resume)
    asyncio.create_task(_do_spawn())
    return {"content": [{"type": "text", "text": f"Agent '{agent_name}' spawn initiated in {agent_cwd}. The agent's channel will be notified when it's ready."}]}


@tool(
    "axi_kill_agent",
    "Kill an Axi agent session and move its Discord channel to the Killed category. "
    "Returns the session ID (for resuming later) or an error message.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the agent to kill"},
        },
        "required": ["name"],
    },
)
async def axi_kill_agent(args):
    agent_name = args.get("name", "").strip()

    if not agent_name:
        return {"content": [{"type": "text", "text": "Error: 'name' is required and cannot be empty."}], "is_error": True}
    if agent_name == MASTER_AGENT_NAME:
        return {"content": [{"type": "text", "text": f"Error: cannot kill reserved agent '{MASTER_AGENT_NAME}'."}], "is_error": True}
    if agent_name not in agents:
        return {"content": [{"type": "text", "text": f"Error: agent '{agent_name}' not found."}], "is_error": True}

    session = agents.get(agent_name)
    session_id = session.session_id if session else None

    # Remove from agents dict immediately so the name is freed for respawn
    agents.pop(agent_name, None)

    async def _do_kill():
        try:
            agent_ch = await get_agent_channel(agent_name)
            if agent_ch:
                if session_id:
                    await send_system(
                        agent_ch,
                        f"Agent **{agent_name}** moved to Killed.\n"
                        f"Session ID: `{session_id}` — use this to resume later.",
                    )
                else:
                    await send_system(agent_ch, f"Agent **{agent_name}** moved to Killed.")
            await sleep_agent(session)
            await move_channel_to_killed(agent_name)
        except Exception:
            log.exception("Error in background kill of agent '%s'", agent_name)

    log.info("Killing agent '%s' via MCP tool (session=%s)", agent_name, session_id)
    asyncio.create_task(_do_kill())

    if session_id:
        return {"content": [{"type": "text", "text": f"Agent '{agent_name}' killed. Session ID: {session_id}"}]}
    return {"content": [{"type": "text", "text": f"Agent '{agent_name}' killed (no session ID available)."}]}


@tool(
    "get_date_and_time",
    "Get the current date and time with logical day/week calculations. "
    "Accounts for the user's configured day boundary (the hour when a new 'day' starts). "
    "Always call this first to orient yourself before working with plans.",
    {"type": "object", "properties": {}, "required": []},
)
async def get_date_and_time(args):
    import arrow

    tz = os.environ.get("SCHEDULE_TIMEZONE", "UTC")
    boundary = DAY_BOUNDARY_HOUR

    now = arrow.now(tz)

    # Logical date: if before boundary hour, it's still "yesterday"
    if now.hour < boundary:
        logical = now.shift(days=-1)
    else:
        logical = now

    # Logical week start (Sunday)
    # arrow weekday(): Monday=0 ... Sunday=6
    days_since_sunday = (logical.weekday() + 1) % 7
    week_start = logical.shift(days=-days_since_sunday).floor("day")
    week_end = week_start.shift(days=6)

    # Format day boundary display
    if boundary == 0:
        boundary_display = "12:00 AM (midnight)"
    elif boundary < 12:
        boundary_display = f"{boundary}:00 AM"
    elif boundary == 12:
        boundary_display = "12:00 PM (noon)"
    else:
        boundary_display = f"{boundary - 12}:00 PM"

    result = {
        "now": now.isoformat(),
        "now_display": now.format("dddd, MMM D, YYYY h:mm A"),
        "logical_date": logical.format("YYYY-MM-DD"),
        "logical_date_display": logical.format("dddd, MMM D, YYYY"),
        "logical_day_of_week": logical.format("dddd"),
        "logical_week_start": week_start.format("YYYY-MM-DD"),
        "logical_week_display": f"Week of {week_start.format('MMM D')} \u2013 {week_end.format('MMM D, YYYY')}",
        "timezone": tz,
        "day_boundary": boundary_display,
    }

    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


_utils_mcp_server = create_sdk_mcp_server(
    name="utils",
    version="1.0.0",
    tools=[get_date_and_time],
)

@tool(
    "axi_restart",
    "Restart the Axi bot. Waits for busy agents to finish first (graceful). "
    "Only use when the user explicitly asks you to restart.",
    {"type": "object", "properties": {}, "required": []},
)
async def axi_restart(args):
    log.info("Restart requested via MCP tool")
    if shutdown_coordinator is None:
        return {"content": [{"type": "text", "text": "Bot is not fully initialized yet."}]}
    asyncio.create_task(shutdown_coordinator.graceful_shutdown("MCP tool", skip_agent=MASTER_AGENT_NAME))
    return {"content": [{"type": "text", "text": "Graceful restart initiated. Waiting for busy agents to finish..."}]}


_axi_mcp_server = create_sdk_mcp_server(
    name="axi",
    version="1.0.0",
    tools=[axi_spawn_agent, axi_kill_agent, axi_restart],
)


# --- Discord REST MCP tools (for cross-server messaging) ---

import httpx

_discord_api = httpx.AsyncClient(
    base_url="https://discord.com/api/v10",
    headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
    timeout=15.0,
)


async def _discord_request(method: str, path: str, **kwargs) -> httpx.Response:
    """Make a Discord API request with rate-limit retry."""
    for attempt in range(3):
        resp = await _discord_api.request(method, path, **kwargs)
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1.0)
            log.warning("Discord API rate limited on %s %s, retrying after %.1fs", method, path, retry_after)
            await asyncio.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


@tool(
    "discord_list_channels",
    "List text channels in a Discord guild/server. Returns channel id, name, and category.",
    {
        "type": "object",
        "properties": {
            "guild_id": {"type": "string", "description": "The Discord guild (server) ID"},
        },
        "required": ["guild_id"],
    },
)
async def discord_list_channels(args):
    guild_id = args["guild_id"]
    try:
        resp = await _discord_request("GET", f"/guilds/{guild_id}/channels")
        channels = resp.json()
        # Filter to text channels (type 0) and format
        text_channels = []
        # Build category map
        categories = {c["id"]: c["name"] for c in channels if c["type"] == 4}
        for ch in channels:
            if ch["type"] == 0:  # GUILD_TEXT
                text_channels.append({
                    "id": ch["id"],
                    "name": ch["name"],
                    "category": categories.get(ch.get("parent_id"), None),
                })
        return {"content": [{"type": "text", "text": json.dumps(text_channels, indent=2)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


@tool(
    "discord_read_messages",
    "Read recent messages from a Discord channel. Returns formatted message history.",
    {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "The Discord channel ID"},
            "limit": {"type": "integer", "description": "Number of messages to fetch (default 20, max 100)"},
        },
        "required": ["channel_id"],
    },
)
async def discord_read_messages(args):
    channel_id = args["channel_id"]
    limit = min(args.get("limit", 20), 100)
    try:
        resp = await _discord_request("GET", f"/channels/{channel_id}/messages", params={"limit": limit})
        messages = resp.json()
        # Messages come newest-first; reverse for chronological order
        messages.reverse()
        formatted = []
        for msg in messages:
            author = msg.get("author", {}).get("username", "unknown")
            content = msg.get("content", "")
            timestamp = msg.get("timestamp", "")
            formatted.append(f"[{timestamp}] {author}: {content}")
        return {"content": [{"type": "text", "text": "\n".join(formatted)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


@tool(
    "discord_send_message",
    "Send a message to a Discord channel OTHER than your own. Your text responses are automatically delivered to your own channel — do NOT use this tool for that. This tool is only for cross-channel messaging.",
    {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "The Discord channel ID"},
            "content": {"type": "string", "description": "The message content to send"},
        },
        "required": ["channel_id", "content"],
    },
)
async def discord_send_message(args):
    channel_id = args["channel_id"]
    content = args["content"]
    # Prevent agents from sending to their own channel (responses are streamed automatically)
    agent_name = channel_to_agent.get(int(channel_id))
    if agent_name:
        return {
            "content": [{"type": "text", "text":
                f"Error: Cannot send to agent channel #{agent_name}. "
                f"Your text responses are automatically sent to your own channel. "
                f"Just write your response as normal text instead of using this tool. "
                f"This tool is only for sending messages to OTHER channels."}],
            "is_error": True,
        }
    try:
        resp = await _discord_request("POST", f"/channels/{channel_id}/messages", json={"content": content})
        msg = resp.json()
        return {"content": [{"type": "text", "text": f"Message sent (id: {msg['id']})"}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


_discord_mcp_server = create_sdk_mcp_server(
    name="discord",
    version="1.0.0",
    tools=[discord_list_channels, discord_read_messages, discord_send_message],
)


# --- Session lifecycle ---


def _get_subprocess_pid(client: ClaudeSDKClient) -> int | None:
    """Extract the PID of the underlying CLI subprocess from a ClaudeSDKClient.

    Returns None if the client has no live subprocess.
    """
    try:
        transport = getattr(client, "_transport", None) or getattr(
            getattr(client, "_query", None), "transport", None
        )
        if transport is None:
            return None
        proc = getattr(transport, "_process", None)
        if proc is None:
            return None
        return proc.pid
    except Exception:
        return None


def _ensure_process_dead(pid: int | None, label: str) -> None:
    """Send SIGTERM to *pid* if it is still alive.

    Workaround for a bug in claude-agent-sdk where Query.close()'s anyio
    cancel-scope leaks a CancelledError into the asyncio event loop,
    preventing SubprocessCLITransport.close() from calling
    process.terminate().  See test_process_leak.py for a reproducer.
    """
    if pid is None:
        return
    try:
        os.kill(pid, 0)  # check if alive (raises OSError if dead)
    except OSError:
        return  # already dead — nothing to do
    log.warning("Subprocess %d for '%s' survived disconnect — sending SIGTERM (SDK bug workaround)", pid, label)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


async def _disconnect_client(client: ClaudeSDKClient, label: str) -> None:
    """Disconnect a ClaudeSDKClient and ensure its subprocess is terminated.

    For bridge-backed clients, calls transport.close() which sends KILL to bridge.
    For direct subprocess clients, handles the anyio cancel-scope leak gracefully.
    """
    # Check if this client uses a BridgeTransport
    transport = getattr(client, "_transport", None)
    if isinstance(transport, BridgeTransport):
        # Bridge transport: close() sends KILL to bridge, no local process to worry about
        try:
            await asyncio.wait_for(transport.close(), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            log.warning("'%s' bridge transport close timed out", label)
        except Exception:
            log.exception("'%s' error closing bridge transport", label)
        return

    # Direct subprocess client — original logic
    pid = _get_subprocess_pid(client)
    try:
        await asyncio.wait_for(client.__aexit__(None, None, None), timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        log.warning("'%s' shutdown timed out or was cancelled", label)
    except RuntimeError as e:
        if "cancel scope" in str(e):
            log.debug("'%s' cross-task cleanup (expected): %s", label, e)
        else:
            raise
    _ensure_process_dead(pid, label)


async def end_session(name: str) -> None:
    """End a named Claude session and remove it from the registry."""
    session = agents.get(name)
    if session is None:
        return
    if session.client is not None:
        await _disconnect_client(session.client, name)
        session.client = None
    session.close_log()
    agents.pop(name, None)
    log.info("Claude session '%s' ended", name)


async def reset_session(name: str, cwd: str | None = None) -> AgentSession:
    """Reset a named session. Preserves its system prompt, channel mapping, and MCP servers.

    Creates a sleeping session (no client) — the agent will wake on next message.
    """
    session = agents.get(name)
    old_cwd = session.cwd if session else DEFAULT_CWD
    old_prompt = session.system_prompt if session else SYSTEM_PROMPT
    old_channel_id = session.discord_channel_id if session else None
    old_mcp = getattr(session, "mcp_servers", None)
    await end_session(name)
    new_session = AgentSession(
        name=name,
        cwd=cwd or old_cwd,
        system_prompt=old_prompt,
        client=None,
        session_id=None,
        discord_channel_id=old_channel_id,
        mcp_servers=old_mcp,
    )
    agents[name] = new_session
    log.info("Session '%s' reset (sleeping, cwd=%s)", name, new_session.cwd)
    return new_session


def _count_awake_agents() -> int:
    """Count the number of agents that are currently awake (client is not None)."""
    return sum(1 for s in agents.values() if s.client is not None)


async def _evict_idle_agent(exclude: str | None = None) -> bool:
    """Sleep the most idle non-busy awake agent to free a slot.

    Returns True if an agent was evicted, False if none available.
    """
    candidates = []
    for name, s in agents.items():
        if name == exclude:
            continue
        if s.client is None:
            continue  # already sleeping
        if s.query_lock.locked():
            continue  # busy
        if s._bridge_busy:
            continue  # reconnected to running CLI
        idle_duration = (datetime.now(timezone.utc) - s.last_activity).total_seconds()
        candidates.append((idle_duration, name, s))

    if not candidates:
        return False

    log.debug("Eviction candidates: %s", [(n, f"{s:.0f}s") for s, n, _ in candidates])

    # Evict the longest-idle agent
    candidates.sort(reverse=True, key=lambda x: x[0])
    idle_secs, evict_name, evict_session = candidates[0]
    log.info("Evicting idle agent '%s' (idle %.0fs) to free concurrency slot", evict_name, idle_secs)
    try:
        await sleep_agent(evict_session)
    except Exception:
        log.exception("Error evicting agent '%s'", evict_name)
        return False
    return True


async def _ensure_awake_slot(requesting_agent: str) -> bool:
    """Ensure there is a free awake-agent slot, evicting idle agents if needed.

    Call this before wake_agent() to enforce the concurrency limit.
    Returns True if a slot is available, False if all slots are busy.
    """
    while _count_awake_agents() >= MAX_AWAKE_AGENTS:
        log.debug("Awake slots full (%d/%d), attempting eviction for '%s'", _count_awake_agents(), MAX_AWAKE_AGENTS, requesting_agent)
        evicted = await _evict_idle_agent(exclude=requesting_agent)
        if not evicted:
            log.warning("Cannot free awake slot for '%s' — all %d slots busy", requesting_agent, MAX_AWAKE_AGENTS)
            return False
    return True


class ConcurrencyLimitError(Exception):
    """Raised when the awake-agent concurrency limit is reached and no slots can be freed."""
    pass


async def sleep_agent(session: AgentSession) -> None:
    """Shut down a session's ClaudeSDKClient but keep the AgentSession in the agents dict.

    No-op if the session is already sleeping (client is None).
    """
    if session.client is None:
        return

    log.info("Sleeping agent '%s'", session.name)
    if session._log:
        session._log.info("SESSION_SLEEP")
    session._bridge_busy = False
    await _disconnect_client(session.client, session.name)
    session.client = None
    log.info("Agent '%s' is now sleeping", session.name)


def _make_agent_options(session: AgentSession, resume_id: str | None = None) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a session."""
    return ClaudeAgentOptions(
        model="opus",
        effort="high",
        # thinking={"type": "enabled", "budget_tokens": 128000},
        thinking={"type": "adaptive"},
        betas=["context-1m-2025-08-07"],
        setting_sources=["local"],
        permission_mode="default",
        can_use_tool=make_cwd_permission_callback(session.cwd),
        cwd=session.cwd,
        system_prompt=session.system_prompt,
        include_partial_messages=True,
        stderr=make_stderr_callback(session),
        resume=resume_id,
        sandbox={"enabled": True, "autoAllowBashIfSandboxed": True},
        mcp_servers=session.mcp_servers or {},
    )


async def _wake_agent_via_bridge(session: AgentSession, options: ClaudeAgentOptions) -> ClaudeSDKClient:
    """Wake an agent using the bridge transport.

    Creates a BridgeTransport, spawns the CLI via bridge, then creates
    a ClaudeSDKClient using that transport.
    """
    transport = BridgeTransport(
        session.name, bridge_conn,
        stderr_callback=make_stderr_callback(session),
    )
    await transport.connect()

    # Build CLI args and spawn via bridge
    cli_args, env, cwd = build_cli_spawn_args(options)
    spawn_result = await transport.spawn(cli_args, env, cwd)

    if spawn_result.already_running:
        log.info("CLI for '%s' already running in bridge (pid=%s)", session.name, spawn_result.pid)

    # Subscribe to start receiving output
    await transport.subscribe()

    # Create SDK client with our bridge transport
    client = ClaudeSDKClient(options=options, transport=transport)
    await client.__aenter__()
    return client


async def wake_agent(session: AgentSession) -> None:
    """Wake a sleeping agent by creating a new ClaudeSDKClient.

    Uses the bridge process to manage the CLI subprocess when bridge_conn
    is available, otherwise falls back to direct subprocess management.

    No-op if the session is already awake (client is not None).
    Falls back to a fresh session if resume fails.
    Enforces the awake-agent concurrency limit by evicting idle agents if needed.
    Uses _wake_lock to prevent TOCTOU races when multiple agents try to wake concurrently.
    """
    if session.client is not None:
        return

    # Serialize concurrency check + wake to prevent races
    async with _wake_lock:
        if session.client is not None:
            return  # Re-check after acquiring lock

        log.debug("Wake lock acquired for '%s', awake_count=%d/%d", session.name, _count_awake_agents(), MAX_AWAKE_AGENTS)

        # Enforce concurrency limit before waking
        slot_available = await _ensure_awake_slot(session.name)
        if not slot_available:
            raise ConcurrencyLimitError(
                f"Cannot wake agent '{session.name}': all {MAX_AWAKE_AGENTS} awake slots are busy. "
                f"Message will be queued and processed when a slot opens."
            )

        log.debug("Awake slot secured for '%s'", session.name)

        log.info("Waking agent '%s' (session_id=%s)", session.name, session.session_id)
        resume_id = session.session_id

        options = _make_agent_options(session, resume_id)

        if bridge_conn and bridge_conn.is_alive:
            # Bridge mode: spawn CLI via bridge
            log.debug("Waking '%s' via bridge (resume=%s)", session.name, resume_id)
            try:
                client = await _wake_agent_via_bridge(session, options)
                session.client = client
                log.info("Agent '%s' is now awake via bridge (resumed=%s)", session.name, resume_id)
                if session._log:
                    session._log.info("SESSION_WAKE via bridge (resumed=%s)", bool(resume_id))
            except Exception:
                if resume_id:
                    log.warning("Failed to resume '%s' via bridge, retrying fresh", session.name)
                    options = _make_agent_options(session, resume_id=None)
                    try:
                        client = await _wake_agent_via_bridge(session, options)
                        session.client = client
                        session.session_id = None
                        log.warning("Agent '%s' woke fresh via bridge (previous context lost)", session.name)
                        if session._log:
                            session._log.info("SESSION_WAKE via bridge (resumed=False, fresh after resume failure)")
                    except Exception:
                        log.exception("Failed to wake '%s' via bridge (even fresh)", session.name)
                        raise
                else:
                    log.exception("Failed to wake '%s' via bridge", session.name)
                    raise
        else:
            # Direct mode: original behavior (no bridge)
            log.debug("Waking '%s' directly (no bridge, resume=%s)", session.name, resume_id)
            try:
                client = ClaudeSDKClient(options=options)
                await client.__aenter__()
                session.client = client
                log.info("Agent '%s' is now awake (resumed=%s)", session.name, resume_id)
                if session._log:
                    session._log.info("SESSION_WAKE (resumed=%s)", bool(resume_id))
            except Exception:
                log.warning("Failed to resume agent '%s' with session_id=%s, retrying fresh", session.name, resume_id)
                options = _make_agent_options(session, resume_id=None)
                client = ClaudeSDKClient(options=options)
                await client.__aenter__()
                session.client = client
                session.session_id = None
                log.warning("Agent '%s' woke with fresh session (previous context lost)", session.name)
                if session._log:
                    session._log.info("SESSION_WAKE (resumed=False, fresh after resume failure)")

        # Post system prompt to Discord on first wake (once per session lifecycle)
        if not session._system_prompt_posted and session.discord_channel_id:
            session._system_prompt_posted = True
            channel = bot.get_channel(session.discord_channel_id)
            if channel and isinstance(channel, TextChannel):
                try:
                    await _post_system_prompt_to_channel(
                        channel,
                        session.system_prompt,
                        is_resume=bool(resume_id),
                        session_id=resume_id,
                    )
                except Exception:
                    log.warning("Failed to post system prompt to Discord for '%s'", session.name, exc_info=True)


def get_master_session() -> AgentSession | None:
    """Get the axi-master session."""
    return agents.get(MASTER_AGENT_NAME)


# --- Guild channel management ---


def _normalize_channel_name(name: str) -> str:
    """Normalize an agent name to a valid Discord channel name."""
    # Discord auto-lowercases and replaces spaces with hyphens
    name = name.lower().replace(" ", "-")
    # Remove characters that Discord doesn't allow in channel names
    name = re.sub(r"[^a-z0-9\-_]", "", name)
    return name[:100]  # Discord channel name limit


def _build_category_overwrites(guild: discord.Guild) -> dict[discord.Object | discord.Member | discord.Role, discord.PermissionOverwrite]:
    """Build permission overwrites for Axi categories: deny @everyone, allow approved users + bot."""
    overwrites: dict[discord.Object | discord.Member | discord.Role, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(
            send_messages=False,
            add_reactions=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
            view_channel=True,
            read_message_history=True,
        ),
        guild.me: discord.PermissionOverwrite(
            send_messages=True,
            add_reactions=True,
            manage_channels=True,
            manage_messages=True,
            manage_threads=True,
            create_public_threads=True,
            create_private_threads=True,
            send_messages_in_threads=True,
            view_channel=True,
            read_message_history=True,
        ),
    }
    for uid in ALLOWED_USER_IDS:
        overwrites[discord.Object(id=uid)] = discord.PermissionOverwrite(
            send_messages=True,
            add_reactions=True,
            create_public_threads=True,
            create_private_threads=True,
            send_messages_in_threads=True,
            view_channel=True,
            read_message_history=True,
        )
    return overwrites


async def ensure_guild_infrastructure() -> tuple[discord.Guild, CategoryChannel, CategoryChannel]:
    """Ensure the guild has Active and Killed categories. Called once during on_ready()."""
    global target_guild, active_category, killed_category

    guild = bot.get_guild(DISCORD_GUILD_ID)
    if guild is None:
        guild = await bot.fetch_guild(DISCORD_GUILD_ID)
    target_guild = guild

    overwrites = _build_category_overwrites(guild)

    # Find or create Active category
    active_cat = None
    killed_cat = None
    for cat in guild.categories:
        if cat.name == ACTIVE_CATEGORY_NAME:
            active_cat = cat
        elif cat.name == KILLED_CATEGORY_NAME:
            killed_cat = cat

    def _overwrites_match(
        existing: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite],
        desired: dict[discord.Object | discord.Member | discord.Role, discord.PermissionOverwrite],
    ) -> bool:
        """Compare overwrites by target ID, ignoring key type differences."""
        a = {getattr(k, "id", k): v for k, v in existing.items()}
        b = {getattr(k, "id", k): v for k, v in desired.items()}
        return a == b

    for name, cat in [
        (ACTIVE_CATEGORY_NAME, active_cat),
        (KILLED_CATEGORY_NAME, killed_cat),
    ]:
        if cat is None:
            cat = await guild.create_category(name, overwrites=overwrites)
            log.info("Created '%s' category", name)
        elif not _overwrites_match(cat.overwrites, overwrites):
            await cat.edit(overwrites=overwrites)
            log.info("Synced permissions on '%s' category", name)
        else:
            log.info("Permissions already current on '%s' category", name)
        if name == ACTIVE_CATEGORY_NAME:
            active_cat = cat
        else:
            killed_cat = cat
    active_category = active_cat
    killed_category = killed_cat

    return guild, active_cat, killed_cat


async def reconstruct_agents_from_channels() -> int:
    """Reconstruct sleeping AgentSession entries from existing Discord channels.

    Scans Active and Killed category channels. For each channel with a valid topic
    (containing cwd), creates a sleeping AgentSession (client=None) and registers
    the channel_to_agent mapping. Skips master channel and already-known agents.
    Returns the number of agents reconstructed.
    """
    reconstructed = 0
    if not active_category:
        return reconstructed

    for cat in [active_category]:
        for ch in cat.text_channels:
            agent_name = ch.name  # channel name IS the agent name (normalized)

            if agent_name == _normalize_channel_name(MASTER_AGENT_NAME):
                channel_to_agent[ch.id] = MASTER_AGENT_NAME
                continue

            if agent_name in agents:
                channel_to_agent[ch.id] = agent_name
                continue

            cwd, session_id = _parse_channel_topic(ch.topic)
            if cwd is None:
                log.debug("No cwd in topic for channel #%s, skipping", agent_name)
                continue

            session = AgentSession(
                name=agent_name,
                client=None,  # sleeping
                cwd=cwd,
                session_id=session_id,
                discord_channel_id=ch.id,
                mcp_servers={
                    "utils": _utils_mcp_server,
                    "schedule": make_schedule_mcp_server(agent_name, SCHEDULES_PATH),
                },
            )
            agents[agent_name] = session
            channel_to_agent[ch.id] = agent_name
            reconstructed += 1
            log.info(
                "Reconstructed agent '%s' from #%s (category=%s, session_id=%s)",
                agent_name, ch.name, cat.name, session_id,
            )

    log.info("Reconstructed %d agent(s) from channels", reconstructed)
    return reconstructed


async def ensure_agent_channel(agent_name: str) -> TextChannel:
    """Find or create a text channel for an agent. Moves from Killed to Active if needed."""
    normalized = _normalize_channel_name(agent_name)

    # Search Active category first
    if active_category:
        for ch in active_category.text_channels:
            if ch.name == normalized:
                channel_to_agent[ch.id] = agent_name
                return ch

    # Search Killed category (agent being respawned)
    if killed_category:
        for ch in killed_category.text_channels:
            if ch.name == normalized:
                await ch.move(category=active_category, beginning=True, sync_permissions=True)
                channel_to_agent[ch.id] = agent_name
                log.info("Moved channel #%s from Killed to Active", normalized)
                return ch

    # Create new channel in Active category
    _bot_creating_channels.add(normalized)
    try:
        channel = await target_guild.create_text_channel(normalized, category=active_category)
    finally:
        _bot_creating_channels.discard(normalized)
    channel_to_agent[channel.id] = agent_name
    log.info("Created channel #%s in Active category", normalized)
    return channel


async def move_channel_to_killed(agent_name: str) -> None:
    """Move an agent's channel from Active to Killed category."""
    if agent_name == MASTER_AGENT_NAME:
        return  # Never archive the master channel

    normalized = _normalize_channel_name(agent_name)
    if active_category:
        for ch in active_category.text_channels:
            if ch.name == normalized:
                try:
                    await ch.move(category=killed_category, end=True, sync_permissions=True)
                    log.info("Moved channel #%s to Killed category", normalized)
                except discord.HTTPException as e:
                    log.warning("Failed to move channel #%s to Killed: %s", normalized, e)
                break


async def get_agent_channel(agent_name: str) -> TextChannel | None:
    """Get the Discord channel for an agent, if it exists."""
    session = agents.get(agent_name)
    if session and session.discord_channel_id:
        ch = bot.get_channel(session.discord_channel_id)
        if ch:
            return ch
    # Fallback: search by name
    normalized = _normalize_channel_name(agent_name)
    if active_category:
        for ch in active_category.text_channels:
            if ch.name == normalized:
                return ch
    return None


async def get_master_channel() -> TextChannel | None:
    """Get the axi-master channel."""
    return await get_agent_channel(MASTER_AGENT_NAME)


# --- Message splitting ---

def split_message(text: str, limit: int = 2000) -> list[str]:
    """Split text into chunks that fit within Discord's message limit.
    Splits on newline boundaries where possible."""
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


async def send_long(channel, text: str) -> None:
    """Send a potentially long message, splitting as needed."""
    chunks = split_message(text.strip())
    log.info("SEND_LONG: channel=%s chunks=%d text=%r", getattr(channel, 'name', '?'), len(chunks), text.strip()[:80])
    for i, chunk in enumerate(chunks):
        if chunk:
            log.info("SEND_LONG: chunk %d/%d len=%d channel=%s", i+1, len(chunks), len(chunk), getattr(channel, 'name', '?'))
            try:
                result = await channel.send(chunk)
                log.info("SEND_LONG: sent msg_id=%s", result.id)
            except discord.NotFound:
                # Channel was deleted — try to recreate it
                agent_name = channel_to_agent.get(channel.id)
                if agent_name:
                    log.warning("Channel for '%s' was deleted, recreating", agent_name)
                    new_ch = await ensure_agent_channel(agent_name)
                    session = agents.get(agent_name)
                    if session:
                        session.discord_channel_id = new_ch.id
                    await new_ch.send(chunk)
                else:
                    raise


async def send_system(channel, text: str) -> None:
    """Send a system-prefixed message."""
    await send_long(channel, f"*System:* {text}")


# --- Rate limit handling ---

def _parse_rate_limit_seconds(text: str) -> int:
    """Parse wait duration from rate limit error text. Returns seconds.

    Tries common patterns from the Claude API/CLI. Falls back to 300s (5 min).
    """
    text_lower = text.lower()

    # "in X seconds/minutes/hours" or "after X seconds/minutes/hours"
    match = re.search(r'(?:in|after)\s+(\d+)\s*(seconds?|minutes?|mins?|hours?|hrs?)', text_lower)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit.startswith('min'):
            return value * 60
        elif unit.startswith('hour') or unit.startswith('hr'):
            return value * 3600
        return value

    # "retry after X" (seconds implied)
    match = re.search(r'retry\s+after\s+(\d+)', text_lower)
    if match:
        return int(match.group(1))

    # "X seconds" anywhere
    match = re.search(r'(\d+)\s*(?:seconds?|secs?)', text_lower)
    if match:
        return int(match.group(1))

    # "X minutes" anywhere
    match = re.search(r'(\d+)\s*(?:minutes?|mins?)', text_lower)
    if match:
        return int(match.group(1)) * 60

    # Default: 5 minutes
    return 300


def _format_time_remaining(seconds: int) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _is_rate_limited() -> bool:
    """Check if we're currently rate limited."""
    global _rate_limited_until
    if _rate_limited_until is None:
        return False
    if datetime.now(timezone.utc) >= _rate_limited_until:
        _rate_limited_until = None
        return False
    return True


def _rate_limit_remaining_seconds() -> int:
    """Get remaining rate limit time in seconds."""
    if _rate_limited_until is None:
        return 0
    remaining = (_rate_limited_until - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(remaining))


def _record_session_usage(agent_name: str, msg: ResultMessage) -> None:
    sid = msg.session_id
    if not sid:
        return
    now = datetime.now(timezone.utc)
    if sid not in _session_usage:
        _session_usage[sid] = SessionUsage(agent_name=agent_name, first_query=now)
    entry = _session_usage[sid]
    entry.queries += 1
    entry.total_cost_usd += msg.total_cost_usd or 0.0
    entry.total_turns += msg.num_turns or 0
    entry.total_duration_ms += msg.duration_ms or 0
    entry.last_query = now


def _update_rate_limit_quota(data: dict) -> None:
    global _rate_limit_quota
    info = data.get("rate_limit_info", {})
    resets_at_unix = info.get("resetsAt")
    if resets_at_unix is None:
        return
    _rate_limit_quota = RateLimitQuota(
        status=info.get("status", "unknown"),
        resets_at=datetime.fromtimestamp(resets_at_unix, tz=timezone.utc),
        rate_limit_type=info.get("rateLimitType", "unknown"),
        utilization=info.get("utilization"),
    )


async def _handle_rate_limit(error_text: str, session: AgentSession, channel) -> None:
    """Handle a rate limit error: set global state, notify user, schedule retry."""
    global _rate_limited_until, _rate_limit_retry_task

    wait_seconds = _parse_rate_limit_seconds(error_text)
    new_limit = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
    already_limited = _is_rate_limited()

    # Update expiry (extend if needed)
    if _rate_limited_until is None or new_limit > _rate_limited_until:
        _rate_limited_until = new_limit

    log.warning("Rate limited — waiting %ds (until %s)", wait_seconds, _rate_limited_until.isoformat())
    log.debug("Rate limit set: duration=%ds, already_limited=%s, agent='%s'", wait_seconds, already_limited, session.name)

    # Only notify user if this is a new rate limit (avoid spam during queue processing)
    if not already_limited:
        remaining = _format_time_remaining(wait_seconds)
        await send_system(
            channel,
            f"⚠️ **Rate limited.** Usage will be available again in ~**{remaining}**. "
            f"Messages sent during this time will be queued and processed automatically.",
        )

    # Schedule/reschedule retry worker if not already running
    if _rate_limit_retry_task is None or _rate_limit_retry_task.done():
        _rate_limit_retry_task = asyncio.create_task(_rate_limit_retry_worker())


async def _rate_limit_retry_worker() -> None:
    """Background task: waits for rate limit to expire, then drains all agent queues."""
    global _rate_limited_until
    try:
        while True:
            # Wait for rate limit to expire (poll every 30s to pick up extensions)
            while _rate_limited_until and datetime.now(timezone.utc) < _rate_limited_until:
                wait = (_rate_limited_until - datetime.now(timezone.utc)).total_seconds()
                await asyncio.sleep(max(1, min(wait + 1, 30)))

            _rate_limited_until = None
            log.info("Rate limit expired — processing queued messages for all agents")

            # Check if any agent has queued messages
            has_queued = any(not s.message_queue.empty() for s in agents.values())
            if not has_queued:
                return

            # Process queued messages for each agent
            re_limited = False
            for name, session in list(agents.items()):
                if _is_rate_limited():
                    re_limited = True
                    break
                if session.message_queue.empty():
                    continue
                channel = bot.get_channel(session.discord_channel_id) if session.discord_channel_id else None
                if channel:
                    await send_system(channel, "✅ Rate limit expired — processing queued messages…")
                    await _process_message_queue(session)

            if not re_limited:
                return  # All done — exit worker
            # Re-rate-limited during processing — loop back and wait again
    except asyncio.CancelledError:
        return
    except Exception:
        log.exception("Error in rate limit retry worker")


# --- Streaming response ---

async def _receive_response_safe(session: AgentSession):
    """Wrapper around receive_messages() that handles unknown message types.

    Yields parsed SDK messages until a ResultMessage is received (one per query).
    Unknown message types are logged as warnings and skipped — never silently dropped.
    """
    from claude_agent_sdk._internal.message_parser import parse_message

    async for data in session.client._query.receive_messages():
        try:
            parsed = parse_message(data)
        except MessageParseError:
            msg_type = data.get("type", "?")
            if msg_type == "rate_limit_event":
                log.info("Rate limit event for '%s': %s", session.name, data)
                if session._log:
                    session._log.info("RATE_LIMIT_EVENT: %s", json.dumps(data)[:500])
                _update_rate_limit_quota(data)
            else:
                log.warning("Unknown SDK message type from '%s': type=%s data=%s",
                            session.name, msg_type, json.dumps(data)[:500])
                if session._log:
                    session._log.warning("UNKNOWN_MSG: type=%s data=%s", msg_type, json.dumps(data)[:500])
            continue
        yield parsed
        if isinstance(parsed, ResultMessage):
            return


def _update_activity(session: AgentSession, event: dict) -> None:
    """Update the agent's activity state from a raw Anthropic stream event."""
    activity = session.activity
    activity.last_event = datetime.now(timezone.utc)
    event_type = event.get("type", "")

    if event_type == "content_block_start":
        block = event.get("content_block", {})
        block_type = block.get("type", "")

        if block_type == "tool_use":
            activity.phase = "tool_use"
            activity.tool_name = block.get("name")
            activity.tool_input_preview = ""
        elif block_type == "thinking":
            activity.phase = "thinking"
            activity.tool_name = None
            activity.tool_input_preview = ""
        elif block_type == "text":
            activity.phase = "writing"
            activity.tool_name = None
            activity.tool_input_preview = ""
            activity.text_chars = 0

    elif event_type == "content_block_delta":
        delta = event.get("delta", {})
        delta_type = delta.get("type", "")

        if delta_type == "thinking_delta":
            activity.phase = "thinking"
        elif delta_type == "text_delta":
            activity.phase = "writing"
            activity.text_chars += len(delta.get("text", ""))
        elif delta_type == "input_json_delta":
            # Accumulate tool input preview (capped at 200 chars)
            if len(activity.tool_input_preview) < 200:
                activity.tool_input_preview += delta.get("partial_json", "")
                activity.tool_input_preview = activity.tool_input_preview[:200]

    elif event_type == "content_block_stop":
        if activity.phase == "tool_use":
            activity.phase = "waiting"  # Tool submitted, waiting for execution/result

    elif event_type == "message_start":
        activity.turn_count += 1

    elif event_type == "message_delta":
        stop_reason = event.get("delta", {}).get("stop_reason")
        if stop_reason == "end_turn":
            activity.phase = "idle"
            activity.tool_name = None
        elif stop_reason == "tool_use":
            activity.phase = "waiting"  # Tools will execute, then new turn starts


def _extract_tool_preview(tool_name: str, raw_json: str) -> str | None:
    """Try to extract a useful preview from partial tool input JSON."""
    try:
        data = json.loads(raw_json)
        if tool_name == "Bash":
            return data.get("command", "")[:100]
        elif tool_name in ("Read", "Write", "Edit"):
            return data.get("file_path", "")[:100]
        elif tool_name == "Grep":
            return f'grep "{data.get("pattern", "")}" {data.get("path", ".")}'[:100]
        elif tool_name == "Glob":
            return f'{data.get("pattern", "")}'[:100]
    except (json.JSONDecodeError, TypeError):
        # Partial JSON — try simple extraction
        if tool_name == "Bash":
            match = re.search(r'"command"\s*:\s*"([^"]*)', raw_json)
            if match:
                return match.group(1)[:100]
        elif tool_name in ("Read", "Write", "Edit"):
            match = re.search(r'"file_path"\s*:\s*"([^"]*)', raw_json)
            if match:
                return match.group(1)[:100]
    return None


async def stream_response_to_channel(session: AgentSession, channel) -> str | None:
    """Stream Claude's response from a specific agent session to a Discord channel.

    Message flow:
      1. StreamEvents arrive in real-time as Claude generates tokens.
         - content_block_delta/text_delta → buffer text for Discord
         - message_delta with stop_reason "end_turn" → Claude is done, stop typing
      2. AssistantMessage arrives after each API round (may include tool calls).
         - Flush any buffered text, log content to agent log.
         - On error (rate_limit, etc.) → handle specially.
      3. ResultMessage arrives once per query (cost/session bookkeeping).
         - Extract session_id, stop typing if still active.
         - Generator terminates here — loop exits.

    The typing indicator is stopped as soon as we detect end_turn in the stream
    events (step 1), NOT when ResultMessage arrives (step 3). This prevents
    the typing indicator from lingering during SDK bookkeeping.
    """

    log.debug("Streaming response for '%s'", session.name)

    text_buffer = ""
    hit_rate_limit = False
    hit_transient_error: str | None = None
    typing_stopped = False

    _flush_count = 0

    async def flush_text(text: str, reason: str = "?") -> None:
        nonlocal _flush_count
        if not text.strip():
            return
        _flush_count += 1
        log.info("FLUSH[%s] #%d reason=%s len=%d text=%r",
                 session.name, _flush_count, reason, len(text.strip()), text.strip()[:120])
        await send_long(channel, text.lstrip())

    def stop_typing() -> None:
        nonlocal typing_stopped
        if not typing_stopped and _typing_ctx and _typing_ctx.task:
            _typing_ctx.task.cancel()
            typing_stopped = True

    _msg_seq = 0

    async with channel.typing() as _typing_ctx:
        async for msg in _receive_response_safe(session):
            _msg_seq += 1
            if session._log:
                session._log.debug("MSG_SEQ[%d] type=%s buf_len=%d", _msg_seq, type(msg).__name__, len(text_buffer))

            # Drain and send any stderr messages first
            for stderr_msg in drain_stderr(session):
                stderr_text = stderr_msg.strip()
                if stderr_text:
                    for part in split_message(f"```\n{stderr_text}\n```"):
                        await channel.send(part)

            if isinstance(msg, StreamEvent):
                event = msg.event
                event_type = event.get("type", "")

                # Update activity state for /status command
                _update_activity(session, event)

                # Log all stream events to agent log
                if session._log:
                    if event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        delta_type = delta.get("type", "")
                        # Don't log full text/thinking deltas (too noisy), just note them
                        if delta_type not in ("text_delta", "thinking_delta", "signature_delta"):
                            session._log.debug("STREAM: %s delta=%s", event_type, delta_type)
                    elif event_type in ("content_block_start", "content_block_stop"):
                        block = event.get("content_block", {})
                        session._log.debug("STREAM: %s type=%s index=%s",
                                           event_type, block.get("type", "?"), event.get("index"))
                    elif event_type == "message_start":
                        msg_data = event.get("message", {})
                        session._log.debug("STREAM: message_start model=%s", msg_data.get("model", "?"))
                    elif event_type == "message_delta":
                        delta = event.get("delta", {})
                        session._log.debug("STREAM: message_delta stop_reason=%s", delta.get("stop_reason"))
                    elif event_type == "message_stop":
                        session._log.debug("STREAM: message_stop")
                    else:
                        session._log.debug("STREAM: %s %s", event_type, json.dumps(event)[:300])

                if hit_rate_limit:
                    continue

                # Buffer text deltas for Discord
                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_buffer += delta.get("text", "")

                # Detect end_turn — Claude is done generating, stop typing immediately.
                # This fires BEFORE the AssistantMessage/ResultMessage, so the typing
                # indicator stops as soon as the API signals completion.
                elif event_type == "message_delta":
                    stop_reason = event.get("delta", {}).get("stop_reason")
                    if stop_reason == "end_turn":
                        await flush_text(text_buffer, "end_turn")
                        text_buffer = ""
                        stop_typing()

            elif isinstance(msg, AssistantMessage):
                if msg.error in ("rate_limit", "billing_error"):
                    error_text = text_buffer
                    for block in (msg.content or []):
                        if hasattr(block, "text"):
                            error_text += " " + block.text
                    log.warning("Agent '%s' hit %s error: %s", session.name, msg.error, error_text[:200])
                    stop_typing()
                    await _handle_rate_limit(error_text, session, channel)
                    text_buffer = ""
                    hit_rate_limit = True
                elif msg.error:
                    error_text = text_buffer
                    for block in (msg.content or []):
                        if hasattr(block, "text"):
                            error_text += " " + block.text
                    log.warning("Agent '%s' hit API error (%s): %s", session.name, msg.error, error_text[:200])
                    stop_typing()
                    await flush_text(text_buffer, "assistant_error")
                    text_buffer = ""
                    hit_transient_error = msg.error
                else:
                    # Normal response — flush any remaining text and stop typing.
                    # Usually end_turn already stopped typing, but this is a safety net
                    # for responses that end with tool_use (no end_turn event).
                    await flush_text(text_buffer, "assistant_msg")
                    text_buffer = ""
                    stop_typing()

                # Log assistant response content to per-agent log
                if session._log:
                    for block in (msg.content or []):
                        if hasattr(block, "text"):
                            session._log.info("ASSISTANT: %s", block.text[:2000])
                        elif hasattr(block, "type") and block.type == "tool_use":
                            session._log.info("TOOL_USE: %s(%s)", block.name,
                                              json.dumps(block.input)[:500] if hasattr(block, "input") else "")

            elif isinstance(msg, ResultMessage):
                stop_typing()
                await _set_session_id(session, msg)
                if not hit_rate_limit:
                    await flush_text(text_buffer, "result_msg")
                text_buffer = ""
                if session._log:
                    session._log.info("RESULT: cost=$%s turns=%d duration=%dms session=%s",
                                      msg.total_cost_usd, msg.num_turns, msg.duration_ms, msg.session_id)
                _record_session_usage(session.name, msg)

            elif isinstance(msg, SystemMessage):
                if session._log:
                    session._log.debug("SYSTEM_MSG: subtype=%s data=%s",
                                       msg.subtype, json.dumps(msg.data)[:500])
                if msg.subtype == "compact_boundary":
                    metadata = msg.data.get("compact_metadata", {})
                    trigger = metadata.get("trigger", "unknown")
                    pre_tokens = metadata.get("pre_tokens")
                    log.info("Agent '%s' context compacted: trigger=%s pre_tokens=%s",
                             session.name, trigger, pre_tokens)
                    token_info = f" ({pre_tokens:,} tokens)" if pre_tokens else ""
                    await channel.send(f"🔄 Context compacted{token_info}")

            else:
                # Log any other parsed message types (UserMessage, etc.)
                if session._log:
                    session._log.debug("OTHER_MSG: %s", type(msg).__name__)

            # When buffer is large enough, flush it mid-turn
            if not hit_rate_limit and len(text_buffer) >= 1800:
                split_at = text_buffer.rfind("\n", 0, 1800)
                if split_at == -1:
                    split_at = 1800
                to_send = text_buffer[:split_at]
                text_buffer = text_buffer[split_at:].lstrip("\n")
                await flush_text(to_send, "mid_turn_split")

    # Flush any remaining stderr
    for stderr_msg in drain_stderr(session):
        stderr_text = stderr_msg.strip()
        if stderr_text:
            for part in split_message(f"```\n{stderr_text}\n```"):
                await channel.send(part)

    if hit_rate_limit:
        return None

    if hit_transient_error:
        return hit_transient_error

    await flush_text(text_buffer, "post_loop")

    log.debug("Stream complete for '%s'", session.name)

    if SHOW_AWAITING_INPUT:
        await send_system(channel, "Bot has finished responding and is awaiting input.")

    return None


async def _stream_with_retry(session: AgentSession, channel) -> bool:
    """Stream response with retry on transient API errors.

    Returns True on success, False if all retries exhausted.
    """
    error = await stream_response_to_channel(session, channel)
    if error is None:
        return True

    for attempt in range(2, API_ERROR_MAX_RETRIES + 1):
        delay = API_ERROR_BASE_DELAY * (2 ** (attempt - 2))
        log.warning(
            "Agent '%s' transient error '%s', retrying in %ds (attempt %d/%d)",
            session.name, error, delay, attempt, API_ERROR_MAX_RETRIES,
        )
        await channel.send(
            f"\u26a0\ufe0f API error, retrying in {delay}s... (attempt {attempt}/{API_ERROR_MAX_RETRIES})"
        )
        await asyncio.sleep(delay)

        try:
            await session.client.query(_as_stream("Continue from where you left off."))
        except Exception:
            log.exception("Agent '%s' retry query failed", session.name)
            continue

        error = await stream_response_to_channel(session, channel)
        if error is None:
            return True

    log.error(
        "Agent '%s' transient error persisted after %d retries",
        session.name, API_ERROR_MAX_RETRIES,
    )
    await channel.send(
        f"\u274c API error persisted after {API_ERROR_MAX_RETRIES} retries. Try again later."
    )
    return False


async def _handle_query_timeout(session: AgentSession, channel) -> None:
    """Handle a query timeout. Try interrupt first, then kill and resume."""
    log.warning("Query timeout for agent '%s', attempting interrupt", session.name)

    # Step 1: Try graceful interrupt
    try:
        # interrupt cancels current query
        await session.client.interrupt()
        async with asyncio.timeout(INTERRUPT_TIMEOUT):
            async for msg in _receive_response_safe(session):
                if isinstance(msg, ResultMessage):
                    await _set_session_id(session, msg)
                    break
        session.last_activity = datetime.now(timezone.utc)
        await send_system(
            channel,
            f"Agent **{session.name}** timed out and was interrupted. Context preserved.",
        )
        return
    except (TimeoutError, Exception):
        log.warning("Interrupt failed for agent '%s', killing and resuming session", session.name)

    # Step 2: Kill and create sleeping session — will wake on next message
    old_session_id = session.session_id
    old_name = session.name
    old_cwd = session.cwd
    old_prompt = session.system_prompt
    old_channel_id = session.discord_channel_id
    old_mcp = session.mcp_servers
    await end_session(old_name)

    new_session = AgentSession(
        name=old_name,
        cwd=old_cwd,
        system_prompt=old_prompt,
        client=None,
        session_id=old_session_id,
        discord_channel_id=old_channel_id,
        mcp_servers=old_mcp,
    )
    agents[old_name] = new_session

    if old_session_id:
        await send_system(channel, f"Agent **{old_name}** timed out and was recovered (sleeping). Context preserved.")
    else:
        await send_system(channel, f"Agent **{old_name}** timed out and was reset (sleeping). Context lost.")


# --- Agent spawning ---


async def reclaim_agent_name(name: str) -> None:
    """If an agent with *name* already exists, kill it silently to free the name."""
    if name not in agents:
        return
    log.info("Reclaiming agent name '%s' — terminating existing session", name)
    session = agents.get(name)
    await sleep_agent(session)
    agents.pop(name, None)
    channel = await get_agent_channel(name)
    if channel:
        await send_system(channel, f"Recycled previous **{name}** session for new scheduled run.")


async def spawn_agent(name: str, cwd: str, initial_prompt: str, resume: str | None = None) -> None:
    """Spawn a new agent session and run its initial prompt in the background."""
    # Auto-create cwd if it doesn't exist
    if not os.path.isdir(cwd):
        os.makedirs(cwd, exist_ok=True)
        log.info("Auto-created working directory: %s", cwd)

    channel = await ensure_agent_channel(name)

    if resume:
        await send_system(channel, f"Resuming agent **{name}** (session `{resume[:8]}…`) in `{cwd}`...")
    else:
        await send_system(channel, f"Spawning agent **{name}** in `{cwd}`...")

    # Create agent as sleeping — _run_initial_prompt will wake it if needed
    session = AgentSession(
        name=name,
        cwd=cwd,
        system_prompt=_make_spawned_agent_system_prompt(cwd),
        client=None,
        session_id=resume,
        discord_channel_id=channel.id,
        mcp_servers={
            "utils": _utils_mcp_server,
            "schedule": make_schedule_mcp_server(name, SCHEDULES_PATH),
        },
    )
    agents[name] = session
    channel_to_agent[channel.id] = name
    log.info("Agent '%s' registered (sleeping, cwd=%s, resume=%s)", name, cwd, resume)

    # Set initial topic with cwd (session_id will be added when agent sleeps)
    desired_topic = _format_channel_topic(cwd, resume)
    if channel.topic != desired_topic:
        log.info("Updating topic on #%s: %r -> %r", channel.name, channel.topic, desired_topic)
        await channel.edit(topic=desired_topic)

    if not initial_prompt:
        await send_system(channel, f"Agent **{name}** is ready (sleeping).")
        return

    asyncio.create_task(_run_initial_prompt(session, initial_prompt, channel))


async def send_prompt_to_agent(agent_name: str, prompt: str) -> None:
    """Send a prompt to an existing agent session in the background.

    Used by the scheduler when a 'session' field maps to an already-running agent.
    Queues the prompt just like a user message would, streaming the response to the
    agent's Discord channel.
    """
    session = agents.get(agent_name)
    if session is None:
        log.warning("send_prompt_to_agent: agent '%s' not found", agent_name)
        return

    channel = await get_agent_channel(agent_name)
    if channel is None:
        log.warning("send_prompt_to_agent: no channel for agent '%s'", agent_name)
        return

    asyncio.create_task(_run_initial_prompt(session, prompt, channel))


async def _run_initial_prompt(session: AgentSession, prompt: str | list, channel: TextChannel) -> None:
    """Run the initial prompt for a spawned agent. Notifies when done."""
    try:
        timed_out = False
        async with session.query_lock:
            # Wake agent if sleeping
            if session.client is None:
                try:
                    await wake_agent(session)
                except ConcurrencyLimitError:
                    log.info("Concurrency limit hit for '%s' initial prompt — queuing", session.name)
                    await session.message_queue.put((prompt, channel, None))
                    awake = _count_awake_agents()
                    await send_system(
                        channel,
                        f"⏳ All {awake} agent slots are busy. "
                        f"Initial prompt queued — will run when a slot opens.",
                    )
                    return
                except Exception:
                    log.exception("Failed to wake agent '%s' for initial prompt", session.name)
                    await send_system(channel, f"Failed to wake agent **{session.name}**.")
                    return

            session.last_activity = datetime.now(timezone.utc)
            drain_stderr(session)
            drain_sdk_buffer(session)

            # Show the initial prompt in Discord so the user sees what was sent
            if isinstance(prompt, str):
                prompt_preview = prompt[:1900]
            else:
                prompt_preview = str(prompt)[:1900]
            await channel.send(f"*System:* 📝 **Initial prompt:**\n{prompt_preview}")

            if session._log:
                session._log.info("PROMPT: %s", _content_summary(prompt))
            log.debug("Running initial prompt for '%s': %s", session.name, _content_summary(prompt))
            session.activity = ActivityState(phase="starting", query_started=datetime.now(timezone.utc))
            try:
                async with asyncio.timeout(QUERY_TIMEOUT):
                    await session.client.query(_as_stream(prompt))
                    await _stream_with_retry(session, channel)
                    session.last_activity = datetime.now(timezone.utc)
            except TimeoutError:
                timed_out = True
                await _handle_query_timeout(session, channel)
            finally:
                session.activity = ActivityState(phase="idle")

        log.debug("Initial prompt completed for '%s'", session.name)

        if not timed_out:
            await send_system(channel, f"Agent **{session.name}** finished initial task.")

    except Exception:
        log.exception("Error running initial prompt for agent '%s'", session.name)
        await send_system(channel, f"Agent **{session.name}** encountered an error during initial task.")

    await _process_message_queue(session)

    # Sleep agent after completing initial prompt and draining the queue
    try:
        await sleep_agent(session)
    except Exception:
        log.exception("Error sleeping agent '%s' after initial prompt", session.name)


async def _process_message_queue(session: AgentSession) -> None:
    """Process any queued messages for an agent after the current query finishes."""
    while not session.message_queue.empty():
        if shutdown_coordinator and shutdown_coordinator.requested:
            log.info("Shutdown requested — not processing further queued messages for '%s'", session.name)
            break
        if _is_rate_limited():
            log.info("Rate limited — pausing queue processing for '%s' (%d messages pending)",
                     session.name, session.message_queue.qsize())
            break  # Retry worker will resume processing when rate limit expires
        content, channel, orig_message = session.message_queue.get_nowait()

        remaining = session.message_queue.qsize()
        log.debug("Processing queued message for '%s' (%d remaining)", session.name, remaining)
        if session._log:
            session._log.info("QUEUED_MSG: %s", _content_summary(content))
        await _remove_reaction(orig_message, "📨")
        # Show inline preview of the queued message being processed
        preview = _content_summary(content)
        remaining_str = f" ({remaining} more in queue)" if remaining > 0 else ""
        await send_system(channel, f"Processing queued message{remaining_str}:\n> {preview}")

        async with session.query_lock:
            # Wake agent if it was sleeping (e.g. after timeout recovery)
            if session.client is None:
                try:
                    await wake_agent(session)
                except Exception:
                    log.exception("Failed to wake agent '%s' for queued message", session.name)
                    await _add_reaction(orig_message, "❌")
                    await send_system(channel, f"Failed to wake agent **{session.name}** — dropping queued message.")
                    # Clear remaining queue
                    while not session.message_queue.empty():
                        _, ch, dropped_msg = session.message_queue.get_nowait()
                        await _remove_reaction(dropped_msg, "📨")
                        await _add_reaction(dropped_msg, "❌")
                        await send_system(ch, f"Failed to wake agent **{session.name}** — dropping queued message.")
                    return

            session.last_activity = datetime.now(timezone.utc)
            session.last_idle_notified = None
            session.idle_reminder_count = 0
            drain_stderr(session)
            drain_sdk_buffer(session)
            session.activity = ActivityState(phase="starting", query_started=datetime.now(timezone.utc))
            try:
                async with asyncio.timeout(QUERY_TIMEOUT):
                    await session.client.query(_as_stream(content))
                    await _stream_with_retry(session, channel)
                await _add_reaction(orig_message, "✅")
            except TimeoutError:
                await _add_reaction(orig_message, "⏳")
                await _handle_query_timeout(session, channel)
            except Exception:
                log.exception("Error querying agent '%s' (queued message)", session.name)
                await _add_reaction(orig_message, "❌")
                await send_system(
                    channel,
                    f"Error processing queued message for **{session.name}**.",
                )
            finally:
                session.activity = ActivityState(phase="idle")



# --- Graceful shutdown (delegated to shutdown.py) ---


# --- Bridge connection and reconnection ---


async def _connect_bridge() -> None:
    """Connect to the agent bridge (or start a new one).

    If the bridge has running agents from a previous bot.py instance,
    schedules reconnect+drain tasks to resume them.
    """
    global bridge_conn

    try:
        bridge_conn = await ensure_bridge(BRIDGE_SOCKET_PATH, timeout=10.0)
        log.info("Bridge connection established")
    except Exception:
        log.exception("Failed to connect to bridge — agents will use direct subprocess mode")
        bridge_conn = None
        return

    # List running agents in the bridge
    try:
        result = await bridge_conn.send_command("list")
        bridge_agents = result.agents or {}
        log.info("Bridge reports %d agent(s): %s", len(bridge_agents), list(bridge_agents.keys()))
    except Exception:
        log.exception("Failed to list bridge agents")
        return

    if not bridge_agents:
        return

    # For each running agent that we've reconstructed, schedule reconnect
    for agent_name, info in bridge_agents.items():
        session = agents.get(agent_name)
        if session is None:
            log.warning("Bridge has agent '%s' but no matching session — killing", agent_name)
            try:
                await bridge_conn.send_command("kill", name=agent_name)
            except Exception:
                log.exception("Failed to kill orphan bridge agent '%s'", agent_name)
            continue

        status = info.get("status", "unknown")
        buffered = info.get("buffered_msgs", 0)
        log.info(
            "Reconnecting agent '%s' (status=%s, buffered=%d)",
            agent_name, status, buffered,
        )

        # Mark as reconnecting to prevent on_message from waking a new CLI
        session._reconnecting = True
        asyncio.create_task(_reconnect_and_drain(session, info))


async def _reconnect_and_drain(session: AgentSession, bridge_info: dict) -> None:
    """Reconnect a single agent to the bridge and drain any buffered output.

    This runs as a background task. It:
    1. Acquires the query_lock (blocks new queries)
    2. Creates BridgeTransport in reconnecting mode (fakes initialize)
    3. Subscribes to the bridge (triggers buffer replay + idle status)
    4. Creates a ClaudeSDKClient on top of the transport
    5. If the CLI is running and NOT idle (mid-task), drains buffered output
       and sets _bridge_busy to prevent auto-sleep
    6. If the CLI is running and idle, just leaves it awake — no drain needed
    7. Clears the _reconnecting flag
    8. Processes any queued messages
    """
    try:
        async with session.query_lock:
            if bridge_conn is None or not bridge_conn.is_alive:
                log.warning("Bridge connection lost during reconnect of '%s'", session.name)
                session._reconnecting = False
                return

            # Create transport in reconnecting mode
            transport = BridgeTransport(
                session.name, bridge_conn,
                reconnecting=True,
                stderr_callback=make_stderr_callback(session),
            )
            await transport.connect()

            # Subscribe to get buffered output + idle status
            sub_result = await transport.subscribe()
            replayed = sub_result.replayed or 0
            cli_status = sub_result.status or "unknown"
            cli_idle = sub_result.idle if sub_result.idle is not None else True
            log.info(
                "Subscribed to '%s' (replayed=%d, status=%s, idle=%s)",
                session.name, replayed, cli_status, cli_idle,
            )

            # Build minimal options for reconnecting (no model/thinking needed — CLI already running)
            options = ClaudeAgentOptions(
                can_use_tool=make_cwd_permission_callback(session.cwd),
                mcp_servers=session.mcp_servers or {},
                permission_mode="default",
                cwd=session.cwd,
                include_partial_messages=True,
                stderr=make_stderr_callback(session),
            )

            # Create SDK client with our bridge transport
            client = ClaudeSDKClient(options=options, transport=transport)
            await client.__aenter__()
            session.client = client
            session.last_activity = datetime.now(timezone.utc)

            if session._log:
                session._log.info(
                    "SESSION_RECONNECT via bridge (replayed=%d, idle=%s)", replayed, cli_idle,
                )

            # If the CLI already exited while we were down, note it
            if cli_status == "exited":
                log.info("Agent '%s' CLI exited while we were down", session.name)
                session._reconnecting = False
                # Let the transport's read_messages detect the exit naturally
                # The buffered exit message was replayed, so stream_response will handle it

            # Clear reconnecting flag — agent is now live
            session._reconnecting = False

            if cli_status == "running" and not cli_idle:
                # Agent is mid-task (bridge saw stdin more recently than stdout).
                # Prevent auto-sleep from killing the running CLI process.
                session._bridge_busy = True
                channel = await get_agent_channel(session.name)
                if replayed > 0 and channel:
                    # There's buffered output to drain — stream it to Discord
                    await send_system(channel, "*(reconnected after restart — resuming output)*")
                    try:
                        async with asyncio.timeout(QUERY_TIMEOUT):
                            await stream_response_to_channel(session, channel)
                    except TimeoutError:
                        log.warning("Drain timeout for '%s' — continuing", session.name)
                    except Exception:
                        log.exception("Error draining buffered output for '%s'", session.name)
                    session._bridge_busy = False
                    session.last_activity = datetime.now(timezone.utc)
                elif channel:
                    await send_system(channel, "*(reconnected after restart — task still running)*")
                log.info(
                    "Agent '%s' reconnected mid-task (idle=False, replayed=%d, bridge_busy=%s)",
                    session.name, replayed, session._bridge_busy,
                )
            elif cli_status == "running":
                # Agent is idle (between turns) — no drain needed, no special protection
                channel = await get_agent_channel(session.name)
                if channel:
                    await send_system(channel, "*(reconnected after restart)*")
                log.info("Agent '%s' reconnected idle (between turns)", session.name)

            log.info("Reconnect complete for '%s'", session.name)

    except Exception:
        log.exception("Failed to reconnect agent '%s'", session.name)
        session._reconnecting = False

    # Process any messages that were queued during reconnect
    await _process_message_queue(session)


def _init_shutdown_coordinator() -> None:
    """Wire up the ShutdownCoordinator with real bot callbacks.

    Called once from on_ready after all helpers are defined.
    In bridge mode, uses exit_for_restart (agents keep running in bridge)
    instead of kill_supervisor (which kills everything).
    """
    global shutdown_coordinator

    async def _notify_agent_channel(agent_name: str, message: str) -> None:
        channel = await get_agent_channel(agent_name)
        if channel:
            await send_system(channel, message)

    async def _send_goodbye() -> None:
        master_ch = await get_master_channel()
        if master_ch:
            await master_ch.send("*System:* Shutting down — see you soon!")

    use_bridge = bridge_conn is not None and bridge_conn.is_alive
    shutdown_coordinator = ShutdownCoordinator(
        agents=agents,
        sleep_fn=sleep_agent,
        close_bot_fn=bot.close,
        kill_fn=exit_for_restart if use_bridge else kill_supervisor,
        notify_fn=_notify_agent_channel,
        goodbye_fn=_send_goodbye,
        bridge_mode=use_bridge,
    )


# --- Message handler ---

@bot.event
async def on_message(message):
    if message.author.id == bot.user.id:
        return
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return  # Ignore system events (pins, boosts, joins, etc.)
    if message.author.bot and message.author.id not in ALLOWED_USER_IDS:
        return

    # DM messages — redirect to guild
    if message.channel.type == ChannelType.private:
        if message.author.id not in ALLOWED_USER_IDS:
            return
        master_session = get_master_session()
        if master_session and master_session.discord_channel_id:
            await message.channel.send(
                f"*System:* Please use <#{master_session.discord_channel_id}> in the server instead."
            )
        else:
            await message.channel.send("*System:* Please use the server channels instead.")
        return

    # Guild messages — only process in our target guild
    if message.guild is None or message.guild.id != DISCORD_GUILD_ID:
        return

    if message.author.id not in ALLOWED_USER_IDS:
        return

    # Extract text + image content from the message (may be str or list of blocks)
    content = await _extract_message_content(message)
    log.info("Message from %s in #%s: %s", message.author, message.channel.name, _content_summary(content))

    if shutdown_coordinator and shutdown_coordinator.requested:
        await send_system(message.channel, "Bot is restarting — not accepting new messages.")
        return

    # Look up which agent owns this channel
    agent_name = channel_to_agent.get(message.channel.id)
    if agent_name is None:
        return  # Untracked channel, ignore

    session = agents.get(agent_name)
    if session is None:
        # Check if this channel is in the Killed category
        if killed_category and hasattr(message.channel, "category_id") and message.channel.category_id == killed_category.id:
            await send_system(message.channel, "This agent has been killed. Use `/spawn` to create a new one.")
            return
        return  # Truly unknown channel

    # Block interaction with killed agents
    if killed_category and hasattr(message.channel, "category_id") and message.channel.category_id == killed_category.id:
        await send_system(message.channel, "This agent has been killed. Use `/spawn` to create a new one.")
        return

    # During bridge reconnect, queue messages instead of waking a new CLI
    if session._reconnecting:
        await session.message_queue.put((message.content, message.channel, message))
        position = session.message_queue.qsize()
        log.debug("Agent '%s' reconnecting after restart, queuing message (queue_size=%d)", agent_name, position)
        await _add_reaction(message, "📨")
        await send_system(
            message.channel,
            f"Agent **{agent_name}** is reconnecting after restart — message queued (position {position}).",
        )
        return

    log.debug("Routing message to agent '%s' (locked=%s, client=%s)", agent_name, session.query_lock.locked(), session.client is not None)

    # Queue messages while rate limited (don't waste API calls)
    if _is_rate_limited() and not session.query_lock.locked():
        await session.message_queue.put((content, message.channel, message))
        position = session.message_queue.qsize()
        log.debug("Rate-limited, queuing for '%s' (queue_size=%d)", agent_name, position)
        await _add_reaction(message, "📨")
        remaining = _format_time_remaining(_rate_limit_remaining_seconds())
        await send_system(
            message.channel,
            f"⏳ Currently rate limited — ~**{remaining}** remaining. "
            f"Message queued (position {position}) and will be sent automatically.",
        )
        return

    if session.query_lock.locked():
        await session.message_queue.put((content, message.channel, message))
        position = session.message_queue.qsize()
        log.debug("Agent '%s' busy, queuing message (queue_size=%d)", agent_name, position)
        await _add_reaction(message, "📨")
        await send_system(
            message.channel,
            f"Agent **{agent_name}** is busy — message queued (position {position}). "
            f"Will process after current turn.",
        )
        return

    async with session.query_lock:
        # Wake agent if sleeping
        if session.client is None:
            log.debug("Waking agent '%s' for user message", agent_name)
            try:
                await wake_agent(session)
            except ConcurrencyLimitError:
                log.info("Concurrency limit hit for '%s' — queuing message", agent_name)
                await session.message_queue.put((content, message.channel, message))
                position = session.message_queue.qsize()
                awake = _count_awake_agents()
                await _add_reaction(message, "📨")
                await send_system(
                    message.channel,
                    f"⏳ All {awake} agent slots are busy. "
                    f"Message queued (position {position}) — will process when a slot opens.",
                )
                return
            except Exception:
                log.exception("Failed to wake agent '%s'", agent_name)
                await _add_reaction(message, "❌")
                await send_system(
                    message.channel,
                    f"Failed to wake agent **{agent_name}**. Try `/kill-agent {agent_name}` and respawn.",
                )
                return

        session.last_activity = datetime.now(timezone.utc)
        session.last_idle_notified = None
        session.idle_reminder_count = 0
        session._bridge_busy = False
        drain_stderr(session)
        drain_sdk_buffer(session)
        if session._log:
            session._log.info("USER: %s", _content_summary(content))
        log.debug("Starting query for '%s'", agent_name)
        session.activity = ActivityState(phase="starting", query_started=datetime.now(timezone.utc))
        try:
            async with asyncio.timeout(QUERY_TIMEOUT):
                await session.client.query(_as_stream(content))
                await _stream_with_retry(session, message.channel)
        except TimeoutError:
            await _add_reaction(message, "⏳")
            await _handle_query_timeout(session, message.channel)
        except Exception:
            log.exception("Error querying agent '%s'", agent_name)
            await _add_reaction(message, "❌")
            await send_system(
                message.channel,
                f"Error communicating with agent **{agent_name}**. The session may have crashed. "
                f"Try `/kill-agent {agent_name}` and respawn.",
            )
        finally:
            session.activity = ActivityState(phase="idle")

    log.debug("Query completed for '%s'", agent_name)

    # Process any messages that were queued while the agent was busy
    await _process_message_queue(session)

    await bot.process_commands(message)


# --- Scheduler loop ---

@tasks.loop(seconds=10)
async def check_schedules():
    # If shutdown is in progress, skip all scheduled work
    if shutdown_coordinator and shutdown_coordinator.requested:
        return

    prune_history()
    prune_skips()

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now(SCHEDULE_TIMEZONE)
    entries = load_schedules()
    fired_one_off_keys: set[str] = set()  # schedule_key values for fired one-offs

    log.debug("Scheduler tick: %d entries, %d agents awake", len(entries), _count_awake_agents())

    # Get master channel for system-level notifications
    master_ch = await get_master_channel()

    for entry in list(entries):
        name = entry.get("name")
        if not name:
            continue

        try:
            if "schedule" in entry:
                # Recurring event — cron is evaluated in SCHEDULE_TIMEZONE
                cron_expr = entry["schedule"]
                if not croniter.is_valid(cron_expr):
                    log.warning("Invalid cron expression for %s: %s", name, cron_expr)
                    continue

                last_occurrence = croniter(cron_expr, now_local).get_prev(datetime)

                skey = schedule_key(entry)
                if skey not in schedule_last_fired:
                    schedule_last_fired[skey] = last_occurrence

                if last_occurrence > schedule_last_fired[skey]:
                    schedule_last_fired[skey] = last_occurrence

                    if check_skip(skey):
                        log.info("Skipping recurring event (one-off skip): %s", name)
                        continue

                    log.info("Firing recurring event: %s", name)
                    agent_name = entry.get("session", name)
                    agent_cwd = entry.get("cwd", os.path.join(AXI_USER_DATA, "agents", agent_name))

                    # Post schedule label to Discord for transparency
                    sched_ch = await get_agent_channel(agent_name) if agent_name in agents else None
                    if sched_ch:
                        await sched_ch.send(f"*System:* 📅 Scheduled: `{name}`")

                    if agent_name in agents:
                        # Session already exists — send prompt to it
                        log.info("Routing event '%s' to existing session '%s'", name, agent_name)
                        await send_prompt_to_agent(agent_name, entry["prompt"])
                    else:
                        await reclaim_agent_name(agent_name)
                        await spawn_agent(agent_name, agent_cwd, entry["prompt"])

            elif "at" in entry:
                # One-off event
                fire_at = datetime.fromisoformat(entry["at"])

                if fire_at <= now_utc:
                    log.info("Firing one-off event: %s", name)
                    agent_name = entry.get("session", name)
                    agent_cwd = entry.get("cwd", os.path.join(AXI_USER_DATA, "agents", agent_name))

                    # Post schedule label to Discord for transparency
                    sched_ch = await get_agent_channel(agent_name) if agent_name in agents else None
                    if sched_ch:
                        await sched_ch.send(f"*System:* 📅 Scheduled (one-off): `{name}`")

                    if agent_name in agents:
                        log.info("Routing event '%s' to existing session '%s'", name, agent_name)
                        await send_prompt_to_agent(agent_name, entry["prompt"])
                    else:
                        await reclaim_agent_name(agent_name)
                        await spawn_agent(agent_name, agent_cwd, entry["prompt"])

                    # Track for removal (actual save happens below under lock)
                    fired_one_off_keys.add(schedule_key(entry))
                    append_history(entry, now_utc)

        except Exception:
            log.exception("Error processing scheduled event %s", name)

    if fired_one_off_keys:
        # Re-read under lock and remove only the fired entries.
        # This avoids overwriting schedules added by MCP tools between
        # our initial load and this save.
        async with schedules_lock:
            current = load_schedules()
            current = [e for e in current if schedule_key(e) not in fired_one_off_keys]
            save_schedules(current)

    # --- Idle agent detection (Active-category agents only) ---
    idle_agents = []
    for agent_name, session in agents.items():
        if agent_name == MASTER_AGENT_NAME:
            continue
        if session.client is None:
            continue  # Sleeping agents don't need idle reminders
        if session.query_lock.locked():
            continue  # Agent is busy (possibly stuck), not idle
        # Skip agents in the Killed category
        if killed_category and session.discord_channel_id:
            ch = bot.get_channel(session.discord_channel_id)
            if ch and ch.category_id == killed_category.id:
                continue
        if session.idle_reminder_count >= len(IDLE_REMINDER_THRESHOLDS):
            continue  # All reminders already sent

        # Cumulative threshold: sum of thresholds up to current reminder count
        cumulative = sum(IDLE_REMINDER_THRESHOLDS[:session.idle_reminder_count + 1], timedelta())
        idle_duration = now_utc - session.last_activity

        if idle_duration > cumulative:
            idle_minutes = int(idle_duration.total_seconds() / 60)
            idle_agents.append((session, agent_name, idle_minutes))

    for session, agent_name, idle_minutes in idle_agents:
        # Notify in the agent's own channel
        agent_ch = await get_agent_channel(agent_name)
        if agent_ch:
            await send_system(
                agent_ch,
                f"Agent **{agent_name}** has been idle for {idle_minutes} minutes. "
                f"Use `/kill-agent` to terminate.",
            )
        # Only notify master channel on the final threshold (48h) to reduce noise
        is_final_threshold = session.idle_reminder_count + 1 >= len(IDLE_REMINDER_THRESHOLDS)
        if master_ch and is_final_threshold:
            await send_system(
                master_ch,
                f"Agent **{agent_name}** has been idle for {idle_minutes} minutes "
                f"(cwd: `{session.cwd}`). Use `/kill-agent` to terminate.",
            )
        session.idle_reminder_count += 1
        session.last_idle_notified = datetime.now(timezone.utc)

    # --- Stranded-message safety net ---
    # Catch any messages stranded by the tiny race between queue-empty check and sleep.
    # Only attempt if there's an awake slot available to avoid re-queuing loops.
    if _count_awake_agents() < MAX_AWAKE_AGENTS:
        for agent_name, session in agents.items():
            if agent_name == MASTER_AGENT_NAME:
                continue
            if (session.client is None
                    and not session.message_queue.empty()
                    and not session.query_lock.locked()):
                content, ch, stranded_msg = session.message_queue.get_nowait()
                log.info("Stranded message found for sleeping agent '%s', waking", agent_name)
                await _remove_reaction(stranded_msg, "📨")
                asyncio.create_task(_run_initial_prompt(session, content, ch))
                break  # One at a time to respect concurrency limit

    # --- Delayed sleep for idle awake agents ---
    # Under concurrency pressure, sleep idle agents immediately; otherwise wait 1 minute.
    awake_count = _count_awake_agents()
    under_pressure = awake_count >= MAX_AWAKE_AGENTS
    idle_threshold = timedelta(seconds=0) if under_pressure else timedelta(minutes=1)
    if under_pressure:
        log.info("Concurrency pressure: %d/%d awake agents — aggressive idle sleep", awake_count, MAX_AWAKE_AGENTS)

    for agent_name, session in list(agents.items()):
        if session.client is None:
            continue  # Already sleeping
        if session.query_lock.locked():
            continue  # Busy
        if session._bridge_busy:
            continue  # Reconnected to running CLI — task still in progress
        idle_duration = now_utc - session.last_activity
        if idle_duration > idle_threshold:
            log.info("Auto-sleeping idle agent '%s' (idle %.0fs, pressure=%s)",
                     agent_name, idle_duration.total_seconds(), under_pressure)
            try:
                await sleep_agent(session)
            except Exception:
                log.exception("Error auto-sleeping agent '%s'", agent_name)


@check_schedules.before_loop
async def before_check_schedules():
    await bot.wait_until_ready()


# --- Slash commands ---

async def killable_agent_autocomplete(interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete callback excluding axi-master."""
    return [
        app_commands.Choice(name=name, value=name)
        for name in agents.keys()
        if name != MASTER_AGENT_NAME and current.lower() in name.lower()
    ][:25]


async def agent_autocomplete(interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete callback for agent name parameters (all agents)."""
    return [
        app_commands.Choice(name=name, value=name)
        for name in agents.keys()
        if current.lower() in name.lower()
    ][:25]


@bot.tree.command(name="ping", description="Check bot latency and uptime.")
async def ping_command(interaction: discord.Interaction):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    def _fmt_uptime(total_seconds: int) -> str:
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s"

    # Bot uptime
    if _bot_start_time is not None:
        bot_uptime = datetime.now(timezone.utc) - _bot_start_time
        bot_str = _fmt_uptime(int(bot_uptime.total_seconds()))
    else:
        bot_str = "initializing"

    # Bridge uptime (if connected)
    bridge_str = None
    if bridge_conn is not None and bridge_conn.is_alive:
        try:
            result = await bridge_conn.send_command("status")
            if result.ok and result.uptime_seconds is not None:
                bridge_str = _fmt_uptime(result.uptime_seconds)
        except Exception:
            bridge_str = "error"

    latency = round(bot.latency * 1000)
    parts = [f"Pong! Latency: {latency}ms", f"Bot uptime: {bot_str}"]
    if bridge_str is not None:
        parts.append(f"Bridge uptime: {bridge_str}")
    elif bridge_conn is None or not bridge_conn.is_alive:
        parts.append("Bridge: not connected")
    await interaction.response.send_message(" | ".join(parts))


@bot.tree.command(name="claude-usage", description="Show Claude API usage for current sessions and rate limit status.")
async def claude_usage_command(interaction: discord.Interaction):
    log.info("Slash command /claude-usage from %s", interaction.user)
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    lines = ["**Claude Usage — Current Sessions**", ""]

    total_cost = 0.0
    total_queries = 0

    if _session_usage:
        # Group by agent name, show each session
        for sid, usage in sorted(_session_usage.items(), key=lambda x: x[1].last_query or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
            total_cost += usage.total_cost_usd
            total_queries += usage.queries

            duration_s = usage.total_duration_ms // 1000
            duration_str = _format_time_remaining(duration_s) if duration_s > 0 else "0s"

            active_str = ""
            if usage.first_query:
                age_s = int((datetime.now(timezone.utc) - usage.first_query).total_seconds())
                active_str = f" | Active since {_format_time_remaining(age_s)} ago"

            lines.append(f"**{usage.agent_name}** (`{sid[:8]}`)")
            lines.append(f"  Cost: **${usage.total_cost_usd:.2f}** | Queries: {usage.queries} | Turns: {usage.total_turns}")
            lines.append(f"  API time: {duration_str}{active_str}")
            lines.append("")

        lines.append(f"**Total: ${total_cost:.2f}** across {total_queries} queries")
    else:
        lines.append("No usage recorded yet.")

    lines.append("")

    # Rate limit section
    if _rate_limit_quota:
        q = _rate_limit_quota
        now = datetime.now(timezone.utc)
        remaining_s = max(0, int((q.resets_at - now).total_seconds()))
        resets_str = _format_time_remaining(remaining_s) if remaining_s > 0 else "now"

        # Format reset time in schedule timezone
        local_reset = q.resets_at.astimezone(SCHEDULE_TIMEZONE)
        reset_time_str = local_reset.strftime("%-I:%M %p")

        if q.status == "rejected":
            if q.utilization is not None:
                pct = int(q.utilization * 100)
                status_str = f"\U0001f6ab Rate limited ({pct}% used)"
            else:
                status_str = "\U0001f6ab Rate limited"
        elif q.status == "allowed_warning" and q.utilization is not None:
            pct = int(q.utilization * 100)
            status_str = f"\u26a0\ufe0f {pct}% used"
        else:
            status_str = "\u2705 OK (< 80%)"

        age_s = int((now - q.updated_at).total_seconds())
        age_str = _format_time_remaining(age_s) if age_s > 0 else "just now"

        lines.append(f"**Rate Limit** ({q.rate_limit_type.replace('_', ' ')})")
        lines.append(f"  Status: {status_str}")
        lines.append(f"  Resets at: {reset_time_str} (in {resets_str})")
        lines.append(f"  Last checked: {age_str} ago")
    elif _rate_limited_until:
        remaining = _format_time_remaining(_rate_limit_remaining_seconds())
        lines.append(f"**Rate Limit**: \U0001f6ab Rate limited (~{remaining} remaining)")
    else:
        lines.append("**Rate Limit**: No data yet (updates on next API call)")

    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="list-agents", description="List all active agent sessions.")
async def list_agents(interaction):
    log.info("Slash command /list-agents from %s", interaction.user)
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    if not agents:
        await interaction.response.send_message("No active agents.", ephemeral=True)
        return

    now = datetime.now(timezone.utc)
    lines = []
    for name, session in agents.items():
        idle_minutes = int((now - session.last_activity).total_seconds() / 60)
        # Determine status indicator
        if session.query_lock.locked():
            status = " [busy]"
        elif session.client is not None:
            status = " [awake]"
        else:
            status = " [sleeping]"
        # Check if in Killed category
        is_killed = False
        if killed_category and session.discord_channel_id:
            ch = bot.get_channel(session.discord_channel_id)
            if ch and ch.category_id == killed_category.id:
                is_killed = True
        killed_tag = " [killed]" if is_killed else ""
        protected = " [protected]" if name == MASTER_AGENT_NAME else ""
        sid = f" | sid: `{session.session_id[:8]}…`" if session.session_id else ""
        ch_mention = f" | <#{session.discord_channel_id}>" if session.discord_channel_id else ""
        lines.append(
            f"- **{name}**{status}{killed_tag}{protected}{ch_mention} | cwd: `{session.cwd}` | idle: {idle_minutes}m{sid}"
        )

    awake = _count_awake_agents()
    header = f"*System:* **Agent Sessions** ({awake}/{MAX_AWAKE_AGENTS} awake):\n"
    await interaction.response.send_message(header + "\n".join(lines))


@bot.tree.command(name="status", description="Show what an agent is currently doing.")
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def agent_status(interaction, agent_name: str | None = None):
    log.info("Slash command /status agent=%s from %s", agent_name, interaction.user)
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    # If no agent specified, try to infer from channel
    if agent_name is None:
        agent_name = channel_to_agent.get(interaction.channel_id)

    # If still None, show all agents summary
    if agent_name is None:
        await _show_all_agents_status(interaction)
        return

    session = agents.get(agent_name)
    if session is None:
        await interaction.response.send_message(
            f"Agent **{agent_name}** not found.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        _format_agent_status(agent_name, session), ephemeral=True
    )


def _format_agent_status(name: str, session: AgentSession) -> str:
    """Format a detailed status message for a single agent."""
    now = datetime.now(timezone.utc)
    lines = [f"**{name}**"]

    # Basic state
    if session.client is None:
        lines.append("State: sleeping")
        idle = int((now - session.last_activity).total_seconds())
        lines.append(f"Last active: {_format_time_remaining(idle)} ago")
    elif session._bridge_busy:
        lines.append("State: **busy** (running in bridge)")
    elif not session.query_lock.locked():
        lines.append("State: awake, idle")
        idle = int((now - session.last_activity).total_seconds())
        lines.append(f"Idle for: {_format_time_remaining(idle)}")
    else:
        # Agent is busy — show detailed activity
        activity = session.activity

        if activity.phase == "thinking":
            lines.append("State: **thinking** (extended thinking)")
        elif activity.phase == "writing":
            lines.append(f"State: **writing response** ({activity.text_chars} chars so far)")
        elif activity.phase == "tool_use" and activity.tool_name:
            display = _tool_display(activity.tool_name)
            lines.append(f"State: **{display}**")
            # Show tool input preview for interesting tools
            if activity.tool_name == "Bash" and activity.tool_input_preview:
                preview = _extract_tool_preview(activity.tool_name, activity.tool_input_preview)
                if preview:
                    lines.append(f"```\n{preview}\n```")
            elif activity.tool_name in ("Read", "Write", "Edit", "Grep", "Glob") and activity.tool_input_preview:
                preview = _extract_tool_preview(activity.tool_name, activity.tool_input_preview)
                if preview:
                    lines.append(f"`{preview}`")
        elif activity.phase == "waiting":
            lines.append("State: **processing tool results...**")
        elif activity.phase == "starting":
            lines.append("State: **starting query...**")
        else:
            lines.append(f"State: **busy** ({activity.phase})")

        # Query duration
        if activity.query_started:
            elapsed = int((now - activity.query_started).total_seconds())
            lines.append(f"Query running for: {_format_time_remaining(elapsed)}")

        # Turn count
        if activity.turn_count > 0:
            lines.append(f"API turns: {activity.turn_count}")

        # Staleness check
        if activity.last_event:
            since_last = int((now - activity.last_event).total_seconds())
            if since_last > 30:
                lines.append(f"No stream events for {_format_time_remaining(since_last)} (may be running a long tool)")

    # Queue
    queue_size = session.message_queue.qsize()
    if queue_size > 0:
        lines.append(f"Queued messages: {queue_size}")

    # Rate limit
    if _is_rate_limited():
        remaining = _format_time_remaining(_rate_limit_remaining_seconds())
        lines.append(f"Rate limited: ~{remaining} remaining")

    # Session info
    if session.session_id:
        lines.append(f"Session: `{session.session_id[:8]}...`")
    lines.append(f"cwd: `{session.cwd}`")

    return "\n".join(lines)


async def _show_all_agents_status(interaction):
    """Show a summary of all agents when /status is used without an agent name."""
    if not agents:
        await interaction.response.send_message("No active agents.", ephemeral=True)
        return

    now = datetime.now(timezone.utc)
    lines = []
    for name, session in agents.items():
        if session.client is None:
            idle = int((now - session.last_activity).total_seconds())
            status = f"sleeping ({_format_time_remaining(idle)})"
        elif session._bridge_busy:
            status = "busy (running in bridge)"
        elif not session.query_lock.locked():
            idle = int((now - session.last_activity).total_seconds())
            status = f"idle ({_format_time_remaining(idle)})"
        else:
            activity = session.activity
            if activity.phase == "thinking":
                status = "thinking..."
            elif activity.phase == "writing":
                status = "writing response..."
            elif activity.phase == "tool_use" and activity.tool_name:
                status = _tool_display(activity.tool_name)
            elif activity.phase == "waiting":
                status = "processing tool results..."
            else:
                status = "busy"

            if activity.query_started:
                elapsed = int((now - activity.query_started).total_seconds())
                status += f" ({_format_time_remaining(elapsed)})"

        queue = session.message_queue.qsize()
        queue_str = f" | {queue} queued" if queue > 0 else ""
        lines.append(f"- **{name}**: {status}{queue_str}")

    awake = _count_awake_agents()
    header = f"**Agent Status** ({awake}/{MAX_AWAKE_AGENTS} awake)"
    if _is_rate_limited():
        remaining = _format_time_remaining(_rate_limit_remaining_seconds())
        header += f" | rate limited (~{remaining})"

    await interaction.response.send_message(
        f"*System:* {header}\n" + "\n".join(lines), ephemeral=True
    )


@bot.tree.command(name="kill-agent", description="Terminate an agent session.")
@app_commands.autocomplete(agent_name=killable_agent_autocomplete)
async def kill_agent(interaction, agent_name: str | None = None):
    log.info("Slash command /kill-agent %s from %s", agent_name, interaction.user)
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    # Infer agent from channel if not specified
    if agent_name is None:
        agent_name = channel_to_agent.get(interaction.channel_id)
        if agent_name is None:
            await interaction.response.send_message(
                "Could not determine agent for this channel. Specify an agent name.", ephemeral=True
            )
            return

    if agent_name == MASTER_AGENT_NAME:
        await interaction.response.send_message(
            "Cannot kill the axi-master session.", ephemeral=True
        )
        return

    if agent_name not in agents:
        await interaction.response.send_message(
            f"Agent **{agent_name}** not found.", ephemeral=True
        )
        return

    await interaction.response.defer()
    session = agents.get(agent_name)
    session_id = session.session_id if session else None

    # Notify in the agent's channel before archiving
    agent_ch = await get_agent_channel(agent_name)
    if agent_ch and agent_ch.id != interaction.channel_id:
        if session_id:
            await send_system(
                agent_ch,
                f"Agent **{agent_name}** moved to Killed.\n"
                f"Session ID: `{session_id}` — use this to resume later.",
            )
        else:
            await send_system(agent_ch, f"Agent **{agent_name}** moved to Killed.")

    # Remove from agents dict immediately so the name is freed for respawn
    agents.pop(agent_name, None)
    await sleep_agent(session)
    await move_channel_to_killed(agent_name)

    if session_id:
        await interaction.followup.send(
            f"*System:* Agent **{agent_name}** moved to Killed.\n"
            f"Session ID: `{session_id}` — use this to resume later."
        )
    else:
        await interaction.followup.send(f"*System:* Agent **{agent_name}** moved to Killed.")


@bot.tree.command(name="stop", description="Interrupt a running agent query (like Ctrl+C).")
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def stop_agent(interaction, agent_name: str | None = None):
    log.info("Slash command /stop agent=%s from %s", agent_name, interaction.user)
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    # Infer agent from channel if not specified
    if agent_name is None:
        agent_name = channel_to_agent.get(interaction.channel_id)
        if agent_name is None:
            await interaction.response.send_message(
                "Could not determine agent for this channel. Specify an agent name.", ephemeral=True
            )
            return

    session = agents.get(agent_name)
    if session is None:
        await interaction.response.send_message(f"Agent **{agent_name}** not found.", ephemeral=True)
        return

    if session.client is None or not session.query_lock.locked():
        await interaction.response.send_message(f"Agent **{agent_name}** is not busy.", ephemeral=True)
        return

    try:
        # interrupt cancels current query
        await session.client.interrupt()
        await interaction.response.send_message(f"*System:* Interrupt signal sent to **{agent_name}**.")
    except Exception as e:
        log.exception("Failed to interrupt agent '%s'", agent_name)
        await interaction.response.send_message(f"Failed to interrupt **{agent_name}**: {e}", ephemeral=True)


@bot.tree.command(name="reset-context", description="Reset an agent's context. Infers agent from current channel, or specify by name.")
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def reset_context(interaction, agent_name: str | None = None, working_dir: str | None = None):
    log.info("Slash command /reset-context agent=%s cwd=%s from %s", agent_name, working_dir, interaction.user)
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    # Infer agent from channel if not specified
    if agent_name is None:
        agent_name = channel_to_agent.get(interaction.channel_id)
        if agent_name is None:
            await interaction.response.send_message(
                "Could not determine agent for this channel. Specify an agent name.", ephemeral=True
            )
            return

    if agent_name not in agents:
        await interaction.response.send_message(
            f"Agent **{agent_name}** not found.", ephemeral=True
        )
        return

    await interaction.response.defer()
    session = await reset_session(agent_name, cwd=working_dir)
    await interaction.followup.send(
        f"*System:* Context reset for **{agent_name}**. Working directory: `{session.cwd}`"
    )


async def _run_agent_sdk_command(interaction, agent_name: str | None, command: str, label: str):
    """Run a Claude Code CLI slash command (e.g. /compact, /clear) on an agent via the SDK."""
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    # Infer agent from channel if not specified
    if agent_name is None:
        agent_name = channel_to_agent.get(interaction.channel_id)
        if agent_name is None:
            await interaction.response.send_message(
                "Could not determine agent for this channel. Specify an agent name.", ephemeral=True
            )
            return

    session = agents.get(agent_name)
    if session is None:
        await interaction.response.send_message(f"Agent **{agent_name}** not found.", ephemeral=True)
        return

    if session.query_lock.locked():
        await interaction.response.send_message(f"Agent **{agent_name}** is busy.", ephemeral=True)
        return

    await interaction.response.defer()

    async with session.query_lock:
        if session.client is None:
            try:
                await wake_agent(session)
            except Exception:
                log.exception("Failed to wake agent '%s'", agent_name)
                await interaction.followup.send(f"Failed to wake agent **{agent_name}**.")
                return

        session.last_activity = datetime.now(timezone.utc)
        drain_stderr(session)
        drain_sdk_buffer(session)

        session.activity = ActivityState(phase="starting", query_started=datetime.now(timezone.utc))
        try:
            async with asyncio.timeout(QUERY_TIMEOUT):
                await session.client.query(_as_stream(command))
                channel = bot.get_channel(session.discord_channel_id) or interaction.channel
                await _stream_with_retry(session, channel)
            await interaction.followup.send(f"*System:* {label} for **{agent_name}**.")
        except TimeoutError:
            await interaction.followup.send(f"*System:* {label} timed out for **{agent_name}**.")
        except Exception as e:
            log.exception("Failed to %s agent '%s'", label.lower(), agent_name)
            await interaction.followup.send(f"Failed to {label.lower()} **{agent_name}**: {e}")
        finally:
            session.activity = ActivityState(phase="idle")


@bot.tree.command(name="compact", description="Compact an agent's conversation context. Infers agent from current channel.")
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def compact_context(interaction, agent_name: str | None = None):
    log.info("Slash command /compact agent=%s from %s", agent_name, interaction.user)
    await _run_agent_sdk_command(interaction, agent_name, "/compact", "Context compacted")


@bot.tree.command(name="clear", description="Clear an agent's conversation context. Infers agent from current channel.")
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def clear_context(interaction, agent_name: str | None = None):
    log.info("Slash command /clear agent=%s from %s", agent_name, interaction.user)
    await _run_agent_sdk_command(interaction, agent_name, "/clear", "Context cleared")


@bot.tree.command(name="restart", description="Hot-reload bot.py (bridge stays alive, agents keep running).")
@app_commands.describe(force="Skip waiting for busy agents and restart immediately")
async def restart_cmd(interaction, force: bool = False):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if shutdown_coordinator is None:
        await interaction.response.send_message("Bot is not fully initialized yet.", ephemeral=True)
        return

    if force:
        await interaction.response.send_message("*System:* Force restarting (hot reload)...")
        log.info("Force restart requested via /restart command")
        await shutdown_coordinator.force_shutdown("/restart force")
        return

    await interaction.response.send_message("*System:* Initiating graceful restart (hot reload)...")
    log.info("Restart requested via /restart command")
    await shutdown_coordinator.graceful_shutdown("/restart command")


@bot.tree.command(
    name="restart-including-bridge",
    description="Full restart — kills bridge + all agents. Sessions will disconnect.",
)
@app_commands.describe(force="Skip waiting for busy agents and restart immediately")
async def restart_including_bridge_cmd(interaction, force: bool = False):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    if shutdown_coordinator is None:
        await interaction.response.send_message("Bot is not fully initialized yet.", ephemeral=True)
        return
    # Guard against double-restart: the existing coordinator tracks _requested
    # for the soft restart path. Check it so we don't start a second shutdown.
    if shutdown_coordinator.requested:
        await interaction.response.send_message(
            "*System:* A restart is already in progress.", ephemeral=True,
        )
        return

    # Build an on-demand coordinator that uses kill_supervisor (full restart)
    # and bridge_mode=False so agents get properly slept before exit.
    async def _notify_agent_channel(agent_name: str, message: str) -> None:
        channel = await get_agent_channel(agent_name)
        if channel:
            await send_system(channel, message)

    async def _send_goodbye() -> None:
        master_ch = await get_master_channel()
        if master_ch:
            await master_ch.send("*System:* Full restart — bridge is going down. See you soon!")

    full_coordinator = ShutdownCoordinator(
        agents=agents,
        sleep_fn=sleep_agent,
        close_bot_fn=bot.close,
        kill_fn=kill_supervisor,
        notify_fn=_notify_agent_channel,
        goodbye_fn=_send_goodbye,
        bridge_mode=False,
    )

    if force:
        await interaction.response.send_message(
            "*System:* Force restarting (full — bridge will be killed, agents will disconnect)..."
        )
        log.info("Force full restart requested via /restart-including-bridge command")
        await full_coordinator.force_shutdown("/restart-including-bridge force")
        return

    await interaction.response.send_message(
        "*System:* Initiating graceful full restart (bridge will be killed, agents will disconnect)..."
    )
    log.info("Full restart requested via /restart-including-bridge command")
    await full_coordinator.graceful_shutdown("/restart-including-bridge command")


# --- Channel creation listener ---

@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    """Auto-register agent when a user manually creates a channel in the Active category."""
    if not isinstance(channel, discord.TextChannel):
        return
    if not active_category or channel.category_id != active_category.id:
        return
    if channel.name in _bot_creating_channels:
        return  # Bot created this channel, spawn_agent will handle registration
    if channel.name == _normalize_channel_name(MASTER_AGENT_NAME):
        return

    agent_name = channel.name
    if agent_name in agents:
        return  # Already registered (e.g. reconstruct or race)

    cwd = os.path.join(AXI_USER_DATA, "agents", agent_name)
    os.makedirs(cwd, exist_ok=True)

    session = AgentSession(
        name=agent_name,
        client=None,
        cwd=cwd,
        discord_channel_id=channel.id,
        mcp_servers={
            "utils": _utils_mcp_server,
            "schedule": make_schedule_mcp_server(agent_name, SCHEDULES_PATH),
        },
    )
    agents[agent_name] = session
    channel_to_agent[channel.id] = agent_name

    desired_topic = _format_channel_topic(cwd)
    try:
        await channel.edit(topic=desired_topic)
    except discord.HTTPException as e:
        log.warning("Failed to set topic on #%s: %s", agent_name, e)

    await send_system(channel, f"Agent **{agent_name}** auto-registered from channel creation.\n`cwd: {cwd}`\nSend a message to wake it up.")
    log.info("Auto-registered agent '%s' from manual channel creation (cwd=%s)", agent_name, cwd)


# --- Readme channel sync ---

async def sync_readme_channel() -> None:
    """Sync the readme channel: find or create #readme, lock permissions, update message.

    Skips entirely if readme_content.md doesn't exist.
    """
    # Load content from file — if no file, skip silently
    try:
        readme_text = open(README_CONTENT_PATH).read().strip()
    except FileNotFoundError:
        log.debug("readme_content.md not found — skipping readme sync")
        return
    if not readme_text:
        log.debug("readme_content.md is empty — skipping readme sync")
        return

    guild = target_guild
    if guild is None:
        log.warning("No guild available — skipping readme sync")
        return

    # Find or create #readme channel
    channel = None
    for ch in guild.text_channels:
            if ch.name == "readme" and ch.category is None:
                channel = ch
                break

    if channel is None:
        # Create it at the top of the channel list, outside any category
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                send_messages=False,
                view_channel=True,
                read_message_history=True,
            ),
            guild.me: discord.PermissionOverwrite(
                send_messages=True,
                manage_messages=True,
                view_channel=True,
                read_message_history=True,
            ),
        }
        channel = await guild.create_text_channel("readme", overwrites=overwrites, position=0)
        log.info("Created #readme channel")
    else:
        # Sync permissions on existing channel
        try:
            overwrites = channel.overwrites.copy()
            overwrites[guild.default_role] = discord.PermissionOverwrite(
                send_messages=False,
                view_channel=True,
                read_message_history=True,
            )
            overwrites[guild.me] = discord.PermissionOverwrite(
                send_messages=True,
                manage_messages=True,
                view_channel=True,
                read_message_history=True,
            )
            await channel.edit(overwrites=overwrites)
            log.info("Readme channel permissions synced")
        except Exception:
            log.exception("Failed to set readme channel permissions")

    # Find existing bot message (should be the only one from us)
    existing_msg = None
    async for msg in channel.history(limit=50):
        if msg.author == bot.user:
            existing_msg = msg
            break

    # Sync content
    if existing_msg is None:
        await channel.send(readme_text)
        log.info("Sent readme message to #%s", channel.name)
    elif existing_msg.content != readme_text:
        await existing_msg.edit(content=readme_text)
        log.info("Updated readme message in #%s", channel.name)
    else:
        log.info("Readme message in #%s already up to date", channel.name)


# --- Startup ---

_on_ready_fired = False

@bot.event
async def on_ready():
    global _on_ready_fired
    log.info("Bot ready as %s", bot.user)

    # Install global exception handler for fire-and-forget asyncio tasks
    asyncio.get_event_loop().set_exception_handler(_handle_task_exception)

    if _on_ready_fired:
        log.info("on_ready fired again (gateway reconnect) — skipping startup logic")
        return
    _on_ready_fired = True

    global _bot_start_time
    _bot_start_time = datetime.now(timezone.utc)

    # Register master agent as sleeping — it will wake on first message
    master_mcp = {"axi": _axi_mcp_server}
    if os.path.isdir(BOT_WORKTREES_DIR):
        master_mcp["discord"] = _discord_mcp_server
    master_session = AgentSession(
        name=MASTER_AGENT_NAME,
        cwd=DEFAULT_CWD,
        system_prompt=MASTER_SYSTEM_PROMPT,
        client=None,
        mcp_servers=master_mcp,
    )
    agents[MASTER_AGENT_NAME] = master_session
    log.info("Master agent registered (sleeping, will wake on first message)")

    # Set up guild infrastructure (categories + master channel)
    try:
        await ensure_guild_infrastructure()
        master_channel = await ensure_agent_channel(MASTER_AGENT_NAME)
        master_session = agents.get(MASTER_AGENT_NAME)
        if master_session:
            master_session.discord_channel_id = master_channel.id
        channel_to_agent[master_channel.id] = MASTER_AGENT_NAME
        log.info("Guild infrastructure ready (guild=%s, master_channel=#%s)", DISCORD_GUILD_ID, master_channel.name)

        # Set channel topic for master (only if changed)
        desired_topic = "Axi master control channel"
        if master_channel.topic != desired_topic:
            log.info("Updating topic on #%s: %r -> %r", master_channel.name, master_channel.topic, desired_topic)
            await master_channel.edit(topic=desired_topic)

    except Exception:
        log.exception("Failed to set up guild infrastructure — guild channels won't work")

    # Sync readme channel
    try:
        await sync_readme_channel()
    except Exception:
        log.exception("Failed to sync readme channel")

    # Reconstruct sleeping agents from existing channels
    try:
        await reconstruct_agents_from_channels()
    except Exception:
        log.exception("Failed to reconstruct agents from channels")

    # Connect to the agent bridge (or start a new one)
    await _connect_bridge()

    # Initialize shutdown coordinator now that all helpers are available
    _init_shutdown_coordinator()

    await bot.tree.sync()
    log.info("Slash commands synced")

    check_schedules.start()
    log.info("Schedule checker started")

    # Check for rollback marker (written by run.sh after auto-rollback)
    rollback_info = None
    if os.path.exists(ROLLBACK_MARKER_PATH):
        try:
            with open(ROLLBACK_MARKER_PATH) as f:
                rollback_info = json.load(f)
            os.remove(ROLLBACK_MARKER_PATH)
            log.info("Rollback marker found and consumed: %s", rollback_info)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to read rollback marker: %s", e)
            try:
                os.remove(ROLLBACK_MARKER_PATH)
            except OSError:
                pass

    # Check for crash analysis marker (written by run.sh after runtime crash)
    crash_info = None
    if os.path.exists(CRASH_ANALYSIS_MARKER_PATH):
        try:
            with open(CRASH_ANALYSIS_MARKER_PATH) as f:
                crash_info = json.load(f)
            os.remove(CRASH_ANALYSIS_MARKER_PATH)
            log.info("Crash analysis marker found and consumed")
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to read crash analysis marker: %s", e)
            try:
                os.remove(CRASH_ANALYSIS_MARKER_PATH)
            except OSError:
                pass

    # Send startup notification to master channel
    master_ch = await get_master_channel()
    if master_ch:
        if rollback_info:
            exit_code = rollback_info.get("exit_code", "unknown")
            uptime = rollback_info.get("uptime_seconds", "?")
            timestamp = rollback_info.get("timestamp", "unknown")
            details = rollback_info.get("rollback_details", "").strip()
            pre_commit = rollback_info.get("pre_launch_commit", "")
            crashed_commit = rollback_info.get("crashed_commit", "")

            msg_lines = [
                f"*System:* **Automatic rollback performed.**",
                f"Axi crashed on startup (exit code {exit_code} after {uptime}s) at {timestamp}.",
            ]
            if details:
                msg_lines.append(f"Actions taken: {details}.")
            if pre_commit and crashed_commit and pre_commit != crashed_commit:
                msg_lines.append(
                    f"Reverted from `{crashed_commit[:7]}` to `{pre_commit[:7]}`."
                )
                msg_lines.append(
                    "Reverted commits are still in the reflog: `git reflog`"
                )
            if "stashed" in details:
                msg_lines.append(
                    "Stashed changes: `git stash list` / `git stash show -p` / `git stash pop`"
                )
            if ENABLE_CRASH_HANDLER:
                msg_lines.append("Spawning crash analysis agent...")
            await master_ch.send("\n".join(msg_lines))
        elif crash_info:
            exit_code = crash_info.get("exit_code", "unknown")
            uptime = crash_info.get("uptime_seconds", "?")
            timestamp = crash_info.get("timestamp", "unknown")
            crash_msg = (
                f"Ow... I think I just blacked out for a second there. What happened?\n\n"
                f"*System:* **Runtime crash detected.**\n"
                f"Axi crashed after {uptime}s of uptime (exit code {exit_code}) at {timestamp}."
            )
            if ENABLE_CRASH_HANDLER:
                crash_msg += "\nSpawning crash analysis agent..."
            await master_ch.send(crash_msg)
        else:
            await master_ch.send("*System:* Axi restarted.")
        log.info("Sent restart notification to master channel")

    # Spawn crash handler agent if a crash was detected (startup or runtime)
    if not ENABLE_CRASH_HANDLER:
        if rollback_info or crash_info:
            log.info("Crash handler not enabled (set ENABLE_CRASH_HANDLER=1 to auto-spawn)")
    elif rollback_info:
        crash_log = rollback_info.get("crash_log", "(no crash log available)")
        exit_code = rollback_info.get("exit_code", "unknown")
        uptime = rollback_info.get("uptime_seconds", "?")
        timestamp = rollback_info.get("timestamp", "unknown")
        details = rollback_info.get("rollback_details", "").strip()
        pre_commit = rollback_info.get("pre_launch_commit", "")
        crashed_commit = rollback_info.get("crashed_commit", "")

        rollback_context = f"- Rollback actions: {details}\n" if details else ""
        if pre_commit and crashed_commit and pre_commit != crashed_commit:
            rollback_context += f"- Reverted from commit {crashed_commit[:7]} to {pre_commit[:7]}\n"
        if "stashed" in details:
            rollback_context += "- Uncommitted changes were stashed (see `git stash list`)\n"

        crash_prompt = (
            "The Discord bot (bot.py) crashed on startup and was auto-rolled-back. "
            "Analyze the crash and create a plan to fix it.\n"
            "\n"
            "## Crash Details\n"
            f"- Exit code: {exit_code}\n"
            f"- Uptime before crash: {uptime} seconds\n"
            f"- Timestamp: {timestamp}\n"
            f"{rollback_context}"
            "\n"
            "## Crash Log (last 200 lines of output before crash)\n"
            "```\n"
            f"{crash_log}\n"
            "```\n"
            "\n"
            "## Instructions\n"
            "1. Analyze the traceback and error messages to identify the root cause.\n"
            "2. Examine the relevant source code in this project directory.\n"
            "3. Check the rolled-back commits or stashed changes (if any) to understand what "
            "code changes caused the crash.\n"
            "4. Create a clear, detailed plan to fix the issue. Describe exactly which files "
            "need to change and what the changes should be.\n"
            "5. Do NOT apply any fixes yourself. Only produce the analysis and plan.\n"
        )

        await reclaim_agent_name("crash-handler")
        await spawn_agent("crash-handler", BOT_DIR, crash_prompt)

    elif crash_info:
        crash_log = crash_info.get("crash_log", "(no crash log available)")
        exit_code = crash_info.get("exit_code", "unknown")
        uptime = crash_info.get("uptime_seconds", "?")
        timestamp = crash_info.get("timestamp", "unknown")

        crash_prompt = (
            "The Discord bot (bot.py) crashed at runtime. Analyze the crash and create a plan to fix it.\n"
            "\n"
            "## Crash Details\n"
            f"- Exit code: {exit_code}\n"
            f"- Uptime before crash: {uptime} seconds\n"
            f"- Timestamp: {timestamp}\n"
            "\n"
            "## Crash Log (last 200 lines of output)\n"
            "```\n"
            f"{crash_log}\n"
            "```\n"
            "\n"
            "## Instructions\n"
            "1. Analyze the traceback and error messages to identify the root cause.\n"
            "2. Examine the relevant source code in this project directory.\n"
            "3. Create a clear, detailed plan to fix the issue. Describe exactly which files "
            "need to change and what the changes should be.\n"
            "4. Do NOT apply any fixes yourself. Only produce the analysis and plan.\n"
        )

        await reclaim_agent_name("crash-handler")
        await spawn_agent("crash-handler", BOT_DIR, crash_prompt)


def _handle_task_exception(loop, context):
    """Global handler for unhandled exceptions in asyncio tasks."""
    exception = context.get("exception")
    if exception:
        # Suppress expected ProcessError from SIGTERM'd subprocesses (our workaround kills them)
        if type(exception).__name__ == "ProcessError" and "-15" in str(exception):
            log.debug("Suppressed expected ProcessError from SIGTERM'd subprocess")
            return
        log.error("Unhandled exception in async task: %s", context.get("message", ""), exc_info=exception)
    else:
        log.error("Unhandled async error: %s", context.get("message", ""))


def _acquire_lock():
    """Acquire an exclusive file lock to prevent duplicate bot instances."""
    import fcntl
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.lock")
    # Open (or create) the lock file — keep the fd open for the process lifetime
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("ERROR: Another bot.py instance is already running (could not acquire .bot.lock). Exiting.")
        raise SystemExit(1)
    # Write our PID for debugging
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd  # caller must keep a reference so the fd stays open


if __name__ == "__main__":
    _lock_fd = _acquire_lock()
    try:
        # log_handler=None prevents discord.py from overriding our logging config
        bot.run(DISCORD_TOKEN, log_handler=None)
    except Exception:
        log.exception("Bot crashed with unhandled exception")
