//! CWD-based tool permission callback.
//!
//! Restricts file-writing tools (Edit, Write, `MultiEdit`, `NotebookEdit`) to
//! the agent's working directory and the user data directory. Other tools
//! are allowed by default, except explicitly forbidden ones (Skill, `EnterWorktree`, Task).

use std::path::{Path, PathBuf};

use tracing::debug;

// ---------------------------------------------------------------------------
// Permission result
// ---------------------------------------------------------------------------

/// Result of a permission check.
pub enum PermissionResult {
    Allow,
    Deny(String),
}

// ---------------------------------------------------------------------------
// Permission checker
// ---------------------------------------------------------------------------

/// Configuration for the permission checker.
pub struct PermissionConfig {
    /// Agent's working directory (resolved to canonical path).
    pub allowed_cwd: PathBuf,
    /// User data directory (always writable).
    pub user_data_dir: PathBuf,
    /// Bot source directory.
    pub bot_dir: PathBuf,
    /// Worktrees directory (writable for code agents).
    pub worktrees_dir: Option<PathBuf>,
    /// Additional writable directories for admin agents.
    pub admin_extra_dirs: Vec<PathBuf>,
    /// Whether this is a "code agent" (CWD is within `bot_dir` or worktrees).
    pub is_code_agent: bool,
}

impl PermissionConfig {
    pub fn new(
        cwd: &Path,
        user_data_dir: &Path,
        bot_dir: &Path,
        worktrees_dir: Option<&Path>,
        admin_extra_dirs: Vec<PathBuf>,
    ) -> Self {
        let allowed_cwd = std::fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf());
        let bot_dir = std::fs::canonicalize(bot_dir).unwrap_or_else(|_| bot_dir.to_path_buf());
        let worktrees_dir =
            worktrees_dir.map(|p| std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf()));

        let is_code_agent = starts_with_or_eq(&allowed_cwd, &bot_dir)
            || worktrees_dir
                .as_ref()
                .is_some_and(|wt| starts_with_or_eq(&allowed_cwd, wt));

        Self {
            allowed_cwd,
            user_data_dir: std::fs::canonicalize(user_data_dir)
                .unwrap_or_else(|_| user_data_dir.to_path_buf()),
            bot_dir,
            worktrees_dir,
            admin_extra_dirs,
            is_code_agent,
        }
    }
}

/// Check if `child` equals or is a subdirectory of `parent`.
fn starts_with_or_eq(child: &Path, parent: &Path) -> bool {
    child == parent || child.starts_with(parent)
}

/// Normalize a path by resolving `.` and `..` components lexically (no filesystem access).
/// Returns None if the path escapes the root (more `..` than components).
fn normalize_path(path: &Path) -> PathBuf {
    use std::path::Component;
    let mut components = Vec::new();
    for component in path.components() {
        match component {
            Component::ParentDir => {
                // Only pop if we have a normal component to go back from
                if matches!(components.last(), Some(Component::Normal(_))) {
                    components.pop();
                } else {
                    components.push(component);
                }
            }
            Component::CurDir => {} // skip
            _ => components.push(component),
        }
    }
    components.iter().collect()
}

/// Forbidden tools that don't work in Discord agent mode.
const FORBIDDEN_TOOLS: &[&str] = &["Skill", "EnterWorktree", "Task"];

/// File-writing tools that need path validation.
const WRITE_TOOLS: &[&str] = &["Edit", "Write", "MultiEdit", "NotebookEdit"];

/// Always-allowed tools.
const ALWAYS_ALLOWED: &[&str] = &["TodoWrite", "EnterPlanMode"];

