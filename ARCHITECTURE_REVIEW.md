# Axi Assistant - Comprehensive Code Review & Architecture Analysis

**Date:** 2026-02-27
**Scope:** bot.py, flowcoder.py, supervisor.py, bridge integration
**Status:** Active production system with optional bridge persistence

---

## EXECUTIVE SUMMARY

The axi-assistant codebase is a sophisticated Discord bot that manages multiple concurrent Claude Code agents and flowcoder processes. The architecture is **functional but showing signs of complexity buildup** in three critical areas:

1. **Cascading conditionals** in message routing (8-10 levels of nesting)
2. **Bridge integration** that claims to be optional but is deeply baked into the design
3. **Duplicated state management** across Claude Code and flowcoder agent types

The system works well but has reached a complexity threshold where further additions will become increasingly difficult to reason about.

---

## 1. AGENT LIFECYCLE & MANAGEMENT

### Current Flow

```
spawn_agent()
    ↓
Create AgentSession (client=None, sleeping)
    ↓
Register in agents{} dict
    ↓
on_message() → wake_agent() [if needed]
    ↓
ClaudeSDKClient created (direct subprocess or bridge)
    ↓
_run_initial_prompt() / _stream_with_retry()
    ↓
sleep_agent() [after query completes]
    ↓
_disconnect_client() + cleanup
```

### Key Components

**AgentSession (lines 162-213)**
- 19 fields tracking agent state, client, locks, activity, etc.
- Per-agent logger setup in `__post_init__`
- Includes unused `system_prompt_posted` flag (line 173)
- Bridge-specific fields: `_reconnecting`, `_bridge_busy` (lines 180-181)

**Initialization Paths**
- **Claude Code agents** (line 2265-2278): System prompt built from SOUL.md + dev_context.md
- **Flowcoder agents** (line 2252-2264): No system prompt, command/args stored instead
- **Master agent** (line 4268-4276): Always Claude Code with special MCP servers

### Issues Found

| Issue | Location | Severity | Impact |
|-------|----------|----------|--------|
| Agent type baked into many conditionals | Throughout bot.py | Medium | Hard to support new agent types |
| `_system_prompt_posted` flag seems unused | Line 173 | Low | Dead code? |
| Two separate wake paths (bridge vs direct) | Lines 1299-1325 vs 1326-1344 | Medium | Code duplication |
| Flowcoder process lifecycle unclear on sleep | Line 1186-1195 | Medium | Bridge detach vs stop logic mixed |
| Session reconstruction skips Killed category | Line 1480 | Low | Inconsistent state after restarts |

---

## 2. CONDITIONALS & CONTROL FLOW MAP

### Message Routing Decision Tree (`on_message`, lines 2973-3139)

```
Message arrives
├─ Reject: own messages (2974)
├─ Reject: system events (2976)
├─ Reject: unauthorized bots (2978)
├─ Reject: DMs (2982)
├─ Reject: wrong guild (2995)
├─ Reject: unauthorized user (2998)
├─ Reject: shutdown in progress (3005)
├─ Unknown channel → return (3011)
├─ Killed category → reject (3023-3025)
│
├─ RECONNECTING MODE (3028-3037)
│   └─ Queue message
│
├─ FLOWCODER RUNNING (3042-3046)
│   └─ Send to flowcoder stdin
│
├─ FLOWCODER FINISHED (3049-3051)
│   └─ Reject message
│
├─ RATE LIMITED + NOT LOCKED (3054-3065)
│   └─ Queue message
│
├─ QUERY LOCK HELD (3067-3077)
│   └─ Queue message
│
└─ NORMAL PATH: Acquire query_lock (3079)
    ├─ Wake if sleeping (3081-3104)
    │   ├─ ConcurrencyLimitError → queue (3085-3096)
    │   └─ Other error → reject (3097-3104)
    └─ Query + stream (3106-3132)
        ├─ Timeout → special handling (3120-3122)
        └─ Error → reject (3123-3130)
```

**Nesting Depth:** 8 levels
**Branch Count:** 20+ distinct paths
**Implicit Assumptions:**
- `session._reconnecting` only set by bridge reconnect logic
- `session.flowcoder_process.is_running` reflects true engine state
- `session.query_lock` is the only mutual exclusion mechanism

### Agent Wake Decision Tree (`wake_agent`, lines 1259-1360)

```
wake_agent(session)
├─ Flowcoder agent → no-op return (1271-1272)
├─ Already awake (client != None) → return (1274-1275)
│
├─ Acquire _wake_lock (serialize concurrent wakes)
│   ├─ Re-check awake (TOCTOU protection) (1279-1280)
│   ├─ Check concurrency limit (1285)
│   │   └─ If full → ConcurrencyLimitError (1287-1290)
│   │
│   └─ BRIDGE MODE (1299-1325)
│       ├─ _wake_agent_via_bridge() (1303)
│       │   ├─ Create BridgeTransport (1237-1241)
│       │   ├─ Spawn via bridge (1244-1245)
│       │   └─ Subscribe (1251)
│       ├─ On failure with resume → retry fresh (1309-1321)
│       └─ On all failure → raise
│
│   └─ DIRECT MODE (1326-1344)
│       ├─ ClaudeSDKClient direct (1329-1334)
│       ├─ On failure with resume → retry fresh (1336-1344)
│
│   └─ POST-WAKE: Post system prompt (1346-1359)
```

