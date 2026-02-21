import os
import json
import asyncio
import threading
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from discord import Intents, app_commands
from discord.ext.commands import Bot
from discord.ext import tasks
from discord.enums import ChannelType
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk.types import AssistantMessage, ResultMessage, StreamEvent
from croniter import croniter

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ALLOWED_USER_IDS = {int(uid.strip()) for uid in os.environ["ALLOWED_USER_IDS"].split(",")}
DEFAULT_CWD = os.environ.get("DEFAULT_CWD", os.getcwd())
ALLOWED_GUILD_IDS = {
    int(gid.strip())
    for gid in os.environ.get("ALLOWED_GUILD_IDS", "").split(",")
    if gid.strip()
}
SCHEDULE_TIMEZONE = ZoneInfo(os.environ.get("SCHEDULE_TIMEZONE", "UTC"))

# --- Discord bot setup ---

intents = Intents(dm_messages=True, message_content=True, guild_messages=True, guilds=True)
bot = Bot(command_prefix="!", intents=intents)

# --- Scheduler state ---

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEDULES_PATH = os.path.join(BOT_DIR, "schedules.json")
HISTORY_PATH = os.path.join(BOT_DIR, "schedule_history.json")
RESTART_SIGNAL_PATH = os.path.join(BOT_DIR, ".restart_requested")
SPAWN_SIGNAL_PATH = os.path.join(BOT_DIR, ".spawn_agent")
ROLLBACK_MARKER_PATH = os.path.join(BOT_DIR, ".rollback_performed")
SERVER_LOG_PATH = os.path.join(BOT_DIR, "server_log.jsonl")
SERVER_LOG_MAX_LINES = 1000
schedule_last_fired: dict[str, datetime] = {}

# --- Agent session management ---

MASTER_AGENT_NAME = "axi-master"
MAX_AGENTS = 20
IDLE_REMINDER_THRESHOLDS = [timedelta(minutes=30), timedelta(hours=3), timedelta(hours=48)]
QUERY_TIMEOUT = 600  # 10 minutes
INTERRUPT_TIMEOUT = 15  # seconds to wait after interrupt


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
    last_response: str = ""
    session_id: str | None = None


agents: dict[str, AgentSession] = {}
active_agent: str = MASTER_AGENT_NAME
auto_switch_enabled: bool = True
visibility_mode: str = "active"  # "active" or "all"


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


def append_server_log(message) -> None:
    """Append a server message to the rolling JSONL log."""
    entry = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "guild": getattr(message.guild, "name", "unknown"),
        "guild_id": getattr(message.guild, "id", 0),
        "channel": getattr(message.channel, "name", "unknown"),
        "channel_id": message.channel.id,
        "author": str(message.author),
        "author_id": message.author.id,
        "content": message.content[:500],
    })
    try:
        with open(SERVER_LOG_PATH, "a") as f:
            f.write(entry + "\n")

        # Trim to max lines
        with open(SERVER_LOG_PATH) as f:
            lines = f.readlines()
        if len(lines) > SERVER_LOG_MAX_LINES:
            with open(SERVER_LOG_PATH, "w") as f:
                f.writelines(lines[-SERVER_LOG_MAX_LINES:])
    except OSError:
        log.exception("Failed to write server log")


