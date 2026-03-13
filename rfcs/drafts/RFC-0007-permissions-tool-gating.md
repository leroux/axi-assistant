# RFC-0007: Permissions & Tool Gating

**Status:** Draft
**Created:** 2026-03-09

## Problem

Agents run with access to file-writing tools, Discord messaging, MCP servers, and
system commands. Without permission gating, an agent could write outside its working
directory, send messages to its own channel (causing double messages), or invoke tools
that break the Discord-agent model (e.g., interactive worktree entry). Both
implementations gate permissions but differ on evaluation order, path resolution
fallback depth, timeout semantics (axi-rs denies on timeout; axi-py's behavior is
unspecified), and MCP server composition.

## Behavior

### Permission Evaluation Chain

1. **Ordered policy chain.** Permission requests are evaluated through an ordered chain
   of policies. The first policy to return a definitive result (allow or deny) wins. If
   all policies return no-opinion, the tool call is allowed by default.

2. **Evaluation order:**
   1. **Block policy** — Deny tools in the forbidden set: `Skill`, `EnterWorktree`,
      `Task`. Returns a static message explaining Discord-agent mode incompatibility.
   2. **Auto-allow policy** — Allow tools in the always-allowed set: `TodoWrite`,
      `EnterPlanMode`. Bypasses all downstream checks.
   3. **Plan-approval hook** (optional) — Interactive gate for `ExitPlanMode`. Blocks
      until user approves or rejects the plan via frontend.
   4. **Question hook** (optional) — Interactive gate for `AskUserQuestion`. Blocks
      until user answers via frontend.
   5. **CWD restriction policy** — Restricts file-writing tools (`Edit`, `Write`,
      `MultiEdit`, `NotebookEdit`) to resolved allowed base paths. Denies any write
      whose resolved path falls outside these paths.
   6. **Default allow** — If no policy matched, allow the tool.

3. **Permission timeout.** When a permission request times out (e.g., no user response
   to an interactive gate), the result must be **deny**, not allow.

### Path Resolution for Write Tools

4. **Three-tier resolution.** When checking whether a file path is within allowed
   directories:
   1. Attempt `canonicalize(path)` (resolves symlinks, normalizes)
   2. Fall back to `canonicalize(parent_dir).join(filename)` (handles nonexistent files)
   3. Fall back to lexical `normalize_path` (resolves `..` components without filesystem
      access, catching traversal attempts even when intermediate directories don't
      exist)

5. **Allowed path computation.** Every agent gets write access to:
   - Its own CWD
   - The user data directory

   Code agents (CWD inside `BOT_DIR` or worktrees directory) additionally get:
   - The entire worktrees directory
   - Admin-allowed extra paths

### Agent Classification

6. **Code agent detection.** An agent is classified as a "code agent" when its CWD is
   within `bot_dir` or `worktrees_dir`. Code agents get expanded write access and
   additional MCP servers.

### Agent Name Validation

7. **Name format.** Agent names must be 1-50 characters, restricted to `[a-z0-9-]`,
   with no leading or trailing hyphen. This prevents path traversal via agent names
   used in filesystem paths and Discord channel-name injection.

### Discord Tool Restrictions

8. **Self-send blocking.** `discord_send_message` must block sends to any channel that
   maps to a registered agent name. Responses are delivered by the streaming layer; MCP
   self-sends cause double messages.

9. **Caller resolution for file sends.** `discord_send_file` auto-resolves the calling
   agent's channel by scanning for sessions whose `query_lock` is currently held. If no
   match is found, the tool returns an error asking for an explicit `channel_id`. This
   approach avoids relying on `ContextVar` or thread-local state, which is unavailable
   in MCP tool execution contexts.

### MCP Server Composition

