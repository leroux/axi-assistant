import os
import json
import asyncio
import threading
import logging
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from discord import Intents
from discord.ext.commands import Bot
from discord.ext import tasks
from discord.enums import ChannelType
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
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

# --- Claude session state ---

client: ClaudeSDKClient | None = None
current_cwd: str = DEFAULT_CWD

# Thread-safe stderr buffer
stderr_lock = threading.Lock()
stderr_buffer: list[str] = []

# --- Scheduler state ---

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEDULES_PATH = os.path.join(BOT_DIR, "schedules.json")
HISTORY_PATH = os.path.join(BOT_DIR, "schedule_history.json")
RESTART_SIGNAL_PATH = os.path.join(BOT_DIR, ".restart_requested")
schedule_last_fired: dict[str, datetime] = {}
query_lock = asyncio.Lock()


def stderr_callback(text: str) -> None:
    with stderr_lock:
        stderr_buffer.append(text)


def drain_stderr() -> list[str]:
    with stderr_lock:
        msgs = list(stderr_buffer)
        stderr_buffer.clear()
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
Optional fields: "reset_context" (boolean, resets conversation before firing). \
Example one-off: {"name": "reminder", "prompt": "Say hello in 10 languages", "at": "2026-02-21T03:00:00+00:00"}. \
Example recurring: {"name": "daily-standup", "prompt": "Ask me what I'm working on today", "schedule": "0 9 * * *"}. \
To restart yourself, create the file \
.restart_requested in your project directory (e.g., `touch .restart_requested`). \
The system will automatically restart within 30 seconds. \
Only restart when the user explicitly asks you to — do not restart after every self-edit.\
"""


async def start_session(cwd: str) -> None:
    global client, current_cwd
    current_cwd = cwd
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        cwd=cwd,
        system_prompt=SYSTEM_PROMPT,
        include_partial_messages=True,
        stderr=stderr_callback,
    )
    client = ClaudeSDKClient(options=options)
    await client.__aenter__()
    log.info("Claude session started (cwd=%s)", cwd)


async def end_session() -> None:
    global client
    if client is not None:
        try:
            await asyncio.wait_for(client.__aexit__(None, None, None), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            log.warning("Claude session shutdown timed out or was cancelled")
        client = None
    log.info("Claude session ended")


async def reset_session(cwd: str | None = None) -> None:
    await end_session()
    await start_session(cwd or current_cwd)


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


# --- Streaming response ---

async def stream_response_to_channel(channel) -> None:
    """Stream Claude's response to a Discord channel.
    Iterates client._query.receive_messages(), extracts text_delta,
    drains stderr, and flushes at 1800 chars."""
    text_buffer = ""

    async with channel.typing():
        async for data in client._query.receive_messages():
            msg_type = data.get("type")

            # Drain and send any stderr messages first
            for stderr_msg in drain_stderr():
                stderr_text = stderr_msg.strip()
                if stderr_text:
                    for part in split_message(f"```\n{stderr_text}\n```"):
                        await channel.send(part)

            # Extract text from stream events
            if msg_type == "stream_event":
                event = data.get("event", {})
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_buffer += delta.get("text", "")
            elif msg_type == "result":
                break

            # When buffer is large enough, flush it
            if len(text_buffer) >= 1800:
                split_at = text_buffer.rfind("\n", 0, 1800)
                if split_at == -1:
                    split_at = 1800
                to_send = text_buffer[:split_at]
                text_buffer = text_buffer[split_at:].lstrip("\n")
                if to_send.strip():
                    await send_long(channel, to_send)

    # Flush any remaining stderr
    for stderr_msg in drain_stderr():
        stderr_text = stderr_msg.strip()
        if stderr_text:
            for part in split_message(f"```\n{stderr_text}\n```"):
                await channel.send(part)

    # Flush remaining text buffer
    if text_buffer.strip():
        await send_long(channel, text_buffer)


# --- Scheduled event runner ---

async def run_scheduled_event(entry: dict, channel) -> None:
    """Run a scheduled event: optionally reset context, send header, query Claude, stream response."""
    if entry.get("reset_context", False):
        await reset_session()

    await channel.send(f"**[Scheduled: {entry['name']}]**")
    drain_stderr()
    await client.query(entry["prompt"])
    await stream_response_to_channel(channel)


# --- DM message handler ---

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.type != ChannelType.private:
        return
    if message.author.id not in ALLOWED_USER_IDS:
        return
    if client is None:
        await message.channel.send("Claude session not ready yet. Please wait.")
        return

    if query_lock.locked():
        await message.channel.send("Currently running a scheduled task. Please wait.")
        return

    async with query_lock:
        drain_stderr()
        await client.query(message.content)
        await stream_response_to_channel(message.channel)

    await bot.process_commands(message)


# --- Scheduler loop ---

@tasks.loop(seconds=30)
async def check_schedules():
    if os.path.exists(RESTART_SIGNAL_PATH):
        os.remove(RESTART_SIGNAL_PATH)
        log.info("Restart signal detected, exiting with code 42")
        await bot.close()
        os._exit(42)

    if client is None:
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
                    if query_lock.locked():
                        log.info("Skipping scheduled event %s — query in progress", name)
                        continue

                    async with query_lock:
                        log.info("Firing recurring event: %s", name)
                        schedule_last_fired[name] = last_occurrence
                        for channel in channels:
                            await run_scheduled_event(entry, channel)

            elif "at" in entry:
                # One-off event
                fire_at = datetime.fromisoformat(entry["at"])

                if fire_at <= now:
                    if query_lock.locked():
                        log.info("Skipping one-off event %s — query in progress", name)
                        continue

                    async with query_lock:
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


@check_schedules.before_loop
async def before_check_schedules():
    await bot.wait_until_ready()


# --- Slash commands ---

@bot.tree.command(name="reset-context", description="Reset Claude context. Optionally set a new working directory.")
async def reset_context(interaction, working_dir: str | None = None):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    await interaction.response.defer()
    await reset_session(cwd=working_dir)
    new_cwd = working_dir or current_cwd
    await interaction.followup.send(f"Context reset. Working directory: `{new_cwd}`")


# --- Startup ---

@bot.event
async def on_ready():
    log.info("Bot ready as %s", bot.user)
    await start_session(DEFAULT_CWD)
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
            await dm.send("Axi restarted.")
        except Exception:
            continue


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