SYSTEM_PROMPT = """\
You are Axi, a personal assistant communicating over Discord DMs. \
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
For recurring events, use a "schedule" field with a cron expression (cron times are in the SCHEDULE_TIMEZONE configured in .env, which handles DST automatically). \
Optional fields: "reset_context" (boolean, resets conversation before firing), \
"agent" (boolean, spawns a new agent session instead of routing through you — use this for heavy tasks to keep your context clean), \
"cwd" (string, working directory for the agent — required when "agent" is true). \
Example one-off: {"name": "reminder", "prompt": "Say hello in 10 languages", "at": "2026-02-21T03:00:00+00:00"}. \
Example recurring: {"name": "daily-standup", "prompt": "Ask me what I'm working on today", "schedule": "0 9 * * *"}. \
Example agent schedule: {"name": "weekly-cleanup", "prompt": "Clean up unused imports", "schedule": "0 9 * * 1", "cwd": "/home/pride/coding-projects/my-app", "agent": true}. \
To restart yourself, create the file \
.restart_requested in your project directory (e.g., `touch .restart_requested`). \
The system will automatically restart within 30 seconds. \
Only restart when the user explicitly asks you to — do not restart after every self-edit.

## Agent Spawning

You can spawn independent Claude Code agent sessions to work on tasks autonomously. \
To spawn an agent, create a file called `.spawn_agent` in your project directory \
(~/coding-projects/personal-assistant/) with the following JSON format:

{"name": "agent-name", "cwd": "/absolute/path/to/project", "prompt": "Initial instructions for the agent"}

Rules for spawning agents:
- "name" must be unique, short, and descriptive (e.g. "feature-auth", "fix-bug-123"). No spaces.
- "cwd" must be the absolute path to the project directory the agent should work in.
- "prompt" is the initial task description. Be specific and detailed since the agent works independently.
- The spawned agent is a standard Claude Code session — it does NOT have your Axi personality or custom instructions.
- The system picks up the file within 30 seconds and spawns the session automatically.
- The user will be notified when the agent starts and when it finishes its initial task.
- The user can interact with spawned agents using /switch-agent, view them with /list-agents, and terminate them with /kill-agent.
- You cannot spawn an agent named "axi-master" — that is reserved for you.
- Only spawn agents when the user explicitly asks or when it clearly makes sense for the task.

When the system notifies you about idle agent sessions, remind the user about them \
and suggest they either switch to the agent to continue work or kill it to free resources.

## Server Message Log

You have read-only access to Discord server messages via server_log.jsonl in your working directory. \
This is a rolling JSONL log (max 1000 entries) of recent messages from allowed servers. \
Each line is a JSON object with: ts, guild, channel, author, content. \
When the user asks about server activity, read this file to answer their questions. \
You do NOT respond in server channels — you only observe and report via DMs.\
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
        permission_mode="bypassPermissions",
        cwd=cwd,
        system_prompt=system_prompt,
        include_partial_messages=True,
        stderr=make_stderr_callback(session),
        resume=resume,
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
            # anyio cancel scopes must be exited from the same task that entered
            # them. When end_session is called from a different task (e.g. a
            # Discord command handler) than the one that called __aenter__, this
            # error is expected and the session resources are already released.
            if "cancel scope" in str(e):
                log.debug("Claude session '%s' cross-task cleanup (expected): %s", name, e)
            else:
                raise
        session.client = None
    agents.pop(name, None)
    log.info("Claude session '%s' ended", name)


async def reset_session(name: str, cwd: str | None = None) -> AgentSession:
    """Reset a named session. Preserves its system prompt."""
    session = agents.get(name)
    old_cwd = session.cwd if session else DEFAULT_CWD
    old_prompt = session.system_prompt if session else SYSTEM_PROMPT
    await end_session(name)
    return await start_session(name, cwd or old_cwd, old_prompt)


def get_active_session() -> AgentSession | None:
    """Get the currently active agent session."""
    return agents.get(active_agent)


def get_master_session() -> AgentSession | None:
    """Get the axi-master session."""
    return agents.get(MASTER_AGENT_NAME)


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
            await channel.send(chunk)


async def send_system(channel, text: str) -> None:
    """Send a system-prefixed message."""
    await send_long(channel, f"*System:* {text}")


# --- Streaming response ---

async def _receive_response_safe(client: ClaudeSDKClient):
    """Wrapper around receive_response() that skips unknown message types.

    The SDK's receive_messages() calls parse_message() internally and raises
    MessageParseError on unrecognised types (e.g. rate_limit_event), which
    terminates the async generator.  We bypass that by reading raw dicts from
    the underlying query object and parsing them ourselves with error handling.
    """
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
    Uses the public receive_response() API. Flushes the text buffer at each
    assistant turn boundary so intermediate messages appear immediately."""
    text_buffer = ""
    full_response_parts: list[str] = []
    prefix = f"*{session.name}:* "

    async def flush_text(text: str) -> None:
        if not text.strip():
            return
        full_response_parts.append(text.lstrip())
        text = prefix + text.lstrip()
        await send_long(channel, text)

    async with channel.typing():
        async for msg in _receive_response_safe(session.client):
            # Drain and send any stderr messages first
            for stderr_msg in drain_stderr(session):
                stderr_text = stderr_msg.strip()
                if stderr_text:
                    for part in split_message(f"```\n{stderr_text}\n```"):
                        await channel.send(part)

            if isinstance(msg, StreamEvent):
                # Real-time text deltas — accumulate in buffer
                event = msg.event
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_buffer += delta.get("text", "")

            elif isinstance(msg, AssistantMessage):
                # End of an assistant turn — flush buffer as a separate message
                await flush_text(text_buffer)
                text_buffer = ""

            elif isinstance(msg, ResultMessage):
                session.session_id = getattr(msg, "session_id", None)
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

    # Store full response for /last-response
    session.last_response = "\n".join(full_response_parts)

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
                    session.session_id = getattr(msg, "session_id", None)
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
    await end_session(old_name)

    if old_session_id:
        await start_session(old_name, old_cwd, old_prompt, resume=old_session_id)
        await send_system(channel, f"Agent **{old_name}** timed out and was recovered. Context preserved.")
    else:
        await start_session(old_name, old_cwd, old_prompt)
        await send_system(channel, f"Agent **{old_name}** timed out and was reset. Context lost.")


