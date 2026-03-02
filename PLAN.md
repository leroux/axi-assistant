# AgentHub Library Design

## What Is AgentHub?

An agent session orchestration library. Manages N concurrent LLM agent sessions -- their lifecycle, concurrency, message queuing, rate limits, hot restart, and graceful shutdown. No UI dependency. The frontend (Discord, CLI, web) plugs in via a callback protocol.

AgentHub is **Claude-specific but protocol-agnostic**. It depends on Claude Wire (the stream-json protocol wrapper), not the Claude SDK directly.

```
Frontend (Discord/CLI/Web)     <- your app, implements FrontendCallbacks
        |
   AgentHub                    <- this library
   |-- Scheduler               (concurrency slots + eviction)
   |-- SessionRegistry         (agent dict, lookup, reconstruction)
   |-- MessageRouter           (queue, backpressure, inter-agent)
   |-- RateLimitTracker        (global rate limit state)
   |-- ShutdownCoordinator     (graceful + force shutdown)
   \-- ReconnectManager        (hot restart via procmux)
        |
   Claude Wire                 <- stream-json protocol
        |
   Procmux                     <- process multiplexer
```

## What Moves Into AgentHub

### From axi/scheduler.py -> agenthub/scheduler.py (whole file)

The centralized scheduler moves entirely. Already frontend-agnostic. Convert module-level state to a Scheduler class.

State: `_max_slots`, `_protected`, `_slots`, `_waiters`, `_yield_set`, `_interactive`
API: `request_slot`, `release_slot`, `should_yield`, `mark_interactive`, `mark_background`, `status`
Internal: `_evict_idle`, `_select_yield_target`

### From axi/agents.py -> agenthub/ (function-level)

**Lifecycle** -> `agenthub/lifecycle.py`:
- `wake_agent` (L1098-1188) -- slot request, SDK client creation, resume fallback
- `sleep_agent` (L1051-1075) -- disconnect, release slot
- `wake_or_queue` (L1190-1214) -- wake with concurrency fallback
- `_create_transport` (L992-1005) -- BridgeTransport creation
- `_create_sdk_client` (L1012-1038) -- ClaudeSDKClient creation
- `_make_agent_options` (L1078-1095) -- build ClaudeAgentOptions
- `is_awake`, `is_processing`, `count_awake_agents`

**Registry** -> `agenthub/registry.py`:
- `agents` dict (name -> session)
- `end_session`, `_rebuild_session`, `reset_session`
- `get_master_session`, `reclaim_agent_name`
- `spawn_agent` orchestration (session creation, registration, initial prompt)

**Messaging** -> `agenthub/messaging.py`:
- `process_message` (L2200-2238) -- query, timeout, retry
- `stream_with_retry` (L2101-2153) -- transient error retry
- `handle_query_timeout` (L2156-2192) -- interrupt, kill, resume
- `run_initial_prompt` (L2363-2430) -- initial prompt with wake + queue
- `process_message_queue` (L2433-2505) -- FIFO drain with yield
- `send_prompt_to_agent`, `drain_sdk_buffer`
- `deliver_inter_agent_message`, `_process_inter_agent_prompt`

**Hot restart** -> `agenthub/reconnect.py`:
- `connect_procmux` (L2640-2694) -- bridge connection + discovery
- `_reconnect_and_drain` (L2697-2791) -- per-agent reconnect + drain

**Response stream** -- SPLIT:
- Hub: `_receive_response_safe`, activity tracking, session_id capture, usage
- Frontend: all Discord rendering (stream handlers, live-edit, thinking, TodoWrite)

### From axi/rate_limits.py -> agenthub/rate_limits.py (most of file)

State + helpers + recording + DI-based handlers. Types: `SessionUsage`, `RateLimitQuota`.

### From axi/shutdown.py -> agenthub/shutdown.py (whole file)

Already cleanly isolated with DI. `ShutdownCoordinator` moves unchanged.

### From axi/axi_types.py -> agenthub/types.py

`AgentSession` split (see below).

### From axi/agents.py -> agenthub/permissions.py

Generic cwd-based permission policy from `make_cwd_permission_callback`. Frontend-specific hooks (plan approval, questions) injected via `PermissionHooks` protocol.

## What Stays in Axi Bot

| Module | Why |
|--------|-----|
| `axi/main.py` | Discord events, slash commands, scheduler loop |
| `axi/channels.py` | Discord category/channel management |
| `axi/tools.py` | MCP tool definitions |
| `axi/schedule_tools.py` | Schedule MCP server |
| `axi/prompts.py` | System prompt generation |

