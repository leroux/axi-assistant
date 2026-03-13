# RFC-0009: Logging & Observability

**Status:** Draft
**Created:** 2026-03-09

## Problem

A multi-agent system with concurrent processes, async I/O, and multiple frontends
produces a high volume of log output. Without structured context, log lines from
different agents interleave unpredictably, making it impossible to reconstruct what
happened during a specific agent's turn. axi-py has a mature logging and tracing system
with context propagation, per-agent log files, and OpenTelemetry integration. axi-rs
currently has no equivalent (Section 9 is absent from the axi-rs spec). This RFC
defines the normative observability requirements that any implementation must satisfy.

## Behavior

### Structured Log Context

1. **Context fields.** Every log record produced during agent-related work must carry
   structured context fields:
   - `agent` — the agent name (e.g., `"atlas"`, `"master"`)
   - `channel` — the frontend channel identifier (Discord channel ID, WebSocket
     session, etc.)
   - `trigger` — what initiated this work (e.g., `"message:user"`, `"schedule:daily-report"`,
     `"inter-agent:atlas"`)
   - `trace` — a trace identifier for correlating related log lines across async
     boundaries (e.g., truncated OTel span ID)
   - `prefix` — a formatted prefix string combining the above for human-readable output

2. **Context injection mechanism.** Context fields are injected at the handler/filter
   level, not the logger level. This ensures child loggers that propagate to parent
   handlers also receive context injection. Missing context fields must default to empty
   strings, never cause errors.

3. **Context propagation.** Log context must propagate automatically through async child
   tasks. When an async function spawns a subtask, the subtask inherits the parent's
   agent name, channel, and trigger. The mechanism is implementation-specific
   (contextvars, task-local storage, tracing spans).

4. **Trigger formatting.** Triggers are formatted as `"{type}:{detail}"` when a detail
   is provided, or bare `"{type}"` otherwise. Examples: `"message:user"`,
   `"schedule:daily-report"`, `"startup"`.

### Log Outputs

5. **Console handler.** A console handler writes to stdout/stderr with color formatting.
   Log level is controlled by `LOG_LEVEL` env var, defaulting to `INFO`.

6. **Root rotating file handler.** A rotating file handler writes all log output at
   `DEBUG` level. Rotation at 10 MB with 3 backup files.

7. **Per-agent log files.** Each agent session gets a dedicated log file at
   `<LOG_DIR>/<name>.log` via a rotating file handler (5 MB, 2 backups). Per-agent
   loggers must not propagate to the root logger to prevent duplicate lines.

8. **UTC timestamps.** All log formatters must use UTC timestamps regardless of system
   timezone.

### Distributed Tracing (Optional)

9. **OpenTelemetry integration.** When configured, a `TracerProvider` exports spans via
   OTLP/gRPC. Initialization must degrade gracefully when the collector (e.g., Jaeger)
   is unavailable — tracing failure must not prevent the bot from starting.

10. **Traced functions.** A `@traced` decorator (or equivalent) wraps async functions in
    OTel spans, records exceptions with `ERROR` status, and re-raises. The trace ID
    from the active span is truncated (16 hex chars) and appended to log prefixes for
    correlation.

### Agent Event Log

11. **Append-only event store.** Each agent has an `AgentLog` — an ordered, append-only
    list of events that occurred during the agent's lifecycle. Events include message
    received, stream started, tool used, error occurred, etc.

12. **Subscriber notification.** Appending an event notifies all active subscribers in
    order. Subscribers are frontend connections that want real-time updates.

13. **Replay.** `AgentLog.replay(since?)` returns all events, or events since a given
    timestamp. This enables frontends to catch up after connecting to a running agent.

14. **Optional JSONL persistence.** Each `LogEvent` is optionally persisted as a JSONL
    line to disk, enabling post-hoc analysis.

### Logger Naming

15. **Consistent naming.** Every module's logger variable must reference the correct
    logger for that module. The variable name must be consistent within a codebase (e.g.,
    always `logger` or always `log`, not mixed).

## Invariants

`StructuredContextFilter.filter` (or equivalent) must always pass the record through
and set all context attributes. Formatters unconditionally reference context fields;
missing fields cause runtime errors. (I9.2-py)

Per-agent loggers must disable propagation to prevent duplicate log lines reaching root
handlers. (I9.3-py)

Logger variable naming must be consistent within each module to prevent `NameError` at
runtime. (I9.1-py)

## Open Questions

1. **axi-rs observability gap.** axi-rs has no Section 9 in its spec. What level of
   structured logging does the Rust implementation currently have? Should the Rust
   implementation use `tracing` crate (which provides structured fields and async-aware
   context propagation natively) or a simpler approach?

2. **Per-agent log files in Rust.** The per-agent rotating file approach is
   straightforward in Python's `logging` module. In Rust with `tracing`, per-agent file
   output typically requires a custom `Layer` or `Subscriber`. Is this a requirement for
   axi-rs or is structured filtering (by agent field) over a single log sufficient?

3. **Event log vs. tracing.** The `AgentLog` append-only event store overlaps with
   distributed tracing (both record what happened to an agent over time). Should these
   be unified, or do they serve different enough purposes (AgentLog for frontend replay,
   tracing for debugging) to justify both?

4. **JSONL persistence.** Is JSONL persistence of agent events normative or
   implementation-optional? For deployments with a tracing collector, JSONL may be
   redundant.

5. **Log rotation parameters.** Are the specific rotation sizes (10 MB root, 5 MB
   per-agent) and backup counts (3 root, 2 per-agent) normative, or should they be
   configurable?

## Implementation Notes

**axi-py:** `StructuredContextFilter` in `axi/log_context.py` injects context via
Python's `logging.Filter` protocol on handlers. `LogContext` uses `contextvars.ContextVar`
for async propagation. Root logger setup in `axi/config.py` with console + rotating file
handlers. Per-agent logger in `axi/axi_types.py` (`setup_agent_log`) with
`propagate = False`. OTel tracing in `axi/tracing.py` with OTLP/gRPC export and
`@traced` decorator. `AgentLog` in `packages/agenthub/agenthub/agent_log.py` with
`append`, `replay`, and subscriber notification. All formatters use `time.gmtime`
converter for UTC.

**axi-rs:** No dedicated logging/observability section in the spec. The Rust ecosystem
typically uses the `tracing` crate which provides structured fields, async-aware spans,
and subscriber-based output routing. The `tracing` crate's span context propagates
naturally through `.instrument()` and `#[instrument]`, which would satisfy the context
propagation requirement. Current implementation status for structured logging is
unknown and needs assessment.