# --- Agent spawning ---


async def reclaim_agent_name(name: str, channels: list) -> None:
    """If an agent with *name* already exists, kill it silently to free the name."""
    global active_agent
    if name not in agents:
        return
    log.info("Reclaiming agent name '%s' — terminating existing session", name)
    await end_session(name)
    if active_agent == name:
        active_agent = MASTER_AGENT_NAME
    for channel in channels:
        await send_system(channel, f"Recycled previous **{name}** session for new scheduled run.")


async def spawn_agent(name: str, cwd: str, initial_prompt: str, channels: list) -> None:
    """Spawn a new agent session and run its initial prompt in the background."""
    global active_agent
    for channel in channels:
        await send_system(channel, f"Spawning agent **{name}** in `{cwd}`...")

    session = await start_session(name, cwd, system_prompt=None)

    auto_switched = False
    if auto_switch_enabled:
        active_agent = name
        auto_switched = True

    if not initial_prompt:
        if auto_switched:
            for channel in channels:
                await send_system(channel, f"Agent **{name}** is ready and now active.")
        else:
            for channel in channels:
                await send_system(channel, f"Agent **{name}** is ready. Use `/switch-agent` to interact.")
        return

    asyncio.create_task(_run_initial_prompt(session, initial_prompt, channels, auto_switched))


async def _run_initial_prompt(session: AgentSession, prompt: str, channels: list, auto_switched: bool = False) -> None:
    """Run the initial prompt for a spawned agent. Notifies when done."""
    try:
        timed_out = False
        async with session.query_lock:
            session.last_activity = datetime.now(timezone.utc)
            drain_stderr(session)
            try:
                async with asyncio.timeout(QUERY_TIMEOUT):
                    await session.client.query(prompt)

                    if visibility_mode == "all" and channels:
                        await stream_response_to_channel(session, channels[0], show_awaiting_input=False)
                    else:
                        # Consume response silently (don't stream to Discord)
                        async for msg in _receive_response_safe(session.client):
                            if isinstance(msg, ResultMessage):
                                session.session_id = getattr(msg, "session_id", None)
                                break

                    session.last_activity = datetime.now(timezone.utc)
            except TimeoutError:
                timed_out = True
                if channels:
                    await _handle_query_timeout(session, channels[0])

        if not timed_out:
            for channel in channels:
                if auto_switched:
                    await send_system(
                        channel,
                        f"Agent **{session.name}** finished initial task and is now active.",
                    )
                else:
                    await send_system(
                        channel,
                        f"Agent **{session.name}** finished initial task. "
                        f"Use `/switch-agent` to interact with it.",
                    )
    except Exception:
        log.exception("Error running initial prompt for agent '%s'", session.name)
        for channel in channels:
            await send_system(channel, f"Agent **{session.name}** encountered an error during initial task.")


# --- Message handler ---

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Guild messages — log passively, never respond
    if message.guild:
        if message.guild.id in ALLOWED_GUILD_IDS:
            append_server_log(message)
        return

    # DM messages — route to the active agent
    if message.channel.type != ChannelType.private:
        return
    if message.author.id not in ALLOWED_USER_IDS:
        return

    session = get_active_session()
    if session is None or session.client is None:
        await send_system(message.channel, "Claude session not ready yet. Please wait.")
        return

    if session.query_lock.locked():
        await send_system(
            message.channel,
            f"Agent **{session.name}** is busy. Please wait or `/switch-agent` to another.",
        )
        return

    async with session.query_lock:
        session.last_activity = datetime.now(timezone.utc)
        session.last_idle_notified = None
        session.idle_reminder_count = 0
        drain_stderr(session)
        try:
            async with asyncio.timeout(QUERY_TIMEOUT):
                await session.client.query(message.content)
                await stream_response_to_channel(session, message.channel)
        except TimeoutError:
            await _handle_query_timeout(session, message.channel)

    await bot.process_commands(message)


# --- Scheduler loop ---

