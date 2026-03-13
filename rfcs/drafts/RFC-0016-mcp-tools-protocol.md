# RFC-0016: MCP Tools & Protocol

**Status:** Draft
**Created:** 2026-03-09

## Problem

The MCP (Model Context Protocol) layer exposes in-process tool servers to Claude agents via JSON-RPC 2.0. The server topology varies by agent role (master vs. spawned), permission checks gate filesystem access, and Discord snowflake parsing must tolerate both string and numeric JSON representations. These details are currently encoded only in the axi-rs implementation with no normative reference, making it difficult to verify correctness or extend the tool surface.

## Behavior

### Wire Protocol

1. **JSON-RPC 2.0 types.** The MCP protocol module defines three wire types: `JsonRpcRequest` (with method, params, id), `JsonRpcResponse` (with result or error, id), and `JsonRpcError` (with code, message, optional data). All MCP communication between Claude Agent SDK and in-process servers uses these types.

2. **Message routing.** MCP messages arrive as `control_request` events with subtype `mcp_message`. The handler extracts `server_name` and `message` fields, dispatches to the matching `McpServer` instance on the agent's session (via `sdk_mcp_servers`), and wraps the result in a `control_response`.

3. **Method dispatch.** `handle_mcp_message` routes JSON-RPC methods to the `McpServer`:
   - `initialize` — returns server capabilities
   - `notifications/initialized` — acknowledged, no response needed
   - `tools/list` — returns all registered tool definitions
   - `tools/call` — dispatches to the named tool handler

### McpServer

4. **Registration.** `McpServer` is a named collection of tools. `add_tool` registers a `ToolDefinition` (name, description, input schema) paired with an async handler closure.

5. **Dispatch.** `call_tool` looks up the handler by name and invokes it. Unknown tool names return an error result (not a panic or exception).

6. **Result constructors.** `ToolResult` has two constructors:
   - `text(content)` — success, wraps a single text `ContentBlock`, `is_error` unset
   - `error(message)` — failure, wraps a single text `ContentBlock`, `is_error = true`

### Server Topology

7. **Per-agent server assignment.** `build_sdk_mcp_config` determines which MCP servers each agent receives:
   - All agents: utils, schedule, discord
   - Master agent: adds `create_master_server` (includes restart, send_message)
   - Spawned agents: adds `create_agent_server` (excludes restart, send_message)

8. **Utils server tools:**
   - `get_date_and_time` — returns current date/time with logical day boundary
   - `discord_send_file` — uploads a file to a Discord channel
   - `set_agent_status` / `clear_agent_status` — sets or clears the agent's custom status

9. **Discord MCP server tools:** `send_message`, `read_messages`, `list_channels`, `add_reaction`, `edit_message`, `delete_message`. All operate via the `DiscordClient` (httpx `AsyncClient`).

10. **Master server tools:** `axi_spawn_agent`, `axi_kill_agent`, `axi_list_agents`, `axi_restart`, `axi_send_message`.

11. **Agent server tools:** `axi_spawn_agent`, `axi_kill_agent`, `axi_list_agents`. Omits `axi_restart` and `axi_send_message`.

### Permission Handling

12. **CWD-based checks.** Permission handling runs CWD-based checks before auto-allowing tool calls. Writes outside the agent's CWD are denied. Forbidden tools (e.g., `Task`) are blocked regardless of CWD.

### Input Parsing

13. **Snowflake ID parsing.** Discord snowflake IDs are parsed flexibly from both string (`"123456"`) and number (`123456`) JSON values via a `parse_id` helper. Both representations must resolve to the same u64 value.

14. **Agent name validation.** Agent names must be 1-50 characters, matching `[a-z0-9-]`, with no leading or trailing hyphens.

## Invariants

**I16.1:** SDK MCP servers must survive session rebuilds. `rebuild_session` must copy `sdk_mcp_servers` from the old session to the new one. Failure to do so causes agents to lose access to all in-process MCP tools after any restart that calls `rebuild_session`.

**I16.2:** SDK MCP servers must not be stripped from flowcoder engine config. The engine relays `control_request`/`control_response` messages, so stripping SDK servers causes agents running through flowcoder to lose MCP tools entirely.

## Open Questions

1. **External server injection.** `build_sdk_mcp_config` currently adds playwright as an external stdio server for all agents. Should external server configuration be data-driven (config file) rather than hardcoded?

2. **Tool-level permissions.** Permission checks currently operate at the CWD level. Should individual tools support finer-grained permission declarations (e.g., read-only vs. read-write)?

## Implementation Notes

**axi-rs:** `McpServer` stores handlers as `Arc<dyn Fn(Value) -> BoxFuture<ToolResult>>` in a `HashMap`. `build_sdk_mcp_config` lives in `mcp_tools.rs`. `handle_mcp_message` lives alongside the protocol types in `mcp_protocol.rs`. `parse_id` uses `serde_json::Value` matching on both `Value::String` and `Value::Number`. Agent name validation regex is compiled once and reused.
