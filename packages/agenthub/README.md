# agenthub

Multi-agent session orchestration. Manages N concurrent LLM agent sessions: lifecycle, concurrency, message queuing, rate limits, hot restart, and graceful shutdown.

No UI dependency. The host app (Discord, CLI, web) plugs in via a narrow frontend protocol for notifications and human-interaction gates.

## Purpose

Extracted from the Axi bot's `agents.py` to make agent orchestration reusable across frontends. AgentHub is Claude-specific — it depends on Claude Wire (the stream-json protocol wrapper).

## Architecture

```
Host app (Discord bot, CLI, web)
      |
      | Frontend protocol + SDK factories
      v
   AgentHub (orchestration)
      |
      | Claude Wire stream protocol
      v
   Claude Agent SDK
```

The hub doesn't import SDK types directly. Instead, the frontend injects three factory functions at construction:
- `make_agent_options(session, resume_id) -> options` — builds SDK options
- `create_client(session, options) -> client` — creates SDK client
- `disconnect_client(client, name)` — tears down client

This keeps AgentHub decoupled from the exact SDK version and option schema.

## Usage

### Creating the hub

```python
from agenthub import AgentHub, FrontendRouter

router = FrontendRouter()
router.add(my_frontend)

hub = AgentHub(
    frontends=[router],
    make_agent_options=my_options_factory,
    create_client=my_client_factory,
    disconnect_client=my_disconnect_fn,
    query_timeout=300.0,
)
```

### Spawning and messaging

```python
from agenthub import spawn_agent, process_message, wake_agent

# Spawn a session (does not run the prompt)
session = await spawn_agent(hub, name="worker-1", cwd="/tmp/work")

# Wake and send a message
await wake_agent(hub, session)
await process_message(hub, session, "Build the feature", my_stream_handler)
```

### Stream handler pattern

The frontend provides a `StreamHandlerFn` — an async callback that consumes the SDK stream and renders to the user. The hub orchestrates query dispatch and retry; the frontend owns rendering.

```python
from agenthub import StreamHandlerFn

async def my_stream_handler(session) -> str | None:
    """Iterate SDK stream, render to Discord/CLI/web.
    Return None on success, or an error string for transient errors."""
    async for msg in session.client.receive_messages():
        render(msg)
    return None
```

### Permission composition

```python
from agenthub import build_permission_callback, compute_allowed_paths

paths = compute_allowed_paths(
    session.cwd,
    user_data_path="/home/user/data",
    bot_dir="/home/user/bot",
    worktrees_dir="/home/user/worktrees",
)

permission_cb = build_permission_callback(
    session,
    allowed_paths=paths,
    plan_approval_hook=my_plan_hook,    # optional
    question_hook=my_question_hook,      # optional
)
```

## Module Layout

| Module | Description |
|---|---|
| `hub.py` | `AgentHub` class — thin state holder, delegates to peer modules |
| `lifecycle.py` | `wake_agent`, `sleep_agent`, `wake_or_queue`, helpers (`is_awake`, `is_processing`, `count_awake`) |
| `registry.py` | `spawn_agent`, `end_session`, `rebuild_session`, `reset_session`, `reclaim_agent_name` |
| `messaging.py` | `process_message`, `run_initial_prompt`, `process_message_queue`, `deliver_inter_agent_message`, `interrupt_session` |
| `reconnect.py` | `connect_procmux`, `reconnect_single` — hot restart via procmux bridge |
| `scheduler.py` | `Scheduler` — slot management, priority-based eviction (background before interactive) |
| `shutdown.py` | `ShutdownCoordinator` — graceful shutdown with busy-agent wait, safety deadline |
| `rate_limits.py` | `RateLimitTracker` — rate limit state, usage recording, quota tracking |
| `permissions.py` | `build_permission_callback` — composes Claude Wire policies with session context + interactive hooks |
| `callbacks.py` | `FrontendCallbacks` — dataclass of async callables the frontend implements |
| `types.py` | `AgentSession`, `SessionUsage`, `RateLimitQuota`, `ConcurrencyLimitError` |
| `procmux_wire.py` | `ProcmuxProcessConnection` — adapter from procmux to claudewire's `ProcessConnection` |
| `tasks.py` | `BackgroundTaskSet` — GC-safe fire-and-forget async tasks |

## Design Principles

- **Functions over methods**: Lifecycle, registry, and messaging are module-level functions that take `hub` as first arg. AgentHub is a thin state holder that delegates — not a god object.
- **DI for side effects**: Shutdown, rate limits, and SDK interaction use injected callbacks. No implicit global state.
- **Explicit data flow**: Hub instance is passed explicitly. State is public (`hub.sessions`, `hub.scheduler`).
- **Frontend-agnostic**: All user-facing notifications go through `FrontendCallbacks`. The hub never touches Discord, terminal, or web APIs.
- **YAGNI**: `AgentSession.frontend_state: Any` lets frontends attach their own state without hub changes. No typed wrappers until needed.

## Dependencies

- `claudewire`
- `procmux`
- `opentelemetry-api`

Requires Python 3.12+.
