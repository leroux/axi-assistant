"""Agent lifecycle, streaming, rate limits, procmux, and channel management.

Migration in progress: lifecycle, registry, and messaging delegate to AgentHub.
Discord-specific rendering (streaming, live-edit, reactions) stays here.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import pathlib
import re
import time
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import anyio
import discord
import httpx
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolPermissionContext,
)
from discord import TextChannel
from opentelemetry import context as otel_context
from opentelemetry import trace

from agenthub.procmux_wire import ProcmuxProcessConnection
from agenthub.tasks import BackgroundTaskSet
from axi import channels as _channels_mod
from axi import config, scheduler
from axi.axi_types import (
    ActivityState,
    AgentSession,
    ConcurrencyLimitError,
    ContentBlock,
    DiscordAgentState,
    MessageContent,
    discord_state,
)
from axi.channels import (
    ensure_agent_channel,
    ensure_guild_infrastructure,
    format_channel_topic,
    get_agent_channel,
    get_master_channel,
    mark_channel_active,
    move_channel_to_killed,
    normalize_channel_name,
    schedule_status_update,
)
from axi.channels import (
    parse_channel_topic as _parse_channel_topic,
)
from axi.log_context import set_agent_context, set_trigger
from axi.prompts import (
    compute_prompt_hash,
    make_spawned_agent_system_prompt,
    post_system_prompt_to_channel,
)
from axi.rate_limits import (
    format_time_remaining,
    is_rate_limited,
    notify_rate_limit_expired,
    rate_limit_quotas,
    rate_limit_remaining_seconds,
    rate_limited_until,
    session_usage,
)
from axi.rate_limits import (
    handle_rate_limit as _rl_handle_rate_limit,
)
from axi.rate_limits import (
    record_session_usage as _record_session_usage,
)
from axi.rate_limits import (
    update_rate_limit_quota as _update_rate_limit_quota,
)
from axi.schedule_tools import make_schedule_mcp_server
from axi.shutdown import ShutdownCoordinator, exit_for_restart, kill_supervisor
from axi.tracing import shutdown_tracing
from claudewire import BridgeTransport
from claudewire.events import as_stream, tool_display, update_activity
from claudewire.session import disconnect_client, get_stdio_logger
from procmux import ensure_running as ensure_bridge

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from agenthub import AgentHub
    from procmux import ProcmuxConnection

log = logging.getLogger("axi")

_tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_bot: Bot | None = None

# The hub owns lifecycle, registry, messaging, scheduler, rate limits.
# Created in init() via hub_wiring.create_hub().
hub: AgentHub | None = None

# Session dict — shared between hub and legacy code.
# hub.sessions points to this same dict during migration.
agents: dict[str, AgentSession] = {}
channel_to_agent: dict[int, str] = {}  # channel_id -> agent_name


def find_session_by_question_message(message_id: int) -> AgentSession | None:
    """Find the agent session waiting for a reaction answer on this message."""
    for session in agents.values():
        ds = discord_state(session)
        if ds.question_message_id == message_id:
            return session
    return None


# Bridge connection — initialized in on_ready(), used by wake_agent/sleep_agent
procmux_conn: ProcmuxConnection | None = None
# Adapted connection for claudewire (wraps procmux_conn)
wire_conn: ProcmuxProcessConnection | None = None

# Shutdown coordinator — initialized via init_shutdown_coordinator() from on_ready
shutdown_coordinator: ShutdownCoordinator | None = None

# Scheduler state
schedule_last_fired: dict[str, datetime] = {}

# Stream tracing
_stream_counter = 0

# Active trace IDs — maps agent name → trace tag string (e.g. "[trace=abc123...]")
# Set when process_message starts its span, cleared when done.
_active_trace_ids: dict[str, str] = {}


def get_active_trace_tag(agent_name: str) -> str:
    """Return the trace tag for the agent's in-flight turn, or empty string."""
    return _active_trace_ids.get(agent_name, "")

# MCP server injection (set by bot.py after tools.py creates them)
_utils_mcp_server: Any = None

# Background task manager — hub.tasks after full migration; kept for legacy callers.
_bg_tasks = BackgroundTaskSet()
fire_and_forget = _bg_tasks.fire_and_forget


def _user_mentions() -> str:
    """Generate Discord @mention string for all allowed users."""
    return " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)


# ---------------------------------------------------------------------------
# Initialization — called once from bot.py after Bot creation
# ---------------------------------------------------------------------------


def init(bot_instance: Bot) -> None:
    """Inject the Bot reference and create the AgentHub. Called once from bot.py."""
    global _bot, hub
    _bot = bot_instance

    # Create the hub — shares the `agents` dict with legacy code
    from axi.hub_wiring import create_hub

    hub = create_hub(bot_instance, agents)

    _channels_mod.init(bot_instance, agents, channel_to_agent, send_to_exceptions)


def set_utils_mcp_server(server: Any) -> None:
    """Set the utils MCP server reference. Called from bot.py after tools.py init."""
    global _utils_mcp_server
    _utils_mcp_server = server


def _next_stream_id(agent_name: str) -> str:
    """Generate a unique stream ID for tracing."""
    global _stream_counter
    _stream_counter += 1
    return f"{agent_name}:S{_stream_counter}"


