# BUGS

## 2026-04-17 stop / queue interaction bugs

### 1. `/stop` drops queued follow-up messages

**Status:** Reproduced.

**Symptom:**
- If the agent is busy, a follow-up message is queued.
- If `/stop` or `//stop` is sent before that queued item runs, the queue is drained.
- This makes it look like the first post-stop follow-up "didn't count"; the next surviving message becomes the one that runs.

**Evidence:**
- Busy inbound messages are queued in `axi/main.py:373-397`.
- Slash `/stop` drains `session.message_queue` in `axi/main.py:1380-1393`.
- Text `//stop` drains `session.message_queue` in `axi/main.py:1684-1695`.
- Focused repro test covers both behaviors in `tests/unit/test_stop_queue_repro.py:105-147`.
- Reducer tests encode the same stop-vs-skip policy in `tests/unit/test_receive_user_message.py:197-223`.

**Impact:**
- User intent is lost during interrupt.
- This directly explains the "only works after the second message" symptom when the first intended follow-up was already queued before stop.

### 2. Interrupted/killed stop path does not explicitly clear the thinking / typing UI

**Status:** Confirmed by code inspection; matches the live symptom the user reported.

**Symptom:**
- After `/stop`, Discord can keep showing `*thinking...*` or a typing/busy state until interrupted-turn cleanup finishes.

**Evidence:**
- Thinking indicator is shown in `axi/discord_stream.py:514-520`.
- Typing indicator cancellation lives in `axi/discord_stream.py:534-538`.
- Normal end/result/error paths clear thinking and/or typing in `axi/discord_stream.py:635-638`, `674-687`, and `716-722`.
- The killed/no-result path finalizes output and force-sleeps the agent, but does not call `_hide_thinking` or `_stop_typing`: `axi/discord_stream.py:1016-1034`.
- Live user report in `#1494223768007217203` at `2026-04-17 05:19:05+00:00`: "when i send stop in the middle of stuff happening is says Axi is thinking ... and finally when it says \"interrupted turn\" ... it can accept messages again."

**Impact:**
- The UI looks stuck even after the interrupt has been issued.
- Stop feels slow and unreliable even when transport-level stop is fast.

### 3. Typed `/stop` in channel is not immediate control traffic

**Status:** Confirmed by code and live channel evidence.

**Symptom:**
- A user typing `/stop` as a normal message in the channel does not hit the dedicated text-command interrupt path.
- It can be queued like ordinary content and only processed later.

**Evidence:**
- The text command handler only recognizes `//...` messages in `axi/main.py:1504-1508`.
- Regular busy-path messages are queued in `axi/main.py:373-397`.
- In `#1494187568160575578` at `2026-04-17 05:19:34+00:00`, the bot posted:
  - `*System:* Processing queued message:`
  - `> [2026-04-17 05:19:23 UTC] /stop`
- The follow-up reply at `2026-04-17 05:19:42+00:00` was delayed: `Stopped. There’s no active task running right now.`

**Impact:**
- There are effectively three different stop semantics right now:
  - Discord slash command `/stop`
  - text command `//stop`
  - plain typed message `/stop`
- The plain-message form is easy to type by habit, but it has the wrong semantics and creates confusing delayed-stop behavior.

## Observed in this interaction but not root-caused yet

### 4. Polling CLI modes are still unreliable for multi-message bot responses

**Status:** Reproduced after the polling work landed.

**Symptom:**
- `axi_test.py msg --wait-mode stable` can return too early on the first non-system bot line instead of waiting for the full response burst to settle.
- `axi_test.py msg --wait-mode substring --check ...` can time out even when the target substring later appears in channel history.

**Evidence:**
- `axi_test.py` delegates polling modes to `wait_for_messages(...)` in `axi_test.py:880-904`.
- The shared helper returns immediately on the first matching substring line, and its stability behavior is purely poll-based rather than response-burst aware: `packages/discordquery/discordquery/wait.py:75-147`.
- Live verification on `stop-flowcoder-investigation` showed:
  - stable mode returned only the early banner line (`⚠️ Running on **gpt-5.4** — switch to opus with `/model opus` for best results.`)
  - substring mode timed out even though `SUBSTRING_POLL_OK` later appeared in channel history
- Direct channel history read confirmed the token was present after the failed substring-mode run.

**Impact:**
- The new polling modes are useful for ad hoc inspection, but they are not yet reliable enough to replace sentinel-based or manual history-based verification for multi-message Discord bot turns.

### 5. Query failure required manual `continue`

**Status:** Observed only.

**Evidence:**
- In `#1494223768007217203` at `2026-04-17 05:39:30+00:00`, the system posted: `*System:* Query failed for agent 'stop-flowcoder-investigation'`.
- The user had to send `continue` at `2026-04-17 05:39:37+00:00` to resume the investigation.

**Notes:**
- I do not have a code-level root cause for this one yet, so it should be treated as an observed failure, not a confirmed `/stop` bug.

## Non-bug note

- Claudewire stop semantics looked healthy in isolation during this investigation; the Axi-specific queue / Discord orchestration is where the confirmed bugs above showed up. Supporting evidence: `tests/unit/test_stop_queue_repro.py:105-147` and the investigation summary in `#1494223768007217203` at `2026-04-17 05:44:52+00:00`.
