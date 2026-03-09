//! Flowcoder engine helpers — binary resolution, CLI arg building, command discovery.
//!
//! The flowcoder-engine is an external binary that proxies Claude CLI with
//! flowchart command support. We spawn it via procmux with the same transport
//! protocol — the only difference is the binary and a few extra flags.

use std::path::{Path, PathBuf};

use tracing::debug;

// ---------------------------------------------------------------------------
// Binary resolution
// ---------------------------------------------------------------------------

/// Resolve the `flowcoder-engine` binary path from `$PATH`.
pub fn get_engine_binary() -> Option<PathBuf> {
    which::which("flowcoder-engine").ok()
}

/// Return flowchart command search paths.
///
/// Combines: default package commands dir, `$FLOWCODER_SEARCH_PATH` entries,
/// and any extra paths provided.
pub fn get_search_paths(extra: &[&str]) -> Vec<String> {
    let mut paths = Vec::new();

    // Default: check for installed flowcoder_engine package commands
    // The Python version does: <pkg_dir>/examples/commands
    // For the Rust bridge, we rely on FLOWCODER_SEARCH_PATH env var
    // since we don't have access to Python package paths.

    // Environment variable paths
    if let Ok(env_raw) = std::env::var("FLOWCODER_SEARCH_PATH") {
        for p in env_raw.split(':') {
            if !p.is_empty() {
                paths.push(p.to_string());
            }
        }
    }

    // Extra paths
    for p in extra {
        paths.push(p.to_string());
    }

    paths
}

// ---------------------------------------------------------------------------
// CLI arg building
// ---------------------------------------------------------------------------

/// Build CLI args for spawning the flowcoder-engine process.
///
/// The engine binary is used as a prefix, followed by `--search-path` flags,
/// then all the regular Claude CLI flags (which the engine passes through).
pub fn build_engine_cli_args(
    engine_binary: &Path,
    search_paths: &[String],
    claude_args: &[String],
) -> Vec<String> {
    let mut args = vec![engine_binary.to_string_lossy().to_string()];

    for sp in search_paths {
        args.push("--search-path".to_string());
        args.push(sp.clone());
    }

    // Separate engine args from Claude args with `--`
    // The engine passes everything after `--` through to the inner Claude CLI
    if claude_args.len() > 1 {
        args.push("--".to_string());
        args.extend(claude_args[1..].iter().cloned());
    }

    args
}

// ---------------------------------------------------------------------------
// Command discovery
// ---------------------------------------------------------------------------

/// A discovered flowchart command.
#[derive(Debug, Clone)]
pub struct FlowchartCommand {
    pub name: String,
    pub description: String,
}

/// List available flowchart commands by scanning search paths for YAML files.
pub fn list_flowchart_commands() -> Vec<FlowchartCommand> {
    let search_paths = get_search_paths(&[]);
    let mut seen = std::collections::HashSet::new();
    let mut results = Vec::new();

    for commands_dir in &search_paths {
        let dir = Path::new(commands_dir);
        if !dir.is_dir() {
            continue;
        }

        let entries = match std::fs::read_dir(dir) {
            Ok(e) => e,
            Err(_) => continue,
        };

        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_dir() {
                continue;
            }

            let name = match path.file_name().and_then(|n| n.to_str()) {
                Some(n) => n.to_string(),
                None => continue,
            };

            if seen.contains(&name) {
                continue;
            }
            seen.insert(name.clone());

            // Try to read description from command.yaml
            let yaml_path = path.join("command.yaml");
            let alt_yaml_path = path.join("command.yml");
            let description = read_command_description(&yaml_path)
                .or_else(|| read_command_description(&alt_yaml_path))
                .unwrap_or_default();

            debug!("Found flowchart command: {} ({})", name, commands_dir);
            results.push(FlowchartCommand { name, description });
        }
    }

    results.sort_by(|a, b| a.name.cmp(&b.name));
    results
}

/// Read the description field from a command YAML file.
fn read_command_description(path: &Path) -> Option<String> {
    let content = std::fs::read_to_string(path).ok()?;
    // Simple YAML parsing — look for "description:" line
    for line in content.lines() {
        let trimmed = line.trim();
        if let Some(rest) = trimmed.strip_prefix("description:") {
            let desc = rest.trim().trim_matches('"').trim_matches('\'');
            if !desc.is_empty() {
                return Some(desc.to_string());
            }
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn engine_cli_args_basic() {
        let engine = PathBuf::from("/usr/bin/flowcoder-engine");
        let search = vec!["/path/to/commands".to_string()];
        let claude_args = vec![
            "claude".to_string(),
            "--output-format".to_string(),
            "stream-json".to_string(),
            "--print".to_string(),
        ];

        let args = build_engine_cli_args(&engine, &search, &claude_args);

        assert_eq!(args[0], "/usr/bin/flowcoder-engine");
        assert_eq!(args[1], "--search-path");
        assert_eq!(args[2], "/path/to/commands");
        assert_eq!(args[3], "--");
        // Claude args after -- (minus the binary name)
        assert!(args[4..].contains(&"--output-format".to_string()));
        assert!(args[4..].contains(&"--print".to_string()));
    }

    #[test]
    fn engine_cli_args_multiple_search_paths() {
        let engine = PathBuf::from("flowcoder-engine");
        let search = vec!["/a".to_string(), "/b".to_string()];
        let claude_args = vec!["claude".to_string()];

        let args = build_engine_cli_args(&engine, &search, &claude_args);

        assert_eq!(args[1], "--search-path");
        assert_eq!(args[2], "/a");
        assert_eq!(args[3], "--search-path");
        assert_eq!(args[4], "/b");
        // No -- when there are no Claude args to pass
        assert!(!args.contains(&"--".to_string()));
    }

    #[test]
    fn list_commands_empty_when_no_paths() {
        // With no FLOWCODER_SEARCH_PATH set and no extra paths,
        // should return empty (or whatever's in the env)
        let commands = list_flowchart_commands();
        // Just verify it doesn't panic
        let _ = commands.len(); // just verify it doesn't panic
    }
}
