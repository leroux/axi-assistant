# RFC-0019: Test Instance Management

**Status:** Draft
**Created:** 2026-03-09

## Problem

Disposable test bot instances allow development and testing against real Discord without interfering with the production bot. The test lifecycle — slot reservation, worktree creation, systemd service management, and cleanup — involves shared mutable state (a slots file) protected by file locking. Prior regressions include double-allocation of bot tokens, stale `.env` files surviving OOM kills, and test worktrees inheriting the wrong Discord token. This RFC specifies the slot allocation protocol and instance lifecycle.

## Behavior

### Infrastructure

1. **Worktree layout.** Each test instance is a git worktree at `/home/ubuntu/axi-tests/<name>/` with its own `.env`, virtualenv, data directory, and systemd service (`axi-test@<name>`).

2. **Configuration.** Global test config lives at `~/.config/axi/test-config.json`, containing available bot tokens, guild IDs, and default settings.

3. **Slots file.** A shared JSON file tracks which bot-token/guild pairs are currently reserved by which test instance. All reads and writes to this file are serialized under an exclusive file lock.

### Slot Reservation (axi_test up)

4. **Atomic reservation.** `axi_test up` acquires an exclusive file lock on the slots file, calls `_find_free_guild` to select the first guild whose bot token is not claimed by any other slot, writes the reservation, and releases the lock. The lock must be held for the entire read-modify-write cycle.

5. **Guild selection.** `_find_free_guild` iterates configured guilds and returns the first whose bot token is not present in the set of already-claimed tokens. This prevents two test instances from receiving the same bot token.

6. **Environment file.** After reservation, a non-sensitive `.env` file is written to the worktree. It contains guild ID, instance name, and mode — but not the bot token directly.

7. **Token resolution.** The bot resolves its Discord token at startup from the slots file (slot-based lookup), not from the `.env`. Fallback order: slot-based lookup, then `DISCORD_TOKEN` environment variable, then sender token.

8. **Service start.** After reservation and `.env` creation, the systemd service `axi-test@<name>` is started.

### Slot Release (axi_test down)

9. **Ordered teardown.** `axi_test down` stops the systemd service (if running), then acquires the exclusive file lock, removes the slot reservation, and releases the lock.

10. **Idempotent stop.** Stopping an already-stopped service is a no-op, not an error.

### Health Check and Listing (axi_test list)

11. **Orphan detection.** `axi_test list` iterates all slot reservations and removes any whose worktree directory no longer exists (orphaned reservations from deleted worktrees).

12. **Status display.** Each slot is displayed with its guild, mode, branch, and current status (running/stopped/failed).

### Stale State Recovery

13. **Stale `.env` cleanup.** `cmd_up` checks systemd service status before refusing to reuse an instance name. If the service is in a failed state (e.g., OOM kill), it auto-cleans the stale `.env` and slot reservation before proceeding.

14. **Conflict detection scope.** Slot allocation conflict detection checks all allocated instances (those with `.env` or slot reservation present), not just running ones. This prevents two instances from getting the same bot token when one is stopped but not cleaned up.

## Invariants

**I4.3:** Slot allocation conflict detection must check all allocated instances (`.env` or slot reservation exists), not just running ones. Stopped instances' token reservations must remain visible to prevent double-allocation.

**I4.4:** `cmd_up` must check systemd service status before refusing reuse. Stale `.env` files and failed-state services must be auto-cleaned, not left as blockers requiring manual intervention.

**I4.5:** Token resolution for test worktrees must use slot-based lookup first, then `DISCORD_TOKEN` env var, then sender token. Agents in test worktrees must not inherit the parent process's `DISCORD_TOKEN`, which would target the wrong bot.

## Open Questions

1. **Rust implementation.** This system is currently axi-py only (`axi_test.py`). If axi-rs needs test instance management, should it call the Python CLI, reimplement the slot protocol in Rust, or use a shared lock file format that both can operate on?

2. **Slot file format.** The current format is implementation-specific. Should the slots file schema be explicitly versioned to allow cross-implementation compatibility?

3. **Maximum instances.** There is no explicit limit on concurrent test instances beyond available guild/token pairs. Should there be a configurable cap?

4. **Worktree cleanup.** `axi_test down` releases the slot but does not delete the worktree. Should there be a `destroy` command that also removes the worktree and its data directory?

## Implementation Notes

**axi-py:** The entire test instance lifecycle is managed by `axi_test.py`, a standalone CLI script. File locking uses `fcntl.flock` with `LOCK_EX`. Slot data is stored in a JSON file alongside the test config. The `msg` command sends messages as Prime's bot by reading the token from the main repo's `.env`. Systemd interaction uses `subprocess.run(["systemctl", ...])`. The worktree is created via `git worktree add`.