10. **Master vs. spawned agent MCP sets.**
    - The master agent gets `axi_master_mcp_server` (includes `axi_send_message`,
      `axi_restart`, spawn/kill tools).
    - Spawned admin agents (CWD inside BOT_DIR) get the narrower `axi_mcp_server`
      (spawn/kill/restart, no send_message).
    - Non-admin agents get only `utils`, `schedule`, and `playwright`.

11. **Discord MCP server.** The master agent receives the `discord_mcp_server` only when
    `BOT_WORKTREES_DIR` exists on disk. Spawned agents never receive it.

12. **Server assembly.** `_build_mcp_servers` / equivalent assembles the base MCP server
    set and merges any `extra_mcp_servers` from per-agent config or spawn arguments.
    SDK-provided servers are inserted first; external servers second (external overrides
    same-name SDK entries).

13. **Consistent tool access across lifecycle.** Both `spawn_agent` and
    `reconstruct_agents_from_channels` (or equivalent reconstruction) must call the same
    MCP server assembly logic, ensuring reconstructed agents get the same tool access as
    freshly spawned ones. SDK MCP servers must survive session rebuilds.

### Config Security

14. **Token redaction.** The Config struct's debug representation must replace the
    discord_token field with `[REDACTED]` to prevent credential exposure in logs.

## Invariants

Permission timeouts must deny, not allow. An unresponsive frontend must never silently
grant tool permissions. (I7.1-rs)

Path traversal via `..` components must be caught even when intermediate directories do
not exist on disk. The lexical normalize fallback is required. (I7.2-rs)

Agent names must be restricted to `[a-z0-9-]` with no leading/trailing hyphen to
prevent path traversal and channel-name injection. (I7.3-rs)

Discord token must not appear in Debug/log output. (I7.4-rs)

CWD-based write restrictions must be enforced in the permission handler, not
auto-allowed. (I7.5-rs)

SDK MCP servers must survive session rebuilds. (I7.6-rs)

Agents must not send Discord messages to their own channel via MCP tools. (I7.1-py)

`discord_send_file` must not rely on ContextVar/thread-local for caller resolution
because MCP tools execute in a separate async context. (I7.2-py)

MCP servers must be wired into both spawn and reconstruction paths. (I7.3-py)

## Open Questions

1. **Default-allow vs. default-deny.** Both implementations default to allow for
   unrecognized tools. Should the default be deny-with-allowlist instead? The current
   approach means new tools added to Claude Code are automatically available, which may
   be undesirable for security-sensitive deployments.

2. **Plan approval timeout duration.** axi-rs uses 10 minutes for Discord plan approval.
   axi-py does not specify an explicit timeout (blocks until user reacts). Should there
   be a normative timeout, and what should happen when it expires?

3. **Admin-extra paths.** The concept of admin-allowed extra paths is mentioned in
   axi-py but not clearly defined in axi-rs. Should there be a normative config key for
   specifying additional allowed write paths per-agent?

4. **MCP server naming divergence.** axi-py distinguishes `axi_master_mcp_server` from
   `axi_mcp_server`; axi-rs uses `sdk_mcp_servers` generically. Should the MCP server
   names and scoping be normatively defined?

## Implementation Notes

**axi-py:** Permission chain is in `packages/claudewire/claudewire/permissions.py`
(`compose` function — first non-None result wins). Path computation in
`packages/agenthub/agenthub/permissions.py` (`compute_allowed_paths`,
`build_permission_callback`). Discord tool restrictions in `axi/tools.py`. MCP server
composition in `axi/agents.py` (`_build_mcp_servers`). Caller resolution for
`discord_send_file` uses `query_lock` scan of `channel_to_agent` mapping.

**axi-rs:** Permissions in `permissions.rs` with `normalize_path` for lexical path
normalization and `check_permission` for CWD-based validation. Agent name validation in
`mcp_tools.rs` (`validate_agent_name`). Config token redaction via custom `Debug` impl
in `axi-config/src/config.rs`. CWD-based check invoked from the bridge's
`handle_permission_request`. Permission timeout auto-denies (changed from the original
auto-allow behavior).
