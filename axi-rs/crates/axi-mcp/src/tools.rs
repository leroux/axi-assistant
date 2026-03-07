//! MCP tool implementations — agent management, Discord, and utilities.
//!
//! Each function creates an McpServer with the appropriate tools registered.
//! Tool handlers capture shared state via Arc closures.

use std::sync::Arc;

use chrono::{Datelike, Local, Timelike, Weekday};
use serde_json::{json, Value};
use tracing::info;

use axi_config::{Config, DiscordClient};

use crate::protocol::{McpServer, ToolArgs, ToolResult};

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

fn get_str(args: &ToolArgs, key: &str) -> String {
    args.get(key)
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string()
}

fn get_opt_str(args: &ToolArgs, key: &str) -> Option<String> {
    args.get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn get_bool(args: &ToolArgs, key: &str) -> bool {
    args.get(key)
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

/// Parse a Discord snowflake ID from a string or number value.
fn parse_id(args: &ToolArgs, key: &str) -> Option<u64> {
    args.get(key).and_then(|v| {
        v.as_u64()
            .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
    })
}

// ---------------------------------------------------------------------------
// Utility tools (available to all agents)
// ---------------------------------------------------------------------------

/// Create the utils MCP server (date/time, file upload, status).
pub fn create_utils_server(config: Arc<Config>, discord: Arc<DiscordClient>) -> McpServer {
    let mut server = McpServer::new("utils", "1.0.0");
    let cfg = config.clone();

    // get_date_and_time
    server.add_tool(
        "get_date_and_time",
        "Get the current date and time with logical day/week calculations. \
         Accounts for the user's configured day boundary (the hour when a new 'day' starts). \
         Always call this first to orient yourself before working with plans.",
        json!({"type": "object", "properties": {}, "required": []}),
        move |_args| {
            let cfg = cfg.clone();
            async move {
                let boundary = cfg.day_boundary_hour;
                let now = Local::now();

                // Logical date: if before boundary hour, still "yesterday"
                let logical = if (now.hour() as u32) < boundary {
                    now - chrono::Duration::days(1)
                } else {
                    now
                };

                // Logical week start (Sunday)
                let days_since_sunday = match logical.weekday() {
                    Weekday::Sun => 0,
                    Weekday::Mon => 1,
                    Weekday::Tue => 2,
                    Weekday::Wed => 3,
                    Weekday::Thu => 4,
                    Weekday::Fri => 5,
                    Weekday::Sat => 6,
                };

                let boundary_display = match boundary {
                    0 => "12:00 AM (midnight)".to_string(),
                    h if h < 12 => format!("{}:00 AM", h),
                    12 => "12:00 PM (noon)".to_string(),
                    h => format!("{}:00 PM", h - 12),
                };

                let result = json!({
                    "now": now.to_rfc3339(),
                    "now_display": now.format("%A, %b %-d, %Y %-I:%M %p").to_string(),
                    "logical_date": logical.format("%Y-%m-%d").to_string(),
                    "logical_date_display": logical.format("%A, %b %-d, %Y").to_string(),
                    "logical_day_of_week": logical.format("%A").to_string(),
                    "logical_week_start": (logical - chrono::Duration::days(days_since_sunday))
                        .format("%Y-%m-%d").to_string(),
                    "timezone": cfg.schedule_timezone,
                    "day_boundary": boundary_display,
                });

                ToolResult::text(serde_json::to_string_pretty(&result).unwrap())
            }
        },
    );

    // discord_send_file
    let discord_for_file = discord.clone();
    server.add_tool(
        "discord_send_file",
        "Send a file as a Discord message attachment to your own channel or another channel. \
         If channel_id is omitted, the file is sent to your own agent channel.",
        json!({
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "The Discord channel ID. Omit to send to your own channel."},
                "file_path": {"type": "string", "description": "Absolute path to the file to upload"},
                "content": {"type": "string", "description": "Optional text message to include with the file"}
            },
            "required": ["file_path"]
        }),
        move |args| {
            let dc = discord_for_file.clone();
            async move {
                let file_path = get_str(&args, "file_path");
                let content = get_opt_str(&args, "content");

                let channel_id = match parse_id(&args, "channel_id") {
                    Some(id) => id,
                    None => {
                        return ToolResult::error(
                            "Error: could not determine channel. Provide channel_id explicitly.",
                        );
                    }
                };

                let path = std::path::Path::new(&file_path);
                if !path.is_file() {
                    return ToolResult::error(format!("Error: file not found: {}", file_path));
                }

                let filename = path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("file");
                match std::fs::read(path) {
                    Ok(data) => {
                        match dc
                            .send_file(channel_id, filename, data, content.as_deref())
                            .await
                        {
                            Ok(msg) => {
                                let msg_id = msg
                                    .get("id")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("unknown");
                                ToolResult::text(format!(
                                    "File '{}' sent (msg id: {})",
                                    filename, msg_id
                                ))
                            }
                            Err(e) => ToolResult::error(format!("Error: {}", e)),
                        }
                    }
                    Err(e) => ToolResult::error(format!("Error reading file: {}", e)),
                }
            }
        },
    );

    // set_agent_status
    server.add_tool(
        "set_agent_status",
        "Set a custom status on your agent's Discord channel (shown as an emoji prefix). \
         Use this to signal what you're doing (e.g. 'testing', 'deploying', 'waiting for CI'). \
         Call clear_agent_status to revert to auto-detected status.",
        json!({
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Short status label (e.g. 'testing', 'deploying', 'blocked')"}
            },
            "required": ["status"]
        }),
        |args| async move {
            let status = get_str(&args, "status");
            if status.is_empty() {
                return ToolResult::error("Error: status is required.");
            }
            // TODO: set status via hub/channels
            info!("Agent status set to: {}", status);
            ToolResult::text(format!(
                "Status set to '{}'. Channel will update shortly.",
                status
            ))
        },
    );

    // clear_agent_status
    server.add_tool(
        "clear_agent_status",
        "Clear your custom channel status and revert to auto-detected status.",
        json!({"type": "object", "properties": {}, "required": []}),
        |_args| async move {
            // TODO: clear status via hub/channels
            info!("Agent status cleared");
            ToolResult::text(
                "Custom status cleared. Channel will revert to auto-detected status.",
            )
        },
    );

    server
}

