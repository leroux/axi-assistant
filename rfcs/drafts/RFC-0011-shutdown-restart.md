# RFC-0011: Shutdown & Restart

**Status:** Draft
**Created:** 2026-03-09

## Problem

Both implementations have a shutdown coordinator, a safety deadline, exit-code-42 restart convention, and bridge-vs-non-bridge divergence, but they differ on supervisor-level lifecycle (axi-py has a full process supervisor; axi-rs delegates to systemd), crash recovery semantics, and the mechanism for escalating graceful to force. A single normative document is needed so that both implementations agree on what "graceful shutdown" means, when agents are slept, and what happens when the process refuses to die.

## Behavior

### Shutdown Modes

There are two shutdown modes: **graceful** and **force**.

| Property | Graceful | Force |
|----------|----------|-------|
| Busy-agent wait (non-bridge) | Yes, poll 5s for up to 300s | No |
| Busy-agent wait (bridge) | No | No |
| Sleep agents (non-bridge) | Yes | Yes |
| Sleep agents (bridge) | No | No |
| Safety deadline | 30s | 30s |

### Graceful Shutdown

1. Check the shutdown-requested flag. If already set, ignore the request (idempotent deduplication). The flag MUST be set atomically (axi-rs: `AtomicBool::swap` with `SeqCst`; axi-py: boolean guarded by the coordinator).
2. Set the shutdown-requested flag.
3. If **not** in bridge mode, poll for busy agents every 5 seconds, waiting up to 300 seconds total. Post status updates to Discord every 30 seconds during the wait.
4. If in bridge mode, skip the busy-agent wait entirely. Agents survive in procmux; sleeping them would be destructive.
5. Proceed to the exit sequence (see Execute Exit below).

### Force Shutdown

1. Set the shutdown-requested flag (escalating a graceful in progress).
2. Skip the busy-agent wait.
3. Immediately proceed to the exit sequence.

A force request while a graceful shutdown is polling MUST interrupt the poll and proceed directly to exit.

### Execute Exit

1. Send a goodbye message to the master agent's channel.
2. If **not** in bridge mode, sleep all agents. Per-agent sleep exceptions MUST be swallowed so one broken agent does not prevent cleanup.
3. Close the Discord bot connection. The close call MUST have a timeout (10 seconds). On timeout, proceed to the kill step regardless.
4. Start the safety deadline (see below).
5. Exit the process with code 42.

### Safety Deadline

A daemon/background thread MUST call the platform's hard-exit function (`os._exit(42)` / `std::process::exit(42)`) after 30 seconds from the start of the exit phase. This guarantees termination even if the graceful path deadlocks.

### Exit Code 42 Convention

Exit code 42 is the universal restart signal:
- The process supervisor (axi-py) or systemd (axi-rs) interprets code 42 as "restart the bot."
- Exit code 0 or death by signal = clean stop, no restart.
- systemd units MUST include `SuccessExitStatus=42` so systemd treats it as a successful exit triggering `Restart=on-failure` or `Restart=always`.

### Supervisor Lifecycle (axi-py only)

The supervisor process manages bot.py's lifecycle:

- **SIGTERM/SIGINT**: Forward the signal to bot.py, wait for exit, then kill the bridge process and exit 0.
- **SIGHUP**: Forward SIGTERM to bot.py only, leave the bridge alive, then relaunch bot.py (hot restart).
- **Bridge kill escalation**: Send SIGTERM to the bridge; if it does not exit within 5 seconds, escalate to SIGKILL.
- **Crash detection**: Distinguish startup crashes (<60s uptime) from runtime crashes. After 3 consecutive crashes, stop relaunching.
- **`kill_supervisor()`**: Send SIGTERM to the parent supervisor, pause 0.5s, then `os._exit(42)`.
- **`exit_for_restart()`**: Call `os._exit(42)` without killing the supervisor (bridge mode path; CLI subprocesses survive).

### Bridge Monitor (axi-rs only)

