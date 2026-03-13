# RFC-0005: Rate Limiting & Quota Tracking

**Status:** Draft
**Created:** 2026-03-09

## Problem

Rate limiting is a critical reliability mechanism: when the Claude API returns a rate limit error, the bot must stop retrying, notify the operator, and resume cleanly when the limit expires. The two implementations parse rate-limit durations differently (axi-py uses a single regex with multiple formats; axi-rs uses a cascading chain of four regexes with per-branch fallbacks), handle notifications differently (axi-py broadcasts and schedules expiry notifications; axi-rs has no notification logic in the spec), and track quota state with different granularity.

## Behavior

### Rate Limit Duration Parsing

`parse_rate_limit_seconds(error_text)` extracts a wait duration from free-text API error messages.

Supported formats (in priority order):
1. Relative time: "in N seconds/minutes/hours" or "retry after N seconds/minutes/hours".
2. Bare duration: "N seconds" or "N minutes".
3. Numeric shorthand: "retry after N" (interpreted as seconds).

Unit conversion:
- seconds: use as-is.
- minutes: multiply by 60.
- hours: multiply by 3600.

Default fallback: When no pattern matches, return **300 seconds** (5 minutes).

Numeric parse failure: When a pattern matches but the number fails to parse, fall back to a reasonable default for that unit (300s for seconds-scale, 300s for minutes-scale).

### Rate Limit State

The bot maintains a global `rate_limited_until` deadline (timestamp or None).

`is_rate_limited()`:
1. If `rate_limited_until` is None, return false.
2. If current time >= `rate_limited_until`, auto-clear (set to None) and return false.
3. Otherwise, return true.

`rate_limit_remaining_seconds()`: Return the non-negative seconds remaining until the rate limit expires, or 0 if not rate-limited.

`format_time_remaining(seconds)`: Format into human-readable strings:
- Sub-minute: `"{N}s"`
- Sub-hour: `"{M}m {S}s"` (omit zero-valued sub-components).
- Hour-scale: `"{H}h {M}m"` (omit zero-valued sub-components).

### Handling a Rate Limit Hit

`handle_rate_limit(duration_seconds)`:
1. Compute new deadline = now + duration_seconds.
2. The deadline MUST only be extended, never shortened. If a new limit arrives while already limited, update the deadline only if the new expiry is later.
3. On the first hit (not already limited):
   a. Set the deadline.
   b. Emit a notification to the triggering channel and the master channel.
   c. Schedule a delayed expiry notification that fires when the limit expires.
4. On duplicate hits while already limited: suppress notification (update deadline only if extending).

### Rate Limit Expiry Notification

When the rate limit expires:
1. Verify the limit has actually cleared (in case it was extended).
2. Send a recovery message to the master channel only.

### Stream Integration

During streaming, when the stream detects a `rate_limit` or `billing_error` in an AssistantMessage:
1. Invoke the rate limit handler.
2. Set a `hit_rate_limit` flag on the stream.
3. Suppress all further text flushing for that stream.
4. Return a distinguishable rate-limit result so `stream_with_retry` does NOT retry.

### Quota Tracking

`update_rate_limit_quota(event)`: Ingest `rate_limit_info` events from the stream.
- Each event contains: status, resets_at, rate_limit_type, and optional utilization.
- Upsert per-type quota state keyed by `rate_limit_type`.
- When an "allowed" event arrives without a utilization value for the same reset window, preserve the previous utilization. Utilization resets only on window rollover.
- Append each event to a persistent JSONL history log.

### Session Usage Recording

`record_session_usage(session_id, stats)`:
1. On first call for a session_id: create a new usage entry with `first_query` = now.
2. On each call: increment query count; accumulate cost, turns, duration, and token counts; set `last_query` = now.
3. When a usage history path is configured, append a JSONL record for each usage event.
4. File I/O errors during append MUST be silently ignored. Recording failures MUST NOT disrupt bot operation.

## Invariants

- **I-RL-1**: Rate limit notifications MUST only go to the triggering channel and master channel, not broadcast to all agent channels. [axi-py I5.1]
- **I-RL-2**: `stream_with_retry` MUST NOT retry when the error is a rate limit. Rate-limited responses retried as transient errors cause repeated failures. [axi-py I5.2]
- **I-RL-3**: When an "allowed" rate_limit_event arrives without a utilization value for the same reset window, the previous utilization MUST be preserved. Overwriting with None causes display issues. [axi-py I5.3]

## Open Questions

1. **Notification scope.** axi-py emits notifications to the triggering channel AND the master channel, and schedules an async expiry notification. axi-rs has no notification logic in the spec (only parsing and state tracking). Should notification behavior be normative? If so, should it include the expiry notification?

2. **Regex implementation divergence.** axi-py uses a single regex with named groups and multiple alternatives. axi-rs uses four separate regexes tried in sequence, each with its own fallback value. The behavioral contract (parse these formats, fall back to 300s) is the same, but edge cases (e.g., "retry after 5 minutes" vs "in 5 minutes") may differ. Should the supported format list be exhaustively specified with test vectors?

3. **Per-branch numeric parse failure defaults.** axi-rs falls back to 5 (minutes? seconds?) for minutes-based patterns vs 300 for seconds-based. axi-py falls back to 300 uniformly. Should fallback values be standardized?

4. **Quota tracking granularity.** Both implementations track per-type quota state, but axi-rs also stores a `RateLimitTracker` struct with per-quota maps. axi-py's quota tracking is spread across `rate_limits.py` and `streaming.py`. Should the data model be normalized?

5. **Usage history file format.** Both use JSONL, but the specific fields and record structure are not defined in either spec. Should the JSONL schema be standardized for cross-implementation compatibility?

## Implementation Notes

### axi-py
- `parse_rate_limit_seconds` uses a single regex with named groups: `(?:in|after)\s+(\d+)\s*(second|minute|hour)`.
- `handle_rate_limit` uses a broadcast callback to emit notifications.
- `notify_rate_limit_expired` is an async task that sleeps until expiry, then sends a recovery message to master.
- `update_rate_limit_quota` preserves utilization on same-window "allowed" events (I5.3).
- `record_session_usage` accumulates stats in memory and appends JSONL.
- `_handle_assistant_message` in `discord_stream.py` detects rate_limit/billing_error, sets `hit_rate_limit=True`, suppresses text flushing.
- `stream_response_to_channel` returns a distinguishable value on rate limit hit.
- Rate-limit deadline is only extended, never shortened (B5.9).

### axi-rs
- `parse_rate_limit_seconds` uses four regex patterns tried in sequence:
  1. `(?:in|after)\s+(\d+)\s*(?:second|minute|hour)` with unit conversion.
  2. `retry after\s+(\d+)` (bare seconds).
  3. `(\d+)\s*seconds?`.
  4. `(\d+)\s*minutes?`.
- Default fallback: 300s when no regex matches.
- Per-branch numeric parse failure fallbacks: 300 for seconds, 5 for minutes.
- `is_rate_limited` auto-clears expired limits.
- `format_time_remaining` produces human-readable strings.
- `record_session_usage` creates/updates `SessionUsage` keyed by session_id, JSONL append.
- `RateLimitTracker` stores per-quota state with status, reset time, type, and utilization.
- File I/O errors silently ignored (`.ok()`).
- No notification or expiry-notification logic in the spec.