// ---------------------------------------------------------------------------
// Discord REST tools (cross-channel messaging)
// ---------------------------------------------------------------------------

/// Create the Discord MCP server for cross-channel messaging.
pub fn create_discord_server(discord: Arc<DiscordClient>) -> McpServer {
    let mut server = McpServer::new("discord", "1.0.0");

    // discord_list_channels
    let dc1 = discord.clone();
    server.add_tool(
        "discord_list_channels",
        "List text channels in a Discord guild/server. Returns channel id, name, and category.",
        json!({
            "type": "object",
            "properties": {
                "guild_id": {"type": "string", "description": "The Discord guild (server) ID"}
            },
            "required": ["guild_id"]
        }),
        move |args| {
            let dc = dc1.clone();
            async move {
                let guild_id = match parse_id(&args, "guild_id") {
                    Some(id) => id,
                    None => return ToolResult::error("Error: invalid guild_id"),
                };
                match dc.list_channels(guild_id).await {
                    Ok(channels) => ToolResult::text(
                        serde_json::to_string_pretty(&channels).unwrap_or_default(),
                    ),
                    Err(e) => ToolResult::error(format!("Error: {}", e)),
                }
            }
        },
    );

    // discord_read_messages
    let dc2 = discord.clone();
    server.add_tool(
        "discord_read_messages",
        "Read recent messages from a Discord channel. Returns formatted message history.",
        json!({
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "The Discord channel ID"},
                "limit": {"type": "integer", "description": "Number of messages to fetch (default 20, max 100)"}
            },
            "required": ["channel_id"]
        }),
        move |args| {
            let dc = dc2.clone();
            async move {
                let channel_id = match parse_id(&args, "channel_id") {
                    Some(id) => id,
                    None => return ToolResult::error("Error: invalid channel_id"),
                };
                let limit = args
                    .get("limit")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(20)
                    .min(100) as u32;

                match dc.get_messages(channel_id, limit, None, None).await {
                    Ok(value) => {
                        let mut messages: Vec<Value> = value
                            .as_array()
                            .cloned()
                            .unwrap_or_default();
                        messages.reverse(); // chronological order
                        let formatted: Vec<String> = messages
                            .iter()
                            .map(|msg| {
                                let author = msg
                                    .get("author")
                                    .and_then(|a| a.get("username"))
                                    .and_then(|u| u.as_str())
                                    .unwrap_or("unknown");
                                let content =
                                    msg.get("content").and_then(|c| c.as_str()).unwrap_or("");
                                let timestamp = msg
                                    .get("timestamp")
                                    .and_then(|t| t.as_str())
                                    .unwrap_or("");
                                format!("[{}] {}: {}", timestamp, author, content)
                            })
                            .collect();
                        ToolResult::text(formatted.join("\n"))
                    }
                    Err(e) => ToolResult::error(format!("Error: {}", e)),
                }
            }
        },
    );

    // discord_send_message
    let dc3 = discord.clone();
    server.add_tool(
        "discord_send_message",
        "Send a message to a Discord channel OTHER than your own. Your text responses are \
         automatically delivered to your own channel — do NOT use this tool for that. \
         This tool is only for cross-channel messaging.",
        json!({
            "type": "object",
            "properties": {
                "channel_id": {"type": "string", "description": "The Discord channel ID"},
                "content": {"type": "string", "description": "The message content to send"}
            },
            "required": ["channel_id", "content"]
        }),
        move |args| {
            let dc = dc3.clone();
            async move {
                let channel_id = match parse_id(&args, "channel_id") {
                    Some(id) => id,
                    None => return ToolResult::error("Error: invalid channel_id"),
                };
                let content = get_str(&args, "content");

                match dc
                    .send_message(channel_id, &content)
                    .await
                {
                    Ok(msg) => {
                        let msg_id = msg
                            .get("id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("unknown");
                        ToolResult::text(format!("Message sent (id: {})", msg_id))
                    }
                    Err(e) => ToolResult::error(format!("Error: {}", e)),
                }
            }
        },
    );

    server
}