From `agents.py`, these Discord-specific functions stay:
- Reactions: `add_reaction`, `remove_reaction`
- Message handling: `extract_message_content`, `send_long`, `send_system`, `send_to_exceptions`
- Plan approval: `_handle_exit_plan_mode` (reaction-based)
- Questions: `_handle_ask_user_question` (reaction-based)
- Todo: `format_todo_list`, `_post_todo_list`, `load_todo_items`
- Streaming: `_StreamCtx`, `_LiveEditState`, all `_handle_*` stream functions, `stream_response_to_channel`
- Persistence: `reconstruct_agents_from_channels`, `_update_channel_topic`, `_save_master_session`
- Misc: `_post_model_warning`, thinking indicators, stderr display

## AgentSession Split

### Hub (`agenthub.AgentSession`)

```python
@dataclass
class AgentSession:
    name: str
    agent_type: str = "claude_code"
    client: Any = None                    # opaque SDK client
    cwd: str = ""
    query_lock: asyncio.Lock
    message_queue: deque[QueuedMessage]   # (content, opaque metadata)
    last_activity: datetime
    idle_reminder_count: int = 0
    session_id: str | None = None
    system_prompt: str | None = None
    mcp_servers: dict | None = None
    reconnecting: bool = False
    bridge_busy: bool = False
    activity: ActivityState
    plan_mode: bool = False
    agent_log: logging.Logger | None = None
    frontend_state: Any = None            # opaque, frontend casts
```

### Frontend (`axi.DiscordAgentState`)

```python
@dataclass
class DiscordAgentState:
    discord_channel_id: int | None = None
    system_prompt_hash: str | None = None
    system_prompt_posted: bool = False
    last_idle_notified: datetime | None = None
    question_future: asyncio.Future[str] | None = None
    question_data: dict | None = None
    question_message_id: int | None = None
    plan_approval_future: asyncio.Future[dict] | None = None
    plan_approval_message_id: int | None = None
    todo_message_id: int | None = None
    todo_items: list[dict] = field(default_factory=list)
    debug: bool = False
    stderr_buffer: list[str] = field(default_factory=list)
    stderr_lock: threading.Lock = field(default_factory=threading.Lock)
```

Hub never touches `frontend_state`. Frontend casts it to `DiscordAgentState`.

## Public API

### AgentHub

```python
class AgentHub:
    def __init__(self, *, max_awake, protected, idle_thresholds,
                 query_timeout, retry_max, retry_base_delay,
                 callbacks: FrontendCallbacks,
                 create_options: CreateOptionsFn,
                 process_conn: ProcessConnection | None): ...

    # Registry
    @property
    def agents(self) -> dict[str, AgentSession]: ...
    def get(self, name: str) -> AgentSession | None: ...

    # Lifecycle
    async def spawn(self, name, *, cwd, prompt="", resume=None,
                    agent_type="claude_code", system_prompt=None,
                    mcp_servers=None) -> AgentSession: ...
    async def kill(self, name) -> str | None: ...   # returns session_id
    async def wake(self, name) -> None: ...
    async def sleep(self, name, *, force=False) -> None: ...

    # Messaging
    async def send(self, name, content, metadata=None) -> None: ...
    async def send_inter_agent(self, sender, target, content) -> str: ...

    # Query (yields raw SDK messages for frontend to render)
    async def process_message(self, session, content) -> AsyncIterator: ...

    # Subsystems
    @property
    def scheduler(self) -> Scheduler: ...
    @property
    def rate_limits(self) -> RateLimitTracker: ...

    # Hot restart
    async def reconnect(self, known: list[ReconstructInfo]) -> ReconnectResult: ...

    # Shutdown
    async def graceful_shutdown(self, source, skip=None) -> None: ...
    async def force_shutdown(self, source) -> None: ...

    # Background tasks
    def fire_and_forget(self, coro) -> asyncio.Task: ...
```

### FrontendCallbacks (expanded)

```python
@dataclass
class FrontendCallbacks:
    post_message: PostMessageFn               # (agent_name, text)
    post_system: PostSystemFn                 # (agent_name, text)
    on_wake: OnLifecycleFn                    # (agent_name)
    on_sleep: OnLifecycleFn                   # (agent_name)
    on_spawn: OnSpawnFn                       # (agent_name, session)
    on_kill: OnKillFn                         # (agent_name, session_id)
    on_session_id: OnSessionIdFn              # (agent_name, session_id)
    broadcast: BroadcastFn                    # (message) -> all channels
    schedule_rate_limit_expiry: ScheduleExpiryFn  # (delay_secs)
    on_idle_reminder: OnIdleFn                # (agent_name, idle_minutes)
    on_reconnect: OnReconnectFn               # (agent_name, was_mid_task)
    permission_hooks: PermissionHooks | None = None
    close_app: CloseAppFn                     # () -> None
    kill_process: KillProcessFn               # () -> NoReturn
    send_goodbye: GoodbyeFn | None = None
```

