# RFC-0006: Process & Bridge Management

**Status:** Draft
**Created:** 2026-03-09

## Problem

Agent CLI processes are managed through a process multiplexer (procmux) that keeps them
alive across bot restarts. Both implementations have converged on the same architecture
— Unix socket, named subprocesses, multiplexed I/O — but diverge on connection
semantics (axi-py buffers and replays on subscribe; axi-rs drops the old client and
resets subscriptions), stdout handling (axi-rs re-routes non-JSON stdout as stderr;
axi-py does not), and transport layering details. This RFC defines the normative wire
protocol, connection lifecycle, and transport adapter behavior.

## Behavior

### Procmux Server

1. **Socket and client model.** The server listens on a Unix socket. At most one client
   connection is active at a time. When a new client connects, the server drops the
   previous connection, resets all processes to unsubscribed state, and begins buffering
   output for each process.

2. **Subprocess spawning.** The `spawn` command starts a named subprocess in a new
   session (`setsid` / new process group) with piped stdin, stdout, and stderr. The
   server spawns relay tasks for stdout and stderr.

3. **Stdout relay and JSON enforcement.** The stdout relay parses each line as JSON
   (NDJSON). Lines that fail JSON parsing are re-routed as stderr messages rather than
   discarded. This ensures non-protocol output (debug prints, library warnings) is
   visible to the client.

4. **Output buffering.** When a process is not subscribed, all stdout/stderr/exit
   messages are buffered per-process. On `subscribe`, all buffered messages are written
   synchronously before setting `subscribed = true`, and only then is the drain/flush
   performed. This prevents interleaving of buffered replay with live relay.

5. **Subscribe response.** After replaying buffered messages, `subscribe` reports the
   process's current idle state, status, and exit code (if exited), then marks the
   process as subscribed for live forwarding.

6. **Idle tracking.** The server tracks per-process idle state by comparing monotonic
   timestamps of the last stdin write vs. the last stdout message.

7. **Kill escalation.** `kill` sends SIGTERM to the entire process group (via
   `killpg`), waits up to 5 seconds, then escalates to SIGKILL. Relay tasks are
   aborted after kill completes.

8. **Interrupt.** `interrupt` sends SIGINT to the process group, allowing both the
   target process and its children to receive the signal.

9. **Shutdown.** On shutdown, the server force-closes the client writer before calling
   close/wait_closed, with a timeout to prevent blocking on a hung client.

### Procmux Client (Connection)

10. **Demux loop.** The client runs a demux loop that routes incoming server messages:
    command results go to a shared command-response channel/queue; stdout/stderr/exit
    messages are routed to per-process registered queues.

11. **Connection loss.** When the demux loop detects EOF (server died or socket closed),
    it sets a closed flag and sends a `ConnectionLost` message to all registered process
    queues.

12. **Command serialization.** Concurrent command sends are serialized via a single lock
    (cmd_lock), with a 30-second timeout on each command-response pair.

### Wire Protocol

13. **Framing.** Newline-delimited JSON over Unix socket.

14. **Message types.** Tagged by `type` field:
    - Client → Server: `cmd` (spawn, subscribe, kill, interrupt), `stdin`
    - Server → Client: `result`, `stdout`, `stderr`, `exit`

15. **Buffer limits.** All socket readline limits and subprocess pipe buffer limits must
    be at least 10 MB to handle large Claude SDK JSON responses. The default 64 KB
    limit is insufficient.

### Transport Layer (BridgeTransport / CliSession)

16. **Transport adapter.** The transport layer adapts procmux's message types to the SDK
    transport interface. `StdoutMsg` → `StdoutEvent`, `StderrMsg` → `StderrEvent`,
    `ExitMsg` → `ExitEvent`, `ConnectionLost` → None / silent drop.

17. **Initialize interception.** For reconnecting agents (resume session), the transport
    intercepts the first `initialize` control_request and injects a synthetic success
    response without forwarding to the subprocess. This avoids redundant CLI
    initialization. The reconnecting flag is cleared after interception.

18. **Bare stream event filtering.** The transport filters bare duplicate stream events
    (e.g., `message_start`, `content_block_delta`) that arrive outside a `stream_event`
    wrapper, yielding only the wrapped versions.

19. **Stop injection.** `stop()` injects a synthetic `ExitEvent` into the local event
    queue to unblock any waiting `read_message`, then schedules the actual process kill
    as an asynchronous background operation.

20. **Fail-fast on dead connection.** `write()` and `read_message()` must raise/return
    an error immediately when the process connection is dead, rather than blocking
    indefinitely.

