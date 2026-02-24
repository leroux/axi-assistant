import os
import re
import json
import asyncio
import threading
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from dotenv import load_dotenv
from discord import Intents, app_commands, CategoryChannel, TextChannel
from discord.ext.commands import Bot
from discord.ext import tasks
from discord.enums import ChannelType
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    ToolPermissionContext,
    PermissionResultAllow,
    PermissionResultDeny,
)
from croniter import croniter

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ALLOWED_USER_IDS = {int(uid.strip()) for uid in os.environ["ALLOWED_USER_IDS"].split(",")}
DEFAULT_CWD = os.environ.get("DEFAULT_CWD", os.getcwd())
SCHEDULE_TIMEZONE = ZoneInfo(os.environ.get("SCHEDULE_TIMEZONE", "UTC"))
DISCORD_GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])

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
SCHEDULES_PATH = os.path.join(BOT_DIR, "schedules.json")
HISTORY_PATH = os.path.join(BOT_DIR, "schedule_history.json")
SKIPS_PATH = os.path.join(BOT_DIR, "schedule_skips.json")
RESTART_SIGNAL_PATH = os.path.join(BOT_DIR, ".restart_requested")
SPAWN_SIGNAL_PATH = os.path.join(BOT_DIR, ".spawn_agent")
KILL_SIGNAL_PATH = os.path.join(BOT_DIR, ".kill_agent")
AGENT_HISTORY_PATH = os.path.join(BOT_DIR, "agent_history.json")
ACTIVE_SESSIONS_PATH = os.path.join(BOT_DIR, ".active_sessions")
ROLLBACK_MARKER_PATH = os.path.join(BOT_DIR, ".rollback_performed")
CRASH_ANALYSIS_MARKER_PATH = os.path.join(BOT_DIR, ".crash_analysis")
schedule_last_fired: dict[str, datetime] = {}

# --- Agent session management ---

MASTER_AGENT_NAME = "axi-master"
MAX_AGENTS = 20
IDLE_REMINDER_THRESHOLDS = [timedelta(minutes=30), timedelta(hours=3), timedelta(hours=48)]
QUERY_TIMEOUT = 600  # 10 minutes
INTERRUPT_TIMEOUT = 15  # seconds to wait after interrupt

ACTIVE_CATEGORY_NAME = "Active"
KILLED_CATEGORY_NAME = "Killed"


@dataclass
class AgentSession:
    name: str
    client: ClaudeSDKClient | None = None
    cwd: str = ""
    query_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stderr_buffer: list[str] = field(default_factory=list)
    stderr_lock: threading.Lock = field(default_factory=threading.Lock)
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    system_prompt: str | None = None
    last_idle_notified: datetime | None = None
    idle_reminder_count: int = 0
    session_id: str | None = None
    discord_channel_id: int | None = None
    message_queue: asyncio.Queue = field(default_factory=asyncio.Queue)


agents: dict[str, AgentSession] = {}

# Graceful shutdown flag
_shutdown_requested = False

# Guild infrastructure (populated in on_ready)
target_guild: discord.Guild | None = None
active_category: CategoryChannel | None = None
killed_category: CategoryChannel | None = None
channel_to_agent: dict[int, str] = {}  # channel_id -> agent_name


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


