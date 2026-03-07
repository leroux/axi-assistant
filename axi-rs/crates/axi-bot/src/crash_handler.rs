//! Crash analysis on startup — reads marker files left by the supervisor
//! and generates prompts for a crash-handler agent.

use std::path::Path;

use serde_json::Value;
use tracing::{info, warn};

/// Crash marker filename (written by supervisor on crash/rollback).
const CRASH_ANALYSIS_MARKER: &str = ".crash_analysis";

/// Read and consume a JSON marker file.
///
/// Returns the parsed value if the file exists, then deletes it.
pub fn consume_marker(dir: &Path) -> Option<Value> {
    let path = dir.join(CRASH_ANALYSIS_MARKER);
    let data = match std::fs::read_to_string(&path) {
        Ok(d) => d,
        Err(_) => return None,
    };
    let value: Value = match serde_json::from_str(&data) {
        Ok(v) => v,
        Err(e) => {
            warn!("Failed to parse crash marker {}: {}", path.display(), e);
            // Still remove it to prevent re-processing
            let _ = std::fs::remove_file(&path);
            return None;
        }
    };
    let _ = std::fs::remove_file(&path);
    info!("Consumed crash analysis marker: {}", path.display());
    Some(value)
}

/// Determine if a crash marker represents a rollback.
pub fn is_rollback(marker: &Value) -> bool {
    marker.get("rollback_details").is_some() || marker.get("pre_launch_commit").is_some()
}

/// Generate the Discord notification message for a crash.
pub fn crash_notification(marker: &Value, enable_handler: bool) -> String {
    if is_rollback(marker) {
        let exit_code = marker
            .get("exit_code")
            .and_then(|v| v.as_i64())
            .map(|v| v.to_string())
            .unwrap_or_else(|| "unknown".to_string());
        let uptime = marker
            .get("uptime_seconds")
            .and_then(|v| v.as_u64())
            .map(|v| v.to_string())
            .unwrap_or_else(|| "?".to_string());
        let timestamp = marker
            .get("timestamp")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");
        let details = marker
            .get("rollback_details")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let pre_commit = marker
            .get("pre_launch_commit")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let crashed_commit = marker
            .get("crashed_commit")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        let mut lines = vec![
            "Ow... I think I just blacked out. What happened?\n".to_string(),
            format!(
                "*System:* **Startup crash detected — auto-rollback performed.**\n\
                 Axi crashed after {}s of uptime (exit code {}) at {}.",
                uptime, exit_code, timestamp
            ),
        ];
        if !details.is_empty() {
            lines.push(format!("Actions taken: {}.", details));
        }
        if !pre_commit.is_empty()
            && !crashed_commit.is_empty()
            && pre_commit != crashed_commit
        {
            lines.push(format!(
                "Reverted from `{}` to `{}`.",
                &crashed_commit[..7.min(crashed_commit.len())],
                &pre_commit[..7.min(pre_commit.len())]
            ));
            lines.push("Reverted commits are still in the reflog: `git reflog`".to_string());
        }
        if details.contains("stashed") {
            lines.push(
                "Stashed changes: `git stash list` / `git stash show -p` / `git stash pop`"
                    .to_string(),
            );
        }
        if enable_handler {
            lines.push("Spawning crash analysis agent...".to_string());
        }
        lines.join("\n")
    } else {
        let exit_code = marker
            .get("exit_code")
            .and_then(|v| v.as_i64())
            .map(|v| v.to_string())
            .unwrap_or_else(|| "unknown".to_string());
        let uptime = marker
            .get("uptime_seconds")
            .and_then(|v| v.as_u64())
            .map(|v| v.to_string())
            .unwrap_or_else(|| "?".to_string());
        let timestamp = marker
            .get("timestamp")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");

        let mut msg = format!(
            "Ow... I think I just blacked out for a second there. What happened?\n\n\
             *System:* **Runtime crash detected.**\n\
             Axi crashed after {}s of uptime (exit code {}) at {}.",
            uptime, exit_code, timestamp
        );
        if enable_handler {
            msg.push_str("\nSpawning crash analysis agent...");
        }
        msg
    }
}

