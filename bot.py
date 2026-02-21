import os
import json
import asyncio
import threading
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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

# --- Discord bot setup ---

intents = Intents(dm_messages=True, message_content=True)
bot = Bot(command_prefix="!", intents=intents)

# --- Scheduler state ---

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEDULES_PATH = os.path.join(BOT_DIR, "schedules.json")
HISTORY_PATH = os.path.join(BOT_DIR, "schedule_history.json")
RESTART_SIGNAL_PATH = os.path.join(BOT_DIR, ".restart_requested")
SPAWN_SIGNAL_PATH = os.path.join(BOT_DIR, ".spawn_agent")
schedule_last_fired: dict[str, datetime] = {}

# --- Agent session management ---

MASTER_AGENT_NAME = "axi-master"
MAX_AGENTS = 20
IDLE_REMINDER_THRESHOLDS = [timedelta(minutes=30), timedelta(hours=3), timedelta(hours=48)]


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


agents: dict[str, AgentSession] = {}
active_agent: str = MASTER_AGENT_NAME


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
For recurring events, use a "schedule" field with a cron expression. \
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
and suggest they either switch to the agent to continue work or kill it to free resources.\
"""


# --- Session lifecycle ---

async def start_session(name: str, cwd: str, system_prompt: str | None = None) -> AgentSession:
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


async def stream_response_to_channel(session: AgentSession, channel) -> None:
    """Stream Claude's response from a specific agent session to a Discord channel.
    Uses the public receive_response() API. Flushes the text buffer at each
    assistant turn boundary so intermediate messages appear immediately."""
    text_buffer = ""
    first_flush = True
    prefix = f"*{session.name}:* "

    async def flush_text(text: str) -> None:
        nonlocal first_flush
        if not text.strip():
            return
        if first_flush:
            text = prefix + text.lstrip()
            first_flush = False
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
                # receive_response() stops after this automatically
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
    await send_system(channel, "Bot has finished responding and is awaiting input.")


# --- Scheduled event runner ---

async def run_scheduled_event(entry: dict, channel) -> None:
    """Run a scheduled event using the master session."""
    master = get_master_session()
    if master is None or master.client is None:
        return

    if entry.get("reset_context", False):
        await reset_session(MASTER_AGENT_NAME)
        master = get_master_session()

    await send_system(channel, f"**[Scheduled: {entry['name']}]**")
    drain_stderr(master)
    await master.client.query(entry["prompt"])
    await stream_response_to_channel(master, channel)


# --- Agent spawning ---

async def spawn_agent(name: str, cwd: str, initial_prompt: str, channels: list) -> None:
    """Spawn a new agent session and run its initial prompt in the background."""
    for channel in channels:
        await send_system(channel, f"Spawning agent **{name}** in `{cwd}`...")

    session = await start_session(name, cwd, system_prompt=None)

    if not initial_prompt:
        for channel in channels:
            await send_system(channel, f"Agent **{name}** is ready. Use `/switch-agent` to interact.")
        return

    asyncio.create_task(_run_initial_prompt(session, initial_prompt, channels))


async def _run_initial_prompt(session: AgentSession, prompt: str, channels: list) -> None:
    """Run the initial prompt for a spawned agent. Notifies when done."""
    try:
        async with session.query_lock:
            session.last_activity = datetime.now(timezone.utc)
            drain_stderr(session)
            await session.client.query(prompt)

            # Consume response silently (don't stream to Discord)
            async for msg in _receive_response_safe(session.client):
                if isinstance(msg, ResultMessage):
                    break

            session.last_activity = datetime.now(timezone.utc)

        for channel in channels:
            await send_system(
                channel,
                f"Agent **{session.name}** finished initial task. "
                f"Use `/switch-agent` to interact with it.",
            )
    except Exception:
        log.exception("Error running initial prompt for agent '%s'", session.name)
        for channel in channels:
            await send_system(channel, f"Agent **{session.name}** encountered an error during initial task.")


# --- DM message handler ---

@bot.event
async def on_message(message):
    if message.author.bot:
        return
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
        await session.client.query(message.content)
        await stream_response_to_channel(session, message.channel)

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

    now = datetime.now(timezone.utc)
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
                # Recurring event
                cron_expr = entry["schedule"]
                if not croniter.is_valid(cron_expr):
                    log.warning("Invalid cron expression for %s: %s", name, cron_expr)
                    continue

                last_occurrence = croniter(cron_expr, now).get_prev(datetime)

                if name not in schedule_last_fired:
                    # First time seeing this event — seed it to prevent immediate fire
                    schedule_last_fired[name] = last_occurrence
                    continue

                if last_occurrence > schedule_last_fired[name]:
                    schedule_last_fired[name] = last_occurrence

                    if entry.get("agent", False):
                        # Spawn a new agent instead of routing through master
                        log.info("Firing recurring agent event: %s", name)
                        agent_name = f"{name}-{last_occurrence.strftime('%Y%m%d-%H%M')}"
                        agent_cwd = entry.get("cwd", DEFAULT_CWD)
                        if agent_name in agents:
                            log.warning("Agent '%s' already exists, skipping", agent_name)
                        elif len(agents) >= MAX_AGENTS:
                            log.warning("Max agents reached, skipping agent event %s", name)
                            for channel in channels:
                                await send_system(channel, f"Scheduled agent **{name}** skipped — max agents ({MAX_AGENTS}) reached.")
                        else:
                            await spawn_agent(agent_name, agent_cwd, entry["prompt"], channels)
                        continue

                    if master.query_lock.locked():
                        log.info("Skipping scheduled event %s — query in progress", name)
                        continue

                    async with master.query_lock:
                        log.info("Firing recurring event: %s", name)
                        for channel in channels:
                            await run_scheduled_event(entry, channel)

            elif "at" in entry:
                # One-off event
                fire_at = datetime.fromisoformat(entry["at"])

                if fire_at <= now:
                    if entry.get("agent", False):
                        # Spawn a new agent instead of routing through master
                        log.info("Firing one-off agent event: %s", name)
                        agent_name = f"{name}-{now.strftime('%Y%m%d-%H%M')}"
                        agent_cwd = entry.get("cwd", DEFAULT_CWD)
                        if agent_name in agents:
                            log.warning("Agent '%s' already exists, skipping", agent_name)
                        elif len(agents) >= MAX_AGENTS:
                            log.warning("Max agents reached, skipping agent event %s", name)
                            for channel in channels:
                                await send_system(channel, f"Scheduled agent **{name}** skipped — max agents ({MAX_AGENTS}) reached.")
                        else:
                            await spawn_agent(agent_name, agent_cwd, entry["prompt"], channels)
                    else:
                        if master.query_lock.locked():
                            log.info("Skipping one-off event %s — query in progress", name)
                            continue

                        async with master.query_lock:
                            log.info("Firing one-off event: %s", name)
                            for channel in channels:
                                await run_scheduled_event(entry, channel)

                    # Remove from schedules and add to history
                    entries.remove(entry)
                    entries_modified = True
                    append_history(entry, now)

        except Exception:
            log.exception("Error processing scheduled event %s", name)

    if entries_modified:
        save_schedules(entries)

    # --- Idle agent detection ---
    if master.client and not master.query_lock.locked():
        idle_agents = []
        for agent_name, session in agents.items():
            if agent_name == MASTER_AGENT_NAME:
                continue
            if session.idle_reminder_count >= len(IDLE_REMINDER_THRESHOLDS):
                continue  # All reminders already sent

            # Cumulative threshold: sum of thresholds up to current reminder count
            cumulative = sum(IDLE_REMINDER_THRESHOLDS[:session.idle_reminder_count + 1], timedelta())
            idle_duration = now - session.last_activity

            if idle_duration > cumulative:
                idle_minutes = int(idle_duration.total_seconds() / 60)
                idle_agents.append((session, f"{agent_name} (idle {idle_minutes}m, cwd: {session.cwd})"))

        if idle_agents and channels:
            try:
                async with master.query_lock:
                    master.last_activity = datetime.now(timezone.utc)
                    idle_list = ", ".join(desc for _, desc in idle_agents)
                    reminder_prompt = (
                        f"The following spawned agent sessions have been idle: {idle_list}. "
                        f"Remind the user about these idle agents. They can use /kill-agent to terminate "
                        f"them or /switch-agent to resume working with them."
                    )
                    drain_stderr(master)
                    await master.client.query(reminder_prompt)
                    for channel in channels:
                        await stream_response_to_channel(master, channel)

                # Update idle notification state
                for session, _ in idle_agents:
                    session.idle_reminder_count += 1
                    session.last_idle_notified = datetime.now(timezone.utc)
            except Exception:
                log.exception("Error sending idle agent reminders")


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


# --- Startup ---

@bot.event
async def on_ready():
    log.info("Bot ready as %s", bot.user)
    await start_session(MASTER_AGENT_NAME, DEFAULT_CWD, system_prompt=SYSTEM_PROMPT)
    await bot.tree.sync()
    log.info("Slash commands synced")

    # Pre-seed schedule_last_fired for recurring events without catch_up
    now = datetime.now(timezone.utc)
    for entry in load_schedules():
        name = entry.get("name")
        cron_expr = entry.get("schedule")
        if name and cron_expr and croniter.is_valid(cron_expr):
            if not entry.get("catch_up", False):
                schedule_last_fired[name] = croniter(cron_expr, now).get_prev(datetime)

    check_schedules.start()
    log.info("Schedule checker started")

    for uid in ALLOWED_USER_IDS:
        try:
            user = await bot.fetch_user(uid)
            dm = await user.create_dm()
            await dm.send("*System:* Axi restarted.")
            log.info("Sent restart notification to user %s", uid)
        except Exception:
            log.exception("Failed to send restart notification to user %s", uid)
        await asyncio.sleep(1)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
