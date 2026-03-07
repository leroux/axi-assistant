# Multi-Frontend Architecture for Axi

## 1. Current State: Discord Coupling Map

### Fully Discord-coupled files (must be abstracted)

**`axi/main.py` (2310 lines) -- The Discord event loop**
- Lines 20-24: `import discord`, `TextChannel`, `app_commands`, `Bot`
- Line 56: `Bot(command_prefix="!", intents=config.intents)` -- the bot singleton
- Lines 59-65: `global_auth_check` -- Discord interaction auth
- Lines 84-95: `on_error` -- Discord event handler
- Lines 98-155: `on_raw_reaction_add` -- reaction-based plan approval & question answering
- Lines 158-376: `on_message` -- the entire message routing pipeline (dedup, auth, DM redirect, plan approval gate, AskUserQuestion gate, text commands, backpressure, wake/query/stream)
- Lines 384-563: `check_schedules` loop -- uses `discord.ext.tasks.loop`, sends to Discord channels
- Lines 576-597: Discord autocomplete helpers
- Lines 600-627: `_resolve_agent` -- Discord interaction resolution
- Lines 634-900+: All slash commands (`/ping`, `/claude-usage`, `/model`, `/list-agents`, `/status`, `/stop`, `/kill-agent`, `/debug`, `/spawn`)