**Issue:** Two separate code paths (bridge vs direct) that should be unified.

### Scheduler (`check_schedules`, lines 3145-3227+)

```
check_schedules() [runs every 10s]
├─ Shutdown check (3147-3148) → return
├─ Prune history/skips (3150-3151)
├─ For each schedule entry:
│   ├─ RECURRING (has "schedule" field)
│   │   ├─ Validate cron (3172-3174)
│   │   ├─ Get last occurrence (3176)
│   │   ├─ Check for skip (3185)
│   │   └─ Fire if needed (3189-3204)
│   │       ├─ If session exists → send_prompt_to_agent() (3201)
│   │       └─ Else → spawn new agent (3204)
│   │
│   └─ ONE-OFF (has "at" field)
│       ├─ Parse ISO datetime (3208)
│       └─ Fire if past (3210-3225)
```

**Issue:** Very similar logic for recurring and one-off events — could be unified.

---

## 3. CODE QUALITY & SMELLS

### Deep Nesting Issues

**Issue 3.1: `on_message()` (lines 2973-3139)**
```python
# 8 levels deep in some branches:
async def on_message(message):
    if message.author.id == bot.user.id:  # Level 1
        return
    ...
    async with session.query_lock:  # Level 4-5
        if session.client is None:  # Level 5-6
            try:  # Level 6-7
                await wake_agent(session)  # Level 7-8
            except ConcurrencyLimitError:  # Level 7-8
                await session.message_queue.put(...)  # Level 8-9
                # 9 levels deep!
```

**Impact:** Hard to read, easy to miss error cases, difficult to test individual branches.

### Code Duplication

**Issue 3.2: Wake logic appears in 3 places**
- Direct in `on_message()` (lines 3081-3104)
- Duplicate in `_process_message_queue()` (lines 2642-2655)
- Duplicate in `_run_initial_prompt()` (lines 2556-2572)

**Issue 3.3: Bridge transport creation appears in 3+ places**
- `_wake_agent_via_bridge()` (lines 1237-1245)
- `_reconnect_and_drain()` (lines 2791-2796)
- `_run_flowcoder()` (lines 2339-2346)
- `_run_inline_flowchart()` (lines 2394-2398)

### Unclear Dependencies

**Issue 3.4: Bridge is "optional" but pervasive**

```python
# Claimed optional at line 2701:
"Failed to connect to bridge — agents will use direct subprocess mode"

# But appears in 15+ conditional checks:
if bridge_conn and bridge_conn.is_alive:  # Lines 1299, 2339, 2393, etc.

# And is REQUIRED for reconnection:
session._reconnecting = True  # Only set by bridge reconnect (2744, 2765)

# And stored in session state:
_bridge_busy = False  # Flag only meaningful in bridge mode (1181)
```

**Real Status:** Bridge is optional for initial wakes but **required for certain features** like agent reconnection after bot restart.

**Verdict: Bridge should be required.** See section 5.

---

## 4. ARCHITECTURE ISSUES

### Problem 1: Two Agent Systems in One Class

**Current State:**
- Claude Code agents: wake/sleep on demand, persistent session, interactive
- Flowcoder agents: spawn and run to completion, no wake/sleep

Both crammed into `AgentSession` with type dispatch:

```python
if session.agent_type == "flowcoder":
    # ... flowcoder logic
else:  # claude_code
    # ... claude code logic
```

This pattern appears 10+ times throughout bot.py.

**Consequence:** Adding a new agent type requires grep-and-modify across entire codebase.

### Problem 2: Bridge Integration Is Asymmetric

**Initial spawn:**
- Bridge optional: Both paths fully functional

**On restart:**
- Bridge required for `_reconnect_and_drain()`
- Direct subprocess agents lose their PID, cannot resume
- No reconnect path for non-bridge agents

**Session persistence:**
- Bridge-backed agents can resume from `_bridge_busy` flag
- Direct subprocess agents have no persistence

**Consequence:** Bridge evolved from "optional optimization" to "required infrastructure."

### Problem 3: Message Queue Handles Too Much

The single message queue handles:
- Rate limiting (global backpressure)
- Agent concurrency (limited slots)
- Agent busy (sequential per-agent)
- Reconnect (temporary state)

These are different concerns that should be handled separately.

---

## 5. BRIDGE REQUIREMENT ANALYSIS

### What Actually Requires Bridge?

| Feature | Bridge | Direct | Status |
|---------|--------|--------|--------|
| Spawn agent | ✅ | ✅ | Both work |
| Wake agent | ✅ | ✅ | Both work |
| Run Claude Code query | ✅ | ✅ | Both work |
| Run flowcoder inline | ✅ | ✅ | Both work |
| **Restart + reconnect** | ✅ | ❌ | **BROKEN in direct mode** |
| **Restart + resume session** | ✅ | ❌ | **BROKEN in direct mode** |

