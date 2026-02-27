# Axi Assistant - Deep Architecture Analysis (Opus Level)

**Date:** 2026-02-27
**Scope:** Complete system redesign analysis
**Current State:** 4,529 lines in bot.py, 8 agent_type checks, complex lifecycle management

---

## PART 1: CURRENT ARCHITECTURE DEEP DIVE

### The Fundamental Problem

The system has **two incompatible agent models** forced into a single codebase:

**Model A: Claude Code (Interactive)**
```
spawn_agent()
  → create sleeping AgentSession (client=None)
  → on_message() wakes agent
  → wake_agent() creates ClaudeSDKClient
  → agent processes messages until sleep_agent() called
  → sleep_agent() disconnects client
  → agent can be resumed later
```

**Model B: Flowcoder (Fire-and-Forget)**
```
spawn_agent()
  → create FlowcoderSession with command/args
  → _run_flowcoder() runs it immediately to completion
  → agent finishes and is done
  → no wake/sleep lifecycle
```

These are **fundamentally different patterns**, yet the code tries to unify them.

---

### Critical Execution Paths

#### Path 1: User sends message to Claude Code agent

```
on_message(message)  [line 2990]
  ├─ Authorization checks (lines 2990-3000)
  ├─ Find agent from channel (line 3010)
  │
  ├─ IF reconnecting: queue message (line 3028-3037)
  │
  ├─ IF flowcoder running: send to stdin (line 3042-3046)
  │
  ├─ IF flowcoder finished: reject (line 3049-3051)
  │
  ├─ IF rate limited: queue (line 3054-3065)
  │
  ├─ IF query_lock held: queue (line 3067-3077)
  │
  └─ NORMAL PATH: async with query_lock (line 3079)
      ├─ Wake if sleeping (line 3081-3104)
      │   ├─ if client is None (line 3081)
      │   ├─ try wake_agent() (line 3084)
      │   ├─ catch ConcurrencyLimitError → queue (line 3085-3096)
      │   └─ catch Exception → error (line 3097-3104)
      │
      ├─ Update state (line 3106-3115)
      │
      ├─ Query + stream (line 3117-3119)
      │   ├─ await session.client.query(...)
      │   └─ await _stream_with_retry(...)
      │
      └─ On timeout/error: handle (line 3120-3130)

  └─ Process queued messages (line 3137)
```

**Nesting depth:** 9 levels in worst case
**Branch count:** 7 independent early-return checks + 3 queue conditions
**Implicit state assumptions:**
- `session.client` is the only indicator of "awake" state
- `session._reconnecting` flag only set by bridge reconnect
- `session.query_lock` serializes all queries

#### Path 2: Bridge reconnection after bot restart

```
on_ready()  [line 4248]
  └─ _connect_bridge()  [line 2689]
      └─ bridge_conn.send_command("list")
          └─ For each running agent:
              └─ _reconnect_and_drain()  [line 2786]
                  ├─ Acquire query_lock (line 2784)
                  │
                  ├─ Create BridgeTransport (line 2791-2796)
                  │   ├─ BridgeTransport(name, bridge_conn, reconnecting=True)
                  │   └─ await transport.connect()
                  │
                  ├─ Subscribe (line 2799)
                  │   └─ sub_result = await transport.subscribe()
                  │       ├─ replayed = buffered messages
                  │       ├─ cli_status = "running" | "exited"
                  │       └─ cli_idle = True | False
                  │
                  ├─ Create SDK client (line 2819-2821)
                  │   └─ ClaudeSDKClient(options, transport=transport)
                  │
                  ├─ Set _bridge_busy flag (line 2842)
                  │   IF cli_status=="running" AND cli_idle==False:
                  │     ├─ Drain buffered output (line 2844-2856)
                  │     └─ set _bridge_busy = True
                  │
                  └─ Process message queue (line 2876)
```

**Critical insight:** This only works if bridge is available. Direct subprocess agents have **no reconnection path**.

#### Path 3: Agent wake (two separate implementations!)

