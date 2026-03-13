# RFC-0010: Discord Rendering & UI

**Status:** Draft
**Created:** 2026-03-09

## Problem

Agent responses are streamed token-by-token from the Claude CLI, but Discord's API has
strict rate limits (5 edits per 5 seconds per channel), a 2000-character message limit,
and no native streaming support. Both implementations render streams to Discord but
diverge on the frontend abstraction (axi-rs has a `Frontend` trait with
`DiscordFrontend` + `WebFrontend` + `FrontendRouter`; axi-py uses module-level functions
behind a thin `DiscordFrontend` adapter), interactive gate semantics (axi-rs races
multiple frontends; axi-py blocks on Discord only), and specific rendering parameters
(edit interval, message split threshold). This RFC defines the normative rendering
behavior for Discord and the frontend abstraction boundary.

## Behavior

### Frontend Abstraction

1. **Frontend trait/protocol.** A `Frontend` defines an async interface with methods for:
   message posting, lifecycle events (wake, sleep, spawn, kill), stream events (text
   delta, tool use, thinking, completion), interactive gates (plan approval, user
   questions), and shutdown.

2. **Frontend router.** A `FrontendRouter` holds multiple frontend implementations and
   dispatches events to all of them:
   - **Non-interactive events** (messages, lifecycle, stream) are broadcast sequentially
     to every registered frontend.
   - **Interactive gates** (plan approval, questions) are dispatched to all frontends
     concurrently. The first frontend to respond wins; remaining frontends are aborted.

3. **BotState composition.** The bot state holds a generic `FrontendRouter` for
   trait-based dispatch, plus a direct reference to the `DiscordFrontend` for
   Discord-specific operations (channel lookups, reactions, channel renaming) that don't
   fit the generic trait.

### Live-Edit Streaming (Discord)

4. **Streaming mode.** When `STREAMING_DISCORD` is enabled, text deltas are accumulated
   into a live-edited Discord message with a block cursor (`U+2588`) appended to
   indicate ongoing output.

5. **Edit throttling.** Live-edit updates are throttled to one edit per
   `STREAMING_EDIT_INTERVAL`. The interval is an implementation choice within Discord's
   rate limits (axi-py: 1.5s, axi-rs: 0.8s).

6. **Message splitting.** When accumulated text exceeds the split threshold (1800-1900
   chars), the current message is finalized at the nearest preceding newline and a new
   message is started for the remainder. The split threshold must be below Discord's
   2000-char limit to leave room for the cursor and timing suffix.

7. **Finalization.** When the stream ends (turn complete, error, or kill), the cursor is
   removed from the current message, any remaining text is posted (splitting if needed),
   and the last flushed message ID is recorded for timing annotation.

### Buffered Mode (Discord)

8. **Non-streaming fallback.** When `STREAMING_DISCORD` is disabled, text is buffered
   until the turn completes, then sent via `send_long` which splits at 2000-character
   boundaries preferring newline split points.

9. **Deferred last message.** In non-streaming mode, the last message is deferred so
   response timing can be appended inline before sending.

### Thinking Indicator

10. **Show/hide.** A `*thinking...*` indicator message is posted when a `thinking`
    content block starts. It is deleted when any of the following occur: a non-thinking
    block starts, `end_turn`, `AssistantMessage`, or `ResultMessage`.

11. **Gating.** Thinking indicators only operate when live-edit streaming is enabled.

### Typing Indicator

12. **Scope.** `channel.typing()` runs for the entire stream duration. The typing task
    is cancelled on result, rate limit, or error.

### Response Timing

13. **Format.** Response timing is appended as a small-text suffix to the final message:
    `-# {elapsed}s` (e.g., `-# 4.2s`). Responses under 0.5 seconds may optionally skip
    the timing annotation.

14. **Trace ID.** When distributed tracing is active, a truncated trace ID is appended
    alongside the timing.

### Completion Reactions

15. **Success:** Checkmark reaction on the triggering message.
16. **Queued:** Mailbox/hourglass reaction when the message is queued for later
    processing.
17. **Error:** X reaction on error.
18. **Rate limit:** X reaction (not checkmark). The stream handler must not retry on
    rate limit.

### Plan Approval (Interactive Gate)

19. **Discord rendering.** The plan is posted as a file attachment. Approve (checkmark)
    and reject (cross) reaction buttons are added. The permission callback blocks until
    the user reacts or types feedback.

20. **Timeout.** Plan approval has a timeout (axi-rs: 10 minutes). On timeout, the
    result is deny.

21. **Cleanup.** After approval or rejection, the unchosen reaction emoji is removed for
    visual clarity.

### User Questions (Interactive Gate)

22. **Sequential posting.** Multi-question `AskUserQuestion` prompts are posted and
    answered one at a time, not all at once.

23. **Reaction options.** For each question, numbered keycap reactions are pre-added for
    each option. The gate waits for either a reaction or a typed answer.

24. **Reaction mapping.** Keycap emoji are mapped to option labels.
    Unrecognized emoji are ignored.

### Channel Status

