//! Todo list rendering and persistence for Discord display.
//!
//! When an agent calls `TodoWrite`, the todo list is formatted with status
//! icons and posted to the agent's Discord channel.

use std::path::{Path, PathBuf};

use serde_json::Value;
use tracing::warn;

// ---------------------------------------------------------------------------
// Status icons
// ---------------------------------------------------------------------------

/// Map todo status to a Discord emoji.
fn status_icon(status: &str) -> &'static str {
    match status {
        "completed" => "\u{2705}",    // green checkmark
        "in_progress" => "\u{1f504}", // arrows (cycle)
        "pending" => "\u{23f3}",      // hourglass
        _ => "\u{2b1c}",             // white square
    }
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

/// Format a todo list for Discord display.
pub fn format_todo_list(todos: &[Value]) -> String {
    if todos.is_empty() {
        return "*Empty todo list*".to_string();
    }

    let lines: Vec<String> = todos
        .iter()
        .map(|item| {
            let status = item
                .get("status")
                .and_then(|v| v.as_str())
                .unwrap_or("pending");
            let content = item
                .get("content")
                .and_then(|v| v.as_str())
                .unwrap_or("???");
            format!("{} {}", status_icon(status), content)
        })
        .collect();

    lines.join("\n")
}

/// Format a full todo list message with header.
pub fn format_todo_message(todos: &[Value]) -> String {
    format!("**Todo List**\n{}", format_todo_list(todos))
}

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

/// Path to the persisted todo state file for an agent.
pub fn todo_path(log_dir: &Path, agent_name: &str) -> PathBuf {
    log_dir.join(format!("{agent_name}.todo.json"))
}

/// Save todo items to disk.
pub fn save_todos(log_dir: &Path, agent_name: &str, todos: &[Value]) {
    let path = todo_path(log_dir, agent_name);
    match serde_json::to_string(todos) {
        Ok(data) => {
            if let Err(e) = std::fs::write(&path, data) {
                warn!("Failed to save todo state for '{}': {}", agent_name, e);
            }
        }
        Err(e) => {
            warn!("Failed to serialize todos for '{}': {}", agent_name, e);
        }
    }
}

/// Load persisted todo items from disk.
pub fn load_todos(log_dir: &Path, agent_name: &str) -> Vec<Value> {
    let path = todo_path(log_dir, agent_name);
    match std::fs::read_to_string(&path) {
        Ok(data) => serde_json::from_str(&data).unwrap_or_default(),
        Err(_) => Vec::new(),
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn format_empty() {
        assert_eq!(format_todo_list(&[]), "*Empty todo list*");
    }

    #[test]
    fn format_with_items() {
        let todos = vec![
            json!({"content": "Build the thing", "status": "completed"}),
            json!({"content": "Test the thing", "status": "in_progress"}),
            json!({"content": "Deploy", "status": "pending"}),
        ];
        let result = format_todo_list(&todos);
        assert!(result.contains("\u{2705} Build the thing"));
        assert!(result.contains("\u{1f504} Test the thing"));
        assert!(result.contains("\u{23f3} Deploy"));
    }

    #[test]
    fn format_message_has_header() {
        let todos = vec![json!({"content": "Task", "status": "pending"})];
        let msg = format_todo_message(&todos);
        assert!(msg.starts_with("**Todo List**"));
    }

    #[test]
    fn persist_and_load() {
        let dir = tempfile::tempdir().unwrap();
        let todos = vec![
            json!({"content": "A", "status": "completed"}),
            json!({"content": "B", "status": "pending"}),
        ];
        save_todos(dir.path(), "test-agent", &todos);

        let loaded = load_todos(dir.path(), "test-agent");
        assert_eq!(loaded.len(), 2);
        assert_eq!(
            loaded[0].get("content").and_then(|v| v.as_str()),
            Some("A")
        );
    }

    #[test]
    fn load_missing_file() {
        let dir = tempfile::tempdir().unwrap();
        let loaded = load_todos(dir.path(), "nonexistent");
        assert!(loaded.is_empty());
    }
}