async def _as_stream(text: str):
    """Wrap a string prompt as an AsyncIterable for streaming mode (required by can_use_tool)."""
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }


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
    """Create a can_use_tool callback that restricts file writes to allowed_cwd."""
    allowed = os.path.realpath(allowed_cwd)

    async def _check_permission(
        tool_name: str, tool_input: dict, ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        # File-writing tools — check path is within cwd
        if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            resolved = os.path.realpath(path)
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return PermissionResultAllow()
            return PermissionResultDeny(
                message=f"Access denied: {path} is outside working directory {allowed}"
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


# --- Agent history helpers ---

def load_agent_history() -> list[dict]:
    try:
        with open(AGENT_HISTORY_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_agent_history(history: list[dict]) -> None:
    with open(AGENT_HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)
        f.write("\n")


def record_agent_spawn(name: str, cwd: str, session_id: str | None = None, resumed_from: str | None = None) -> None:
    """Record an agent spawn event in persistent history."""
    history = load_agent_history()
    history.append({
        "name": name,
        "cwd": cwd,
        "session_id": session_id,
        "spawned_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "resumed_from": resumed_from,
    })
    save_agent_history(history)


def update_agent_history_session_id(name: str, session_id: str) -> None:
    """Update the session_id for the most recent entry of an agent."""
    history = load_agent_history()
    for entry in reversed(history):
        if entry["name"] == name and entry["status"] == "active":
            entry["session_id"] = session_id
            break
    save_agent_history(history)


def mark_agent_killed(name: str, session_id: str | None = None) -> None:
    """Mark the most recent active entry for an agent as killed."""
    history = load_agent_history()
    for entry in reversed(history):
        if entry["name"] == name and entry["status"] == "active":
            entry["status"] = "killed"
            entry["killed_at"] = datetime.now(timezone.utc).isoformat()
            if session_id:
                entry["session_id"] = session_id
            break
    save_agent_history(history)


def save_active_sessions() -> None:
    """Save active non-master agent sessions to disk for resumption after restart."""
    sessions = []
    for name, session in agents.items():
        if name == MASTER_AGENT_NAME:
            continue
        if session.session_id:
            sessions.append({
                "name": name,
                "cwd": session.cwd,
                "session_id": session.session_id,
            })
    with open(ACTIVE_SESSIONS_PATH, "w") as f:
        json.dump(sessions, f, indent=2)
        f.write("\n")
    log.info("Saved %d active agent session(s) for restart resumption", len(sessions))


def load_active_sessions() -> list[dict]:
    """Load saved active sessions from disk. Deletes the file after reading."""
    if not os.path.exists(ACTIVE_SESSIONS_PATH):
        return []
    try:
        with open(ACTIVE_SESSIONS_PATH) as f:
            sessions = json.load(f)
        os.remove(ACTIVE_SESSIONS_PATH)
        log.info("Loaded %d saved agent session(s) to resume", len(sessions))
        return sessions
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load active sessions: %s", e)
        try:
            os.remove(ACTIVE_SESSIONS_PATH)
        except OSError:
            pass
        return []


def _set_session_id(session: AgentSession, msg: ResultMessage) -> None:
    """Extract session_id from a ResultMessage and persist to agent history."""
    sid = getattr(msg, "session_id", None)
    session.session_id = sid
    if sid and session.name != MASTER_AGENT_NAME:
        update_agent_history_session_id(session.name, sid)


SYSTEM_PROMPT = """\
You are Axi, a personal assistant communicating in a Discord server. \
Each agent session has its own dedicated text channel — you (the master agent) use #axi-master. \
You are a complete, autonomous system — not just an LLM behind a bot. \
Your surrounding infrastructure can send messages independently (e.g. startup notifications, scheduled events), not only in response to user messages. \
Keep responses concise and well-formatted for Discord (markdown, code blocks). \
Your user's profile and preferences are in USER_PROFILE.md in the current working directory. \
Their projects live under ~/coding-projects. \
The code in ~/coding-projects/personal-assistant is your own source code. \
Read USER_PROFILE.md at the start of conversations to personalize your responses. \
You can schedule events by editing schedules.json in your working directory. \
Each entry MUST have a "name" field (short identifier) and a "prompt" field (the message/instructions for you to respond to). \
For one-off events, use an "at" field with a timezone-aware ISO datetime (e.g. "2026-02-21T02:24:17+00:00"). \
For recurring events, use a "schedule" field with a cron expression. \
IMPORTANT: Cron times are evaluated in the SCHEDULE_TIMEZONE configured in .env, NOT in UTC. \
For example, if SCHEDULE_TIMEZONE=US/Pacific, then "0 10 * * *" means 10:00 AM Pacific, not 10:00 AM UTC. Do NOT write cron times in UTC — always use the local SCHEDULE_TIMEZONE. DST is handled automatically. \
Optional fields: "reset_context" (boolean, resets conversation before firing), \
"agent" (boolean, spawns a new agent session instead of routing through you — use this for heavy tasks to keep your context clean), \
"cwd" (string, working directory for the agent — required when "agent" is true), \
"session" (string, agent session name to reuse — multiple events with the same "session" value share one persistent agent. \
If the session already exists when an event fires, the prompt is sent to the existing agent instead of spawning a new one). \
Example one-off: {"name": "reminder", "prompt": "Say hello in 10 languages", "at": "2026-02-21T03:00:00+00:00"}. \
Example recurring: {"name": "daily-standup", "prompt": "Ask me what I'm working on today", "schedule": "0 9 * * *"}. \
Example agent schedule: {"name": "weekly-cleanup", "prompt": "Clean up unused imports", "schedule": "0 9 * * 1", "cwd": "/home/pride/coding-projects/my-app", "agent": true}. \
Example shared session: multiple events with "session": "my-agent" will all route to the same persistent agent session. \
To restart yourself, create the file \
.restart_requested in your project directory (e.g., `touch .restart_requested`). \
The system will automatically restart within 30 seconds. \
Only restart when the user explicitly asks you to — do not restart after every self-edit.

## Schedule Skips (One-Off Cancellations)

You can skip a single occurrence of a recurring event by editing schedule_skips.json in your working directory. \
Each entry has a "name" (matching the recurring event name) and a "skip_date" (YYYY-MM-DD in the SCHEDULE_TIMEZONE). \
Example: {"name": "morning-checkin", "skip_date": "2026-02-22"} skips the morning-checkin on Feb 22 only — it fires normally every other day. \
Expired skips (past dates) are auto-pruned by the scheduler. \
To **move** a recurring event to a different time on a specific day, compose two actions: \
1) Add a skip entry for that day in schedule_skips.json, and \
2) Add a one-off event in schedules.json with the same prompt but at the desired time. \
This is not a special feature — it's just combining a skip with a one-off.

## Agent Spawning

IMPORTANT: When the user says "spawn an agent" or "spawn a new agent," they mean an Axi agent session \
(a persistent Claude Code session with its own Discord channel), NOT a background subagent via the Task tool. \
Always use the `.spawn_agent` mechanism described below, not the Task tool, when the user asks to spawn an agent.

You can spawn independent Claude Code agent sessions to work on tasks autonomously. \
To spawn an agent, create a file called `.spawn_agent` in your project directory \
(~/coding-projects/personal-assistant/) with the following JSON format:

{"name": "agent-name", "cwd": "/absolute/path/to/project", "prompt": "Initial instructions for the agent"}

To resume a previous agent session, include the "resume" field with the session ID:
{"name": "agent-name", "cwd": "/absolute/path/to/project", "prompt": "Continue where you left off", "resume": "session-id-here"}

Rules for spawning agents:
- "name" must be unique, short, and descriptive (e.g. "feature-auth", "fix-bug-123"). No spaces.
- "cwd" must be the absolute path to the project directory the agent should work in.
- "prompt" is the initial task description. Be specific and detailed since the agent works independently.
- "resume" is an optional session ID from a previously killed agent. When provided, the new agent resumes \
with the full conversation context of the old session. Session IDs are shown when agents are killed, \
in /list-agents output, and persisted in agent_history.json. \
To find a previous agent's session ID, read agent_history.json — it logs every spawn and kill with \
the agent name, session ID, cwd, and timestamps.
- The spawned agent is a standard Claude Code session — it does NOT have your Axi personality or custom instructions.
- The system picks up the file within 30 seconds and spawns the session automatically.
- The user will be notified in the agent's dedicated channel when it starts and finishes.
- Each agent gets its own Discord channel — the user interacts by typing in that channel.
- You cannot spawn an agent named "axi-master" — that is reserved for you.
- Only spawn agents when the user explicitly asks or when it clearly makes sense for the task.

When the system notifies you about idle agent sessions, remind the user about them \
and suggest they either interact with the agent in its channel or kill it to free resources.

## Discord Message Query Tool

You can query Discord server message history on demand using discord_query.py in your working directory. \
Run it via bash to look up messages, browse channel history, or search for content.

### List servers the bot is in
```
python discord_query.py guilds
```
Returns JSONL with guild id and name. Use this to discover guild IDs.

### List channels in a server
```
python discord_query.py channels <guild_id>
```
Returns JSONL with channel id, name, type, and category.

### Fetch message history from a channel
```
python discord_query.py history <channel_id> [--limit 50] [--before DATETIME_OR_ID] [--after DATETIME_OR_ID] [--format text]
```
You can use guild_id:channel_name instead of a raw channel ID (e.g. `123456789:general`). \
Default format is JSONL. Use --format text for human-readable output. \
Accepts ISO datetimes (e.g. 2026-02-21T10:00:00+00:00) or Discord snowflake IDs for --before/--after. \
Max 500 messages per query.

### Search messages in a server
```
python discord_query.py search <guild_id> "search term" [--channel CHANNEL] [--author USERNAME] [--limit 50] [--format text]
```
Case-insensitive substring search over recent message history. \
Use --channel to limit to a specific channel, --author to filter by username. \
This scans recent history (not a full-text index), so results are limited to the last ~500 messages per channel.

## Communication Style

You are chatting in a Discord server channel — the user sees nothing until you send a message. \
Long silences feel broken. Send short progress updates as you work so the user knows you're alive. \
For example: "Reading the file now...", "Found the issue, fixing it", "Running tests". \
A one-line status every 30-60 seconds of work is ideal. Don't wait until you have a complete answer \
to say anything — a quick "looking into it" immediately followed by the full answer later is \
far better than 3 minutes of silence. Keep updates casual and brief (one short sentence). \
Final answers should still be thorough and well-formatted.

IMPORTANT: Never guess or fabricate answers. If you don't know something or lack context \
(e.g. from previous sessions), say so honestly and look it up — check files, code, history, \
or ask the user. Being wrong confidently is far worse than admitting you need to verify.

## Tool Restrictions — Discord Interface Compatibility

You are running inside a Discord channel interface, NOT the Claude Code terminal. \
The user can only see plain text messages you send — they cannot see or interact with \
structured UI elements from Claude Code tools. The following tools MUST NOT be used \
because they render as invisible or broken in Discord:

- **AskUserQuestion** — Do NOT use. The structured multiple-choice UI is invisible to the user. \
Instead, ask questions as normal text messages. If you want the user to choose between options, \
list them in your message (e.g. "1. Option A, 2. Option B — which do you prefer?").
- **TodoWrite** — Do NOT use. The visual task list is invisible to the user. \
If you need to track tasks, write them in a file or just list them in a message.
- **EnterPlanMode / ExitPlanMode** — Do NOT use. Plan mode is a Claude Code UI concept \
that doesn't exist in Discord. If you need to plan, just write out your plan in a message.
- **Skill** — Do NOT use. Skills are Claude Code UI features that don't translate to Discord.
- **EnterWorktree** — Do NOT use. Worktree management is a Claude Code UI feature.

Tools that DO work fine over Discord (use freely): \
Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, Task (for spawning subagents), \
NotebookEdit, and all MCP tools.\
"""


# --- Session lifecycle ---

async def start_session(name: str, cwd: str, system_prompt: str | None = None, resume: str | None = None) -> AgentSession:
    """Start a new named Claude session. Returns the AgentSession."""
    session = AgentSession(
        name=name,
        cwd=cwd,
        system_prompt=system_prompt,
    )
    options = ClaudeAgentOptions(
        model="opus",
        effort="high",
        thinking={"type": "adaptive"},
        betas=["context-1m-2025-08-07"],
        setting_sources=["user", "project", "local"],
        permission_mode="default",
        can_use_tool=make_cwd_permission_callback(cwd),
        cwd=cwd,
        system_prompt=system_prompt,
        include_partial_messages=True,
        stderr=make_stderr_callback(session),
        resume=resume,
        sandbox={"enabled": True, "autoAllowBashIfSandboxed": True},
    )
    session.client = ClaudeSDKClient(options=options)
    await session.client.__aenter__()
    agents[name] = session
    log.info("Claude session '%s' started (cwd=%s)", name, cwd)
    return session


async def end_session(name: str) -> None:
    """End a named Claude session and remove it from the registry."""
    session = agents.get(name)
    if session is None:
        return
    if session.client is not None:
        try:
            await asyncio.wait_for(session.client.__aexit__(None, None, None), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            log.warning("Claude session '%s' shutdown timed out or was cancelled", name)
        except RuntimeError as e:
            if "cancel scope" in str(e):
                log.debug("Claude session '%s' cross-task cleanup (expected): %s", name, e)
            else:
                raise
        session.client = None
    agents.pop(name, None)
    log.info("Claude session '%s' ended", name)


async def reset_session(name: str, cwd: str | None = None) -> AgentSession:
    """Reset a named session. Preserves its system prompt and channel mapping."""
    session = agents.get(name)
    old_cwd = session.cwd if session else DEFAULT_CWD
    old_prompt = session.system_prompt if session else SYSTEM_PROMPT
    old_channel_id = session.discord_channel_id if session else None
    await end_session(name)
    new_session = await start_session(name, cwd or old_cwd, old_prompt)
    new_session.discord_channel_id = old_channel_id
    return new_session


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
            view_channel=True,
            read_message_history=True,
        ),
        guild.me: discord.PermissionOverwrite(
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            view_channel=True,
            read_message_history=True,
        ),
    }
    for uid in ALLOWED_USER_IDS:
        overwrites[discord.Object(id=uid)] = discord.PermissionOverwrite(
            send_messages=True,
            add_reactions=True,
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

    if active_cat is None:
        active_cat = await guild.create_category(ACTIVE_CATEGORY_NAME, overwrites=overwrites)
        log.info("Created '%s' category", ACTIVE_CATEGORY_NAME)
    else:
        # Sync permissions on existing category
        await active_cat.edit(overwrites=overwrites)
        log.info("Synced permissions on '%s' category", ACTIVE_CATEGORY_NAME)
    active_category = active_cat

    if killed_cat is None:
        killed_cat = await guild.create_category(KILLED_CATEGORY_NAME, overwrites=overwrites)
        log.info("Created '%s' category", KILLED_CATEGORY_NAME)
    else:
        await killed_cat.edit(overwrites=overwrites)
        log.info("Synced permissions on '%s' category", KILLED_CATEGORY_NAME)
    killed_category = killed_cat

    return guild, active_cat, killed_cat


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
    channel = await target_guild.create_text_channel(normalized, category=active_category)
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

    # Remove from channel_to_agent mapping
    to_remove = [cid for cid, name in channel_to_agent.items() if name == agent_name]
    for cid in to_remove:
        del channel_to_agent[cid]


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
    for chunk in split_message(text.strip()):
        if chunk:
            try:
                await channel.send(chunk)
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


# --- Streaming response ---

async def _receive_response_safe(client: ClaudeSDKClient):
    """Wrapper around receive_response() that skips unknown message types."""
    from claude_agent_sdk._internal.message_parser import parse_message

    async for data in client._query.receive_messages():
        try:
            parsed = parse_message(data)
        except MessageParseError:
            log.debug("Skipping unknown SDK message type: %s", data.get("type"))
            continue
        yield parsed
        if isinstance(parsed, ResultMessage):
            return


async def stream_response_to_channel(session: AgentSession, channel, show_awaiting_input: bool = True) -> None:
    """Stream Claude's response from a specific agent session to a Discord channel.
    Each agent always streams to its own channel — no visibility filtering needed."""

    text_buffer = ""

    async def flush_text(text: str) -> None:
        if not text.strip():
            return
        await send_long(channel, text.lstrip())

    async with channel.typing():
        async for msg in _receive_response_safe(session.client):
            # Drain and send any stderr messages first
            for stderr_msg in drain_stderr(session):
                stderr_text = stderr_msg.strip()
                if stderr_text:
                    for part in split_message(f"```\n{stderr_text}\n```"):
                        await channel.send(part)

            if isinstance(msg, StreamEvent):
                event = msg.event
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_buffer += delta.get("text", "")

            elif isinstance(msg, AssistantMessage):
                await flush_text(text_buffer)
                text_buffer = ""

            elif isinstance(msg, ResultMessage):
                _set_session_id(session, msg)
                break

            # When buffer is large enough, flush it mid-turn
            if len(text_buffer) >= 1800:
                split_at = text_buffer.rfind("\n", 0, 1800)
                if split_at == -1:
                    split_at = 1800
                to_send = text_buffer[:split_at]
                text_buffer = text_buffer[split_at:].lstrip("\n")
                await flush_text(to_send)

    # Flush any remaining stderr
    for stderr_msg in drain_stderr(session):
        stderr_text = stderr_msg.strip()
        if stderr_text:
            for part in split_message(f"```\n{stderr_text}\n```"):
                await channel.send(part)

    # Flush remaining text buffer
    await flush_text(text_buffer)

    # Notify that the bot is done responding
    if show_awaiting_input:
        await send_system(channel, "Bot has finished responding and is awaiting input.")


async def _handle_query_timeout(session: AgentSession, channel) -> None:
    """Handle a query timeout. Try interrupt first, then kill and resume."""
    log.warning("Query timeout for agent '%s', attempting interrupt", session.name)

    # Step 1: Try graceful interrupt
    try:
        await session.client.interrupt()
        async with asyncio.timeout(INTERRUPT_TIMEOUT):
            async for msg in _receive_response_safe(session.client):
                if isinstance(msg, ResultMessage):
                    _set_session_id(session, msg)
                    break
        session.last_activity = datetime.now(timezone.utc)
        await send_system(
            channel,
            f"Agent **{session.name}** timed out and was interrupted. Context preserved.",
        )
        return
    except (TimeoutError, Exception):
        log.warning("Interrupt failed for agent '%s', killing and resuming session", session.name)

    # Step 2: Kill and resume from last known session_id
    old_session_id = session.session_id
    old_name = session.name
    old_cwd = session.cwd
    old_prompt = session.system_prompt
    old_channel_id = session.discord_channel_id
    await end_session(old_name)

    if old_session_id:
        new_session = await start_session(old_name, old_cwd, old_prompt, resume=old_session_id)
        new_session.discord_channel_id = old_channel_id
        await send_system(channel, f"Agent **{old_name}** timed out and was recovered. Context preserved.")
    else:
        new_session = await start_session(old_name, old_cwd, old_prompt)
        new_session.discord_channel_id = old_channel_id
        await send_system(channel, f"Agent **{old_name}** timed out and was reset. Context lost.")


# --- Agent spawning ---


async def reclaim_agent_name(name: str) -> None:
    """If an agent with *name* already exists, kill it silently to free the name."""
    if name not in agents:
        return
    log.info("Reclaiming agent name '%s' — terminating existing session", name)
    session = agents.get(name)
    mark_agent_killed(name, session.session_id if session else None)
    await end_session(name)
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

    session = await start_session(name, cwd, system_prompt=None, resume=resume)
    if resume:
        session.session_id = resume
    session.discord_channel_id = channel.id
    channel_to_agent[channel.id] = name
    record_agent_spawn(name, cwd, session_id=resume, resumed_from=resume)

    if not initial_prompt:
        await send_system(channel, f"Agent **{name}** is ready.")
        return

    asyncio.create_task(_run_initial_prompt(session, initial_prompt, channel))


async def send_prompt_to_agent(agent_name: str, prompt: str) -> None:
    """Send a prompt to an existing agent session in the background.

    Used by the scheduler when a 'session' field maps to an already-running agent.
    Queues the prompt just like a user message would, streaming the response to the
    agent's Discord channel.
    """
    session = agents.get(agent_name)
    if session is None or session.client is None:
        log.warning("send_prompt_to_agent: agent '%s' not found or has no client", agent_name)
        return

    channel = await get_agent_channel(agent_name)
    if channel is None:
        log.warning("send_prompt_to_agent: no channel for agent '%s'", agent_name)
        return

    asyncio.create_task(_run_initial_prompt(session, prompt, channel))


async def _run_initial_prompt(session: AgentSession, prompt: str, channel: TextChannel) -> None:
    """Run the initial prompt for a spawned agent. Notifies when done."""
    try:
        timed_out = False
        async with session.query_lock:
            session.last_activity = datetime.now(timezone.utc)
            drain_stderr(session)
            drain_sdk_buffer(session)
            try:
                async with asyncio.timeout(QUERY_TIMEOUT):
                    await session.client.query(_as_stream(prompt))
                    await stream_response_to_channel(session, channel, show_awaiting_input=False)
                    session.last_activity = datetime.now(timezone.utc)
            except TimeoutError:
                timed_out = True
                await _handle_query_timeout(session, channel)

        if not timed_out:
            await send_system(channel, f"Agent **{session.name}** finished initial task.")

    except Exception:
        log.exception("Error running initial prompt for agent '%s'", session.name)
        await send_system(channel, f"Agent **{session.name}** encountered an error during initial task.")

    await _process_message_queue(session)


async def _process_message_queue(session: AgentSession) -> None:
    """Process any queued messages for an agent after the current query finishes."""
    while not session.message_queue.empty():
        if _shutdown_requested:
            log.info("Shutdown requested — not processing further queued messages for '%s'", session.name)
            break
        content, channel = session.message_queue.get_nowait()
        if session.client is None:
            await send_system(channel, f"Agent **{session.name}** session ended — dropping queued message.")
            # Clear remaining queue
            while not session.message_queue.empty():
                _, ch = session.message_queue.get_nowait()
                await send_system(ch, f"Agent **{session.name}** session ended — dropping queued message.")
            return

        remaining = session.message_queue.qsize()
        if remaining > 0:
            await send_system(channel, f"Processing queued message ({remaining} more in queue)…")

        async with session.query_lock:
            session.last_activity = datetime.now(timezone.utc)
            session.last_idle_notified = None
            session.idle_reminder_count = 0
            drain_stderr(session)
            drain_sdk_buffer(session)
            try:
                async with asyncio.timeout(QUERY_TIMEOUT):
                    await session.client.query(_as_stream(content))
                    await stream_response_to_channel(session, channel)
            except TimeoutError:
                await _handle_query_timeout(session, channel)
            except Exception:
                log.exception("Error querying agent '%s' (queued message)", session.name)
                await send_system(
                    channel,
                    f"Error processing queued message for **{session.name}**.",
                )

        await process_kill_signal()
        await process_spawn_signal()


# --- Spawn signal processing ---


async def process_spawn_signal() -> None:
    """Check for and process a pending .spawn_agent signal file."""
    if not os.path.exists(SPAWN_SIGNAL_PATH):
        return

    try:
        with open(SPAWN_SIGNAL_PATH) as f:
            spawn_data = json.load(f)
        os.remove(SPAWN_SIGNAL_PATH)

        agent_name = spawn_data.get("name", "").strip()
        agent_cwd = spawn_data.get("cwd", DEFAULT_CWD)
        agent_prompt = spawn_data.get("prompt", "")
        agent_resume = spawn_data.get("resume")  # optional session ID to resume

        # For error notifications, use the master channel
        master_ch = await get_master_channel()

        if not agent_name:
            log.warning("Spawn signal missing 'name' field, ignoring")
        elif agent_name == MASTER_AGENT_NAME:
            log.warning("Cannot spawn agent with reserved name '%s'", MASTER_AGENT_NAME)
            if master_ch:
                await send_system(master_ch, f"Cannot spawn agent with reserved name **{MASTER_AGENT_NAME}**.")
        elif agent_name in agents and not agent_resume:
            log.warning("Agent '%s' already exists, ignoring spawn signal", agent_name)
            if master_ch:
                await send_system(master_ch, f"Agent **{agent_name}** already exists.")
        elif agent_name in agents and agent_resume:
            # Resume implies replacing the old session with the resumed one
            log.info("Reclaiming agent '%s' for resume (session=%s)", agent_name, agent_resume)
            await reclaim_agent_name(agent_name)
            log.info("Spawning agent '%s' (cwd=%s, resume=%s)", agent_name, agent_cwd, agent_resume)
            await spawn_agent(agent_name, agent_cwd, agent_prompt, resume=agent_resume)
        elif len(agents) >= MAX_AGENTS:
            log.warning("Max agents (%d) reached, ignoring spawn signal", MAX_AGENTS)
            if master_ch:
                await send_system(master_ch, f"Maximum number of agents ({MAX_AGENTS}) reached. Kill an agent first.")
        else:
            log.info("Spawning agent '%s' (cwd=%s, resume=%s)", agent_name, agent_cwd, agent_resume)
            await spawn_agent(agent_name, agent_cwd, agent_prompt, resume=agent_resume)
    except Exception:
        log.exception("Error processing spawn signal")
        if os.path.exists(SPAWN_SIGNAL_PATH):
            os.remove(SPAWN_SIGNAL_PATH)


async def process_kill_signal() -> None:
    """Check for and process a pending .kill_agent signal file.

    File format: {"name": "agent-name"}
    """
    if not os.path.exists(KILL_SIGNAL_PATH):
        return

    try:
        with open(KILL_SIGNAL_PATH) as f:
            kill_data = json.load(f)
        os.remove(KILL_SIGNAL_PATH)

        agent_name = kill_data.get("name", "").strip()
        master_ch = await get_master_channel()

        if not agent_name:
            log.warning("Kill signal missing 'name' field, ignoring")
        elif agent_name == MASTER_AGENT_NAME:
            log.warning("Cannot kill reserved agent '%s'", MASTER_AGENT_NAME)
            if master_ch:
                await send_system(master_ch, f"Cannot kill reserved agent **{MASTER_AGENT_NAME}**.")
        elif agent_name not in agents:
            log.warning("Kill signal for unknown agent '%s', ignoring", agent_name)
            if master_ch:
                await send_system(master_ch, f"Agent **{agent_name}** not found.")
        else:
            session = agents.get(agent_name)
            session_id = session.session_id if session else None
            agent_ch = await get_agent_channel(agent_name)

            log.info("Killing agent '%s' via signal file (session=%s)", agent_name, session_id)
            mark_agent_killed(agent_name, session_id)

            # Notify in the agent's channel before moving it
            if agent_ch:
                if session_id:
                    await send_system(
                        agent_ch,
                        f"Agent **{agent_name}** terminated.\n"
                        f"Session ID: `{session_id}` — use this to resume later.",
                    )
                else:
                    await send_system(agent_ch, f"Agent **{agent_name}** terminated.")

            await end_session(agent_name)
            await move_channel_to_killed(agent_name)

    except Exception:
        log.exception("Error processing kill signal")
        if os.path.exists(KILL_SIGNAL_PATH):
            os.remove(KILL_SIGNAL_PATH)


# --- Graceful shutdown ---


async def _graceful_shutdown(source: str) -> None:
    """Wait for all busy agents to finish, then exit with code 42."""
    global _shutdown_requested
    if _shutdown_requested:
        log.info("Graceful shutdown already in progress (ignoring duplicate from %s)", source)
        return
    _shutdown_requested = True
    log.info("Graceful shutdown initiated from %s", source)

    # Find busy agents
    busy = {name: s for name, s in agents.items() if s.query_lock.locked()}

    if not busy:
        log.info("No agents busy — exiting immediately")
        save_active_sessions()
        await bot.close()
        os._exit(42)

    # Notify each busy agent's channel
    for name, session in busy.items():
        channel = await get_agent_channel(name)
        if channel:
            await send_system(channel, f"Restart pending — waiting for **{name}** to finish current task...")

    # Wait loop: check every 5s, message every 30s, hard timeout at 10 min
    HARD_TIMEOUT = QUERY_TIMEOUT  # 10 minutes
    elapsed = 0
    last_status_msg = 0

    while elapsed < HARD_TIMEOUT:
        await asyncio.sleep(5)
        elapsed += 5

        still_busy = {name: s for name, s in agents.items() if s.query_lock.locked()}
        if not still_busy:
            log.info("All agents finished after %ds — exiting", elapsed)
            save_active_sessions()
            await bot.close()
            os._exit(42)

        # Send status update every 30s
        if elapsed - last_status_msg >= 30:
            last_status_msg = elapsed
            for name in still_busy:
                channel = await get_agent_channel(name)
                if channel:
                    await send_system(channel, f"Still waiting for **{name}** to finish... ({elapsed}s)")

    # Hard timeout
    still_busy = [name for name, s in agents.items() if s.query_lock.locked()]
    log.warning("Hard timeout reached (%ds) — force exiting. Still busy: %s", HARD_TIMEOUT, still_busy)
    save_active_sessions()
    await bot.close()
    os._exit(42)


# --- Message handler ---

@bot.event
async def on_message(message):
    if message.author.bot:
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

    log.info("Message from %s in #%s: %s", message.author, message.channel.name, message.content[:200])

    if _shutdown_requested:
        await send_system(message.channel, "Bot is restarting — not accepting new messages.")
        return

    # Look up which agent owns this channel
    agent_name = channel_to_agent.get(message.channel.id)
    if agent_name is None:
        # Check if the channel is in the killed category
        if (killed_category and message.channel.category_id
                and message.channel.category_id == killed_category.id):
            master_session = get_master_session()
            master_mention = f"<#{master_session.discord_channel_id}>" if master_session and master_session.discord_channel_id else "#axi-master"
            await send_system(
                message.channel,
                f"This agent has been terminated. To respawn it, ask in {master_mention} "
                f"or use the session ID from the kill message.",
            )
        return  # Untracked channel, ignore

    session = agents.get(agent_name)
    if session is None or session.client is None:
        await send_system(message.channel, f"Agent **{agent_name}** has no active session.")
        return

    if session.query_lock.locked():
        await session.message_queue.put((message.content, message.channel))
        position = session.message_queue.qsize()
        await send_system(
            message.channel,
            f"Agent **{agent_name}** is busy — message queued (position {position}).",
        )
        return

    async with session.query_lock:
        session.last_activity = datetime.now(timezone.utc)
        session.last_idle_notified = None
        session.idle_reminder_count = 0
        drain_stderr(session)
        drain_sdk_buffer(session)
        try:
            async with asyncio.timeout(QUERY_TIMEOUT):
                await session.client.query(_as_stream(message.content))
                await stream_response_to_channel(session, message.channel)
        except TimeoutError:
            await _handle_query_timeout(session, message.channel)
        except Exception:
            log.exception("Error querying agent '%s'", agent_name)
            await send_system(
                message.channel,
                f"Error communicating with agent **{agent_name}**. The session may have crashed. "
                f"Try `/kill-agent {agent_name}` and respawn.",
            )

    # Process kill/spawn signals immediately after query completes
    await process_kill_signal()
    await process_spawn_signal()

    # Process any messages that were queued while the agent was busy
    await _process_message_queue(session)

    await bot.process_commands(message)


# --- Scheduler loop ---

@tasks.loop(seconds=10)
async def check_schedules():
    # Restart signal check
    if os.path.exists(RESTART_SIGNAL_PATH):
        os.remove(RESTART_SIGNAL_PATH)
        log.info("Restart signal detected")
        await _graceful_shutdown("restart signal file")
        return

    # If shutdown is in progress, skip all scheduled work
    if _shutdown_requested:
        return

    # Kill/spawn signal checks
    await process_kill_signal()
    await process_spawn_signal()

    master = get_master_session()
    if master is None or master.client is None:
        return

    prune_history()
    prune_skips()

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now(SCHEDULE_TIMEZONE)
    entries = load_schedules()
    entries_modified = False

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

                if name not in schedule_last_fired:
                    schedule_last_fired[name] = last_occurrence

                if last_occurrence > schedule_last_fired[name]:
                    schedule_last_fired[name] = last_occurrence

                    if check_skip(name):
                        log.info("Skipping recurring event (one-off skip): %s", name)
                        continue

                    log.info("Firing recurring event: %s", name)
                    agent_name = entry.get("session", name)
                    agent_cwd = entry.get("cwd", DEFAULT_CWD)

                    if agent_name in agents:
                        # Session already exists — send prompt to it
                        log.info("Routing event '%s' to existing session '%s'", name, agent_name)
                        await send_prompt_to_agent(agent_name, entry["prompt"])
                    elif len(agents) >= MAX_AGENTS:
                        log.warning("Max agents reached, skipping event %s", name)
                        if master_ch:
                            await send_system(master_ch, f"Scheduled event **{name}** skipped — max agents ({MAX_AGENTS}) reached.")
                    else:
                        await reclaim_agent_name(agent_name)
                        await spawn_agent(agent_name, agent_cwd, entry["prompt"])

            elif "at" in entry:
                # One-off event
                fire_at = datetime.fromisoformat(entry["at"])

                if fire_at <= now_utc:
                    log.info("Firing one-off event: %s", name)
                    agent_name = entry.get("session", name)
                    agent_cwd = entry.get("cwd", DEFAULT_CWD)

                    if agent_name in agents:
                        log.info("Routing event '%s' to existing session '%s'", name, agent_name)
                        await send_prompt_to_agent(agent_name, entry["prompt"])
                    elif len(agents) >= MAX_AGENTS:
                        log.warning("Max agents reached, skipping event %s", name)
                        if master_ch:
                            await send_system(master_ch, f"Scheduled event **{name}** skipped — max agents ({MAX_AGENTS}) reached.")
                    else:
                        await reclaim_agent_name(agent_name)
                        await spawn_agent(agent_name, agent_cwd, entry["prompt"])

                    # Remove from schedules and add to history
                    entries.remove(entry)
                    entries_modified = True
                    append_history(entry, now_utc)

        except Exception:
            log.exception("Error processing scheduled event %s", name)

    if entries_modified:
        save_schedules(entries)

    # --- Idle agent detection ---
    idle_agents = []
    for agent_name, session in agents.items():
        if agent_name == MASTER_AGENT_NAME:
            continue
        if session.query_lock.locked():
            continue  # Agent is busy (possibly stuck), not idle
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
        # Also notify in the master channel so Axi can remind the user
        if master_ch:
            await send_system(
                master_ch,
                f"Agent **{agent_name}** has been idle for {idle_minutes} minutes "
                f"(cwd: `{session.cwd}`). Use `/kill-agent` to terminate.",
            )
        session.idle_reminder_count += 1
        session.last_idle_notified = datetime.now(timezone.utc)


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
        busy = " [busy]" if session.query_lock.locked() else ""
        protected = " [protected]" if name == MASTER_AGENT_NAME else ""
        sid = f" | sid: `{session.session_id[:8]}…`" if session.session_id else ""
        ch_mention = f" | <#{session.discord_channel_id}>" if session.discord_channel_id else ""
        lines.append(
            f"- **{name}**{busy}{protected}{ch_mention} | cwd: `{session.cwd}` | idle: {idle_minutes}m{sid}"
        )

    await interaction.response.send_message("*System:* **Agent Sessions:**\n" + "\n".join(lines))


@bot.tree.command(name="kill-agent", description="Terminate an agent session.")
@app_commands.autocomplete(agent_name=killable_agent_autocomplete)
async def kill_agent(interaction, agent_name: str):
    log.info("Slash command /kill-agent %s from %s", agent_name, interaction.user)
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
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
    mark_agent_killed(agent_name, session_id)

    # Notify in the agent's channel before archiving
    agent_ch = await get_agent_channel(agent_name)
    if agent_ch and agent_ch.id != interaction.channel_id:
        if session_id:
            await send_system(
                agent_ch,
                f"Agent **{agent_name}** terminated.\n"
                f"Session ID: `{session_id}` — use this to resume later.",
            )
        else:
            await send_system(agent_ch, f"Agent **{agent_name}** terminated.")

    await end_session(agent_name)
    await move_channel_to_killed(agent_name)

    if session_id:
        await interaction.followup.send(
            f"*System:* Agent **{agent_name}** terminated. Channel moved to Killed.\n"
            f"Session ID: `{session_id}` — use this to resume later."
        )
    else:
        await interaction.followup.send(f"*System:* Agent **{agent_name}** terminated. Channel moved to Killed.")


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


@bot.tree.command(name="restart", description="Restart the bot. Use force=True to skip waiting for agents.")
@app_commands.describe(force="Skip waiting for busy agents and restart immediately")
async def restart_cmd(interaction, force: bool = False):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    if force:
        await interaction.response.send_message("*System:* Force restarting...")
        log.info("Force restart requested via /restart command")
        save_active_sessions()
        await bot.close()
        os._exit(42)

    await interaction.response.send_message("*System:* Initiating graceful restart...")
    log.info("Restart requested via /restart command")
    await _graceful_shutdown("slash command")


# --- Startup ---

_on_ready_fired = False

@bot.event
async def on_ready():
    global _on_ready_fired
    log.info("Bot ready as %s", bot.user)

    if _on_ready_fired:
        log.info("on_ready fired again (gateway reconnect) — skipping startup logic")
        return
    _on_ready_fired = True

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            await start_session(MASTER_AGENT_NAME, DEFAULT_CWD, system_prompt=SYSTEM_PROMPT)
            break
        except Exception:
            log.exception("start_session failed (attempt %d/%d)", attempt, max_retries)
            if attempt < max_retries:
                await asyncio.sleep(5 * attempt)
            else:
                log.critical("All %d session startup attempts failed — exiting", max_retries)
                os._exit(1)

    # Set up guild infrastructure (categories + master channel)
    try:
        await ensure_guild_infrastructure()
        master_channel = await ensure_agent_channel(MASTER_AGENT_NAME)
        master_session = agents.get(MASTER_AGENT_NAME)
        if master_session:
            master_session.discord_channel_id = master_channel.id
        channel_to_agent[master_channel.id] = MASTER_AGENT_NAME
        log.info("Guild infrastructure ready (guild=%s, master_channel=#%s)", DISCORD_GUILD_ID, master_channel.name)

        # Set channel topic for master
        try:
            await master_channel.edit(topic="Axi master agent — always active, cannot be killed")
        except discord.HTTPException:
            pass  # Not critical

    except Exception:
        log.exception("Failed to set up guild infrastructure — guild channels won't work")

    # Discover existing agent channels in Active category
    if active_category:
        for ch in active_category.text_channels:
            normalized = ch.name
            # Check if there's a known agent for this channel
            # (agents are only in-memory after restart, so only master is known at this point)
            if normalized not in [_normalize_channel_name(n) for n in agents]:
                log.debug("Found orphan Active channel #%s (no matching agent session)", normalized)

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
            msg_lines.append("Spawning crash analysis agent...")
            await master_ch.send("\n".join(msg_lines))
        elif crash_info:
            exit_code = crash_info.get("exit_code", "unknown")
            uptime = crash_info.get("uptime_seconds", "?")
            timestamp = crash_info.get("timestamp", "unknown")
            await master_ch.send(
                f"*System:* **Runtime crash detected.**\n"
                f"Axi crashed after {uptime}s of uptime (exit code {exit_code}) at {timestamp}.\n"
                f"Spawning crash analysis agent..."
            )
        else:
            await master_ch.send("*System:* Axi restarted.")
        log.info("Sent restart notification to master channel")

    # Resume previously active agent sessions (saved before restart)
    saved_sessions = load_active_sessions()
    for saved in saved_sessions:
        name = saved.get("name")
        cwd = saved.get("cwd")
        sid = saved.get("session_id")
        if not name or not cwd or not sid:
            log.warning("Skipping invalid saved session entry: %s", saved)
            continue
        try:
            log.info("Resuming agent '%s' (session=%s, cwd=%s)", name, sid[:8], cwd)
            await spawn_agent(name, cwd, initial_prompt="", resume=sid)
        except Exception:
            log.exception("Failed to resume agent '%s'", name)
            if master_ch:
                await master_ch.send(f"*System:* Failed to resume agent **{name}** after restart.")

    # Spawn crash handler agent if a crash was detected (startup or runtime)
    if rollback_info:
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

        if len(agents) < MAX_AGENTS:
            await spawn_agent("crash-handler", BOT_DIR, crash_prompt)
        else:
            log.warning("Max agents reached, cannot spawn crash handler")
            if master_ch:
                await send_system(master_ch, "Could not spawn crash analysis agent — max agents reached.")

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

        if len(agents) < MAX_AGENTS:
            await spawn_agent("crash-handler", BOT_DIR, crash_prompt)
        else:
            log.warning("Max agents reached, cannot spawn crash handler")
            if master_ch:
                await send_system(master_ch, "Could not spawn crash analysis agent — max agents reached.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
