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

from axi import agents, channels, config, scheduler, tools, worktrees
from axi.axi_types import ActivityState, AgentSession, ConcurrencyLimitError, discord_state, tool_display
from axi.discord_wire import (
    audited_channel_send,
    audited_interaction_followup_send,
    audited_interaction_response_send,
    audited_message_edit,
    log_inbound_message,
    log_inbound_reaction,
)
from axi.log_context import set_agent_context, set_trigger
from axi.metrics import (
    make_discord_http_trace_config,
    observe_agent_message_event,
    observe_inbound_discord_event,
    set_agent_sessions_provider,
    set_scheduler_status_provider,
)
from axi.prompts import (
    MASTER_SYSTEM_PROMPT,
    compute_prompt_hash,
    make_spawned_agent_system_prompt,
)
from axi.schedule_tools import (
    append_history,
    check_skip,
    load_schedules,
    make_schedule_mcp_server,
    prune_history,
    prune_skips,
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
bot.http.http_trace = make_discord_http_trace_config("discordpy")


@bot.tree.interaction_check  # type: ignore[arg-type]
async def global_auth_check(interaction: discord.Interaction[Any]) -> bool:
    """Reject all slash commands from non-authorized users."""
    if interaction.user.id not in config.ALLOWED_USER_IDS:
        await audited_interaction_response_send(interaction,"Not authorized.", ephemeral=True)
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
    observe_inbound_discord_event("reaction_add", "received")
    # Ignore own reactions (bot pre-adding them)
    if bot.user and payload.user_id == bot.user.id:
        observe_inbound_discord_event("reaction_add", "ignored_self")
        return

    log_inbound_reaction(payload)

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


async def _replace_latest_queued_user_message(
    session: AgentSession,
    content: Any,
    channel: TextChannel,
    message: discord.Message,
    raw_content: Any,
) -> int:
    """For axi-master, keep only the latest queued user message while busy."""
    dropped = 0
    while session.message_queue:
        _, _, dropped_msg, *_ = session.message_queue.popleft()
        await agents.remove_reaction(dropped_msg, "📨")
        dropped += 1
    session.message_queue.append((content, channel, message, raw_content))
    return dropped


@bot.event
async def on_message(message: discord.Message) -> None:
    """Handle incoming Discord messages."""
    observe_inbound_discord_event("message", "received")
    if not _startup_complete:
        observe_inbound_discord_event("message", "ignored_startup")
        return

    # Dedup: Discord may deliver the same message twice on gateway reconnects
    if message.id in _seen_message_ids:
        observe_inbound_discord_event("message", "deduplicated")
        log.warning("DEDUP[%s] duplicate on_message delivery — skipping", message.id)
        return
    _seen_message_ids[message.id] = None
    while len(_seen_message_ids) > _DEDUP_CAPACITY:
        _seen_message_ids.popitem(last=False)

    # --- Authorization and channel checks ---
    if bot.user is not None and message.author.id == bot.user.id:
        observe_inbound_discord_event("message", "ignored_self")
        return
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        observe_inbound_discord_event("message", "ignored_type")
        return
    if message.author.bot and message.author.id not in config.ALLOWED_USER_IDS:
        observe_inbound_discord_event("message", "ignored_bot")
        return

    # DM messages — redirect to guild
    if message.channel.type == ChannelType.private:
        if message.author.id not in config.ALLOWED_USER_IDS:
            observe_inbound_discord_event("message", "ignored_dm_unauthorized")
            return
        master_session = agents.get_master_session()
        if master_session and discord_state(master_session).channel_id:
            await audited_channel_send(
                message.channel,
                f"*System:* Please use <#{discord_state(master_session).channel_id}> in the server instead.",
                operation="dm.redirect",
            )
        else:
            await audited_channel_send(
                message.channel,
                "*System:* Please use the server channels instead.",
                operation="dm.redirect",
            )
        return

    # Guild messages — only process in our target guild
    if message.guild is None or message.guild.id != config.DISCORD_GUILD_ID:
        observe_inbound_discord_event("message", "ignored_other_guild")
        return
    if not isinstance(message.channel, TextChannel):
        observe_inbound_discord_event("message", "ignored_non_text")
        return
    if message.author.id not in config.ALLOWED_USER_IDS:
        observe_inbound_discord_event("message", "ignored_unauthorized")
        return

    channel = message.channel

    # Track channel activity for recency reordering
    channels.mark_channel_active(channel.id)

    # --- Get content and look up agent ---
    content = await agents.extract_message_content(message)
    log_inbound_message(message)

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
        observe_inbound_discord_event("message", "ignored_shutdown")
        await agents.send_system(channel, "Bot is restarting — not accepting new messages.")
        return

    if agent_name is None:
        observe_inbound_discord_event("message", "ignored_unmapped_channel")
        return

    session = agents.agents.get(agent_name)
    if session is None:
        observe_inbound_discord_event("message", "ignored_missing_session")
        if channels.is_killed_channel(channel):
            await agents.send_system(
                channel,
                "This agent has been killed. Ask the master agent to spawn a new one.",
            )
        return

    # Block killed agents
    if channels.is_killed_channel(channel):
        observe_inbound_discord_event("message", "ignored_killed_channel")
        await agents.send_system(
            channel,
            "This agent has been killed. Use `/spawn` to create a new one.",
        )
        return

    ds = discord_state(session)

    # --- Text command handling ---
    if message.content.strip().startswith("/"):
        handled = await _handle_text_command(message, session, agent_name)
        if handled:
            return

    # --- Plan approval gate ---
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

    # --- Centralized message processing via Axi hub wrapper ---
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

    raw_content = content
    content = agents.wrap_content_with_flowchart(content, session)

    if agents.hub and agents.hub.shutdown_requested:
        await agents.send_system(channel, "Bot is restarting — not accepting new messages.")
        result_status = "shutdown"
    elif session.reconnecting:
        observe_agent_message_event("queue_enqueued_reconnecting")
        session.message_queue.append((content, channel, message, raw_content))
        position = len(session.message_queue)
        await agents.send_system(
            channel,
            f"Agent **{session.name}** is reconnecting — message queued (position {position}).",
        )
        result_status = "queued_reconnecting"
    elif session.query_lock.locked():
        replaced = 0
        if session.name == "axi-master":
            replaced = await _replace_latest_queued_user_message(session, content, channel, message, raw_content)
        else:
            session.message_queue.append((content, channel, message, raw_content))
        observe_agent_message_event("queue_enqueued_busy")
        position = len(session.message_queue)
        if session.compacting:
            detail = " Replaced older queued message." if replaced else ""
            await agents.send_system(
                channel,
                f"🔄 Agent **{session.name}** is compacting context — message queued (position {position}). Will process after compaction completes.{detail}",
            )
        else:
            activity = session.activity
            tool_suffix = ""
            if activity.phase == "waiting" and activity.tool_name:
                tool_suffix = f" (currently {tool_display(activity.tool_name)})"
            await _interrupt_agent(session)
            detail = " Replaced older queued message." if replaced else ""
            await agents.send_system(
                channel,
                f"Agent **{session.name}** is busy — message queued (position {position}). Interrupting current task.{tool_suffix}{detail}",
            )
        result_status = "queued"
    else:
        agents.scheduler.mark_interactive(session.name)
        async with session.query_lock:
            ready = True
            if not agents.is_awake(session):
                try:
                    await agents.wake_agent(session)
                except ConcurrencyLimitError:
                    observe_agent_message_event("queue_enqueued_slot_wait")
                    session.message_queue.append((content, channel, message, raw_content))
                    awake = agents.count_awake_agents()
                    await agents.send_system(
                        channel,
                        f"⏳ All {awake} agent slots busy. Message queued — will run when a slot opens.",
                    )
                    result_status = "queued"
                    ready = False
                except Exception:
                    log.exception("Failed to wake agent '%s' for user message", session.name)
                    await agents.send_system(channel, f"Failed to wake agent **{session.name}**.")
                    result_status = "error"
                    ready = False
            if ready:
                try:
                    await agents.process_message(session, content, channel)
                    result_status = "processed"
                except RuntimeError as e:
                    log.warning("Runtime error for '%s': %s", session.name, e)
                    await agents.send_system(channel, str(e))
                    result_status = "error"

    _RESULT_REACTIONS = {
        "processed": "✅",
        "queued": "📨",
        "queued_reconnecting": "📨",
        "timeout": "⏳",
    }
    reaction = _RESULT_REACTIONS.get(result_status, "❌")
    if result_status != "shutdown":
        await agents.add_reaction(message, reaction)

    if result_status == "processed":
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

                    if check_skip(skey):
                        log.info("Skipping recurring event (one-off skip): %s", name)
                        continue

                    set_agent_context(entry.get("session") or entry.get("owner") or name)
                    set_trigger("schedule", name=name)
                    log.info("Firing recurring event: %s", name)
                    _tracer.start_span("schedule.fire", attributes={"schedule.name": name, "schedule.type": "recurring"}).end()
                    agent_name = entry.get("session") or entry.get("owner") or name
                    agent_cwd = entry.get("cwd", os.path.join(config.AXI_USER_DATA, "agents", agent_name))

                    sched_ch = await agents.get_agent_channel(agent_name) if agent_name in agents.agents else None
                    if sched_ch:
                        await audited_channel_send(sched_ch, f"*System:* 📅 Scheduled: `{name}`", operation="schedule.fire")

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
                    set_agent_context(entry.get("session") or entry.get("owner") or name)
                    set_trigger("schedule_one_off", name=name)
                    log.info("Firing one-off event: %s", name)
                    _tracer.start_span("schedule.fire", attributes={"schedule.name": name, "schedule.type": "one_off"}).end()
                    agent_name = entry.get("session") or entry.get("owner") or name
                    agent_cwd = entry.get("cwd", os.path.join(config.AXI_USER_DATA, "agents", agent_name))

                    sched_ch = await agents.get_agent_channel(agent_name) if agent_name in agents.agents else None
                    if sched_ch:
                        await audited_channel_send(
                            sched_ch,
                            f"*System:* 📅 Scheduled (one-off): `{name}`",
                            operation="schedule.fire_one_off",
                        )

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
        if ds.channel_id:
            ch = bot.get_channel(ds.channel_id)
            if isinstance(ch, TextChannel) and channels.is_killed_channel(ch):
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
                content, ch, stranded_msg, *_ = session.message_queue.popleft()
                log.info("Stranded message found for sleeping agent '%s', waking", _agent_name)
                await agents.remove_reaction(stranded_msg, "📨")
                agents.fire_and_forget(agents.run_initial_prompt(session, content, ch))
                break


async def _auto_sleep_idle_agents(now_utc: datetime) -> None:
    """Put idle awake agents to sleep after inactivity.

    Under concurrency pressure (at MAX_AWAKE_AGENTS), sleep idle agents immediately.
    Otherwise wait IDLE_SLEEP_SECONDS.
    """
    awake_count = agents.count_awake_agents()
    under_pressure = awake_count >= config.MAX_AWAKE_AGENTS
    idle_threshold = timedelta(seconds=0) if under_pressure else timedelta(seconds=config.IDLE_SLEEP_SECONDS)
    if under_pressure:
        log.info("Concurrency pressure: %d/%d awake agents — aggressive idle sleep", awake_count, config.MAX_AWAKE_AGENTS)

    for agent_name, session in list(agents.agents.items()):
        if session.client is None:
            continue
        if session.query_lock.locked():
            continue
        if session.bridge_busy:
            continue
        idle_duration = now_utc - session.last_activity
        if idle_duration > idle_threshold:
            log.info("Auto-sleeping idle agent '%s' (idle %.0fs, pressure=%s)", agent_name, idle_duration.total_seconds(), under_pressure)
            try:
                await agents.sleep_agent(session)
            except Exception:
                log.exception("Error auto-sleeping agent '%s'", agent_name)


@tasks.loop(seconds=10)
async def check_schedules() -> None:
    if agents.shutdown_coordinator and agents.shutdown_coordinator.requested:
        return

    prune_history()
    prune_skips()

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
    if session.dispatch_lock.locked():
        log.info("Interrupt already in flight for '%s' — skipping duplicate request", session.name)
        return
    async with session.dispatch_lock:
        if await agents.graceful_interrupt(session):
            return
        # Graceful interrupt failed — fall back to killing the CLI process
        log.warning("Graceful interrupt failed for '%s', falling back to process kill", session.name)
        await agents.interrupt_session(session)


# ---------------------------------------------------------------------------
# Slash command error handler
# ---------------------------------------------------------------------------


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """Log slash command errors to our logger (discord.py's default goes to its own silent logger)."""
    command_name = interaction.command.name if interaction.command else "unknown"
    log.error("Slash command /%s error: %s", command_name, error, exc_info=error)
    if not interaction.response.is_done():
        await audited_interaction_response_send(interaction,
            f"*System:* Command failed: {error}", ephemeral=True
        )


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
            await audited_interaction_response_send(interaction,
                "Could not determine agent for this channel. Specify an agent name.", ephemeral=True
            )
            return None

    session = agents.agents.get(agent_name)
    if session is None:
        await audited_interaction_response_send(interaction,f"Agent **{agent_name}** not found.", ephemeral=True)
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
    bridge_conn = agents.procmux_conn
    if bridge_conn is not None and bridge_conn.is_alive:
        try:
            result = await bridge_conn.send_command("status")
            if result.ok and result.uptime_seconds is not None:
                procmux_str = _fmt_uptime(result.uptime_seconds)
        except Exception:
            procmux_str = "error"

    latency = round(bot.latency * 1000)
    parts = [f"Pong! Latency: {latency}ms", f"Bot uptime: {bot_str}"]
    if procmux_str is not None:
        parts.append(f"Bridge uptime: {procmux_str}")
    elif bridge_conn is None or not bridge_conn.is_alive:
        parts.append("Bridge: not connected")
    await audited_interaction_response_send(interaction," | ".join(parts))


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
        await audited_interaction_response_send(interaction,"\n".join(lines))
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

    await audited_interaction_response_send(interaction,"\n".join(lines))


@bot.tree.command(name="model", description="Get or set the LLM model for this agent or future spawned agents.")
@app_commands.describe(name="Model name (for example: opus, sonnet, haiku, gpt-5.4) — omit to view current")
async def model_command(interaction: discord.Interaction, name: str | None = None) -> None:
    log.info("Slash command /model name=%s from %s", name, interaction.user)

    if name is None:
        agent_name = agents.channel_to_agent.get(interaction.channel_id or 0)
        if agent_name == config.MASTER_AGENT_NAME:
            agent_name = None
        if agent_name and agent_name in agents.agents:
            session = agents.agents[agent_name]
            current = session.model or config.get_model()
            await audited_interaction_response_send(
                interaction, f"Current model for **{agent_name}**: **{current}**"
            )
        else:
            current = config.get_model()
            await audited_interaction_response_send(interaction, f"Current default model: **{current}**")
        return

    error = config.validate_model(name)
    if error:
        await audited_interaction_response_send(interaction, f"*System:* {error}", ephemeral=True)
        return

    normalized = config.normalize_model(name)
    agent_name = agents.channel_to_agent.get(interaction.channel_id or 0)
    if agent_name == config.MASTER_AGENT_NAME:
        agent_name = None
    if agent_name and agent_name in agents.agents:
        session = agents.agents[agent_name]
        session.model = normalized
        agent_cfg = agents._load_agent_config(agent_name)
        saved_ext = agent_cfg.get("extensions")
        agents._save_agent_config(
            agent_name,
            session.mcp_server_names,
            extensions=saved_ext,
            model=normalized,
        )
        await interaction.response.defer()
        await agents.reset_session(agent_name)
        await audited_interaction_followup_send(
            interaction,
            f"*System:* Agent **{agent_name}** switched to **{normalized}** and restarted with a fresh session.",
        )
        channel = await agents.get_agent_channel(agent_name)
        if channel is not None and channel.id != interaction.channel_id:
            await agents.send_system(
                channel,
                f"Agent **{agent_name}** switched to **{normalized}** and restarted with a fresh session.",
            )
        return

    error = config.set_model(normalized)
    if error:
        await audited_interaction_response_send(interaction, f"*System:* {error}", ephemeral=True)
    else:
        await audited_interaction_response_send(interaction, f"*System:* Default model set to **{config.get_model()}**.")


@bot.tree.command(name="list-agents", description="List all active agent sessions.")
async def list_agents(interaction: discord.Interaction) -> None:
    log.info("Slash command /list-agents from %s", interaction.user)


    if not agents.agents:
        await audited_interaction_response_send(interaction,"No active agents.", ephemeral=True)
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
        if ds.channel_id:
            ch = bot.get_channel(ds.channel_id)
            if isinstance(ch, TextChannel) and channels.is_killed_channel(ch):
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
    full_text = header + "\n".join(lines)
    if len(full_text) <= 2000:
        await audited_interaction_response_send(interaction, full_text)
    else:
        from discordquery import split_message

        parts = split_message(full_text)
        await audited_interaction_response_send(interaction, parts[0])
        for part in parts[1:]:
            await audited_interaction_followup_send(interaction, part)


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
        await audited_interaction_response_send(interaction,f"Agent **{agent_name}** not found.", ephemeral=True)
        return

    await audited_interaction_response_send(interaction,_format_agent_status(agent_name, session), ephemeral=True)


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
        await audited_interaction_response_send(interaction,"No active agents.", ephemeral=True)
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

    await audited_interaction_response_send(interaction,f"*System:* {header}\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="verbose", description="Toggle verbose output (tool calls, thinking) for an agent.")
@app_commands.describe(mode="on / off / omit to toggle")
async def verbose_command(interaction: discord.Interaction, mode: str | None = None) -> None:
    log.info("Slash command /verbose mode=%s from %s", mode, interaction.user)

    resolved = await _resolve_agent(interaction, None)
    if resolved is None:
        return
    agent_name, session = resolved

    if mode is not None:
        mode_lower = mode.strip().lower()
        if mode_lower == "on":
            discord_state(session).verbose = True
        elif mode_lower == "off":
            discord_state(session).verbose = False
        else:
            await audited_interaction_response_send(interaction,
                "Usage: `/verbose` (toggle), `/verbose on`, `/verbose off`", ephemeral=True
            )
            return
    else:
        discord_state(session).verbose = not discord_state(session).verbose

    state = "on" if discord_state(session).verbose else "off"
    await audited_interaction_response_send(interaction,f"*System:* Verbose output **{state}** for **{agent_name}**.")


@bot.tree.command(name="debug", description="Toggle debug output (stderr) for an agent.")
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
            await audited_interaction_response_send(interaction,
                "Usage: `/debug` (toggle), `/debug on`, `/debug off`", ephemeral=True
            )
            return
    else:
        discord_state(session).debug = not discord_state(session).debug

    state = "on" if discord_state(session).debug else "off"
    await audited_interaction_response_send(interaction,f"*System:* Debug output **{state}** for **{agent_name}**.")


@bot.tree.command(name="debug-all", description="Toggle debug output (stderr) for ALL agents.")
async def debug_all_command(interaction: discord.Interaction, mode: str | None = None) -> None:
    log.info("Slash command /debug-all mode=%s from %s", mode, interaction.user)

    if mode is not None:
        mode_lower = mode.strip().lower()
        if mode_lower == "on":
            new_state = True
        elif mode_lower == "off":
            new_state = False
        else:
            await audited_interaction_response_send(interaction,
                "Usage: `/debug-all` (toggle), `/debug-all on`, `/debug-all off`", ephemeral=True
            )
            return
    else:
        on_count = sum(1 for s in agents.agents.values() if discord_state(s).debug)
        new_state = on_count <= len(agents.agents) // 2

    for session in agents.agents.values():
        discord_state(session).debug = new_state

    state = "on" if new_state else "off"
    await audited_interaction_response_send(interaction,
        f"*System:* Debug output **{state}** for all **{len(agents.agents)}** agents."
    )


@bot.tree.command(name="kill-agent", description="Terminate an agent session.")
@app_commands.autocomplete(agent_name=killable_agent_autocomplete)
async def kill_agent(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /kill-agent %s from %s", agent_name, interaction.user)

    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, session = resolved

    if agent_name == config.MASTER_AGENT_NAME:
        await audited_interaction_response_send(interaction,"Cannot kill the axi-master session.", ephemeral=True)
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
        await audited_interaction_followup_send(
            interaction,
            f"*System:* Agent **{agent_name}** moved to Killed.\nSession ID: `{session_id}` — use this to resume later.",
        )
    else:
        await audited_interaction_followup_send(interaction, f"*System:* Agent **{agent_name}** moved to Killed.")


@bot.tree.command(name="spawn", description="Spawn a new agent session with its own Discord channel.")
@app_commands.describe(
    model="Optional model override (for example: opus, sonnet, haiku, gpt-5.4)"
)
async def spawn_agent_cmd(
    interaction: discord.Interaction,
    name: str,
    prompt: str,
    cwd: str | None = None,
    resume: str | None = None,
    model: str | None = None,
) -> None:
    log.info("Slash command /spawn %s from %s", name, interaction.user)
    if interaction.user.id not in config.ALLOWED_USER_IDS:
        await audited_interaction_response_send(interaction,"Not authorized.", ephemeral=True)
        return

    agent_name = name.strip()
    if not agent_name:
        await audited_interaction_response_send(interaction,"Agent name cannot be empty.", ephemeral=True)
        return
    if agent_name == config.MASTER_AGENT_NAME:
        await audited_interaction_response_send(interaction,
            f"Cannot spawn agent with reserved name '{config.MASTER_AGENT_NAME}'.", ephemeral=True
        )
        return
    if agent_name in agents.agents and not resume:
        await audited_interaction_response_send(interaction,
            f"Agent **{agent_name}** already exists. Kill it first or use `resume` to replace it.", ephemeral=True
        )
        return

    default_cwd = os.path.join(config.AXI_USER_DATA, "agents", agent_name)
    agent_cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else default_cwd
    agent_model = config.normalize_model(model) if model else None

    if agent_model:
        error = config.validate_model(agent_model)
        if error:
            await interaction.response.send_message(f"*System:* {error}", ephemeral=True)
            return

    if not any(agent_cwd == d or agent_cwd.startswith(d + os.sep) for d in config.ALLOWED_CWDS):
        await audited_interaction_response_send(interaction,
            "Error: cwd is not in allowed directories.", ephemeral=True
        )
        return

    await interaction.response.defer()

    async def _do_spawn():
        try:
            if agent_name in agents.agents and resume:
                await agents.reclaim_agent_name(agent_name)
            await agents.spawn_agent(agent_name, agent_cwd, prompt, resume=resume, model=agent_model)
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
    model_suffix = f" using **{agent_model}**" if agent_model else ""
    await audited_interaction_followup_send(
        interaction,
        f"*System:* Spawning agent **{agent_name}** in `{agent_cwd}`{model_suffix}...",
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
        await audited_interaction_response_send(interaction,
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

    await audited_interaction_followup_send(
        interaction,
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
        await audited_interaction_response_send(interaction,f"Agent **{agent_name}** is not busy.", ephemeral=True)
        return

    await interaction.response.defer()
    trace_tag = agents.get_active_trace_tag(agent_name)
    _tracer.start_span(
        "slash.stop_agent",
        attributes={"agent.name": agent_name, "interrupted.trace_tag": trace_tag},
    ).end()

    try:
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
        session.state.stop_requested = True
        while session.message_queue:
            _, _, dropped_msg, *_ = session.message_queue.popleft()
            await agents.remove_reaction(dropped_msg, "📨")
            cleared += 1

        await _interrupt_agent(session)

        parts = [f"*System:* Interrupt signal sent to **{agent_name}**."]
        if cleared:
            parts.append(f"Cleared {cleared} queued message{'s' if cleared != 1 else ''}.")
        if plan_was_active:
            parts.append("Plan mode deactivated.")
        if trace_tag:
            parts.append(f"\n-# Interrupted turn {trace_tag}")
        await audited_interaction_followup_send(interaction, " ".join(parts))
    except Exception as e:
        log.exception("Failed to interrupt agent '%s'", agent_name)
        await audited_interaction_followup_send(interaction, f"Failed to interrupt **{agent_name}**: {e}")


@bot.tree.command(name="skip", description="Interrupt the current query but keep processing queued messages.")
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def skip_agent(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /skip agent=%s from %s", agent_name, interaction.user)

    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, session = resolved

    if session.client is None or not session.query_lock.locked():
        await audited_interaction_response_send(interaction,f"Agent **{agent_name}** is not busy.", ephemeral=True)
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
            noun = "message" if queued == 1 else "messages"
            msg = (
                f"*System:* Skipped current query for **{agent_name}**{tool_suffix}. "
                f"Latest {noun} will continue processing."
            )
        else:
            msg = f"*System:* Skipped current query for **{agent_name}**{tool_suffix}. No queued messages."
        if trace_tag:
            msg += f"\n-# Skipped turn {trace_tag}"
        await audited_interaction_followup_send(interaction,msg)
    except Exception as e:
        log.exception("Failed to interrupt agent '%s'", agent_name)
        await audited_interaction_followup_send(interaction,f"Failed to skip **{agent_name}**: {e}")


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
            await audited_interaction_response_send(interaction,
                f"Failed to set plan mode for **{agent_name}**: {e}", ephemeral=True
            )
            return

    if new_mode:
        await audited_interaction_response_send(interaction,
            f"📋 **Plan mode ON** for **{agent_name}** — next query will plan before implementing."
        )
    else:
        await audited_interaction_response_send(interaction,
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
    await audited_interaction_followup_send(interaction,f"*System:* Context reset for **{agent_name}**. Working directory: `{session.cwd}`")


# ---------------------------------------------------------------------------
# Text command handler (// prefix)
# ---------------------------------------------------------------------------


async def _handle_text_command(message: discord.Message, session: AgentSession, agent_name: str) -> bool:
    """Handle single-slash text commands from Discord messages. Returns True if handled."""
    text = message.content.strip()
    if not text.startswith("/"):
        return False

    parts = text[1:].split(None, 1)
    if not parts:
        return False

    cmd = parts[0].lower()
    allowed_commands = {"verbose", "debug", "status", "todo", "clear", "compact", "flowchart", "skip", "stop"}
    if cmd not in allowed_commands:
        return False
    cmd_args = parts[1].strip() if len(parts) > 1 else None
    assert isinstance(message.channel, TextChannel)
    channel = message.channel
    _tracer.start_span("text_command", attributes={"command": cmd, "agent.name": agent_name}).end()

    if cmd == "verbose":
        if cmd_args is not None:
            mode_lower = cmd_args.lower()
            if mode_lower == "on":
                discord_state(session).verbose = True
            elif mode_lower == "off":
                discord_state(session).verbose = False
            else:
                await agents.send_system(channel, "Usage: `/verbose` (toggle), `/verbose on`, `/verbose off`")
                return True
        else:
            discord_state(session).verbose = not discord_state(session).verbose
        state = "on" if discord_state(session).verbose else "off"
        await agents.send_system(channel, f"Verbose output **{state}** for **{agent_name}**.")
        return True

    if cmd == "debug":
        if cmd_args is not None:
            mode_lower = cmd_args.lower()
            if mode_lower == "on":
                discord_state(session).debug = True
            elif mode_lower == "off":
                discord_state(session).debug = False
            else:
                await agents.send_system(channel, "Usage: `/debug` (toggle), `/debug on`, `/debug off`")
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
            await agents.send_system(channel, "Usage: `/flowchart <command> [args]`")
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
                noun = "message" if queued == 1 else "messages"
                msg = (
                    f"Skipped current query for **{agent_name}**{tool_suffix}. "
                    f"Latest {noun} will continue processing."
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
            session.state.stop_requested = True
            while session.message_queue:
                _, _, dropped_msg, *_ = session.message_queue.popleft()
                await agents.remove_reaction(dropped_msg, "📨")
                cleared += 1

            await _interrupt_agent(session)

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
        await audited_interaction_response_send(interaction,f"Agent **{agent_name}** is busy.", ephemeral=True)
        return

    await interaction.response.defer()

    async with session.query_lock:
        if not agents.is_awake(session):
            try:
                await agents.wake_agent(session)
            except Exception:
                log.exception("Failed to wake agent '%s'", agent_name)
                await audited_interaction_followup_send(interaction,f"Failed to wake agent **{agent_name}**.")
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
            await audited_interaction_followup_send(interaction,f"*System:* {label} for **{agent_name}**.")
        except TimeoutError:
            await audited_interaction_followup_send(interaction,f"*System:* {label} timed out for **{agent_name}**.")
        except Exception as e:
            log.exception("Failed to %s agent '%s'", label.lower(), agent_name)
            await audited_interaction_followup_send(interaction,f"Failed to {label.lower()} **{agent_name}**: {e}")
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
# Build user profile interview
# ---------------------------------------------------------------------------


async def _run_profile_interview(session: AgentSession, channel: TextChannel) -> None:
    """Inject build_user_profile.md into the agent so Claude conducts the profile interview."""
    interview_path = os.path.join(config.BOT_DIR, ".claude", "commands", "build_user_profile.md")

    try:
        with open(interview_path) as f:
            interview_instructions = f.read()
    except FileNotFoundError:
        await audited_channel_send(
            channel,
            "*System:* Could not find `build_user_profile.md`. Cannot start profile interview.",
            operation="profile_interview.error",
        )
        return
    except OSError as e:
        await audited_channel_send(
            channel,
            f"*System:* Error reading build_user_profile.md: {e}",
            operation="profile_interview.error",
        )
        return

    # Expand %(axi_user_data)s in instructions
    interview_instructions = interview_instructions.replace("%(axi_user_data)s", config.AXI_USER_DATA)

    query = (
        "The user has triggered the profile interview via Discord. "
        "Please conduct the interview now, following the instructions below exactly.\n\n"
        "--- PROFILE INTERVIEW INSTRUCTIONS ---\n\n"
        f"{interview_instructions}"
    )

    log.info("Starting profile interview for agent '%s'", session.name)
    assert session.client is not None
    await session.client.query(agents.as_stream(query))
    await agents.stream_with_retry(session, channel)


@bot.tree.command(
    name="build-user-profile",
    description="Conversational interview to build your user profile. Infers agent from current channel.",
)
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def build_user_profile_cmd(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /build-user-profile agent=%s from %s", agent_name, interaction.user)

    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, session = resolved

    if session.query_lock.locked():
        await audited_interaction_response_send(interaction,
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
                await audited_interaction_followup_send(interaction,f"Failed to wake agent **{agent_name}**.")
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
                await _run_profile_interview(session, ch)
            await audited_interaction_followup_send(interaction,f"*System:* Profile interview complete for **{agent_name}**.")
        except TimeoutError:
            await audited_interaction_followup_send(interaction,f"*System:* Profile interview timed out for **{agent_name}**.")
        except Exception as e:
            log.exception("Failed to run profile interview for agent '%s'", agent_name)
            await audited_interaction_followup_send(interaction,f"Failed to start profile interview for **{agent_name}**: {e}")
        finally:
            session.activity = ActivityState(phase="idle")


# ---------------------------------------------------------------------------
# Music preferences interview
# ---------------------------------------------------------------------------


async def _run_music_prefs_interview(session: AgentSession, channel: TextChannel) -> None:
    """Inject build_music_preferences.md into the agent so Claude conducts the interview."""
    interview_path = os.path.join(config.BOT_DIR, ".claude", "commands", "build_music_preferences.md")
    prefs_path = os.path.join(config.AXI_USER_DATA, "profile", "refs", "music-preferences.md")

    try:
        with open(interview_path) as f:
            interview_instructions = f.read()
    except FileNotFoundError:
        await audited_channel_send(
            channel,
            "*System:* Could not find `build_music_preferences.md`. Cannot start interview.",
            operation="music_prefs_interview.error",
        )
        return
    except OSError as e:
        await audited_channel_send(
            channel,
            f"*System:* Error reading build_music_preferences.md: {e}",
            operation="music_prefs_interview.error",
        )
        return

    # Expand %(axi_user_data)s in instructions
    interview_instructions = interview_instructions.replace("%(axi_user_data)s", config.AXI_USER_DATA)

    query = (
        "The user has triggered the music preferences interview via Discord. "
        "Please conduct the interview now, following the instructions below exactly. "
        f"Write results to `{prefs_path}` as you go.\n\n"
        "--- MUSIC PREFERENCES INTERVIEW INSTRUCTIONS ---\n\n"
        f"{interview_instructions}"
    )

    log.info("Starting music preferences interview for agent '%s'", session.name)
    assert session.client is not None
    await session.client.query(agents.as_stream(query))
    await agents.stream_with_retry(session, channel)


@bot.tree.command(
    name="build-music-preferences",
    description="Interactive music preferences interview — builds your listening profile for auto-dj.",
)
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def build_music_preferences_cmd(interaction: discord.Interaction, agent_name: str | None = None) -> None:
    log.info("Slash command /build-music-preferences agent=%s from %s", agent_name, interaction.user)

    resolved = await _resolve_agent(interaction, agent_name)
    if resolved is None:
        return
    agent_name, session = resolved

    if session.query_lock.locked():
        await audited_interaction_response_send(interaction,
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
                await audited_interaction_followup_send(interaction,f"Failed to wake agent **{agent_name}**.")
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
                await _run_music_prefs_interview(session, ch)
            await audited_interaction_followup_send(interaction,f"*System:* Music preferences interview complete for **{agent_name}**.")
        except TimeoutError:
            await audited_interaction_followup_send(interaction,f"*System:* Music preferences interview timed out for **{agent_name}**.")
        except Exception as e:
            log.exception("Failed to run music preferences interview for agent '%s'", agent_name)
            await audited_interaction_followup_send(interaction,f"Failed to run music preferences interview for **{agent_name}**: {e}")
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
        await audited_interaction_response_send(interaction,
            "Flowcharts are only available for **flowcoder** agents.", ephemeral=True
        )
        return

    if session.query_lock.locked():
        await audited_interaction_response_send(interaction,
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

    await audited_interaction_followup_send(interaction,f"*System:* Flowchart `{fc_name}` started on **{agent_name}**.")


@bot.tree.command(name="flowchart-list", description="List available flowchart commands.")
async def flowchart_list_cmd(interaction: discord.Interaction) -> None:
    log.info("Slash command /flowchart-list from %s", interaction.user)


    commands = _list_flowchart_commands()
    if not commands:
        await audited_interaction_response_send(interaction,"No flowchart commands found.", ephemeral=True)
        return

    fc_lines: list[str] = []
    for cmd in commands:
        desc = f" — {cmd['description']}" if cmd["description"] else ""
        fc_lines.append(f"• `{cmd['name']}`{desc}")

    await audited_interaction_response_send(interaction,
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
        await audited_interaction_response_send(interaction,"Bot is not fully initialized yet.", ephemeral=True)
        return

    if force:
        await audited_interaction_response_send(interaction,"*System:* Force restarting (hot reload)...")
        log.info("Force restart requested via /restart command")
        await agents.shutdown_coordinator.force_shutdown("/restart force")
        return

    await audited_interaction_response_send(interaction,"*System:* Initiating graceful restart (hot reload)...")
    log.info("Restart requested via /restart command")
    await agents.shutdown_coordinator.graceful_shutdown("/restart command")


@bot.tree.command(
    name="restart-including-bridge",
    description="Full restart — kills bridge + all agents. Sessions will disconnect.",
)
@app_commands.describe(force="Skip waiting for busy agents and restart immediately")
async def restart_including_bridge_cmd(interaction: discord.Interaction, force: bool = False) -> None:

    if agents.shutdown_coordinator is None:
        await audited_interaction_response_send(interaction,"Bot is not fully initialized yet.", ephemeral=True)
        return
    if agents.shutdown_coordinator.requested:
        await audited_interaction_response_send(interaction,
            "*System:* A restart is already in progress.",
            ephemeral=True,
        )
        return

    async def _send_goodbye() -> None:
        master_ch = await agents.get_master_channel()
        if master_ch:
            await audited_channel_send(
                master_ch,
                "*System:* Full restart — bridge is going down. See you soon!",
                operation="restart.goodbye",
            )

    full_coordinator = agents.make_shutdown_coordinator(
        close_bot_fn=bot.close,
        kill_fn=kill_supervisor,
        goodbye_fn=_send_goodbye,
        bridge_mode=False,
    )

    if force:
        await audited_interaction_response_send(interaction,
            "*System:* Force restarting (full — bridge will be killed, agents will disconnect)..."
        )
        log.info("Force full restart requested via /restart-including-bridge command")
        await full_coordinator.force_shutdown("/restart-including-bridge force")
        return

    await audited_interaction_response_send(interaction,
        "*System:* Initiating graceful full restart (bridge will be killed, agents will disconnect)..."
    )
    log.info("Full restart requested via /restart-including-bridge command")
    await full_coordinator.graceful_shutdown("/restart-including-bridge command")


# ---------------------------------------------------------------------------
# Voice commands
# ---------------------------------------------------------------------------


@bot.tree.command(name="vc-join", description="Join a voice channel and stream Dynamic Radio.")
@app_commands.describe(channel="Voice channel to join (defaults to your current VC)")
async def vc_join_command(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel | None = None,
) -> None:
    from axi import voice

    if channel is None:
        if interaction.user.voice and interaction.user.voice.channel:
            channel = interaction.user.voice.channel  # type: ignore[assignment]
        else:
            await audited_interaction_response_send(interaction,"Join a voice channel first, or specify one.", ephemeral=True)
            return

    await interaction.response.defer()
    try:
        await voice.join(channel)
        await audited_interaction_followup_send(interaction,f"Streaming Dynamic Radio in **{channel.name}**.")
    except Exception as e:
        log.error("Failed to join voice channel: %s", e)
        await audited_interaction_followup_send(interaction,f"Failed to join: {e}", ephemeral=True)


@bot.tree.command(name="vc-leave", description="Leave the voice channel and stop streaming.")
async def vc_leave_command(interaction: discord.Interaction) -> None:
    from axi import voice

    if interaction.guild is None:
        await audited_interaction_response_send(interaction,"This command only works in a server.", ephemeral=True)
        return

    if await voice.leave(interaction.guild):
        await audited_interaction_response_send(interaction,"Disconnected from voice.")
    else:
        await audited_interaction_response_send(interaction,"Not in a voice channel.", ephemeral=True)


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
        agent_type=config.get_default_agent_type(),
        client=None,
        cwd=cwd,
        system_prompt=prompt,
        system_prompt_hash=compute_prompt_hash(prompt),
        mcp_servers=mcp_servers,
    )
    discord_state(session).channel_id = channel.id
    # Late-substitute channel info into system prompt
    if isinstance(session.system_prompt, dict) and "append" in session.system_prompt:
        session.system_prompt["append"] = (
            session.system_prompt["append"]
            .replace("{channel_id}", str(channel.id))
            .replace("{channel_name}", channels.strip_status_prefix(channel.name))
            .replace("{guild_id}", str(channel.guild.id))
            .replace("{guild_name}", channel.guild.name)
        )
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
    return worktrees.create_worktree(name)


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

    if channels.is_axi_channel(channel):
        await _handle_axi_channel_create(channel)
    elif channels.is_active_channel(channel):
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
        await channels.ensure_category_positions()
    except Exception:
        log.exception("Failed to re-enforce channel/category positions")


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
        await audited_channel_send(channel, readme_text, operation="readme.post")
        log.info("Sent readme message to #%s", channel.name)
    elif existing_msg.content != readme_text:
        await audited_message_edit(existing_msg, content=readme_text, operation="readme.edit")
        log.info("Updated readme message in #%s", channel.name)
    else:
        log.info("Readme message in #%s already up to date", channel.name)


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------


def _load_master_session_data() -> tuple[str | None, str | None]:
    """Load master session_id and prompt_hash from disk.

    Skips resume if the saved guild_id doesn't match the current guild —
    prevents cross-guild message leaks from stale conversation context.
    """
    try:
        if os.path.isfile(config.MASTER_SESSION_PATH):
            with open(config.MASTER_SESSION_PATH) as f:
                raw = f.read().strip()
            if raw:
                if raw.startswith("{"):
                    data = json.loads(raw)
                    resume_id = data.get("session_id")
                    prompt_hash = data.get("prompt_hash")
                    saved_guild = data.get("guild_id")
                else:
                    resume_id = raw
                    prompt_hash = None
                    saved_guild = None
                if saved_guild and str(config.DISCORD_GUILD_ID) != saved_guild:
                    log.warning(
                        "Guild mismatch: saved=%s current=%s — discarding master session to prevent cross-guild leaks",
                        saved_guild, config.DISCORD_GUILD_ID,
                    )
                    return None, None
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
        agent_type=config.get_default_agent_type(),
        cwd=config.DEFAULT_CWD,
        system_prompt=MASTER_SYSTEM_PROMPT,
        system_prompt_hash=prompt_hash,
        client=None,
        mcp_servers=master_mcp,
        compact_instructions=(
            "List of active/spawned agents and their current status. "
            "Any ongoing tasks or investigations in progress. "
            "Recent user requests that haven't been completed yet. "
            "Important context the user has shared (preferences, decisions, constraints)."
        ),
        extra_write_dirs=[os.path.expanduser("~/.config/systemd/user")],
    )
    session.session_id = resume_id
    ds = discord_state(session)
    ds.todo_items = agents.load_todo_items(config.MASTER_AGENT_NAME)
    agents.agents[config.MASTER_AGENT_NAME] = session
    log.info("Master agent registered (sleeping, session_id=%s)", resume_id and resume_id[:8])
    return session


def _register_egress_snowflakes(bot: discord.Client) -> None:
    """Register guild and channel snowflake IDs for egress filtering."""
    from axi.egress_filter import register_snowflakes

    ids: dict[str, str] = {}
    guild = bot.get_guild(config.DISCORD_GUILD_ID)
    if guild:
        ids[str(guild.id)] = "[guild-id]"
        for ch in guild.channels:
            ids[str(ch.id)] = "[channel-id]"
    # Also register the bot's own user ID
    if bot.user:
        ids[str(bot.user.id)] = "[bot-id]"
    if ids:
        register_snowflakes(ids)
        log.info("Egress filter: registered %d snowflake IDs", len(ids))


def _register_egress_startup_secrets() -> None:
    """Scan the bot's repo root for .env files and register their values."""
    from axi.egress_filter import register_secrets_from_dir

    register_secrets_from_dir(config.BOT_DIR)


async def _setup_guild_infrastructure(master_session: AgentSession) -> None:
    """Set up Discord guild categories and master channel."""
    try:
        await agents.ensure_guild_infrastructure()
        await channels.deduplicate_master_channel()
        master_channel = await agents.ensure_agent_channel(config.MASTER_AGENT_NAME)
        discord_state(master_session).channel_id = master_channel.id
        # Late-substitute channel info into master system prompt
        if isinstance(master_session.system_prompt, dict) and "append" in master_session.system_prompt:
            master_session.system_prompt["append"] = (
                master_session.system_prompt["append"]
                .replace("{channel_id}", str(master_channel.id))
                .replace("{channel_name}", master_channel.name)
                .replace("{guild_id}", str(master_channel.guild.id))
                .replace("{guild_name}", master_channel.guild.name)
            )
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
        await channels.ensure_category_positions()
    except Exception:
        log.exception("Failed to ensure category positions")

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
        await audited_channel_send(master_ch, "\n".join(msg_lines), operation="startup.rollback_notice")
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
        await audited_channel_send(master_ch, crash_msg, operation="startup.crash_notice")
    await audited_channel_send(master_ch, f"*System:* Axi ready. ({startup_elapsed:.1f}s){trace_tag}", operation="startup.ready")
    mentions = " ".join(f"<@{uid}>" for uid in config.ALLOWED_USER_IDS)
    await audited_channel_send(master_ch, mentions, operation="startup.mentions")
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


def _log_runtime_configuration() -> None:
    """Log the resolved harness/model configuration without printing secrets."""
    axi_model, claude_model_arg, runtime_env = config.get_resolved_model()
    log.info(
        "Runtime config: harness=%s model=%s claude_model_arg=%s effort=%s fc_wrap=%s proxy_base_url=%s proxy_model=%s",
        config.get_harness(),
        axi_model,
        claude_model_arg or "<env>",
        config.get_effort(),
        config.get_fc_wrap() or "<off>",
        runtime_env.get("ANTHROPIC_BASE_URL", "<none>"),
        runtime_env.get("ANTHROPIC_MODEL", "<none>"),
    )


@bot.event
async def on_ready() -> None:
    global _on_ready_fired
    set_agent_context("system")
    set_trigger("startup")
    log.info("Bot ready as %s", bot.user)
    _log_runtime_configuration()

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
    set_agent_sessions_provider(lambda: agents.agents)
    set_scheduler_status_provider(scheduler.status)
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

        # Register known Discord snowflake IDs for egress filtering
        _register_egress_snowflakes(bot)
        # Scan our own repo root for .env values to scrub from outgoing text
        _register_egress_startup_secrets()

        _startup_t0 = time.monotonic()
        master_ch = await agents.get_master_channel()
        if master_ch:
            await audited_channel_send(master_ch, f"*System:* Axi starting up...{_trace_tag}", operation="startup.begin")

        await agents.connect_procmux()
        agents.init_shutdown_coordinator()

        await bot.tree.sync()
        log.info("Slash commands synced")

        # Rejoin voice channels from previous session
        from axi import voice
        for guild_id, channel_id in voice.get_saved_channels().items():
            guild = bot.get_guild(guild_id)
            if not guild:
                continue
            channel = guild.get_channel(channel_id)
            if channel and isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                try:
                    await voice.join(channel)
                    log.info("Auto-rejoined VC #%s in %s", channel.name, guild.name)
                except Exception:
                    log.warning("Failed to auto-rejoin VC %d", channel_id, exc_info=True)
                    voice._clear_state(guild_id)

        check_schedules.start()
        log.info("Schedule checker started")

        rollback_info = _consume_json_marker(config.ROLLBACK_MARKER_PATH, "Rollback")
        crash_info = _consume_json_marker(config.CRASH_ANALYSIS_MARKER_PATH, "Crash analysis")

        global _startup_complete
        _startup_complete = True

        if config.HTTP_API_PORT:
            import uvicorn

            from axi.http_api import app as http_app

            uvi_config = uvicorn.Config(
                http_app,
                host=config.HTTP_API_HOST,
                port=config.HTTP_API_PORT,
                log_level="warning",
            )
            _http_server = uvicorn.Server(uvi_config)
            _http_server.install_signal_handlers = lambda: None
            _http_task = asyncio.create_task(_http_server.serve())
            _http_task.add_done_callback(
                lambda t: log.error("HTTP API server exited unexpectedly: %s", t.exception()) if t.exception() else None
            )
            log.info("HTTP API server starting on %s:%s", config.HTTP_API_HOST, config.HTTP_API_PORT)

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