def _build_mcp_servers(
    agent_name: str,
    cwd: str | None = None,
    extra_mcp_servers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard MCP server dict for an agent.

    Args:
        agent_name: The agent's name (used for schedule scoping).
        cwd: The agent's working directory.
        extra_mcp_servers: Additional MCP servers to merge in (e.g. from
            mcp_servers.json).  These are McpStdioServerConfig-compatible
            dicts keyed by name.
    """
    servers: dict[str, Any] = {}
    if _utils_mcp_server is not None:
        servers["utils"] = _utils_mcp_server
    servers["schedule"] = make_schedule_mcp_server(agent_name, config.SCHEDULES_PATH, cwd)
    servers["playwright"] = {
        "command": "npx",
        "args": ["@playwright/mcp@latest", "--headless"],
    }
    if extra_mcp_servers:
        servers.update(extra_mcp_servers)
    return servers


def _save_agent_config(
    agent_name: str,
    mcp_server_names: list[str] | None,
    packs: list[str] | None = None,
) -> None:
    """Persist per-agent config (MCP servers, packs) to disk."""
    config_dir = os.path.join(config.AXI_USER_DATA, "agents", agent_name)
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "agent_config.json")
    data: dict[str, Any] = {}
    if mcp_server_names:
        data["mcp_servers"] = mcp_server_names
    if packs is not None:
        data["packs"] = packs
    try:
        with open(config_path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        log.warning("Failed to save agent config for '%s'", agent_name, exc_info=True)


def _load_agent_config(agent_name: str) -> dict[str, Any]:
    """Load per-agent config from disk. Returns {} if not found."""
    config_path = os.path.join(config.AXI_USER_DATA, "agents", agent_name, "agent_config.json")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path) as f:
            return json.load(f)
    except Exception:
        log.warning("Failed to load agent config for '%s'", agent_name, exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# SDK utilities
# ---------------------------------------------------------------------------


def _close_agent_log(session: AgentSession) -> None:
    """Remove all handlers from the per-agent logger."""
    if session.agent_log:
        for handler in session.agent_log.handlers[:]:
            handler.close()
            session.agent_log.removeHandler(handler)


_AUTOCOMPACT_RE = re.compile(r"autocompact: tokens=(\d+) threshold=\d+ effectiveWindow=(\d+)")


def make_stderr_callback(session: AgentSession):
    """Create a stderr callback bound to a specific agent session."""
    ds = discord_state(session)  # cache — callback runs in a thread

    def callback(text: str) -> None:
        with ds.stderr_lock:
            ds.stderr_buffer.append(text)
        # Parse autocompact debug line for context window monitoring
        m = _AUTOCOMPACT_RE.search(text)
        if m:
            session.context_tokens = int(m.group(1))
            session.context_window = int(m.group(2))

    return callback


def drain_stderr(session: AgentSession) -> list[str]:
    """Drain stderr buffer for a specific agent session."""
    ds = discord_state(session)
    with ds.stderr_lock:
        msgs = list(ds.stderr_buffer)
        ds.stderr_buffer.clear()
    return msgs


def drain_sdk_buffer(session: AgentSession) -> int:
    """Drain any stale messages from the SDK message buffer before sending a new query."""
    if session.client is None or getattr(session.client, "_query", None) is None:
        return 0

    client = session.client
    # narrowing: getattr check above guarantees this
    assert client._query is not None  # pyright: ignore[reportPrivateUsage]
    receive_stream = client._query._message_receive  # pyright: ignore[reportPrivateUsage]
    drained: list[dict[str, Any]] = []
    while True:
        try:
            msg = receive_stream.receive_nowait()
            drained.append(msg)
        except anyio.WouldBlock:
            break
        except Exception:
            log.warning("Unexpected error draining SDK buffer for '%s'", session.name, exc_info=True)
            break

    if drained:
        for msg in drained:
            msg_type = msg.get("type", "?")
            msg_role = msg.get("message", {}).get("role", "") if isinstance(msg.get("message"), dict) else ""
            log.warning(
                "Drained stale SDK message from '%s': type=%s role=%s",
                session.name,
                msg_type,
                msg_role,
            )
            if msg_type == "rate_limit_event":
                _update_rate_limit_quota(msg)
        log.warning("Total drained from '%s': %d stale messages", session.name, len(drained))

    return len(drained)


# ---------------------------------------------------------------------------
# Discord helpers: reactions, message extraction, splitting, and sending
# ---------------------------------------------------------------------------

# Exceptions channel (REST-based, works in any context)
_exceptions_channel_id: str | None = None


async def add_reaction(message: discord.Message | None, emoji: str) -> None:
    """Add a reaction to a message, silently ignoring errors."""
    if message is None:
        return
    try:
        await message.add_reaction(emoji)
        log.info("Reaction +%s on message %s", emoji, message.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Reaction +%s failed on message %s: %s", emoji, message.id, exc)


async def remove_reaction(message: discord.Message | None, emoji: str) -> None:
    """Remove the bot's own reaction from a message, silently ignoring errors."""
    if message is None:
        return
    try:
        assert _bot is not None
        assert _bot.user is not None
        await message.remove_reaction(emoji, _bot.user)
        log.info("Reaction -%s on message %s", emoji, message.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Reaction -%s failed on message %s: %s", emoji, message.id, exc)


# ---------------------------------------------------------------------------
# Image attachment support
# ---------------------------------------------------------------------------

_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB per image


async def extract_message_content(message: discord.Message) -> MessageContent:
    """Extract text and image content from a Discord message."""
    # Discord long-message: blank content with an attached message.txt
    if not message.content.strip() and message.attachments:
        for a in message.attachments:
            if a.filename == "message.txt" and a.size <= 100_000:
                try:
                    data = await a.read()
                    text = data.decode("utf-8")
                    log.debug("Read long message from message.txt (%d chars)", len(text))
                    message.content = text
                    break
                except Exception:
                    log.warning("Failed to read message.txt attachment", exc_info=True)

    ts_prefix = message.created_at.strftime("[%Y-%m-%d %H:%M:%S UTC] ")

    image_attachments = [
        a
        for a in message.attachments
        if a.content_type
        and a.content_type.split(";")[0].strip() in _SUPPORTED_IMAGE_TYPES
        and a.size <= _MAX_IMAGE_SIZE
    ]

    if not image_attachments:
        return ts_prefix + message.content

    blocks: list[ContentBlock] = []
    blocks.append({"type": "text", "text": ts_prefix + (message.content or "")})

    for attachment in image_attachments:
        try:
            data = await attachment.read()
            b64 = base64.b64encode(data).decode("utf-8")
            mime = (attachment.content_type or "application/octet-stream").split(";")[0].strip()
            blocks.append({"type": "image", "data": b64, "mimeType": mime})
            log.debug("Attached image: %s (%s, %d bytes)", attachment.filename, mime, len(data))
        except Exception:
            log.warning("Failed to download attachment %s", attachment.filename, exc_info=True)

    return blocks or message.content


def content_summary(content: MessageContent) -> str:
    """Short text summary of message content for logging."""
    if isinstance(content, str):
        return content[:200]
    parts: list[str] = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block["text"][:100])
        elif block.get("type") == "image":
            parts.append(f"[image:{block.get('mimeType', '?')}]")
    return " ".join(parts)[:200]


# ---------------------------------------------------------------------------
# Exceptions channel (REST-based, works in any context)
# ---------------------------------------------------------------------------


async def _get_or_create_exceptions_channel() -> str | None:
    """Get or create the #exceptions channel via REST API."""
    global _exceptions_channel_id

    if _exceptions_channel_id is not None:
        return _exceptions_channel_id
    try:
        guild_id = str(config.DISCORD_GUILD_ID)
        ch = await config.discord_client.find_channel(guild_id, "exceptions")
        if ch:
            _exceptions_channel_id = ch["id"]
            return _exceptions_channel_id
        created = await config.discord_client.create_channel(guild_id, "exceptions")
        _exceptions_channel_id = created["id"]
        log.info("Created #exceptions channel (id=%s)", _exceptions_channel_id)
        return _exceptions_channel_id
    except Exception:
        log.warning("Failed to get/create #exceptions channel", exc_info=True)
        return None


async def send_to_exceptions(message: str) -> bool:
    """Send a message to the #exceptions channel. Returns True on success."""
    global _exceptions_channel_id

    try:
        ch_id = await _get_or_create_exceptions_channel()
        if ch_id is None:
            return False
        await config.discord_client.send_message(ch_id, message[:2000])
        return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            log.warning("#exceptions channel %s returned 404; clearing cached ID", _exceptions_channel_id)
            _exceptions_channel_id = None
        else:
            log.warning("Failed to send to #exceptions", exc_info=True)
        return False
    except Exception:
        log.warning("Failed to send to #exceptions", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Message splitting / sending
# ---------------------------------------------------------------------------

from discordquery import split_message


async def send_long(channel: TextChannel, text: str) -> discord.Message | None:
    """Send a potentially long message, splitting as needed. Returns the last sent message."""
    # Track channel activity for recency reordering
    mark_channel_active(channel.id)

    span = _tracer.start_span(
        "discord.send_long",
        attributes={"discord.channel": getattr(channel, "name", "?"), "message.length": len(text)},
    )
    chunks = split_message(text.strip())
    span.set_attribute("message.chunks", len(chunks))
    span.end()
    last_msg: discord.Message | None = None
    for i, chunk in enumerate(chunks):
        if chunk:
            if log.isEnabledFor(logging.INFO):
                caller = "".join(f.name or "?" for f in traceback.extract_stack(limit=4)[:-1])
                log.info(
                    "DISCORD_SEND[#%s] chunk %d/%d len=%d caller=%s text=%r",
                    getattr(channel, "name", "?"),
                    i + 1,
                    len(chunks),
                    len(chunk),
                    caller,
                    chunk[:80],
                )
            try:
                last_msg = await channel.send(chunk)
            except discord.NotFound:
                agent_name = channel_to_agent.get(channel.id)
                if agent_name:
                    log.warning("Channel for '%s' was deleted, recreating", agent_name)
                    session = agents.get(agent_name)
                    new_ch = await ensure_agent_channel(agent_name, cwd=session.cwd if session else None)
                    if session:
                        discord_state(session).channel_id = new_ch.id
                    last_msg = await new_ch.send(chunk)
                else:
                    raise
    return last_msg


async def send_system(channel: TextChannel, text: str) -> None:
    """Send a system-prefixed message."""
    await send_long(channel, f"*System:* {text}")


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def make_cwd_permission_callback(allowed_cwd: str, session: AgentSession | None = None):
    """Create a can_use_tool callback that restricts file writes to allowed_cwd and AXI_USER_DATA."""
    allowed = os.path.realpath(allowed_cwd)
    user_data = os.path.realpath(config.AXI_USER_DATA)
    worktrees = os.path.realpath(config.BOT_WORKTREES_DIR)
    bot_dir = os.path.realpath(config.BOT_DIR)

    is_code_agent = allowed in (bot_dir, worktrees) or allowed.startswith((bot_dir + os.sep, worktrees + os.sep))
    bases = [allowed, user_data]
    if is_code_agent:
        bases.append(worktrees)
        bases.extend(config.ADMIN_ALLOWED_CWDS)

    async def _check_permission(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        forbidden_tools = {"Skill", "EnterWorktree", "Task"}
        if tool_name in forbidden_tools:
            return PermissionResultDeny(
                message=f"{tool_name} is not compatible with Discord-based agent mode. Use text messages to communicate instead."
            )

        if tool_name == "TodoWrite":
            return PermissionResultAllow()

        if tool_name == "EnterPlanMode":
            return PermissionResultAllow()

        if tool_name == "ExitPlanMode":
            return await _handle_exit_plan_mode(session, tool_input)

        if tool_name == "AskUserQuestion":
            return await _handle_ask_user_question(session, tool_input)

        if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            resolved = os.path.realpath(path)
            for base in bases:
                if resolved == base or resolved.startswith(base + os.sep):
                    return PermissionResultAllow()
            return PermissionResultDeny(
                message=f"Access denied: {path} is outside working directory {allowed} and user data {user_data}"
            )
        return PermissionResultAllow()

    return _check_permission


_PLAN_FILE_MAX_AGE_SECS = 300  # Only consider plan files modified within the last 5 minutes

# Common plan file names agents write to their CWD
_CWD_PLAN_FILENAMES = ("PLAN.md", "plan.md")


def _read_latest_plan_file(cwd: str | None = None) -> str | None:
    """Read the most recently modified plan file.

    Searches two locations (returns the most recently modified match):
    1. The agent's CWD for PLAN.md / plan.md
    2. ~/.claude/plans/ for Claude Code's auto-generated plan files

    Claude Code writes plans to ~/.claude/plans/<random-name>.md when running
    in a terminal, but SDK agents often write PLAN.md to their CWD instead.
    """
    now = time.time()
    best: tuple[float, pathlib.Path] | None = None  # (mtime, path)

    # Check CWD for PLAN.md / plan.md
    if cwd:
        cwd_path = pathlib.Path(cwd)
        for name in _CWD_PLAN_FILENAMES:
            p = cwd_path / name
            try:
                mtime = p.stat().st_mtime
                if now - mtime <= _PLAN_FILE_MAX_AGE_SECS:
                    if best is None or mtime > best[0]:
                        best = (mtime, p)
            except OSError:
                continue

    # Check ~/.claude/plans/
    plans_dir = pathlib.Path.home() / ".claude" / "plans"
    if plans_dir.is_dir():
        try:
            candidates = sorted(
                plans_dir.glob("*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for path in candidates[:3]:
                try:
                    mtime = path.stat().st_mtime
                    if now - mtime > _PLAN_FILE_MAX_AGE_SECS:
                        break  # Sorted by mtime desc, no point checking older files
                    if best is None or mtime > best[0]:
                        best = (mtime, path)
                except OSError:
                    continue
        except OSError:
            pass

    if best is None:
        return None
    try:
        content = best[1].read_text(encoding="utf-8").strip()
        return content or None
    except OSError:
        return None


async def _handle_exit_plan_mode(
    session: AgentSession | None,
    tool_input: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    """Handle ExitPlanMode by posting the plan to Discord and waiting for user approval."""
    if session is None:
        return PermissionResultAllow()
    ds = discord_state(session)
    if ds.channel_id is None:
        return PermissionResultAllow()

    channel_id = ds.channel_id

    async def _send_plan_msg(content: str) -> None:
        await config.discord_client.send_message(channel_id, content)

    plan_content = (tool_input.get("plan") or "").strip() or None

    # Heuristic fallback: the LLM doesn't always include the plan in tool_input
    # (the "plan" key is an additionalProperty, not a defined schema field).
    # Claude Code writes plans to ~/.claude/plans/<name>.md (terminal) or
    # PLAN.md in the agent's CWD (SDK agents).  We check both locations.
    used_heuristic = False
    if not plan_content:
        plan_content = _read_latest_plan_file(cwd=session.cwd)
        if plan_content:
            used_heuristic = True
            log.info("Read plan from disk for '%s' (tool_input had no plan key)", session.name)

    header = f"\U0001f4cb **Plan from {session.name}** \u2014 waiting for approval"
    try:
        if plan_content:
            plan_bytes = plan_content.encode("utf-8")
            heuristic_note = (
                "\n*(Plan recovered from disk via heuristic — Claude Code bug omitted it from tool input)*"
                if used_heuristic
                else ""
            )
            await config.discord_client.send_file(channel_id, "plan.txt", plan_bytes, content=header + heuristic_note)
        else:
            await _send_plan_msg(
                f"{header}\n\n*(Plan file not found \u2014 the agent should have described the plan in its messages above.)*"
            )

        resp = await config.discord_client.send_message(
            channel_id,
            f"React with \u2705 to approve or \u274c to reject, or type feedback to revise the plan. {_user_mentions()}",
        )
        approval_msg_id = resp["id"]

        # Pre-react with approval/rejection emojis so the user can click them
        for emoji in ("\u2705", "\u274c"):
            await config.discord_client.add_reaction(channel_id, approval_msg_id, emoji)
        ds.plan_approval_message_id = int(approval_msg_id)
    except Exception:
        log.exception("_handle_exit_plan_mode: failed to post plan to Discord \u2014 denying")
        return PermissionResultDeny(message="Could not post plan to Discord for approval. Try again.")

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    ds.plan_approval_future = future  # type: ignore[assignment]
    schedule_status_update()

    log.info("Agent '%s' paused waiting for plan approval", session.name)

    # Stop the typing indicator while waiting for the user's decision
    _cancel_typing(ds)

    try:
        result = await future
    finally:
        ds.plan_approval_future = None
        ds.plan_approval_message_id = None
        schedule_status_update()

    # Remove the unchosen reaction so the result is visually clear
    remove_emoji = "\u274c" if result.get("approved") else "\u2705"
    try:
        await config.discord_client.remove_reaction(channel_id, approval_msg_id, remove_emoji)
    except Exception:
        log.debug("Failed to remove reaction from plan approval message", exc_info=True)

    if result.get("approved"):
        log.info("Agent '%s' plan approved by user", session.name)
        if session.plan_mode:
            session.plan_mode = False
            if session.client:
                try:
                    await session.client.set_permission_mode("default")
                    log.info("Agent '%s' permission mode reset to default after plan approval", session.name)
                except Exception:
                    log.exception("Failed to reset permission mode for '%s'", session.name)
        return PermissionResultAllow()
    else:
        message = result.get("message", "User rejected the plan.")
        log.info("Agent '%s' plan rejected: %s", session.name, message)
        return PermissionResultDeny(message=json.dumps(message) if not isinstance(message, str) else message)


# ---------------------------------------------------------------------------
# AskUserQuestion
# ---------------------------------------------------------------------------

# Keycap emoji for options 1-9
_NUMBER_EMOJI = [
    "1\ufe0f\u20e3",
    "2\ufe0f\u20e3",
    "3\ufe0f\u20e3",
    "4\ufe0f\u20e3",
    "5\ufe0f\u20e3",
    "6\ufe0f\u20e3",
    "7\ufe0f\u20e3",
    "8\ufe0f\u20e3",
    "9\ufe0f\u20e3",
]
_CUSTOM_EMOJI = "\U0001f4dd"  # 📝 for "Other"


def _format_question_for_discord(q: dict[str, Any], index: int, total: int) -> str:
    """Format a single AskUserQuestion question for Discord display."""
    prefix = f"**Question {index + 1}/{total}:** " if total > 1 else ""
    header = q.get("header", "")
    question_text = q.get("question", "")
    multi = q.get("multiSelect", False)

    lines: list[str] = []
    if header:
        lines.append(f"{prefix}[{header}] {question_text}")
    else:
        lines.append(f"{prefix}{question_text}")

    options = q.get("options", [])
    for i, opt in enumerate(options):
        emoji = _NUMBER_EMOJI[i] if i < len(_NUMBER_EMOJI) else f"**{i + 1}.**"
        label = opt.get("label", "")
        desc = opt.get("description", "")
        if desc:
            lines.append(f"  {emoji} {label} — {desc}")
        else:
            lines.append(f"  {emoji} {label}")

    lines.append(f"  {_CUSTOM_EMOJI} Other (type your own answer)")

    if multi:
        lines.append("\n*React to choose, or type a custom answer.*")
    else:
        lines.append("\n*React to choose, or type a custom answer.*")

    return "\n".join(lines)


def parse_question_answer(raw: str, question: dict[str, Any]) -> str:
    """Parse a user's text reply into an answer string for one question."""
    options = question.get("options", [])
    multi = question.get("multiSelect", False)
    stripped = raw.strip()

    if multi:
        parts = [p.strip() for p in stripped.split(",")]
        selected: list[str] = []
        for part in parts:
            try:
                idx = int(part)
                if 1 <= idx <= len(options):
                    selected.append(options[idx - 1].get("label", part))
                else:
                    selected.append(part)
            except ValueError:
                selected.append(part)
        return ", ".join(selected) if selected else stripped
    else:
        try:
            idx = int(stripped)
            if 1 <= idx <= len(options):
                return options[idx - 1].get("label", stripped)
        except ValueError:
            pass
        return stripped


def resolve_reaction_answer(emoji_str: str, question: dict[str, Any]) -> str | None:
    """Map a reaction emoji to an answer string. Returns None if unrecognized."""
    options = question.get("options", [])
    for i, e in enumerate(_NUMBER_EMOJI):
        if emoji_str == e and i < len(options):
            return options[i].get("label", str(i + 1))
    if emoji_str == _CUSTOM_EMOJI:
        return "Other"
    return None


async def _handle_ask_user_question(
    session: AgentSession | None,
    tool_input: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    """Handle AskUserQuestion by posting questions one at a time and waiting for each answer."""
    if session is None:
        return PermissionResultAllow()
    ds = discord_state(session)
    if ds.channel_id is None:
        return PermissionResultAllow()

    channel_id = ds.channel_id
    questions = tool_input.get("questions", [])
    if not questions:
        return PermissionResultAllow()

    loop = asyncio.get_running_loop()
    answers: dict[str, str] = {}

    try:
        header = f"\u2753 **{session.name}** is asking you a question {_user_mentions()}"
        await config.discord_client.send_message(channel_id, header)
    except Exception:
        log.exception("_handle_ask_user_question: failed to post header — denying")
        return PermissionResultDeny(message="Could not post question to Discord.")

    for i, q in enumerate(questions):
        # Post the question and get message ID
        try:
            formatted = _format_question_for_discord(q, i, len(questions))
            msg = await config.discord_client.send_message(channel_id, formatted)
            msg_id = int(msg["id"])
        except Exception:
            log.exception("_handle_ask_user_question: failed to post question %d — denying", i)
            return PermissionResultDeny(message="Could not post question to Discord.")

        # Pre-add reaction emojis for each option
        options = q.get("options", [])
        for j in range(min(len(options), len(_NUMBER_EMOJI))):
            try:
                await config.discord_client.add_reaction(channel_id, msg_id, _NUMBER_EMOJI[j])
            except Exception:
                log.debug("Failed to add reaction %d to question message", j + 1)

        # Set session state for this question
        discord_state(session).question_message_id = msg_id
        discord_state(session).question_data = q

        future: asyncio.Future[str] = loop.create_future()
        discord_state(session).question_future = future
        schedule_status_update()

        log.info("Agent '%s' waiting for answer to question %d/%d", session.name, i + 1, len(questions))

        # Stop the typing indicator while waiting for the user's answer
        _cancel_typing(ds)

        try:
            answer = await future
        finally:
            discord_state(session).question_future = None
            discord_state(session).question_message_id = None
            discord_state(session).question_data = None
            schedule_status_update()

        # Empty answer means interrupted (e.g. /stop)
        if not answer:
            break

        answers[q.get("question", "")] = answer

    log.info("Agent '%s' got answers: %s", session.name, answers)

    updated = dict(tool_input)
    updated["answers"] = answers
    return PermissionResultAllow(updated_input=updated)


# ---------------------------------------------------------------------------
# TodoWrite display
# ---------------------------------------------------------------------------

_TODO_STATUS = {"completed": "\u2705", "in_progress": "\U0001f504", "pending": "\u23f3"}


def format_todo_list(todos: list[dict[str, Any]]) -> str:
    """Format a todo list for Discord display."""
    lines: list[str] = []
    for item in todos:
        status = item.get("status", "pending")
        icon = _TODO_STATUS.get(status, "\u2b1c")
        content = item.get("content", "???")
        lines.append(f"{icon} {content}")
    return "\n".join(lines) or "*Empty todo list*"


def _todo_path(agent_name: str) -> str:
    """Path to the persisted todo state file for an agent."""
    return os.path.join(config.LOG_DIR, f"{agent_name}.todo.json")


def _save_todo_items(agent_name: str, todos: list[dict[str, Any]]) -> None:
    """Persist todo items to disk."""
    try:
        with open(_todo_path(agent_name), "w") as f:
            json.dump(todos, f)
    except OSError:
        log.warning("Failed to save todo state for '%s'", agent_name, exc_info=True)


def load_todo_items(agent_name: str) -> list[dict[str, Any]]:
    """Load persisted todo items from disk."""
    try:
        with open(_todo_path(agent_name)) as f:
            data: list[dict[str, Any]] = json.load(f)
        return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


async def _post_todo_list(session: AgentSession, tool_input: dict[str, Any]) -> None:
    """Post the updated todo list as a new message in Discord."""
    todos = tool_input.get("todos", [])
    ds = discord_state(session)
    ds.todo_items = todos
    _save_todo_items(session.name, todos)
    body = f"**Todo List**\n{format_todo_list(todos)}"
    channel_id = ds.channel_id
    if channel_id is None:
        return

    try:
        await config.discord_client.send_message(channel_id, body)
    except Exception:
        log.exception("Failed to post todo list for agent '%s'", session.name)


# ---------------------------------------------------------------------------
# Rate limiting adapter
# ---------------------------------------------------------------------------


async def _handle_rate_limit(error_text: str, session: AgentSession, channel: TextChannel) -> None:
    """Handle a rate limit error: set global state, notify all agent channels."""
    assert _bot is not None
    bot_ref = _bot

    async def _broadcast(msg_text: str) -> None:
        notified_channels: set[int] = set()
        for agent_session in agents.values():
            ads = discord_state(agent_session)
            if not ads.channel_id:
                continue
            ch = bot_ref.get_channel(ads.channel_id)
            if isinstance(ch, TextChannel) and ch.id not in notified_channels:
                notified_channels.add(ch.id)
                try:
                    await send_system(ch, msg_text)
                except Exception:
                    log.warning("Failed to notify channel %s about rate limit", ch.id)

    def _schedule_expiry(delay: float) -> None:
        fire_and_forget(notify_rate_limit_expired(delay, get_master_channel, send_system))

    await _rl_handle_rate_limit(error_text, _broadcast, _schedule_expiry)


# ---------------------------------------------------------------------------
# Shutdown coordinator init
# ---------------------------------------------------------------------------


async def _notify_agent_channel(agent_name: str, message: str) -> None:
    """Notify an agent's Discord channel with a system message."""
    channel = await get_agent_channel(agent_name)
    if channel:
        await send_system(channel, message)


def make_shutdown_coordinator(
    *,
    close_bot_fn: Any,
    kill_fn: Any,
    goodbye_fn: Any,
    bridge_mode: bool,
) -> ShutdownCoordinator:
    """Create a ShutdownCoordinator with standard agents/sleep/notify wiring."""
    return ShutdownCoordinator(
        agents=agents,
        sleep_fn=lambda s: sleep_agent(s, force=True),
        close_bot_fn=close_bot_fn,
        kill_fn=kill_fn,
        notify_fn=_notify_agent_channel,
        goodbye_fn=goodbye_fn,
        bridge_mode=bridge_mode,
    )


def init_shutdown_coordinator() -> None:
    """Wire up the ShutdownCoordinator with real bot callbacks.

    Called once from on_ready after all helpers are defined.
    """
    global shutdown_coordinator

    assert _bot is not None

    async def _send_goodbye() -> None:
        master_ch = await get_master_channel()
        if master_ch:
            await master_ch.send("*System:* Shutting down \u2014 see you soon!")

    bot_ref = _bot

    async def _close_bot() -> None:
        shutdown_tracing()
        await bot_ref.close()

    use_bridge = procmux_conn is not None and procmux_conn.is_alive
    shutdown_coordinator = make_shutdown_coordinator(
        close_bot_fn=_close_bot,
        kill_fn=exit_for_restart if use_bridge else kill_supervisor,
        goodbye_fn=_send_goodbye,
        bridge_mode=use_bridge,
    )


# ---------------------------------------------------------------------------
# Lifecycle: wake, sleep, reset, reconstruct
# ---------------------------------------------------------------------------


def is_awake(session: AgentSession) -> bool:
    """Check if agent is ready to process messages."""
    return session.client is not None


def is_processing(session: AgentSession) -> bool:
    """Check if agent has active work."""
    return session.query_lock.locked()


def _reset_session_activity(session: AgentSession) -> None:
    """Reset idle tracking and activity state for the start of a new query."""
    session.last_activity = datetime.now(UTC)
    ds = discord_state(session)
    ds.last_idle_notified = None
    ds.task_done = False
    ds.task_error = False
    session.idle_reminder_count = 0
    session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))
    schedule_status_update()


# ---------------------------------------------------------------------------
# Session lifecycle internals
# ---------------------------------------------------------------------------


async def _schema_validation_callback(msg: dict[str, Any], errors: list[Any]) -> None:
    """Report schema validation errors to #exceptions for visibility."""
    error_strs = [str(e) for e in errors[:5]]
    error_summary = "\n".join(error_strs)
    preview = json.dumps(msg)[:400]
    msg_type = msg.get("type", "?")
    await send_to_exceptions(
        f"\u26a0\ufe0f Schema validation ({msg_type}):\n{error_summary}\n```json\n{preview}\n```"
    )


async def create_transport(session: AgentSession, reconnecting: bool = False):
    """Create a transport for Claude Code agent (bridge or direct)."""
    if wire_conn and wire_conn.is_alive:
        transport = BridgeTransport(
            session.name,
            wire_conn,
            reconnecting=reconnecting,
            stderr_callback=make_stderr_callback(session),
            stdio_logger=get_stdio_logger(session.name, config.LOG_DIR),
            on_validation_error=_schema_validation_callback,
        )
        await transport.connect()
        return transport
    else:
        return None


# Alias for callers within this module
_disconnect_client = disconnect_client


# ---------------------------------------------------------------------------
# Concurrency management
# ---------------------------------------------------------------------------


def count_awake_agents() -> int:
    """Count agents that are currently awake."""
    return sum(1 for s in agents.values() if s.client is not None)


# ---------------------------------------------------------------------------
# Sleep / wake — delegate to hub lifecycle
# ---------------------------------------------------------------------------



async def sleep_agent(session: AgentSession, *, force: bool = False) -> None:
    """Shut down an agent. Delegates to hub lifecycle.

    If force=False (default), skips sleeping if the agent's query_lock is held.
    """
    from agenthub import lifecycle

    assert hub is not None
    await lifecycle.sleep_agent(hub, session, force=force)
    schedule_status_update()


async def wake_agent(session: AgentSession) -> None:
    """Wake a sleeping agent. Delegates core lifecycle to hub, then handles Discord post-wake."""
    from agenthub import lifecycle

    assert _bot is not None
    assert hub is not None

    if is_awake(session):
        return

    resume_id = session.session_id
    await lifecycle.wake_agent(hub, session)

    # --- Discord-specific post-wake logic ---

    # Prompt change detection
    prompt_changed = False
    if resume_id and session.system_prompt is not None:
        current_hash = compute_prompt_hash(session.system_prompt)
        if session.system_prompt_hash is not None and current_hash != session.system_prompt_hash:
            prompt_changed = True
            log.info(
                "System prompt changed for '%s' (old=%s, new=%s)",
                session.name,
                session.system_prompt_hash,
                current_hash,
            )
        session.system_prompt_hash = current_hash

    # Post system prompt to Discord on first wake
    ds = discord_state(session)
    if not ds.system_prompt_posted and ds.channel_id:
        ds.system_prompt_posted = True
        channel = _bot.get_channel(ds.channel_id)
        if channel and isinstance(channel, TextChannel):
            try:
                await post_system_prompt_to_channel(
                    channel,
                    session.system_prompt,
                    is_resume=bool(resume_id),
                    prompt_changed=prompt_changed,
                    session_id=session.session_id or resume_id,
                )
            except Exception:
                log.warning(
                    "Failed to post system prompt to Discord for '%s'",
                    session.name,
                    exc_info=True,
                )

    await _post_model_warning(session)
    schedule_status_update()


async def wake_or_queue(
    session: AgentSession,
    content: MessageContent,
    channel: TextChannel,
    orig_message: discord.Message | None,
) -> bool:
    """Try to wake agent, return True if successful, False if queued.

    Adds a ⏳ reaction immediately so the user knows the message was received
    while we wait for a slot / SDK client creation.  The reaction is removed
    on success or replaced with 📨/❌ on failure.
    """
    # Immediate feedback — user sees we received the message
    await add_reaction(orig_message, "\u23f3")

    # Check cwd exists before attempting wake — avoids slow SDK failure
    # when the working directory has been deleted (e.g. removed worktree)
    if session.cwd and not os.path.isdir(session.cwd):
        log.error("Agent '%s' cwd does not exist: %s", session.name, session.cwd)
        await remove_reaction(orig_message, "\u23f3")
        await add_reaction(orig_message, "\u274c")
        await send_system(
            channel,
            f"Agent **{session.name}** working directory no longer exists: `{session.cwd}`\n"
            f"Kill with `/kill-agent {session.name}` and respawn.",
        )
        return False

    try:
        await wake_agent(session)
        # Woke successfully — remove the waiting indicator
        await remove_reaction(orig_message, "\u23f3")
        return True
    except ConcurrencyLimitError:
        session.message_queue.append((content, channel, orig_message))
        position = len(session.message_queue)
        awake = count_awake_agents()
        log.debug("Concurrency limit hit for '%s', queuing message (position %d)", session.name, position)
        # Swap ⏳ → 📨 to indicate "queued, will process later"
        await remove_reaction(orig_message, "\u23f3")
        await add_reaction(orig_message, "\U0001f4e8")
        await send_system(channel, f"\u23f3 All {awake} agent slots busy. Message queued (position {position}).")
        return False
    except Exception:
        log.exception("Failed to wake agent '%s'", session.name)
        await remove_reaction(orig_message, "\u23f3")
        await add_reaction(orig_message, "\u274c")
        await send_system(
            channel, f"Failed to wake agent **{session.name}**. Try `/kill-agent {session.name}` and respawn."
        )
        return False


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


async def end_session(name: str) -> None:
    """End a named Claude session and remove it from the registry."""
    session = agents.get(name)
    if session is None:
        return
    if session.client is not None:
        await _disconnect_client(session.client, name)
        session.client = None
        scheduler.release_slot(name)
    _close_agent_log(session)
    agents.pop(name, None)
    log.info("Session '%s' ended", name)


async def _rebuild_session(name: str, *, cwd: str | None = None, session_id: str | None = None) -> AgentSession:
    """End an existing session and create a fresh sleeping AgentSession.

    Preserves system prompt, channel mapping, and MCP servers from the old session.
    """
    session = agents.get(name)
    old_cwd = session.cwd if session else config.DEFAULT_CWD
    old_channel_id = discord_state(session).channel_id if session else None
    old_mcp = getattr(session, "mcp_servers", None)
    resolved_cwd = cwd or old_cwd
    prompt = (
        session.system_prompt if session and session.system_prompt else make_spawned_agent_system_prompt(resolved_cwd)
    )
    prompt_hash = session.system_prompt_hash if session and session.system_prompt_hash else compute_prompt_hash(prompt)
    await end_session(name)
    new_session = AgentSession(
        name=name,
        cwd=resolved_cwd,
        system_prompt=prompt,
        system_prompt_hash=prompt_hash,
        client=None,
        session_id=session_id,
        mcp_servers=old_mcp,
    )
    discord_state(new_session).channel_id = old_channel_id
    agents[name] = new_session
    return new_session


async def reset_session(name: str, cwd: str | None = None) -> AgentSession:
    """Reset a named session. Preserves system prompt, channel mapping, and MCP servers."""
    new_session = await _rebuild_session(name, cwd=cwd)
    log.info("Session '%s' reset (sleeping, cwd=%s)", name, new_session.cwd)
    return new_session


async def restart_agent(name: str) -> AgentSession:
    """Restart an agent's CLI process with a fresh system prompt, preserving session context.

    Sleeps the agent (disconnects the SDK client), rebuilds the system prompt
    from scratch (picks up SYSTEM_PROMPT.md changes, pack changes, etc.), and
    leaves the agent sleeping.  The next message will wake it with the new prompt
    and the same session ID (conversation context preserved via resume).
    """
    session = agents.get(name)
    if session is None:
        raise ValueError(f"Agent '{name}' not found")

    session_id = session.session_id

    # Sleep the agent (disconnect CLI)
    if is_awake(session):
        await sleep_agent(session, force=True)

    # Rebuild system prompt from scratch
    agent_cfg = _load_agent_config(name)
    saved_packs = agent_cfg.get("packs")
    new_prompt = make_spawned_agent_system_prompt(
        session.cwd, packs=saved_packs, compact_instructions=session.compact_instructions
    )

    # Update session in place — preserves channel mapping, MCP servers, queue, etc.
    session.system_prompt = new_prompt
    session.system_prompt_hash = compute_prompt_hash(new_prompt)
    session.session_id = session_id  # ensure session ID preserved for resume

    # Reset prompt-posted flag so the new prompt gets posted to Discord on next wake
    discord_state(session).system_prompt_posted = False

    log.info("Agent '%s' restarted (session=%s, new prompt hash=%s)", name, session_id, session.system_prompt_hash)
    return session


def get_master_session() -> AgentSession | None:
    """Get the axi-master session."""
    return agents.get(config.MASTER_AGENT_NAME)


async def reconstruct_agents_from_channels() -> int:
    """Reconstruct sleeping AgentSession entries from existing Discord channels."""
    reconstructed = 0
    categories = [c for c in (_channels_mod.axi_category, _channels_mod.active_category) if c is not None]
    if not categories:
        return reconstructed

    for cat in categories:
        for ch in cat.text_channels:
            agent_name = _channels_mod.strip_status_prefix(ch.name) if config.CHANNEL_STATUS_ENABLED else ch.name

            if agent_name == normalize_channel_name(config.MASTER_AGENT_NAME):
                channel_to_agent[ch.id] = config.MASTER_AGENT_NAME
                continue

            if agent_name in agents:
                channel_to_agent[ch.id] = agent_name
                continue

            cwd, session_id, old_prompt_hash, agent_type = _parse_channel_topic(ch.topic)
            if cwd is None:
                log.debug("No cwd in topic for channel #%s, skipping", agent_name)
                continue

            agent_cfg = _load_agent_config(agent_name)
            saved_packs = agent_cfg.get("packs")  # None = use defaults
            prompt = make_spawned_agent_system_prompt(cwd, packs=saved_packs)
            mcp_names = agent_cfg.get("mcp_servers") or None
            extra_mcp = config.load_mcp_servers(mcp_names) if mcp_names else None
            mcp_servers = _build_mcp_servers(agent_name, cwd, extra_mcp_servers=extra_mcp)

            session = AgentSession(
                name=agent_name,
                agent_type=agent_type or "flowcoder",
                client=None,
                cwd=cwd,
                system_prompt=prompt,
                system_prompt_hash=old_prompt_hash,
                session_id=session_id,
                mcp_servers=mcp_servers,
                mcp_server_names=mcp_names,
            )
            ds = discord_state(session)
            ds.channel_id = ch.id
            ds.todo_items = load_todo_items(agent_name)
            agents[agent_name] = session
            channel_to_agent[ch.id] = agent_name
            reconstructed += 1
            log.info(
                "Reconstructed agent '%s' from #%s (category=%s, type=%s, session_id=%s, prompt_hash=%s)",
                agent_name,
                ch.name,
                cat.name,
                session.agent_type,
                session_id,
                old_prompt_hash,
            )

    log.info("Reconstructed %d agent(s) from channels", reconstructed)
    return reconstructed


# ---------------------------------------------------------------------------
# Session ID persistence
# ---------------------------------------------------------------------------


def _update_channel_topic(session: AgentSession, channel: TextChannel | None = None) -> None:
    """Update the Discord channel topic with current session metadata (spawned agents only)."""
    assert _bot is not None
    ds = discord_state(session)
    if session.name == config.MASTER_AGENT_NAME or not ds.channel_id:
        return
    ch = channel or _bot.get_channel(ds.channel_id)
    if not ch or not isinstance(ch, TextChannel):
        return
    desired_topic = format_channel_topic(
        session.cwd,
        session.session_id,
        session.system_prompt_hash,
        agent_type=session.agent_type,
    )
    if ch.topic != desired_topic:
        log.info("Updating topic on #%s: %r -> %r", ch.name, ch.topic, desired_topic)

        async def _do_update(c: Any, t: str) -> None:
            try:
                await c.edit(topic=t)
            except Exception:
                log.warning("Failed to update topic on #%s", c.name, exc_info=True)

        fire_and_forget(_do_update(ch, desired_topic))


def _save_master_session(session: AgentSession) -> None:
    """Save master agent session metadata (session_id, prompt_hash) to disk."""
    try:
        data: dict[str, Any] = {}
        if session.session_id:
            data["session_id"] = session.session_id
        if session.system_prompt_hash:
            data["prompt_hash"] = session.system_prompt_hash
        with open(config.MASTER_SESSION_PATH, "w") as f:
            json.dump(data, f)
        log.info("Saved master session data to %s", config.MASTER_SESSION_PATH)
    except OSError:
        log.warning("Failed to save master session data", exc_info=True)


async def _set_session_id(session: AgentSession, msg_or_sid: Any, channel: TextChannel | None = None) -> None:
    """Update session's session_id and persist it (topic or file).

    Skips persisting if the session_id matches one that previously failed resume,
    to prevent an infinite stale-ID cycle (Claude Code reuses session IDs per
    project, so a fresh session returns the same ID that failed to resume).
    """
    assert _bot is not None
    sid: str | None = msg_or_sid if isinstance(msg_or_sid, str) else getattr(msg_or_sid, "session_id", None)
    failed_id = session.last_failed_resume_id
    if sid and failed_id and sid == failed_id:
        # Don't persist a session_id that previously failed resume —
        # it would cause the same failure on next wake.
        log.debug("Skipping session_id update for '%s': %s matches failed resume ID", session.name, sid[:8])
        return
    if sid and sid != session.session_id:
        session.session_id = sid
        if session.name == config.MASTER_AGENT_NAME:
            _save_master_session(session)
        else:
            _update_channel_topic(session, channel)
    else:
        session.session_id = sid


# ---------------------------------------------------------------------------
# Model warning
# ---------------------------------------------------------------------------


async def _post_model_warning(session: AgentSession) -> None:
    """Post a warning to Discord if the agent is running on a non-opus model."""
    assert _bot is not None
    model = config.get_model()
    ds = discord_state(session)
    if model == "opus" or not ds.channel_id:
        return
    channel = _bot.get_channel(ds.channel_id)
    if channel and isinstance(channel, TextChannel):
        try:
            await channel.send(
                f"\u26a0\ufe0f Running on **{model}** \u2014 switch to opus with `/model opus` for best results."
            )
        except Exception:
            log.warning("Failed to post model warning for '%s'", session.name, exc_info=True)


# ---------------------------------------------------------------------------
# Response streaming: read Claude SDK messages and relay to Discord
# ---------------------------------------------------------------------------


async def _receive_response_safe(session: AgentSession):
    """Wrapper around receive_messages() that handles unknown message types."""
    assert session.client is not None
    assert session.client._query is not None  # pyright: ignore[reportPrivateUsage]
    async for data in session.client._query.receive_messages():  # pyright: ignore[reportPrivateUsage]
        try:
            parsed = parse_message(data)
        except MessageParseError:
            msg_type = data.get("type", "?")
            if msg_type == "rate_limit_event":
                log.info("Rate limit event for '%s': %s", session.name, data)
                if session.agent_log:
                    session.agent_log.info("RATE_LIMIT_EVENT: %s", json.dumps(data)[:500])
                _update_rate_limit_quota(data)
            else:
                log.warning(
                    "Unknown SDK message type from '%s': type=%s data=%s",
                    session.name,
                    msg_type,
                    json.dumps(data)[:500],
                )
                if session.agent_log:
                    session.agent_log.warning("UNKNOWN_MSG: type=%s data=%s", msg_type, json.dumps(data)[:500])
                preview = json.dumps(data)[:400]
                await send_to_exceptions(
                    f"\u26a0\ufe0f Unknown SDK message type `{msg_type}` from **{session.name}**:\n```json\n{preview}\n```"
                )
            continue
        yield parsed
        if isinstance(parsed, ResultMessage):
            return


# ---------------------------------------------------------------------------
# Activity tracking
# ---------------------------------------------------------------------------


def _update_activity(session: AgentSession, event: dict[str, Any]) -> None:
    """Update the agent's activity state from a raw Anthropic stream event."""
    update_activity(session.activity, event)


def extract_tool_preview(tool_name: str, raw_json: str) -> str | None:
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
            return f"{data.get('pattern', '')}"[:100]
    except (json.JSONDecodeError, TypeError):
        if tool_name == "Bash":
            match = re.search(r'"command"\s*:\s*"([^"]*)', raw_json)
            if match:
                return match.group(1)[:100]
        elif tool_name in ("Read", "Write", "Edit"):
            match = re.search(r'"file_path"\s*:\s*"([^"]*)', raw_json)
            if match:
                return match.group(1)[:100]
    return None


# ---------------------------------------------------------------------------
# Stream context + helpers
# ---------------------------------------------------------------------------


class _LiveEditState:
    """Tracks the Discord message currently being live-edited during streaming."""

    __slots__ = (
        "channel_id",
        "content",
        "edit_pending",
        "finalized",
        "last_edit_time",
        "message_id",
    )

    def __init__(self, channel_id: int) -> None:
        self.channel_id: int = channel_id
        self.message_id: str | None = None  # Set after first message is posted
        self.content: str = ""  # Full content of the current message
        self.last_edit_time: float = 0.0  # monotonic timestamp of last edit
        self.finalized: bool = False  # True once this message is complete
        self.edit_pending: bool = False  # True if content changed since last edit


class _StreamCtx:
    """Mutable state for a single stream_response_to_channel invocation."""

    __slots__ = (
        "deferred_msg",
        "flush_count",
        "got_result",
        "hit_rate_limit",
        "hit_transient_error",
        "in_flowchart",
        "last_flushed_channel_id",
        "last_flushed_content",
        "last_flushed_msg_id",
        "live_edit",
        "msg_total",
        "text_buffer",
        "thinking_message",
        "tool_input_json",
        "tool_msg_ids_to_delete",
        "tool_progress_channel_id",
        "tool_progress_msg_id",
        "typing_stopped",
    )

    def __init__(self, live_edit: _LiveEditState | None = None) -> None:
        self.text_buffer: str = ""
        self.got_result: bool = False  # True once a ResultMessage is received
        self.hit_rate_limit: bool = False
        self.hit_transient_error: str | None = None
        self.typing_stopped: bool = False
        self.flush_count: int = 0
        self.msg_total: int = 0
        self.tool_input_json: str = ""  # Accumulates full tool input JSON for current tool_use block
        self.in_flowchart: bool = False  # True during flowchart execution (protects session_id)
        self.live_edit: _LiveEditState | None = live_edit  # Set when STREAMING_DISCORD is enabled
        self.thinking_message: discord.Message | None = None  # Temporary "thinking..." indicator
        self.last_flushed_msg_id: str | None = None  # ID of the last Discord message sent/edited
        self.last_flushed_channel_id: int | None = None
        self.last_flushed_content: str = ""  # Content of the last flushed message
        self.deferred_msg: str = ""  # Non-streaming: holds back the last message for timing append
        # Clean tool messages: temporary progress indicator
        self.tool_progress_msg_id: str | None = None  # Current progress message Discord ID
        self.tool_progress_channel_id: int | None = None
        self.tool_msg_ids_to_delete: list[str] = []  # Message IDs to delete at end of turn


async def _flush_text(ctx: _StreamCtx, session: AgentSession, channel: TextChannel, reason: str = "?") -> None:
    """Flush accumulated text buffer to Discord.

    When live-edit streaming is active, this finalizes the current streaming
    message (ensuring content is up to date) and resets the live-edit state
    so the next text block starts a new message.
    """
    text = ctx.text_buffer
    if not text.strip():
        return
    ctx.flush_count += 1
    log.info(
        "FLUSH[%s] #%d reason=%s len=%d text=%r",
        session.name,
        ctx.flush_count,
        reason,
        len(text.strip()),
        text.strip()[:120],
    )

    le = ctx.live_edit
    if le is not None:
        # Streaming mode: finalize the current live-edit message(s)
        await _live_edit_finalize(ctx, session)
    else:
        # Non-streaming: send any previously deferred message, then defer the current one.
        # This holds back the last message so timing can be appended before sending.
        if ctx.deferred_msg:
            last_msg = await send_long(channel, ctx.deferred_msg)
            if last_msg is not None:
                ctx.last_flushed_msg_id = str(last_msg.id)
                ctx.last_flushed_channel_id = channel.id
                ctx.last_flushed_content = last_msg.content
        ctx.deferred_msg = text.lstrip()


# ---------------------------------------------------------------------------
# Live-edit streaming helpers (STREAMING_DISCORD)
# ---------------------------------------------------------------------------

_STREAMING_CURSOR = "\u2588"  # Block cursor to indicate "still typing"
_STREAMING_MSG_LIMIT = 1900  # Leave room for cursor and splitting overhead


async def _live_edit_post(le: _LiveEditState, content: str, session: AgentSession) -> None:
    """Post a new message via REST and record its ID in the live-edit state."""
    try:
        resp = await config.discord_client.send_message(le.channel_id, content)
        le.message_id = resp["id"]
        le.content = content
        le.last_edit_time = time.monotonic()
        le.edit_pending = False
        log.debug("LIVE_EDIT_POST[%s] msg_id=%s len=%d", session.name, le.message_id, len(content))
    except Exception:
        log.exception("LIVE_EDIT_POST[%s] failed to post initial message", session.name)
        raise


async def _live_edit_update(le: _LiveEditState, content: str, session: AgentSession) -> None:
    """Edit the current live-edit message with new content."""
    if le.message_id is None:
        return
    try:
        await config.discord_client.edit_message(le.channel_id, le.message_id, content)
        le.content = content
        le.last_edit_time = time.monotonic()
        le.edit_pending = False
        log.debug("LIVE_EDIT_UPDATE[%s] msg_id=%s len=%d", session.name, le.message_id, len(content))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            retry_after = exc.response.json().get("retry_after", 2.0)
            log.warning("LIVE_EDIT_UPDATE[%s] rate limited, backing off %.1fs", session.name, retry_after)
            le.last_edit_time = time.monotonic() + retry_after
            le.edit_pending = True
        else:
            log.warning("LIVE_EDIT_UPDATE[%s] edit failed: %s", session.name, exc)
    except Exception:
        log.warning("LIVE_EDIT_UPDATE[%s] edit failed", session.name, exc_info=True)


async def _live_edit_tick(ctx: _StreamCtx, session: AgentSession) -> None:
    """Called on each text_delta. Posts or edits the message if enough time has passed.

    Flow:
    1. If no message exists yet, post the first chunk immediately.
    2. If content exceeds the limit, finalize current message and start a new one.
    3. If enough time has passed since last edit, edit with current content + cursor.
    """
    le = ctx.live_edit
    if le is None or le.finalized:
        return

    text = ctx.text_buffer.lstrip()
    if not text:
        return

    now = time.monotonic()

    # First message: post immediately
    if le.message_id is None:
        await _live_edit_post(le, text + _STREAMING_CURSOR, session)
        return

    # Content exceeds limit: finalize current message at a good split point, start new
    if len(text) > _STREAMING_MSG_LIMIT:
        split_at = text.rfind("\n", 0, _STREAMING_MSG_LIMIT)
        if split_at == -1:
            split_at = _STREAMING_MSG_LIMIT
        # Finalize the current message with content up to split point (no cursor)
        final_content = text[:split_at]
        await _live_edit_update(le, final_content, session)
        # Reset buffer to remainder and start a new message
        ctx.text_buffer = text[split_at:].lstrip("\n")
        le.message_id = None
        le.content = ""
        le.edit_pending = False
        # Post the remainder as a new message if there's content
        remainder = ctx.text_buffer.lstrip()
        if remainder:
            await _live_edit_post(le, remainder + _STREAMING_CURSOR, session)
        return

    # Throttled edit: only update if enough time has passed
    if now - le.last_edit_time >= config.STREAMING_EDIT_INTERVAL:
        await _live_edit_update(le, text + _STREAMING_CURSOR, session)


async def _live_edit_finalize(ctx: _StreamCtx, session: AgentSession) -> None:
    """Finalize the current live-edit message: remove cursor, post any remaining content."""
    le = ctx.live_edit
    if le is None:
        return

    text = ctx.text_buffer.lstrip()

    if le.message_id is not None and text:
        # Final edit: remove the cursor character
        # If text is too long, we need to split
        chunks = split_message(text)
        if len(chunks) == 1:
            await _live_edit_update(le, chunks[0], session)
            ctx.last_flushed_msg_id = le.message_id
            ctx.last_flushed_channel_id = le.channel_id
            ctx.last_flushed_content = chunks[0]
        else:
            # First chunk goes into the existing message
            await _live_edit_update(le, chunks[0], session)
            # Remaining chunks are new messages
            for chunk in chunks[1:]:
                resp = await config.discord_client.send_message(le.channel_id, chunk)
                ctx.last_flushed_msg_id = resp["id"]
                ctx.last_flushed_channel_id = le.channel_id
                ctx.last_flushed_content = chunk
    elif le.message_id is None and text:
        # Never posted — just send normally
        for chunk in split_message(text):
            resp = await config.discord_client.send_message(le.channel_id, chunk)
            ctx.last_flushed_msg_id = resp["id"]
            ctx.last_flushed_channel_id = le.channel_id
            ctx.last_flushed_content = chunk

    # Reset for next text block
    le.message_id = None
    le.content = ""
    le.edit_pending = False
    le.finalized = False


async def _show_thinking(ctx: _StreamCtx, channel: TextChannel) -> None:
    """Send a temporary 'thinking...' indicator message."""
    if ctx.thinking_message is None:
        try:
            ctx.thinking_message = await channel.send("*thinking...*")
        except Exception:
            log.debug("Failed to send thinking indicator", exc_info=True)


async def _hide_thinking(ctx: _StreamCtx) -> None:
    """Delete the temporary thinking indicator message."""
    msg = ctx.thinking_message
    if msg is not None:
        ctx.thinking_message = None
        try:
            await msg.delete()
        except Exception:
            log.debug("Failed to delete thinking indicator", exc_info=True)


def _cancel_typing(ds: DiscordAgentState) -> None:
    """Cancel the typing indicator on a DiscordAgentState.

    Used by permission callbacks (AskUserQuestion, ExitPlanMode) to stop
    the typing indicator while waiting for user input.  The streaming loop
    will restart typing when the next stream event arrives.
    """
    typing_obj = ds.typing_obj
    if typing_obj and hasattr(typing_obj, "task"):
        typing_obj.task.cancel()


def _stop_typing(ctx: _StreamCtx, session: AgentSession) -> None:
    """Cancel the typing indicator stored on the session's DiscordAgentState."""
    if ctx.typing_stopped:
        return
    ds = discord_state(session)
    _cancel_typing(ds)
    ctx.typing_stopped = True



# ---------------------------------------------------------------------------
# Clean tool messages: temporary progress indicator
# ---------------------------------------------------------------------------

# Tools whose messages should NOT be deleted (they produce persistent user-facing output)
_PERSISTENT_TOOLS = {"EnterPlanMode", "ExitPlanMode", "AskUserQuestion", "TodoWrite"}


async def _show_tool_progress(
    ctx: _StreamCtx, session: AgentSession, channel: TextChannel
) -> None:
    """Post or update a temporary tool progress message in Discord.

    Called when CLEAN_TOOL_MESSAGES is enabled and a non-persistent tool starts executing.
    Reuses the same message for successive tools (edit in-place).
    """
    tool = session.activity.tool_name
    if not tool or tool in _PERSISTENT_TOOLS:
        return

    display = tool_display(tool)
    preview = extract_tool_preview(tool, session.activity.tool_input_preview)
    if preview:
        text = f"\u2699 *{display}:* `{preview[:120]}`"
    else:
        text = f"\u2699 *{display}...*"

    channel_id = discord_state(session).channel_id or channel.id
    try:
        if ctx.tool_progress_msg_id is not None and ctx.tool_progress_channel_id is not None:
            # Edit existing progress message in-place
            log.info("TOOL_PROGRESS[%s] edit tool=%s text=%r", session.name, tool, text[:80])
            await config.discord_client.edit_message(
                ctx.tool_progress_channel_id, ctx.tool_progress_msg_id, text
            )
        else:
            # Post a new progress message
            log.info("TOOL_PROGRESS[%s] post tool=%s text=%r", session.name, tool, text[:80])
            resp = await config.discord_client.send_message(channel_id, text)
            msg_id: str = resp["id"]
            ctx.tool_progress_msg_id = msg_id
            ctx.tool_progress_channel_id = channel_id
            if msg_id not in ctx.tool_msg_ids_to_delete:
                ctx.tool_msg_ids_to_delete.append(msg_id)
    except Exception:
        log.warning("Failed to post/edit tool progress for '%s'", session.name, exc_info=True)


async def _delete_tool_progress_messages(ctx: _StreamCtx) -> None:
    """Delete all temporary tool progress messages. Best-effort."""
    log.info("TOOL_CLEANUP deleting %d progress message(s)", len(ctx.tool_msg_ids_to_delete))
    for msg_id in ctx.tool_msg_ids_to_delete:
        try:
            if ctx.tool_progress_channel_id is not None:
                await config.discord_client.delete_message(
                    ctx.tool_progress_channel_id, msg_id
                )
                log.debug("TOOL_CLEANUP deleted msg_id=%s", msg_id)
        except Exception:
            log.warning("Failed to delete tool progress message %s", msg_id, exc_info=True)
    ctx.tool_msg_ids_to_delete.clear()
    ctx.tool_progress_msg_id = None


# ---------------------------------------------------------------------------
# Stream event handlers
# ---------------------------------------------------------------------------


async def _handle_stream_event(
    ctx: _StreamCtx, session: AgentSession, channel: TextChannel, msg: StreamEvent,
) -> None:
    """Handle a StreamEvent during response streaming."""
    event = msg.event
    event_type = event.get("type", "")

    if not ctx.in_flowchart and msg.session_id and msg.session_id != session.session_id:
        await _set_session_id(session, msg.session_id, channel=channel)

    _update_activity(session, event)

    # Thinking indicator — show/hide based on phase transitions
    if event_type == "content_block_start":
        block = event.get("content_block", {})
        block_type = block.get("type", "")
        if block_type == "thinking":
            await _show_thinking(ctx, channel)
        elif ctx.thinking_message is not None:
            # New non-thinking block started — hide the indicator
            await _hide_thinking(ctx)

    # Track tool input JSON for TodoWrite display
    if event_type == "content_block_start":
        block = event.get("content_block", {})
        if block.get("type") == "tool_use":
            ctx.tool_input_json = ""
    elif event_type == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "input_json_delta":
            ctx.tool_input_json += delta.get("partial_json", "")

    # TodoWrite display — post/update todo list in Discord
    if event_type == "content_block_stop" and session.activity.phase == "waiting":
        if session.activity.tool_name == "TodoWrite":
            try:
                tool_input: dict[str, Any] = json.loads(ctx.tool_input_json) if ctx.tool_input_json else {}
                await _post_todo_list(session, tool_input)
            except Exception:
                log.exception("Failed to parse/post TodoWrite for '%s'", session.name)
        ctx.tool_input_json = ""

    # Clean tool messages: show temporary progress indicator
    if config.CLEAN_TOOL_MESSAGES and event_type == "content_block_stop":
        if session.activity.phase == "waiting" and session.activity.tool_name:
            await _show_tool_progress(ctx, session, channel)

    # When text starts, reset tool progress pointer (text supersedes progress)
    if config.CLEAN_TOOL_MESSAGES and event_type == "content_block_start":
        block = event.get("content_block", {})
        if block.get("type") == "text":
            ctx.tool_progress_msg_id = None

    # Debug output (suppressed for tool calls when CLEAN_TOOL_MESSAGES handles them)
    if discord_state(session).debug and event_type == "content_block_stop":
        if session.activity.phase == "thinking" and session.activity.thinking_text:
            thinking = session.activity.thinking_text.strip()
            if thinking:
                file = discord.File(io.BytesIO(thinking.encode("utf-8")), filename="thinking.md")
                await channel.send("\U0001f4ad", file=file)
                session.activity.thinking_text = ""
        elif session.activity.phase == "waiting" and session.activity.tool_name:
            if not config.CLEAN_TOOL_MESSAGES:
                tool = session.activity.tool_name
                preview = extract_tool_preview(tool, session.activity.tool_input_preview)
                if preview:
                    await channel.send(f"`\U0001f527 {tool}: {preview[:120]}`")
                else:
                    await channel.send(f"`\U0001f527 {tool}`")

    # Log stream events
    if session.agent_log:
        _log_stream_event(session, event_type, event)

    # Raw stdio log
    get_stdio_logger(session.name, config.LOG_DIR).debug("<<< STDOUT %s", json.dumps(event))

    if ctx.hit_rate_limit:
        return

    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta":
            ctx.text_buffer += delta.get("text", "")
            if ctx.live_edit is not None:
                await _live_edit_tick(ctx, session)
    elif event_type == "message_delta":
        stop_reason = event.get("delta", {}).get("stop_reason")
        if stop_reason == "end_turn":
            await _flush_text(ctx, session, channel, "end_turn")
            ctx.text_buffer = ""
            await _hide_thinking(ctx)


def _log_stream_event(session: AgentSession, event_type: str, event: dict[str, Any]) -> None:
    """Log a stream event to the agent's log."""
    assert session.agent_log is not None
    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        delta_type = delta.get("type", "")
        if delta_type not in ("text_delta", "thinking_delta", "signature_delta"):
            session.agent_log.debug("STREAM: %s delta=%s", event_type, delta_type)
    elif event_type in ("content_block_start", "content_block_stop"):
        block = event.get("content_block", {})
        session.agent_log.debug("STREAM: %s type=%s index=%s", event_type, block.get("type", "?"), event.get("index"))
    elif event_type == "message_start":
        msg_data = event.get("message", {})
        session.agent_log.debug("STREAM: message_start model=%s", msg_data.get("model", "?"))
    elif event_type == "message_delta":
        delta = event.get("delta", {})
        session.agent_log.debug("STREAM: message_delta stop_reason=%s", delta.get("stop_reason"))
    elif event_type == "message_stop":
        session.agent_log.debug("STREAM: message_stop")
    else:
        session.agent_log.debug("STREAM: %s %s", event_type, json.dumps(event)[:300])


async def _handle_assistant_message(
    ctx: _StreamCtx, session: AgentSession, channel: TextChannel, msg: AssistantMessage,
) -> None:
    """Handle an AssistantMessage during response streaming."""
    if msg.error in ("rate_limit", "billing_error"):
        error_text = ctx.text_buffer
        for block in msg.content or []:
            if hasattr(block, "text"):
                error_text += " " + cast("str", getattr(block, "text", ""))
        log.warning("Agent '%s' hit %s error: %s", session.name, msg.error, error_text[:200])
        await _hide_thinking(ctx)
        _stop_typing(ctx, session)
        await _handle_rate_limit(error_text, session, channel)
        ctx.text_buffer = ""
        ctx.hit_rate_limit = True
    elif msg.error:
        error_text = ctx.text_buffer
        for block in msg.content or []:
            if hasattr(block, "text"):
                error_text += " " + cast("str", getattr(block, "text", ""))
        log.warning("Agent '%s' hit API error (%s): %s", session.name, msg.error, error_text[:200])
        await _hide_thinking(ctx)
        _stop_typing(ctx, session)
        await _flush_text(ctx, session, channel, "assistant_error")
        ctx.text_buffer = ""
        ctx.hit_transient_error = msg.error
    else:
        # When text arrives in full AssistantMessages (flowcoder engine path)
        # rather than via StreamEvent deltas (Claude Code path), the buffer
        # will be empty.  Extract text from content blocks in that case.
        if not ctx.text_buffer.strip():
            for block in msg.content or []:
                if hasattr(block, "text"):
                    ctx.text_buffer += cast("str", getattr(block, "text", ""))
        await _flush_text(ctx, session, channel, "assistant_msg")
        ctx.text_buffer = ""
        await _hide_thinking(ctx)

    if session.agent_log:
        for block in msg.content or []:
            block_any: Any = block
            if hasattr(block, "text"):
                session.agent_log.info("ASSISTANT: %s", block_any.text[:2000])
            elif hasattr(block, "type") and block_any.type == "tool_use":
                session.agent_log.info(
                    "TOOL_USE: %s(%s)",
                    block_any.name,
                    json.dumps(block_any.input)[:500] if hasattr(block, "input") else "",
                )


async def _handle_result_message(
    ctx: _StreamCtx, session: AgentSession, channel: TextChannel, msg: ResultMessage,
) -> None:
    """Handle a ResultMessage during response streaming."""
    ctx.got_result = True
    await _hide_thinking(ctx)
    _stop_typing(ctx, session)
    if not ctx.hit_rate_limit:
        await _flush_text(ctx, session, channel, "result_msg")
    ctx.text_buffer = ""

    # Flowchart results use session_id="flowchart" — don't update agent session or record usage
    if msg.session_id == "flowchart":
        if session.agent_log:
            session.agent_log.info(
                "FLOWCHART_RESULT: cost=$%s turns=%d duration=%dms error=%s",
                msg.total_cost_usd,
                msg.num_turns,
                msg.duration_ms,
                msg.is_error,
            )
        return

    await _set_session_id(session, msg, channel=channel)
    if session.agent_log:
        session.agent_log.info(
            "RESULT: cost=$%s turns=%d duration=%dms session=%s",
            msg.total_cost_usd,
            msg.num_turns,
            msg.duration_ms,
            msg.session_id,
        )
    _record_session_usage(session.name, msg)


_SILENT_BLOCK_TYPES = {"start", "end", "variable"}


async def _handle_system_message(
    session: AgentSession, channel: TextChannel, msg: SystemMessage, ctx: _StreamCtx | None = None
) -> None:
    """Handle a SystemMessage during response streaming."""
    if session.agent_log:
        session.agent_log.debug("SYSTEM_MSG: subtype=%s data=%s", msg.subtype, json.dumps(msg.data)[:500])
    if msg.subtype == "status" and msg.data.get("status") == "compacting":
        # CLI signals compaction is starting — only notify if we didn't trigger it ourselves
        if session.name not in _self_compacting:
            token_info = f" ({session.context_tokens:,} tokens)" if session.context_tokens else ""
            log.info("Agent '%s' compaction started (CLI-triggered)%s", session.name, token_info)
            await channel.send(f"\U0001f504 Compacting{token_info}...")

    elif msg.subtype == "compact_boundary":
        metadata = msg.data.get("compact_metadata", {})
        trigger = metadata.get("trigger", "unknown")
        pre_tokens = metadata.get("pre_tokens")
        start_time = _compact_start_times.pop(session.name, None)
        log.info(
            "Agent '%s' context compacted: trigger=%s pre_tokens=%s",
            session.name, trigger, pre_tokens,
        )
        # Defer the completion message to the next query, when post_tokens
        # will be available from the autocompact stderr line.
        if pre_tokens:
            _pending_compact[session.name] = {
                "pre_tokens": pre_tokens,
                "start_time": start_time or time.monotonic(),
            }
        else:
            await channel.send("\U0001f504 Context compacted")

    # Flowchart events (emitted by flowcoder-engine during takeover mode)
    elif msg.subtype == "block_start":
        if ctx:
            await _flush_text(ctx, session, channel, "block_start")
            ctx.text_buffer = ""
        data = msg.data.get("data", {})
        block_name = data.get("block_name", "?")
        block_type = data.get("block_type", "?")
        session.activity = ActivityState(
            phase="tool_use",
            tool_name=f"flowcoder:{block_type}",
            query_started=session.activity.query_started,
        )
        if block_type not in _SILENT_BLOCK_TYPES:
            await channel.send(f"\u25b6 **{block_name}** (`{block_type}`)")

    elif msg.subtype == "block_complete":
        if ctx:
            await _flush_text(ctx, session, channel, "block_complete")
            ctx.text_buffer = ""
        data = msg.data.get("data", {})
        if not data.get("success", True):
            block_name = data.get("block_name", "?")
            await channel.send(f"> {block_name} **FAILED**")

    elif msg.subtype == "flowchart_start":
        data = msg.data.get("data", {})
        log.info(
            "Flowchart started for '%s': command=%s blocks=%s",
            session.name,
            data.get("command"),
            data.get("block_count"),
        )

    elif msg.subtype == "flowchart_complete":
        data = msg.data.get("data", {})
        duration_s = data.get("duration_ms", 0) / 1000
        cost = data.get("cost_usd", 0)
        blocks = data.get("blocks_executed", 0)
        status = "**completed**" if data.get("status") == "completed" else "**failed**"
        await send_system(channel, f"Flowchart {status} in {duration_s:.0f}s | Cost: ${cost:.4f} | Blocks: {blocks}")


# ---------------------------------------------------------------------------
# Main streaming entrypoint
# ---------------------------------------------------------------------------


async def stream_response_to_channel(session: AgentSession, channel: TextChannel) -> str | None:
    """Stream Claude's response from an agent session to a Discord channel.

    Returns None on success, or an error string for transient errors (for retry).
    """
    stream_id = _next_stream_id(session.name)
    if log.isEnabledFor(logging.INFO):
        caller = "".join(f.name or "?" for f in traceback.extract_stack(limit=4)[:-1])
        log.info("STREAM_START[%s] caller=%s", stream_id, caller)

    span = _tracer.start_span("stream_to_discord", attributes={"agent.name": session.name, "discord.channel": str(channel.id)})
    ctx_token = otel_context.attach(trace.set_span_in_context(span))
    t0 = time.monotonic()
    t_first_event: float | None = None

    live_edit = _LiveEditState(channel.id) if config.STREAMING_DISCORD else None
    ctx = _StreamCtx(live_edit=live_edit)

    # Manage the typing indicator manually so permission callbacks
    # (AskUserQuestion, ExitPlanMode) can cancel it while waiting for
    # user input and restart it once the user answers.
    typing_obj = channel.typing()
    await typing_obj.__aenter__()
    ds = discord_state(session)
    ds.typing_obj = typing_obj
    try:
        async for msg in _receive_response_safe(session):
            # Restart typing if it was externally cancelled (e.g. question/plan wait resolved)
            if (
                not ctx.typing_stopped
                and not ctx.got_result
                and ds.typing_obj is not None
                and hasattr(ds.typing_obj, "task")
                and ds.typing_obj.task.done()
            ):
                typing_obj = channel.typing()
                await typing_obj.__aenter__()
                ds.typing_obj = typing_obj

            if t_first_event is None:
                t_first_event = time.monotonic()
            ctx.msg_total += 1
            if session.agent_log:
                session.agent_log.debug(
                    "MSG_SEQ[%s][%d] type=%s buf_len=%d",
                    stream_id,
                    ctx.msg_total,
                    type(msg).__name__,
                    len(ctx.text_buffer),
                )

            if isinstance(msg, StreamEvent):
                await _handle_stream_event(ctx, session, channel, msg)
            elif isinstance(msg, AssistantMessage):
                await _handle_assistant_message(ctx, session, channel, msg)
            elif isinstance(msg, ResultMessage):
                await _handle_result_message(ctx, session, channel, msg)
            elif isinstance(msg, SystemMessage):
                if msg.subtype == "flowchart_start":
                    ctx.in_flowchart = True
                elif msg.subtype == "flowchart_complete":
                    ctx.in_flowchart = False
                await _handle_system_message(session, channel, msg, ctx)
            elif session.agent_log:
                session.agent_log.debug("OTHER_MSG: %s", type(msg).__name__)

            # Mid-turn flush (skipped in streaming mode — live-edit handles splitting)
            if not ctx.hit_rate_limit and ctx.live_edit is None and len(ctx.text_buffer) >= 1800:
                split_at = ctx.text_buffer.rfind("\n", 0, 1800)
                if split_at == -1:
                    split_at = 1800
                remainder = ctx.text_buffer[split_at:].lstrip("\n")
                ctx.text_buffer = ctx.text_buffer[:split_at]
                await _flush_text(ctx, session, channel, "mid_turn_split")
                ctx.text_buffer = remainder
    finally:
        # Always cancel the typing indicator on exit (ds.typing_obj may have been
        # replaced by a restart, so cancel whatever is current)
        current_typing = ds.typing_obj
        if current_typing and hasattr(current_typing, "task"):
            current_typing.task.cancel()
        ds.typing_obj = None

    # Clean up temporary tool progress messages
    if config.CLEAN_TOOL_MESSAGES and ctx.tool_msg_ids_to_delete:
        await _delete_tool_progress_messages(ctx)

    if ctx.hit_rate_limit:
        # Finalize any in-flight streaming message (remove cursor)
        if ctx.live_edit is not None and ctx.live_edit.message_id is not None:
            await _live_edit_finalize(ctx, session)
        log.info("STREAM_END[%s] result=rate_limit msgs=%d flushes=%d", stream_id, ctx.msg_total, ctx.flush_count)
        span.set_attributes({"stream.msg_total": ctx.msg_total, "stream.flush_count": ctx.flush_count})
        otel_context.detach(ctx_token)
        span.end()
        return None

    if ctx.hit_transient_error:
        # Send any deferred message before exiting
        if ctx.deferred_msg:
            await send_long(channel, ctx.deferred_msg)
            ctx.deferred_msg = ""
        log.info(
            "STREAM_END[%s] result=transient_error(%s) msgs=%d flushes=%d",
            stream_id,
            ctx.hit_transient_error,
            ctx.msg_total,
            ctx.flush_count,
        )
        span.set_attributes({"stream.msg_total": ctx.msg_total, "stream.flush_count": ctx.flush_count})
        otel_context.detach(ctx_token)
        span.end()
        return ctx.hit_transient_error

    if not ctx.got_result:
        # Stream ended without a ResultMessage — the CLI process was killed
        # (e.g. by /stop) or crashed.  Sleep the agent so the next message
        # triggers a fresh wake with a new CLI process.
        if ctx.deferred_msg:
            await send_long(channel, ctx.deferred_msg)
            ctx.deferred_msg = ""
        if ctx.live_edit is not None and ctx.live_edit.message_id is not None:
            await _live_edit_finalize(ctx, session)
        await _flush_text(ctx, session, channel, "post_kill")
        log.info("STREAM_END[%s] result=killed msgs=%d flushes=%d", stream_id, ctx.msg_total, ctx.flush_count)
        span.set_attributes({"stream.msg_total": ctx.msg_total, "stream.flush_count": ctx.flush_count})
        span.end()
        await sleep_agent(session, force=True)
        return None

    # Append response timing inline to the last message
    if session.activity.query_started and (ctx.flush_count > 0 or ctx.text_buffer.strip()):
        elapsed = (datetime.now(UTC) - session.activity.query_started).total_seconds()
        _sc = span.get_span_context()
        _trace_tag = f" [trace={format(_sc.trace_id, '032x')[:16]}]" if _sc and _sc.trace_id else ""
        timing_suffix = f"\n-# {elapsed:.1f}s{_trace_tag}"

        if ctx.deferred_msg:
            # Non-streaming: deferred message waiting — append timing and send (no edit needed)
            await send_long(channel, ctx.deferred_msg + timing_suffix)
            ctx.deferred_msg = ""
        elif ctx.text_buffer.strip():
            # Buffer has content — append inline and flush normally
            ctx.text_buffer += timing_suffix
            await _flush_text(ctx, session, channel, "post_loop")
        elif ctx.last_flushed_msg_id is not None and ctx.last_flushed_channel_id is not None:
            # Streaming: buffer empty, message already sent — edit to append timing
            new_content = ctx.last_flushed_content + timing_suffix
            try:
                await config.discord_client.edit_message(
                    ctx.last_flushed_channel_id, ctx.last_flushed_msg_id, new_content
                )
            except Exception:
                log.warning("Failed to edit last message to append timing", exc_info=True)
                await channel.send(f"-# {elapsed:.1f}s{_trace_tag}")
    else:
        # No timing — send any deferred message as-is
        if ctx.deferred_msg:
            await send_long(channel, ctx.deferred_msg)
            ctx.deferred_msg = ""
        await _flush_text(ctx, session, channel, "post_loop")
    log.info("STREAM_END[%s] result=ok msgs=%d flushes=%d", stream_id, ctx.msg_total, ctx.flush_count)

    if config.SHOW_AWAITING_INPUT:
        mentions = " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)
        await send_system(channel, f"Bot has finished responding and is awaiting input. {mentions}")

    ttfe_ms = (t_first_event - t0) * 1000 if t_first_event is not None else -1
    span.set_attributes({
        "stream.msg_total": ctx.msg_total,
        "stream.flush_count": ctx.flush_count,
        "stream.time_to_first_event_ms": ttfe_ms,
    })
    otel_context.detach(ctx_token)
    span.end()
    return None


# ---------------------------------------------------------------------------
# Interrupt
# ---------------------------------------------------------------------------


async def interrupt_session(session: AgentSession) -> None:
    """Kill the CLI process for an agent session.

    For bridge-managed agents (flowcoder): calls transport.stop() which
    immediately terminates the streaming loop by injecting an ExitEvent,
    then kills the process in the background.  Returns instantly.

    For direct-subprocess agents (claude_code): uses the SDK interrupt with
    a short timeout.
    """
    trace_tag = get_active_trace_tag(session.name)
    _tracer.start_span(
        "interrupt_session",
        attributes={
            "agent.name": session.name,
            "interrupt.trace_tag": trace_tag,
        },
    ).end()

    # Flowcoder agents: use transport.stop() for instant termination
    if session.transport is not None:
        await session.transport.stop()
        return

    # Fallback: try procmux kill directly (e.g. transport lost but procmux alive)
    if procmux_conn and procmux_conn.is_alive:
        result = await procmux_conn.send_command("kill", name=session.name)
        if not result.ok:
            log.warning("Bridge kill for '%s' failed: %s", session.name, result.error)
        else:
            return

    # Non-bridge agents (claude_code): SDK interrupt
    if session.client is not None:
        try:
            async with asyncio.timeout(5):
                await session.client.interrupt()
        except (TimeoutError, Exception):
            pass


async def graceful_interrupt(session: AgentSession) -> bool:
    """Gracefully interrupt the current turn without killing the CLI process.

    Sends a control_request.interrupt to the CLI, which aborts the current
    API call and emits a result.  The CLI stays alive with full conversation
    context -- ready for the next user message.

    Returns True if the interrupt was sent successfully, False otherwise.
    On failure the queued message will process after the current turn finishes
    (graceful degradation -- same as the old behavior).
    """
    if session.client is None:
        log.debug("graceful_interrupt: no client for '%s'", session.name)
        return False

    try:
        async with asyncio.timeout(5):
            await session.client.interrupt()
        log.info("INTERRUPT[%s] graceful interrupt sent", session.name)
        return True
    except TimeoutError:
        log.warning("INTERRUPT[%s] graceful interrupt timed out", session.name)
        return False
    except Exception:
        log.warning("INTERRUPT[%s] graceful interrupt failed", session.name, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Retry / timeout
# ---------------------------------------------------------------------------


async def stream_with_retry(session: AgentSession, channel: TextChannel) -> bool:
    """Stream response with retry on transient API errors. Returns True on success."""
    with _tracer.start_as_current_span("stream_with_retry", attributes={"agent.name": session.name}) as span:
        log.info("RETRY_ENTER[%s] starting initial stream", session.name)
        error = await stream_response_to_channel(session, channel)
        if error is None:
            log.info("RETRY_EXIT[%s] first attempt succeeded", session.name)
            span.set_attribute("retry.attempts", 1)
            return True

        log.warning("RETRY_TRIGGERED[%s] error=%s \u2014 will retry", session.name, error)
        for attempt in range(2, config.API_ERROR_MAX_RETRIES + 1):
            delay = config.API_ERROR_BASE_DELAY * (2 ** (attempt - 2))
            log.warning(
                "Agent '%s' transient error '%s', retrying in %ds (attempt %d/%d)",
                session.name,
                error,
                delay,
                attempt,
                config.API_ERROR_MAX_RETRIES,
            )
            await channel.send(
                f"\u26a0\ufe0f API error, retrying in {delay}s... (attempt {attempt}/{config.API_ERROR_MAX_RETRIES})"
            )
            await asyncio.sleep(delay)

            try:
                assert session.client is not None
                get_stdio_logger(session.name, config.LOG_DIR).debug(
                    ">>> STDIN  %s", json.dumps({"type": "retry", "content": "Continue from where you left off."})
                )
                await session.client.query(as_stream("Continue from where you left off."))
            except Exception:
                log.exception("Agent '%s' retry query failed", session.name)
                continue

            error = await stream_response_to_channel(session, channel)
            if error is None:
                span.set_attribute("retry.attempts", attempt)
                return True

        log.error(
            "Agent '%s' transient error persisted after %d retries",
            session.name,
            config.API_ERROR_MAX_RETRIES,
        )
        await channel.send(f"\u274c API error persisted after {config.API_ERROR_MAX_RETRIES} retries. Try again later.")
        span.set_attribute("retry.exhausted", True)
        span.set_status(trace.StatusCode.ERROR, "retries exhausted")
        return False


async def handle_query_timeout(session: AgentSession, channel: TextChannel) -> None:
    """Handle a query timeout by killing the CLI and rebuilding the session."""
    log.warning("Query timeout for agent '%s', killing session", session.name)

    try:
        await interrupt_session(session)
    except Exception:
        log.exception("interrupt_session failed for '%s'", session.name)

    old_session_id = session.session_id
    new_session = await _rebuild_session(session.name, session_id=old_session_id)

    if old_session_id:
        await send_system(
            channel, f"Agent **{new_session.name}** timed out and was recovered (sleeping). Context preserved."
        )
    else:
        await send_system(channel, f"Agent **{new_session.name}** timed out and was reset (sleeping). Context lost.")


# ---------------------------------------------------------------------------
# Axi-owned auto-compact
# ---------------------------------------------------------------------------

_self_compacting: set[str] = set()  # Agent names currently in Axi-triggered compaction
_compact_start_times: dict[str, float] = {}  # Agent name -> monotonic start time
# Pending compact result — shown at the start of the next query when post_tokens is available
_pending_compact: dict[str, dict[str, int | float]] = {}  # agent name -> {"pre_tokens": int, "start_time": float}


async def _maybe_compact(session: AgentSession, channel: TextChannel) -> None:
    """Trigger manual compaction with custom instructions if context is getting full."""
    if session.context_tokens <= 0 or session.context_window <= 0:
        return
    usage_pct = session.context_tokens / session.context_window
    if usage_pct < config.COMPACT_THRESHOLD:
        return

    pre_tokens = session.context_tokens
    instructions = session.compact_instructions or ""
    cmd = f"/compact {instructions}".strip()
    log.info(
        "Auto-compact for '%s': %d/%d tokens (%.0f%%), sending: %s",
        session.name, pre_tokens, session.context_window,
        usage_pct * 100, cmd[:80],
    )

    await channel.send(f"\U0001f504 Context at {usage_pct:.0%} ({pre_tokens:,} tokens) \u2014 compacting...")
    _self_compacting.add(session.name)
    _compact_start_times[session.name] = time.monotonic()
    try:
        await session.client.query(as_stream(cmd))
        await stream_with_retry(session, channel)
    finally:
        _self_compacting.discard(session.name)
    # compact_boundary handler posts the completion message with timing + stats


# ---------------------------------------------------------------------------
# Message processing, spawning, and inter-agent delivery
# ---------------------------------------------------------------------------


async def process_message(session: AgentSession, content: MessageContent, channel: TextChannel) -> None:
    """Process a user message through the agent's Claude session.

    Flowcoder agents are a superset of Claude Code agents — the engine acts as
    a transparent proxy for normal messages and intercepts slash commands for
    flowchart execution. All messages go through session.client (the SDK).
    """
    if session.client is None:
        raise RuntimeError(f"Agent '{session.name}' not awake")

    set_agent_context(session.name, channel_id=channel.id)

    _reset_session_activity(session)
    session.bridge_busy = False
    drain_stderr(session)
    drained = drain_sdk_buffer(session)

    if session.agent_log:
        session.agent_log.info("USER: %s", content_summary(content))
    log.info("PROCESS[%s] drained=%d, calling query+stream", session.name, drained)
    get_stdio_logger(session.name, config.LOG_DIR).debug(
        ">>> STDIN  %s", json.dumps({"type": "user", "content": content if isinstance(content, str) else "[blocks]"})
    )
    with _tracer.start_as_current_span(
        "process_message",
        attributes={
            "agent.name": session.name,
            "agent.type": session.agent_type or "claude_code",
            "message.length": len(content) if isinstance(content, str) else -1,
            "discord.channel": getattr(channel, "name", "?"),
        },
    ) as pm_span:
        # Store trace ID so /stop and /skip can reference the interrupted turn
        _sc = pm_span.get_span_context()
        if _sc and _sc.trace_id:
            _active_trace_ids[session.name] = f"[trace={format(_sc.trace_id, '032x')[:16]}]"
        try:
            async with asyncio.timeout(config.QUERY_TIMEOUT):
                await session.client.query(as_stream(content))
                # After query() the CLI emits the autocompact stderr line with
                # updated token counts. Brief yield lets the stderr thread process it.
                await asyncio.sleep(0.3)
                # Show deferred compact result now that we have fresh post_tokens
                pending = _pending_compact.pop(session.name, None)
                if pending:
                    post_tokens = session.context_tokens
                    pre_tokens = int(pending["pre_tokens"])
                    elapsed = time.monotonic() - float(pending["start_time"])
                    if post_tokens > 0 and post_tokens != pre_tokens:
                        saved = int(pre_tokens - post_tokens)
                        pct = post_tokens / session.context_window if session.context_window else 0
                        await channel.send(
                            f"\U0001f504 Compacted in {elapsed:.1f}s: {pre_tokens:,} \u2192 {post_tokens:,} tokens "
                            f"({saved:,} freed, {pct:.0%} used)"
                        )
                    else:
                        await channel.send(
                            f"\U0001f504 Compacted in {elapsed:.1f}s ({pre_tokens:,} tokens)"
                        )
                await stream_with_retry(session, channel)
                # Axi-owned auto-compact: trigger after response if context is near full
                await _maybe_compact(session, channel)
                # Auto-resume after compaction: if compact_boundary was seen
                # during this turn (CLI-triggered or Axi-triggered), the agent
                # may have lost its place.  Send a continuation nudge so it
                # picks up where it left off.  Limited to one resume per
                # process_message call to prevent infinite loops.
                pending_resume = _pending_compact.pop(session.name, None)
                if pending_resume is not None:
                    log.info("Auto-resuming agent '%s' after compaction", session.name)
                    _reset_session_activity(session)
                    resume_msg = "Continue from where you left off."
                    get_stdio_logger(session.name, config.LOG_DIR).debug(
                        ">>> STDIN  %s", json.dumps({"type": "auto_resume", "content": resume_msg})
                    )
                    await session.client.query(as_stream(resume_msg))
                    await asyncio.sleep(0.3)
                    # Show compact stats with fresh post_tokens from stderr
                    post_tokens = session.context_tokens
                    pre_tokens = int(pending_resume["pre_tokens"])
                    elapsed = time.monotonic() - float(pending_resume["start_time"])
                    if post_tokens > 0 and post_tokens != pre_tokens:
                        saved = int(pre_tokens - post_tokens)
                        pct = post_tokens / session.context_window if session.context_window else 0
                        await channel.send(
                            f"\U0001f504 Compacted in {elapsed:.1f}s: {pre_tokens:,} \u2192 {post_tokens:,} tokens "
                            f"({saved:,} freed, {pct:.0%} used) \u2014 resuming"
                        )
                    else:
                        await channel.send(
                            f"\U0001f504 Compacted in {elapsed:.1f}s ({pre_tokens:,} tokens) \u2014 resuming"
                        )
                    await stream_with_retry(session, channel)
                    # Safety: compact again if the resume filled context, but
                    # don't auto-resume a second time.
                    await _maybe_compact(session, channel)
        except TimeoutError:
            await handle_query_timeout(session, channel)
        except Exception:
            log.exception("Error querying Claude Code agent '%s'", session.name)
            raise RuntimeError(f"Query failed for agent '{session.name}'") from None
        finally:
            _active_trace_ids.pop(session.name, None)


# ---------------------------------------------------------------------------
# Agent spawning
# ---------------------------------------------------------------------------


async def reclaim_agent_name(name: str) -> None:
    """If an agent with *name* already exists, kill it silently to free the name."""
    if name not in agents:
        return
    _tracer.start_span("reclaim_agent_name", attributes={"agent.name": name}).end()
    log.info("Reclaiming agent name '%s' \u2014 terminating existing session", name)
    session = agents[name]
    await sleep_agent(session, force=True)
    agents.pop(name, None)
    channel = await get_agent_channel(name)
    if channel:
        await send_system(channel, f"Recycled previous **{name}** session for new scheduled run.")


async def spawn_agent(
    name: str,
    cwd: str,
    initial_prompt: str,
    resume: str | None = None,
    agent_type: str = "flowcoder",
    command: str = "",
    command_args: str = "",
    packs: list[str] | None = None,
    compact_instructions: str | None = None,
    extra_mcp_servers: dict[str, Any] | None = None,
) -> None:
    """Spawn a new agent session and run its initial prompt in the background."""
    with _tracer.start_as_current_span(
        "spawn_agent",
        attributes={
            "agent.name": name,
            "agent.type": agent_type,
            "agent.cwd": cwd,
            "agent.resumed": bool(resume),
            "prompt.length": len(initial_prompt),
        },
    ):
        os.makedirs(cwd, exist_ok=True)

        set_agent_context(name)
        set_trigger("spawn", detail=f"type={agent_type}")

        normalized = normalize_channel_name(name)
        _channels_mod.bot_creating_channels.add(normalized)
        channel = await ensure_agent_channel(name, cwd=cwd)

        agent_label = "flowcoder" if agent_type == "flowcoder" else "claude code"
        if resume:
            await send_system(
                channel, f"Resuming **{agent_label}** agent **{name}** (session `{resume[:8]}\u2026`) in `{cwd}`..."
            )
        else:
            await send_system(channel, f"Spawning **{agent_label}** agent **{name}** in `{cwd}`...")

        prompt = make_spawned_agent_system_prompt(cwd, packs=packs, compact_instructions=compact_instructions)
        mcp_servers = _build_mcp_servers(name, cwd, extra_mcp_servers=extra_mcp_servers)

        mcp_names = list(extra_mcp_servers.keys()) if extra_mcp_servers else None

        session = AgentSession(
            name=name,
            agent_type=agent_type,
            cwd=cwd,
            system_prompt=prompt,
            system_prompt_hash=compute_prompt_hash(prompt),
            client=None,
            session_id=resume,
            mcp_servers=mcp_servers,
            mcp_server_names=mcp_names,
            compact_instructions=compact_instructions,
        )
        discord_state(session).channel_id = channel.id

        # Persist agent config (MCP servers, packs) for restart reconstruction
        if mcp_names or packs is not None:
            _save_agent_config(name, mcp_names, packs=packs)

        agents[name] = session
        channel_to_agent[channel.id] = name
        _channels_mod.bot_creating_channels.discard(normalized)
        log.info("Agent '%s' registered (type=%s, cwd=%s, resume=%s)", name, agent_type, cwd, resume)

        # Update channel topic — fire-and-forget to avoid blocking on Discord's
        # strict channel-edit rate limit (2 per 10 min).  A category move during
        # kill/respawn already consumes the budget, so a synchronous topic edit
        # would stall spawn_agent and prevent the initial prompt from launching.
        desired_topic = format_channel_topic(cwd, resume, session.system_prompt_hash, agent_type=agent_type)
        if channel.topic != desired_topic:
            log.info("Updating topic on #%s: %r -> %r", channel.name, channel.topic, desired_topic)

            async def _update_topic(ch: Any, topic: str) -> None:
                try:
                    await ch.edit(topic=topic)
                except Exception:
                    log.warning("Failed to update topic on #%s", ch.name, exc_info=True)

            fire_and_forget(_update_topic(channel, desired_topic))

        if not initial_prompt:
            await send_system(channel, f"**{agent_label.title()}** agent **{name}** is ready (sleeping).")
            return

        fire_and_forget(run_initial_prompt(session, initial_prompt, channel))


async def send_prompt_to_agent(agent_name: str, prompt: str) -> None:
    """Send a prompt to an existing agent session in the background."""
    session = agents.get(agent_name)
    if session is None:
        log.warning("send_prompt_to_agent: agent '%s' not found", agent_name)
        return

    channel = await get_agent_channel(agent_name)
    if channel is None:
        log.warning("send_prompt_to_agent: no channel for agent '%s'", agent_name)
        return

    ts_prefix = datetime.now(UTC).strftime("[%Y-%m-%d %H:%M:%S UTC] ")
    prompt = ts_prefix + prompt

    fire_and_forget(run_initial_prompt(session, prompt, channel))


# ---------------------------------------------------------------------------
# Initial prompt / message queue
# ---------------------------------------------------------------------------


async def run_initial_prompt(session: AgentSession, prompt: MessageContent, channel: TextChannel) -> None:
    """Run the initial prompt for a spawned agent."""
    set_agent_context(session.name, channel_id=channel.id)
    set_trigger("initial_prompt")
    with _tracer.start_as_current_span(
        "run_initial_prompt",
        attributes={
            "agent.name": session.name,
            "prompt.length": len(prompt) if isinstance(prompt, str) else -1,
        },
    ):
        try:
            async with session.query_lock:
                if not is_awake(session):
                    try:
                        await wake_agent(session)
                    except ConcurrencyLimitError:
                        log.info("Concurrency limit hit for '%s' initial prompt \u2014 queuing", session.name)
                        session.message_queue.append((prompt, channel, None))
                        awake = count_awake_agents()
                        await send_system(
                            channel,
                            f"\u23f3 All {awake} agent slots are busy. Initial prompt queued \u2014 will run when a slot opens.",
                        )
                        return
                    except Exception:
                        log.exception("Failed to wake agent '%s' for initial prompt", session.name)
                        await send_system(channel, f"Failed to wake agent **{session.name}**.")
                        return

                session.last_activity = datetime.now(UTC)
                drain_stderr(session)
                drain_sdk_buffer(session)

                prompt_text = prompt if isinstance(prompt, str) else str(prompt)
                await send_long(channel, f"*System:* \U0001f4dd **Initial prompt:**\n{prompt_text}")

                if session.agent_log:
                    session.agent_log.info("PROMPT: %s", content_summary(prompt))
                log.info("INITIAL_PROMPT[%s] running initial prompt: %s", session.name, content_summary(prompt))
                session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))
                try:
                    await process_message(session, prompt, channel)
                    session.last_activity = datetime.now(UTC)
                except RuntimeError as e:
                    log.warning("Handler error for '%s' initial prompt: %s", session.name, e)
                    await send_system(channel, f"Error: {e}")
                finally:
                    session.activity = ActivityState(phase="idle")

            log.debug("Initial prompt completed for '%s'", session.name)
            discord_state(session).task_done = True
            schedule_status_update()
            await send_system(channel, f"Agent **{session.name}** finished initial task. {_user_mentions()}")

        except Exception:
            log.exception("Error running initial prompt for agent '%s'", session.name)
            discord_state(session).task_error = True
            schedule_status_update()
            await send_system(
                channel, f"Agent **{session.name}** encountered an error during initial task. {_user_mentions()}"
            )

        if scheduler.should_yield(session.name):
            log.info("Scheduler yield: '%s' sleeping after initial prompt (skipping queue)", session.name)
        else:
            await process_message_queue(session)

        try:
            await sleep_agent(session)
        except Exception:
            log.exception("Error sleeping agent '%s' after initial prompt", session.name)


async def process_message_queue(session: AgentSession) -> None:
    """Process any queued messages for an agent after the current query finishes."""
    if session.message_queue:
        log.info("QUEUE[%s] processing %d queued messages", session.name, len(session.message_queue))
        _tracer.start_span(
            "process_message_queue",
            attributes={"agent.name": session.name, "queue.size": len(session.message_queue)},
        ).end()  # mark event; individual messages are traced via process_message
    while session.message_queue:
        if hub and hub.shutdown_requested:
            log.info("Shutdown requested \u2014 not processing further queued messages for '%s'", session.name)
            break
        # Yield slot if scheduler needs it for another agent
        if scheduler.should_yield(session.name):
            log.info("Scheduler yield: '%s' deferring %d queued messages", session.name, len(session.message_queue))
            await sleep_agent(session)
            return
        content, channel, orig_message = session.message_queue.popleft()

        remaining = len(session.message_queue)
        log.debug("Processing queued message for '%s' (%d remaining)", session.name, remaining)
        if session.agent_log:
            session.agent_log.info("QUEUED_MSG: %s", content_summary(content))
        await remove_reaction(orig_message, "\U0001f4e8")
        preview = content_summary(content)
        remaining_str = f" ({remaining} more in queue)" if remaining > 0 else ""
        await send_system(channel, f"Processing queued message{remaining_str}:\n> {preview}")

        async with session.query_lock:
            if not is_awake(session):
                try:
                    await wake_agent(session)
                except Exception:
                    log.exception("Failed to wake agent '%s' for queued message", session.name)
                    await add_reaction(orig_message, "\u274c")
                    await send_system(
                        channel,
                        f"Failed to wake agent **{session.name}** \u2014 dropping queued message.",
                    )
                    while session.message_queue:
                        _, ch, dropped_msg = session.message_queue.popleft()
                        await remove_reaction(dropped_msg, "\U0001f4e8")
                        await add_reaction(dropped_msg, "\u274c")
                        await send_system(
                            ch,
                            f"Failed to wake agent **{session.name}** \u2014 dropping queued message.",
                        )
                    return

            _reset_session_activity(session)
            try:
                await process_message(session, content, channel)
                await add_reaction(orig_message, "\u2705")
            except TimeoutError:
                await add_reaction(orig_message, "\u23f3")
                await handle_query_timeout(session, channel)
            except RuntimeError as e:
                log.warning(
                    "Runtime error processing queued message for '%s': %s",
                    session.name,
                    e,
                )
                await add_reaction(orig_message, "\u274c")
                await send_system(channel, str(e))
            except Exception:
                log.exception("Error processing queued message for '%s'", session.name)
                await add_reaction(orig_message, "\u274c")
                await send_system(
                    channel,
                    f"Error processing queued message for **{session.name}**.",
                )
            finally:
                session.activity = ActivityState(phase="idle")
                schedule_status_update()


# ---------------------------------------------------------------------------
# Inter-agent messaging
# ---------------------------------------------------------------------------


async def deliver_inter_agent_message(
    sender_name: str,
    target_session: AgentSession,
    content: str,
) -> str:
    """Deliver a message from one agent to another."""
    set_agent_context(target_session.name, channel_id=discord_state(target_session).channel_id)
    set_trigger("inter_agent", detail=f"from={sender_name}")
    _tracer.start_span(
        "deliver_inter_agent_message",
        attributes={
            "agent.sender": sender_name,
            "agent.target": target_session.name,
            "message.length": len(content),
        },
    ).end()
    channel = await get_agent_channel(target_session.name)
    if channel is None:
        return f"No Discord channel found for agent '{target_session.name}'"

    await send_system(
        channel,
        f"\U0001f4e8 **Message from {sender_name}:**\n> {content}",
    )

    ts_prefix = datetime.now(UTC).strftime("[%Y-%m-%d %H:%M:%S UTC] ")
    prompt = ts_prefix + f"[Inter-agent message from {sender_name}] {content}"

    if target_session.query_lock.locked():
        target_session.message_queue.appendleft((prompt, channel, None))
        log.info(
            "Inter-agent message from '%s' to busy agent '%s' \u2014 interrupting",
            sender_name,
            target_session.name,
        )
        interrupted = await graceful_interrupt(target_session)
        if not interrupted:
            log.warning(
                "Graceful interrupt failed for '%s' inter-agent message (message still queued)",
                target_session.name,
            )
        return f"delivered to busy agent '{target_session.name}' (interrupted, will process next)"
    else:
        fire_and_forget(_process_inter_agent_prompt(target_session, prompt, channel))
        return f"delivered to agent '{target_session.name}'"


async def _process_inter_agent_prompt(
    session: AgentSession,
    content: str,
    channel: TextChannel,
) -> None:
    """Background task to wake (if needed) and process an inter-agent message."""
    try:
        async with session.query_lock:
            if not is_awake(session):
                try:
                    await wake_agent(session)
                except ConcurrencyLimitError:
                    session.message_queue.append((content, channel, None))
                    awake = count_awake_agents()
                    log.info(
                        "Concurrency limit hit for '%s' inter-agent message \u2014 queuing",
                        session.name,
                    )
                    await send_system(
                        channel,
                        f"\u23f3 All {awake} agent slots busy. Inter-agent message queued.",
                    )
                    return
                except Exception:
                    log.exception(
                        "Failed to wake agent '%s' for inter-agent message",
                        session.name,
                    )
                    await send_system(
                        channel,
                        f"Failed to wake agent **{session.name}** for inter-agent message.",
                    )
                    return

            _reset_session_activity(session)
            try:
                await process_message(session, content, channel)
            except TimeoutError:
                await handle_query_timeout(session, channel)
            except RuntimeError as e:
                log.warning(
                    "Runtime error processing inter-agent message for '%s': %s",
                    session.name,
                    e,
                )
                await send_system(channel, str(e))
            except Exception:
                log.exception(
                    "Error processing inter-agent message for '%s'",
                    session.name,
                )
                await send_system(
                    channel,
                    f"Error processing inter-agent message for **{session.name}**.",
                )
            finally:
                session.activity = ActivityState(phase="idle")
                schedule_status_update()

        if scheduler.should_yield(session.name):
            log.info("Scheduler yield: '%s' sleeping after inter-agent message", session.name)
            await sleep_agent(session)
        else:
            await process_message_queue(session)
    except Exception:
        log.exception(
            "Unhandled error in _process_inter_agent_prompt for '%s'",
            session.name,
        )


# ---------------------------------------------------------------------------
# Bridge connection and reconnection logic
# ---------------------------------------------------------------------------


async def connect_procmux() -> None:
    """Connect to the agent bridge and schedule reconnections for running agents."""
    global procmux_conn, wire_conn
    with _tracer.start_as_current_span("connect_procmux") as span:
        try:
            procmux_conn = await ensure_bridge(config.BRIDGE_SOCKET_PATH, timeout=10.0)
            wire_conn = ProcmuxProcessConnection(procmux_conn)
            log.info("Bridge connection established")
            span.set_attribute("procmux.connected", True)
        except Exception:
            log.exception("Failed to connect to bridge \u2014 agents will use direct subprocess mode")
            procmux_conn = None
            wire_conn = None
            span.set_attribute("procmux.connected", False)
            return

        try:
            result = await procmux_conn.send_command("list")
            bridge_agents = result.agents or {}
            log.info("Bridge reports %d agent(s): %s", len(bridge_agents), list(bridge_agents.keys()))
            span.set_attribute("procmux.agents_found", len(bridge_agents))
        except Exception:
            log.exception("Failed to list bridge agents")
            return

        if not bridge_agents:
            return

        for agent_name, info in bridge_agents.items():
            session = agents.get(agent_name)
            if session is None:
                log.warning("Bridge has agent '%s' but no matching session \u2014 killing", agent_name)
                try:
                    await procmux_conn.send_command("kill", name=agent_name)
                except Exception:
                    log.exception("Failed to kill orphan bridge agent '%s'", agent_name)
                continue

            status = info.get("status", "unknown")
            buffered = info.get("buffered_msgs", 0)
            log.info(
                "Reconnecting agent '%s' (status=%s, buffered=%d)",
                agent_name,
                status,
                buffered,
            )

            session.reconnecting = True
            fire_and_forget(_reconnect_and_drain(session, info))


async def _reconnect_and_drain(session: AgentSession, bridge_info: dict[str, Any]) -> None:
    """Reconnect a single agent to the bridge and drain any buffered output.

    IMPORTANT: The SDK client must be created and initialized BEFORE subscribing
    to the bridge agent. Subscribe replays buffered messages into the transport
    queue — if those messages are in the queue when the SDK sends its initialize
    control_request, the SDK reads stale data instead of the initialize response,
    corrupting the handshake and leaving the agent stuck.
    """
    span = _tracer.start_span(
        "reconnect_and_drain",
        attributes={"agent.name": session.name, "procmux.buffered_msgs": bridge_info.get("buffered_msgs", 0)},
    )
    ctx_token = otel_context.attach(trace.set_span_in_context(span))
    try:
        async with session.query_lock:
            if procmux_conn is None or not procmux_conn.is_alive:
                log.warning("Bridge connection lost during reconnect of '%s'", session.name)
                session.reconnecting = False
                return

            transport = await create_transport(session, reconnecting=True)
            assert transport is not None
            session.transport = transport

            # Create and initialize SDK client FIRST — the queue is empty so
            # the initialize handshake (intercepted by BridgeTransport) completes
            # cleanly without interference from replayed messages.
            options = ClaudeAgentOptions(
                can_use_tool=make_cwd_permission_callback(session.cwd, session),
                mcp_servers=session.mcp_servers or {},
                permission_mode="plan" if session.plan_mode else "default",
                cwd=session.cwd,
                include_partial_messages=True,
                stderr=make_stderr_callback(session),
                disallowed_tools=["Task"],
                extra_args={"debug-to-stderr": None},
                env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "100"},
            )

            client = ClaudeSDKClient(options=options, transport=transport)  # pyright: ignore[reportArgumentType]
            await client.__aenter__()

            # NOW subscribe — replayed messages flow into the queue after init.
            sub_result = await transport.subscribe()
            replayed = sub_result.replayed or 0
            cli_status = sub_result.status or "unknown"
            cli_idle = sub_result.idle if sub_result.idle is not None else True
            log.info(
                "Subscribed to '%s' (replayed=%d, status=%s, idle=%s)",
                session.name,
                replayed,
                cli_status,
                cli_idle,
            )

            # Handle exited processes — clean up and leave agent sleeping
            if cli_status == "exited":
                log.info("Agent '%s' CLI exited while we were down — cleaning up", session.name)
                await _disconnect_client(client, session.name)
                session.transport = None
                session.reconnecting = False
                if session.agent_log:
                    session.agent_log.info("SESSION_RECONNECT aborted — CLI exited")
                log.info("Agent '%s' left sleeping (CLI dead, will respawn on next message)", session.name)
                return

            session.client = client
            scheduler.restore_slot(session.name)
            session.last_activity = datetime.now(UTC)

            if session.agent_log:
                session.agent_log.info(
                    "SESSION_RECONNECT via bridge (replayed=%d, idle=%s)",
                    replayed,
                    cli_idle,
                )

            session.reconnecting = False

            if cli_status == "running" and not cli_idle:
                # Agent was mid-task — drain output regardless of replayed count.
                # Even with replayed=0, the agent is actively producing output that
                # needs to be consumed. After subscribe, live output flows into the
                # queue alongside any replayed messages.
                session.bridge_busy = True
                channel = await get_agent_channel(session.name)
                if channel:
                    log.info("RECONNECT_DRAIN[%s] draining output (replayed=%d)", session.name, replayed)
                    await send_system(channel, "*(reconnected after restart \u2014 resuming output)*")
                    try:
                        async with asyncio.timeout(config.QUERY_TIMEOUT):
                            await stream_response_to_channel(session, channel)
                    except TimeoutError:
                        log.warning("Drain timeout for '%s' \u2014 continuing", session.name)
                    except Exception:
                        log.exception("Error draining buffered output for '%s'", session.name)
                session.bridge_busy = False
                session.last_activity = datetime.now(UTC)
                log.info(
                    "Agent '%s' reconnected mid-task (idle=False, replayed=%d)",
                    session.name,
                    replayed,
                )
            elif cli_status == "running":
                channel = await get_agent_channel(session.name)
                if channel:
                    await send_system(channel, "*(reconnected after restart)*")
                log.info("Agent '%s' reconnected idle (between turns)", session.name)

            log.info("Reconnect complete for '%s'", session.name)

    except Exception:
        log.exception("Failed to reconnect agent '%s'", session.name)
        span.set_status(trace.StatusCode.ERROR, "reconnect failed")
        session.reconnecting = False
    finally:
        otel_context.detach(ctx_token)
        span.end()

    await process_message_queue(session)


# ---------------------------------------------------------------------------
# Re-exports from channels module (agents.py used to re-export these)
# ---------------------------------------------------------------------------

__all__ = [
    "_parse_channel_topic",
    "add_reaction",
    "agents",
    "as_stream",
    "channel_to_agent",
    "connect_procmux",
    "content_summary",
    "count_awake_agents",
    "deliver_inter_agent_message",
    "drain_sdk_buffer",
    "drain_stderr",
    "end_session",
    "ensure_agent_channel",
    "ensure_guild_infrastructure",
    "extract_message_content",
    "extract_tool_preview",
    "format_channel_topic",
    "format_time_remaining",
    "format_todo_list",
    "get_active_trace_tag",
    "get_agent_channel",
    "get_master_channel",
    "get_master_session",
    "graceful_interrupt",
    "handle_query_timeout",
    "init",
    "init_shutdown_coordinator",
    "interrupt_session",
    "is_awake",
    "is_processing",
    "is_rate_limited",
    "load_todo_items",
    "make_cwd_permission_callback",
    "make_shutdown_coordinator",
    "make_stderr_callback",
    "move_channel_to_killed",
    "normalize_channel_name",
    "process_message",
    "process_message_queue",
    "procmux_conn",
    "rate_limit_quotas",
    "rate_limit_remaining_seconds",
    "rate_limited_until",
    "reclaim_agent_name",
    "reconstruct_agents_from_channels",
    "remove_reaction",
    "reset_session",
    "run_initial_prompt",
    "schedule_last_fired",
    "send_long",
    "send_prompt_to_agent",
    "send_system",
    "send_to_exceptions",
    "session_usage",
    "set_utils_mcp_server",
    "shutdown_coordinator",
    "sleep_agent",
    "spawn_agent",
    "split_message",
    "stream_response_to_channel",
    "stream_with_retry",
    "wake_agent",
    "wake_or_queue",
    "wire_conn",
]
