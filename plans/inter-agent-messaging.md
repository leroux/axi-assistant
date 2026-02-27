# Plan: Inter-Agent Messaging (Master → Spawned)

## Goal

Allow the master agent to send a message to any spawned agent via an MCP tool. The message appears in the target's Discord channel with sender attribution, and the target processes it like a normal user message — waking if asleep, interrupting if busy.

## Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Delivery mechanism | New MCP tool `axi_send_message` on `_axi_master_mcp_server` |
| 2 | Busy agent handling | Interrupt current query, drain only already-sent text (not user-queued messages). Inter-agent message processes next, before any queued user messages |
| 3 | Wake behavior | Same as Discord message — `wake_agent` with concurrency limit, `_wake_lock`, etc. |
| 4 | Discord visibility | Message posted to target's channel: `"📨 **Message from axi-master:**\n> {content}"` |
| 5 | Security | Tool-level gating — only master has `axi_send_message`. Target must exist and not be master. No crypto needed (no untrusted transport) |
| 6 | Direction | Master → spawned only (for now) |
| 7 | Queue data structure | Switch `message_queue` from `asyncio.Queue` to `collections.deque` for `appendleft` (priority insertion) |

## Implementation

### Step 1: Switch `message_queue` to `collections.deque`

`asyncio.Queue` doesn't support push-to-front. Every existing usage is synchronous (`put()` never blocks on an unbounded queue, `get_nowait()`, `empty()`, `qsize()`), so `deque` is a drop-in.

**Change the field definition** at line 256:
```python
# Before
message_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
# After
message_queue: deque = field(default_factory=deque)
```

**Migration of all 20 call sites:**

| Pattern | Before (`asyncio.Queue`) | After (`deque`) | Lines |
|---|---|---|---|
| Add to back | `await session.message_queue.put((x, y, z))` | `session.message_queue.append((x, y, z))` | 1493, 3154, 3656, 3683 |
| Remove from front | `session.message_queue.get_nowait()` | `session.message_queue.popleft()` | 3220, 3249, 3893, 4476 |
| Check empty | `session.message_queue.empty()` | `not session.message_queue` | 3216, 3248, 3891, 4475 |
| Get size | `session.message_queue.qsize()` | `len(session.message_queue)` | 1494, 3222, 3657, 3684, 4281, 4332, 4517 |

Add `from collections import deque` to imports.

### Step 2: Add `_deliver_inter_agent_message()`

Core delivery function. Three cases based on target state:

```
async def _deliver_inter_agent_message(
    sender_name: str,
    target_session: AgentSession,
    content: str,
) -> str:
```

**Case A — Target is sleeping** (not awake, query_lock not held):
1. Post attributed message to target's Discord channel
2. Put `(content, channel, None)` into target's `message_queue`
3. Acquire `query_lock`, wake agent, process message (same as `on_message` normal path)
4. Drain queue after completion

This is essentially what `on_message` does for a sleeping agent. We can reuse `_run_initial_prompt` directly by putting the message into the queue and triggering the wake+process cycle, but it's cleaner to follow the `on_message` pattern since `_run_initial_prompt` sleeps the agent after finishing (which we may not want if there are follow-up messages).

Better: create an `asyncio.Task` that mimics the `on_message` normal path:
- Acquire `query_lock`
- `wake_or_queue` (handles concurrency limit)
- `session.process_message(content, channel)`
- `_process_message_queue(session)` after

**Case B — Target is idle** (awake, query_lock not held):
Same as Case A but skip the wake step. Acquire lock, send query, drain queue.

**Case C — Target is busy** (query_lock held):
1. Post attributed message to target's Discord channel
2. Push `(content, channel, None)` to **front** of queue via `appendleft`
3. Interrupt: `bridge_conn.send_command("interrupt")` or `client.interrupt()`
4. The interrupted query's `stream_response_to_channel` drains remaining text, returns
5. The caller's `_process_message_queue` picks up our front-of-queue message next
6. User-queued messages process after

**Key insight for Case C**: We don't need to manage the lock ourselves. The interrupt causes the current holder of `query_lock` to finish (the stream drains, lock releases, `_process_message_queue` runs). By putting our message at the front of the deque *before* interrupting, the existing `_process_message_queue` flow handles everything correctly.

### Step 3: Add `axi_send_message` MCP tool

```python
@tool(
    "axi_send_message",
    "Send a message to a spawned agent. The message appears in the agent's Discord channel "
    "and is processed like a user message. If the agent is busy, its current query is "
    "interrupted and this message is processed next.",
    {
        "type": "object",
        "properties": {
            "agent_name": {"type": "string", "description": "Name of the target agent"},
            "content": {"type": "string", "description": "Message content to send"},
        },
        "required": ["agent_name", "content"],
    },
)
async def axi_send_message(args):
    ...
```

**Validation:**
- `agent_name` must exist in `agents` dict
- `agent_name` must not be `MASTER_AGENT_NAME` (no self-messaging)
- `content` must be non-empty

**Behavior:**
- Calls `_deliver_inter_agent_message` in a background `asyncio.Task` (same pattern as spawn/kill)
- Returns immediately with confirmation

### Step 4: Register the tool on master MCP server

At line 1268:
```python
_axi_master_mcp_server = create_sdk_mcp_server(
    name="axi",
    version="1.0.0",
    tools=[axi_spawn_agent, axi_kill_agent, axi_restart, axi_send_message],  # added
)
```

### Step 5: Update SOUL.md

Add a brief section documenting the tool so the master agent knows it exists:

```markdown
## Inter-Agent Messaging

You can send messages to spawned agents using the `axi_send_message` MCP tool:
- The message appears in the target agent's Discord channel with your name as sender
- If the agent is sleeping, it wakes up to process the message
- If the agent is busy, its current work is interrupted and your message is processed next
- User-queued messages are preserved and process after your message
```

---

## File Changes Summary

| File | Change |
|---|---|
| `bot.py` | Add `from collections import deque` |
| `bot.py` | Change `message_queue` field type from `asyncio.Queue` to `deque` |
| `bot.py` | Update ~20 call sites (mechanical replacements) |
| `bot.py` | Add `_deliver_inter_agent_message()` function |
| `bot.py` | Add `axi_send_message` MCP tool |
| `bot.py` | Add tool to `_axi_master_mcp_server` |
| `SOUL.md` | Add inter-agent messaging section |

## Risks

- **Interrupt timing**: If the interrupt arrives between `stream_response_to_channel` finishing and `_process_message_queue` starting, our `appendleft`'d message is already in the deque and will be picked up normally. Safe.
- **Interrupt failure**: If `client.interrupt()` fails (agent already finished), no harm — our message is in the deque and `_process_message_queue` will pick it up on the next drain cycle.
- **Concurrency limit on wake**: If all slots are full when waking a sleeping target, the message gets queued (same as user messages). Master gets a response saying delivery was queued.
- **deque thread safety**: `deque.append` and `deque.popleft` are atomic in CPython. All our usage is on the asyncio event loop thread anyway, so no risk.