@tasks.loop(seconds=30)
async def check_schedules():
    # Restart signal check
    if os.path.exists(RESTART_SIGNAL_PATH):
        os.remove(RESTART_SIGNAL_PATH)
        log.info("Restart signal detected, exiting with code 42")
        await bot.close()
        os._exit(42)

    # Spawn signal check
    if os.path.exists(SPAWN_SIGNAL_PATH):
        try:
            with open(SPAWN_SIGNAL_PATH) as f:
                spawn_data = json.load(f)
            os.remove(SPAWN_SIGNAL_PATH)

            agent_name = spawn_data.get("name", "").strip()
            agent_cwd = spawn_data.get("cwd", DEFAULT_CWD)
            agent_prompt = spawn_data.get("prompt", "")

            # Resolve DM channels for notifications
            spawn_channels = []
            for uid in ALLOWED_USER_IDS:
                try:
                    user = await bot.fetch_user(uid)
                    spawn_channels.append(await user.create_dm())
                except Exception:
                    continue

            if not agent_name:
                log.warning("Spawn signal missing 'name' field, ignoring")
            elif agent_name == MASTER_AGENT_NAME:
                log.warning("Cannot spawn agent with reserved name '%s'", MASTER_AGENT_NAME)
                for ch in spawn_channels:
                    await send_system(ch, f"Cannot spawn agent with reserved name **{MASTER_AGENT_NAME}**.")
            elif agent_name in agents:
                log.warning("Agent '%s' already exists, ignoring spawn signal", agent_name)
                for ch in spawn_channels:
                    await send_system(ch, f"Agent **{agent_name}** already exists.")
            elif len(agents) >= MAX_AGENTS:
                log.warning("Max agents (%d) reached, ignoring spawn signal", MAX_AGENTS)
                for ch in spawn_channels:
                    await send_system(ch, f"Maximum number of agents ({MAX_AGENTS}) reached. Kill an agent first.")
            else:
                log.info("Spawning agent '%s' (cwd=%s)", agent_name, agent_cwd)
                await spawn_agent(agent_name, agent_cwd, agent_prompt, spawn_channels)
        except Exception:
            log.exception("Error processing spawn signal")
            if os.path.exists(SPAWN_SIGNAL_PATH):
                os.remove(SPAWN_SIGNAL_PATH)

    master = get_master_session()
    if master is None or master.client is None:
        return

    prune_history()

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now(SCHEDULE_TIMEZONE)
    entries = load_schedules()
    entries_modified = False

    # Resolve DM channels for all approved members
    channels = []
    for uid in ALLOWED_USER_IDS:
        try:
            user = await bot.fetch_user(uid)
            channels.append(await user.create_dm())
        except Exception:
            continue

    if not channels:
        log.warning("Could not resolve any DM channels for scheduled events")
        return

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

                    log.info("Firing recurring event: %s", name)
                    await reclaim_agent_name(name, channels)
                    agent_cwd = entry.get("cwd", DEFAULT_CWD)
                    if len(agents) >= MAX_AGENTS:
                        log.warning("Max agents reached, skipping event %s", name)
                        for channel in channels:
                            await send_system(channel, f"Scheduled event **{name}** skipped — max agents ({MAX_AGENTS}) reached.")
                    else:
                        await spawn_agent(name, agent_cwd, entry["prompt"], channels)

            elif "at" in entry:
                # One-off event
                fire_at = datetime.fromisoformat(entry["at"])

                if fire_at <= now_utc:
                    log.info("Firing one-off event: %s", name)
                    await reclaim_agent_name(name, channels)
                    agent_cwd = entry.get("cwd", DEFAULT_CWD)
                    if len(agents) >= MAX_AGENTS:
                        log.warning("Max agents reached, skipping event %s", name)
                        for channel in channels:
                            await send_system(channel, f"Scheduled event **{name}** skipped — max agents ({MAX_AGENTS}) reached.")
                    else:
                        await spawn_agent(name, agent_cwd, entry["prompt"], channels)

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
        for channel in channels:
            await send_system(
                channel,
                f"Agent **{agent_name}** has been idle for {idle_minutes} minutes "
                f"(cwd: `{session.cwd}`). Use `/kill-agent` to terminate or "
                f"`/switch-agent` to resume.",
            )
        session.idle_reminder_count += 1
        session.last_idle_notified = datetime.now(timezone.utc)


@check_schedules.before_loop
async def before_check_schedules():
    await bot.wait_until_ready()


# --- Slash commands ---