A monitor loop checks bridge `is_alive()` every 2 seconds. On connection loss:
1. Notify the master channel.
2. Wait a 3-second grace period (allows procmux's own restart to complete).
3. Exit with code 42 to trigger systemd restart.

Bridge connection loss MUST trigger exit, not an in-place reconnect attempt.

### Process Lock

Only one bot instance may run at a time. This MUST be enforced by an exclusive file lock (e.g., `fcntl.flock` on `.bot.lock`). A second launch attempt MUST fail immediately.

## Invariants

- **I-SD-1**: The supervisor MUST escalate SIGTERM to SIGKILL on bridge processes that do not exit within 5 seconds. Without escalation, a hung bridge survives indefinitely, delaying restarts. [axi-py I11.1]
- **I-SD-2**: Bridge shutdown MUST force-close the client writer before calling `wait_closed()` on the server. `wait_closed()` blocks while the client connection is open. [axi-py I11.2]
- **I-SD-3**: Only one bot instance may run at a time, enforced by an exclusive file lock. Without this, the supervisor can launch a second bot before the first fully exits, causing duplicate messages. [axi-py I11.3]
- **I-SD-4**: Default config files MUST be seeded in the user data directory (`AXI_USER_DATA`), not the bot directory (`BOT_DIR`). [axi-py I11.4]
- **I-SD-5**: Bridge connection loss MUST trigger exit code 42, not an in-place reconnect attempt. Without the bridge, the bot has stale state and no way to communicate with agents. [axi-rs I11.1]
- **I-SD-6**: Shutdown deduplication MUST use an atomic swap so only the first caller proceeds. Concurrent shutdown triggers (e.g., SIGTERM during `/restart`) must not execute the exit path twice. [axi-rs I11.2]

## Open Questions

1. **Supervisor vs. systemd.** axi-py has a full process supervisor (`supervisor.py`) that handles crash counting, SIGHUP hot restart, and bridge lifecycle. axi-rs relies entirely on systemd for restart and has no supervisor process. Should the supervisor be normative, or is systemd-only acceptable? The supervisor provides crash counting and hot restart that systemd alone cannot replicate.

2. **Bridge monitor placement.** axi-py's bridge monitor is implicit (supervisor detects child exit). axi-rs has an explicit `is_alive()` polling loop at 2-second intervals. Should bridge health monitoring be normative, and if so, what is the detection interval?

3. **Crash counting threshold.** axi-py stops relaunching after 3 consecutive crashes. axi-rs has no crash counting (relies on systemd `StartLimitBurst`). Should the 3-crash limit be normative? What distinguishes a startup crash from a runtime crash?

4. **Discord close timeout.** axi-py wraps `bot.close()` in a 10-second timeout. axi-rs does not specify a close timeout (the 30-second safety deadline is the backstop). Should the 10-second close timeout be normative?

## Implementation Notes

### axi-py
- `ShutdownCoordinator` in `axi/shutdown.py` manages the idempotent flag, graceful poll loop, and execute_exit.
- `kill_supervisor()` sends SIGTERM to `os.getppid()`, pauses 0.5s, then `os._exit(42)`.
- `exit_for_restart()` calls `os._exit(42)` directly (bridge mode; supervisor stays alive).
- Supervisor in `axi/supervisor.py` has `_stop_handler` (SIGTERM/SIGINT), `_hup_handler` (SIGHUP), `_kill_bridge` (escalation), and a main loop with crash counting.
- Process lock via `fcntl.flock(lock_fd, LOCK_EX | LOCK_NB)` in `axi/main.py`.
- `sleep_all` swallows per-agent exceptions.
- `bot.close()` wrapped in `asyncio.wait_for` with 10s timeout.

### axi-rs
- `ShutdownCoordinator` uses `AtomicBool::swap(true, SeqCst)` for deduplication.
- `execute_exit` calls `close_app()` which calls `std::process::exit(42)`.
- Safety deadline spawns a `std::thread::spawn` that sleeps 30s then calls `std::process::exit(42)`.
- Bridge monitor in `startup.rs` polls `is_alive()` every 2s; on loss, waits 3s grace, then `exit(42)`.
- No supervisor process; relies on systemd `SuccessExitStatus=42` + `Restart=always`.
- No process lock mechanism (systemd `Type=exec` prevents concurrent starts).
- Status updates every 30s during graceful wait via Discord message.
