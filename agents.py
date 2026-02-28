# pyright: reportPrivateUsage=false, reportUnusedFunction=false
"""Agent lifecycle, streaming, rate limits, bridge, and channel management.

This is the engine module: everything that operates on AgentSession state lives here.
Merges the former handlers.py (now deleted) — handler classes are replaced by plain functions.

Dependency: imports config, types, prompts, bridge, schedule_tools, shutdown.
Nothing imports from this module except tools.py and bot.py.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import signal
import time
import traceback
from datetime import UTC, datetime, timedelta
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
from discord import CategoryChannel, TextChannel

import config
from axi_types import (
    ActivityState,
    AgentSession,
    ConcurrencyLimitError,
    ContentBlock,
    MessageContent,
    RateLimitQuota,
    SessionUsage,
)
from bridge import BridgeConnection, BridgeTransport, ensure_bridge
from prompts import (
    _compute_prompt_hash,
    _make_spawned_agent_system_prompt,
    _post_system_prompt_to_channel,
)
from schedule_tools import make_schedule_mcp_server
from shutdown import ShutdownCoordinator, exit_for_restart, kill_supervisor

if TYPE_CHECKING:
    from discord.ext.commands import Bot

    from flowcoder import BridgeFlowcoderProcess, FlowcoderProcess

if config.FLOWCODER_ENABLED:
    from flowcoder import BridgeFlowcoderProcess, FlowcoderProcess

log = logging.getLogger("axi")


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_bot: Bot | None = None

agents: dict[str, AgentSession] = {}
channel_to_agent: dict[int, str] = {}  # channel_id -> agent_name
_wake_lock = asyncio.Lock()

# Bridge connection — initialized in on_ready(), used by wake_agent/sleep_agent
bridge_conn: BridgeConnection | None = None

# Shutdown coordinator — initialized via init_shutdown_coordinator() from on_ready
shutdown_coordinator: ShutdownCoordinator | None = None

# Global rate limit state (all agents share the same API account)
_rate_limited_until: datetime | None = None
_session_usage: dict[str, SessionUsage] = {}
_rate_limit_quotas: dict[str, RateLimitQuota] = {}

# Guild infrastructure (populated in on_ready)
target_guild: discord.Guild | None = None
active_category: CategoryChannel | None = None
killed_category: CategoryChannel | None = None
_bot_creating_channels: set[str] = set()  # channel names currently being created

# Exceptions channel (REST-based, works in any context)
_exceptions_channel_id: str | None = None

# Scheduler state
schedule_last_fired: dict[str, datetime] = {}

# Stream tracing
_stream_counter = 0


# ---------------------------------------------------------------------------
# Initialization — called once from bot.py after Bot creation
# ---------------------------------------------------------------------------


def init(bot_instance: Bot) -> None:
    """Inject the Bot reference. Called once from bot.py."""
    global _bot
    _bot = bot_instance


# ---------------------------------------------------------------------------
# Handler-replacement helpers
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
# Stderr / SDK utilities
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


async def _as_stream(content: MessageContent):
    """Wrap a prompt as an AsyncIterable for streaming mode."""
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


def drain_sdk_buffer(session: AgentSession) -> int:
    """Drain any stale messages from the SDK message buffer before sending a new query."""
    if session.client is None or getattr(session.client, "_query", None) is None:
        return 0

    client = session.client
    assert client._query is not None  # narrowing: getattr check above guarantees this
    receive_stream = client._query._message_receive
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
# Emoji reaction helpers
# ---------------------------------------------------------------------------


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


async def _extract_message_content(message: discord.Message) -> MessageContent:
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


def _content_summary(content: MessageContent) -> str:
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
# Stream tracing (debug instrumentation)
# ---------------------------------------------------------------------------


def _next_stream_id(agent_name: str) -> str:
    """Generate a unique stream ID for tracing."""
    global _stream_counter
    _stream_counter += 1
    return f"{agent_name}:S{_stream_counter}"


# ---------------------------------------------------------------------------
# Permission callbacks
# ---------------------------------------------------------------------------


def make_cwd_permission_callback(allowed_cwd: str, session: AgentSession | None = None):
    """Create a can_use_tool callback that restricts file writes to allowed_cwd and AXI_USER_DATA."""
    allowed = os.path.realpath(allowed_cwd)
    user_data = os.path.realpath(config.AXI_USER_DATA)
    worktrees = os.path.realpath(config.BOT_WORKTREES_DIR)
    bot_dir = os.path.realpath(config.BOT_DIR)

    is_code_agent = (
        allowed in (bot_dir, worktrees)
        or allowed.startswith((bot_dir + os.sep, worktrees + os.sep))
    )
    bases = [allowed, user_data]
    if is_code_agent:
        bases.append(worktrees)
        bases.extend(config.ADMIN_ALLOWED_CWDS)

    async def _check_permission(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        forbidden_tools = {"AskUserQuestion", "TodoWrite", "Skill", "EnterWorktree", "Task"}
        if tool_name in forbidden_tools:
            return PermissionResultDeny(
                message=f"{tool_name} is not compatible with Discord-based agent mode. Use text messages to communicate instead."
            )

        if tool_name == "EnterPlanMode":
            return PermissionResultAllow()

        if tool_name == "ExitPlanMode":
            return await _handle_exit_plan_mode(session, tool_input)

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


async def _handle_exit_plan_mode(
    session: AgentSession | None,
    tool_input: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    """Handle ExitPlanMode by posting the plan to Discord and waiting for user approval."""
    if session is None or session.discord_channel_id is None:
        return PermissionResultAllow()

    channel_id = session.discord_channel_id

    async def _send_plan_msg(content: str) -> None:
        await config._discord_request("POST", f"/channels/{channel_id}/messages", json={"content": content})

    plan_content = (tool_input.get("plan") or "").strip() or None

    header = f"📋 **Plan from {session.name}** — waiting for approval"
    try:
        if plan_content:
            plan_bytes = plan_content.encode("utf-8")
            await config._discord_request(
                "POST", f"/channels/{channel_id}/messages",
                data={"content": header},
                files={"files[0]": ("plan.txt", plan_bytes)},
            )
        else:
            await _send_plan_msg(
                f"{header}\n\n*(Plan file not found — the agent should have described the plan in its messages above.)*"
            )

        await _send_plan_msg(
            "Reply with **approve** to proceed, **reject** to cancel, or type feedback to revise the plan."
        )
    except Exception:
        log.exception("_handle_exit_plan_mode: failed to post plan to Discord — auto-approving")
        return PermissionResultAllow()

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    session.plan_approval_future = future

    log.info("Agent '%s' paused waiting for plan approval", session.name)

    try:
        result = await future
    finally:
        session.plan_approval_future = None

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
        return PermissionResultDeny(message=message)


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------


def load_schedules() -> list[dict[str, Any]]:
    try:
        with open(config.SCHEDULES_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_schedules(entries: list[dict[str, Any]]) -> None:
    with open(config.SCHEDULES_PATH, "w") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")


def load_history() -> list[dict[str, Any]]:
    try:
        with open(config.HISTORY_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def append_history(entry: dict[str, Any], fired_at: datetime) -> None:
    history = load_history()
    history.append(
        {
            "name": entry["name"],
            "prompt": entry["prompt"],
            "fired_at": fired_at.isoformat(),
        }
    )
    with open(config.HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)
        f.write("\n")


def prune_history() -> None:
    history = load_history()
    cutoff = datetime.now(UTC) - timedelta(days=7)
    pruned = [h for h in history if datetime.fromisoformat(h["fired_at"]) > cutoff]
    if len(pruned) != len(history):
        with open(config.HISTORY_PATH, "w") as f:
            json.dump(pruned, f, indent=2)
            f.write("\n")


def load_skips() -> list[dict[str, Any]]:
    try:
        with open(config.SKIPS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_skips(skips: list[dict[str, Any]]) -> None:
    with open(config.SKIPS_PATH, "w") as f:
        json.dump(skips, f, indent=2)
        f.write("\n")


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


# ---------------------------------------------------------------------------
# Channel topic helpers
# ---------------------------------------------------------------------------


def _format_channel_topic(cwd: str, session_id: str | None = None, prompt_hash: str | None = None) -> str:
    """Format agent metadata for a Discord channel topic."""
    parts = [f"cwd: {cwd}"]
    if session_id:
        parts.append(f"session: {session_id}")
    if prompt_hash:
        parts.append(f"prompt_hash: {prompt_hash}")
    return " | ".join(parts)


def _parse_channel_topic(topic: str | None) -> tuple[str | None, str | None, str | None]:
    """Parse cwd, session_id, and prompt_hash from a channel topic."""
    if not topic:
        return None, None, None
    cwd = None
    session_id = None
    prompt_hash = None
    for part in topic.split("|"):
        key, _, value = part.strip().partition(": ")
        if key == "cwd":
            cwd = value.strip()
        elif key == "session":
            session_id = value.strip()
        elif key == "prompt_hash":
            prompt_hash = value.strip()
    return cwd, session_id, prompt_hash


async def _set_session_id(session: AgentSession, msg_or_sid: ResultMessage | str, channel: TextChannel | None = None) -> None:
    """Update session's session_id and persist it (topic or file)."""
    assert _bot is not None
    sid = msg_or_sid if isinstance(msg_or_sid, str) else getattr(msg_or_sid, "session_id", None)
    if sid and sid != session.session_id:
        session.session_id = sid
        if session.name == config.MASTER_AGENT_NAME:
            try:
                data = {"session_id": sid}
                if session.system_prompt_hash:
                    data["prompt_hash"] = session.system_prompt_hash
                with open(config.MASTER_SESSION_PATH, "w") as f:
                    json.dump(data, f)
                log.info("Saved master session data to %s", config.MASTER_SESSION_PATH)
            except OSError:
                log.warning("Failed to save master session data", exc_info=True)
        elif session.discord_channel_id:
            ch = channel or _bot.get_channel(session.discord_channel_id)
            if ch and isinstance(ch, TextChannel):
                desired_topic = _format_channel_topic(session.cwd, sid, session.system_prompt_hash)
                if ch.topic != desired_topic:
                    log.info("Updating topic on #%s: %r -> %r", ch.name, ch.topic, desired_topic)
                    await ch.edit(topic=desired_topic)
    else:
        session.session_id = sid