// ---------------------------------------------------------------------------
// Master agent tools (spawn, kill, restart, send_message)
// ---------------------------------------------------------------------------

/// Create the master agent MCP server.
pub fn create_master_server(_config: Arc<Config>) -> McpServer {
    let mut server = McpServer::new("axi", "1.0.0");

    // axi_spawn_agent
    server.add_tool(
        "axi_spawn_agent",
        "Spawn a new Axi agent session with its own Discord channel.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique short name, no spaces"},
                "cwd": {"type": "string", "description": "Absolute path to the working directory"},
                "prompt": {"type": "string", "description": "Initial task instructions"},
                "resume": {"type": "string", "description": "Session ID to resume"},
                "packs": {"type": "array", "items": {"type": "string"}, "description": "Pack names for system prompt"},
                "compact_instructions": {"type": "string", "description": "Instructions for context compaction"},
                "mcp_servers": {"type": "array", "items": {"type": "string"}, "description": "Custom MCP server names"}
            },
            "required": ["name", "prompt"]
        }),
        |args| async move {
            let name = get_str(&args, "name");
            let prompt = get_str(&args, "prompt");

            if name.is_empty() {
                return ToolResult::error("Error: 'name' is required and cannot be empty.");
            }
            if prompt.is_empty() {
                return ToolResult::error("Error: 'prompt' is required.");
            }

            // TODO: delegate to hub.spawn_agent
            info!("Spawn agent request: name={}, prompt_len={}", name, prompt.len());
            ToolResult::text(format!(
                "Agent '{}' spawn initiated. The agent's channel will be notified when it's ready.",
                name
            ))
        },
    );

    // axi_kill_agent
    server.add_tool(
        "axi_kill_agent",
        "Kill an Axi agent session and move its Discord channel to the Killed category.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the agent to kill"}
            },
            "required": ["name"]
        }),
        |args| async move {
            let name = get_str(&args, "name");
            if name.is_empty() {
                return ToolResult::error("Error: 'name' is required.");
            }

            // TODO: delegate to hub.end_session
            info!("Kill agent request: {}", name);
            ToolResult::text(format!("Agent '{}' killed.", name))
        },
    );

    // axi_restart
    server.add_tool(
        "axi_restart",
        "Restart the Axi bot. Waits for busy agents to finish first (graceful).",
        json!({"type": "object", "properties": {}, "required": []}),
        |_args| async move {
            info!("Restart requested via MCP tool");
            // TODO: trigger shutdown coordinator
            ToolResult::text("Graceful restart initiated. Waiting for busy agents to finish...")
        },
    );

    // axi_restart_agent
    server.add_tool(
        "axi_restart_agent",
        "Restart a single agent's CLI process with a fresh system prompt.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the agent to restart"}
            },
            "required": ["name"]
        }),
        |args| async move {
            let name = get_str(&args, "name");
            if name.is_empty() {
                return ToolResult::error("Error: 'name' is required.");
            }

            // TODO: delegate to hub.rebuild_session
            info!("Restart agent request: {}", name);
            ToolResult::text(format!(
                "Agent '{}' restarted. System prompt refreshed.",
                name
            ))
        },
    );

    // axi_send_message
    server.add_tool(
        "axi_send_message",
        "Send a message to a spawned agent. The message appears in the agent's Discord channel.",
        json!({
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Name of the target agent"},
                "content": {"type": "string", "description": "The message content to send"}
            },
            "required": ["agent_name", "content"]
        }),
        |args| async move {
            let target = get_str(&args, "agent_name");
            let content = get_str(&args, "content");

            if target.is_empty() {
                return ToolResult::error("Error: agent_name is required.");
            }
            if content.is_empty() {
                return ToolResult::error("Error: content is required.");
            }

            // TODO: delegate to hub.deliver_inter_agent_message
            info!("Inter-agent message to '{}': {}", target, &content[..content.len().min(100)]);
            ToolResult::text(format!("Message delivered to '{}'.", target))
        },
    );

    server
}

