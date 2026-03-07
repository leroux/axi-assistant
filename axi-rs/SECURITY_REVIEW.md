# Security Design Review — axi-rs

Reviewed: 2026-03-07

## Critical

### 1. Path traversal in permission check (`permissions.rs:115-117`)
When `canonicalize()` fails (file doesn't exist yet), the fallback uses the raw path string.
An agent can request write access to `/home/ubuntu/axi-tests/legit-dir/../../etc/cron.d/evil`
— `starts_with` passes on the non-canonical string but the actual write lands outside the allowed tree.

**Fix**: Normalize `..` components manually before the `starts_with` check (don't rely on `canonicalize` succeeding).

### 2. `discord_send_file` arbitrary file exfiltration (`tools.rs:221-230`)
Accepts any absolute path — an agent can upload `/etc/shadow`, `.env` files, or private keys to a Discord channel it controls.

**Fix**: Restrict to the agent's CWD subtree or an explicit allowlist.

## High

### 3. No CWD validation on `axi_spawn_agent` (`tools.rs:511`)
The `cwd` parameter is passed straight through. A malicious or confused agent can spawn a child with `cwd: /` and gain unrestricted filesystem access.

**Fix**: Validate CWD is under an allowed root (e.g. `/home/ubuntu/axi-tests`, `/home/ubuntu/axi-user-data`).

### 4. Agent name injection (`tools.rs`, `server.rs`)
Agent names flow into filesystem paths (log dirs, data dirs, socket names) and process arguments without sanitization.
`../../etc/cron.d/evil` as an agent name could create files outside intended directories.

**Fix**: Validate agent names at MCP tool entry: reject `/`, `..`, non-ASCII; enforce `[a-z0-9-]` pattern.

### 5. Procmux Unix socket has no authentication (`server.rs:100`)
Any local process with filesystem access to the socket can connect and control all managed subprocesses (send input, read output, kill).

**Fix**: At minimum, verify peer UID via `SO_PEERCRED`. Consider a shared secret or session token.

## Medium

### 6. Cross-channel Discord access in MCP tools (`tools.rs`)
`discord_read_messages` and `discord_send_message` accept arbitrary channel IDs. An agent assigned to channel A can read/write channel B.

**Fix**: Check the channel ID against the agent's assigned channel(s) before executing.

### 7. Permission timeout defaults to auto-allow (`bridge.rs:856,957`)
When a tool permission request times out (60s), it's auto-approved. A stalled or unreachable user = silent approval.

**Fix**: Default to deny on timeout; require explicit approval.

### 8. `Config` derives `Debug` — leaks token (`config.rs:11`)
`#[derive(Debug)]` on `Config` means any `dbg!()`, `tracing::debug!("{:?}", config)`, or panic backtrace will print the Discord bot token.

**Fix**: Implement `Debug` manually, redacting `discord_token`.

### 9. UTF-8 byte-boundary panic in message truncation (`streaming.rs`)
`content[..4000]` panics if byte 4000 lands inside a multi-byte UTF-8 character.

**Fix**: Use `content.char_indices()` or `floor_char_boundary()` to find a safe split point.

## Low

### 10. `send_file` bypasses rate-limit retry (`discord.rs`)
The `send_file` method in `DiscordClient` doesn't use the retry/rate-limit logic that `send_message` has.

**Fix**: Route through the same retry wrapper.

### 11. No per-agent message rate limiting
A runaway agent can flood Discord channels with no throttle.

**Fix**: Add a per-agent token-bucket or sliding-window rate limiter.

## Priority Order

| # | Issue | Severity | Effort |
|---|-------|----------|--------|
| 1 | Path traversal in permissions | Critical | Small |
| 2 | send_file arbitrary read | Critical | Small |
| 4 | Agent name injection | High | Small |
| 7 | Permission timeout auto-allow | High | Trivial |
| 3 | CWD validation on spawn | High | Small |