# ---------------------------------------------------------------------------
# Model warning
# ---------------------------------------------------------------------------


async def _post_model_warning(session: AgentSession) -> None:
    """Post a warning to Discord if the agent is running on a non-opus model."""
    assert _bot is not None
    model = config._get_model()
    if model == "opus" or not session.discord_channel_id:
        return
    channel = _bot.get_channel(session.discord_channel_id)
    if channel and isinstance(channel, TextChannel):
        try:
            await channel.send(f"⚠️ Running on **{model}** — switch to opus with `/model opus` for best results.")
        except Exception:
            log.warning("Failed to post model warning for '%s'", session.name, exc_info=True)


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
        resp = await config._discord_request("GET", f"/guilds/{guild_id}/channels")
        for ch in resp.json():
            if ch.get("name") == "exceptions" and ch.get("type") == 0:
                _exceptions_channel_id = ch["id"]
                return _exceptions_channel_id
        resp = await config._discord_request(
            "POST",
            f"/guilds/{guild_id}/channels",
            json={"name": "exceptions", "type": 0},
        )
        _exceptions_channel_id = resp.json()["id"]
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
        await config._discord_request(
            "POST",
            f"/channels/{ch_id}/messages",
            json={"content": message[:2000]},
        )
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


def split_message(text: str, limit: int = 2000) -> list[str]:
    """Split text into chunks that fit within Discord's message limit."""
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


async def send_long(channel: TextChannel, text: str) -> None:
    """Send a potentially long message, splitting as needed."""
    chunks = split_message(text.strip())
    caller = "".join(f.name or "?" for f in traceback.extract_stack(limit=4)[:-1])
    for i, chunk in enumerate(chunks):
        if chunk:
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
# Rate limit handling
# ---------------------------------------------------------------------------


def _parse_rate_limit_seconds(text: str) -> int:
    """Parse wait duration from rate limit error text. Returns seconds."""
    text_lower = text.lower()

    match = re.search(r"(?:in|after)\s+(\d+)\s*(seconds?|minutes?|mins?|hours?|hrs?)", text_lower)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("min"):
            return value * 60
        elif unit.startswith(("hour", "hr")):
            return value * 3600
        return value

    match = re.search(r"retry\s+after\s+(\d+)", text_lower)
    if match:
        return int(match.group(1))

    match = re.search(r"(\d+)\s*(?:seconds?|secs?)", text_lower)
    if match:
        return int(match.group(1))

    match = re.search(r"(\d+)\s*(?:minutes?|mins?)", text_lower)
    if match:
        return int(match.group(1)) * 60

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
    if datetime.now(UTC) >= _rate_limited_until:
        _rate_limited_until = None
        return False
    return True


def _rate_limit_remaining_seconds() -> int:
    """Get remaining rate limit time in seconds."""
    if _rate_limited_until is None:
        return 0
    remaining = (_rate_limited_until - datetime.now(UTC)).total_seconds()
    return max(0, int(remaining))