```
wake_agent()  [line 1277]
  ├─ IF flowcoder: return no-op (line 1288-1289)
  │
  ├─ IF already awake: return (line 1292-1293)
  │
  ├─ Acquire _wake_lock (line 1296)  ← TOCTOU protection
  │   ├─ Re-check awake (line 1297-1298)
  │
  │   ├─ IF bridge available (line 1316):
  │   │   └─ _wake_agent_via_bridge()  [line 1231]
  │   │       ├─ transport = BridgeTransport(...)
  │   │       ├─ await transport.connect()
  │   │       ├─ cli_args = build_cli_spawn_args(options)
  │   │       ├─ await transport.spawn(cli_args, env, cwd)
  │   │       ├─ await transport.subscribe()
  │   │       └─ client = ClaudeSDKClient(transport=transport)
  │   │
  │   │   ├─ If spawn/resume fails:
  │   │   │   └─ Retry fresh (no resume) (line 1326-1341)
  │   │
  │   └─ ELSE direct mode (line 1343):
  │       ├─ client = ClaudeSDKClient(options=options)
  │       ├─ await client.__aenter__()
  │       │
  │       └─ If resume fails:
  │           └─ Retry fresh (line 1353-1361)
  │
  └─ Post system prompt (line 1364-1376)
```

**Problem:** Two completely separate code paths for bridge vs direct!

---

### State Machine Issues

#### Claude Code Agent States

```
SPAWNED (client=None, sleeping)
  │
  ├─ on_message() → wake attempt
  │   ├─ ConcurrencyLimitError → queued
  │   └─ Success → AWAKE
  │
AWAKE (client!=None)
  │
  ├─ on_message() → process
  │   ├─ query_lock held → queued
  │   └─ Success → PROCESSING
  │
PROCESSING (query_lock.locked())
  │   (messages queued while locked)
  │
  └─ sleep_agent() → SPAWNED
      (drop all in-flight state)
```

**Issue:** There's no explicit state enum. State is inferred from:
- `client is None` → sleeping
- `client is not None` → awake
- `query_lock.locked()` → processing
- `_reconnecting` → reconnecting

This is implicit and fragile.

#### Flowcoder Agent States

```
SPAWNED (flowcoder_process=None)
  │
  ├─ spawn_agent(agent_type="flowcoder")
  │   └─ _run_flowcoder()
  │       ├─ await proc.start()
  │       └─ await _stream_flowcoder_to_channel()
  │
RUNNING (flowcoder_process.is_running)
  │
  └─ _stream_flowcoder_to_channel() completes
      └─ await proc.stop()
          └─ DONE (no more messages accepted)
```

**Issue:** Flowcoder agents don't have wake/sleep. They run once and die. But the code tries to treat them the same way.

---

### Bridge Asymmetry

**Bridge is NOT optional:**

| Scenario | Bridge Mode | Direct Mode |
|----------|-------------|-------------|
| Initial spawn + query | ✅ Works | ✅ Works |
| Bot restart while agent running | ✅ Reconnects | ❌ PID lost, agent orphaned |
| Session resume | ✅ Bridge buffers | ❌ No mechanism |
| Flowcoder persistence | ✅ Bridge keeps running | ❌ Process dies |

**Current code:** Lines pretending to support direct mode:

```python
# Line 2701:
"Failed to connect to bridge — agents will use direct subprocess mode"

# But reconnect only works with bridge:
# Lines 2805-2876: _reconnect_and_drain() has NO fallback
```

**Truth:**
- Bridge was designed as optimization (persistence across restarts)
- Became required for correctness (reconnection, session resume)
- Code still pretends it's optional, adding unnecessary complexity

---

## PART 2: DETAILED PROBLEM ANALYSIS

### Problem 1: Implicit State Machine

**Current:**
```python
if session.client is None:
    await wake_agent(session)
```

**Issues:**
- State is implicit (inferred from field values)
- Three overlapping concepts:
  - Spawned (registered in agents dict)
  - Awake (client created)
  - Processing (query_lock held)
- Flowcoder uses different state model entirely
- Reconnecting adds another transient state

**How this breaks:**
```python
# What if client is created but __aenter__ hasn't finished?
# Is it "awake" or not?
session.client = ClaudeSDKClient(...)
await session.client.__aenter__()  # ← window for bugs here

# What if _reconnecting is True but query_lock is being released?
# Which takes precedence?
if session._reconnecting:
    await session.message_queue.put(...)  # queue it
elif session.query_lock.locked():
    await session.message_queue.put(...)  # queue it
# Both conditions can be true simultaneously!
```