### CLI Configuration

21. **CLI arguments.** `to_cli_args` (or equivalent) is the single source of truth for
    CLI flag names. It must always emit `--output-format stream-json`,
    `--input-format stream-json`, and `--permission-prompt-tool stdio`.

22. **Environment variables.** The transport sets `CLAUDE_CODE_ENTRYPOINT=sdk-py` and
    `CLAUDE_AGENT_SDK_VERSION` for SDK protocol compatibility, removes `CLAUDECODE` to
    prevent nested-session detection, and disables internal compaction via
    `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=100`.

23. **MCP server merging.** SDK-provided MCP servers are inserted first, external
    servers second. External entries override SDK entries with the same name.

### Bridge Monitor

24. **Liveness check.** A background loop checks bridge connectivity every 2 seconds.
    On connection loss, it notifies the master channel, waits a 3-second grace period
    for procmux's own restart, then exits with code 42 to trigger a systemd restart.

25. **Initial connection.** Uses exponential backoff (starting at 500ms, doubling up to
    16s) for up to 6 attempts (~30s total).

### Direct Process Mode

26. **DirectProcessConnection / spawn mode.** A zero-overhead single-process backend
    that spawns a local PTY subprocess without procmux. Satisfies the same
    ProcessConnection protocol. Used when bridge mode is not configured.

## Invariants

All socket and subprocess pipe buffer limits must be at least 10 MB. (I6.1-py, I6.3-py)

Transport write/read must fail fast with a connection error when the underlying process
is dead. (I6.2-py)

Buffered messages must be fully replayed before setting subscribed=true to prevent
interleaving with live relay. (I6.5-py)

Server shutdown must force-close the client writer and use a timeout on close/wait to
prevent blocking. (I6.6-py)

Kill escalation from SIGTERM to SIGKILL must have a bounded timeout (5 seconds).
(I6.7-py)

The bot must exit with code 42 on bridge death to trigger a clean systemd restart
rather than attempting in-place reconnection with stale state. (I6.3-rs)

Unknown protocol message types must have serde fallback variants to prevent
deserialization failures when the upstream CLI adds new types. (I6.4-rs)

`--permission-prompt-tool stdio` must always be emitted in CLI arguments so permission
prompts route through the control protocol. (I6.7-rs)

## Open Questions

1. **Non-JSON stdout handling.** axi-py does not re-route non-JSON stdout lines as
   stderr; axi-rs does. Should non-JSON stdout always be re-routed as stderr
   (preserving visibility) or is silent discard acceptable?

2. **New client behavior.** axi-py documents "only one client at a time" with buffering
   on disconnect; axi-rs explicitly drops the previous connection and resets
   subscriptions. Both achieve single-client semantics but differ on whether the old
   client is gracefully notified. Should the server send a disconnect message to the old
   client before dropping it?

3. **Command timeout.** axi-rs uses 30s timeout on command-response pairs. axi-py does
   not specify an explicit timeout. Should there be a normative timeout, and if so,
   what value?

4. **Direct process mode scope.** axi-py has DirectProcessConnection as a full
   alternative; axi-rs routes everything through procmux. Should direct mode be
   maintained as a supported path or deprecated in favor of bridge-only?

5. **CLAUDE_CODE_ENTRYPOINT value.** Both implementations set `sdk-py` even though
   axi-rs is a Rust implementation. Is this intentional (protocol compat) or should
   axi-rs use a different value?

## Implementation Notes

**axi-py:** Procmux is a separate Python package (`packages/procmux`). Transport layer
is in `packages/claudewire`. `ProcmuxProcessConnection` uses `_TranslatingQueue` for
message type adaptation. `DirectProcessConnection` is a PTY-based alternative.
`ensure_running` is the helper that starts procmux as a detached subprocess and polls
until the socket accepts. Buffer limits are set to `10 * 1024 * 1024` on both server
listener and client connection.

**axi-rs:** Procmux is a separate Rust crate (`procmux/`). Transport layer is in
`claudewire/`. Uses `setsid` in `pre_exec` for new process groups. `cmd_lock` mutex
serializes commands with 30s timeout. `translate_process_msg` maps `ProcessMsg` variants
to `ProcessEvent` variants. Unknown content block and delta types use
`#[serde(other)] Unknown` fallback. Bridge binary path derived from `AXI_RS_BINARY` env
var (not hardcoded). Systemd `ExecStart` uses a bash wrapper for env var expansion.
Stdio log files rotate at 10 MB with 3 rotations.
