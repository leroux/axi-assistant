# RFC-0014: Interactive Gates

**Status:** Draft
**Created:** 2026-03-09

## Problem

Interactive gates (plan approval and user questions) block an agent's execution until a human responds via Discord reaction or typed message. The two implementations handle this differently: axi-py uses a permission callback chain with per-session futures and sequential question collection, while axi-rs uses a pending-questions map keyed by message ID with a multi-frontend racing model. The divergence means plan approval and question UX behaves differently across implementations, and neither documents the full state machine for gate lifecycle including cancellation.

## Behavior

### Gate Types

| Gate | Trigger | Resolution |
|------|---------|------------|
| **Plan Approval** | Agent calls ExitPlanMode | User reacts with approve/reject emoji, or types feedback |
| **User Question** | Agent calls AskUserQuestion | User reacts with numbered emoji, or types an answer |

### Plan Approval Flow

1. Agent calls ExitPlanMode with a plan.
2. Locate the plan content: first check the tool_input dict for a `"plan"` key; if absent, search for plan files on disk (agent's CWD for `PLAN.md`/`plan.md`, then `~/.claude/plans/`). Plan files older than 300 seconds MUST be ignored.
3. Post the plan to the agent's Discord channel as a file attachment.
4. Pre-add approve (checkmark) and reject (cross-mark) reaction emoji to the message.
5. Block the agent (via permission result future or pending-questions map).
6. Set channel status to `"plan_review"`.
7. Wait for resolution:
   - **Approve emoji** (U+2705, U+2714+FE0F): Set permission mode to `"default"`, clear plan_mode. Return allow.
   - **Reject emoji** (U+274C, U+274E): Return deny.
   - **Typed message**: Treat as feedback. Return deny with the feedback text.
8. After resolution, remove the unchosen reaction emoji for visual clarity.

### User Question Flow

1. Agent calls AskUserQuestion. The request may contain multiple questions.
2. Questions MUST be posted and awaited individually in sequence (not batched).
3. For each question:
   a. Post the question text to the agent's Discord channel.
   b. Pre-add numbered keycap emoji (1-4) matching the options, plus a custom-text emoji.
   c. Set channel status to `"question"`.
   d. Wait for resolution:
      - **Keycap emoji reaction**: Map the emoji index to the corresponding option label.
      - **Typed message**: Parse as option index (1-based) or literal text.
      - **Unrecognized emoji**: Re-insert the question into the pending state (do not consume it).
4. Collect all answers into a dict.
5. Inject the answers dict into the tool_input via `updated_input` on PermissionResultAllow.

### Reaction Filtering

- Reactions from the bot itself MUST be ignored.
- Reactions MUST only be processed from allowed users in the target guild.
- Reactions on messages not in the pending set: fall through to legacy handling (axi-rs sends a text message for non-pending plan reactions).

### Multi-Frontend Racing (axi-rs)

When multiple frontends are registered (e.g., Discord + web UI):
1. Race all frontends on the gate request. Each frontend's `request_plan_approval` / `ask_question` runs concurrently.
2. The first response wins; remaining tasks are aborted.
3. If no frontends are registered: plan approval auto-approves, questions auto-timeout.

### Cancellation (/stop)

The `/stop` command MUST resolve all pending gates for the agent:
- Pending plan approval futures: resolve with rejection.
- Pending question futures: resolve with empty string.
This unblocks the agent immediately.

### No Session / No Channel

If no session exists or the session has no `channel_id`, all gate handlers MUST return PermissionResultAllow without blocking. Gates are a UI concern; headless agents should not deadlock.

### TodoWrite (axi-py)

When an agent calls TodoWrite:
1. Format the todo list for Discord display.
2. Post to the agent's channel.
3. Persist to disk.

This is not a blocking gate — no user response required.

### Message Deduplication (axi-rs)

Discord gateway reconnects can replay events. A bounded FIFO dedup buffer (VecDeque) MUST prevent duplicate processing of the same message or reaction.

## Invariants

- **I-IG-1**: Multi-question AskUserQuestion MUST collect answers one at a time, each with its own future. Splitting a single reply across all questions produces mismatched answers. [axi-py I14.1]
- **I-IG-2**: Plan file discovery MUST search the agent's CWD (for PLAN.md/plan.md) in addition to ~/.claude/plans/. SDK agents write plans to their CWD. [axi-py I14.2]
- **I-IG-3**: Plan posting MUST fall back to reading the plan file from disk when the tool_input dict lacks a "plan" key. The LLM may omit the plan from tool_input. [axi-py I14.3]
- **I-IG-4**: Plan files older than 300 seconds MUST be ignored. Stale plans from a previous session should not be shown to the user. [axi-py I14.4]
- **I-IG-5**: Interactive gates MUST race across all active frontends; the first response wins, remaining are aborted. Without this, questions block indefinitely when the responding user is on a different frontend. [axi-rs I14.1]

## Open Questions

1. **Permission callback chain vs. pending map.** axi-py intercepts ExitPlanMode and AskUserQuestion via hook policies in a permission callback chain. axi-rs stores pending questions in a map keyed by Discord message ID and resolves them on reaction events. Should one approach be normative, or are both acceptable as long as the observable behavior matches?

2. **Multi-frontend racing.** axi-py does not have a multi-frontend racing model (Discord is the only frontend). axi-rs races Discord and web UI frontends. Should multi-frontend racing be normative? If so, what should happen when no frontends are registered?

3. **Legacy plan approval fallback.** axi-rs has a fallback for reactions on non-pending messages (sends a text message to the agent). axi-py does not. Should this fallback be normative, or removed?

4. **TodoWrite scope.** TodoWrite is only in axi-py and is not a blocking gate. Should it be part of this RFC or split into its own?

5. **Reaction cleanup.** axi-py removes the unchosen reaction after resolution. axi-rs does not specify this cleanup. Should post-resolution reaction cleanup be normative?

6. **Auto-approve behavior.** axi-rs auto-approves plans when no frontends are registered. axi-py requires a session with a channel to block. Should headless/no-frontend agents auto-approve plans?

## Implementation Notes

### axi-py
- Gate handlers in `axi/discord_ui.py`: `_handle_exit_plan_mode`, `_handle_ask_user_question`.
- `_read_latest_plan_file` searches CWD and `~/.claude/plans/` with 300s staleness check.
- `resolve_reaction_answer` maps keycap emoji to option labels.
- `parse_question_answer` handles typed replies (index or literal).
- `_post_todo_list` formats and persists todo lists.
- Permission callback chain in `packages/agenthub/agenthub/permissions.py`: `plan_approval_hook` and `question_hook` inserted between block/allow policies.
- Channel status detection in `axi/channels.py`: `_detect_agent_status`.
- `/stop` resolves `plan_approval_future` with rejection, `question_future` with empty string.
- Pre-adds both approve and reject emoji; removes the unchosen one after resolution.

### axi-rs
- `pending_questions` in `BotState` keyed by Discord message ID string.
- `handle_reaction_add` in `events.rs` removes from pending map, matches emoji, resolves.
- Unrecognized emoji re-inserts question into pending map (non-consuming).
- `EMOJI_NUMBERS` constant maps numbered emoji to option indices.
- `FrontendRouter::request_plan_approval` in `frontend.rs` races all frontends via `futures_unordered_first`.
- `futures_unordered_first` calls `set.abort_all()` after first result.
- Legacy fallback sends text "Plan approved. Proceed with implementation." for non-pending reactions.
- Message dedup via bounded `VecDeque` with FIFO eviction.
- `send_goodbye` to master channel only.
- No TodoWrite implementation.
- No post-resolution reaction cleanup.