def _record_session_usage(agent_name: str, msg: ResultMessage) -> None:
    sid = msg.session_id
    if not sid:
        return
    now = datetime.now(UTC)
    usage: dict[str, Any] = getattr(msg, "usage", None) or {}
    input_tokens: int = usage.get("input_tokens", 0)
    output_tokens: int = usage.get("output_tokens", 0)

    if sid not in _session_usage:
        _session_usage[sid] = SessionUsage(agent_name=agent_name, first_query=now)
    entry = _session_usage[sid]
    entry.queries += 1
    entry.total_cost_usd += msg.total_cost_usd or 0.0
    entry.total_turns += msg.num_turns or 0
    entry.total_duration_ms += msg.duration_ms or 0
    entry.total_input_tokens += input_tokens
    entry.total_output_tokens += output_tokens
    entry.last_query = now

    try:
        record: dict[str, Any] = {
            "ts": now.isoformat(),
            "agent": agent_name,
            "session_id": sid,
            "cost_usd": msg.total_cost_usd,
            "turns": msg.num_turns,
            "duration_ms": msg.duration_ms,
            "duration_api_ms": msg.duration_api_ms,
            "is_error": msg.is_error,
            "usage": usage or None,
        }
        with open(config.USAGE_HISTORY_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        log.warning("Failed to write usage history", exc_info=True)


def _update_rate_limit_quota(data: dict[str, Any]) -> None:
    info = data.get("rate_limit_info", {})
    resets_at_unix = info.get("resetsAt")
    if resets_at_unix is None:
        return
    rl_type = info.get("rateLimitType", "unknown")
    new_status = info.get("status", "unknown")
    new_resets_at = datetime.fromtimestamp(resets_at_unix, tz=UTC)
    new_utilization = info.get("utilization")

    existing = _rate_limit_quotas.get(rl_type)
    if existing is not None and new_utilization is None and existing.resets_at == new_resets_at:
        new_utilization = existing.utilization

    _rate_limit_quotas[rl_type] = RateLimitQuota(
        status=new_status,
        resets_at=new_resets_at,
        rate_limit_type=rl_type,
        utilization=new_utilization,
    )

    try:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "type": rl_type,
            "status": new_status,
            "utilization": new_utilization,
        }
        with open(config.RATE_LIMIT_HISTORY_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        log.warning("Failed to write rate limit history", exc_info=True)


async def _handle_rate_limit(error_text: str, session: AgentSession, channel: TextChannel) -> None:
    """Handle a rate limit error: set global state, notify all agent channels."""
    global _rate_limited_until
    assert _bot is not None

    wait_seconds = _parse_rate_limit_seconds(error_text)
    new_limit = datetime.now(UTC) + timedelta(seconds=wait_seconds)
    already_limited = _is_rate_limited()

    if _rate_limited_until is None or new_limit > _rate_limited_until:
        _rate_limited_until = new_limit

    log.warning("Rate limited — waiting %ds (until %s)", wait_seconds, _rate_limited_until.isoformat())

    if not already_limited:
        remaining = _format_time_remaining(wait_seconds)
        reset_time = _rate_limited_until.strftime("%H:%M:%S UTC")

        quota_lines = ""
        if _rate_limit_quotas:
            rl_parts: list[str] = []
            for rl_type, quota in _rate_limit_quotas.items():
                pct = f"{quota.utilization:.0%}" if quota.utilization is not None else "?"
                rl_parts.append(f"{rl_type}: {pct}")
            quota_lines = "\nUtilization: " + " · ".join(rl_parts)

        msg_text = f"⚠️ **Rate limited by Claude API.** Resets in ~**{remaining}** (at {reset_time}).{quota_lines}"

        notified_channels: set[int] = set()
        for agent_session in agents.values():
            if not agent_session.discord_channel_id:
                continue
            ch = _bot.get_channel(agent_session.discord_channel_id)
            if isinstance(ch, TextChannel) and ch.id not in notified_channels:
                notified_channels.add(ch.id)
                try:
                    await send_system(ch, msg_text)
                except Exception:
                    log.warning("Failed to notify channel %s about rate limit", ch.id)

        asyncio.create_task(_notify_rate_limit_expired(wait_seconds))


async def _notify_rate_limit_expired(delay: float) -> None:
    """Sleep until rate limit expires, then notify master channel."""
    try:
        await asyncio.sleep(delay)
        if not _is_rate_limited():
            ch = await get_master_channel()
            if ch:
                await send_system(ch, "✅ Rate limit expired — usage available again.")
    except asyncio.CancelledError:
        return
    except Exception:
        log.warning("Failed to send rate limit expiry notification", exc_info=True)


# ---------------------------------------------------------------------------
# MCP server injection (set by bot.py after tools.py creates them)
# ---------------------------------------------------------------------------

_utils_mcp_server: Any = None


def set_utils_mcp_server(server: Any) -> None:
    """Set the utils MCP server reference. Called from bot.py after tools.py init."""
    global _utils_mcp_server
    _utils_mcp_server = server


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def _get_subprocess_pid(client: ClaudeSDKClient) -> int | None:
    """Extract the PID of the underlying CLI subprocess from a ClaudeSDKClient."""
    try:
        transport = getattr(client, "_transport", None) or getattr(getattr(client, "_query", None), "transport", None)
        if transport is None:
            return None
        proc = getattr(transport, "_process", None)
        if proc is None:
            return None
        return proc.pid
    except Exception:
        return None


def _ensure_process_dead(pid: int | None, label: str) -> None:
    """Send SIGTERM to *pid* if it is still alive."""
    if pid is None:
        return
    try:
        os.kill(pid, 0)
    except OSError:
        return
    log.warning("Subprocess %d for '%s' survived disconnect — sending SIGTERM (SDK bug workaround)", pid, label)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


async def _create_transport(session: AgentSession, reconnecting: bool = False):
    """Create a transport for Claude Code agent (bridge or direct)."""
    if bridge_conn and bridge_conn.is_alive:
        transport = BridgeTransport(
            session.name,
            bridge_conn,
            reconnecting=reconnecting,
            stderr_callback=make_stderr_callback(session),
        )
        await transport.connect()
        return transport
    else:
        return None


async def _disconnect_client(client: ClaudeSDKClient, label: str) -> None:
    """Disconnect a ClaudeSDKClient and ensure its subprocess is terminated."""
    transport = getattr(client, "_transport", None)
    if isinstance(transport, BridgeTransport):
        try:
            await asyncio.wait_for(transport.close(), timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            log.warning("'%s' bridge transport close timed out", label)
        except Exception:
            log.exception("'%s' error closing bridge transport", label)
        return

    pid = _get_subprocess_pid(client)
    try:
        await asyncio.wait_for(client.__aexit__(None, None, None), timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        log.warning("'%s' shutdown timed out or was cancelled", label)
    except RuntimeError as e:
        if "cancel scope" in str(e):
            log.debug("'%s' cross-task cleanup (expected): %s", label, e)
        else:
            raise
    _ensure_process_dead(pid, label)


def _count_awake_agents() -> int:
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
        if s._bridge_busy:
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
    while _count_awake_agents() >= config.MAX_AWAKE_AGENTS:
        log.debug(
            "Awake slots full (%d/%d), attempting eviction for '%s'",
            _count_awake_agents(),
            config.MAX_AWAKE_AGENTS,
            requesting_agent,
        )
        evicted = await _evict_idle_agent(exclude=requesting_agent)
        if not evicted:
            log.warning("Cannot free awake slot for '%s' — all %d slots busy", requesting_agent, config.MAX_AWAKE_AGENTS)
            return False
    return True


async def sleep_agent(session: AgentSession, *, force: bool = False) -> None:
    """Shut down an agent. Keep the AgentSession in the agents dict.

    If force=False (default), skips sleeping if the agent's query_lock is held
    (i.e. the agent is actively processing a query).
    """
    if not force and session.query_lock.locked():
        log.debug("Skipping sleep for '%s' — query_lock is held", session.name)
        return

    if session.flowcoder_process:
        proc = session.flowcoder_process
        if isinstance(proc, BridgeFlowcoderProcess) and session.query_lock.locked():
            await proc.detach()
        else:
            await proc.stop()
        session.flowcoder_process = None

    if session.agent_type == "flowcoder":
        return

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
        model=config._get_model(),
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
            _count_awake_agents(),
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
            if session._log:
                session._log.info("SESSION_WAKE (resumed=%s)", bool(resume_id))
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
            if session._log:
                session._log.info("SESSION_WAKE (resumed=False, fresh after resume failure)")

        prompt_changed = False
        if resume_id and session.system_prompt is not None:
            current_hash = _compute_prompt_hash(session.system_prompt)
            if session.system_prompt_hash is not None and current_hash != session.system_prompt_hash:
                prompt_changed = True
                log.info(
                    "System prompt changed for '%s' (old=%s, new=%s)",
                    session.name, session.system_prompt_hash, current_hash,
                )
            session.system_prompt_hash = current_hash

        if not session._system_prompt_posted and session.discord_channel_id:
            session._system_prompt_posted = True
            channel = _bot.get_channel(session.discord_channel_id)
            if channel and isinstance(channel, TextChannel):
                try:
                    await _post_system_prompt_to_channel(
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
    channel: discord.TextChannel,
    orig_message: discord.Message | None,
) -> bool:
    """Try to wake agent, return True if successful, False if queued."""
    try:
        await wake_agent(session)
        return True
    except ConcurrencyLimitError:
        session.message_queue.append((content, channel, orig_message))
        position = len(session.message_queue)
        awake = _count_awake_agents()
        log.debug("Concurrency limit hit for '%s', queuing message (position %d)", session.name, position)
        await _add_reaction(orig_message, "📨")
        await send_system(channel, f"⏳ All {awake} agent slots busy. Message queued (position {position}).")
        return False
    except Exception:
        log.exception("Failed to wake agent '%s'", session.name)
        await _add_reaction(orig_message, "❌")
        await send_system(
            channel, f"Failed to wake agent **{session.name}**. Try `/kill-agent {session.name}` and respawn."
        )
        return False


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


async def reset_session(name: str, cwd: str | None = None) -> AgentSession:
    """Reset a named session. Preserves system prompt, channel mapping, and MCP servers."""
    session = agents.get(name)
    old_cwd = session.cwd if session else config.DEFAULT_CWD
    old_channel_id = session.discord_channel_id if session else None
    old_mcp = getattr(session, "mcp_servers", None)
    resolved_cwd = cwd or old_cwd
    prompt = session.system_prompt if session and session.system_prompt else _make_spawned_agent_system_prompt(resolved_cwd)
    await end_session(name)
    new_session = AgentSession(
        name=name,
        cwd=resolved_cwd,
        system_prompt=prompt,
        system_prompt_hash=_compute_prompt_hash(prompt),
        client=None,
        session_id=None,
        discord_channel_id=old_channel_id,
        mcp_servers=old_mcp,
    )
    agents[name] = new_session
    log.info("Session '%s' reset (sleeping, cwd=%s)", name, new_session.cwd)
    return new_session


def get_master_session() -> AgentSession | None:
    """Get the axi-master session."""
    return agents.get(config.MASTER_AGENT_NAME)


# ---------------------------------------------------------------------------
# Guild channel management
# ---------------------------------------------------------------------------


def _normalize_channel_name(name: str) -> str:
    """Normalize an agent name to a valid Discord channel name."""
    name = name.lower().replace(" ", "-")
    name = re.sub(r"[^a-z0-9\-_]", "", name)
    return name[:100]


def _build_category_overwrites(
    guild: discord.Guild,
) -> dict[discord.Object | discord.Member | discord.Role, discord.PermissionOverwrite]:
    """Build permission overwrites for Axi categories."""
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
    for uid in config.ALLOWED_USER_IDS:
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
    assert _bot is not None

    guild = _bot.get_guild(config.DISCORD_GUILD_ID)
    if guild is None:
        guild = await _bot.fetch_guild(config.DISCORD_GUILD_ID)
    target_guild = guild

    overwrites = _build_category_overwrites(guild)

    active_cat = None
    killed_cat = None
    for cat in guild.categories:
        if cat.name == config.ACTIVE_CATEGORY_NAME:
            active_cat = cat
        elif cat.name == config.KILLED_CATEGORY_NAME:
            killed_cat = cat

    def _overwrites_match(
        existing: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite],
        desired: dict[discord.Object | discord.Member | discord.Role, discord.PermissionOverwrite],
    ) -> bool:
        a = {getattr(k, "id", k): v for k, v in existing.items()}
        b = {getattr(k, "id", k): v for k, v in desired.items()}
        return a == b

    for name, cat in [
        (config.ACTIVE_CATEGORY_NAME, active_cat),
        (config.KILLED_CATEGORY_NAME, killed_cat),
    ]:
        if cat is None:
            cat = await guild.create_category(name, overwrites=overwrites)
            log.info("Created '%s' category", name)
        elif not _overwrites_match(cat.overwrites, overwrites):
            await cat.edit(overwrites=overwrites)
            log.info("Synced permissions on '%s' category", name)
        else:
            log.info("Permissions already current on '%s' category", name)
        if name == config.ACTIVE_CATEGORY_NAME:
            active_cat = cat
        else:
            killed_cat = cat
    active_category = active_cat
    killed_category = killed_cat

    assert active_cat is not None
    assert killed_cat is not None
    return guild, active_cat, killed_cat


async def reconstruct_agents_from_channels() -> int:
    """Reconstruct sleeping AgentSession entries from existing Discord channels."""
    reconstructed = 0
    if not active_category:
        return reconstructed

    for cat in [active_category]:
        for ch in cat.text_channels:
            agent_name = ch.name

            if agent_name == _normalize_channel_name(config.MASTER_AGENT_NAME):
                channel_to_agent[ch.id] = config.MASTER_AGENT_NAME
                continue

            if agent_name in agents:
                channel_to_agent[ch.id] = agent_name
                continue

            cwd, session_id, old_prompt_hash = _parse_channel_topic(ch.topic)
            if cwd is None:
                log.debug("No cwd in topic for channel #%s, skipping", agent_name)
                continue

            prompt = _make_spawned_agent_system_prompt(cwd)
            mcp_servers: dict[str, Any] = {}
            if _utils_mcp_server is not None:
                mcp_servers["utils"] = _utils_mcp_server
            mcp_servers["schedule"] = make_schedule_mcp_server(agent_name, config.SCHEDULES_PATH)

            session = AgentSession(
                name=agent_name,
                client=None,
                cwd=cwd,
                system_prompt=prompt,
                system_prompt_hash=old_prompt_hash,
                session_id=session_id,
                discord_channel_id=ch.id,
                mcp_servers=mcp_servers,
            )
            agents[agent_name] = session
            channel_to_agent[ch.id] = agent_name
            reconstructed += 1
            log.info(
                "Reconstructed agent '%s' from #%s (category=%s, session_id=%s, prompt_hash=%s)",
                agent_name,
                ch.name,
                cat.name,
                session_id,
                old_prompt_hash,
            )

    log.info("Reconstructed %d agent(s) from channels", reconstructed)
    return reconstructed


async def ensure_agent_channel(agent_name: str) -> TextChannel:
    """Find or create a text channel for an agent. Moves from Killed to Active if needed."""
    normalized = _normalize_channel_name(agent_name)

    if active_category:
        for ch in active_category.text_channels:
            if ch.name == normalized:
                channel_to_agent[ch.id] = agent_name
                return ch

    if killed_category:
        for ch in killed_category.text_channels:
            if ch.name == normalized:
                try:
                    await ch.move(category=active_category, beginning=True, sync_permissions=True)
                except discord.HTTPException as e:
                    log.warning("Failed to move channel #%s from Killed to Active: %s", normalized, e)
                    await send_to_exceptions(f"Failed to move #**{normalized}** from Killed → Active: `{e}`")
                channel_to_agent[ch.id] = agent_name
                log.info("Moved channel #%s from Killed to Active", normalized)
                return ch

    already_guarded = normalized in _bot_creating_channels
    _bot_creating_channels.add(normalized)
    try:
        assert target_guild is not None
        channel = await target_guild.create_text_channel(normalized, category=active_category)
    except discord.HTTPException as e:
        log.warning("Failed to create channel #%s: %s", normalized, e)
        await send_to_exceptions(f"Failed to create channel #**{normalized}**: `{e}`")
        raise
    finally:
        if not already_guarded:
            _bot_creating_channels.discard(normalized)
    channel_to_agent[channel.id] = agent_name
    log.info("Created channel #%s in Active category", normalized)
    return channel


async def move_channel_to_killed(agent_name: str) -> None:
    """Move an agent's channel from Active to Killed category."""
    if agent_name == config.MASTER_AGENT_NAME:
        return

    normalized = _normalize_channel_name(agent_name)
    if active_category:
        for ch in active_category.text_channels:
            if ch.name == normalized:
                try:
                    await ch.move(category=killed_category, end=True, sync_permissions=True)
                    log.info("Moved channel #%s to Killed category", normalized)
                except discord.HTTPException as e:
                    log.warning("Failed to move channel #%s to Killed: %s", normalized, e)
                    await send_to_exceptions(f"Failed to move #**{normalized}** to Killed category: `{e}`")
                break


async def get_agent_channel(agent_name: str) -> TextChannel | None:
    """Get the Discord channel for an agent, if it exists."""
    assert _bot is not None
    session = agents.get(agent_name)
    if session and session.discord_channel_id:
        ch = _bot.get_channel(session.discord_channel_id)
        if isinstance(ch, TextChannel):
            return ch
    normalized = _normalize_channel_name(agent_name)
    if active_category:
        for ch in active_category.text_channels:
            if ch.name == normalized:
                return ch
    return None


async def get_master_channel() -> TextChannel | None:
    """Get the axi-master channel."""
    return await get_agent_channel(config.MASTER_AGENT_NAME)


# ---------------------------------------------------------------------------
# Streaming response
# ---------------------------------------------------------------------------


async def _receive_response_safe(session: AgentSession):
    """Wrapper around receive_messages() that handles unknown message types."""
    assert session.client is not None
    assert session.client._query is not None
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
                log.warning(
                    "Unknown SDK message type from '%s': type=%s data=%s",
                    session.name,
                    msg_type,
                    json.dumps(data)[:500],
                )
                if session._log:
                    session._log.warning("UNKNOWN_MSG: type=%s data=%s", msg_type, json.dumps(data)[:500])
                preview = json.dumps(data)[:400]
                await send_to_exceptions(
                    f"⚠️ Unknown SDK message type `{msg_type}` from **{session.name}**:\n```json\n{preview}\n```"
                )
            continue
        yield parsed
        if isinstance(parsed, ResultMessage):
            return


def _update_activity(session: AgentSession, event: dict[str, Any]) -> None:
    """Update the agent's activity state from a raw Anthropic stream event."""
    activity = session.activity
    activity.last_event = datetime.now(UTC)
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
            activity.thinking_text = ""
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
            activity.thinking_text += delta.get("thinking", "")
        elif delta_type == "text_delta":
            activity.phase = "writing"
            activity.text_chars += len(delta.get("text", ""))
        elif delta_type == "input_json_delta":
            if len(activity.tool_input_preview) < 200:
                activity.tool_input_preview += delta.get("partial_json", "")
                activity.tool_input_preview = activity.tool_input_preview[:200]

    elif event_type == "content_block_stop":
        if activity.phase == "tool_use":
            activity.phase = "waiting"

    elif event_type == "message_start":
        activity.turn_count += 1

    elif event_type == "message_delta":
        stop_reason = event.get("delta", {}).get("stop_reason")
        if stop_reason == "end_turn":
            activity.phase = "idle"
            activity.tool_name = None
        elif stop_reason == "tool_use":
            activity.phase = "waiting"


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


async def _query_agent(
    session: AgentSession, content: MessageContent, channel: TextChannel, *, show_awaiting_input: bool = True
) -> None:
    """Send a message to an agent and stream the response."""
    assert session.client is not None
    await session.client.query(_as_stream(content))
    await stream_response_to_channel(session, channel, show_awaiting_input=show_awaiting_input)


async def stream_response_to_channel(session: AgentSession, channel: TextChannel, show_awaiting_input: bool = True) -> str | None:
    """Stream Claude's response from an agent session to a Discord channel.

    Returns None on success, or an error string for transient errors (for retry).
    """
    stream_id = _next_stream_id(session.name)
    log.info(
        "STREAM_START[%s] caller=%s",
        stream_id,
        "".join(f.name or "?" for f in traceback.extract_stack(limit=4)[:-1]),
    )

    text_buffer = ""
    hit_rate_limit = False
    hit_transient_error: str | None = None
    typing_stopped = False

    _flush_count = 0
    _msg_total = 0

    async def flush_text(text: str, reason: str = "?") -> None:
        nonlocal _flush_count
        if not text.strip():
            return
        _flush_count += 1
        log.info(
            "FLUSH[%s] #%d reason=%s len=%d text=%r",
            session.name,
            _flush_count,
            reason,
            len(text.strip()),
            text.strip()[:120],
        )
        await send_long(channel, text.lstrip())

    def stop_typing() -> None:
        nonlocal typing_stopped
        if not typing_stopped and _typing_ctx and _typing_ctx.task:
            _typing_ctx.task.cancel()
            typing_stopped = True

    async with channel.typing() as _typing_ctx:
        async for msg in _receive_response_safe(session):
            _msg_total += 1
            if session._log:
                session._log.debug(
                    "MSG_SEQ[%s][%d] type=%s buf_len=%d", stream_id, _msg_total, type(msg).__name__, len(text_buffer)
                )

            for stderr_msg in drain_stderr(session):
                stderr_text = stderr_msg.strip()
                if stderr_text:
                    for part in split_message(f"```\n{stderr_text}\n```"):
                        await channel.send(part)

            if isinstance(msg, StreamEvent):
                event = msg.event
                event_type = event.get("type", "")

                if msg.session_id and msg.session_id != session.session_id:
                    await _set_session_id(session, msg.session_id, channel=channel)

                _update_activity(session, event)

                # Debug output
                if session.debug:
                    if event_type == "content_block_stop":
                        if session.activity.phase == "thinking" and session.activity.thinking_text:
                            thinking = session.activity.thinking_text.strip()
                            if thinking:
                                file = discord.File(
                                    io.BytesIO(thinking.encode("utf-8")),
                                    filename="thinking.md",
                                )
                                await channel.send("💭", file=file)
                                session.activity.thinking_text = ""
                        elif session.activity.phase == "waiting" and session.activity.tool_name:
                            tool = session.activity.tool_name
                            preview = _extract_tool_preview(tool, session.activity.tool_input_preview)
                            if preview:
                                await channel.send(f"`🔧 {tool}: {preview[:120]}`")
                            else:
                                await channel.send(f"`🔧 {tool}`")

                # Log stream events
                if session._log:
                    if event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        delta_type = delta.get("type", "")
                        if delta_type not in ("text_delta", "thinking_delta", "signature_delta"):
                            session._log.debug("STREAM: %s delta=%s", event_type, delta_type)
                    elif event_type in ("content_block_start", "content_block_stop"):
                        block = event.get("content_block", {})
                        session._log.debug(
                            "STREAM: %s type=%s index=%s", event_type, block.get("type", "?"), event.get("index")
                        )
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

                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_buffer += delta.get("text", "")

                elif event_type == "message_delta":
                    stop_reason = event.get("delta", {}).get("stop_reason")
                    if stop_reason == "end_turn":
                        await flush_text(text_buffer, "end_turn")
                        text_buffer = ""
                        stop_typing()

            elif isinstance(msg, AssistantMessage):
                if msg.error in ("rate_limit", "billing_error"):
                    error_text = text_buffer
                    for block in msg.content or []:
                        if hasattr(block, "text"):
                            error_text += " " + cast("str", getattr(block, "text", ""))
                    log.warning("Agent '%s' hit %s error: %s", session.name, msg.error, error_text[:200])
                    stop_typing()
                    await _handle_rate_limit(error_text, session, channel)
                    text_buffer = ""
                    hit_rate_limit = True
                elif msg.error:
                    error_text = text_buffer
                    for block in msg.content or []:
                        if hasattr(block, "text"):
                            error_text += " " + cast("str", getattr(block, "text", ""))
                    log.warning("Agent '%s' hit API error (%s): %s", session.name, msg.error, error_text[:200])
                    stop_typing()
                    await flush_text(text_buffer, "assistant_error")
                    text_buffer = ""
                    hit_transient_error = msg.error
                else:
                    await flush_text(text_buffer, "assistant_msg")
                    text_buffer = ""
                    stop_typing()

                if session._log:
                    for block in msg.content or []:
                        block_any: Any = block
                        if hasattr(block, "text"):
                            session._log.info("ASSISTANT: %s", block_any.text[:2000])
                        elif hasattr(block, "type") and block_any.type == "tool_use":
                            session._log.info(
                                "TOOL_USE: %s(%s)",
                                block_any.name,
                                json.dumps(block_any.input)[:500] if hasattr(block, "input") else "",
                            )

            elif isinstance(msg, ResultMessage):
                stop_typing()
                await _set_session_id(session, msg, channel=channel)
                if not hit_rate_limit:
                    await flush_text(text_buffer, "result_msg")
                text_buffer = ""
                if session._log:
                    session._log.info(
                        "RESULT: cost=$%s turns=%d duration=%dms session=%s",
                        msg.total_cost_usd,
                        msg.num_turns,
                        msg.duration_ms,
                        msg.session_id,
                    )
                _record_session_usage(session.name, msg)

            elif isinstance(msg, SystemMessage):
                if session._log:
                    session._log.debug("SYSTEM_MSG: subtype=%s data=%s", msg.subtype, json.dumps(msg.data)[:500])
                if msg.subtype == "compact_boundary":
                    metadata = msg.data.get("compact_metadata", {})
                    trigger = metadata.get("trigger", "unknown")
                    pre_tokens = metadata.get("pre_tokens")
                    log.info(
                        "Agent '%s' context compacted: trigger=%s pre_tokens=%s", session.name, trigger, pre_tokens
                    )
                    token_info = f" ({pre_tokens:,} tokens)" if pre_tokens else ""
                    await channel.send(f"🔄 Context compacted{token_info}")

            else:
                if session._log:
                    session._log.debug("OTHER_MSG: %s", type(msg).__name__)

            # Mid-turn flush
            if not hit_rate_limit and len(text_buffer) >= 1800:
                split_at = text_buffer.rfind("\n", 0, 1800)
                if split_at == -1:
                    split_at = 1800
                to_send = text_buffer[:split_at]
                text_buffer = text_buffer[split_at:].lstrip("\n")
                await flush_text(to_send, "mid_turn_split")

    # Flush remaining stderr
    for stderr_msg in drain_stderr(session):
        stderr_text = stderr_msg.strip()
        if stderr_text:
            for part in split_message(f"```\n{stderr_text}\n```"):
                await channel.send(part)

    if hit_rate_limit:
        log.info("STREAM_END[%s] result=rate_limit msgs=%d flushes=%d", stream_id, _msg_total, _flush_count)
        return None

    if hit_transient_error:
        log.info(
            "STREAM_END[%s] result=transient_error(%s) msgs=%d flushes=%d",
            stream_id,
            hit_transient_error,
            _msg_total,
            _flush_count,
        )
        return hit_transient_error

    await flush_text(text_buffer, "post_loop")
    log.info("STREAM_END[%s] result=ok msgs=%d flushes=%d", stream_id, _msg_total, _flush_count)

    if config.SHOW_AWAITING_INPUT:
        mentions = " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)
        await send_system(channel, f"Bot has finished responding and is awaiting input. {mentions}")

    return None


# ---------------------------------------------------------------------------
# Retry / timeout
# ---------------------------------------------------------------------------


async def _stream_with_retry(session: AgentSession, channel: TextChannel) -> bool:
    """Stream response with retry on transient API errors. Returns True on success."""
    log.info("RETRY_ENTER[%s] starting initial stream", session.name)
    error = await stream_response_to_channel(session, channel)
    if error is None:
        log.info("RETRY_EXIT[%s] first attempt succeeded", session.name)
        return True

    log.warning("RETRY_TRIGGERED[%s] error=%s — will retry", session.name, error)
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
            f"⚠️ API error, retrying in {delay}s... (attempt {attempt}/{config.API_ERROR_MAX_RETRIES})"
        )
        await asyncio.sleep(delay)

        try:
            assert session.client is not None
            await session.client.query(_as_stream("Continue from where you left off."))
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
    await channel.send(f"❌ API error persisted after {config.API_ERROR_MAX_RETRIES} retries. Try again later.")
    return False


async def _handle_query_timeout(session: AgentSession, channel: TextChannel) -> None:
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
    old_name = session.name
    old_cwd = session.cwd
    old_prompt = session.system_prompt or _make_spawned_agent_system_prompt(session.cwd)
    old_prompt_hash = session.system_prompt_hash or _compute_prompt_hash(old_prompt)
    old_channel_id = session.discord_channel_id
    old_mcp = session.mcp_servers
    await end_session(old_name)

    new_session = AgentSession(
        name=old_name,
        cwd=old_cwd,
        system_prompt=old_prompt,
        system_prompt_hash=old_prompt_hash,
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


# ---------------------------------------------------------------------------
# process_message — unified handler replacement
# ---------------------------------------------------------------------------


async def process_message(session: AgentSession, content: MessageContent, channel: TextChannel) -> None:
    """Process a user message through the appropriate agent type.

    Replaces the former AgentHandler.process_message() delegation.
    """
    if session.agent_type == "flowcoder":
        if session.flowcoder_process is None:
            raise RuntimeError(f"Flowcoder '{session.name}' not running")
        if isinstance(content, str):
            text = content
        else:
            # Extract text blocks only — flowcoder doesn't support images/attachments
            text_parts = [b["text"] for b in content if b.get("type") == "text"]
            if not text_parts:
                await send_system(channel, "Flowcoder agents don't support image-only messages.")
                return
            text = "\n".join(text_parts)
        user_msg = {
            "type": "user",
            "message": {"role": "user", "content": text},
        }
        await session.flowcoder_process.send(user_msg)
        log.debug("Sent message to flowcoder '%s'", session.name)
        return

    # Claude Code agent
    if session.client is None:
        raise RuntimeError(f"Claude Code agent '{session.name}' not awake")

    _reset_session_activity(session)
    session._bridge_busy = False
    drain_stderr(session)
    drained = drain_sdk_buffer(session)

    if session._log:
        session._log.info("USER: %s", _content_summary(content))
    log.info("PROCESS[%s] drained=%d, calling query+stream", session.name, drained)
    try:
        async with asyncio.timeout(config.QUERY_TIMEOUT):
            await session.client.query(_as_stream(content))
            await _stream_with_retry(session, channel)
    except TimeoutError:
        await _handle_query_timeout(session, channel)
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
    log.info("Reclaiming agent name '%s' — terminating existing session", name)
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
    agent_type: str = "claude_code",
    command: str = "",
    command_args: str = "",
    packs: list[str] | None = None,
) -> None:
    """Spawn a new agent session and run its initial prompt in the background."""
    if not os.path.isdir(cwd):
        os.makedirs(cwd, exist_ok=True)
        log.info("Auto-created working directory: %s", cwd)

    normalized = _normalize_channel_name(name)
    _bot_creating_channels.add(normalized)
    channel = await ensure_agent_channel(name)

    if agent_type == "flowcoder":
        await send_system(channel, f"Spawning flowcoder agent **{name}** — command `{command}` in `{cwd}`...")
    elif resume:
        await send_system(channel, f"Resuming agent **{name}** (session `{resume[:8]}…`) in `{cwd}`...")
    else:
        await send_system(channel, f"Spawning agent **{name}** in `{cwd}`...")

    if agent_type == "flowcoder":
        session = AgentSession(
            name=name,
            agent_type="flowcoder",
            cwd=cwd,
            session_id=None,
            discord_channel_id=channel.id,
            mcp_servers=None,
            flowcoder_command=command,
            flowcoder_args=command_args,
        )
    else:
        prompt = _make_spawned_agent_system_prompt(cwd, packs=packs)
        mcp_servers: dict[str, Any] = {}
        if _utils_mcp_server is not None:
            mcp_servers["utils"] = _utils_mcp_server
        mcp_servers["schedule"] = make_schedule_mcp_server(name, config.SCHEDULES_PATH)

        session = AgentSession(
            name=name,
            cwd=cwd,
            system_prompt=prompt,
            system_prompt_hash=_compute_prompt_hash(prompt),
            client=None,
            session_id=resume,
            discord_channel_id=channel.id,
            mcp_servers=mcp_servers,
        )

    agents[name] = session
    channel_to_agent[channel.id] = name
    _bot_creating_channels.discard(normalized)
    log.info("Agent '%s' registered (type=%s, cwd=%s, resume=%s)", name, agent_type, cwd, resume)

    desired_topic = _format_channel_topic(cwd, resume, session.system_prompt_hash)
    if channel.topic != desired_topic:
        log.info("Updating topic on #%s: %r -> %r", channel.name, channel.topic, desired_topic)
        await channel.edit(topic=desired_topic)

    if agent_type == "flowcoder":
        asyncio.create_task(_run_flowcoder(session, channel))
        return

    if not initial_prompt:
        await send_system(channel, f"Agent **{name}** is ready (sleeping).")
        return

    asyncio.create_task(_run_initial_prompt(session, initial_prompt, channel))


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

    asyncio.create_task(_run_initial_prompt(session, prompt, channel))


# ---------------------------------------------------------------------------
# Flowcoder
# ---------------------------------------------------------------------------


async def _run_and_stream_flowcoder(
    session: AgentSession, channel: TextChannel, command: str, args: str, label: str
) -> None:
    """Shared: create flowcoder process, stream output, handle cancellation/cleanup."""
    if bridge_conn and bridge_conn.is_alive:
        proc = BridgeFlowcoderProcess(
            bridge_name=f"{session.name}:flowcoder",
            conn=bridge_conn,
            command=command,
            args=args,
            cwd=session.cwd,
        )
    else:
        proc = FlowcoderProcess(command=command, args=args, cwd=session.cwd)
    await proc.start()
    session.flowcoder_process = proc

    try:
        await _stream_flowcoder_to_channel(session, channel)
    except asyncio.CancelledError:
        if isinstance(proc, BridgeFlowcoderProcess):
            await proc.detach()
            session.flowcoder_process = None
            session.activity = ActivityState(phase="idle")
            log.info("%s '%s' detached on cancel (bridge will buffer)", label, session.name)
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


async def _run_flowcoder(session: AgentSession, channel: TextChannel) -> None:
    """Run a flowcoder agent to completion, streaming output to Discord."""
    log.info("Starting flowcoder agent '%s' (channel=%s)", session.name, channel.id)
    try:
        async with session.query_lock:
            session.last_activity = datetime.now(UTC)
            session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))

            cmd_display = session.flowcoder_command
            if session.flowcoder_args:
                cmd_display += f" {session.flowcoder_args}"
            await channel.send(f"*System:* Running flowcoder command: `{cmd_display}`")
            await _run_and_stream_flowcoder(
                session, channel, session.flowcoder_command, session.flowcoder_args, "Flowcoder"
            )

        log.info("Flowcoder agent '%s' finished", session.name)

    except asyncio.CancelledError:
        log.debug("Flowcoder agent '%s' task cancelled", session.name)
    except Exception:
        log.exception("Error running flowcoder agent '%s'", session.name)
        await send_system(channel, f"Flowcoder agent **{session.name}** encountered an error.")