**`axi/channels.py` (449 lines) -- Discord guild infrastructure**
- Everything is Discord-specific: categories (Axi/Active/Killed), channel creation, permission overwrites, channel movement, topic management
- New: channel pinning (pin #axi-master to position 0), channel reordering by recency
- Pure helpers `normalize_channel_name`, `format_channel_topic`, `parse_channel_topic` encode Discord conventions

**`axi/agents.py` (2977 lines) -- Mixed: core logic + Discord rendering**
- Lines 285-306: `add_reaction`, `remove_reaction` -- Discord-only
- Lines 317-371: `extract_message_content` -- Discord attachment/image handling
- Lines 429-468: `send_long`, `send_system` -- Discord message sending
- Lines 590-845: Permission callbacks with Discord-specific `_handle_exit_plan_mode` (posts plan to Discord, uses reactions), `_handle_ask_user_question` (posts questions, uses emoji reactions)
- Lines 892-930: `_post_todo_list` -- always posts new messages now (no more in-place editing)
- Lines 925-947: Rate limit broadcast to Discord channels
- Lines 1074-1124: `wake_agent` -- Discord post-wake logic (post system prompt, model warning)
- Lines 1127-1170: `wake_or_queue` -- immediate hourglass reaction feedback while waking
- Lines 1227-1280: `reconstruct_agents_from_channels` -- rebuilds state from Discord channel topics
- Lines 1288-1377: Session ID persistence via channel topics
- Lines 1455-1476: `_LiveEditState`, `_StreamCtx` -- Discord streaming state
- Lines 1984-2113: `stream_response_to_channel` -- the streaming engine with live-editing, typing indicators, inline duration timing appended via message edit
- Lines 2158-2207: `stream_with_retry` -- uses Discord channel for error messages
- Lines 2210-2270: `handle_query_timeout` -- uses Discord channel for error messages
- Lines 2273-2315: `process_message` -- calls `stream_with_retry(session, channel)`
- Lines 2357-2465: `spawn_agent` -- creates Discord channel, sends system messages
- Lines 2613+: `deliver_inter_agent_message` -- posts to Discord channel

**`axi/tools.py` (620 lines) -- MCP tool definitions**
- Lines 43-225: `axi_spawn_agent` -- mentions "Discord channel" in description, calls Discord channel creation
- Lines 228-287: `axi_kill_agent` -- moves to "Killed category"
- Lines 426-470: `discord_send_file` -- Discord-specific tool
- Lines 478-597: `discord_list_channels`, `discord_read_messages`, `discord_send_message` -- Discord REST tools

**`axi/prompts.py` (243 lines) -- System prompt construction**
- Lines 125-146: `_AGENT_CONTEXT_PROMPT` -- hardcoded "Discord-based personal assistant", "Discord text channel"
- Lines 206-243: `post_system_prompt_to_channel` -- uses `discord.File`, `TextChannel`

**`axi/config.py` (324 lines) -- Configuration**
- Lines 147-189: `_resolve_discord_token` -- Discord token resolution
- Line 199: `DISCORD_GUILD_ID`
- Lines 209-217: Discord intents
- Lines 314-316: Category names
- Lines 322-324: `discord_client = AsyncDiscordClient(DISCORD_TOKEN)`

**`axi/hub_wiring.py` (222 lines) -- AgentHub construction**
- Lines 31-118: `_build_callbacks` -- all callbacks delegate to Discord channel operations
- Lines 126-147: `_make_agent_options` -- NOT Discord-specific
- Lines 150-178: `_create_client` -- NOT Discord-specific

**`axi/axi_types.py` (136 lines) -- Data types**
- Lines 69-101: `DiscordAgentState` -- Discord-specific session state (channel_id, reactions, question/plan futures, todo items)
- Lines 104-108: `discord_state()` helper

### Already frontend-agnostic (no changes needed)

| Component | Location | Notes |
|-----------|----------|-------|
| **AgentHub** | `packages/agenthub/` | Core orchestrator. Uses `FrontendCallbacks` protocol. Zero Discord imports. |
| **Scheduler** | `agenthub/scheduler.py` | Slot management, eviction. Pure logic. |
| **Lifecycle** | `agenthub/lifecycle.py` | Wake/sleep, transport. Calls callbacks for notifications. |
| **Registry** | `agenthub/registry.py` | Session dict, spawn, kill. |
| **Messaging** | `agenthub/messaging.py` | Queue, backpressure, inter-agent. |
| **Rate limits** | `agenthub/rate_limits.py` | Quota tracking, history. |
| **Shutdown** | `agenthub/shutdown.py` | Graceful shutdown coordinator. |
| **Reconnect** | `agenthub/reconnect.py` | Procmux hot restart. |
| **Procmux** | `packages/procmux/` | Process multiplexer. Pure infrastructure. |
| **Claude Wire** | `packages/claudewire/` | Stream-JSON protocol wrapper. |
| **Flowcoder** | `packages/flowcoder_engine/` | Flowchart executor. |
| **AgentSession** | `agenthub/types.py` | Core session type. Has `frontend_state: Any` for opaque frontend data. |

**Key insight**: AgentHub already has the right abstraction boundary. `FrontendCallbacks` is the frontend interface -- it's just incomplete. The AgentHub extraction was designed with multi-frontend in mind (see existing PLAN.md: "The frontend (Discord, CLI, web) plugs in via FrontendCallbacks").

---

## 2. Architecture Recommendation: Frontend Adapter Pattern

### Why not the others?

**Event bus**: Over-engineering. We have a single-process system, not a distributed one. An event bus adds indirection and makes control flow harder to trace. AgentHub's callback pattern is already the right level of abstraction.

**HTTP API as primary**: Adds a network hop between components that live in the same process. Good for a public API later, but shouldn't be the internal architecture. A web frontend can connect directly to AgentHub via WebSocket within the same process.

**Adapter pattern**: Natural extension of what exists. AgentHub already talks to `FrontendCallbacks`. Each frontend implements this interface. Multiple frontends can run simultaneously because they each get their own callbacks and state.

### Architecture

```
                    +---------------------+
                    |      AgentHub       |
                    |  (session orchestr.) |
                    +---------+-----------+
                              |
                   FrontendCallbacks (per-frontend)
                              |
              +---------------+---------------+
              |               |               |
   +----------+------+  +----+----+  +-------+-----+
   | DiscordFrontend  |  |WebFront.|  |SlackFrontend|
   |  (discord.py)    |  |(fastapi)|  |  (future)   |
   +------------------+  +---------+  +-------------+
```

But there's a critical nuance: **AgentHub currently supports only one set of FrontendCallbacks**. For multiple simultaneous frontends, we need a **multiplexer**:

```
                    +---------------------+
                    |      AgentHub       |
                    +---------+-----------+
                              |
                   FrontendCallbacks
                              |
                    +---------+-----------+
                    |   FrontendRouter    |
                    | (multiplexes calls  |
                    |  to all frontends)  |
                    +---------+-----------+
                              |
              +---------------+---------------+
              |               |               |
        DiscordFrontend  WebFrontend    SlackFrontend
```

### FrontendRouter behavior

The router wraps N frontends and dispatches callbacks:

- **`post_message(agent_name, text)`** -> send to ALL frontends (every connected UI shows the message)
- **`post_system(agent_name, text)`** -> send to ALL frontends
- **`on_wake/on_sleep/on_spawn/on_kill`** -> notify ALL frontends
- **`broadcast(text)`** -> send to ALL frontends
- **`get_channel(agent_name)`** -> return the "primary" frontend's channel (or an abstracted channel handle)
- **`close_app()`** -> close ALL frontends
- **Message ingestion** is the reverse: each frontend independently calls `hub.send()` when it receives a user message. No routing needed -- hub handles it.

---

## 3. The Frontend Interface

### What a frontend must implement

```python
@dataclass
class Frontend:
    """A single frontend adapter (Discord, Web, Slack, etc.)."""

    name: str  # "discord", "web", "slack"

    # --- Outbound: hub -> frontend ---
    async def post_message(self, agent_name: str, text: str) -> None: ...
    async def post_system(self, agent_name: str, text: str) -> None: ...
    async def on_wake(self, agent_name: str) -> None: ...
    async def on_sleep(self, agent_name: str) -> None: ...
    async def on_spawn(self, session: AgentSession) -> None: ...
    async def on_kill(self, agent_name: str, session_id: str | None) -> None: ...
    async def on_session_id(self, agent_name: str, session_id: str) -> None: ...
    async def broadcast(self, text: str) -> None: ...
    async def on_idle_reminder(self, agent_name: str, idle_minutes: float) -> None: ...
    async def on_reconnect(self, agent_name: str, was_mid_task: bool) -> None: ...

    # --- Stream rendering ---
    async def stream_event(self, agent_name: str, event: StreamEvent) -> None: ...
    async def stream_text_delta(self, agent_name: str, text: str) -> None: ...
    async def stream_flush(self, agent_name: str, text: str) -> None: ...
    async def stream_start(self, agent_name: str) -> None: ...
    async def stream_end(self, agent_name: str) -> None: ...
    async def show_thinking(self, agent_name: str) -> None: ...
    async def hide_thinking(self, agent_name: str) -> None: ...
    async def show_typing(self, agent_name: str) -> None: ...
    async def stop_typing(self, agent_name: str) -> None: ...

    # --- Interactive gates ---
    async def request_plan_approval(self, agent_name: str, plan: str) -> PlanApprovalResult: ...
    async def ask_question(self, agent_name: str, question: dict) -> str: ...
    async def update_todo(self, agent_name: str, todos: list[dict]) -> None: ...

    # --- Channel/room management ---
    async def ensure_channel(self, agent_name: str, cwd: str | None) -> Any: ...
    async def move_to_killed(self, agent_name: str) -> None: ...

    # --- Lifecycle ---
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    # --- Session persistence ---
    async def save_session_metadata(self, session: AgentSession) -> None: ...
    async def reconstruct_sessions(self) -> list[dict]: ...
```

### Key design decisions

1. **Stream rendering is per-frontend, not in AgentHub.** Discord does live-editing of messages, batching text into 2000-char chunks, typing indicators, inline duration appended via message edit. A web UI would push raw SSE/WebSocket events. Slack would batch differently. The streaming engine in `agents.py` (lines 1455-2113) must be extracted into a `DiscordStreamRenderer` that the Discord frontend owns.

2. **Interactive gates (plan approval, questions) are per-frontend.** Discord uses reactions and message futures. A web UI would use a modal dialog. These are fundamentally different UX patterns that can't share implementation.

3. **Channel/room management is frontend-specific.** Discord has categories/channels/permissions/pinning/reordering. Web has virtual rooms. Slack has channels/threads. Each frontend implements its own version.

4. **Session persistence can use different backends.** Discord stores metadata in channel topics. Web frontend could use a database. AgentHub shouldn't care.

---

## 4. Shared Message Log (source of truth for parity)

For full parity between frontends (no missing messages), **Discord can't be the message history**. AgentHub needs a canonical per-agent event log that all frontends render from.

### Design

```python
@dataclass
class LogEvent:
    """A single event in an agent's message log."""
    ts: datetime
    kind: str          # "user", "assistant", "system", "tool_use", "thinking",
                       # "plan_request", "plan_result", "question", "answer",
                       # "todo_update", "stream_start", "stream_end", "error"
    agent: str         # agent name
    text: str = ""     # primary text content
    source: str = ""   # which frontend originated it ("discord", "web", "")
    data: dict = field(default_factory=dict)  # kind-specific structured data


class AgentLog:
    """Append-only event log for one agent. Source of truth for all frontends."""

    def __init__(self, agent_name: str, persist_path: str | None = None):
        self.agent_name = agent_name
        self.events: list[LogEvent] = []
        self.subscribers: list[Callable[[LogEvent], Awaitable[None]]] = []
        self._persist_path = persist_path  # JSONL file

    async def append(self, event: LogEvent) -> None:
        self.events.append(event)
        if self._persist_path:
            self._persist(event)
        for sub in self.subscribers:
            try:
                await sub(event)
            except Exception:
                log.warning("Subscriber error for %s", self.agent_name, exc_info=True)

    def subscribe(self, callback: Callable[[LogEvent], Awaitable[None]]) -> None:
        self.subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[LogEvent], Awaitable[None]]) -> None:
        self.subscribers.remove(callback)

    def replay(self, since: datetime | None = None) -> list[LogEvent]:
        """Return events for catch-up (e.g. web UI connecting to a running agent)."""
        if since is None:
            return list(self.events)
        return [e for e in self.events if e.ts >= since]
```

### Flow

```
User types in Discord
  -> DiscordFrontend receives it
  -> hub.receive_message("fix-auth", "fix the bug", source="discord")
  -> AgentLog.append(LogEvent(kind="user", text="fix the bug", source="discord"))
  -> All subscribers notified:
      -> DiscordFrontend: already displayed it (source == self), skip
      -> WebFrontend: pushes over WebSocket to browser
  -> Hub processes message, agent responds
  -> AgentLog.append(LogEvent(kind="assistant", text="I'll look into..."))
  -> Both frontends render it
```

### Key properties
- **No message bus needed** -- single process, just asyncio callbacks
- **Persistence** -- JSONL per agent, enables history reload on web connect
- **Catch-up** -- `replay(since=last_seen)` when a frontend connects late
- **Source tracking** -- each event records where it originated, so frontends can skip duplicates
- **Replaces Discord as history** -- agents currently reconstruct state from channel topics; the log replaces this

### Where it lives

`AgentLog` is owned by `AgentHub`, one instance per agent in `hub.sessions[name]` (or a parallel dict). It's frontend-agnostic -- just typed events and subscriber notifications.

---

## 5. What needs to change in AgentHub

AgentHub is *almost* ready for multi-frontend. These gaps need filling:

### 5a. Stream rendering must move out of agents.py

The biggest single change. Currently `stream_response_to_channel()` (agents.py:1984-2113) mixes:
- SDK message parsing (frontend-agnostic)
- Discord live-editing, typing indicators, message splitting, inline duration edit (Discord-specific)
- Activity tracking (frontend-agnostic)
- Error handling, rate limits (frontend-agnostic)

**Proposal**: Split into two layers:
1. **`agenthub/streaming.py`** -- iterates SDK messages, tracks activity, handles errors, yields normalized events
2. **Frontend's stream renderer** -- consumes normalized events and renders them (Discord edits messages, Web pushes SSE)

```python
# agenthub/streaming.py
async def stream_response(session: AgentSession) -> AsyncIterator[StreamOutput]:
    """Yield normalized stream outputs from a Claude session."""
    async for msg in receive_response_safe(session):
        if isinstance(msg, StreamEvent):
            event = msg.event
            update_activity(session.activity, event)
            if event_type == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    yield TextDelta(text=delta.get("text", ""))
            elif event_type == "message_delta":
                if event.get("delta", {}).get("stop_reason") == "end_turn":
                    yield EndTurn()
            # ... etc
        elif isinstance(msg, AssistantMessage):
            yield AssistantText(text=..., error=msg.error)
        elif isinstance(msg, ResultMessage):
            yield QueryResult(session_id=msg.session_id, cost=msg.total_cost_usd, ...)
        elif isinstance(msg, SystemMessage):
            yield SystemNotification(subtype=msg.subtype, data=msg.data)

# Normalized event types:
@dataclass
class TextDelta:
    text: str

@dataclass
class EndTurn:
    pass

@dataclass
class ThinkingStart:
    pass

@dataclass
class ThinkingEnd:
    pass

@dataclass
class ToolUse:
    tool_name: str
    preview: str | None

@dataclass
class QueryResult:
    session_id: str | None
    cost: float
    turns: int
    duration_ms: int

# etc.
```

### 5b. FrontendCallbacks needs stream rendering hooks

The existing `StreamHandlerFn` in `messaging.py` is already the right pattern:
```python
StreamHandlerFn = Callable[[AgentSession, Any], Awaitable[str | None]]
```

Each frontend provides its own stream handler. The Discord frontend provides one that does live-editing, the Web frontend provides one that pushes WebSocket events.

### 5c. Permission callbacks need frontend abstraction

`_handle_exit_plan_mode` and `_handle_ask_user_question` in agents.py currently:
1. Post to Discord
2. Wait for user reaction/text
3. Return PermissionResultAllow/Deny

These should delegate to the frontend:
```python
# In permission callback:
if tool_name == "ExitPlanMode":
    result = await frontend.request_plan_approval(agent_name, plan_content)
    return PermissionResultAllow() if result.approved else PermissionResultDeny(...)
```

### 5d. FrontendRouter for multiplexing

```python
class FrontendRouter:
    """Multiplexes hub callbacks to multiple frontends."""

    def __init__(self) -> None:
        self.frontends: dict[str, Frontend] = {}

    def add(self, frontend: Frontend) -> None:
        self.frontends[frontend.name] = frontend

    def remove(self, name: str) -> None:
        self.frontends.pop(name, None)

    def as_callbacks(self) -> FrontendCallbacks:
        """Build FrontendCallbacks that broadcast to all frontends."""
        async def post_message(agent_name: str, text: str) -> None:
            for fe in self.frontends.values():
                await fe.post_message(agent_name, text)
        # ... same pattern for all callbacks
        return FrontendCallbacks(
            post_message=post_message,
            post_system=post_system,
            # ...
        )
```

### 5e. Message ingestion: user -> agent routing

Currently `on_message` in main.py does:
1. Discord auth check
2. Channel -> agent lookup
3. Plan approval / question gate
4. Queue management
5. Wake + process

Most of this logic should move into AgentHub as a `receive_user_message(agent_name, content, metadata)` method. The frontend just:
1. Does its own auth
2. Maps its channel/room to an agent name
3. Calls `hub.receive_user_message(agent_name, content)`

AgentHub handles the rest (gates, queueing, wake, process).

---

## 6. Web UI Design

### Tech stack: two options

**Option A: FastAPI + React (in-process)**
- Backend: FastAPI (Python, async-native, WebSocket support)
- Frontend: React with TypeScript
- Runs in the same process as Discord bot -- direct access to AgentHub
- Simpler deployment, but a web crash could affect Discord

**Option B: Phoenix LiveView (separate process)**
- Server-rendered real-time UI -- streaming is a first-class primitive
- No React build toolchain, no client state management
- Elixir's concurrency model handles WebSocket connections trivially
- Requires AgentHub to expose an HTTP/WebSocket API (the LiveView app connects to it)
- Two runtimes to deploy, but stronger isolation
- The API becomes a general-purpose integration point for future consumers (Slack, CLI, scripts)

**Recommendation**: Decide based on whether we want the external API regardless. If yes, LiveView is compelling -- it forces us to build the API we'll want anyway, and the real-time UX is better with less code. If we want to move fast and stay single-language, FastAPI + React.

Either choice only affects Phase 3+. Phases 0-2 are identical.

### Connection model (Option A)

The web frontend runs **in the same process** as the Discord bot. No separate server.

```python
# In bot.py startup:
from axi.web_frontend import WebFrontend

web = WebFrontend(hub, port=8080)
router.add(web)
await web.start()  # starts FastAPI in background
```

### Connection model (Option B)

The LiveView app runs as a separate process, connects to AgentHub via HTTP/WebSocket API.

```
AgentHub (Python process)
  |-- DiscordFrontend (in-process)
  \-- HTTP/WS API on :8080
        \-- Phoenix LiveView connects here (separate Elixir process)
```

### UI layout

```
+-----------------------------------------------------+
|  Axi Web                                    [user]   |
+----------+------------------------------------------+
|          |                                           |
| Channels |  #axi-master                              |
|          |  ------------------------------------     |
| * master |  System: Bot started.                     |
| * agent1 |                                           |
| o agent2 |  [12:00] User: spawn an agent to fix      |
|          |  the auth bug                             |
| Killed   |                                           |
| o old-1  |  System: Spawning agent fix-auth...       |
|          |                                           |
|          |  [12:01] Axi: I'll create a worktree...   |
|          |                                           |
|          |  > Reading file... (agents.py)             |
|          |  > Editing file... (auth.py:45)            |
|          |                                           |
|          |  Done. The auth bug is fixed.              |
|          |  ------- 45.2s -------                     |
|          |                                           |
|          |  +----------------------------------+     |
|          |  | Type a message...            Send |     |
|          |  +----------------------------------+     |
+----------+------------------------------------------+
```

### Key features
- Channel list on left (mirrors Discord categories: Axi, Active, Killed)
- Chat area with real-time streaming (text appears token-by-token)
- Tool use shown inline (like Discord's debug mode)
- Plan approval: modal dialog with approve/reject/feedback
- AskUserQuestion: rendered as a form with radio buttons
- Todo list: rendered as a sidebar widget or inline
- File uploads via drag-and-drop
- Slash commands in the input box (same as Discord)

### WebSocket protocol

```typescript
// Client -> Server
{ type: "message", agent: "axi-master", content: "spawn an agent" }
{ type: "plan_approval", agent: "fix-auth", approved: true, feedback: "" }
{ type: "question_answer", agent: "fix-auth", answer: "Option 1" }
{ type: "command", command: "stop", agent: "fix-auth" }

// Server -> Client
{ type: "text_delta", agent: "axi-master", text: "I'll create..." }
{ type: "system", agent: "axi-master", text: "Spawning agent..." }
{ type: "tool_use", agent: "fix-auth", tool: "Edit", preview: "auth.py:45" }
{ type: "thinking_start", agent: "fix-auth" }
{ type: "thinking_end", agent: "fix-auth" }
{ type: "stream_end", agent: "fix-auth", elapsed: 45.2 }
{ type: "plan_request", agent: "fix-auth", plan: "..." }
{ type: "question", agent: "fix-auth", data: {...} }
{ type: "todo_update", agent: "fix-auth", todos: [...] }
{ type: "agent_list", agents: [...] }
```

---

## 7. Slack Considerations (future)

Slack frontend would implement the same `Frontend` interface:
- Channels map to Slack channels or threads
- Messages use Slack's Block Kit instead of Discord markdown
- Slash commands via Slack's slash command API
- Reactions for plan approval work similarly
- File uploads via Slack's file API
- Rate limits are different (1 msg/sec per channel vs Discord's 5/5s)
- Threading: could use Slack threads for agent responses (better than Discord's flat channels)

The adapter pattern makes this straightforward -- implement `SlackFrontend`, register it with the router.

---

## 8. Migration Path (incremental, never breaks Discord)

### Phase 0: Preparation (no behavior change)
1. **Extract `DiscordStreamRenderer`** from `agents.py` -- move the streaming engine (lines 1455-2113) into a new `axi/discord_stream.py`. Same code, just reorganized. All tests still pass.
2. **Extract `DiscordPermissionHandlers`** -- move `_handle_exit_plan_mode` (L590), `_handle_ask_user_question` (L776) into `axi/discord_ui.py`.
3. **Extract `DiscordMessageHandler`** -- move the `on_message` logic into a class that can be tested independently.

### Phase 1: AgentLog + abstract stream renderer
1. Implement `AgentLog` in `agenthub/agent_log.py` -- append-only per-agent event log with subscriber notifications and JSONL persistence
2. Define `LogEvent` types (user, assistant, system, tool_use, thinking, plan, question, todo, error)
3. Define `StreamOutput` types in `agenthub/stream_types.py`
4. Create `agenthub/streaming.py` with `stream_response()` that yields `StreamOutput` events and appends to `AgentLog`
5. Rewrite `DiscordStreamRenderer` to consume `StreamOutput` events instead of raw SDK messages
6. Verify Discord behavior is identical

### Phase 2: Build the Frontend protocol
1. Define `Frontend` base class/protocol
2. Wrap existing Discord code into `DiscordFrontend` class -- subscribes to `AgentLog` for notifications
3. Build `FrontendRouter`
4. Wire `hub_wiring.py` to use `FrontendRouter` with just Discord
5. Verify everything works the same

### Phase 3: Build Web Frontend (MVP)
1. Add web server (FastAPI or Phoenix LiveView -- see section 6) as `axi/web_frontend.py`
2. Implement `WebFrontend` -- subscribes to `AgentLog`, pushes events over WebSocket
3. On WebSocket connect: replay `AgentLog` for full history catch-up
4. Register alongside Discord via `FrontendRouter`
5. Both frontends receive messages simultaneously
6. No slash commands yet -- just chat

### Phase 4: Web Frontend features
1. Plan approval UI (modal or inline)
2. AskUserQuestion UI (form with radio buttons)
3. Todo list display (sidebar or inline widget)
4. File upload
5. Agent spawning / killing
6. Streaming text display (token-by-token rendering)

### Phase 5: Move message ingestion into AgentHub
1. Add `hub.receive_user_message()` that handles gates, queueing, wake
2. Both frontends call this instead of duplicating the logic
3. Remove duplicated logic from `on_message`

### Phase 6: Clean up
1. Move Discord-specific config (GUILD_ID, intents, categories) into `DiscordFrontend`
2. Make SOUL.md frontend-aware (parameterize "Discord" references)
3. Make system prompts frontend-aware

---

## 9. File-level change summary

| File | Change | Phase |
|------|--------|-------|
| `axi/agents.py` | Extract streaming (1455-2113) -> `discord_stream.py` | 0 |
| `axi/agents.py` | Extract UI handlers (590-930) -> `discord_ui.py` | 0 |
| `axi/discord_stream.py` | New: Discord stream renderer | 0 |
| `axi/discord_ui.py` | New: Discord interactive UI (plans, questions, todos) | 0 |
| `agenthub/agent_log.py` | New: Per-agent append-only event log with subscribers | 1 |
| `agenthub/stream_types.py` | New: Normalized stream output types | 1 |
| `agenthub/streaming.py` | New: Frontend-agnostic stream iterator, writes to AgentLog | 1 |
| `axi/discord_frontend.py` | New: `DiscordFrontend` class | 2 |
| `axi/frontend_router.py` | New: `FrontendRouter` multiplexer | 2 |
| `axi/hub_wiring.py` | Wire router instead of raw callbacks | 2 |
| `axi/web_frontend.py` | New: FastAPI + WebSocket frontend | 3 |
| `web/` | New: React web UI (or Phoenix LiveView app) | 3 |
| `axi/main.py` | Thin down to just Discord event handlers | 2-5 |
| `axi/channels.py` | Move into `DiscordFrontend` | 2 |
| `axi/config.py` | Separate Discord config from core config | 6 |
| `axi/prompts.py` | Parameterize frontend references | 6 |
| `SOUL.md` | Parameterize "Discord" references | 6 |

---

## 10. Open questions

1. **Should the web UI be a separate process?** Running in-process is simpler but means a web crash could take down Discord. Separate process means we need HTTP between hub and web frontend. Recommendation: start in-process, extract later if stability is a concern.

2. **How do plan approval / questions work across frontends?** If a plan is posted to both Discord and Web, which approval wins? Recommendation: first response wins (race), both UIs show the result.

3. **Should agents be frontend-scoped?** Can you spawn an agent from the web UI and see it in Discord? Recommendation: yes, all agents are visible everywhere. Frontends are views, not partitions.

4. **How does the `discordquery` package fit?** It provides `split_message` and `AsyncDiscordClient`. The client stays Discord-only. `split_message` could be generalized or each frontend does its own splitting.

5. **MCP tools**: `discord_send_file`, `discord_list_channels`, `discord_read_messages`, `discord_send_message` -- these are Discord-specific tools given to agents. Should agents get frontend-specific tools based on which frontend spawned them? Or generic "send_file", "list_channels" tools that delegate to the active frontend? Recommendation: keep Discord MCP tools as-is, add web-specific tools later. Agents spawned from either frontend still get the same tools (they talk to Discord for cross-channel messaging -- that's the external API, not the UI).

6. **Phoenix LiveView vs FastAPI+React?** LiveView gives better real-time UX with less code but adds a language boundary and forces an external API. FastAPI+React stays single-language but requires more client-side code for streaming. Decision depends on whether we want the external API regardless and Elixir comfort level.