/// Generate the prompt for a crash analysis agent.
pub fn crash_analysis_prompt(marker: &Value) -> String {
    let exit_code = marker
        .get("exit_code")
        .and_then(|v| v.as_i64())
        .map(|v| v.to_string())
        .unwrap_or_else(|| "unknown".to_string());
    let uptime = marker
        .get("uptime_seconds")
        .and_then(|v| v.as_u64())
        .map(|v| v.to_string())
        .unwrap_or_else(|| "?".to_string());
    let timestamp = marker
        .get("timestamp")
        .and_then(|v| v.as_str())
        .unwrap_or("unknown");
    let crash_log = marker
        .get("crash_log")
        .and_then(|v| v.as_str())
        .unwrap_or("(no crash log available)");

    if is_rollback(marker) {
        let details = marker
            .get("rollback_details")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        let pre_commit = marker
            .get("pre_launch_commit")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let crashed_commit = marker
            .get("crashed_commit")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        let mut rollback_ctx = String::new();
        if !details.is_empty() {
            rollback_ctx.push_str(&format!("- Rollback actions: {}\n", details));
        }
        if !pre_commit.is_empty()
            && !crashed_commit.is_empty()
            && pre_commit != crashed_commit
        {
            rollback_ctx.push_str(&format!(
                "- Reverted from commit {} to {}\n",
                &crashed_commit[..7.min(crashed_commit.len())],
                &pre_commit[..7.min(pre_commit.len())]
            ));
        }
        if details.contains("stashed") {
            rollback_ctx
                .push_str("- Uncommitted changes were stashed (see `git stash list`)\n");
        }

        format!(
            "The Discord bot crashed on startup and was auto-rolled-back. \
             Analyze the crash and create a plan to fix it.\n\
             \n\
             ## Crash Details\n\
             - Exit code: {exit_code}\n\
             - Uptime before crash: {uptime} seconds\n\
             - Timestamp: {timestamp}\n\
             {rollback_ctx}\
             \n\
             ## Crash Log (last 200 lines of output before crash)\n\
             ```\n\
             {crash_log}\n\
             ```\n\
             \n\
             ## Instructions\n\
             1. Analyze the traceback and error messages to identify the root cause.\n\
             2. Examine the relevant source code in this project directory.\n\
             3. Check the rolled-back commits or stashed changes (if any) to understand what \
             code changes caused the crash.\n\
             4. Create a clear, detailed plan to fix the issue. Describe exactly which files \
             need to change and what the changes should be.\n\
             5. Do NOT apply any fixes yourself. Only produce the analysis and plan.\n"
        )
    } else {
        format!(
            "The Discord bot crashed at runtime. Analyze the crash and create a plan to fix it.\n\
             \n\
             ## Crash Details\n\
             - Exit code: {exit_code}\n\
             - Uptime before crash: {uptime} seconds\n\
             - Timestamp: {timestamp}\n\
             \n\
             ## Crash Log (last 200 lines of output)\n\
             ```\n\
             {crash_log}\n\
             ```\n\
             \n\
             ## Instructions\n\
             1. Analyze the traceback and error messages to identify the root cause.\n\
             2. Examine the relevant source code in this project directory.\n\
             3. Create a clear, detailed plan to fix the issue. Describe exactly which files \
             need to change and what the changes should be.\n\
             4. Do NOT apply any fixes yourself. Only produce the analysis and plan.\n"
        )
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
    fn consume_and_delete_marker() {
        let dir = tempfile::tempdir().unwrap();
        let marker_path = dir.path().join(CRASH_ANALYSIS_MARKER);
        std::fs::write(
            &marker_path,
            r#"{"exit_code": 1, "uptime_seconds": 42, "crash_log": "panic!"}"#,
        )
        .unwrap();

        let result = consume_marker(dir.path());
        assert!(result.is_some());
        assert_eq!(result.unwrap()["exit_code"], 1);
        assert!(!marker_path.exists());
    }

    #[test]
    fn consume_missing_marker() {
        let dir = tempfile::tempdir().unwrap();
        assert!(consume_marker(dir.path()).is_none());
    }

    #[test]
    fn rollback_detection() {
        let rollback = json!({
            "exit_code": 1,
            "rollback_details": "reset --hard abc1234",
            "pre_launch_commit": "abc1234",
            "crashed_commit": "def5678"
        });
        assert!(is_rollback(&rollback));

        let crash = json!({"exit_code": 1, "uptime_seconds": 100});
        assert!(!is_rollback(&crash));
    }

    #[test]
    fn runtime_crash_notification() {
        let marker = json!({
            "exit_code": 1,
            "uptime_seconds": 300,
            "timestamp": "2026-03-07T09:00:00Z"
        });
        let msg = crash_notification(&marker, false);
        assert!(msg.contains("Runtime crash detected"));
        assert!(msg.contains("300s"));
        assert!(!msg.contains("crash analysis agent"));

        let msg_with_handler = crash_notification(&marker, true);
        assert!(msg_with_handler.contains("crash analysis agent"));
    }

    #[test]
    fn rollback_notification() {
        let marker = json!({
            "exit_code": 1,
            "uptime_seconds": 5,
            "timestamp": "2026-03-07T09:00:00Z",
            "rollback_details": "reset --hard; stashed changes",
            "pre_launch_commit": "abc1234",
            "crashed_commit": "def5678"
        });
        let msg = crash_notification(&marker, true);
        assert!(msg.contains("auto-rollback"));
        assert!(msg.contains("def5678"));
        assert!(msg.contains("abc1234"));
        assert!(msg.contains("stash"));
        assert!(msg.contains("crash analysis agent"));
    }

    #[test]
    fn crash_prompt_runtime() {
        let marker = json!({
            "exit_code": -11,
            "uptime_seconds": 500,
            "timestamp": "2026-03-07T09:00:00Z",
            "crash_log": "thread panicked at main.rs:42"
        });
        let prompt = crash_analysis_prompt(&marker);
        assert!(prompt.contains("crashed at runtime"));
        assert!(prompt.contains("-11"));
        assert!(prompt.contains("thread panicked"));
        assert!(prompt.contains("Do NOT apply any fixes"));
    }

    #[test]
    fn crash_prompt_rollback() {
        let marker = json!({
            "exit_code": 1,
            "uptime_seconds": 3,
            "timestamp": "2026-03-07T09:00:00Z",
            "crash_log": "error: cannot find module",
            "rollback_details": "reset --hard abc1234",
            "pre_launch_commit": "abc1234",
            "crashed_commit": "def5678"
        });
        let prompt = crash_analysis_prompt(&marker);
        assert!(prompt.contains("auto-rolled-back"));
        assert!(prompt.contains("Reverted from commit"));
        assert!(prompt.contains("cannot find module"));
    }
}
