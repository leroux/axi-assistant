"""Discord bot orchestrator — event handlers, slash commands, and scheduler.

Thin layer that wires Discord events to agent lifecycle functions in agents.py.
All agent state and operations live in agents.py; MCP tools in tools.py.
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
from croniter import croniter
from discord import TextChannel, app_commands
from discord.enums import ChannelType
from discord.ext import tasks
from discord.ext.commands import Bot
from opentelemetry import trace

from axi import agents, channels, config, scheduler, tools
from axi.axi_types import ActivityState, AgentSession, discord_state, tool_display
from axi.log_context import set_agent_context, set_trigger
from axi.prompts import (
    MASTER_SYSTEM_PROMPT,
    compute_prompt_hash,
    make_spawned_agent_system_prompt,
)
from axi.schedule_tools import (
    append_history,
    load_schedules,
    make_schedule_mcp_server,
    prune_history,
    save_schedules,
    schedule_key,
    schedules_lock,
)
from axi.shutdown import kill_supervisor
from axi.tracing import init_tracing

log = logging.getLogger("axi")
_tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# Debug: dump all thread stacks on SIGUSR1, asyncio tasks on SIGUSR2
# ---------------------------------------------------------------------------

faulthandler.enable()  # dump traceback on SIGSEGV/SIGABRT/etc.


def _dump_stacks(sig: int, frame: Any) -> None:
    """Dump all thread stack traces to stderr on SIGUSR1."""
    output = [f"\n{'=' * 60}", f"STACK DUMP (signal {sig})", f"{'=' * 60}"]
    for tid, stack in sys._current_frames().items():  # pyright: ignore[reportPrivateUsage]
        name = next((t.name for t in threading.enumerate() if t.ident == tid), f"thread-{tid}")
        output.append(f"\n--- {name} (tid={tid}) ---")
        output.append("".join(traceback.format_stack(stack)))
    dump = "\n".join(output)
    sys.stderr.write(dump)
    # Also write to a file for easy retrieval
    try:
        with open(os.path.join(config.LOG_DIR, "stack-dump.txt"), "w") as f:
            f.write(dump)
    except Exception:
        pass


def _dump_asyncio_tasks(sig: int, frame: Any) -> None:
    """Dump all asyncio tasks to stderr on SIGUSR2."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        sys.stderr.write("No running asyncio loop\n")
        return
    output = [f"\n{'=' * 60}", "ASYNCIO TASK DUMP", f"{'=' * 60}"]
    for task in asyncio.all_tasks(loop):
        output.append(f"\n--- {task.get_name()} (done={task.done()}) ---")
        # Capture stack to string
        import io
        buf = io.StringIO()
        task.print_stack(file=buf)
        output.append(buf.getvalue())
    dump = "\n".join(output)
    sys.stderr.write(dump)
    try:
        with open(os.path.join(config.LOG_DIR, "asyncio-dump.txt"), "w") as f:
            f.write(dump)
    except Exception:
        pass


signal.signal(signal.SIGUSR1, _dump_stacks)
signal.signal(signal.SIGUSR2, _dump_asyncio_tasks)


# ---------------------------------------------------------------------------
# Bot creation
# ---------------------------------------------------------------------------

bot = Bot(command_prefix="!", intents=config.intents)


@bot.tree.interaction_check  # type: ignore[arg-type]
async def global_auth_check(interaction: discord.Interaction[Any]) -> bool:
    """Reject all slash commands from non-authorized users."""
    if interaction.user.id not in config.ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Bot-local state
# ---------------------------------------------------------------------------

_on_ready_fired = False
_startup_complete = False
_bot_start_time: datetime | None = None
_seen_message_ids: OrderedDict[int, None] = OrderedDict()  # dedup guard for Discord duplicate delivery
_DEDUP_CAPACITY = 500


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


@bot.event
async def on_error(event_method: str, *args: Any, **kwargs: Any) -> None:
    """Route unhandled event-handler exceptions to logger and #exceptions."""
    log.exception("Unhandled exception in event handler '%s'", event_method)
    exc_info = __import__("sys").exc_info()
    exc = exc_info[1]
    if exc:
        exc_str = f"{type(exc).__name__}: {exc}"
        await agents.send_to_exceptions(
            f"🔥 Unhandled exception in **{event_method}**:\n```\n{exc_str[:1500]}\n```"
        )


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    """Handle reaction adds — plan approval and AskUserQuestion emoji answers."""
    # Ignore own reactions (bot pre-adding them)
    if bot.user and payload.user_id == bot.user.id:
        return

    _tracer.start_span(
        "on_raw_reaction_add",
        attributes={"discord.emoji": str(payload.emoji), "discord.channel_id": str(payload.channel_id)},
    ).end()

    # --- Plan approval via reaction ---
    if (
        payload.guild_id == config.DISCORD_GUILD_ID
        and payload.user_id in config.ALLOWED_USER_IDS
    ):
        agent_name = agents.channel_to_agent.get(payload.channel_id)
        if agent_name is not None:
            session = agents.agents.get(agent_name)
            if session is not None:
                ds = discord_state(session)
                if (
                    ds.plan_approval_message_id is not None
                    and ds.plan_approval_message_id == payload.message_id
                    and ds.plan_approval_future is not None
                    and not ds.plan_approval_future.done()
                ):
                    emoji = str(payload.emoji)
                    channel = bot.get_channel(payload.channel_id)
                    if emoji == "\u2705":
                        ds.plan_approval_future.set_result({"approved": True, "message": ""})
                        if isinstance(channel, TextChannel):
                            await agents.send_system(channel, "Plan approved — agent resuming implementation.")
                        return
                    elif emoji == "\u274c":
                        ds.plan_approval_future.set_result(
                            {"approved": False, "message": "User rejected the plan. Please revise."}
                        )
                        if isinstance(channel, TextChannel):
                            await agents.send_system(channel, "Plan rejected — agent will revise.")
                        return

    # --- AskUserQuestion emoji answers ---
    session = agents.find_session_by_question_message(payload.message_id)
    if session is None:
        return
    ds = discord_state(session)
    if ds.question_future is None or ds.question_future.done():
        return

    emoji_str = str(payload.emoji)
    q = ds.question_data or {}
    answer = agents.resolve_reaction_answer(emoji_str, q)
    if answer is None:
        return  # Unrecognized emoji, ignore

    ds.question_future.set_result(answer)
    log.info("Question answered via reaction: %s -> %s", emoji_str, answer)


@bot.event
async def on_message(message: discord.Message) -> None:
    """Handle incoming Discord messages."""
    if not _startup_complete:
        return

    # Dedup: Discord may deliver the same message twice on gateway reconnects
    if message.id in _seen_message_ids:
        log.warning("DEDUP[%s] duplicate on_message delivery — skipping", message.id)
        return
    _seen_message_ids[message.id] = None
    while len(_seen_message_ids) > _DEDUP_CAPACITY:
        _seen_message_ids.popitem(last=False)

    # --- Authorization and channel checks ---
    if bot.user is not None and message.author.id == bot.user.id:
        return
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return
    if message.author.bot and message.author.id not in config.ALLOWED_USER_IDS:
        return

    # DM messages — redirect to guild
    if message.channel.type == ChannelType.private:
        if message.author.id not in config.ALLOWED_USER_IDS:
            return
        master_session = agents.get_master_session()
        if master_session and discord_state(master_session).channel_id:
            await message.channel.send(
                f"*System:* Please use <#{discord_state(master_session).channel_id}> in the server instead."
            )
        else:
            await message.channel.send("*System:* Please use the server channels instead.")
        return

    # Guild messages — only process in our target guild
    if message.guild is None or message.guild.id != config.DISCORD_GUILD_ID:
        return
    if not isinstance(message.channel, TextChannel):
        return
    if message.author.id not in config.ALLOWED_USER_IDS:
        return

    channel = message.channel

    # Track channel activity for recency reordering
    channels.mark_channel_active(channel.id)

    # --- Get content and look up agent ---
    content = await agents.extract_message_content(message)

    agent_name = agents.channel_to_agent.get(channel.id)

    # Set structured log context for this message
    set_agent_context(agent_name or "unknown", channel_id=channel.id)
    set_trigger("user_message", message_id=message.id)

    log.info(
        "Message from %s (%s) in #%s: %s",
        message.author.name,
        message.author.id,
        channel.name,
        agents.content_summary(content),
    )

    if agents.shutdown_coordinator and agents.shutdown_coordinator.requested:
        await agents.send_system(channel, "Bot is restarting — not accepting new messages.")
        return

    if agent_name is None:
        return

    session = agents.agents.get(agent_name)
    if session is None:
        if channels.killed_category and channel.category_id == channels.killed_category.id:
            await agents.send_system(
                channel,
                "This agent has been killed. Ask the master agent to spawn a new one.",
            )
        return

    # Block killed agents
    if channels.killed_category and channel.category_id == channels.killed_category.id:
        await agents.send_system(
            channel,
            "This agent has been killed. Use `/spawn` to create a new one.",
        )
        return

    # --- Plan approval gate ---
    ds = discord_state(session)
    if ds.plan_approval_future is not None and not ds.plan_approval_future.done():
        raw = content.strip() if isinstance(content, str) else ""
        text = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\]\s*", "", raw).strip().lower()
        if text in ("approve", "approved", "yes", "y", "lgtm", "go", "proceed", "ok"):
            ds.plan_approval_future.set_result({"approved": True, "message": ""})
            await agents.add_reaction(message, "✅")
            await agents.send_system(channel, "Plan approved — agent resuming implementation.")
        elif text in ("reject", "rejected", "no", "n", "cancel", "stop"):
            ds.plan_approval_future.set_result(
                {"approved": False, "message": "User rejected the plan. Please revise."}
            )
            await agents.add_reaction(message, "❌")
            await agents.send_system(channel, "Plan rejected — agent will revise.")
        else:
            feedback = content if isinstance(content, str) else str(content)
            ds.plan_approval_future.set_result(
                {
                    "approved": False,
                    "message": f"User wants changes to the plan: {feedback}",
                }
            )
            await agents.add_reaction(message, "📝")
            await agents.send_system(channel, "Feedback received — agent will revise the plan.")
        return

    # --- AskUserQuestion gate (text reply) ---
    if ds.question_future is not None and not ds.question_future.done():
        raw = content.strip() if isinstance(content, str) else str(content)
        raw = re.sub(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\]\s*", "", raw).strip()
        q = ds.question_data or {}
        answer = agents.parse_question_answer(raw, q)
        ds.question_future.set_result(answer)
        await agents.add_reaction(message, "\u2705")
        return

    # --- Text command handling (// prefix) ---
    if message.content.strip().startswith("//"):
        handled = await _handle_text_command(message, session, agent_name)
        if handled:
            return

    # --- Centralized message processing via hub ---
    from agenthub.messaging import receive_user_message

    msg_id = message.id
    log.info(
        "ON_MSG[%s][%s] processing=%s reconnecting=%s queue_size=%d lock_locked=%s",
        agent_name,
        msg_id,
        agents.is_processing(session),
        session.reconnecting,
        len(session.message_queue),
        session.query_lock.locked(),
    )

    # Stream handler: Discord live-edit rendering to the agent's channel
    async def _discord_stream_handler(s: AgentSession) -> str | None:
        return await agents.stream_response_to_channel(s, channel)

    # Route through /soul or /soul-flow flowcharts
    content = agents.wrap_content_with_soul(content, session)

    assert agents.hub is not None
    result = await receive_user_message(
        agents.hub,
        session,
        content,
        _discord_stream_handler,
        # Discord-specific queue item format for process_message_queue
        queue_item=(content, channel, message),
    )

    # Discord-specific reactions based on result
    _RESULT_REACTIONS = {
        "processed": "✅",
        "queued": "📨",
        "queued_reconnecting": "📨",
        "timeout": "⏳",
    }
    reaction = _RESULT_REACTIONS.get(result.status, "❌")
    if result.status != "shutdown":
        await agents.add_reaction(message, reaction)

    # Post-processing: yield check + queue drain (Discord's 3-tuple format)
    if result.status == "processed":
        if scheduler.should_yield(session.name):
            log.info("Scheduler yield: '%s' sleeping after user message", session.name)
            await agents.sleep_agent(session)
        else:
            await agents.process_message_queue(session)

    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------