async def _run_inline_flowchart(session: AgentSession, channel: TextChannel, command: str, args: str) -> None:
    """Run a flowchart command inline in an agent's channel."""
    async with session.query_lock:
        session.last_activity = datetime.now(UTC)
        session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))

        cmd_display = command + (f" {args}" if args else "")
        await send_system(channel, f"Running flowchart: `{cmd_display}`")
        await _run_and_stream_flowcoder(session, channel, command, args, f"Flowchart `{command}`")

    await _process_message_queue(session)


async def _stream_flowcoder_to_channel(session: AgentSession, channel: TextChannel) -> None:
    """Read flowcoder messages and translate to Discord output."""
    proc = session.flowcoder_process
    assert proc is not None

    text_buffer: list[str] = []
    current_block = ""
    block_count = 0
    start_time = time.monotonic()

    _SILENT_BLOCK_TYPES = {"start", "end", "variable"}

    async def _flush_buffer() -> None:
        if text_buffer:
            text = "".join(text_buffer)
            text_buffer.clear()
            if text.strip():
                await send_long(channel, text)

    async for msg in proc.messages():
        session.last_activity = datetime.now(UTC)
        msg_type = msg.get("type", "")
        msg_subtype = msg.get("subtype", "")

        if msg_type == "system" and msg_subtype == "block_start":
            await _flush_buffer()
            data = msg.get("data", {})
            block_count += 1
            block_name = data.get("block_name", "?")
            block_type = data.get("block_type", "?")
            current_block = block_name
            session.activity = ActivityState(
                phase="tool_use",
                tool_name=f"flowcoder:{block_type}",
                query_started=session.activity.query_started,
            )
            if block_type not in _SILENT_BLOCK_TYPES:
                await channel.send(f"▶ **{block_name}** (`{block_type}`)")

        elif msg_type == "system" and msg_subtype == "block_complete":
            await _flush_buffer()
            data = msg.get("data", {})
            success = data.get("success", True)
            block_name = data.get("block_name", current_block)
            if not success:
                await channel.send(f"> {block_name} **FAILED**")

        elif msg_type == "system" and msg_subtype == "session_message":
            data = msg.get("data", {})
            inner = data.get("message", {})
            inner_type = inner.get("type", "")

            if inner_type == "assistant":
                if text_buffer:
                    await _flush_buffer()
                else:
                    message: dict[str, Any] = inner.get("message", {})
                    raw_content: Any = message.get("content", [])
                    parts: list[str] = []
                    if isinstance(raw_content, str):
                        parts.append(raw_content)
                    elif isinstance(raw_content, list):
                        content_blocks = cast("list[ContentBlock]", raw_content)
                        parts.extend(
                            str(blk.get("text", ""))
                            for blk in content_blocks
                            if blk.get("type") == "text"
                        )
                    fallback_text = "\n".join(parts)
                    if fallback_text.strip():
                        await send_long(channel, fallback_text)

            elif inner_type == "stream_event":
                event = inner.get("event", {})
                event_type = event.get("type", "")
                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_buffer.append(delta.get("text", ""))
                        buf_len = sum(len(t) for t in text_buffer)
                        if buf_len >= 1800:
                            await _flush_buffer()

        elif msg_type == "result":
            await _flush_buffer()
            elapsed = time.monotonic() - start_time
            is_error = msg.get("is_error", False)
            status = "**failed**" if is_error else "**completed**"
            cost = msg.get("total_cost_usd", 0)

            summary = f"Flowchart {status} in {elapsed:.0f}s | Cost: ${cost:.4f} | Blocks: {block_count}"
            await send_system(channel, summary)
            break

        elif msg_type == "status_response":
            if not msg.get("busy", False):
                break

        elif msg_type == "control_request":
            request = msg.get("request", msg)
            request_id = request.get("request_id", "")
            response = {
                "type": "control_response",
                "response": {
                    "request_id": request_id,
                    "allowed": True,
                },
            }
            await proc.send(response)

    await _flush_buffer()


