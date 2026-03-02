"""Agent lifecycle, streaming, rate limits, bridge, and channel management."""

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

from agenthub.procmux_wire import ProcmuxProcessConnection
from agenthub.tasks import BackgroundTaskSet
from axi import channels as _channels_mod
from axi import config
from axi.axi_types import (
    ActivityState,
    AgentSession,
    ConcurrencyLimitError,
    ContentBlock,
    MessageContent,
)
from axi.bridge import BridgeTransport, ensure_bridge
from axi.channels import (
    ensure_agent_channel,
    ensure_guild_infrastructure,
    format_channel_topic,
    get_agent_channel,
    get_master_channel,
    move_channel_to_killed,
    normalize_channel_name,
)
from axi.channels import (
    parse_channel_topic as _parse_channel_topic,
)
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
from claudewire.events import as_stream, update_activity
from claudewire.session import disconnect_client, get_stdio_logger

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from axi.bridge import BridgeConnection
    from axi.flowcoder import FlowcoderProcess, ManagedFlowcoderProcess

if config.FLOWCODER_ENABLED:
    from axi.flowcoder import FlowcoderProcess, ManagedFlowcoderProcess

log = logging.getLogger("axi")


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_bot: Bot | None = None

agents: dict[str, AgentSession] = {}
channel_to_agent: dict[int, str] = {}  # channel_id -> agent_name
_wake_lock = asyncio.Lock()


def find_session_by_question_message(message_id: int) -> AgentSession | None:
    """Find the agent session waiting for a reaction answer on this message."""
    for session in agents.values():
        if session.question_message_id == message_id:
            return session
    return None


# Bridge connection — initialized in on_ready(), used by wake_agent/sleep_agent
bridge_conn: BridgeConnection | None = None
# Adapted connection for claudewire (wraps bridge_conn)
wire_conn: ProcmuxProcessConnection | None = None

# Shutdown coordinator — initialized via init_shutdown_coordinator() from on_ready
shutdown_coordinator: ShutdownCoordinator | None = None

# Scheduler state
schedule_last_fired: dict[str, datetime] = {}

# Stream tracing
_stream_counter = 0

# MCP server injection (set by bot.py after tools.py creates them)
_utils_mcp_server: Any = None

# Background task manager — prevents GC of fire-and-forget tasks.
_bg_tasks = BackgroundTaskSet()
fire_and_forget = _bg_tasks.fire_and_forget


def _user_mentions() -> str:
    """Generate Discord @mention string for all allowed users."""
    return " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)


# ---------------------------------------------------------------------------
# Initialization — called once from bot.py after Bot creation
# ---------------------------------------------------------------------------


def init(bot_instance: Bot) -> None:
    """Inject the Bot reference. Called once from bot.py."""
    global _bot
    _bot = bot_instance
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


def _build_mcp_servers(agent_name: str, cwd: str | None = None) -> dict[str, Any]:
    """Build the standard MCP server dict for an agent."""
    servers: dict[str, Any] = {}
    if _utils_mcp_server is not None:
        servers["utils"] = _utils_mcp_server
    servers["schedule"] = make_schedule_mcp_server(agent_name, config.SCHEDULES_PATH, cwd)
    return servers


# ---------------------------------------------------------------------------
# SDK utilities
# ---------------------------------------------------------------------------


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


async def send_long(channel: TextChannel, text: str) -> None:
    """Send a potentially long message, splitting as needed."""
    chunks = split_message(text.strip())
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
                await channel.send(chunk)
            except discord.NotFound:
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


_PLAN_FILE_MAX_AGE_SECS = 120  # Only consider plan files modified within the last 2 minutes


