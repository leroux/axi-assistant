# RFC-0004: Channel & Guild Management

**Status:** Draft
**Created:** 2026-03-09

## Problem

Discord channel management is the primary user-facing surface of the bot. The two implementations handle channel naming, category placement, topic encoding, and status prefixes differently. axi-py has features absent from axi-rs (master channel deduplication, recency-based reordering, status rename debouncing, test instance slot reservation) and axi-rs lacks the cwd-based category routing that axi-py enforces. Without alignment, agents appear in wrong categories and channel names behave inconsistently across implementations.

## Behavior

### Guild Infrastructure

On startup, ensure three Discord categories exist:

| Category | Purpose |
|----------|---------|
| **Axi** | Agents working on the bot's own codebase (cwd within BOT_DIR or BOT_WORKTREES_DIR). |
| **Active** | All other active agents. |
| **Killed** | Channels for ended sessions (archived). |

Each category is created with permission overwrites for the bot, allowed users, and the default role. If a category already exists (matched by name), it is reused.

### Channel Name Normalization

`normalize_channel_name(name)`:
1. Lowercase the input.
2. Replace spaces with hyphens.
3. Strip all characters that are not alphanumeric, hyphen, or underscore.
4. Truncate to 100 characters.

### Channel Topic Encoding

Channel topics encode agent metadata in pipe-delimited format:

```
cwd|session_id|prompt_hash|agent_type
```

- `agent_type` is omitted when it equals the default type.
- `parse_channel_topic` is the inverse: extracts the four fields, returning all-None/empty for absent or empty topics.
- Topic updates MUST be fire-and-forget to avoid blocking on Discord's channel-edit rate limit (2 per 10 min).

### Status Prefixes

Seven status prefixes are defined, each mapped to a Unicode emoji:

| Status | Emoji | Meaning |
|--------|-------|---------|
| working | (wrench) | Agent is processing a query |
| plan_review | (clipboard) | Awaiting plan approval |
| question | (question mark) | Awaiting user input |
| done | (checkmark) | Task completed |
| idle | (zzz) | Agent is idle |
| error | (warning) | Error state |
| custom | (varies) | Custom status |

`set_channel_status(name, status)`: Rename the channel to `{emoji}-{normalized_name}` for known statuses, or bare `{normalized_name}` for unknown/cleared statuses.

`strip_status_prefix(channel_name)`: Remove a leading `{emoji}-` prefix, returning the bare agent name for matching.

`match_channel_name(channel_name, agent_name)`: Compare a channel name to a normalized agent name, optionally stripping status prefixes when status is enabled.

Status renames SHOULD be debounced (per-channel cooldown) to avoid hitting Discord's channel-rename rate limit.

### ensure_agent_channel

1. Search for an existing text channel matching the normalized agent name (with optional status prefix stripping).
2. If found:
   - If in the Killed category, move it back to the correct live category.
   - If in the wrong live category, move it to the correct one.
   - Return the existing channel.
3. If not found, create a new channel in the appropriate category.

Category placement is determined by the agent's cwd:
- cwd within BOT_DIR or BOT_WORKTREES_DIR -> Axi category.
- All other cwds -> Active category.

### Master Channel

The master agent's channel (`#axi-master`) has special treatment:
- Always ensured to exist during startup.
- Placed at position 0 with no parent category (top of the server), or under the Axi category.
- Deduplication: at startup, scan for duplicate master channels; delete extras, preferring the uncategorized or Axi-category survivor.

### Channel Recency Reordering

Channels within Axi and Active categories SHOULD be sorted by last activity (most-recent-first), with `#axi-master` always first. Reordering SHOULD be debounced (e.g., 60-second cooldown) to avoid excessive API calls.

`mark_channel_active`: Move a channel to position 0 within its category so the most recently active agent appears at the top.

### Reconstruction

`reconstruct_agents_from_channels` (or `reconstruct_channel_map`):
1. Iterate all text channels in managed categories (Axi, Active; optionally Killed).
2. Parse channel topics for cwd, session_id, prompt_hash, agent_type.
3. Build a ChannelId -> agent_name mapping.
4. Create sleeping AgentSession entries for discovered agents.

### Startup Sequence