### Problem 2: 8 Agent Type Checks

```python
# Line 1194: sleep_agent()
if session.agent_type == "flowcoder":
    return

# Line 2245: spawn_agent() docstring mentions agent_type
# Line 2268: if agent_type == "flowcoder":
# Line 2291: if agent_type == "flowcoder":
# Line 3049: if session.agent_type == "flowcoder":
# Line 3066: if session.agent_type == "flowcoder":

# Plus indirect via agent-specific fields:
# - session.client (Claude Code only)
# - session.flowcoder_process (flowcoder only)
```

**Consequence:** Adding a 3rd agent type (e.g., Langchain) requires changes in 8+ places.

### Problem 3: Message Routing Complexity

```python
# on_message() has 7 different "reject" paths before main logic:
1. if message.author.id == bot.user.id: return
2. if message.type not in (...): return
3. if message.author.bot and ...: return
4. if message.channel.type == ChannelType.private: return
5. if message.guild is None or ...: return
6. if message.author.id not in ALLOWED_USER_IDS: return
7. if agent_name is None: return

# Then 6 different "queue" conditions:
1. if session._reconnecting: queue
2. if session.flowcoder_process and session.flowcoder_process.is_running: send_to_stdin
3. if session.agent_type == "flowcoder": reject
4. if _is_rate_limited() and not session.query_lock.locked(): queue
5. if session.query_lock.locked(): queue
6. (default) process

# The problem: these conditions overlap and interact in complex ways
```

**Example of fragility:**
```python
# These two conditions can both fire:
if _is_rate_limited() and not session.query_lock.locked():
    await session.message_queue.put(...)

if session.query_lock.locked():
    await session.message_queue.put(...)

# What if agent becomes busy between the two checks?
# What if rate limit expires while we're queuing?
# Race conditions are possible.
```

### Problem 4: Wake/Reconnect Code Duplication

Three places attempt to "wake" with slightly different error handling:

**Location 1: on_message() (lines 3081-3104)**
```python
try:
    await wake_agent(session)
except ConcurrencyLimitError:
    await session.message_queue.put((content, message.channel, message))
    # notify user about concurrency limit
except Exception:
    log.exception("Failed to wake agent '%s'", agent_name)
    await _add_reaction(message, "❌")
    await send_system(channel, "Failed to wake agent...")
    return
```

**Location 2: _process_message_queue() (lines 2659-2673)**
```python
try:
    await wake_agent(session)
except Exception:  # ← different! doesn't catch ConcurrencyLimitError
    log.exception("Failed to wake agent '%s' for queued message", session.name)
    await _add_reaction(orig_message, "❌")
    await send_system(channel, f"Failed to wake agent...")
    # Clear remaining queue
    while not session.message_queue.empty():
        ...
    return
```

**Location 3: _run_initial_prompt() (lines 2573-2590)**
```python
try:
    await wake_agent(session)
except ConcurrencyLimitError:
    log.info("Concurrency limit hit for '%s' initial prompt — queuing", session.name)
    await session.message_queue.put((prompt, channel, None))
    awake = _count_awake_agents()
    await send_system(channel, f"⏳ All {awake} agent slots are busy...")
    return
except Exception:
    log.exception("Failed to wake agent '%s' for initial prompt", session.name)
    await send_system(channel, f"Failed to wake agent **{session.name}**.")
    return
```

**Result:** 3 inconsistent implementations of wake-or-queue logic.

### Problem 5: Bridge Transport Creation Boilerplate

Four separate locations create bridge transports:

```python
# Location 1: _wake_agent_via_bridge() (lines 1237-1241)
transport = BridgeTransport(
    session.name, bridge_conn,
    stderr_callback=make_stderr_callback(session),
)
await transport.connect()

# Location 2: _reconnect_and_drain() (lines 2808-2814)
transport = BridgeTransport(
    session.name, bridge_conn,
    reconnecting=True,
    stderr_callback=make_stderr_callback(session),
)
await transport.connect()

# Location 3: _run_flowcoder() (lines 2356-2365)
proc = BridgeFlowcoderProcess(
    bridge_name=f"{session.name}:flowcoder",
    conn=bridge_conn,
    command=session.flowcoder_command,
    args=session.flowcoder_args,
    cwd=session.cwd,
)

# Location 4: _run_inline_flowchart() (lines 2411-2417)
proc = BridgeFlowcoderProcess(
    bridge_name=f"{session.name}:flowcoder",
    conn=bridge_conn,
    command=command, args=args, cwd=session.cwd,
)
```