### Verdict: Bridge Is Not Optional

Bridge was designed as an optional optimization but the architecture evolved to require it:

1. **Without bridge:** Bot restart = all agents die (sessions lost)
2. **With bridge:** Bot restart = agents reconnect automatically

The fallback to "direct subprocess mode" is **incomplete**. It should either:
- **Option A:** Make bridge truly optional (implement reconnect for direct mode)
- **Option B:** Admit bridge is required and simplify code

**Recommendation:** **Go with Option B.** Bridge is already essential, and pretending it's optional adds complexity.

---

## 6. SPECIFIC CODE SMELLS

### Smell 1: Repeated Wake Pattern (3 locations)

Lines 3081, 2642, 2556 have identical wake/error logic:

```python
try:
    await wake_agent(session)
except ConcurrencyLimitError:
    await session.message_queue.put((content, message.channel, message))
except Exception:
    log.exception("Failed to wake agent")
    await send_system(channel, "Failed to wake agent...")
```

**Fix:** Extract to `wake_or_queue()` helper.

### Smell 2: Bridge Transport Creation (4 locations)

Lines 1237, 2791, 2339, 2394 create `BridgeTransport` similarly:

```python
transport = BridgeTransport(session.name, bridge_conn, ...)
await transport.connect()
```

**Fix:** Extract to `create_bridge_transport()` helper.

### Smell 3: Agent Type Conditionals (15+ locations)

Checking `agent_type == "flowcoder"` scattered throughout.

**Fix:** Create polymorphic agent classes (see Refactoring section).

### Smell 4: Missing Timeout on Subscribe

Line 2799: `sub_result = await transport.subscribe()` has no timeout.

**Fix:** Add `asyncio.timeout(10.0)` wrapper.

### Smell 5: Process Leak Workaround

Lines 1020-1038: Manual SIGTERM to subprocess that "survived disconnect."

**Root cause:** SDK cleanup not reliable. This is a symptom of deeper issue.

---

## 7. REFACTORING RECOMMENDATIONS

### Refactor 1: Extract Wake Helpers (Effort: 2 hours)

Create `wake_or_queue()` to eliminate 3 duplicate wake/error paths.

**Impact:** 30 lines removed, single source of truth for wake failure.

### Refactor 2: Unify Bridge vs Direct Mode (Effort: 1 day)

Two separate code paths in `wake_agent()` (lines 1299 vs 1326).

**Goal:** Single code path with pluggable transport:

```python
# Create transport (bridge or direct)
transport = await create_transport(session, resume_id)

# Use it the same way
client = ClaudeSDKClient(options=options, transport=transport)
```

**Impact:** Eliminate 50 lines, make reconnect logic uniform.

### Refactor 3: Agent Type Polymorphism (Effort: 2-3 days)

Create `Agent` base class, move type-specific logic into subclasses.

**Impact:** Remove 50+ conditional checks, make adding new types trivial.

### Refactor 4: Separate Bridge Persistence (Effort: 1 day)

Move all bridge reconnect logic to a `BridgePersistence` class.

**Impact:** Agent lifecycle code becomes 200 lines shorter, bridge behavior testable independently.

---

## 8. CRITICAL RECOMMENDATIONS

### Priority 1: Admit Bridge Is Required

Remove pretense of bridge being optional. Simplify code accordingly.

**Impact:** Removes 10+ conditional checks, clarifies architecture.

### Priority 2: Extract Shared Functions

Create `wake_or_queue()`, `create_bridge_transport()` helpers.

**Impact:** Reduces duplication, improves maintainability.

### Priority 3: Decouple Bridge from Agent Lifecycle

Move bridge reconnect logic to separate class.

**Impact:** Cleaner separation of concerns.

### Priority 4: Agent Type Polymorphism

Create `Agent` base class for new agent types.

**Impact:** Future-proofs the system for extensibility.

---

## 9. SUMMARY

| Category | Current State | Issues | Priority |
|----------|---------------|--------|----------|
| **Lifecycle** | Lazy wake/sleep works | Flowcoder lifecycle unclear | Medium |
| **Conditionals** | 8-10 nesting levels | Hard to read/test | High |
| **Duplication** | 3x wake, 4x transport | Maintenance burden | High |
| **Bridge** | "Optional" but required | Misleading architecture | Critical |
| **Agent Types** | String-based dispatch | Hard to extend | Medium |
| **Error Handling** | Asymmetric | Some timeouts missing | Medium |

---

## 10. CONCLUSION

The axi-assistant architecture **works well** but has **reached a complexity threshold**. The system needs clarification on:

1. **Bridge is required** — stop pretending it's optional
2. **Agent types need polymorphism** — prepare for new types
3. **Control flow needs simplification** — extract helpers to reduce nesting

**If adding features in the next 6 months:** Refactor now (2-3 days investment).

**If keeping as-is:** Add documentation explaining implicit assumptions.

---

Generated: 2026-02-27
