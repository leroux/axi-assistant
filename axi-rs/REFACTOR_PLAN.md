# Refactor Plan — axi-rs

Goal: Align the codebase with CODE-PHILOSOPHY.md. Eliminate abstraction that exists for hypothetical flexibility, make data flow explicit, simplify locking.

## Phase 1: Collapse crates

Merge `axi-hub`, `axi-bot`, `axi-config`, `axi-mcp` into a single `axi` crate. Keep `procmux`, `claudewire`, `discordquery` separate — they have genuine boundaries (separate binaries, no Discord/bot knowledge).

**Why this first**: The crate boundaries are the root cause of most abstraction. Traits, function pointers, and `Box<dyn Any>` all exist to bridge crate boundaries that shouldn't exist at 16K lines.

**Steps**:
1. Create `axi` crate, move all source files in (keep as modules: `config`, `hub`, `mcp`, `bot`)
2. Update `Cargo.toml` — single dependency set
3. Delete the four old crate directories
4. Fix all `use` paths

## Phase 2: Kill `FrontendCallbacks` trait

There's one implementation (`DiscordFrontend`). Replace the trait with direct function calls.

**Steps**:
1. Replace `hub.callbacks.post_message(name, text)` with `discord::post_message(state, name, text)` (direct call)
2. `AgentHub` loses the `callbacks: Arc<dyn FrontendCallbacks>` field — gets a reference to shared state instead
3. Delete `callbacks.rs` trait definition
4. Move `frontend.rs` callback implementations to free functions

## Phase 3: Kill `ToolContext` trait

Single implementation (`BotToolContext`). 11 methods of `Pin<Box<dyn Future>>` ceremony.

**Steps**:
1. MCP tool handlers take `&BotState` directly (or `Arc<BotState>`)
2. Delete `ToolContext` trait and `BotToolContext` wrapper
3. Tool functions call state methods directly — no boxing, no indirection

## Phase 4: Kill function pointer factories

`CreateClientFn`, `DisconnectClientFn`, `SendQueryFn` — three `Arc<dyn Fn>` indirections that each have one wiring.

**Steps**:
1. `AgentHub` stores `Arc<BotState>` (or whatever it needs) instead of function pointers
2. `lifecycle.rs` calls `bridge::create_client(state, name, resume_id)` directly
3. Delete the three type aliases from `hub.rs`
4. `(hub.create_client)(name, id)` → `bridge::create_client(&hub.state, name, id)` everywhere

## Phase 5: Kill `Box<dyn Any>` on AgentSession

`client: Option<Box<dyn Any>>` is only ever checked for `is_some()` / `is_none()`. It's a boolean pretending to be a value.

**Steps**:
1. Determine if the transport handle needs to live on the session or if `TransportMap` is sufficient (it already exists in `BotState`)
2. If transport map is sufficient: replace `client` with `connected: bool`
3. If a handle is needed: use a typed field (e.g. `Arc<Mutex<BridgeTransport>>`)
4. Same for `frontend_state: Option<Box<dyn Any>>` — type it or remove it

## Phase 6: Simplify session locking

The current pattern (lock map → clone data → drop lock → use clone → lock again → mutate → drop) is fighting the borrow checker.

**Steps**:
1. Move per-agent mutable state into `Arc<Mutex<SessionInner>>` per agent
2. The session map becomes `HashMap<String, Arc<Mutex<SessionInner>>>` — you grab the Arc, drop the map lock, then lock only the session you need
3. Eliminates the "lock entire map to touch one session" pattern
4. `rebuild_session` goes from 5 lock operations to 2

## Phase 7: Fix bugs found in review

Small targeted fixes, no architecture change needed.

**Steps**:
1. `MessageContent::preview` — use `floor_char_boundary()` or `char_indices()` instead of `&s[..max_len]`
2. Path traversal in `permissions.rs` — normalize `..` components before `starts_with` check
3. `discord_send_file` — restrict to agent's CWD subtree
4. Agent name validation — enforce `[a-z0-9-]` pattern at entry points
5. Permission timeout — default to deny, not allow
6. `Config` Debug impl — redact `discord_token`

---

## Order and dependencies

Phases 1-4 are a chain: collapse crates first, then the traits/indirections become removable.
Phase 5-6 can happen independently after phase 1.
Phase 7 is independent — can happen anytime.

## What stays the same

- `procmux` crate (separate binary, clean boundary)
- `claudewire` crate (protocol types + transport, no bot knowledge)
- `discordquery` crate (standalone CLI binary)
- Stream event processing architecture
- Permission system logic (just fix the traversal bug)
- The overall data flow: Discord event → hub → bridge → procmux → Claude CLI