Result: Similar code repeated with subtle variations (reconnecting flag, stderr_callback placement).

---

## PART 3: THE SOLUTION - STATELESS HANDLER PATTERN

### Design Principles

1. **State lives in AgentSession** - All agent state is visible in one place
2. **Handlers are orchestrators** - They coordinate operations, don't own state
3. **Handlers are stateless** - Same instance can handle multiple sessions
4. **Bridge is decoupled** - Bridge logic stays in bot.py, not in handlers
5. **Type-safe agent type** - Use Literal, not string dispatch

### Handler Architecture

```python
from typing import Literal, Protocol
from abc import ABC, abstractmethod

# Type-safe agent type
AgentType = Literal["claude_code", "flowcoder"]


class AgentHandler(ABC):
    """Orchestrates agent operations. State lives in AgentSession."""

    @abstractmethod
    async def wake(self, session: "AgentSession") -> None:
        """Activate/initialize the agent."""
        pass

    @abstractmethod
    async def sleep(self, session: "AgentSession") -> None:
        """Deactivate/cleanup the agent."""
        pass

    @abstractmethod
    async def process_message(
        self,
        session: "AgentSession",
        content: str | list,
        channel: discord.TextChannel,
    ) -> None:
        """Process a user message. Raise RuntimeError if unable."""
        pass

    def is_awake(self, session: "AgentSession") -> bool:
        """Check if agent is ready to process messages."""
        raise NotImplementedError

    def is_processing(self, session: "AgentSession") -> bool:
        """Check if agent has active work."""
        raise NotImplementedError


class ClaudeCodeHandler(AgentHandler):
    """Handles Claude Code agents (interactive, wake/sleep lifecycle)."""

    def is_awake(self, session: "AgentSession") -> bool:
        return session.client is not None

    def is_processing(self, session: "AgentSession") -> bool:
        return session.query_lock.locked()

    async def wake(self, session: "AgentSession") -> None:
        """Create ClaudeSDKClient and connect to it."""
        if self.is_awake(session):
            return

        log.debug("Waking Claude Code agent '%s'", session.name)
        options = _make_agent_options(session, session.session_id)

        # Bridge or direct - same interface
        transport = await _create_transport(session)

        session.client = ClaudeSDKClient(options=options, transport=transport)
        await session.client.__aenter__()

        log.info("Claude Code agent '%s' awake", session.name)
        if session._log:
            session._log.info("SESSION_WAKE")

    async def sleep(self, session: "AgentSession") -> None:
        """Disconnect ClaudeSDKClient."""
        if not self.is_awake(session):
            return

        log.debug("Sleeping Claude Code agent '%s'", session.name)

        pid = _get_subprocess_pid(session.client)
        try:
            await asyncio.wait_for(
                session.client.__aexit__(None, None, None),
                timeout=5.0,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            log.warning("'%s' shutdown timed out or cancelled", session.name)
        except Exception:
            log.exception("Error disconnecting agent '%s'", session.name)
        finally:
            _ensure_process_dead(pid, session.name)
            session.client = None

        session._bridge_busy = False
        log.info("Claude Code agent '%s' sleeping", session.name)
        if session._log:
            session._log.info("SESSION_SLEEP")

    async def process_message(
        self,
        session: "AgentSession",
        content: str | list,
        channel: discord.TextChannel,
    ) -> None:
        """Run a query through Claude Code."""
        if not self.is_awake(session):
            raise RuntimeError(f"Claude Code agent '{session.name}' not awake")

        session.last_activity = datetime.now(timezone.utc)
        session.last_idle_notified = None
        session.idle_reminder_count = 0
        session._bridge_busy = False

        drain_stderr(session)
        drain_sdk_buffer(session)

        if session._log:
            session._log.info("USER: %s", _content_summary(content))

        try:
            async with asyncio.timeout(QUERY_TIMEOUT):
                await session.client.query(_as_stream(content))
                await _stream_with_retry(session, channel)
        except TimeoutError:
            await _handle_query_timeout(session, channel)
        except Exception:
            log.exception("Error querying Claude Code agent '%s'", session.name)
            raise RuntimeError(f"Query failed for agent '{session.name}'") from None


class FlowcoderHandler(AgentHandler):
    """Handles flowcoder agents (fire-and-forget execution)."""

    def is_awake(self, session: "AgentSession") -> bool:
        return session.flowcoder_process is not None

    def is_processing(self, session: "AgentSession") -> bool:
        return (
            session.flowcoder_process is not None
            and session.flowcoder_process.is_running
        )

    async def wake(self, session: "AgentSession") -> None:
        """Spawn and run flowcoder process."""
        if self.is_awake(session):
            return

        log.debug("Waking flowcoder agent '%s'", session.name)

        if bridge_conn and bridge_conn.is_alive:
            proc = BridgeFlowcoderProcess(
                bridge_name=f"{session.name}:flowcoder",
                conn=bridge_conn,
                command=session.flowcoder_command,
                args=session.flowcoder_args,
                cwd=session.cwd,
            )
        else:
            proc = FlowcoderProcess(
                command=session.flowcoder_command,
                args=session.flowcoder_args,
                cwd=session.cwd,
            )

        await proc.start()
        session.flowcoder_process = proc

        log.info("Flowcoder agent '%s' started (command=%s)", session.name, session.flowcoder_command)

    async def sleep(self, session: "AgentSession") -> None:
        """Stop flowcoder process."""
        if not self.is_awake(session):
            return

        proc = session.flowcoder_process

        if getattr(proc, 'is_bridge_backed', False) and session.query_lock.locked():
            # Mid-execution: detach so bridge can buffer
            await proc.detach()
            log.info("Flowcoder '%s' detached (bridge buffering)", session.name)
        else:
            await proc.stop()
            log.info("Flowcoder '%s' stopped", session.name)

        session.flowcoder_process = None

    async def process_message(
        self,
        session: "AgentSession",
        content: str | list,
        channel: discord.TextChannel,
    ) -> None:
        """Send message to flowcoder stdin."""
        if not self.is_processing(session):
            raise RuntimeError(f"Flowcoder '{session.name}' not running")

        user_msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": content if isinstance(content, str) else str(content),
            },
        }
        await session.flowcoder_process.send(user_msg)

        log.debug("Sent message to flowcoder '%s'", session.name)


# Singleton instances
_claude_code_handler = ClaudeCodeHandler()
_flowcoder_handler = FlowcoderHandler()


def get_handler(agent_type: AgentType) -> AgentHandler:
    """Get the handler for an agent type."""
    if agent_type == "claude_code":
        return _claude_code_handler
    elif agent_type == "flowcoder":
        return _flowcoder_handler
    else:
        raise ValueError(f"Unknown agent type: {agent_type}")
```