## Hot Restart Design

First-class AgentHub feature.

### Pre-restart

```python
frontend.save_all_sessions()   # persist to Discord topics / file / DB
await hub.graceful_shutdown("restart", skip="axi-master")
# Bridge mode: bot.close() -> os._exit(42) -> supervisor restarts
# Procmux and all CLI subprocesses keep running
```

### Post-restart

```python
known = frontend.load_saved_sessions()
# -> [ReconstructInfo(name, cwd, session_id, agent_type, ...)]

result = await hub.reconnect(known)
```

`reconnect()` does:
1. Connect to procmux socket
2. List running processes
3. For each known session with matching procmux process:
   - Create AgentSession, subscribe (triggers buffer replay)
   - Create SDK client (reconnecting mode -- fakes initialize)
   - If mid-task (`idle=False`): set `bridge_busy=True`, frontend drains events
4. Orphan processes (no matching session): killed
5. Known sessions with no running process: created as sleeping

### What enables it

- **Procmux stays alive** -- separate process, unaffected by bot restart
- **Output buffered** -- procmux buffers CLI stdout/stderr while bot is down
- **Subscribe + replay** -- reconnecting gets all buffered output
- **Initialize interception** -- BridgeTransport fakes the init handshake
- **Session resume** -- Claude CLI `--resume` with session IDs

### Persistent state (frontend's responsibility)

Per-agent: `name`, `cwd`, `session_id`, `agent_type`, `system_prompt`, `mcp_servers`.
Hub provides `hub.snapshot(name) -> SessionSnapshot` for serialization.

## Scheduler Integration

Instance owned by AgentHub (not module-level state):

```python
self._scheduler = Scheduler(
    max_slots=max_awake, protected=protected,
    get_sessions=lambda: self._sessions,
    sleep_fn=self._do_sleep,
)
```

Callback pattern for eviction avoids circular deps.

## Claude-Specificity Decision

**Claude Wire-specific, not generic.** Wake/sleep/resume, permissions, stream protocol, procmux -- all depend on Claude Code's model. Generic abstraction premature with zero non-Claude consumers. The `create_options` callback lets frontends configure model/thinking/permissions.

## Dependency Direction

```
axi-assistant -> agenthub, claudewire, procmux, claude_agent_sdk
agenthub      -> claudewire, procmux (optional), claude_agent_sdk
claudewire    -> claude_agent_sdk
procmux       -> (standalone)
```

AgentHub does NOT import from `axi/`. One-way: `axi/` -> `agenthub`.

## File Layout

```
packages/agenthub/agenthub/
    __init__.py
    hub.py            # AgentHub class
    scheduler.py      # Scheduler class
    lifecycle.py      # wake, sleep, transport
    registry.py       # session dict, spawn, kill
    messaging.py      # process_message, queue, inter-agent
    reconnect.py      # hot restart
    rate_limits.py    # rate limit tracking
    shutdown.py       # ShutdownCoordinator
    permissions.py    # generic cwd policy
    types.py          # AgentSession, errors, configs
    callbacks.py      # FrontendCallbacks
    tasks.py          # BackgroundTaskSet (exists)
    procmux_wire.py   # ProcmuxProcessConnection (exists)
```

## Migration Path

1. Create `AgentHub` + `Scheduler` classes (encapsulate module state)
2. Move `shutdown.py` (already isolated)
3. Move `rate_limits.py` (already DI)
4. Move scheduler (convert to class)
5. Split `AgentSession`
6. Extract lifecycle (wake/sleep)
7. Extract messaging
8. Extract reconnect
9. Extract permissions
10. Wire Axi bot to `AgentHub` instance
11. Verify hot restart e2e

## Open Questions

1. **Response streaming**: raw SDK passthrough vs typed `ResponseEvent`? Start raw, YAGNI.
2. **MCP servers**: frontend passes pre-built. App-specific.
3. **Flowcoder**: uniform via `agent_type` + `create_options`. CLI args stay in frontend.
4. **`frontend_state`**: `Any` now, generic `AgentSession[F]` later if needed.
5. **`message_queue`**: becomes `(content, metadata: Any)` where metadata is opaque to hub.