async def agent_autocomplete(interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete callback for agent name parameters."""
    return [
        app_commands.Choice(name=name, value=name)
        for name in agents.keys()
        if current.lower() in name.lower()
    ][:25]


async def killable_agent_autocomplete(interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete callback excluding axi-master."""
    return [
        app_commands.Choice(name=name, value=name)
        for name in agents.keys()
        if name != MASTER_AGENT_NAME and current.lower() in name.lower()
    ][:25]


@bot.tree.command(name="switch-agent", description="Switch the active agent for chat.")
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def switch_agent(interaction, agent_name: str):
    global active_agent
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    if agent_name not in agents:
        await interaction.response.send_message(
            f"Agent **{agent_name}** not found. Use `/list-agents` to see available agents.",
            ephemeral=True,
        )
        return

    active_agent = agent_name
    session = agents[agent_name]
    await interaction.response.send_message(
        f"*System:* Switched to agent **{agent_name}** (cwd: `{session.cwd}`)"
    )


@bot.tree.command(name="list-agents", description="List all active agent sessions.")
async def list_agents(interaction):
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
        marker = " **(active)**" if name == active_agent else ""
        busy = " [busy]" if session.query_lock.locked() else ""
        protected = " [protected]" if name == MASTER_AGENT_NAME else ""
        lines.append(
            f"- **{name}**{marker}{busy}{protected} | cwd: `{session.cwd}` | idle: {idle_minutes}m"
        )

    await interaction.response.send_message("*System:* **Agent Sessions:**\n" + "\n".join(lines))


@bot.tree.command(name="kill-agent", description="Terminate an agent session.")
@app_commands.autocomplete(agent_name=killable_agent_autocomplete)
async def kill_agent(interaction, agent_name: str):
    global active_agent
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
    await end_session(agent_name)

    if active_agent == agent_name:
        active_agent = MASTER_AGENT_NAME

    await interaction.followup.send(f"*System:* Agent **{agent_name}** terminated.")


@bot.tree.command(name="reset-context", description="Reset the active agent's context. Optionally set a new working directory.")
async def reset_context(interaction, working_dir: str | None = None):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    await interaction.response.defer()
    session = await reset_session(active_agent, cwd=working_dir)
    await interaction.followup.send(
        f"*System:* Context reset for **{active_agent}**. Working directory: `{session.cwd}`"
    )


@bot.tree.command(name="last-response", description="Re-render the active agent's last response.")
async def last_response(interaction):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    session = get_active_session()
    if session is None:
        await interaction.response.send_message("No active session.", ephemeral=True)
        return

    if not session.last_response:
        await interaction.response.send_message(
            f"No previous response from **{session.name}**.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    prefix = f"*{session.name} (last response):*\n"
    await send_long(interaction.channel, prefix + session.last_response)
    await interaction.followup.send("Done.", ephemeral=True)


@bot.tree.command(name="config", description="View or update bot settings (auto_switch, visibility).")
@app_commands.choices(
    auto_switch=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ],
    visibility=[
        app_commands.Choice(name="active", value="active"),
        app_commands.Choice(name="all", value="all"),
    ],
)
async def config_cmd(
    interaction,
    auto_switch: app_commands.Choice[str] | None = None,
    visibility: app_commands.Choice[str] | None = None,
):
    global auto_switch_enabled, visibility_mode
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    # No args → show current settings
    if auto_switch is None and visibility is None:
        await interaction.response.send_message(
            f"*System:* **Current config:**\n"
            f"- auto_switch: **{'on' if auto_switch_enabled else 'off'}**\n"
            f"- visibility: **{visibility_mode}**"
        )
        return

    changes = []
    if auto_switch is not None:
        auto_switch_enabled = auto_switch.value == "on"
        changes.append(f"auto_switch → **{auto_switch.value}**")
    if visibility is not None:
        visibility_mode = visibility.value
        changes.append(f"visibility → **{visibility.value}**")

    await interaction.response.send_message(
        f"*System:* Config updated: {', '.join(changes)}"
    )


# --- Startup ---

@bot.event
async def on_ready():
    log.info("Bot ready as %s", bot.user)
    await start_session(MASTER_AGENT_NAME, DEFAULT_CWD, system_prompt=SYSTEM_PROMPT)
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

    for uid in ALLOWED_USER_IDS:
        try:
            user = await bot.fetch_user(uid)
            dm = await user.create_dm()
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
                await dm.send("\n".join(msg_lines))
            else:
                await dm.send("*System:* Axi restarted.")
            log.info("Sent restart notification to user %s", uid)
        except Exception:
            log.exception("Failed to send restart notification to user %s", uid)
        await asyncio.sleep(1)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