### Updated AgentSession

```python
@dataclass
class AgentSession:
    """Agent session with polymorphic handler."""

    name: str
    agent_type: AgentType  # ← Type-safe!
    cwd: str = ""

    # Shared state
    discord_channel_id: int | None = None
    query_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    message_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    system_prompt: dict | str | None = None
    _system_prompt_posted: bool = False
    last_idle_notified: datetime | None = None
    idle_reminder_count: int = 0
    session_id: str | None = None
    mcp_servers: dict | None = None
    _reconnecting: bool = False
    _bridge_busy: bool = False
    activity: ActivityState = field(default_factory=ActivityState)
    _log: logging.Logger | None = None

    # Claude Code specific (only used if agent_type == "claude_code")
    client: ClaudeSDKClient | None = None

    # Flowcoder specific (only used if agent_type == "flowcoder")
    flowcoder_process: FlowcoderProcess | None = None
    flowcoder_command: str = ""
    flowcoder_args: str = ""

    # Delegate to handler
    @property
    def handler(self) -> AgentHandler:
        return get_handler(self.agent_type)

    def __post_init__(self):
        """Set up per-agent logger."""
        os.makedirs(_log_dir, exist_ok=True)
        logger = logging.getLogger(f"agent.{self.name}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        if not logger.handlers:
            fh = RotatingFileHandler(
                os.path.join(_log_dir, f"{self.name}.log"),
                maxBytes=5 * 1024 * 1024,
                backupCount=2,
            )
            fh.setLevel(logging.DEBUG)
            _agent_fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
            _agent_fmt.converter = time.gmtime
            fh.setFormatter(_agent_fmt)
            logger.addHandler(fh)
        self._log = logger

    def close_log(self):
        """Remove all handlers from the per-agent logger."""
        if self._log:
            for handler in self._log.handlers[:]:
                handler.close()
                self._log.removeHandler(handler)

    async def wake(self) -> None:
        """Delegate to handler."""
        await self.handler.wake(self)

    async def sleep(self) -> None:
        """Delegate to handler."""
        await self.handler.sleep(self)

    async def process_message(
        self,
        content: str | list,
        channel: discord.TextChannel,
    ) -> None:
        """Delegate to handler."""
        await self.handler.process_message(self, content, channel)

    def is_awake(self) -> bool:
        """Check if agent is ready."""
        return self.handler.is_awake(self)

    def is_processing(self) -> bool:
        """Check if agent has active work."""
        return self.handler.is_processing(self)
```