1. `ensure_guild_infrastructure` (create/sync categories).
2. `reconstruct_channel_map` / `reconstruct_agents_from_channels`.
3. Register discovered channel-to-agent mappings.
4. Ensure master channel exists and is positioned.
5. Send startup notification to master channel.
6. Set `startup_complete` flag atomically.

### Test Instance Slot Reservation

(axi-py only; for disposable test instances)

- `up`: Reserve a guild/bot-token slot atomically under an exclusive file lock. Write a `.env` file. The bot resolves its token from the slots file at startup.
- `down`: Stop the systemd service and release the slot atomically under the same file lock.
- `list`: Health check that removes orphaned reservations and displays all slots.
- Slot selection uses `_find_free_guild` to pick the first guild whose bot token is not claimed by another slot.

## Invariants

- **I-CH-1**: Channel category placement MUST be based on agent cwd. Channels in the wrong category MUST be moved. `move_channel_to_killed` MUST search both Axi and Active categories. [axi-py I4.1]
- **I-CH-2**: Channel topic updates MUST be fire-and-forget. Asyncio tasks MUST be stored in a reference-holding set to prevent GC. [axi-py I4.2]
- **I-CH-3**: Slot allocation conflict detection MUST check all allocated instances (not just running ones) to prevent double-allocation of bot tokens. [axi-py I4.3]
- **I-CH-4**: `cmd_up` MUST check systemd service status before refusing reuse. Stale `.env` files from OOM kills MUST be auto-cleaned. [axi-py I4.4]
- **I-CH-5**: Token resolution for test worktrees MUST use slot-based lookup first, then environment variable, then sender_token. Inheriting the parent process's token targets the wrong bot. [axi-py I4.5]
- **I-CH-6**: All Discord REST API calls MUST go through the wrapper client (httpx AsyncDiscordClient), not raw request calls. Direct calls bypass URL encoding, retry logic, and error handling. [axi-py I4.6]

## Open Questions

1. **Category placement by cwd.** axi-py routes channels to Axi vs Active based on cwd (B4.2, I4.1). axi-rs places all agent channels under a specified category without cwd-based routing. Should cwd-based placement be normative?

2. **Master channel positioning.** axi-py pins `#axi-master` to position 0 with no parent category (top of server). axi-rs places it under the Axi category. Which behavior should be normative?

3. **Recency reordering.** axi-py sorts channels by last activity with 60s debounce. axi-rs uses `mark_channel_active` (move to position 0) instead of full sorts. Should full recency sorting be normative, or is move-to-top sufficient?

4. **Status rename debouncing.** axi-py has a 5-minute per-channel cooldown for status renames. axi-rs does not mention debouncing. Should a cooldown be normative?

5. **Test instance slot reservation.** This is axi-py only. Should it be part of the normative spec, or is it an axi-py implementation detail?

6. **Default agent_type in topic encoding.** axi-rs omits `agent_type` from the topic when it equals `"flowcoder"`. axi-py's default is `"claude_code"`. The omission logic depends on which default is chosen. (See also RFC-0001 Open Question 1.)

## Implementation Notes

### axi-py
- `ensure_guild_infrastructure` creates categories with permission overwrites (bot, allowed users, default role).
- `_is_axi_cwd` checks `real.startswith((bot_real + os.sep, worktrees_real + os.sep))`.
- `deduplicate_master_channel` runs at startup before master channel setup.
- `ensure_master_channel_position` uses a direct REST API PATCH call to pin position 0 with no parent.
- Channel status emoji prefixes use a debounced rename batch with 5-minute per-channel cooldown.
- `reorder_channels_by_recency` sorts by last activity, 60s debounced.
- `fire_and_forget` stores tasks in `_background_tasks` set with done-callback removal.
- Test instance slot reservation uses `fcntl.flock` for exclusive file locking.
- All Discord REST API calls go through `AsyncDiscordClient` (httpx wrapper).

### axi-rs
- `normalize_channel_name` via `chars().take(100).collect()`.
- `format_channel_topic` omits agent_type when it equals `"flowcoder"`.
- `reconstruct_channel_map` filters to channels parented under managed categories.
- `mark_channel_active` moves channel to position 0 within its category.
- `startup_complete` flag set with `SeqCst` atomic ordering.
- Startup notification sent to master channel after master agent registration.
- No cwd-based category routing; no master channel deduplication; no recency-based full sort; no status rename debouncing.