def _read_latest_plan_file() -> str | None:
    """Read the most recently modified plan file from ~/.claude/plans/.

    Claude Code writes plans to ~/.claude/plans/<random-name>.md.  The LLM
    doesn't always include the plan content in the ExitPlanMode tool_input,
    so this serves as a reliable fallback.
    """
    plans_dir = pathlib.Path.home() / ".claude" / "plans"
    if not plans_dir.is_dir():
        return None
    try:
        candidates = sorted(
            plans_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    now = time.time()
    for path in candidates[:1]:
        try:
            age = now - path.stat().st_mtime
            if age > _PLAN_FILE_MAX_AGE_SECS:
                return None
            content = path.read_text(encoding="utf-8").strip()
            return content or None
        except OSError:
            continue
    return None


async def _handle_exit_plan_mode(
    session: AgentSession | None,
    tool_input: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    """Handle ExitPlanMode by posting the plan to Discord and waiting for user approval."""
    if session is None or session.discord_channel_id is None:
        return PermissionResultAllow()

    channel_id = session.discord_channel_id

    async def _send_plan_msg(content: str) -> None:
        await config.discord_client.send_message(channel_id, content)

    plan_content = (tool_input.get("plan") or "").strip() or None

    # Heuristic fallback: the LLM doesn't always include the plan in tool_input
    # (the "plan" key is an additionalProperty, not a defined schema field).
    # Claude Code always writes the plan to ~/.claude/plans/<name>.md before
    # ExitPlanMode fires, so we pick the most recently modified file as a
    # best-effort fallback.  This is imprecise when multiple agents exit plan
    # mode within the same 2-minute window, but in practice that's rare.
    used_heuristic = False
    if not plan_content:
        plan_content = _read_latest_plan_file()
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
        session.plan_approval_message_id = int(approval_msg_id)
    except Exception:
        log.exception("_handle_exit_plan_mode: failed to post plan to Discord \u2014 denying")
        return PermissionResultDeny(message="Could not post plan to Discord for approval. Try again.")

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    session.plan_approval_future = future  # type: ignore[assignment]

    log.info("Agent '%s' paused waiting for plan approval", session.name)

    try:
        result = await future
    finally:
        session.plan_approval_future = None
        session.plan_approval_message_id = None

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
    if session is None or session.discord_channel_id is None:
        return PermissionResultAllow()

    channel_id = session.discord_channel_id
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
        session.question_message_id = msg_id
        session.question_data = q

        future: asyncio.Future[str] = loop.create_future()
        session.question_future = future

        log.info("Agent '%s' waiting for answer to question %d/%d", session.name, i + 1, len(questions))

        try:
            answer = await future
        finally:
            session.question_future = None
            session.question_message_id = None
            session.question_data = None

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
    """Post or update the todo list display in Discord."""
    todos = tool_input.get("todos", [])
    session.todo_items = todos
    _save_todo_items(session.name, todos)
    body = f"**Todo List**\n{format_todo_list(todos)}"
    channel_id = session.discord_channel_id
    if channel_id is None:
        return

    try:
        if session.todo_message_id is not None:
            # Edit existing message
            await config.discord_client.edit_message(channel_id, session.todo_message_id, body)
        else:
            # Create new message
            msg = await config.discord_client.send_message(channel_id, body)
            msg_id = msg.get("id")
            if msg_id is not None:
                session.todo_message_id = int(msg_id)
                # Persist the new message ID
                if session.name == config.MASTER_AGENT_NAME:
                    _save_master_session(session)
                else:
                    _update_channel_topic(session)
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
            if not agent_session.discord_channel_id:
                continue
            ch = bot_ref.get_channel(agent_session.discord_channel_id)
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
        from axi.flowcoder import ManagedFlowcoderProcess as _MFPType

        for s in agents.values():
            if s.flowcoder_process and isinstance(s.flowcoder_process, _MFPType):
                await s.flowcoder_process.detach()
                log.info("Detached managed flowcoder for '%s' before shutdown", s.name)
                s.flowcoder_process = None

        master_ch = await get_master_channel()
        if master_ch:
            await master_ch.send("*System:* Shutting down \u2014 see you soon!")

    use_bridge = bridge_conn is not None and bridge_conn.is_alive
    shutdown_coordinator = make_shutdown_coordinator(
        close_bot_fn=_bot.close,
        kill_fn=exit_for_restart if use_bridge else kill_supervisor,
        goodbye_fn=_send_goodbye,
        bridge_mode=use_bridge,
    )


# ---------------------------------------------------------------------------
# Lifecycle: wake, sleep, reset, reconstruct
# ---------------------------------------------------------------------------


def is_awake(session: AgentSession) -> bool:
    """Check if agent is ready to process messages."""
    if session.agent_type == "flowcoder":
        return session.flowcoder_process is not None
    return session.client is not None


def is_processing(session: AgentSession) -> bool:
    """Check if agent has active work."""
    if session.agent_type == "flowcoder":
        return session.flowcoder_process is not None and session.flowcoder_process.is_running
    return session.query_lock.locked()


def _reset_session_activity(session: AgentSession) -> None:
    """Reset idle tracking and activity state for the start of a new query."""
    session.last_activity = datetime.now(UTC)
    session.last_idle_notified = None
    session.idle_reminder_count = 0
    session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Session lifecycle internals
# ---------------------------------------------------------------------------


async def _create_transport(session: AgentSession, reconnecting: bool = False):
    """Create a transport for Claude Code agent (bridge or direct)."""
    if wire_conn and wire_conn.is_alive:
        transport = BridgeTransport(
            session.name,
            wire_conn,
            reconnecting=reconnecting,
            stderr_callback=make_stderr_callback(session),
            stdio_logger=get_stdio_logger(session.name, config.LOG_DIR),
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
    return sum(
        1
        for s in agents.values()
        if s.client is not None or (s.flowcoder_process is not None and s.flowcoder_process.is_running)
    )


async def _evict_idle_agent(exclude: str | None = None) -> bool:
    """Sleep the most idle non-busy awake agent to free a slot."""
    candidates: list[tuple[float, str, AgentSession]] = []
    for name, s in agents.items():
        if name == exclude:
            continue
        if s.client is None:
            continue
        if s.query_lock.locked():
            continue
        if s.bridge_busy:
            continue
        idle_duration = (datetime.now(UTC) - s.last_activity).total_seconds()
        candidates.append((idle_duration, name, s))

    if not candidates:
        return False

    log.debug("Eviction candidates: %s", [(n, f"{s:.0f}s") for s, n, _ in candidates])
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
    """Ensure there is a free awake-agent slot, evicting idle agents if needed."""
    awake = count_awake_agents()
    while awake >= config.MAX_AWAKE_AGENTS:
        log.debug(
            "Awake slots full (%d/%d), attempting eviction for '%s'",
            awake,
            config.MAX_AWAKE_AGENTS,
            requesting_agent,
        )
        evicted = await _evict_idle_agent(exclude=requesting_agent)
        if not evicted:
            log.warning(
                "Cannot free awake slot for '%s' \u2014 all %d slots busy", requesting_agent, config.MAX_AWAKE_AGENTS
            )
            return False
        awake = count_awake_agents()
    return True


# ---------------------------------------------------------------------------
# Sleep / wake
# ---------------------------------------------------------------------------


async def sleep_agent(session: AgentSession, *, force: bool = False) -> None:
    """Shut down an agent. Keep the AgentSession in the agents dict.

    If force=False (default), skips sleeping if the agent's query_lock is held
    (i.e. the agent is actively processing a query).
    """
    if not force and session.query_lock.locked():
        log.debug("Skipping sleep for '%s' \u2014 query_lock is held", session.name)
        return

    if session.flowcoder_process:
        from axi.flowcoder import ManagedFlowcoderProcess as _MFPType

        proc = session.flowcoder_process
        if isinstance(proc, _MFPType) and session.query_lock.locked():
            await proc.detach()
        else:
            await proc.stop()
        session.flowcoder_process = None

    if session.client is None:
        return

    log.info("Sleeping agent '%s'", session.name)
    if session.agent_log:
        session.agent_log.info("SESSION_SLEEP")
    session.bridge_busy = False
    await _disconnect_client(session.client, session.name)
    session.client = None
    log.info("Agent '%s' is now sleeping", session.name)


def _make_agent_options(session: AgentSession, resume_id: str | None = None) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a session."""
    return ClaudeAgentOptions(
        model=config.get_model(),
        effort="high",
        thinking={"type": "enabled", "budget_tokens": 128000},
        setting_sources=["local"],
        permission_mode="plan" if session.plan_mode else "default",
        can_use_tool=make_cwd_permission_callback(session.cwd, session),
        cwd=session.cwd,
        system_prompt=session.system_prompt,
        include_partial_messages=True,
        stderr=make_stderr_callback(session),
        resume=resume_id,
        sandbox={"enabled": True, "autoAllowBashIfSandboxed": True},
        mcp_servers=session.mcp_servers or {},
        disallowed_tools=["Task"],
    )


async def wake_agent(session: AgentSession) -> None:
    """Wake a sleeping agent. Enforces concurrency limit. Posts system prompt on first wake."""
    assert _bot is not None

    if is_awake(session):
        return

    async with _wake_lock:
        if is_awake(session):
            return

        log.debug(
            "Wake lock acquired for '%s', awake_count=%d/%d",
            session.name,
            count_awake_agents(),
            config.MAX_AWAKE_AGENTS,
        )

        slot_available = await _ensure_awake_slot(session.name)
        if not slot_available:
            raise ConcurrencyLimitError(
                f"Cannot wake agent '{session.name}': all {config.MAX_AWAKE_AGENTS} awake slots are busy. "
                f"Message will be queued and processed when a slot opens."
            )

        log.debug("Awake slot secured for '%s'", session.name)
        log.info("Waking agent '%s' (session_id=%s)", session.name, session.session_id)

        resume_id = session.session_id
        options = _make_agent_options(session, resume_id)

        log.debug("Waking '%s' (resume=%s)", session.name, resume_id)
        try:
            client = ClaudeSDKClient(options=options)
            await client.__aenter__()
            session.client = client
            log.info("Agent '%s' is now awake (resumed=%s)", session.name, resume_id)
            if session.agent_log:
                session.agent_log.info("SESSION_WAKE (resumed=%s)", bool(resume_id))
        except Exception:
            log.warning("Failed to resume agent '%s' with session_id=%s, retrying fresh", session.name, resume_id)
            options = _make_agent_options(session, resume_id=None)
            client = ClaudeSDKClient(options=options)
            await client.__aenter__()
            session.client = client
            session.session_id = None
            if session.name == config.MASTER_AGENT_NAME:
                try:
                    os.remove(config.MASTER_SESSION_PATH)
                except OSError:
                    pass
            log.warning("Agent '%s' woke with fresh session (previous context lost)", session.name)
            if session.agent_log:
                session.agent_log.info("SESSION_WAKE (resumed=False, fresh after resume failure)")

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

        if not session.system_prompt_posted and session.discord_channel_id:
            session.system_prompt_posted = True
            channel = _bot.get_channel(session.discord_channel_id)
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


async def wake_or_queue(
    session: AgentSession,
    content: MessageContent,
    channel: TextChannel,
    orig_message: discord.Message | None,
) -> bool:
    """Try to wake agent, return True if successful, False if queued."""
    try:
        await wake_agent(session)
        return True
    except ConcurrencyLimitError:
        session.message_queue.append((content, channel, orig_message))
        position = len(session.message_queue)
        awake = count_awake_agents()
        log.debug("Concurrency limit hit for '%s', queuing message (position %d)", session.name, position)
        await add_reaction(orig_message, "\U0001f4e8")
        await send_system(channel, f"\u23f3 All {awake} agent slots busy. Message queued (position {position}).")
        return False
    except Exception:
        log.exception("Failed to wake agent '%s'", session.name)
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
    if session.flowcoder_process:
        await session.flowcoder_process.stop()
        session.flowcoder_process = None
    if session.client is not None:
        await _disconnect_client(session.client, name)
        session.client = None
    session.close_log()
    agents.pop(name, None)
    log.info("Session '%s' ended", name)


async def _rebuild_session(name: str, *, cwd: str | None = None, session_id: str | None = None) -> AgentSession:
    """End an existing session and create a fresh sleeping AgentSession.

    Preserves system prompt, channel mapping, and MCP servers from the old session.
    """
    session = agents.get(name)
    old_cwd = session.cwd if session else config.DEFAULT_CWD
    old_channel_id = session.discord_channel_id if session else None
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
        discord_channel_id=old_channel_id,
        mcp_servers=old_mcp,
    )
    agents[name] = new_session
    return new_session


async def reset_session(name: str, cwd: str | None = None) -> AgentSession:
    """Reset a named session. Preserves system prompt, channel mapping, and MCP servers."""
    new_session = await _rebuild_session(name, cwd=cwd)
    log.info("Session '%s' reset (sleeping, cwd=%s)", name, new_session.cwd)
    return new_session


def get_master_session() -> AgentSession | None:
    """Get the axi-master session."""
    return agents.get(config.MASTER_AGENT_NAME)


async def reconstruct_agents_from_channels() -> int:
    """Reconstruct sleeping AgentSession entries from existing Discord channels."""
    reconstructed = 0
    if not _channels_mod.active_category:
        return reconstructed

    for cat in [_channels_mod.active_category]:
        for ch in cat.text_channels:
            agent_name = ch.name

            if agent_name == normalize_channel_name(config.MASTER_AGENT_NAME):
                channel_to_agent[ch.id] = config.MASTER_AGENT_NAME
                continue

            if agent_name in agents:
                channel_to_agent[ch.id] = agent_name
                continue

            cwd, session_id, old_prompt_hash, todo_msg, agent_type = _parse_channel_topic(ch.topic)
            if cwd is None:
                log.debug("No cwd in topic for channel #%s, skipping", agent_name)
                continue

            prompt = make_spawned_agent_system_prompt(cwd)
            mcp_servers = _build_mcp_servers(agent_name, cwd)

            session = AgentSession(
                name=agent_name,
                agent_type=agent_type or "flowcoder",
                client=None,
                cwd=cwd,
                system_prompt=prompt,
                system_prompt_hash=old_prompt_hash,
                session_id=session_id,
                discord_channel_id=ch.id,
                mcp_servers=mcp_servers,
                todo_items=load_todo_items(agent_name),
                todo_message_id=todo_msg,
            )
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
    if session.name == config.MASTER_AGENT_NAME or not session.discord_channel_id:
        return
    ch = channel or _bot.get_channel(session.discord_channel_id)
    if not ch or not isinstance(ch, TextChannel):
        return
    desired_topic = format_channel_topic(
        session.cwd,
        session.session_id,
        session.system_prompt_hash,
        session.todo_message_id,
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
    """Save master agent session metadata (session_id, prompt_hash, todo_message_id) to disk."""
    try:
        data: dict[str, Any] = {}
        if session.session_id:
            data["session_id"] = session.session_id
        if session.system_prompt_hash:
            data["prompt_hash"] = session.system_prompt_hash
        if session.todo_message_id is not None:
            data["todo_message_id"] = session.todo_message_id
        with open(config.MASTER_SESSION_PATH, "w") as f:
            json.dump(data, f)
        log.info("Saved master session data to %s", config.MASTER_SESSION_PATH)
    except OSError:
        log.warning("Failed to save master session data", exc_info=True)


async def _set_session_id(session: AgentSession, msg_or_sid: Any, channel: TextChannel | None = None) -> None:
    """Update session's session_id and persist it (topic or file)."""
    assert _bot is not None
    sid: str | None = msg_or_sid if isinstance(msg_or_sid, str) else getattr(msg_or_sid, "session_id", None)
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
    if model == "opus" or not session.discord_channel_id:
        return
    channel = _bot.get_channel(session.discord_channel_id)
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


_KNOWN_ENGINE_TYPES = {"assistant", "stream_event", "result", "system"}


def _parse_flowcoder_message(
    data: dict[str, Any],
) -> AssistantMessage | StreamEvent | ResultMessage | SystemMessage | None:
    """Parse a flowcoder engine JSON message into an SDK typed object.

    Returns None for message types not handled by SDK (e.g. rate_limit_event).
    Fills missing fields that the engine doesn't emit but parse_message requires.
    """
    msg_type = data.get("type", "")
    if data.get("type") == "result":
        data.setdefault("duration_api_ms", 0)
    try:
        return parse_message(data)  # type: ignore[return-value]
    except MessageParseError as exc:
        if msg_type in _KNOWN_ENGINE_TYPES:
            log.warning("Failed to parse flowcoder %s message: %s  data=%s", msg_type, exc, str(data)[:300])
        return None


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


class _StreamCtx:
    """Mutable state for a single stream_response_to_channel invocation."""

    __slots__ = (
        "flush_count",
        "hit_rate_limit",
        "hit_transient_error",
        "msg_total",
        "text_buffer",
        "tool_input_json",
        "typing_stopped",
    )

    def __init__(self) -> None:
        self.text_buffer: str = ""
        self.hit_rate_limit: bool = False
        self.hit_transient_error: str | None = None
        self.typing_stopped: bool = False
        self.flush_count: int = 0
        self.msg_total: int = 0
        self.tool_input_json: str = ""  # Accumulates full tool input JSON for current tool_use block


async def _flush_text(ctx: _StreamCtx, session: AgentSession, channel: TextChannel, reason: str = "?") -> None:
    """Flush accumulated text buffer to Discord."""
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
    await send_long(channel, text.lstrip())


def _stop_typing(ctx: _StreamCtx, typing_ctx: Any) -> None:
    """Cancel the typing indicator."""
    if not ctx.typing_stopped and typing_ctx and typing_ctx.task:
        typing_ctx.task.cancel()
        ctx.typing_stopped = True


async def _drain_stderr_to_channel(session: AgentSession, channel: TextChannel) -> None:
    """Send any accumulated stderr to the channel."""
    for stderr_msg in drain_stderr(session):
        stderr_text = stderr_msg.strip()
        if stderr_text:
            for part in split_message(f"```\n{stderr_text}\n```"):
                await channel.send(part)


# ---------------------------------------------------------------------------
# Stream event handlers
# ---------------------------------------------------------------------------


async def _handle_stream_event(
    ctx: _StreamCtx, session: AgentSession, channel: TextChannel, msg: StreamEvent, typing_ctx: Any
) -> None:
    """Handle a StreamEvent during response streaming."""
    event = msg.event
    event_type = event.get("type", "")

    if msg.session_id and msg.session_id != session.session_id:
        await _set_session_id(session, msg.session_id, channel=channel)

    _update_activity(session, event)

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

    # Debug output
    if session.debug and event_type == "content_block_stop":
        if session.activity.phase == "thinking" and session.activity.thinking_text:
            thinking = session.activity.thinking_text.strip()
            if thinking:
                file = discord.File(io.BytesIO(thinking.encode("utf-8")), filename="thinking.md")
                await channel.send("\U0001f4ad", file=file)
                session.activity.thinking_text = ""
        elif session.activity.phase == "waiting" and session.activity.tool_name:
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
    elif event_type == "message_delta":
        stop_reason = event.get("delta", {}).get("stop_reason")
        if stop_reason == "end_turn":
            await _flush_text(ctx, session, channel, "end_turn")
            ctx.text_buffer = ""
            _stop_typing(ctx, typing_ctx)


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
    ctx: _StreamCtx, session: AgentSession, channel: TextChannel, msg: AssistantMessage, typing_ctx: Any
) -> None:
    """Handle an AssistantMessage during response streaming."""
    if msg.error in ("rate_limit", "billing_error"):
        error_text = ctx.text_buffer
        for block in msg.content or []:
            if hasattr(block, "text"):
                error_text += " " + cast("str", getattr(block, "text", ""))
        log.warning("Agent '%s' hit %s error: %s", session.name, msg.error, error_text[:200])
        _stop_typing(ctx, typing_ctx)
        await _handle_rate_limit(error_text, session, channel)
        ctx.text_buffer = ""
        ctx.hit_rate_limit = True
    elif msg.error:
        error_text = ctx.text_buffer
        for block in msg.content or []:
            if hasattr(block, "text"):
                error_text += " " + cast("str", getattr(block, "text", ""))
        log.warning("Agent '%s' hit API error (%s): %s", session.name, msg.error, error_text[:200])
        _stop_typing(ctx, typing_ctx)
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
        _stop_typing(ctx, typing_ctx)

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
    ctx: _StreamCtx, session: AgentSession, channel: TextChannel, msg: ResultMessage, typing_ctx: Any
) -> None:
    """Handle a ResultMessage during response streaming."""
    _stop_typing(ctx, typing_ctx)
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
    if msg.subtype == "compact_boundary":
        metadata = msg.data.get("compact_metadata", {})
        trigger = metadata.get("trigger", "unknown")
        pre_tokens = metadata.get("pre_tokens")
        log.info("Agent '%s' context compacted: trigger=%s pre_tokens=%s", session.name, trigger, pre_tokens)
        token_info = f" ({pre_tokens:,} tokens)" if pre_tokens else ""
        await channel.send(f"\U0001f504 Context compacted{token_info}")

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

    ctx = _StreamCtx()

    async with channel.typing() as typing_ctx:
        async for msg in _receive_response_safe(session):
            ctx.msg_total += 1
            if session.agent_log:
                session.agent_log.debug(
                    "MSG_SEQ[%s][%d] type=%s buf_len=%d",
                    stream_id,
                    ctx.msg_total,
                    type(msg).__name__,
                    len(ctx.text_buffer),
                )

            await _drain_stderr_to_channel(session, channel)

            if isinstance(msg, StreamEvent):
                await _handle_stream_event(ctx, session, channel, msg, typing_ctx)
            elif isinstance(msg, AssistantMessage):
                await _handle_assistant_message(ctx, session, channel, msg, typing_ctx)
            elif isinstance(msg, ResultMessage):
                await _handle_result_message(ctx, session, channel, msg, typing_ctx)
            elif isinstance(msg, SystemMessage):
                await _handle_system_message(session, channel, msg)
            elif session.agent_log:
                session.agent_log.debug("OTHER_MSG: %s", type(msg).__name__)

            # Mid-turn flush
            if not ctx.hit_rate_limit and len(ctx.text_buffer) >= 1800:
                split_at = ctx.text_buffer.rfind("\n", 0, 1800)
                if split_at == -1:
                    split_at = 1800
                remainder = ctx.text_buffer[split_at:].lstrip("\n")
                ctx.text_buffer = ctx.text_buffer[:split_at]
                await _flush_text(ctx, session, channel, "mid_turn_split")
                ctx.text_buffer = remainder

    # Flush remaining stderr
    await _drain_stderr_to_channel(session, channel)

    if ctx.hit_rate_limit:
        log.info("STREAM_END[%s] result=rate_limit msgs=%d flushes=%d", stream_id, ctx.msg_total, ctx.flush_count)
        return None

    if ctx.hit_transient_error:
        log.info(
            "STREAM_END[%s] result=transient_error(%s) msgs=%d flushes=%d",
            stream_id,
            ctx.hit_transient_error,
            ctx.msg_total,
            ctx.flush_count,
        )
        return ctx.hit_transient_error

    await _flush_text(ctx, session, channel, "post_loop")
    log.info("STREAM_END[%s] result=ok msgs=%d flushes=%d", stream_id, ctx.msg_total, ctx.flush_count)

    if config.SHOW_AWAITING_INPUT:
        mentions = " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)
        await send_system(channel, f"Bot has finished responding and is awaiting input. {mentions}")

    return None


# ---------------------------------------------------------------------------
# Retry / timeout
# ---------------------------------------------------------------------------


async def stream_with_retry(session: AgentSession, channel: TextChannel) -> bool:
    """Stream response with retry on transient API errors. Returns True on success."""
    log.info("RETRY_ENTER[%s] starting initial stream", session.name)
    error = await stream_response_to_channel(session, channel)
    if error is None:
        log.info("RETRY_EXIT[%s] first attempt succeeded", session.name)
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
            return True

    log.error(
        "Agent '%s' transient error persisted after %d retries",
        session.name,
        config.API_ERROR_MAX_RETRIES,
    )
    await channel.send(f"\u274c API error persisted after {config.API_ERROR_MAX_RETRIES} retries. Try again later.")
    return False


async def handle_query_timeout(session: AgentSession, channel: TextChannel) -> None:
    """Handle a query timeout. Try interrupt first, then kill and resume."""
    log.warning("Query timeout for agent '%s', attempting interrupt", session.name)

    try:
        if bridge_conn and bridge_conn.is_alive:
            result = await bridge_conn.send_command("interrupt", name=session.name)
            if not result.ok:
                log.warning("Bridge SIGINT for '%s' failed: %s", session.name, result.error)
        try:
            if session.client is not None:
                await session.client.interrupt()
        except Exception:
            pass
        async with asyncio.timeout(config.INTERRUPT_TIMEOUT):
            async for msg in _receive_response_safe(session):
                if isinstance(msg, ResultMessage):
                    await _set_session_id(session, msg, channel=channel)
                    break
        session.last_activity = datetime.now(UTC)
        await send_system(
            channel,
            f"Agent **{session.name}** timed out and was interrupted. Context preserved.",
        )
        return
    except Exception:
        log.warning("Interrupt failed for agent '%s', killing and resuming session", session.name)

    old_session_id = session.session_id
    new_session = await _rebuild_session(session.name, session_id=old_session_id)

    if old_session_id:
        await send_system(
            channel, f"Agent **{new_session.name}** timed out and was recovered (sleeping). Context preserved."
        )
    else:
        await send_system(channel, f"Agent **{new_session.name}** timed out and was reset (sleeping). Context lost.")


# ---------------------------------------------------------------------------
# Message processing, spawning, and inter-agent delivery
# ---------------------------------------------------------------------------


async def process_message(session: AgentSession, content: MessageContent, channel: TextChannel) -> None:
    """Process a user message through the appropriate agent type.

    Flowcoder agents are a superset of Claude Code agents — they use the same
    Claude session for conversation, but can also run flowcharts. If a flowchart
    is actively running, forward the message to the engine instead.
    """
    # If a flowchart engine is actively running, forward to it
    if session.flowcoder_process and session.flowcoder_process.is_running:
        if isinstance(content, str):
            text = content
        else:
            text_parts = [b["text"] for b in content if b.get("type") == "text"]
            if not text_parts:
                await send_system(channel, "Flowcharts don't support image-only messages.")
                return
            text = "\n".join(text_parts)
        user_msg = {
            "type": "user",
            "message": {"role": "user", "content": text},
        }
        await session.flowcoder_process.send(user_msg)
        log.debug("Sent message to flowcoder engine '%s'", session.name)
        return

    # Claude Code / Flowcoder agent — use the Claude session
    if session.client is None:
        raise RuntimeError(f"Agent '{session.name}' not awake")

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
    try:
        async with asyncio.timeout(config.QUERY_TIMEOUT):
            await session.client.query(as_stream(content))
            await stream_with_retry(session, channel)
    except TimeoutError:
        await handle_query_timeout(session, channel)
    except Exception:
        log.exception("Error querying Claude Code agent '%s'", session.name)
        raise RuntimeError(f"Query failed for agent '{session.name}'") from None


# ---------------------------------------------------------------------------
# Agent spawning
# ---------------------------------------------------------------------------


async def reclaim_agent_name(name: str) -> None:
    """If an agent with *name* already exists, kill it silently to free the name."""
    if name not in agents:
        return
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
) -> None:
    """Spawn a new agent session and run its initial prompt in the background."""
    os.makedirs(cwd, exist_ok=True)

    normalized = normalize_channel_name(name)
    _channels_mod.bot_creating_channels.add(normalized)
    channel = await ensure_agent_channel(name)

    agent_label = "flowcoder" if agent_type == "flowcoder" else "claude code"
    if resume:
        await send_system(
            channel, f"Resuming **{agent_label}** agent **{name}** (session `{resume[:8]}\u2026`) in `{cwd}`..."
        )
    else:
        await send_system(channel, f"Spawning **{agent_label}** agent **{name}** in `{cwd}`...")

    prompt = make_spawned_agent_system_prompt(cwd, packs=packs)
    mcp_servers = _build_mcp_servers(name, cwd)

    session = AgentSession(
        name=name,
        agent_type=agent_type,
        cwd=cwd,
        system_prompt=prompt,
        system_prompt_hash=compute_prompt_hash(prompt),
        client=None,
        session_id=resume,
        discord_channel_id=channel.id,
        mcp_servers=mcp_servers,
    )

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
        await send_system(channel, f"Agent **{session.name}** finished initial task. {_user_mentions()}")

    except Exception:
        log.exception("Error running initial prompt for agent '%s'", session.name)
        await send_system(
            channel, f"Agent **{session.name}** encountered an error during initial task. {_user_mentions()}"
        )

    await process_message_queue(session)

    try:
        await sleep_agent(session)
    except Exception:
        log.exception("Error sleeping agent '%s' after initial prompt", session.name)


