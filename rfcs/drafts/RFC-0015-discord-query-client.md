# RFC-0015: Discord Query Client

**Status:** Draft
**Created:** 2026-03-09

## Problem

The Discord query client provides both sync and async REST wrappers used by CLI tooling and the bot process for channel operations, message history, search, and reaction management. This module exists only in axi-py (`discordquery` package). axi-rs embeds equivalent HTTP retry logic in its `DiscordClient` (covered in RFC-0012) but does not have a standalone query/wait CLI. Documenting this as a normative spec ensures feature parity if axi-rs needs CLI-driven Discord operations (e.g., `axi_test msg`, search, wait-for-response).

## Behavior

### Client Variants

| Variant | Transport | Use Case |
|---------|-----------|----------|
| `DiscordClient` | Sync httpx, context-manager | CLI tools (`query`, `wait` subcommands) |
| `AsyncDiscordClient` | Async httpx, async context-manager | Bot process (MCP tools, message sending) |

### HTTP Retry Policy

Both clients share the same retry policy:

| Condition | Action | Max Retries |
|-----------|--------|-------------|
| HTTP 429 | Sleep for `retry_after` from response body | 3 |
| HTTP 5xx | Exponential backoff: `2^attempt` seconds | 3 |
| HTTP 4xx (non-429) | Raise `HTTPStatusError` immediately | 0 |

**Success codes:**
- Sync client: 200, 204.
- Async client: 200, 201, 204.

### Channel Operations

**list_channels(guild_id)**:
1. Fetch all channels for the guild.
2. Build a category name lookup from type-4 (category) channels.
3. Filter to type-0 (text) and type-5 (announcement) channels.
4. Sort by position.
5. Return with resolved category names.

**find_channel(guild_id, name)** (async only):
- Case-insensitive name matching against text and announcement channels.
- Return `None` on no match.

### Message Operations

**get_messages(channel_id, limit, before, after)**:
- Clamp `limit` to 100 (Discord's per-request maximum).
- Support `before` and `after` pagination cursors (snowflake IDs).

**send_file(channel_id, filename, content_bytes, text)** (async only):
- Multipart file upload via httpx `data`/`files` kwargs.
- Optional text `content` field alongside the file.

**split_message(text, limit=2000)**:
- Split at newline boundaries when possible.
- Fall back to hard split at the character limit.
- Return a list of chunks, each within the limit.

### Reaction Operations

**add_reaction(channel_id, message_id, emoji)** / **remove_reaction(...)**:
- URL-encode the emoji string via `urllib.parse.quote` before interpolating into the API path. Raw Unicode in URLs causes malformed requests.

### Message Formatting

**format_message(msg, format)**:
- JSONL mode: Output `{id, ts, author, author_id, content, attachments, embeds}` per line.
- Human-readable mode: Formatted text output.

**is_system_message(content)**:
- Identify bot system messages by the `*System:*` content prefix.

### Snowflake Resolution

**resolve_snowflake(value)**:
- Accept a numeric snowflake ID string: return as-is.
- Accept an ISO datetime string: convert to a snowflake using the Discord epoch (1420070400000 ms).

### Wait-for-Messages

**wait_for_messages(channel_id, after, timeout, filter_author_ids)**:

1. Poll `get_messages` with the `after` cursor.
2. Filter out messages from `filter_author_ids` and system messages (by `*System:*` prefix).
3. If new non-filtered messages exist, return `(messages, cursor)`.
4. If messages exist but all are filtered, advance the `after_id` baseline to the latest message ID. This prevents infinite re-scanning of the same filtered messages.
5. If no messages exist, sleep and retry.
6. On timeout, return what has been collected (may be empty).

**Wait CLI auto-detection**:
- When `--after` is not provided, auto-detect the latest message ID as the baseline.
- On completion, emit a `{"cursor": ...}` JSON line for chaining sequential wait calls.

### Query CLI

The `query` CLI provides four subcommands:

| Subcommand | Description |
|------------|-------------|
| `guilds` | List guilds the bot is a member of |
| `channels` | List channels in a guild (validates guild membership first) |
| `history` | Fetch message history for a channel with pagination |
| `search` | Client-side substring search across guild channels |

**Channel resolution** (`resolve_channel`):
- Accept raw channel ID (numeric string).
- Accept `guild_id:channel_name` syntax with case-insensitive name lookup.

**Search** (`cmd_search`):
- Client-side case-insensitive substring matching.
- Scans all text channels in the guild.
- Per-channel scan limits.
- Optional author filtering.

### Entry Point

The `__main__` module dispatches `query` and `wait` subcommands via lazy imports. `DISCORD_TOKEN` is read from the environment.

## Invariants

- **I-DQ-1**: Emoji strings in reaction API paths MUST be URL-encoded via `urllib.parse.quote`. Raw Unicode in URL paths produces malformed API requests. [axi-py I15.1]
- **I-DQ-2**: All Discord REST API calls MUST go through the httpx client wrapper with retry/rate-limit handling, not raw request calls. Bypassing the wrapper loses retry and rate-limit protection. [axi-py I15.2]

## Open Questions

1. **Sync vs. async unification.** The sync and async clients accept different success codes (sync: 200/204; async: 200/201/204). Should success codes be unified to 200/201/204 for both?

2. **axi-rs coverage.** axi-rs has HTTP retry logic in its `DiscordClient` but no standalone query/wait CLI. Is a Rust query CLI needed, or can the Python `discordquery` package serve both codebases as a shared tool?

3. **Client-side search.** `cmd_search` performs client-side substring matching by fetching message history. This is slow for large channels. Should this be replaced by or supplemented with Discord's server-side search API (available for bot accounts)?

4. **Wait poller interval.** The polling interval for `wait_for_messages` is not specified in the spec. What should it be? Too frequent risks rate limits; too infrequent adds latency for interactive use (e.g., `axi_test msg --wait`).

5. **Multipart upload content type.** `send_file` uses httpx multipart upload but does not specify a content type for the file part. Should a MIME type be inferred or required?

## Implementation Notes

### axi-py
- `DiscordClient` and `AsyncDiscordClient` in `packages/discordquery/discordquery/client.py`.
- Sync retry loop: `while retries < MAX_RETRIES`, checks 429 then 5xx.
- Async success codes include 201 for resource creation endpoints.
- `wait_for_messages` in `packages/discordquery/discordquery/wait.py`.
- `is_system_message` checks `*System:*` prefix.
- `resolve_snowflake` and `split_message` in `packages/discordquery/discordquery/helpers.py`.
- `resolve_channel`, `cmd_search`, `cmd_history` in `packages/discordquery/discordquery/query.py`.
- `__main__.py` dispatches `query` and `wait` subcommands.
- `add_reaction` / `remove_reaction` use `urllib.parse.quote(emoji)`.

### axi-rs
- `DiscordClient` in `axi-config/src/discord.rs` wraps reqwest with 429 retry and 5xx exponential backoff (3 retries). This covers the HTTP retry policy but not the query/wait CLI, channel resolution, search, or message formatting.
- No standalone query CLI exists in axi-rs. Discord operations are performed through MCP tools or direct API calls within the bot process.
- Reaction URL-encoding handled by reqwest's URL builder.
