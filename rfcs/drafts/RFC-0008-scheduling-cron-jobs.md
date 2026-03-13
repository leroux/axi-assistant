# RFC-0008: Scheduling & Cron Jobs

**Status:** Draft
**Created:** 2026-03-09

## Problem

Agents need to perform recurring tasks (daily summaries, periodic checks) and one-off
future actions (reminders, delayed processing). Both implementations have a scheduler
but diverge on cron evaluation strategy (axi-py uses `croniter` to find the most recent
tick; axi-rs uses field-level matching against the current time), timezone handling
(axi-py explicitly evaluates in local time; axi-rs is less explicit), and first-fire
suppression semantics. These differences can cause schedules to fire at different times
or double-fire across a restart boundary.

## Behavior

### Scheduler Loop

1. **Tick interval.** The scheduler runs as a background task, ticking every 10 seconds.

2. **Time computation.** On each tick, compute both `now_utc` (for one-off comparisons
   and history timestamps) and `now_local` (in `SCHEDULE_TIMEZONE`, for cron
   evaluation). All cron expressions are written and evaluated in local time.

3. **Cron evaluation.** Recurring schedules fire when the current local time matches the
   cron expression AND the schedule has not already fired within the current cron tick
   window. The `last_fired` map, keyed by `"owner/name"`, tracks the most recent fire
   time for each schedule.

4. **Cron expression format.** 5-field expressions: minute, hour, day-of-month, month,
   day-of-week. Supports wildcards (`*`), ranges (`1-5`), steps (`*/15`), comma-separated
   lists (`1,15,30`), and day-of-week `7` as alias for Sunday (`0`).

5. **One-off evaluation.** One-off schedules fire when `at <= now_utc`. After firing,
   the entry is immediately removed from `schedules.json`. The fire is recorded in
   history.

6. **First-fire suppression.** On first encounter of a schedule key (e.g., after
   restart), `last_fired` is initialized to the current time (or the most recent cron
   tick) without actually firing. This prevents catch-up fires for schedules that were
   due during the downtime window.

7. **Duplicate prevention within a minute.** Because the scheduler ticks every 10
   seconds, a cron expression matching the current minute could trigger up to 6 times.
   The `last_fired` check prevents this: a recurring schedule fires at most once per
   cron tick.

### Schedule Routing

8. **Target resolution.** When a schedule fires, the target agent is resolved via the
   `owner` field (preferred), falling back to `session` field for backward
   compatibility. If the target agent exists, it receives the prompt via
   `wake_or_queue`. If it does not exist, a new agent session is spawned.

9. **Reset context.** Schedules support a `reset_context` boolean. When set, the agent's
   conversation context is reset before delivering the scheduled prompt.

### MCP Schedule Tools

10. **Per-agent scoping.** Each agent gets schedule MCP tools (`schedule_list`,
    `schedule_create`, `schedule_delete`) scoped by its `owner` field. An agent can only
    see, create, and delete its own schedules.

11. **Creation validation:**
    - Name: 1-50 characters, lowercase alphanumeric + hyphens, no leading/trailing
      hyphen (same rules as agent names)
    - Prompt: non-empty, max 2000 characters
    - Cron: must be a valid 5-field cron expression
    - One-off `at`: must be a timezone-aware ISO 8601 datetime in the future

12. **Uniqueness and limits.** Schedule names must be unique per agent (enforced by
    `owner` + `name` key). Each agent is limited to 20 schedules.

13. **Deletion authorization.** `schedule_delete` requires both `name` and `owner` to
    match, preventing agents from deleting each other's schedules.

### History

14. **Recording.** Every schedule fire is recorded in `schedule_history.json` with the
    schedule name, owner, prompt, and UTC timestamp.

15. **Deduplication.** Before recording, check the most recent history entry for the
    same schedule name/owner. Skip recording if within the dedup window: 5 minutes for
    recurring schedules, 0 (no dedup) for one-off schedules.

16. **Pruning.** On every tick, remove history entries older than 7 days.

### File I/O

17. **Serialization.** All schedule file read-modify-write cycles (schedules.json,
    schedule_history.json) are serialized through a single lock
    (`schedules_lock` / `SCHEDULES_LOCK`) shared between the scheduler loop and MCP
    schedule tools. This prevents races between concurrent creates, deletes, and fires.

### Master Agent

18. **Master registration.** The master agent must be registered with the schedule MCP
    server so it can create and manage its own schedules.

## Invariants

Schedule routing must use the `owner` field to determine the target agent, not the
legacy `session` field. (I8.1-py)

`last_fired` must be initialized on first encounter to suppress catch-up fires after
restart. (I8.2-py, I8.1-rs)

All datetime comparisons must use timezone-aware datetimes. Naive `datetime.now()` is
never acceptable. (I8.3-py)

Cron expressions must be evaluated in `SCHEDULE_TIMEZONE` (local time), not UTC.
(I8.4-py)

Recurring schedule fires must be recorded in history with dedup. (I8.5-py, I8.2-rs)

The master agent must have schedule MCP server registered. (I8.6-py)

## Open Questions

1. **Cron evaluation strategy.** axi-py uses `croniter.get_prev()` to find the most
   recent cron tick and compares against `last_fired`. axi-rs uses direct field-level
   matching (`cron_matches`) against the current time. Both achieve the same goal but
   the edge-case behavior may differ (e.g., when the scheduler tick lands exactly on a
   minute boundary, or when DST transitions shift local time). Should one strategy be
   normative?

2. **Timezone configuration.** axi-py uses `SCHEDULE_TIMEZONE` config. axi-rs uses
   `Local` (system timezone). Should the timezone be explicitly configurable or should
   it always use the system timezone?

3. **Catch-up firing.** Both implementations suppress catch-up fires. But should there
   be an option for schedules that want catch-up (e.g., "if I missed the 9am report,
   still run it when I come back online")? Currently there is no way to opt in.

4. **`reset_context` semantics.** axi-rs documents `reset_context` as a boolean on
   schedule entries. axi-py does not mention it. Should this be normative, and what
   exactly does "reset context" mean — clear `session_id`? Rebuild the session?

5. **History storage format.** Both use JSON files. At scale, this becomes a performance
   concern (reading/writing the entire file on every tick). Is the JSON file approach
   normative, or should implementations be free to use other storage?

## Implementation Notes

**axi-py:** Scheduler loop in `axi/main.py` (`check_schedules`, `_fire_schedules`).
Uses `croniter` library for cron evaluation — `croniter(cron_expr, now_local).get_prev(datetime)`.
MCP tools in `axi/schedule_tools.py` with `schedule_key` function producing
`"{owner}/{name}"` keys. History dedup uses 5-minute window comparison. Master agent
registration in `axi/main.py` (`_register_master_agent`). All file I/O through
`schedules_lock` asyncio Lock.

**axi-rs:** Scheduler in `scheduler.rs` as a background tokio task. Custom `cron_matches`
function for 5-field expressions with field-level matching. `last_fired` HashMap with
first-seen skip (`if !last_fired.contains_key { insert now; continue }`). MCP tools in
`mcp_schedule.rs` with same `is_valid_name` validation as agent names. File I/O through
static tokio `Mutex` (`SCHEDULES_LOCK`). `schedule_key` falls back from `owner` to
`session` field. Routing via `wake_or_queue` with spawn if no session exists.