async def process_message_queue(session: AgentSession) -> None:
    """Process any queued messages for an agent after the current query finishes."""
    if session.message_queue:
        log.info("QUEUE[%s] processing %d queued messages", session.name, len(session.message_queue))
    while session.message_queue:
        if shutdown_coordinator and shutdown_coordinator.requested:
            log.info("Shutdown requested \u2014 not processing further queued messages for '%s'", session.name)
            break
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


# ---------------------------------------------------------------------------
# Inter-agent messaging
# ---------------------------------------------------------------------------


async def deliver_inter_agent_message(
    sender_name: str,
    target_session: AgentSession,
    content: str,
) -> str:
    """Deliver a message from one agent to another."""
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
        try:
            if bridge_conn and bridge_conn.is_alive:
                await bridge_conn.send_command("interrupt", name=target_session.name)
            if target_session.client:
                try:
                    await target_session.client.interrupt()
                except Exception:
                    pass
        except Exception:
            log.exception(
                "Failed to interrupt '%s' for inter-agent message (message still queued)",
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

        await process_message_queue(session)
    except Exception:
        log.exception(
            "Unhandled error in _process_inter_agent_prompt for '%s'",
            session.name,
        )


# ---------------------------------------------------------------------------
# Bridge connection and reconnection logic
# ---------------------------------------------------------------------------


async def connect_bridge() -> None:
    """Connect to the agent bridge and schedule reconnections for running agents."""
    global bridge_conn, wire_conn

    try:
        bridge_conn = await ensure_bridge(config.BRIDGE_SOCKET_PATH, timeout=10.0)
        wire_conn = ProcmuxProcessConnection(bridge_conn)
        log.info("Bridge connection established")
    except Exception:
        log.exception("Failed to connect to bridge \u2014 agents will use direct subprocess mode")
        bridge_conn = None
        wire_conn = None
        return

    try:
        result = await bridge_conn.send_command("list")
        bridge_agents = result.agents or {}
        log.info("Bridge reports %d agent(s): %s", len(bridge_agents), list(bridge_agents.keys()))
    except Exception:
        log.exception("Failed to list bridge agents")
        return

    if not bridge_agents:
        return

    for agent_name, info in bridge_agents.items():
        if agent_name.endswith(":flowcoder"):
            if not config.FLOWCODER_ENABLED:
                log.info("Flowcoder disabled \u2014 killing managed flowcoder '%s'", agent_name)
                try:
                    await wire_conn.kill(agent_name)
                except Exception:
                    log.exception("Failed to kill managed flowcoder '%s' (disabled)", agent_name)
                continue
            base_name = agent_name.removesuffix(":flowcoder")
            session = agents.get(base_name)
            if session is None:
                log.warning("Procmux has flowcoder '%s' but no matching session \u2014 killing", agent_name)
                try:
                    await wire_conn.kill(agent_name)
                except Exception:
                    log.exception("Failed to kill orphan flowcoder '%s'", agent_name)
                continue
            if info.get("status") == "exited":
                log.info("Managed flowcoder '%s' already exited \u2014 cleaning up", agent_name)
                try:
                    await wire_conn.kill(agent_name)
                except Exception:
                    pass
                continue
            session.reconnecting = True
            fire_and_forget(_reconnect_flowcoder(session, agent_name, info))
            continue

        session = agents.get(agent_name)
        if session is None:
            log.warning("Bridge has agent '%s' but no matching session \u2014 killing", agent_name)
            try:
                await bridge_conn.send_command("kill", name=agent_name)
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
    """Reconnect a single agent to the bridge and drain any buffered output."""
    try:
        async with session.query_lock:
            if bridge_conn is None or not bridge_conn.is_alive:
                log.warning("Bridge connection lost during reconnect of '%s'", session.name)
                session.reconnecting = False
                return

            transport = await _create_transport(session, reconnecting=True)
            assert transport is not None

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

            options = ClaudeAgentOptions(
                can_use_tool=make_cwd_permission_callback(session.cwd, session),
                mcp_servers=session.mcp_servers or {},
                permission_mode="plan" if session.plan_mode else "default",
                cwd=session.cwd,
                include_partial_messages=True,
                stderr=make_stderr_callback(session),
                disallowed_tools=["Task"],
            )

            client = ClaudeSDKClient(options=options, transport=transport)  # pyright: ignore[reportArgumentType]
            await client.__aenter__()
            session.client = client
            session.last_activity = datetime.now(UTC)

            if session.agent_log:
                session.agent_log.info(
                    "SESSION_RECONNECT via bridge (replayed=%d, idle=%s)",
                    replayed,
                    cli_idle,
                )

            if cli_status == "exited":
                log.info("Agent '%s' CLI exited while we were down", session.name)
                session.reconnecting = False

            session.reconnecting = False

            if cli_status == "running" and not cli_idle:
                session.bridge_busy = True
                channel = await get_agent_channel(session.name)
                if replayed > 0 and channel:
                    log.info("RECONNECT_DRAIN[%s] draining buffered output (replayed=%d)", session.name, replayed)
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
                elif channel:
                    await send_system(channel, "*(reconnected after restart \u2014 task still running)*")
                log.info(
                    "Agent '%s' reconnected mid-task (idle=False, replayed=%d, bridge_busy=%s)",
                    session.name,
                    replayed,
                    session.bridge_busy,
                )
            elif cli_status == "running":
                channel = await get_agent_channel(session.name)
                if channel:
                    await send_system(channel, "*(reconnected after restart)*")
                log.info("Agent '%s' reconnected idle (between turns)", session.name)

            log.info("Reconnect complete for '%s'", session.name)

    except Exception:
        log.exception("Failed to reconnect agent '%s'", session.name)
        session.reconnecting = False

    await process_message_queue(session)


async def _reconnect_flowcoder(session: AgentSession, process_name: str, process_info: dict[str, Any]) -> None:
    """Reconnect to a flowcoder engine that survived bot.py restart."""
    log.info("Reconnecting flowcoder '%s' for session '%s'", process_name, session.name)
    try:
        async with session.query_lock:
            if wire_conn is None or not wire_conn.is_alive:
                log.warning("Procmux connection lost during flowcoder reconnect of '%s'", process_name)
                session.reconnecting = False
                return

            proc = ManagedFlowcoderProcess(
                process_name=process_name,
                conn=wire_conn,
                command=session.flowcoder_command,
                args=session.flowcoder_args,
                cwd=session.cwd,
            )
            sub_result = await proc.subscribe()
            session.flowcoder_process = proc
            session.reconnecting = False

            replayed = sub_result.replayed or 0
            log.info(
                "Flowcoder '%s' subscribed (replayed=%d, status=%s)",
                process_name,
                replayed,
                sub_result.status,
            )

            channel = await get_agent_channel(session.name)

            await proc.send({"type": "status_request"})

            if channel:
                if replayed > 0:
                    await send_system(channel, "*(reconnected \u2014 resuming flowchart)*")
                await _stream_flowcoder_to_channel(session, channel)

            await proc.stop()
            session.flowcoder_process = None
            session.activity = ActivityState(phase="idle")
            log.info("Flowcoder '%s' reconnect complete \u2014 cleaned up", process_name)

    except Exception:
        log.exception("Failed to reconnect flowcoder '%s'", process_name)
        session.reconnecting = False

    await process_message_queue(session)


# ---------------------------------------------------------------------------
# Flowcoder streaming — reuses Claude Code message handlers
# ---------------------------------------------------------------------------


async def _auto_approve_control(proc: Any, raw: dict[str, Any]) -> None:
    """Auto-approve a control_request from the flowcoder engine."""
    request = raw.get("request", raw)
    request_id = request.get("request_id", "")
    await proc.send(
        {
            "type": "control_response",
            "response": {"request_id": request_id, "allowed": True},
        }
    )


async def _stream_flowcoder_to_channel(session: AgentSession, channel: TextChannel) -> None:
    """Stream flowcoder engine messages to Discord, reusing Claude Code handlers.

    The engine emits the same message types as Claude Code (assistant,
    stream_event, result, system) plus flowchart-specific system subtypes.
    Messages are parsed into SDK typed objects and dispatched through the
    same handlers used by stream_response_to_channel.
    """
    proc = session.flowcoder_process
    assert proc is not None

    ctx = _StreamCtx()
    in_flowchart = False  # Track whether we're inside a flowchart execution

    async with channel.typing() as typing_ctx:
        log.info("Flowcoder streaming started for '%s', waiting for messages...", session.name)
        async for raw in proc.messages():
            session.last_activity = datetime.now(UTC)
            raw_type = raw.get("type", "")
            log.debug("Flowcoder raw message for '%s': type=%s", session.name, raw_type)

            # Auto-approve control requests from inner Claude
            if raw_type == "control_request":
                await _auto_approve_control(proc, raw)
                continue

            # Skip status_response (used for reconnect polling)
            if raw_type == "status_response":
                if not raw.get("busy", False):
                    break
                continue

            # Replace inner Claude session_ids with the agent's own session_id
            # so they don't overwrite the agent's real session.  Keep session_id
            # on result messages — we need the original value to detect flowchart
            # results (session_id=="flowchart") vs inner Claude results.
            # We replace rather than remove because parse_message requires
            # session_id for stream_event messages.
            if raw_type != "result" and "session_id" in raw:
                raw["session_id"] = session.session_id or ""

            parsed = _parse_flowcoder_message(raw)
            if parsed is None:
                continue

            # During flowcharts, don't stop the typing indicator on inner
            # Claude assistant messages — the flowchart is still running.
            # The final result handler will stop typing.
            fc_typing = None if in_flowchart else typing_ctx

            if isinstance(parsed, StreamEvent):
                await _handle_stream_event(ctx, session, channel, parsed, fc_typing)
            elif isinstance(parsed, AssistantMessage):
                await _handle_assistant_message(ctx, session, channel, parsed, fc_typing)
            elif isinstance(parsed, ResultMessage):
                # During flowcharts, inner block results should not end the stream —
                # only the final result (session_id="flowchart") or a proxy turn result should.
                if in_flowchart and parsed.session_id != "flowchart":
                    # Inner block result — flush text buffer between blocks, then continue
                    if ctx.text_buffer.strip():
                        await _flush_text(ctx, session, channel, "block_result")
                    continue
                await _handle_result_message(ctx, session, channel, parsed, typing_ctx)
                break
            else:  # SystemMessage
                # Track flowchart state
                if parsed.subtype == "flowchart_start":
                    in_flowchart = True
                elif parsed.subtype == "flowchart_complete":
                    in_flowchart = False
                await _handle_system_message(session, channel, parsed, ctx)

            # Mid-turn flush (same as Claude Code path)
            if not ctx.hit_rate_limit and len(ctx.text_buffer) >= 1800:
                split_at = ctx.text_buffer.rfind("\n", 0, 1800)
                if split_at == -1:
                    split_at = 1800
                remainder = ctx.text_buffer[split_at:].lstrip("\n")
                ctx.text_buffer = ctx.text_buffer[:split_at]
                await _flush_text(ctx, session, channel, "mid_turn_split")
                ctx.text_buffer = remainder

    # Final flush
    if ctx.text_buffer.strip():
        await _flush_text(ctx, session, channel, "flowcoder_end")


# ---------------------------------------------------------------------------
# Run helpers
# ---------------------------------------------------------------------------


async def _run_and_stream_flowcoder(
    session: AgentSession, channel: TextChannel, command: str, args: str, label: str
) -> None:
    """Shared: create flowcoder process, stream output, handle cancellation/cleanup."""
    if wire_conn and wire_conn.is_alive:
        proc = ManagedFlowcoderProcess(
            process_name=f"{session.name}:flowcoder",
            conn=wire_conn,
            command=command,
            args=args,
            cwd=session.cwd,
        )
    else:
        proc = FlowcoderProcess(command=command, args=args, cwd=session.cwd)
    await proc.start()
    session.flowcoder_process = proc

    # The engine is a persistent proxy — send the flowchart command as a
    # user message with a slash command so the engine intercepts it.
    slash_content = f"/{command}" + (f" {args}" if args else "")
    await proc.send(
        {
            "type": "user",
            "message": {"content": slash_content},
        }
    )
    log.info("Sent flowchart command to engine: %s", slash_content)

    try:
        await _stream_flowcoder_to_channel(session, channel)
    except asyncio.CancelledError:
        if isinstance(proc, ManagedFlowcoderProcess):
            await proc.detach()
            session.flowcoder_process = None
            session.activity = ActivityState(phase="idle")
            log.info("%s '%s' detached on cancel (procmux will buffer)", label, session.name)
            raise
        raise
    except Exception:
        log.exception("Error streaming %s for '%s'", label, session.name)
        await send_system(channel, f"{label} **{session.name}** encountered a streaming error.")
    finally:
        if session.flowcoder_process is not None:
            await proc.stop()
            session.flowcoder_process = None
            session.activity = ActivityState(phase="idle")


async def run_inline_flowchart(session: AgentSession, channel: TextChannel, command: str, args: str) -> None:
    """Run a flowchart command inline in an agent's channel."""
    async with session.query_lock:
        session.last_activity = datetime.now(UTC)
        session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))

        cmd_display = command + (f" {args}" if args else "")
        await send_system(channel, f"Running flowchart: `{cmd_display}`")
        await _run_and_stream_flowcoder(session, channel, command, args, f"Flowchart `{command}`")

    await process_message_queue(session)