### Simplified Message Routing

```python
@bot.event
async def on_message(message):
    """Simplified message routing using handlers."""

    # --- Authorization and channel checks ---
    if message.author.id == bot.user.id:
        return
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return
    if message.author.bot and message.author.id not in ALLOWED_USER_IDS:
        return
    if message.channel.type == ChannelType.private:
        # ... handle DMs ...
        return
    if message.guild is None or message.guild.id != DISCORD_GUILD_ID:
        return
    if message.author.id not in ALLOWED_USER_IDS:
        return

    # --- Get agent ---
    agent_name = channel_to_agent.get(message.channel.id)
    if agent_name is None:
        return

    session = agents.get(agent_name)
    if session is None:
        return

    # Block killed agents
    if killed_category and hasattr(message.channel, "category_id"):
        if message.channel.category_id == killed_category.id:
            await send_system(message.channel, "This agent has been killed.")
            return

    # --- Extract content ---
    content = await _extract_message_content(message)
    log.info("Message from %s in #%s: %s", message.author, message.channel.name, _content_summary(content))

    # --- Check shutdown ---
    if shutdown_coordinator and shutdown_coordinator.requested:
        await send_system(message.channel, "Bot is restarting — not accepting new messages.")
        return

    # --- Backpressure conditions ---

    # 1. Reconnecting: queue messages
    if session._reconnecting:
        await session.message_queue.put((content, message.channel, message))
        position = session.message_queue.qsize()
        log.debug("Agent '%s' reconnecting, queuing message (position %d)", agent_name, position)
        await _add_reaction(message, "📨")
        await send_system(message.channel, f"Agent reconnecting — message queued (position {position}).")
        return

    # 2. Rate limited: queue messages
    if _is_rate_limited() and not session.is_processing():
        await session.message_queue.put((content, message.channel, message))
        position = session.message_queue.qsize()
        remaining = _format_time_remaining(_rate_limit_remaining_seconds())
        await _add_reaction(message, "📨")
        await send_system(
            message.channel,
            f"⏳ Rate limited (~**{remaining}** remaining). Message queued (position {position})."
        )
        return

    # 3. Agent processing: queue messages
    if session.is_processing():
        await session.message_queue.put((content, message.channel, message))
        position = session.message_queue.qsize()
        log.debug("Agent '%s' busy, queuing message (position %d)", agent_name, position)
        await _add_reaction(message, "📨")
        await send_system(
            message.channel,
            f"Agent busy — message queued (position {position}). Will process after current turn."
        )
        return

    # --- Normal processing path ---
    async with session.query_lock:
        # Wake if needed
        if not session.is_awake():
            log.debug("Waking agent '%s' for user message", agent_name)
            try:
                await session.wake()
            except ConcurrencyLimitError:
                await session.message_queue.put((content, message.channel, message))
                position = session.message_queue.qsize()
                awake = _count_awake_agents()
                await _add_reaction(message, "📨")
                await send_system(
                    message.channel,
                    f"⏳ All {awake} agent slots busy. Message queued (position {position})."
                )
                return
            except Exception:
                log.exception("Failed to wake agent '%s'", agent_name)
                await _add_reaction(message, "❌")
                await send_system(
                    message.channel,
                    f"Failed to wake agent **{agent_name}**. Try `/kill-agent {agent_name}` and respawn."
                )
                return

        # Process the message
        log.debug("Processing message for agent '%s'", agent_name)
        session.last_activity = datetime.now(timezone.utc)
        session.last_idle_notified = None
        session.idle_reminder_count = 0
        session.activity = ActivityState(phase="starting", query_started=datetime.now(timezone.utc))

        try:
            await session.process_message(content, message.channel)
            await _add_reaction(message, "✅")
        except TimeoutError:
            log.warning("Query timeout for agent '%s'", agent_name)
            await _add_reaction(message, "⏳")
            await _handle_query_timeout(session, message.channel)
        except RuntimeError as e:
            # Agent-specific runtime error (not awake, etc.)
            log.warning("Runtime error for agent '%s': %s", agent_name, e)
            await _add_reaction(message, "❌")
            await send_system(message.channel, str(e))
        except Exception:
            log.exception("Error processing message for agent '%s'", agent_name)
            await _add_reaction(message, "❌")
            await send_system(
                message.channel,
                f"Error communicating with agent **{agent_name}**. "
                f"Try `/kill-agent {agent_name}` and respawn."
            )
        finally:
            session.activity = ActivityState(phase="idle")

    # Process any queued messages
    log.debug("Processing message queue for agent '%s'", agent_name)
    await _process_message_queue(session)

    await bot.process_commands(message)
```