async def _fire_schedules(
    entries: list[dict[str, Any]], now_utc: datetime, now_local: datetime
) -> None:
    """Fire due cron/one-off schedules and clean up consumed one-offs."""
    fired_one_off_keys: set[str] = set()

    for entry in list(entries):
        name = entry.get("name")
        if not name:
            continue

        if entry.get("disabled"):
            continue

        try:
            if "schedule" in entry:
                cron_expr = entry["schedule"]
                if not croniter.is_valid(cron_expr):
                    log.warning("Invalid cron expression for %s: %s", name, cron_expr)
                    continue

                last_occurrence = croniter(cron_expr, now_local).get_prev(datetime)

                skey = schedule_key(entry)
                if skey not in agents.schedule_last_fired:
                    # First time seeing this schedule (e.g. after restart):
                    # assume it already fired, so we don't catch-up all
                    # past occurrences at once.
                    agents.schedule_last_fired[skey] = last_occurrence

                if last_occurrence > agents.schedule_last_fired[skey]:
                    agents.schedule_last_fired[skey] = last_occurrence

                    set_agent_context(entry.get("owner") or entry.get("session") or name)
                    set_trigger("schedule", name=name)
                    log.info("Firing recurring event: %s", name)
                    _tracer.start_span("schedule.fire", attributes={"schedule.name": name, "schedule.type": "recurring"}).end()
                    agent_name = entry.get("owner") or entry.get("session") or name
                    agent_cwd = entry.get("cwd", os.path.join(config.AXI_USER_DATA, "agents", agent_name))

                    sched_ch = await agents.get_agent_channel(agent_name) if agent_name in agents.agents else None
                    if sched_ch:
                        await sched_ch.send(f"*System:* 📅 Scheduled: `{name}`")

                    if agent_name in agents.agents:
                        log.info("Routing event '%s' to existing session '%s'", name, agent_name)
                        await agents.send_prompt_to_agent(agent_name, entry["prompt"])
                    else:
                        await agents.reclaim_agent_name(agent_name)
                        await agents.spawn_agent(agent_name, agent_cwd, entry["prompt"])

                    append_history(entry, now_utc, dedup_minutes=5)

            elif "at" in entry:
                fire_at = datetime.fromisoformat(entry["at"])

                if fire_at <= now_utc:
                    set_agent_context(entry.get("owner") or entry.get("session") or name)
                    set_trigger("schedule_one_off", name=name)
                    log.info("Firing one-off event: %s", name)
                    _tracer.start_span("schedule.fire", attributes={"schedule.name": name, "schedule.type": "one_off"}).end()
                    agent_name = entry.get("owner") or entry.get("session") or name
                    agent_cwd = entry.get("cwd", os.path.join(config.AXI_USER_DATA, "agents", agent_name))

                    sched_ch = await agents.get_agent_channel(agent_name) if agent_name in agents.agents else None
                    if sched_ch:
                        await sched_ch.send(f"*System:* 📅 Scheduled (one-off): `{name}`")

                    if agent_name in agents.agents:
                        log.info("Routing event '%s' to existing session '%s'", name, agent_name)
                        await agents.send_prompt_to_agent(agent_name, entry["prompt"])
                    else:
                        await agents.reclaim_agent_name(agent_name)
                        await agents.spawn_agent(agent_name, agent_cwd, entry["prompt"])

                    fired_one_off_keys.add(schedule_key(entry))
                    append_history(entry, now_utc)

        except Exception:
            log.exception("Error processing scheduled event %s", name)

    if fired_one_off_keys:
        async with schedules_lock:
            current = load_schedules()
            current = [e for e in current if schedule_key(e) not in fired_one_off_keys]
            save_schedules(current)


async def _check_idle_agents(now_utc: datetime, master_ch: TextChannel | None) -> None:
    """Send idle reminders to agents that have been inactive too long."""
    idle_agents: list[tuple[AgentSession, str, int]] = []
    for agent_name, session in list(agents.agents.items()):
        if session.client is None:
            continue
        if session.query_lock.locked():
            continue
        ds = discord_state(session)
        if channels.killed_category and ds.channel_id:
            ch = bot.get_channel(ds.channel_id)
            if isinstance(ch, TextChannel) and ch.category_id == channels.killed_category.id:
                continue
        if session.idle_reminder_count >= len(config.IDLE_REMINDER_THRESHOLDS):
            continue

        cumulative = sum(config.IDLE_REMINDER_THRESHOLDS[: session.idle_reminder_count + 1], timedelta())
        idle_duration = now_utc - session.last_activity

        if idle_duration > cumulative:
            idle_minutes = int(idle_duration.total_seconds() / 60)
            idle_agents.append((session, agent_name, idle_minutes))

    for session, agent_name, idle_minutes in idle_agents:
        agent_ch = await agents.get_agent_channel(agent_name)
        if agent_ch:
            await agents.send_system(
                agent_ch,
                f"Agent **{agent_name}** has been idle for {idle_minutes} minutes. Use `/kill-agent` to terminate.",
            )
        is_final_threshold = session.idle_reminder_count + 1 >= len(config.IDLE_REMINDER_THRESHOLDS)
        if master_ch and is_final_threshold:
            await agents.send_system(
                master_ch,
                f"Agent **{agent_name}** has been idle for {idle_minutes} minutes "
                f"(cwd: `{session.cwd}`). Use `/kill-agent` to terminate.",
            )
        session.idle_reminder_count += 1
        discord_state(session).last_idle_notified = datetime.now(UTC)


async def _recover_stranded_messages() -> None:
    """Wake sleeping agents that have queued messages (stranded-message safety net)."""
    if scheduler.slot_count() < config.MAX_AWAKE_AGENTS:
        for _agent_name, session in list(agents.agents.items()):
            if session.client is None and session.message_queue and not session.query_lock.locked():
                content, ch, stranded_msg = session.message_queue.popleft()
                log.info("Stranded message found for sleeping agent '%s', waking", _agent_name)
                await agents.remove_reaction(stranded_msg, "📨")
                agents.fire_and_forget(agents.run_initial_prompt(session, content, ch))
                break


async def _auto_sleep_idle_agents(now_utc: datetime) -> None:
    """Put idle awake agents to sleep after 1 minute of inactivity.

    Scheduler handles eviction under pressure; this is just cleanup.
    """
    idle_threshold = timedelta(minutes=1)

    for agent_name, session in list(agents.agents.items()):
        if session.client is None:
            continue
        if session.query_lock.locked():
            continue
        if session.bridge_busy:
            continue
        idle_duration = now_utc - session.last_activity
        if idle_duration > idle_threshold:
            log.info("Auto-sleeping idle agent '%s' (idle %.0fs)", agent_name, idle_duration.total_seconds())
            try:
                await agents.sleep_agent(session)
            except Exception:
                log.exception("Error auto-sleeping agent '%s'", agent_name)


@tasks.loop(seconds=10)
async def check_schedules() -> None:
    if agents.shutdown_coordinator and agents.shutdown_coordinator.requested:
        return

    prune_history()

    now_utc = datetime.now(UTC)
    now_local = datetime.now(config.SCHEDULE_TIMEZONE)
    entries = load_schedules()

    log.debug("Scheduler tick: %d entries, %d agents awake", len(entries), agents.count_awake_agents())

    master_ch = await agents.get_master_channel()

    await _fire_schedules(entries, now_utc, now_local)
    await _check_idle_agents(now_utc, master_ch)
    await _recover_stranded_messages()
    await _auto_sleep_idle_agents(now_utc)


@check_schedules.before_loop
async def before_check_schedules() -> None:
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Interrupt helpers
# ---------------------------------------------------------------------------


async def _interrupt_agent(session: AgentSession) -> None:
    """Gracefully interrupt an agent's current turn, falling back to process kill.

    Sends a control_request.interrupt to the CLI, which aborts the current API
    call and emits a result message.  The CLI stays alive with context preserved
    — no session rebuild needed.  Falls back to destructive interrupt_session()
    only if the graceful interrupt fails (timeout/error).
    """
    if await agents.graceful_interrupt(session):
        return
    # Graceful interrupt failed — fall back to killing the CLI process
    log.warning("Graceful interrupt failed for '%s', falling back to process kill", session.name)
    await agents.interrupt_session(session)