/// Check if a tool call should be allowed for this agent.
pub fn check_permission(
    config: &PermissionConfig,
    tool_name: &str,
    tool_input: &serde_json::Value,
) -> PermissionResult {
    // Forbidden tools
    if FORBIDDEN_TOOLS.contains(&tool_name) {
        return PermissionResult::Deny(format!(
            "{tool_name} is not compatible with Discord-based agent mode. Use text messages instead."
        ));
    }

    // Always allowed
    if ALWAYS_ALLOWED.contains(&tool_name) {
        return PermissionResult::Allow;
    }

    // File-writing tools: validate path
    if WRITE_TOOLS.contains(&tool_name) {
        let path_str = tool_input
            .get("file_path")
            .or_else(|| tool_input.get("notebook_path"))
            .and_then(|v| v.as_str())
            .unwrap_or("");

        if path_str.is_empty() {
            return PermissionResult::Allow; // let the tool itself handle missing path
        }

        // Canonicalize to resolve symlinks and `..` components.
        // If the file doesn't exist yet (Write to new file), canonicalize
        // the parent and append the filename. Final fallback: normalize
        // lexically to catch `..` escapes even when paths don't exist.
        let resolved = std::fs::canonicalize(path_str).unwrap_or_else(|_| {
            let path = PathBuf::from(path_str);
            if let (Some(parent), Some(name)) = (path.parent(), path.file_name()) {
                std::fs::canonicalize(parent)
                    .map_or_else(|_| normalize_path(&path), |p| p.join(name))
            } else {
                normalize_path(&path)
            }
        });

        // Build list of allowed base paths
        let mut bases = vec![&config.allowed_cwd, &config.user_data_dir];
        if config.is_code_agent {
            if let Some(wt) = &config.worktrees_dir {
                bases.push(wt);
            }
            for extra in &config.admin_extra_dirs {
                bases.push(extra);
            }
        }

        for base in &bases {
            if starts_with_or_eq(&resolved, base) {
                debug!(
                    "Permission granted for {} on {} (base: {})",
                    tool_name,
                    path_str,
                    base.display()
                );
                return PermissionResult::Allow;
            }
        }

        return PermissionResult::Deny(format!(
            "Access denied: {} is outside working directory {} and user data {}",
            path_str,
            config.allowed_cwd.display(),
            config.user_data_dir.display()
        ));
    }

    // Default: allow
    PermissionResult::Allow
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn test_config(dir: &Path) -> PermissionConfig {
        let cwd = dir.join("work");
        let user_data = dir.join("user-data");
        let bot_dir = dir.join("bot");
        std::fs::create_dir_all(&cwd).unwrap();
        std::fs::create_dir_all(&user_data).unwrap();
        std::fs::create_dir_all(&bot_dir).unwrap();

        PermissionConfig::new(&cwd, &user_data, &bot_dir, None, vec![])
    }

    #[test]
    fn forbidden_tools_denied() {
        let dir = tempfile::tempdir().unwrap();
        let config = test_config(dir.path());

        for tool in FORBIDDEN_TOOLS {
            match check_permission(&config, tool, &json!({})) {
                PermissionResult::Deny(msg) => {
                    assert!(msg.contains("not compatible"));
                }
                PermissionResult::Allow => panic!("{tool} should be denied"),
            }
        }
    }

    #[test]
    fn always_allowed_tools() {
        let dir = tempfile::tempdir().unwrap();
        let config = test_config(dir.path());

        for tool in ALWAYS_ALLOWED {
            match check_permission(&config, tool, &json!({})) {
                PermissionResult::Allow => {}
                PermissionResult::Deny(msg) => panic!("{tool} should be allowed: {msg}"),
            }
        }
    }

    #[test]
    fn write_to_cwd_allowed() {
        let dir = tempfile::tempdir().unwrap();
        let config = test_config(dir.path());

        let file_path = dir.path().join("work").join("test.txt");
        std::fs::write(&file_path, "test").unwrap();

        let input = json!({"file_path": file_path.to_str().unwrap()});
        match check_permission(&config, "Write", &input) {
            PermissionResult::Allow => {}
            PermissionResult::Deny(msg) => panic!("Should be allowed: {msg}"),
        }
    }

    #[test]
    fn write_outside_cwd_denied() {
        let dir = tempfile::tempdir().unwrap();
        let config = test_config(dir.path());

        let input = json!({"file_path": "/etc/passwd"});
        match check_permission(&config, "Edit", &input) {
            PermissionResult::Deny(_) => {}
            PermissionResult::Allow => panic!("Should be denied"),
        }
    }

    #[test]
    fn write_to_user_data_allowed() {
        let dir = tempfile::tempdir().unwrap();
        let config = test_config(dir.path());

        let file_path = dir.path().join("user-data").join("notes.txt");
        std::fs::write(&file_path, "note").unwrap();

        let input = json!({"file_path": file_path.to_str().unwrap()});
        match check_permission(&config, "Write", &input) {
            PermissionResult::Allow => {}
            PermissionResult::Deny(msg) => panic!("Should be allowed: {msg}"),
        }
    }

    #[test]
    fn unknown_tool_allowed() {
        let dir = tempfile::tempdir().unwrap();
        let config = test_config(dir.path());

        match check_permission(&config, "Bash", &json!({"command": "ls"})) {
            PermissionResult::Allow => {}
            PermissionResult::Deny(msg) => panic!("Bash should be allowed: {msg}"),
        }
    }

    #[test]
    fn code_agent_can_write_worktrees() {
        let dir = tempfile::tempdir().unwrap();
        let bot_dir = dir.path().join("bot");
        let worktrees = dir.path().join("worktrees");
        let cwd = worktrees.join("feature");
        let user_data = dir.path().join("user-data");
        std::fs::create_dir_all(&cwd).unwrap();
        std::fs::create_dir_all(&bot_dir).unwrap();
        std::fs::create_dir_all(&user_data).unwrap();

        let config = PermissionConfig::new(&cwd, &user_data, &bot_dir, Some(&worktrees), vec![]);
        assert!(config.is_code_agent);

        let file_in_wt = worktrees.join("other-branch").join("file.rs");
        std::fs::create_dir_all(file_in_wt.parent().unwrap()).unwrap();
        std::fs::write(&file_in_wt, "code").unwrap();

        let input = json!({"file_path": file_in_wt.to_str().unwrap()});
        match check_permission(&config, "Write", &input) {
            PermissionResult::Allow => {}
            PermissionResult::Deny(msg) => {
                panic!("Code agent should access worktrees: {msg}")
            }
        }
    }

    #[test]
    fn path_traversal_denied() {
        let dir = tempfile::tempdir().unwrap();
        let config = test_config(dir.path());

        // Try to escape CWD via `..` in the path
        let escape_path = format!(
            "{}/../../etc/shadow",
            dir.path().join("work").display()
        );
        let input = json!({"file_path": escape_path});
        match check_permission(&config, "Write", &input) {
            PermissionResult::Deny(_) => {}
            PermissionResult::Allow => panic!("Path traversal should be denied"),
        }
    }
}