# ---------------------------------------------------------------------------
# Initial prompt / message queue
# ---------------------------------------------------------------------------


async def _run_initial_prompt(session: AgentSession, prompt: MessageContent, channel: TextChannel) -> None:
    """Run the initial prompt for a spawned agent."""
    try:
        async with session.query_lock:
            if not is_awake(session):
                try:
                    await wake_agent(session)
                except ConcurrencyLimitError:
                    log.info("Concurrency limit hit for '%s' initial prompt — queuing", session.name)
                    session.message_queue.append((prompt, channel, None))
                    awake = _count_awake_agents()
                    await send_system(
                        channel,
                        f"⏳ All {awake} agent slots are busy. Initial prompt queued — will run when a slot opens.",
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
            await send_long(channel, f"*System:* 📝 **Initial prompt:**\n{prompt_text}")

            if session._log:
                session._log.info("PROMPT: %s", _content_summary(prompt))
            log.info("INITIAL_PROMPT[%s] running initial prompt: %s", session.name, _content_summary(prompt))
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
        await send_system(channel, f"Agent **{session.name}** finished initial task.")

    except Exception:
        log.exception("Error running initial prompt for agent '%s'", session.name)
        await send_system(channel, f"Agent **{session.name}** encountered an error during initial task.")

    await _process_message_queue(session)

    try:
        await sleep_agent(session)
    except Exception:
        log.exception("Error sleeping agent '%s' after initial prompt", session.name)


async def _process_message_queue(session: AgentSession) -> None:
    """Process any queued messages for an agent after the current query finishes."""
    if session.message_queue:
        log.info("QUEUE[%s] processing %d queued messages", session.name, len(session.message_queue))
    while session.message_queue:
        if shutdown_coordinator and shutdown_coordinator.requested:
            log.info("Shutdown requested — not processing further queued messages for '%s'", session.name)
            break
        content, channel, orig_message = session.message_queue.popleft()

        remaining = len(session.message_queue)
        log.debug("Processing queued message for '%s' (%d remaining)", session.name, remaining)
        if session._log:
            session._log.info("QUEUED_MSG: %s", _content_summary(content))
        await _remove_reaction(orig_message, "📨")
        preview = _content_summary(content)
        remaining_str = f" ({remaining} more in queue)" if remaining > 0 else ""
        await send_system(channel, f"Processing queued message{remaining_str}:\n> {preview}")

        async with session.query_lock:
            if not is_awake(session):
                try:
                    await wake_agent(session)
                    await _post_model_warning(session)
                except Exception:
                    log.exception("Failed to wake agent '%s' for queued message", session.name)
                    await _add_reaction(orig_message, "❌")
                    await send_system(
                        channel,
                        f"Failed to wake agent **{session.name}** — dropping queued message.",
                    )
                    while session.message_queue:
                        _, ch, dropped_msg = session.message_queue.popleft()
                        await _remove_reaction(dropped_msg, "📨")
                        await _add_reaction(dropped_msg, "❌")
                        await send_system(
                            ch,
                            f"Failed to wake agent **{session.name}** — dropping queued message.",
                        )
                    return

            _reset_session_activity(session)
            try:
                await process_message(session, content, channel)
                await _add_reaction(orig_message, "✅")
            except TimeoutError:
                await _add_reaction(orig_message, "⏳")
                await _handle_query_timeout(session, channel)
            except RuntimeError as e:
                log.warning(
                    "Runtime error processing queued message for '%s': %s",
                    session.name,
                    e,
                )
                await _add_reaction(orig_message, "❌")
                await send_system(channel, str(e))
            except Exception:
                log.exception("Error processing queued message for '%s'", session.name)
                await _add_reaction(orig_message, "❌")
                await send_system(
                    channel,
                    f"Error processing queued message for **{session.name}**.",
                )
            finally:
                session.activity = ActivityState(phase="idle")


# ---------------------------------------------------------------------------
# Inter-agent messaging
# ---------------------------------------------------------------------------


async def _deliver_inter_agent_message(
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
        f"📨 **Message from {sender_name}:**\n> {content}",
    )

    ts_prefix = datetime.now(UTC).strftime("[%Y-%m-%d %H:%M:%S UTC] ")
    prompt = ts_prefix + f"[Inter-agent message from {sender_name}] {content}"

    if target_session.query_lock.locked():
        target_session.message_queue.appendleft((prompt, channel, None))
        log.info(
            "Inter-agent message from '%s' to busy agent '%s' — interrupting",
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
        asyncio.create_task(_process_inter_agent_prompt(target_session, prompt, channel))
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
                    await _post_model_warning(session)
                except ConcurrencyLimitError:
                    session.message_queue.append((content, channel, None))
                    awake = _count_awake_agents()
                    log.info(
                        "Concurrency limit hit for '%s' inter-agent message — queuing",
                        session.name,
                    )
                    await send_system(
                        channel,
                        f"⏳ All {awake} agent slots busy. Inter-agent message queued.",
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
                await _handle_query_timeout(session, channel)
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

        await _process_message_queue(session)
    except Exception:
        log.exception(
            "Unhandled error in _process_inter_agent_prompt for '%s'",
            session.name,
        )


# ---------------------------------------------------------------------------
# Bridge connection and reconnection
# ---------------------------------------------------------------------------


async def _connect_bridge() -> None:
    """Connect to the agent bridge and schedule reconnections for running agents."""
    global bridge_conn

    try:
        bridge_conn = await ensure_bridge(config.BRIDGE_SOCKET_PATH, timeout=10.0)
        log.info("Bridge connection established")
    except Exception:
        log.exception("Failed to connect to bridge — agents will use direct subprocess mode")
        bridge_conn = None
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
                log.info("Flowcoder disabled — killing bridge agent '%s'", agent_name)
                try:
                    await bridge_conn.send_command("kill", name=agent_name)
                except Exception:
                    log.exception("Failed to kill bridge flowcoder '%s' (disabled)", agent_name)
                continue
            base_name = agent_name.removesuffix(":flowcoder")
            session = agents.get(base_name)
            if session is None:
                log.warning("Bridge has flowcoder '%s' but no matching session — killing", agent_name)
                try:
                    await bridge_conn.send_command("kill", name=agent_name)
                except Exception:
                    log.exception("Failed to kill orphan bridge flowcoder '%s'", agent_name)
                continue
            if info.get("status") == "exited":
                log.info("Bridge flowcoder '%s' already exited — cleaning up", agent_name)
                try:
                    await bridge_conn.send_command("kill", name=agent_name)
                except Exception:
                    pass
                continue
            session._reconnecting = True
            asyncio.create_task(_reconnect_flowcoder(session, agent_name, info))
            continue

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
            agent_name,
            status,
            buffered,
        )

        session._reconnecting = True
        asyncio.create_task(_reconnect_and_drain(session, info))


async def _reconnect_and_drain(session: AgentSession, bridge_info: dict[str, Any]) -> None:
    """Reconnect a single agent to the bridge and drain any buffered output."""
    try:
        async with session.query_lock:
            if bridge_conn is None or not bridge_conn.is_alive:
                log.warning("Bridge connection lost during reconnect of '%s'", session.name)
                session._reconnecting = False
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

            if session._log:
                session._log.info(
                    "SESSION_RECONNECT via bridge (replayed=%d, idle=%s)",
                    replayed,
                    cli_idle,
                )

            if cli_status == "exited":
                log.info("Agent '%s' CLI exited while we were down", session.name)
                session._reconnecting = False

            session._reconnecting = False

            if cli_status == "running" and not cli_idle:
                session._bridge_busy = True
                channel = await get_agent_channel(session.name)
                if replayed > 0 and channel:
                    log.info("RECONNECT_DRAIN[%s] draining buffered output (replayed=%d)", session.name, replayed)
                    await send_system(channel, "*(reconnected after restart — resuming output)*")
                    try:
                        async with asyncio.timeout(config.QUERY_TIMEOUT):
                            await stream_response_to_channel(session, channel)
                    except TimeoutError:
                        log.warning("Drain timeout for '%s' — continuing", session.name)
                    except Exception:
                        log.exception("Error draining buffered output for '%s'", session.name)
                    session._bridge_busy = False
                    session.last_activity = datetime.now(UTC)
                elif channel:
                    await send_system(channel, "*(reconnected after restart — task still running)*")
                log.info(
                    "Agent '%s' reconnected mid-task (idle=False, replayed=%d, bridge_busy=%s)",
                    session.name,
                    replayed,
                    session._bridge_busy,
                )
            elif cli_status == "running":
                channel = await get_agent_channel(session.name)
                if channel:
                    await send_system(channel, "*(reconnected after restart)*")
                log.info("Agent '%s' reconnected idle (between turns)", session.name)

            log.info("Reconnect complete for '%s'", session.name)

    except Exception:
        log.exception("Failed to reconnect agent '%s'", session.name)
        session._reconnecting = False

    await _process_message_queue(session)


async def _reconnect_flowcoder(session: AgentSession, bridge_name: str, bridge_info: dict[str, Any]) -> None:
    """Reconnect to a flowcoder engine that survived bot.py restart."""
    log.info("Reconnecting flowcoder '%s' for session '%s'", bridge_name, session.name)
    try:
        async with session.query_lock:
            if bridge_conn is None or not bridge_conn.is_alive:
                log.warning("Bridge connection lost during flowcoder reconnect of '%s'", bridge_name)
                session._reconnecting = False
                return

            proc = BridgeFlowcoderProcess(
                bridge_name=bridge_name,
                conn=bridge_conn,
                command=session.flowcoder_command,
                args=session.flowcoder_args,
                cwd=session.cwd,
            )
            sub_result = await proc.subscribe()
            session.flowcoder_process = proc
            session._reconnecting = False

            replayed = sub_result.replayed or 0
            log.info(
                "Flowcoder '%s' subscribed (replayed=%d, status=%s)",
                bridge_name,
                replayed,
                sub_result.status,
            )

            channel = await get_agent_channel(session.name)

            await proc.send({"type": "status_request"})

            if channel:
                if replayed > 0:
                    await send_system(channel, "*(reconnected — resuming flowchart)*")
                await _stream_flowcoder_to_channel(session, channel)

            await proc.stop()
            session.flowcoder_process = None
            session.activity = ActivityState(phase="idle")
            log.info("Flowcoder '%s' reconnect complete — cleaned up", bridge_name)

    except Exception:
        log.exception("Failed to reconnect flowcoder '%s'", bridge_name)
        session._reconnecting = False

    await _process_message_queue(session)


# ---------------------------------------------------------------------------
# Shutdown coordinator initialization
# ---------------------------------------------------------------------------


def init_shutdown_coordinator() -> None:
    """Wire up the ShutdownCoordinator with real bot callbacks.

    Called once from on_ready after all helpers are defined.
    """
    global shutdown_coordinator
    assert _bot is not None

    async def _notify_agent_channel(agent_name: str, message: str) -> None:
        channel = await get_agent_channel(agent_name)
        if channel:
            await send_system(channel, message)

    async def _send_goodbye() -> None:
        for s in agents.values():
            if s.flowcoder_process and isinstance(s.flowcoder_process, BridgeFlowcoderProcess):
                await s.flowcoder_process.detach()
                log.info("Detached bridge flowcoder for '%s' before shutdown", s.name)
                s.flowcoder_process = None

        master_ch = await get_master_channel()
        if master_ch:
            await master_ch.send("*System:* Shutting down — see you soon!")

    use_bridge = bridge_conn is not None and bridge_conn.is_alive
    shutdown_coordinator = ShutdownCoordinator(
        agents=agents,
        sleep_fn=lambda s: sleep_agent(s, force=True),
        close_bot_fn=_bot.close,
        kill_fn=exit_for_restart if use_bridge else kill_supervisor,
        notify_fn=_notify_agent_channel,
        goodbye_fn=_send_goodbye,
        bridge_mode=use_bridge,
    )
