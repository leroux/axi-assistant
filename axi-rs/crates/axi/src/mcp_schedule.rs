//! Per-agent schedule management MCP tools.
//!
//! Each agent gets its own MCP server instance scoped to its name.
//! Schedules are stored in a shared JSON file with ownership tracking.

use std::path::{Path, PathBuf};

use serde_json::json;
use tokio::sync::Mutex;
use tracing::info;

use crate::mcp_protocol::{McpServer, ToolResult};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_SCHEDULES_PER_AGENT: usize = 20;
const MAX_NAME_LEN: usize = 50;
const MAX_PROMPT_LEN: usize = 2000;

/// Shared lock for schedules.json read-modify-write cycles.
static SCHEDULES_LOCK: Mutex<()> = Mutex::const_new(());

// ---------------------------------------------------------------------------
// File I/O
// ---------------------------------------------------------------------------

fn load_schedules(path: &Path) -> Vec<serde_json::Value> {
    match std::fs::read_to_string(path) {
        Ok(data) => serde_json::from_str(&data).unwrap_or_default(),
        Err(_) => Vec::new(),
    }
}

fn save_schedules(path: &Path, entries: &[serde_json::Value]) {
    if let Ok(data) = serde_json::to_string_pretty(entries) {
        let _ = std::fs::write(path, format!("{data}\n"));
    }
}

/// Check if a schedule name is valid.
fn is_valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= MAX_NAME_LEN
        && name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        && name
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphanumeric())
}