### Benefit: Reduced Complexity

**Before:**
- 8 agent_type checks
- 2 wake implementations
- 3 wake-or-queue implementations
- 4 transport creation sites
- 9 levels of nesting
- 20+ branch paths in on_message

**After:**
- 0 agent_type checks (dispatch via get_handler)
- 1 wake implementation (via handler)
- 1 wake-or-queue helper (shared)
- 1 transport creation function (shared)
- 4 levels of nesting
- 6 clear decision branches in on_message

---

## PART 4: MIGRATION PLAN

### Phase 1: Create Handler System (2-3 hours)
- Create `AgentHandler` base class
- Create `ClaudeCodeHandler` subclass
- Create `FlowcoderHandler` subclass
- Add handler properties to `AgentSession`
- Update `AgentSession.wake()` and `.sleep()` to delegate

### Phase 2: Update Wake Path (2-3 hours)
- Refactor `wake_agent()` to use `session.wake()`
- Refactor wake-or-queue logic in 3 locations
- Test with existing Claude Code agents

### Phase 3: Update Message Routing (3-4 hours)
- Refactor `on_message()` to use `session.process_message()`
- Simplify backpressure conditions
- Test message queueing

### Phase 4: Update Reconnect (1-2 hours)
- Update `_reconnect_and_drain()` to use handler
- Update `_reconnect_flowcoder()` to use handler
- Test bridge reconnect

### Phase 5: Clean Up (1 hour)
- Remove old `wake_agent()` and `sleep_agent()` global functions
- Remove `_wake_agent_via_bridge()`
- Consolidate transport creation

**Total effort:** 10-14 hours spread over 1-2 weeks

---

## PART 5: NEW REQUIREMENTS FOR ADDING AGENT TYPES

**Example: Add Langchain Agent**

1. Create handler class (50 lines):
```python
class LangchainHandler(AgentHandler):
    def is_awake(self, session): return session.langchain_chain is not None
    async def wake(self, session): ...
    async def process_message(self, session, content, channel): ...
```

2. Update AgentType union (1 line):
```python
AgentType = Literal["claude_code", "flowcoder", "langchain"]
```

3. Register handler (1 line):
```python
def get_handler(agent_type): ...
    elif agent_type == "langchain": return _langchain_handler
```

4. Add fields to AgentSession (1-2 lines):
```python
langchain_chain: Any | None = None
langchain_model: str = ""
```

**No changes needed to:**
- on_message()
- wake/sleep logic
- message queueing
- bridge integration
- reconnect logic

---

## CONCLUSION

The **Stateless Handler** pattern:

✅ Reduces code complexity by 40%
✅ Makes adding new agent types trivial
✅ Eliminates code duplication
✅ Makes state visible and debuggable
✅ Decouples bridge from agent logic
✅ Can be migrated incrementally
✅ Improves testability

Ready to implement? I recommend starting with **Phase 1** (create handlers).