# ---------------------------------------------------------------------------
# Autocomplete helpers
# ---------------------------------------------------------------------------


async def killable_agent_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete callback excluding axi-master."""
    return [
        app_commands.Choice(name=name, value=name)
        for name in agents.agents
        if name != config.MASTER_AGENT_NAME and current.lower() in name.lower()
    ][:25]


async def agent_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete callback for agent name parameters (all agents)."""
    return [app_commands.Choice(name=name, value=name) for name in agents.agents if current.lower() in name.lower()][:25]


# ---------------------------------------------------------------------------
# Agent resolution helper
# ---------------------------------------------------------------------------


async def _resolve_agent(
    interaction: discord.Interaction, agent_name: str | None
) -> tuple[str, AgentSession] | None:
    """Resolve an agent name (or infer from channel) and look up its session.

    Returns ``(name, session)`` on success, or ``None`` after sending an
    ephemeral error to the user.
    """
    if agent_name is None:
        agent_name = agents.channel_to_agent.get(interaction.channel_id or 0)
        if agent_name is None:
            await interaction.response.send_message(
                "Could not determine agent for this channel. Specify an agent name.", ephemeral=True
            )
            return None

    session = agents.agents.get(agent_name)
    if session is None:
        await interaction.response.send_message(f"Agent **{agent_name}** not found.", ephemeral=True)
        return None

    return agent_name, session


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


@bot.tree.command(name="ping", description="Check bot latency and uptime.")
async def ping_command(interaction: discord.Interaction) -> None:


    def _fmt_uptime(total_seconds: int) -> str:
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s"

    if _bot_start_time is not None:
        bot_uptime = datetime.now(UTC) - _bot_start_time
        bot_str = _fmt_uptime(int(bot_uptime.total_seconds()))
    else:
        bot_str = "initializing"

    procmux_str = None
    raw_conn = agents.hub.raw_procmux_conn if agents.hub else None
    if raw_conn is not None and raw_conn.is_alive:
        try:
            result = await raw_conn.send_command("status")
            if result.ok and result.uptime_seconds is not None:
                procmux_str = _fmt_uptime(result.uptime_seconds)
        except Exception:
            procmux_str = "error"

    latency = round(bot.latency * 1000)
    parts = [f"Pong! Latency: {latency}ms", f"Bot uptime: {bot_str}"]
    if procmux_str is not None:
        parts.append(f"Bridge uptime: {procmux_str}")
    elif raw_conn is None or not raw_conn.is_alive:
        parts.append("Bridge: not connected")
    await interaction.response.send_message(" | ".join(parts))


@bot.tree.command(name="claude-usage", description="Show Claude API usage for current sessions and rate limit status.")
@app_commands.describe(history="Number of recent rate limit events to show (omit for current status)")
async def claude_usage_command(interaction: discord.Interaction, history: int | None = None) -> None:
    log.info("Slash command /claude-usage history=%s from %s", history, interaction.user)


    if history is not None:
        count = max(1, min(history, 50))
        lines = [f"**Rate Limit History** (last {count} events)", ""]
        try:
            with open(config.RATE_LIMIT_HISTORY_PATH) as f:
                all_lines = f.readlines()
            recent = all_lines[-count:]
            if not recent:
                lines.append("No history recorded yet.")
            else:
                for raw_line in recent:
                    try:
                        r = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    ts = datetime.fromisoformat(r["ts"]).astimezone(config.SCHEDULE_TIMEZONE)
                    ts_str = ts.strftime("%-m/%-d %-I:%M %p")
                    rl_type = r.get("type", "?").replace("_", " ")
                    status = r.get("status", "?")
                    util = r.get("utilization")
                    if status == "rejected":
                        icon = "\U0001f6ab"
                    elif status == "allowed_warning":
                        icon = "\u26a0\ufe0f"
                    else:
                        icon = "\u2705"
                    util_str = f" ({int(util * 100)}%)" if util is not None else ""
                    lines.append(f"`{ts_str}` {icon} {rl_type}: {status}{util_str}")
        except FileNotFoundError:
            lines.append("No history file yet — events are recorded on API calls.")
        await interaction.response.send_message("\n".join(lines))
        return

    lines = ["**Claude Usage — Current Sessions**", ""]

    total_cost = 0.0
    total_queries = 0

    if agents.session_usage:
        for sid, usage in sorted(
            agents.session_usage.items(), key=lambda x: x[1].last_query or datetime.min.replace(tzinfo=UTC), reverse=True
        ):
            total_cost += usage.total_cost_usd
            total_queries += usage.queries

            duration_s = usage.total_duration_ms // 1000
            duration_str = agents.format_time_remaining(duration_s) if duration_s > 0 else "0s"

            active_str = ""
            if usage.first_query:
                age_s = int((datetime.now(UTC) - usage.first_query).total_seconds())
                active_str = f" | Active since {agents.format_time_remaining(age_s)} ago"

            token_str = ""
            if usage.total_input_tokens or usage.total_output_tokens:
                token_str = f" | Tokens: {usage.total_input_tokens:,}in / {usage.total_output_tokens:,}out"

            lines.append(f"**{usage.agent_name}** (`{sid[:8]}`)")
            lines.append(
                f"  Cost: **${usage.total_cost_usd:.2f}** | Queries: {usage.queries} | Turns: {usage.total_turns}{token_str}"
            )
            lines.append(f"  API time: {duration_str}{active_str}")
            lines.append("")

        lines.append(f"**Total: ${total_cost:.2f}** across {total_queries} queries")
    else:
        lines.append("No usage recorded yet.")

    lines.append("")

    if agents.rate_limit_quotas:
        now = datetime.now(UTC)
        lines.append("**Rate Limits**")

        display_order = ["five_hour", "seven_day"]
        sorted_keys = [k for k in display_order if k in agents.rate_limit_quotas]
        sorted_keys += [k for k in agents.rate_limit_quotas if k not in display_order]

        for rl_type in sorted_keys:
            q = agents.rate_limit_quotas[rl_type]
            remaining_s = max(0, int((q.resets_at - now).total_seconds()))
            resets_str = agents.format_time_remaining(remaining_s) if remaining_s > 0 else "now"

            local_reset = q.resets_at.astimezone(config.SCHEDULE_TIMEZONE)
            reset_time_str = local_reset.strftime("%-I:%M %p")
            local_now = now.astimezone(config.SCHEDULE_TIMEZONE)
            if local_reset.date() != local_now.date():
                reset_time_str = local_reset.strftime("%-I:%M %p %a")

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

            label = q.rate_limit_type.replace("_", " ")
            lines.append(f"  {label}: {status_str} — resets at {reset_time_str} (in {resets_str})")

        latest_update = max(q.updated_at for q in agents.rate_limit_quotas.values())
        age_s = int((now - latest_update).total_seconds())
        age_str = agents.format_time_remaining(age_s) if age_s > 0 else "just now"
        lines.append(f"  Last checked: {age_str} ago")
    elif agents.rate_limited_until:
        remaining = agents.format_time_remaining(agents.rate_limit_remaining_seconds())
        lines.append(f"**Rate Limit**: \U0001f6ab Rate limited (~{remaining} remaining)")
    else:
        lines.append("**Rate Limit**: No data yet (updates on next API call)")

    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="model", description="Get or set the default LLM model for spawned agents.")