/// Get schedules owned by a specific agent.
fn agent_schedules(entries: &[serde_json::Value], agent_name: &str) -> Vec<serde_json::Value> {
    entries
        .iter()
        .filter(|e| {
            let owner = e
                .get("owner")
                .or_else(|| e.get("session"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            owner == agent_name
        })
        .cloned()
        .collect()
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

/// Create a per-agent schedule MCP server.
pub fn create_schedule_server(
    agent_name: String,
    schedules_path: PathBuf,
    agent_cwd: Option<String>,
) -> McpServer {
    let mut server = McpServer::new("schedule", "1.0.0");

    let name1 = agent_name.clone();
    let path1 = schedules_path.clone();

    // schedule_list
    server.add_tool(
        "schedule_list",
        "List all of your scheduled tasks (one-off and recurring).",
        json!({"type": "object", "properties": {}, "required": []}),
        move |_args| {
            let name = name1.clone();
            let path = path1.clone();
            async move {
                let _lock = SCHEDULES_LOCK.lock().await;
                let entries = load_schedules(&path);
                let mine = agent_schedules(&entries, &name);

                if mine.is_empty() {
                    return ToolResult::text("You have no scheduled tasks.");
                }

                let result: Vec<serde_json::Value> = mine
                    .iter()
                    .map(|e| {
                        let mut item = json!({
                            "name": e.get("name").and_then(|v| v.as_str()).unwrap_or(""),
                            "prompt": e.get("prompt").and_then(|v| v.as_str()).unwrap_or(""),
                        });
                        if let Some(schedule) = e.get("schedule") {
                            item["type"] = json!("recurring");
                            item["schedule"] = schedule.clone();
                        } else if let Some(at) = e.get("at") {
                            item["type"] = json!("one_off");
                            item["at"] = at.clone();
                        }
                        if e.get("reset_context")
                            .and_then(serde_json::Value::as_bool)
                            .unwrap_or(false)
                        {
                            item["reset_context"] = json!(true);
                        }
                        item
                    })
                    .collect();

                ToolResult::text(serde_json::to_string_pretty(&result).unwrap_or_default())
            }
        },
    );

    // schedule_create
    let name2 = agent_name.clone();
    let path2 = schedules_path.clone();
    let cwd2 = agent_cwd;

    server.add_tool(
        "schedule_create",
        "Create a new scheduled task. Use schedule_type 'recurring' with a cron expression, \
         or 'one_off' with an ISO 8601 datetime for a single future event.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short identifier (lowercase, numbers, hyphens). Must be unique."},
                "prompt": {"type": "string", "description": "The message sent when this schedule fires."},
                "schedule_type": {"type": "string", "enum": ["recurring", "one_off"], "description": "Whether this repeats or fires once."},
                "cron": {"type": "string", "description": "Cron expression (required for recurring)."},
                "at": {"type": "string", "description": "ISO 8601 datetime (required for one_off)."},
                "reset_context": {"type": "boolean", "description": "If true, resets conversation context when fired."}
            },
            "required": ["name", "prompt", "schedule_type"]
        }),
        move |args| {
            let name = name2.clone();
            let path = path2.clone();
            let cwd = cwd2.clone();
            async move {
                let sched_name = args
                    .get("name")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_string();
                let prompt = args
                    .get("prompt")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_string();
                let stype = args
                    .get("schedule_type")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_string();
                let cron_expr = args
                    .get("cron")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_string();
                let at_str = args
                    .get("at")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_string();
                let reset_context = args
                    .get("reset_context")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false);

                // Validate name
                if !is_valid_name(&sched_name) {
                    return ToolResult::error(format!(
                        "Invalid name: must be 1-{MAX_NAME_LEN} chars, lowercase alphanumeric and hyphens."
                    ));
                }

                // Validate prompt
                if prompt.is_empty() {
                    return ToolResult::error("Prompt is required and cannot be empty.");
                }
                if prompt.len() > MAX_PROMPT_LEN {
                    return ToolResult::error(format!(
                        "Prompt too long ({} chars). Max is {}.",
                        prompt.len(),
                        MAX_PROMPT_LEN
                    ));
                }

                // Validate schedule type
                if stype != "recurring" && stype != "one_off" {
                    return ToolResult::error(
                        "schedule_type must be 'recurring' or 'one_off'.",
                    );
                }

                let _lock = SCHEDULES_LOCK.lock().await;
                let mut entries = load_schedules(&path);
                let mine = agent_schedules(&entries, &name);

                // Check duplicates
                if mine.iter().any(|e| {
                    e.get("name").and_then(|v| v.as_str()) == Some(&sched_name)
                }) {
                    return ToolResult::error(format!(
                        "Schedule '{sched_name}' already exists. Delete it first or use a different name."
                    ));
                }

                // Check limit
                if mine.len() >= MAX_SCHEDULES_PER_AGENT {
                    return ToolResult::error(format!(
                        "You have {} schedules (max {}). Delete some first.",
                        mine.len(),
                        MAX_SCHEDULES_PER_AGENT
                    ));
                }

                let mut entry = json!({
                    "name": sched_name,
                    "prompt": prompt,
                    "owner": name,
                });

                if let Some(cwd) = &cwd {
                    entry["cwd"] = json!(cwd);
                }

                if stype == "recurring" {
                    if cron_expr.is_empty() {
                        return ToolResult::error(
                            "Cron expression is required for recurring schedules.",
                        );
                    }
                    // Basic cron validation (5 fields)
                    if cron_expr.split_whitespace().count() != 5 {
                        return ToolResult::error(
                            "Invalid cron expression. Must have 5 fields (min hour dom mon dow).",
                        );
                    }
                    entry["schedule"] = json!(cron_expr);
                } else {
                    // one_off
                    if at_str.is_empty() {
                        return ToolResult::error(
                            "ISO 8601 datetime is required for one_off schedules.",
                        );
                    }
                    entry["at"] = json!(at_str);
                }

                if reset_context {
                    entry["reset_context"] = json!(true);
                }

                entries.push(entry);
                save_schedules(&path, &entries);

                info!(
                    "Schedule '{}' created for agent '{}' (type={})",
                    sched_name, name, stype
                );

                let type_detail = if stype == "recurring" {
                    format!("recurring ({cron_expr})")
                } else {
                    format!("one-off at {at_str}")
                };

                ToolResult::text(format!(
                    "Schedule '{sched_name}' created successfully ({type_detail})."
                ))
            }
        },
    );

    // schedule_delete
    let name3 = agent_name;
    let path3 = schedules_path;

    server.add_tool(
        "schedule_delete",
        "Delete one of your scheduled tasks by name.",
        json!({
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The name of the schedule to delete."}
            },
            "required": ["name"]
        }),
        move |args| {
            let name = name3.clone();
            let path = path3.clone();
            async move {
                let sched_name = args
                    .get("name")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_string();

                if sched_name.is_empty() {
                    return ToolResult::error("Schedule name is required.");
                }

                let _lock = SCHEDULES_LOCK.lock().await;
                let mut entries = load_schedules(&path);

                let original_len = entries.len();
                entries.retain(|e| {
                    let e_name = e
                        .get("name")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let e_owner = e
                        .get("owner")
                        .or_else(|| e.get("session"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    !(e_name == sched_name && e_owner == name)
                });

                if entries.len() == original_len {
                    return ToolResult::error(format!(
                        "Schedule '{sched_name}' not found."
                    ));
                }

                save_schedules(&path, &entries);
                info!("Schedule '{}' deleted by agent '{}'", sched_name, name);
                ToolResult::text(format!("Schedule '{sched_name}' deleted."))
            }
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
    use crate::mcp_protocol::ToolArgs;

    #[test]
    fn test_is_valid_name() {
        assert!(is_valid_name("my-schedule"));
        assert!(is_valid_name("test123"));
        assert!(is_valid_name("a"));
        assert!(!is_valid_name(""));
        assert!(!is_valid_name("-invalid"));
        assert!(!is_valid_name("HAS_UPPER"));
        assert!(!is_valid_name("has spaces"));
    }

    #[tokio::test]
    async fn schedule_crud() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("schedules.json");

        let server = create_schedule_server(
            "test-agent".to_string(),
            path.clone(),
            Some("/tmp".to_string()),
        );

        // List (empty)
        let result = server
            .call_tool("schedule_list", ToolArgs::new())
            .await;
        assert!(result.content[0].text.contains("no scheduled"));

        // Create
        let mut args = ToolArgs::new();
        args.insert("name".to_string(), json!("daily-check"));
        args.insert("prompt".to_string(), json!("Check status"));
        args.insert("schedule_type".to_string(), json!("recurring"));
        args.insert("cron".to_string(), json!("0 9 * * *"));
        let result = server.call_tool("schedule_create", args).await;
        assert!(result.is_error.is_none());
        assert!(result.content[0].text.contains("created"));

        // List (has entry)
        let result = server
            .call_tool("schedule_list", ToolArgs::new())
            .await;
        assert!(result.content[0].text.contains("daily-check"));

        // Delete
        let mut args = ToolArgs::new();
        args.insert("name".to_string(), json!("daily-check"));
        let result = server.call_tool("schedule_delete", args).await;
        assert!(result.is_error.is_none());
        assert!(result.content[0].text.contains("deleted"));

        // List (empty again)
        let result = server
            .call_tool("schedule_list", ToolArgs::new())
            .await;
        assert!(result.content[0].text.contains("no scheduled"));
    }
}