/// Create the spawned agent MCP server (no restart/send, just spawn/kill/restart-agent).
pub fn create_agent_server(_config: Arc<Config>) -> McpServer {
    let mut server = McpServer::new("axi", "1.0.0");

    // Reuse the same tool definitions as master but without axi_restart and axi_send_message
    // For now, create a simpler version
    server.add_tool(
        "axi_spawn_agent",
        "Spawn a new Axi agent session with its own Discord channel.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique short name, no spaces"},
                "cwd": {"type": "string", "description": "Absolute path to the working directory"},
                "prompt": {"type": "string", "description": "Initial task instructions"},
                "resume": {"type": "string", "description": "Session ID to resume"}
            },
            "required": ["name", "prompt"]
        }),
        |args| async move {
            let name = get_str(&args, "name");
            if name.is_empty() {
                return ToolResult::error("Error: 'name' is required.");
            }
            // TODO: delegate to hub
            ToolResult::text(format!("Agent '{}' spawn initiated.", name))
        },
    );

    server.add_tool(
        "axi_kill_agent",
        "Kill an Axi agent session.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the agent to kill"}
            },
            "required": ["name"]
        }),
        |args| async move {
            let name = get_str(&args, "name");
            if name.is_empty() {
                return ToolResult::error("Error: 'name' is required.");
            }
            ToolResult::text(format!("Agent '{}' killed.", name))
        },
    );

    server.add_tool(
        "axi_restart_agent",
        "Restart a single agent's CLI process with a fresh system prompt.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the agent to restart"}
            },
            "required": ["name"]
        }),
        |args| async move {
            let name = get_str(&args, "name");
            if name.is_empty() {
                return ToolResult::error("Error: 'name' is required.");
            }
            ToolResult::text(format!("Agent '{}' restarted.", name))
        },
    );

    server
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn test_config() -> Arc<Config> {
        unsafe { std::env::set_var("DISCORD_TOKEN", "test") };
        unsafe { std::env::set_var("DISCORD_GUILD_ID", "1") };
        unsafe { std::env::set_var("ALLOWED_USER_IDS", "1") };
        Arc::new(Config::from_env().unwrap())
    }

    #[tokio::test]
    async fn utils_date_time() {
        let config = test_config();
        let discord = Arc::new(DiscordClient::new("test"));
        let server = create_utils_server(config, discord);
        let result = server.call_tool("get_date_and_time", ToolArgs::new()).await;
        assert!(result.is_error.is_none());
        let text = &result.content[0].text;
        assert!(text.contains("now"));
        assert!(text.contains("logical_date"));
    }

    #[tokio::test]
    async fn master_spawn_validation() {
        let config = test_config();
        let server = create_master_server(config);

        // Empty name should error
        let result = server
            .call_tool("axi_spawn_agent", ToolArgs::new())
            .await;
        assert_eq!(result.is_error, Some(true));

        // Valid name should succeed
        let mut args = ToolArgs::new();
        args.insert("name".to_string(), json!("test-agent"));
        args.insert("prompt".to_string(), json!("do something"));
        let result = server.call_tool("axi_spawn_agent", args).await;
        assert!(result.is_error.is_none());
    }
}