@app_commands.describe(name="Model name (haiku, sonnet, opus) — omit to view current")
async def model_command(interaction: discord.Interaction, name: str | None = None) -> None:
    log.info("Slash command /model name=%s from %s", name, interaction.user)


    if name is None:
        current = config.get_model()
        await interaction.response.send_message(f"Current model: **{current}**")
    else:
        error = config.set_model(name)
        if error:
            await interaction.response.send_message(f"*System:* {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"*System:* Model set to **{name.lower()}**.")


@bot.tree.command(name="list-agents", description="List all active agent sessions.")
async def list_agents(interaction: discord.Interaction) -> None:
    log.info("Slash command /list-agents from %s", interaction.user)


    if not agents.agents:
        await interaction.response.send_message("No active agents.", ephemeral=True)
        return

    now = datetime.now(UTC)
    lines: list[str] = []
    for name, session in agents.agents.items():
        idle_minutes = int((now - session.last_activity).total_seconds() / 60)
        if session.query_lock.locked():
            status = " [busy]"
        elif session.client is not None:
            status = " [awake]"
        else:
            status = " [sleeping]"
        is_killed = False
        ds = discord_state(session)
        if channels.killed_category and ds.channel_id:
            ch = bot.get_channel(ds.channel_id)
            if isinstance(ch, TextChannel) and ch.category_id == channels.killed_category.id:
                is_killed = True
        killed_tag = " [killed]" if is_killed else ""
        protected = " [protected]" if name == config.MASTER_AGENT_NAME else ""
        sid = f" | sid: `{session.session_id[:8]}…`" if session.session_id else ""
        ch_mention = f" | <#{discord_state(session).channel_id}>" if discord_state(session).channel_id else ""
        lines.append(
            f"- **{name}**{status}{killed_tag}{protected}{ch_mention} | cwd: `{session.cwd}` | idle: {idle_minutes}m{sid}"
        )

    awake = agents.count_awake_agents()
    header = f"*System:* **Agent Sessions** ({awake}/{config.MAX_AWAKE_AGENTS} awake):\n"
    await interaction.response.send_message(header + "\n".join(lines))


@bot.tree.command(name="status", description="Show what an agent is currently doing.")
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def agent_status(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /status agent=%s from %s", agent_name, interaction.user)


    if agent_name is None:
        agent_name = agents.channel_to_agent.get(interaction.channel_id or 0)

    if agent_name is None:
        await _show_all_agents_status(interaction)
        return

    session = agents.agents.get(agent_name)
    if session is None:
        await interaction.response.send_message(f"Agent **{agent_name}** not found.", ephemeral=True)
        return

    await interaction.response.send_message(_format_agent_status(agent_name, session), ephemeral=True)


def _format_agent_status(name: str, session: AgentSession) -> str:
    """Format a detailed status message for a single agent."""
    now = datetime.now(UTC)
    lines = [f"**{name}**"]

    # Flowcoder-specific status
    if session.agent_type == "flowcoder":
        lines.append("Type: flowcoder")
        lines.append(f"cwd: `{session.cwd}`")
        return "\n".join(lines)

    # Basic state
    if session.client is None:
        lines.append("State: sleeping")
        idle = int((now - session.last_activity).total_seconds())
        lines.append(f"Last active: {agents.format_time_remaining(idle)} ago")
    elif session.bridge_busy:
        lines.append("State: **busy** (running in bridge)")
    elif not session.query_lock.locked():
        lines.append("State: awake, idle")
        idle = int((now - session.last_activity).total_seconds())
        lines.append(f"Idle for: {agents.format_time_remaining(idle)}")
    else:
        activity = session.activity

        if activity.phase == "thinking":
            lines.append("State: **thinking** (extended thinking)")
        elif activity.phase == "writing":
            lines.append(f"State: **writing response** ({activity.text_chars} chars so far)")
        elif activity.phase == "tool_use" and activity.tool_name:
            display = tool_display(activity.tool_name)
            lines.append(f"State: **{display}**")
            if activity.tool_name == "Bash" and activity.tool_input_preview:
                preview = agents.extract_tool_preview(activity.tool_name, activity.tool_input_preview)
                if preview:
                    lines.append(f"```\n{preview}\n```")
            elif activity.tool_name in ("Read", "Write", "Edit", "Grep", "Glob") and activity.tool_input_preview:
                preview = agents.extract_tool_preview(activity.tool_name, activity.tool_input_preview)
                if preview:
                    lines.append(f"`{preview}`")
        elif activity.phase == "waiting":
            lines.append("State: **processing tool results...**")
        elif activity.phase == "starting":
            lines.append("State: **starting query...**")
        else:
            lines.append(f"State: **busy** ({activity.phase})")

        if activity.query_started:
            elapsed = int((now - activity.query_started).total_seconds())
            lines.append(f"Query running for: {agents.format_time_remaining(elapsed)}")

        if activity.turn_count > 0:
            lines.append(f"API turns: {activity.turn_count}")

        if activity.last_event:
            since_last = int((now - activity.last_event).total_seconds())
            if since_last > 30:
                lines.append(f"No stream events for {agents.format_time_remaining(since_last)} (may be running a long tool)")

    queue_size = len(session.message_queue)
    if queue_size > 0:
        lines.append(f"Queued messages: {queue_size}")

    if agents.is_rate_limited():
        remaining = agents.format_time_remaining(agents.rate_limit_remaining_seconds())
        lines.append(f"Rate limited: ~{remaining} remaining")

    if session.plan_mode:
        lines.append("📋 **Plan mode active**")

    if session.context_tokens > 0 and session.context_window > 0:
        pct = session.context_tokens / session.context_window
        lines.append(f"Context: {session.context_tokens:,}/{session.context_window:,} tokens ({pct:.0%})")

    if session.session_id:
        lines.append(f"Session: `{session.session_id[:8]}...`")
    lines.append(f"cwd: `{session.cwd}`")

    return "\n".join(lines)


def _agent_state_summary(session: AgentSession) -> str:
    """Return a short state string for an agent (e.g. 'sleeping (5m)', 'thinking...')."""
    now = datetime.now(UTC)
    if session.client is None:
        idle = int((now - session.last_activity).total_seconds())
        return f"sleeping ({agents.format_time_remaining(idle)})"
    if session.bridge_busy:
        return "busy (running in bridge)"
    if not session.query_lock.locked():
        idle = int((now - session.last_activity).total_seconds())
        return f"idle ({agents.format_time_remaining(idle)})"

    activity = session.activity
    if activity.phase == "thinking":
        status = "thinking..."
    elif activity.phase == "writing":
        status = "writing response..."
    elif activity.phase == "tool_use" and activity.tool_name:
        status = tool_display(activity.tool_name)
    elif activity.phase == "waiting":
        status = "processing tool results..."
    else:
        status = "busy"

    if activity.query_started:
        elapsed = int((now - activity.query_started).total_seconds())
        status += f" ({agents.format_time_remaining(elapsed)})"
    return status


async def _show_all_agents_status(interaction: discord.Interaction) -> None:
    """Show a summary of all agents when /status is used without an agent name."""
    if not agents.agents:
        await interaction.response.send_message("No active agents.", ephemeral=True)
        return

    lines: list[str] = []
    for name, session in agents.agents.items():
        status = _agent_state_summary(session)
        queue = len(session.message_queue)
        queue_str = f" | {queue} queued" if queue > 0 else ""
        lines.append(f"- **{name}**: {status}{queue_str}")

    awake = agents.count_awake_agents()
    header = f"**Agent Status** ({awake}/{config.MAX_AWAKE_AGENTS} awake)"
    if agents.is_rate_limited():
        remaining = agents.format_time_remaining(agents.rate_limit_remaining_seconds())
        header += f" | rate limited (~{remaining})"

    await interaction.response.send_message(f"*System:* {header}\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="debug", description="Toggle debug output (tool calls, thinking) for an agent.")
@app_commands.describe(mode="on / off / omit to toggle")
async def debug_command(interaction: discord.Interaction, mode: str | None = None) -> None:
    log.info("Slash command /debug mode=%s from %s", mode, interaction.user)

    resolved = await _resolve_agent(interaction, None)
    if resolved is None:
        return
    agent_name, session = resolved

    if mode is not None:
        mode_lower = mode.strip().lower()
        if mode_lower == "on":
            discord_state(session).debug = True
        elif mode_lower == "off":
            discord_state(session).debug = False
        else:
            await interaction.response.send_message(
                "Usage: `/debug` (toggle), `/debug on`, `/debug off`", ephemeral=True
            )
            return
    else:
        discord_state(session).debug = not discord_state(session).debug

    state = "on" if discord_state(session).debug else "off"
    await interaction.response.send_message(f"*System:* Debug output **{state}** for **{agent_name}**.")


@bot.tree.command(name="kill-agent", description="Terminate an agent session.")
@app_commands.autocomplete(agent_name=killable_agent_autocomplete)
async def kill_agent(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /kill-agent %s from %s", agent_name, interaction.user)

    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, session = resolved

    if agent_name == config.MASTER_AGENT_NAME:
        await interaction.response.send_message("Cannot kill the axi-master session.", ephemeral=True)
        return

    await interaction.response.defer()
    session_id = session.session_id
    _tracer.start_span("slash.kill_agent", attributes={"agent.name": agent_name}).end()

    agent_ch = await agents.get_agent_channel(agent_name)
    if agent_ch and agent_ch.id != interaction.channel_id:
        if session_id:
            await agents.send_system(
                agent_ch,
                f"Agent **{agent_name}** moved to Killed.\nSession ID: `{session_id}` — use this to resume later.",
            )
        else:
            await agents.send_system(agent_ch, f"Agent **{agent_name}** moved to Killed.")

    agents.agents.pop(agent_name, None)
    await agents.sleep_agent(session, force=True)
    await agents.move_channel_to_killed(agent_name)

    if session_id:
        await interaction.followup.send(
            f"*System:* Agent **{agent_name}** moved to Killed.\nSession ID: `{session_id}` — use this to resume later."
        )
    else:
        await interaction.followup.send(f"*System:* Agent **{agent_name}** moved to Killed.")


@bot.tree.command(name="spawn", description="Spawn a new agent session with its own Discord channel.")
async def spawn_agent_cmd(
    interaction: discord.Interaction,
    name: str,
    prompt: str,
    cwd: str | None = None,
    resume: str | None = None,
) -> None:
    log.info("Slash command /spawn %s from %s", name, interaction.user)
    if interaction.user.id not in config.ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    agent_name = name.strip()
    if not agent_name:
        await interaction.response.send_message("Agent name cannot be empty.", ephemeral=True)
        return
    if agent_name == config.MASTER_AGENT_NAME:
        await interaction.response.send_message(
            f"Cannot spawn agent with reserved name '{config.MASTER_AGENT_NAME}'.", ephemeral=True
        )
        return
    if agent_name in agents.agents and not resume:
        await interaction.response.send_message(
            f"Agent **{agent_name}** already exists. Kill it first or use `resume` to replace it.", ephemeral=True
        )
        return

    default_cwd = os.path.join(config.AXI_USER_DATA, "agents", agent_name)
    agent_cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else default_cwd

    if not any(agent_cwd == d or agent_cwd.startswith(d + os.sep) for d in config.ALLOWED_CWDS):
        await interaction.response.send_message(
            "Error: cwd is not in allowed directories.", ephemeral=True
        )
        return

    await interaction.response.defer()

    async def _do_spawn():
        try:
            if agent_name in agents.agents and resume:
                await agents.reclaim_agent_name(agent_name)
            await agents.spawn_agent(agent_name, agent_cwd, prompt, resume=resume)
        except Exception:
            channels.bot_creating_channels.discard(channels.normalize_channel_name(agent_name))
            log.exception("Error in background spawn of agent '%s'", agent_name)
            try:
                channel = await agents.get_agent_channel(agent_name)
                if channel:
                    await agents.send_system(channel, f"Failed to spawn agent **{agent_name}**. Check logs for details.")
            except Exception:
                pass

    channels.bot_creating_channels.add(channels.normalize_channel_name(agent_name))
    asyncio.create_task(_do_spawn())
    await interaction.followup.send(
        f"*System:* Spawning agent **{agent_name}** in `{agent_cwd}`..."
    )


@bot.tree.command(
    name="restart-agent",
    description="Restart an agent's CLI process with a fresh system prompt (preserves session context).",
)
@app_commands.autocomplete(agent_name=killable_agent_autocomplete)
async def restart_agent_cmd(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /restart-agent %s from %s", agent_name, interaction.user)

    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, session = resolved

    if agent_name == config.MASTER_AGENT_NAME:
        await interaction.response.send_message(
            "Cannot restart axi-master this way. Use `/restart` instead.", ephemeral=True
        )
        return

    await interaction.response.defer()
    _tracer.start_span("slash.restart_agent", attributes={"agent.name": agent_name}).end()

    session = await agents.restart_agent(agent_name)

    agent_ch = await agents.get_agent_channel(agent_name)
    if agent_ch and agent_ch.id != interaction.channel_id:
        await agents.send_system(
            agent_ch,
            f"Agent **{agent_name}** restarted with fresh system prompt. Session context preserved.",
        )

    await interaction.followup.send(
        f"*System:* Agent **{agent_name}** restarted. System prompt refreshed, session `{session.session_id or 'none'}` preserved."
    )


@bot.tree.command(name="stop", description="Interrupt a running agent query (like Ctrl+C).")
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def stop_agent(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /stop agent=%s from %s", agent_name, interaction.user)

    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, session = resolved

    if session.client is None or not session.query_lock.locked():
        await interaction.response.send_message(f"Agent **{agent_name}** is not busy.", ephemeral=True)
        return

    await interaction.response.defer()
    trace_tag = agents.get_active_trace_tag(agent_name)
    _tracer.start_span(
        "slash.stop_agent",
        attributes={"agent.name": agent_name, "interrupted.trace_tag": trace_tag},
    ).end()

    try:
        await _interrupt_agent(session)

        plan_was_active = session.plan_mode
        if plan_was_active:
            session.plan_mode = False
            try:
                await session.client.set_permission_mode("default")
            except Exception:
                log.exception("Failed to reset permission mode for '%s' during /stop", agent_name)

        ds = discord_state(session)
        if ds.plan_approval_future and not ds.plan_approval_future.done():
            ds.plan_approval_future.set_result({"approved": False, "message": "Interrupted by /stop."})
        ds.plan_approval_message_id = None

        if ds.question_future and not ds.question_future.done():
            ds.question_future.set_result("")
            ds.question_data = None
            ds.question_message_id = None

        cleared = 0
        while session.message_queue:
            _, ch, dropped_msg = session.message_queue.popleft()
            await agents.remove_reaction(dropped_msg, "📨")
            cleared += 1

        parts = [f"*System:* Interrupt signal sent to **{agent_name}**."]
        if cleared:
            parts.append(f"Cleared {cleared} queued message{'s' if cleared != 1 else ''}.")
        if plan_was_active:
            parts.append("Plan mode deactivated.")
        if trace_tag:
            parts.append(f"\n-# Interrupted turn {trace_tag}")
        await interaction.followup.send(" ".join(parts))
    except Exception as e:
        log.exception("Failed to interrupt agent '%s'", agent_name)
        await interaction.followup.send(f"Failed to interrupt **{agent_name}**: {e}")


@bot.tree.command(name="skip", description="Interrupt the current query but keep processing queued messages.")
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def skip_agent(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /skip agent=%s from %s", agent_name, interaction.user)

    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, session = resolved

    if session.client is None or not session.query_lock.locked():
        await interaction.response.send_message(f"Agent **{agent_name}** is not busy.", ephemeral=True)
        return

    await interaction.response.defer()
    trace_tag = agents.get_active_trace_tag(agent_name)
    _tracer.start_span(
        "slash.skip_agent",
        attributes={"agent.name": agent_name, "interrupted.trace_tag": trace_tag},
    ).end()

    queued = len(session.message_queue)
    activity = session.activity
    tool_suffix = ""
    if activity.phase == "waiting" and activity.tool_name:
        tool_suffix = f" (was {tool_display(activity.tool_name)})"

    try:
        await _interrupt_agent(session)
        if queued:
            msg = (
                f"*System:* Skipped current query for **{agent_name}**{tool_suffix}. "
                f"{queued} queued message{'s' if queued != 1 else ''} will continue processing."
            )
        else:
            msg = f"*System:* Skipped current query for **{agent_name}**{tool_suffix}. No queued messages."
        if trace_tag:
            msg += f"\n-# Skipped turn {trace_tag}"
        await interaction.followup.send(msg)
    except Exception as e:
        log.exception("Failed to interrupt agent '%s'", agent_name)
        await interaction.followup.send(f"Failed to skip **{agent_name}**: {e}")


@bot.tree.command(
    name="plan",
    description="Toggle plan mode — agent will plan before implementing. Infers agent from current channel.",
)
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def toggle_plan_mode(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /plan agent=%s from %s", agent_name, interaction.user)

    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, session = resolved

    new_mode = not session.plan_mode
    session.plan_mode = new_mode

    if session.client is not None:
        try:
            mode_str = "plan" if new_mode else "default"
            await session.client.set_permission_mode(mode_str)
            log.info("Agent '%s' permission mode set to '%s'", agent_name, mode_str)
        except Exception as e:
            log.exception("Failed to set permission mode for '%s'", agent_name)
            session.plan_mode = not new_mode
            await interaction.response.send_message(
                f"Failed to set plan mode for **{agent_name}**: {e}", ephemeral=True
            )
            return

    if new_mode:
        await interaction.response.send_message(
            f"📋 **Plan mode ON** for **{agent_name}** — next query will plan before implementing."
        )
    else:
        await interaction.response.send_message(
            f"🔧 **Plan mode OFF** for **{agent_name}** — back to normal execution."
        )


@bot.tree.command(
    name="reset-context", description="Reset an agent's context. Infers agent from current channel, or specify by name."
)
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def reset_context(interaction: discord.Interaction, agent_name: str | None = None, working_dir: str | None = None) -> None:
    log.info("Slash command /reset-context agent=%s cwd=%s from %s", agent_name, working_dir, interaction.user)

    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, _ = resolved

    await interaction.response.defer()
    session = await agents.reset_session(agent_name, cwd=working_dir)
    await interaction.followup.send(f"*System:* Context reset for **{agent_name}**. Working directory: `{session.cwd}`")


# ---------------------------------------------------------------------------
# Text command handler (// prefix)
# ---------------------------------------------------------------------------


async def _handle_text_command(message: discord.Message, session: AgentSession, agent_name: str) -> bool:
    """Handle // text commands from Discord messages. Returns True if handled."""
    text = message.content.strip()
    if not text.startswith("//"):
        return False

    parts = text[2:].split(None, 1)
    if not parts:
        return False

    cmd = parts[0].lower()
    cmd_args = parts[1].strip() if len(parts) > 1 else None
    assert isinstance(message.channel, TextChannel)
    channel = message.channel
    _tracer.start_span("text_command", attributes={"command": cmd, "agent.name": agent_name}).end()

    if cmd == "debug":
        if cmd_args is not None:
            mode_lower = cmd_args.lower()
            if mode_lower == "on":
                discord_state(session).debug = True
            elif mode_lower == "off":
                discord_state(session).debug = False
            else:
                await agents.send_system(channel, "Usage: `//debug` (toggle), `//debug on`, `//debug off`")
                return True
        else:
            discord_state(session).debug = not discord_state(session).debug
        state = "on" if discord_state(session).debug else "off"
        await agents.send_system(channel, f"Debug output **{state}** for **{agent_name}**.")
        return True

    if cmd == "status":
        status_text = _format_agent_status(agent_name, session)
        await agents.send_long(channel, status_text)
        return True

    if cmd == "todo":
        if discord_state(session).todo_items:
            await agents.send_long(channel, f"**Todo List**\n{agents.format_todo_list(discord_state(session).todo_items)}")
        else:
            await agents.send_system(channel, f"No todo list for **{agent_name}**.")
        return True

    if cmd in ("clear", "compact"):
        label = "Context cleared" if cmd == "clear" else "Context compacted"
        command = f"/{cmd}"
        # Append stored compact instructions for /compact
        if cmd == "compact" and session.compact_instructions:
            command = f"/compact {session.compact_instructions}"

        if session.query_lock.locked():
            await agents.send_system(channel, f"Agent **{agent_name}** is busy.")
            return True

        async with session.query_lock:
            if not agents.is_awake(session):
                try:
                    await agents.wake_agent(session)
                except Exception:
                    log.exception("Failed to wake agent '%s'", agent_name)
                    await agents.send_system(channel, f"Failed to wake agent **{agent_name}**.")
                    return True

            session.last_activity = datetime.now(UTC)
            agents.drain_stderr(session)
            agents.drain_sdk_buffer(session)

            assert session.client is not None
            session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))
            try:
                async with asyncio.timeout(config.QUERY_TIMEOUT):
                    await session.client.query(agents.as_stream(command))
                    await agents.stream_with_retry(session, channel)
                await agents.send_system(channel, f"{label} for **{agent_name}**.")
            except TimeoutError:
                await agents.send_system(channel, f"{label} timed out for **{agent_name}**.")
            except Exception as e:
                log.exception("Failed to %s agent '%s'", label.lower(), agent_name)
                await agents.send_system(channel, f"Failed to {label.lower()} **{agent_name}**: {e}")
            finally:
                session.activity = ActivityState(phase="idle")
        return True

    if cmd == "flowchart" and config.FLOWCODER_ENABLED:
        if session.agent_type != "flowcoder":
            await agents.send_system(channel, "Flowcharts are only available for **flowcoder** agents.")
            return True

        if not cmd_args:
            await agents.send_system(channel, "Usage: `//flowchart <command> [args]`")
            return True

        fc_parts = cmd_args.split(None, 1)
        fc_name = fc_parts[0].lstrip("/")
        fc_args = fc_parts[1] if len(fc_parts) > 1 else ""

        if session.query_lock.locked():
            await agents.send_system(channel, f"Agent **{agent_name}** is busy.")
            return True

        slash_content = f"/{fc_name}" + (f" {fc_args}" if fc_args else "")

        async def _run_flowchart() -> None:
            if not agents.is_awake(session):
                await agents.wake_agent(session)
            async with session.query_lock:
                await agents.process_message(session, slash_content, channel)

        agents.fire_and_forget(_run_flowchart())
        return True

    if cmd == "skip":
        if session.client is None or not session.query_lock.locked():
            await agents.send_system(channel, f"Agent **{agent_name}** is not busy.")
            return True

        try:
            activity = session.activity
            tool_suffix = ""
            if activity.phase == "waiting" and activity.tool_name:
                tool_suffix = f" (was {tool_display(activity.tool_name)})"

            await _interrupt_agent(session)
            queued = len(session.message_queue)
            if queued:
                msg = (
                    f"Skipped current query for **{agent_name}**{tool_suffix}. "
                    f"{queued} queued message{'s' if queued != 1 else ''} will continue processing."
                )
            else:
                msg = f"Skipped current query for **{agent_name}**{tool_suffix}. No queued messages."
            await agents.send_system(channel, msg)
        except Exception as e:
            log.exception("Failed to skip agent '%s'", agent_name)
            await agents.send_system(channel, f"Failed to skip **{agent_name}**: {e}")
        return True

    if cmd == "stop":
        if session.client is None or not session.query_lock.locked():
            await agents.send_system(channel, f"Agent **{agent_name}** is not busy.")
            return True

        try:
            await _interrupt_agent(session)

            plan_was_active = session.plan_mode
            if plan_was_active:
                session.plan_mode = False
                try:
                    await session.client.set_permission_mode("default")
                except Exception:
                    log.exception("Failed to reset permission mode for '%s' during //stop", agent_name)

            ds = discord_state(session)
            if ds.plan_approval_future and not ds.plan_approval_future.done():
                ds.plan_approval_future.set_result({"approved": False, "message": "Interrupted by //stop."})
            ds.plan_approval_message_id = None

            if ds.question_future and not ds.question_future.done():
                ds.question_future.set_result("")
                ds.question_data = None
                ds.question_message_id = None

            cleared = 0
            while session.message_queue:
                _, ch_q, dropped_msg = session.message_queue.popleft()
                await agents.remove_reaction(dropped_msg, "📨")
                cleared += 1

            parts = [f"Interrupt signal sent to **{agent_name}**."]
            if cleared:
                parts.append(f"Cleared {cleared} queued message{'s' if cleared != 1 else ''}.")
            if plan_was_active:
                parts.append("Plan mode deactivated.")
            await agents.send_system(channel, " ".join(parts))
        except Exception as e:
            log.exception("Failed to interrupt agent '%s'", agent_name)
            await agents.send_system(channel, f"Failed to interrupt **{agent_name}**: {e}")
        return True

    return False


# ---------------------------------------------------------------------------
# SDK command helper (shared by /compact, /clear)
# ---------------------------------------------------------------------------


async def _run_agent_sdk_command(interaction: discord.Interaction, agent_name: str | None, command: str, label: str) -> None:
    """Run a Claude Code CLI slash command on an agent via the SDK."""
    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, session = resolved

    if session.query_lock.locked():
        await interaction.response.send_message(f"Agent **{agent_name}** is busy.", ephemeral=True)
        return

    await interaction.response.defer()

    async with session.query_lock:
        if not agents.is_awake(session):
            try:
                await agents.wake_agent(session)
            except Exception:
                log.exception("Failed to wake agent '%s'", agent_name)
                await interaction.followup.send(f"Failed to wake agent **{agent_name}**.")
                return

        session.last_activity = datetime.now(UTC)
        agents.drain_stderr(session)
        agents.drain_sdk_buffer(session)

        assert session.client is not None
        session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))
        try:
            async with asyncio.timeout(config.QUERY_TIMEOUT):
                ds = discord_state(session)
                assert ds.channel_id is not None
                ch = bot.get_channel(ds.channel_id)
                assert isinstance(ch, TextChannel)
                await session.client.query(agents.as_stream(command))
                await agents.stream_with_retry(session, ch)
            await interaction.followup.send(f"*System:* {label} for **{agent_name}**.")
        except TimeoutError:
            await interaction.followup.send(f"*System:* {label} timed out for **{agent_name}**.")
        except Exception as e:
            log.exception("Failed to %s agent '%s'", label.lower(), agent_name)
            await interaction.followup.send(f"Failed to {label.lower()} **{agent_name}**: {e}")
        finally:
            session.activity = ActivityState(phase="idle")


@bot.tree.command(
    name="compact", description="Compact an agent's conversation context. Infers agent from current channel."
)
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def compact_context(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /compact agent=%s from %s", agent_name, interaction.user)
    # Resolve agent to get compact_instructions
    resolved_name = agent_name or agents.channel_to_agent.get(interaction.channel_id or 0)
    session = agents.agents.get(resolved_name) if resolved_name else None
    command = "/compact"
    if session and session.compact_instructions:
        command = f"/compact {session.compact_instructions}"
    await _run_agent_sdk_command(interaction, agent_name, command, "Context compacted")


@bot.tree.command(name="clear", description="Clear an agent's conversation context. Infers agent from current channel.")
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def clear_context(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /clear agent=%s from %s", agent_name, interaction.user)
    await _run_agent_sdk_command(interaction, agent_name, "/clear", "Context cleared")


# ---------------------------------------------------------------------------
# TELOS interview
# ---------------------------------------------------------------------------


async def _run_telos_interview(session: AgentSession, channel: TextChannel) -> None:
    """Inject telos_interview.md into the agent so Claude conducts the TELOS interview."""
    interview_path = os.path.join(config.BOT_DIR, ".claude", "commands", "telos_interview.md")
    telos_path = os.path.join(config.BOT_DIR, "TELOS.md")

    try:
        with open(interview_path) as f:
            interview_instructions = f.read()
    except FileNotFoundError:
        await channel.send("*System:* Could not find `telos_interview.md`. Cannot start TELOS interview.")
        return
    except OSError as e:
        await channel.send(f"*System:* Error reading telos_interview.md: {e}")
        return

    query = (
        "The user has triggered the TELOS interview via Discord. "
        "Please conduct the interview now, following the instructions below exactly. "
        f"Write completed sections to `{telos_path}` as you go.\n\n"
        "--- TELOS INTERVIEW INSTRUCTIONS ---\n\n"
        f"{interview_instructions}"
    )

    log.info("Starting TELOS interview for agent '%s'", session.name)
    assert session.client is not None
    await session.client.query(agents.as_stream(query))
    await agents.stream_with_retry(session, channel)


@bot.tree.command(
    name="telos",
    description="Start a TELOS identity interview to build your user profile. Infers agent from current channel.",
)
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def telos_interview_cmd(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /telos agent=%s from %s", agent_name, interaction.user)

    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, session = resolved

    if session.query_lock.locked():
        await interaction.response.send_message(
            f"Agent **{agent_name}** is busy. Wait for it to finish.", ephemeral=True
        )
        return

    await interaction.response.defer()

    async with session.query_lock:
        if session.client is None:
            try:
                await agents.wake_agent(session)
            except Exception:
                log.exception("Failed to wake agent '%s'", agent_name)
                await interaction.followup.send(f"Failed to wake agent **{agent_name}**.")
                return

        session.last_activity = datetime.now(UTC)
        agents.drain_stderr(session)
        agents.drain_sdk_buffer(session)
        session.activity = ActivityState(phase="starting", query_started=datetime.now(UTC))

        try:
            async with asyncio.timeout(config.QUERY_TIMEOUT):
                ds = discord_state(session)
                assert ds.channel_id is not None
                ch = bot.get_channel(ds.channel_id)
                assert isinstance(ch, TextChannel)
                await _run_telos_interview(session, ch)
            await interaction.followup.send(f"*System:* TELOS interview complete for **{agent_name}**.")
        except TimeoutError:
            await interaction.followup.send(f"*System:* TELOS interview timed out for **{agent_name}**.")
        except Exception as e:
            log.exception("Failed to run TELOS interview for agent '%s'", agent_name)
            await interaction.followup.send(f"Failed to start TELOS interview for **{agent_name}**: {e}")
        finally:
            session.activity = ActivityState(phase="idle")


# ---------------------------------------------------------------------------
# Flowchart commands
# ---------------------------------------------------------------------------


def _list_flowchart_commands() -> list[dict[str, Any]]:
    """Return available flowchart commands as [{name, description}, ...]."""
    from axi.flowcoder import get_search_paths

    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for commands_dir in get_search_paths():
        if not os.path.isdir(commands_dir):
            continue
        for fname in sorted(os.listdir(commands_dir)):
            if not fname.endswith(".json"):
                continue
            name = fname.removesuffix(".json")
            if name in seen:
                continue
            seen.add(name)
            try:
                with open(os.path.join(commands_dir, fname)) as f:
                    data = json.load(f)
                results.append(
                    {
                        "name": data.get("name", name),
                        "description": data.get("description", ""),
                    }
                )
            except Exception:
                results.append({"name": name, "description": ""})
    return results


async def flowchart_name_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete callback for flowchart command names."""
    commands = _list_flowchart_commands()
    return [
        app_commands.Choice(name=cmd["name"], value=cmd["name"])
        for cmd in commands
        if current.lower() in cmd["name"].lower()
    ][:25]


@bot.tree.command(name="flowchart", description="Run a flowchart command inline in the current agent's channel.")
@app_commands.describe(name="Flowchart command name", args="Arguments for the flowchart command")
@app_commands.autocomplete(name=flowchart_name_autocomplete)
async def flowchart_cmd(interaction: discord.Interaction, name: str, args: str | None = None) -> None:
    log.info("Slash command /flowchart name=%s args=%s from %s", name, args, interaction.user)

    resolved = await _resolve_agent(interaction, None)
    if resolved is None:
        return
    agent_name, session = resolved

    if session.agent_type != "flowcoder":
        await interaction.response.send_message(
            "Flowcharts are only available for **flowcoder** agents.", ephemeral=True
        )
        return

    if session.query_lock.locked():
        await interaction.response.send_message(
            f"Agent **{agent_name}** is busy. Wait for it to finish.", ephemeral=True
        )
        return

    await interaction.response.defer()

    ds = discord_state(session)
    assert ds.channel_id is not None
    ch = bot.get_channel(ds.channel_id)
    assert isinstance(ch, TextChannel)

    fc_name = name.lstrip("/")
    fc_args = args or ""
    slash_content = f"/{fc_name}" + (f" {fc_args}" if fc_args else "")

    async def _run_flowchart() -> None:
        if not agents.is_awake(session):
            await agents.wake_agent(session)
        async with session.query_lock:
            await agents.process_message(session, slash_content, ch)

    agents.fire_and_forget(_run_flowchart())

    await interaction.followup.send(f"*System:* Flowchart `{fc_name}` started on **{agent_name}**.")


@bot.tree.command(name="flowchart-list", description="List available flowchart commands.")
async def flowchart_list_cmd(interaction: discord.Interaction) -> None:
    log.info("Slash command /flowchart-list from %s", interaction.user)


    commands = _list_flowchart_commands()
    if not commands:
        await interaction.response.send_message("No flowchart commands found.", ephemeral=True)
        return

    fc_lines: list[str] = []
    for cmd in commands:
        desc = f" — {cmd['description']}" if cmd["description"] else ""
        fc_lines.append(f"• `{cmd['name']}`{desc}")

    await interaction.response.send_message(
        f"*System:* **Available flowcharts** ({len(commands)}):\n" + "\n".join(fc_lines),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Restart commands
# ---------------------------------------------------------------------------


@bot.tree.command(name="restart", description="Hot-reload bot.py (bridge stays alive, agents keep running).")
@app_commands.describe(force="Skip waiting for busy agents and restart immediately")
async def restart_cmd(interaction: discord.Interaction, force: bool = False) -> None:

    _tracer.start_span("slash.restart", attributes={"restart.force": force}).end()

    if agents.shutdown_coordinator is None:
        await interaction.response.send_message("Bot is not fully initialized yet.", ephemeral=True)
        return

    if force:
        await interaction.response.send_message("*System:* Force restarting (hot reload)...")
        log.info("Force restart requested via /restart command")
        await agents.shutdown_coordinator.force_shutdown("/restart force")
        return

    await interaction.response.send_message("*System:* Initiating graceful restart (hot reload)...")
    log.info("Restart requested via /restart command")
    await agents.shutdown_coordinator.graceful_shutdown("/restart command")


@bot.tree.command(
    name="restart-including-bridge",
    description="Full restart — kills bridge + all agents. Sessions will disconnect.",
)
@app_commands.describe(force="Skip waiting for busy agents and restart immediately")
async def restart_including_bridge_cmd(interaction: discord.Interaction, force: bool = False) -> None:

    if agents.shutdown_coordinator is None:
        await interaction.response.send_message("Bot is not fully initialized yet.", ephemeral=True)
        return
    if agents.shutdown_coordinator.requested:
        await interaction.response.send_message(
            "*System:* A restart is already in progress.",
            ephemeral=True,
        )
        return

    async def _send_goodbye() -> None:
        master_ch = await agents.get_master_channel()
        if master_ch:
            await master_ch.send("*System:* Full restart — bridge is going down. See you soon!")

    full_coordinator = agents.make_shutdown_coordinator(
        close_bot_fn=bot.close,
        kill_fn=kill_supervisor,
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


# ---------------------------------------------------------------------------
# Channel creation / deletion listeners
# ---------------------------------------------------------------------------


def _register_agent_from_channel(channel: TextChannel, cwd: str) -> None:
    """Register a sleeping agent session for a manually-created channel."""
    agent_name = channel.name
    prompt = make_spawned_agent_system_prompt(cwd, agent_name=agent_name)
    mcp_servers: dict[str, Any] = {"utils": tools.utils_mcp_server}
    mcp_servers["schedule"] = make_schedule_mcp_server(agent_name, config.SCHEDULES_PATH, cwd)

    session = AgentSession(
        name=agent_name,
        client=None,
        cwd=cwd,
        system_prompt=prompt,
        system_prompt_hash=compute_prompt_hash(prompt),
        mcp_servers=mcp_servers,
    )
    discord_state(session).channel_id = channel.id
    agents.agents[agent_name] = session
    agents.channel_to_agent[channel.id] = agent_name


async def _set_channel_topic(channel: TextChannel, cwd: str, prompt_hash: str | None) -> None:
    """Set channel topic (fire-and-forget safe)."""
    desired_topic = agents.format_channel_topic(cwd, prompt_hash=prompt_hash)
    if channel.topic != desired_topic:
        try:
            await channel.edit(topic=desired_topic)
        except discord.HTTPException as e:
            log.warning("Failed to set topic on #%s: %s", channel.name, e)


async def _handle_active_channel_create(channel: TextChannel) -> None:
    """Handle a channel created in the Active category — general-purpose agent."""
    agent_name = channel.name
    cwd = os.path.join(config.AXI_USER_DATA, "agents", agent_name)
    os.makedirs(cwd, exist_ok=True)

    _register_agent_from_channel(channel, cwd)
    session = agents.agents[agent_name]

    agents.fire_and_forget(_set_channel_topic(channel, cwd, session.system_prompt_hash))
    await agents.send_system(
        channel,
        f"Agent **{agent_name}** registered (sleeping). Send a message to wake it.\n"
        f"Working directory: `{cwd}`",
    )
    log.info("Auto-registered agent '%s' from manually created Active channel", agent_name)


def _create_worktree(name: str) -> str | None:
    """Create a git worktree for an axi-dev agent. Returns worktree path or None on failure."""
    worktree_path = os.path.join(config.BOT_WORKTREES_DIR, name)

    if os.path.isdir(worktree_path):
        # Check if it's already a valid git worktree
        git_marker = os.path.join(worktree_path, ".git")
        if os.path.exists(git_marker):
            log.info("Reusing existing worktree for '%s' at %s", name, worktree_path)
            return worktree_path
        # Directory exists but isn't a worktree — conflict
        log.warning("Directory exists at %s but is not a git worktree", worktree_path)
        return None

    branch = f"feature/{name}"
    result = subprocess.run(
        ["git", "-C", config.BOT_DIR, "worktree", "add", worktree_path, "-b", branch],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Branch might already exist, try without -b
        result = subprocess.run(
            ["git", "-C", config.BOT_DIR, "worktree", "add", worktree_path, branch],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.warning("Failed to create worktree for '%s': %s", name, result.stderr.strip())
            return None
        log.info("Created worktree for '%s' at %s (existing branch)", name, worktree_path)
    else:
        log.info("Created worktree for '%s' at %s", name, worktree_path)

    return worktree_path


async def _handle_axi_channel_create(channel: TextChannel) -> None:
    """Handle a channel created in the Axi category — codebase dev agent with worktree."""
    agent_name = channel.name
    worktree_path = _create_worktree(agent_name)

    if worktree_path is None:
        await agents.send_system(
            channel,
            f"Failed to create worktree for **{agent_name}**. "
            f"A non-worktree directory may already exist at `{config.BOT_WORKTREES_DIR}/{agent_name}`, "
            f"or git worktree creation failed. Check logs.",
        )
        return

    _register_agent_from_channel(channel, worktree_path)
    session = agents.agents[agent_name]

    agents.fire_and_forget(_set_channel_topic(channel, worktree_path, session.system_prompt_hash))
    await agents.send_system(
        channel,
        f"Agent **{agent_name}** registered (sleeping) with worktree.\n"
        f"Working directory: `{worktree_path}`",
    )
    log.info("Auto-registered axi-dev agent '%s' from manually created Axi channel", agent_name)


@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel) -> None:
    """Auto-register agent when a user manually creates a channel in Axi or Active category."""
    if not isinstance(channel, discord.TextChannel):
        return
    if channel.name in channels.bot_creating_channels:
        return
    if channel.name == agents.normalize_channel_name(config.MASTER_AGENT_NAME):
        return

    if channels.axi_category and channel.category_id == channels.axi_category.id:
        await _handle_axi_channel_create(channel)
    elif channels.active_category and channel.category_id == channels.active_category.id:
        await _handle_active_channel_create(channel)

    # Ensure master stays at top after any channel creation
    if _startup_complete:
        try:
            await channels.ensure_master_channel_position()
        except Exception:
            log.exception("Failed to re-enforce master channel position after channel create")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    """Clean up agent state when a channel is deleted."""
    if not isinstance(channel, discord.TextChannel):
        return

    agent_name = agents.channel_to_agent.pop(channel.id, None)
    if agent_name is None:
        return

    session = agents.agents.pop(agent_name, None)
    if session is None:
        log.info("Channel #%s deleted — removed stale mapping for '%s'", channel.name, agent_name)
        return

    if agents.is_awake(session):
        try:
            await agents.sleep_agent(session, force=True)
        except Exception:
            log.exception("Error sleeping agent '%s' during channel deletion", agent_name)

    log.info("Channel #%s deleted — agent '%s' cleaned up (cwd=%s)", channel.name, agent_name, session.cwd)


@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
    """Re-enforce #axi-master position when channels are reordered."""
    if not _startup_complete:
        return
    if after.guild.id != config.DISCORD_GUILD_ID:
        return
    # Only care about position or category changes
    if before.position == after.position and getattr(before, "category_id", None) == getattr(after, "category_id", None):
        return
    try:
        await channels.ensure_master_channel_position()
    except Exception:
        log.exception("Failed to re-enforce master channel position")


# ---------------------------------------------------------------------------
# Readme channel sync
# ---------------------------------------------------------------------------


async def sync_readme_channel() -> None:
    """Sync the readme channel: find or create #readme, lock permissions, update message."""
    try:
        with open(config.README_CONTENT_PATH) as f:
            readme_text = f.read().strip()
    except FileNotFoundError:
        log.debug("readme_content.md not found — skipping readme sync")
        return
    if not readme_text:
        log.debug("readme_content.md is empty — skipping readme sync")
        return

    guild = channels.target_guild
    if guild is None:
        log.warning("No guild available — skipping readme sync")
        return

    channel = None
    for ch in guild.text_channels:
        if ch.name == "readme" and ch.category is None:
            channel = ch
            break

    if channel is None:
        overwrites: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite] = {
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
        try:
            ow = channel.overwrites.copy()
            ow[guild.default_role] = discord.PermissionOverwrite(
                send_messages=False,
                view_channel=True,
                read_message_history=True,
            )
            ow[guild.me] = discord.PermissionOverwrite(
                send_messages=True,
                manage_messages=True,
                view_channel=True,
                read_message_history=True,
            )
            await channel.edit(overwrites=ow)
            log.info("Readme channel permissions synced")
        except Exception:
            log.exception("Failed to set readme channel permissions")

    existing_msg = None
    async for msg in channel.history(limit=50):
        if msg.author == bot.user:
            existing_msg = msg
            break

    if existing_msg is None:
        await channel.send(readme_text)
        log.info("Sent readme message to #%s", channel.name)
    elif existing_msg.content != readme_text:
        await existing_msg.edit(content=readme_text)
        log.info("Updated readme message in #%s", channel.name)
    else:
        log.info("Readme message in #%s already up to date", channel.name)


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------


def _load_master_session_data() -> tuple[str | None, str | None]:
    """Load master session_id and prompt_hash from disk."""
    try:
        if os.path.isfile(config.MASTER_SESSION_PATH):
            with open(config.MASTER_SESSION_PATH) as f:
                raw = f.read().strip()
            if raw:
                if raw.startswith("{"):
                    data = json.loads(raw)
                    resume_id = data.get("session_id")
                    prompt_hash = data.get("prompt_hash")
                else:
                    resume_id = raw
                    prompt_hash = None
                if resume_id:
                    log.info("Loaded master session_id from %s: %s (prompt_hash=%s)", config.MASTER_SESSION_PATH, resume_id[:8], prompt_hash)
                return resume_id, prompt_hash
    except (OSError, json.JSONDecodeError):
        log.warning("Failed to read master session data", exc_info=True)
    return None, None


def _register_master_agent(resume_id: str | None, prompt_hash: str | None) -> AgentSession:
    """Create and register the master AgentSession (sleeping)."""
    master_mcp: dict[str, Any] = {"axi": tools.axi_master_mcp_server}
    master_mcp["utils"] = tools.utils_mcp_server
    master_mcp["schedule"] = make_schedule_mcp_server(
        config.MASTER_AGENT_NAME, config.SCHEDULES_PATH, config.DEFAULT_CWD,
    )
    master_mcp["playwright"] = {
        "command": "npx",
        "args": ["@playwright/mcp@latest", "--headless"],
    }
    if os.path.isdir(config.BOT_WORKTREES_DIR):
        master_mcp["discord"] = tools.discord_mcp_server
    session = AgentSession(
        name=config.MASTER_AGENT_NAME,
        agent_type="flowcoder",
        cwd=config.DEFAULT_CWD,
        system_prompt=MASTER_SYSTEM_PROMPT,
        system_prompt_hash=prompt_hash,
        client=None,
        mcp_servers=master_mcp,
        session_id=resume_id,
        compact_instructions=(
            "List of active/spawned agents and their current status. "
            "Any ongoing tasks or investigations in progress. "
            "Recent user requests that haven't been completed yet. "
            "Important context the user has shared (preferences, decisions, constraints)."
        ),
    )
    ds = discord_state(session)
    ds.todo_items = agents.load_todo_items(config.MASTER_AGENT_NAME)
    agents.agents[config.MASTER_AGENT_NAME] = session
    log.info("Master agent registered (sleeping, session_id=%s)", resume_id and resume_id[:8])
    return session


async def _setup_guild_infrastructure(master_session: AgentSession) -> None:
    """Set up Discord guild categories and master channel."""
    try:
        await agents.ensure_guild_infrastructure()
        await channels.deduplicate_master_channel()
        master_channel = await agents.ensure_agent_channel(config.MASTER_AGENT_NAME)
        discord_state(master_session).channel_id = master_channel.id
        agents.channel_to_agent[master_channel.id] = config.MASTER_AGENT_NAME
        log.info("Guild infrastructure ready (guild=%s, master_channel=#%s)", config.DISCORD_GUILD_ID, master_channel.name)

        desired_topic = "Axi master control channel"
        if master_channel.topic != desired_topic:
            log.info("Updating topic on #%s: %r -> %r", master_channel.name, master_channel.topic, desired_topic)
            await master_channel.edit(topic=desired_topic)
    except Exception:
        log.exception("Failed to set up guild infrastructure — guild channels won't work")

    try:
        await sync_readme_channel()
    except Exception:
        log.exception("Failed to sync readme channel")

    try:
        await channels.ensure_master_channel_position()
    except Exception:
        log.exception("Failed to ensure master channel position")

    try:
        await agents.reconstruct_agents_from_channels()
    except Exception:
        log.exception("Failed to reconstruct agents from channels")


def _consume_json_marker(path: str, label: str) -> dict[str, Any] | None:
    """Read and delete a JSON marker file. Returns parsed data or None."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data: dict[str, Any] = json.load(f)
        os.remove(path)
        log.info("%s marker found and consumed", label)
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read %s marker: %s", label, e)
        try:
            os.remove(path)
        except OSError:
            pass
        return None


async def _send_startup_notification(
    master_ch: TextChannel,
    rollback_info: dict[str, Any] | None,
    crash_info: dict[str, Any] | None,
    startup_elapsed: float = 0.0,
    trace_tag: str = "",
) -> None:
    """Send startup/rollback/crash notification to master channel."""
    if rollback_info:
        exit_code = rollback_info.get("exit_code", "unknown")
        uptime = rollback_info.get("uptime_seconds", "?")
        timestamp = rollback_info.get("timestamp", "unknown")
        details = rollback_info.get("rollback_details", "").strip()
        pre_commit = rollback_info.get("pre_launch_commit", "")
        crashed_commit = rollback_info.get("crashed_commit", "")

        msg_lines = [
            "*System:* **Automatic rollback performed.**",
            f"Axi crashed on startup (exit code {exit_code} after {uptime}s) at {timestamp}.",
        ]
        if details:
            msg_lines.append(f"Actions taken: {details}.")
        if pre_commit and crashed_commit and pre_commit != crashed_commit:
            msg_lines.append(f"Reverted from `{crashed_commit[:7]}` to `{pre_commit[:7]}`.")
            msg_lines.append("Reverted commits are still in the reflog: `git reflog`")
        if "stashed" in details:
            msg_lines.append("Stashed changes: `git stash list` / `git stash show -p` / `git stash pop`")
        if config.ENABLE_CRASH_HANDLER:
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
        if config.ENABLE_CRASH_HANDLER:
            crash_msg += "\nSpawning crash analysis agent..."
        await master_ch.send(crash_msg)
    await master_ch.send(f"*System:* Axi ready. ({startup_elapsed:.1f}s){trace_tag}")
    log.info("Sent restart notification to master channel")


async def _spawn_crash_handler(crash_data: dict[str, Any], is_rollback: bool) -> None:
    """Spawn a crash-handler agent to analyze a crash or rollback."""
    crash_log = crash_data.get("crash_log", "(no crash log available)")
    exit_code = crash_data.get("exit_code", "unknown")
    uptime = crash_data.get("uptime_seconds", "?")
    timestamp = crash_data.get("timestamp", "unknown")

    if is_rollback:
        details = crash_data.get("rollback_details", "").strip()
        pre_commit = crash_data.get("pre_launch_commit", "")
        crashed_commit = crash_data.get("crashed_commit", "")

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
    else:
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

    await agents.reclaim_agent_name("crash-handler")
    await agents.spawn_agent("crash-handler", config.BOT_DIR, crash_prompt)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@bot.event
async def on_ready() -> None:
    global _on_ready_fired
    set_agent_context("system")
    set_trigger("startup")
    log.info("Bot ready as %s", bot.user)

    asyncio.get_event_loop().set_exception_handler(_handle_task_exception)

    if _on_ready_fired:
        log.info("on_ready fired again (gateway reconnect) — skipping startup logic")
        return
    _on_ready_fired = True

    global _bot_start_time
    _bot_start_time = datetime.now(UTC)

    agents.init(bot)

    # Symlink extension commands into commands/ so FlowCoder can discover them
    from axi.extensions import sync_extension_commands
    sync_extension_commands()

    scheduler.init(
        max_slots=config.MAX_AWAKE_AGENTS,
        protected={config.MASTER_AGENT_NAME},
        get_agents=lambda: agents.agents,
        sleep_fn=lambda s: agents.sleep_agent(s),
    )
    init_tracing("axi-bot")

    # Re-initialize _tracer now that the provider is set up
    global _tracer
    _tracer = trace.get_tracer(__name__)

    agents.set_utils_mcp_server(tools.utils_mcp_server)

    with _tracer.start_as_current_span("on_ready.startup") as startup_span:
        _span_ctx = startup_span.get_span_context()
        _trace_tag = ""
        if _span_ctx and _span_ctx.trace_id:
            _trace_tag = f" [trace={format(_span_ctx.trace_id, '032x')[:16]}]"

        master_resume_id, master_old_prompt_hash = _load_master_session_data()
        master_session = _register_master_agent(master_resume_id, master_old_prompt_hash)
        await _setup_guild_infrastructure(master_session)

        _startup_t0 = time.monotonic()
        master_ch = await agents.get_master_channel()
        if master_ch:
            await master_ch.send(f"*System:* Axi starting up...{_trace_tag}")

        await agents.connect_procmux()
        agents.init_shutdown_coordinator()

        await bot.tree.sync()
        log.info("Slash commands synced")

        check_schedules.start()
        log.info("Schedule checker started")

        rollback_info = _consume_json_marker(config.ROLLBACK_MARKER_PATH, "Rollback")
        crash_info = _consume_json_marker(config.CRASH_ANALYSIS_MARKER_PATH, "Crash analysis")

        global _startup_complete
        _startup_complete = True

        _startup_elapsed = time.monotonic() - _startup_t0
        startup_span.set_attribute("startup.elapsed_s", _startup_elapsed)
        master_ch = await agents.get_master_channel()
        if master_ch:
            await _send_startup_notification(master_ch, rollback_info, crash_info, _startup_elapsed, _trace_tag)

    if not config.ENABLE_CRASH_HANDLER:
        if rollback_info or crash_info:
            log.info("Crash handler not enabled (set ENABLE_CRASH_HANDLER=1 to auto-spawn)")
    elif rollback_info:
        await _spawn_crash_handler(rollback_info, is_rollback=True)
    elif crash_info:
        await _spawn_crash_handler(crash_info, is_rollback=False)


def _handle_task_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Global handler for unhandled exceptions in asyncio tasks."""
    exception = context.get("exception")
    if exception:
        if type(exception).__name__ == "ProcessError" and "-15" in str(exception):
            log.debug("Suppressed expected ProcessError from SIGTERM'd subprocess")
            return
        log.error("Unhandled exception in async task: %s", context.get("message", ""), exc_info=exception)
        msg_text = context.get("message", "")
        exc_str = f"{type(exception).__name__}: {exception}"
        loop.create_task(
            agents.send_to_exceptions(f"🔥 Unhandled exception in async task:\n**{msg_text}**\n```\n{exc_str[:1500]}\n```")
        )
    else:
        log.error("Unhandled async error: %s", context.get("message", ""))


def _acquire_lock() -> Any:
    """Acquire an exclusive file lock to prevent duplicate bot instances."""
    import fcntl

    lock_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".bot.lock")
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        log.critical("Another bot.py instance is already running (could not acquire .bot.lock). Exiting.")
        raise SystemExit(1) from exc
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd


if __name__ == "__main__":
    _lock_fd = _acquire_lock()
    try:
        bot.run(config.DISCORD_TOKEN, log_handler=None)
    except Exception:
        log.exception("Bot crashed with unhandled exception")