25. **Status emoji.** Channel names are prefixed with status emoji on wake (working) and
    sleep (idle), gated by a `channel_status_enabled` config flag.

26. **Kill category.** On agent kill, the channel is moved to a "Killed" category if one
    exists in the guild infrastructure.

### Compaction UI

27. **Progress indication.** A spinner or progress message is shown when context
    compaction starts. A completion message with token count is shown when done.

### Debug Mode

28. **Thinking attachment.** In debug mode, thinking blocks are uploaded as
    `thinking.md` file attachments rather than displayed inline.

29. **Tool use preview.** In debug mode, tool uses are shown as inline code previews.

### Error Handling

30. **Rate limit backoff.** On HTTP 429, the live-edit backs off by the `retry_after`
    duration from the response.

31. **Transient error retry.** On transient API errors, stream processing retries with
    exponential backoff up to `max_retries`.

32. **Stream killed.** If the stream ends without a `ResultMessage`, in-flight content
    is flushed and the agent is force-slept so the next message triggers a fresh CLI
    process.

### Web Frontend (axi-rs only)

33. **WebSocket broadcast.** `WebFrontend` broadcasts JSON events to connected WebSocket
    clients with agent-level subscription filtering (empty subscription = all agents).

34. **Interactive gates.** Web plan approval uses oneshot channels with JSON gate
    messages rather than reactions.

### Exit Codes

35. **Restart signal.** `close_app` exits with code 42, which systemd treats as a clean
    restart trigger (`SuccessExitStatus=42`). `kill_process` exits with code 0.

## Invariants

The thinking indicator must be hidden on every exit path — non-thinking block start,
end_turn, AssistantMessage, and ResultMessage. Failure to hide it leaves a stale
indicator alongside response text. (I10.1-py)

Rate-limited messages must receive X (not checkmark) as their completion reaction, and
the stream handler must not retry. (I10.2-py)

Multi-question `AskUserQuestion` prompts must collect answers sequentially, not attempt
to split a single message across all questions. (I10.3-py)

Interactive gates must race across all frontends, returning the first response and
aborting the rest. Single-frontend blocking prevents other frontends from ever
responding. (I10.1-rs)

Discord-specific state must be separated from core bot state to support multiple
frontends. (I10.2-rs)

## Open Questions

1. **Edit interval divergence.** axi-py uses 1.5s, axi-rs uses 0.8s. Discord's rate
   limit is 5 edits per 5 seconds per channel (1.0s average). The 0.8s interval risks
   occasional rate limits under burst; the 1.5s interval is conservative but results in
   choppier output. Should a single value be normative?

2. **Message split threshold.** axi-py uses 1900 chars, axi-rs uses 1900 chars for
   live-edit but `send_long` splits at 2000. Should the split threshold be unified, and
   should it account for the timing suffix length?

3. **Frontend trait scope.** The `Frontend` trait in axi-rs includes lifecycle events
   (wake/sleep/spawn/kill) and channel status management. axi-py handles these as
   direct Discord API calls, not through the frontend adapter. Should lifecycle events
   be part of the normative frontend interface?

4. **Web frontend normative status.** axi-rs has `WebFrontend` with WebSocket
   broadcasting. axi-py has no web frontend. Should the web frontend be normative or
   implementation-optional?

5. **Awaiting-input sentinel.** axi-rs posts a "Bot has finished responding and is
   awaiting input" sentinel after query completion (gated by config). axi-py does not.
   This is used by the test harness to detect completion. Should this be normative
   behavior?

6. **Tool progress messages.** axi-rs has `show_tool_progress` / `delete_tool_progress`
   for ephemeral tool messages (e.g., `*Running command...*`), gated by
   `clean_tool_messages` flag. axi-py does not have this feature. Should tool progress
   messages be normative?

## Implementation Notes

**axi-py:** Rendering logic in `axi/discord_stream.py` with module-level functions
(`_live_edit_tick`, `_live_edit_finalize`, `_show_thinking`, `_hide_thinking`,
`_handle_stream_event`). `DiscordFrontend` is a thin adapter wrapping these functions
into the Frontend protocol. `stream_response_to_channel` is the top-level function that
runs the entire stream-to-Discord pipeline. `send_long` handles message splitting for
non-streaming mode. Plan approval and question handling are inline in the stream
handler. Edit interval: 1.5s. Split threshold: 1900 chars.

**axi-rs:** `Frontend` trait in `frontend.rs` with `DiscordFrontend`, `WebFrontend`,
and `FrontendRouter` implementations. `FrontendRouter` uses `futures_unordered_first`
for racing interactive gates. `BotState` holds both `FrontendRouter` and direct
`Arc<DiscordFrontend>`. Channel status emoji management on wake/sleep. Kill moves
channel to "Killed" category. Exit code 42 for restarts. Live-edit in `messaging.rs`
(`live_edit_tick`, `live_edit_finalize`). Edit interval: 0.8s. Split threshold: 1900
chars. `post_awaiting_input` sentinel after query completion. Activity phase tracking
(`Starting`/`Thinking`/`ToolUse`/`Working`/`Idle`).