# ---------------------------------------------------------------------------
# Re-exports from channels module (agents.py used to re-export these)
# ---------------------------------------------------------------------------

__all__ = [
    "_parse_channel_topic",
    # Discord helpers
    "add_reaction",
    # Module-level state
    "agents",
    # Streaming
    "as_stream",
    "bridge_conn",
    "channel_to_agent",
    # Bridge
    "connect_bridge",
    "content_summary",
    "count_awake_agents",
    "deliver_inter_agent_message",
    # SDK helpers
    "drain_sdk_buffer",
    "drain_stderr",
    "end_session",
    # Channel/guild management (re-exported from channels module)
    "ensure_agent_channel",
    "ensure_guild_infrastructure",
    # Message handling
    "extract_message_content",
    "extract_tool_preview",
    "format_channel_topic",
    "format_time_remaining",
    "format_todo_list",
    "get_agent_channel",
    "get_master_channel",
    "get_master_session",
    "handle_query_timeout",
    # Initialization
    "init",
    "init_shutdown_coordinator",
    # Session lifecycle
    "is_awake",
    "is_processing",
    # Rate limiting
    "is_rate_limited",
    "load_todo_items",
    "make_cwd_permission_callback",
    "make_shutdown_coordinator",
    "make_stderr_callback",
    "move_channel_to_killed",
    "normalize_channel_name",
    "process_message",
    "process_message_queue",
    "rate_limit_quotas",
    "rate_limit_remaining_seconds",
    "rate_limited_until",
    "reclaim_agent_name",
    "reconstruct_agents_from_channels",
    "remove_reaction",
    "reset_session",
    "run_initial_prompt",
    # Flowcoder
    "run_inline_flowchart",
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
