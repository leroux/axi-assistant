use std::path::PathBuf;

use crate::error::ParseError;
use crate::model::Command;
use crate::parse::parse_command;

/// Information about a discovered command file.
#[derive(Debug, Clone)]
pub struct CommandInfo {
    pub name: String,
    pub path: PathBuf,
    pub description: Option<String>,
}

/// Resolve a command by name from search paths.
///
/// Search order:
/// 1. Current directory: `cwd/commands/<name>.json`, `cwd/<name>.json`
/// 2. Each search path: `path/<name>.json`, `path/commands/<name>.json`
/// 3. Home directory: `~/.flowchart/commands/<name>.json`
pub fn resolve_command(name: &str, search_paths: &[PathBuf]) -> Result<Command, ResolveError> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    // CWD first (matches Python resolver behavior)
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd.join("commands").join(format!("{name}.json")));
        candidates.push(cwd.join(format!("{name}.json")));
    }

    // Search paths
    for sp in search_paths {
        candidates.push(sp.join(format!("{name}.json")));
        candidates.push(sp.join("commands").join(format!("{name}.json")));
    }

    // Home directory
    if let Some(home) = home_dir() {
        candidates.push(home.join(".flowchart").join("commands").join(format!("{name}.json")));
    }

    for candidate in &candidates {
        if candidate.exists() {
            let json = std::fs::read_to_string(candidate).map_err(|e| {
                ResolveError::Io(candidate.clone(), e)
            })?;
            return parse_command(&json).map_err(ResolveError::Parse);
        }
    }

    let searched: Vec<String> = candidates
        .iter()
        .filter_map(|c| c.parent().map(|p| p.display().to_string()))
        .collect::<Vec<_>>();
    // Deduplicate while preserving order
    let mut seen = std::collections::HashSet::new();
    let unique: Vec<&str> = searched
        .iter()
        .filter(|s| seen.insert(s.as_str()))
        .map(String::as_str)
        .collect();

    Err(ResolveError::NotFound {
        name: name.to_owned(),
        searched: unique.join(", "),
    })
}

/// List all discoverable commands from search paths.
pub fn list_commands(search_paths: &[PathBuf]) -> Vec<CommandInfo> {
    let mut dirs: Vec<PathBuf> = Vec::new();

    for sp in search_paths {
        dirs.push(sp.to_owned());
        dirs.push(sp.join("commands"));
    }
    if let Some(home) = home_dir() {
        dirs.push(home.join(".flowchart").join("commands"));
    }

    let mut result = Vec::new();
    let mut seen = std::collections::HashSet::new();

    for dir in &dirs {
        let entries = match std::fs::read_dir(dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().is_some_and(|ext| ext == "json")
                && let Some(stem) = path.file_stem().and_then(|s| s.to_str())
            {
                let name = stem.to_owned();
                if seen.insert(name.clone()) {
                    let description = std::fs::read_to_string(&path)
                        .ok()
                        .and_then(|json| parse_command(&json).ok())
                        .and_then(|cmd| cmd.description);
                    result.push(CommandInfo {
                        name,
                        path: path.clone(),
                        description,
                    });
                }
            }
        }
    }

    result
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

#[derive(Debug, thiserror::Error)]
pub enum ResolveError {
    #[error("Command '{name}' not found. Searched: {searched}")]
    NotFound { name: String, searched: String },

    #[error("Failed to read {0}: {1}")]
    Io(PathBuf, std::io::Error),

    #[error("Failed to parse command: {0}")]
    Parse(#[from] ParseError),
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::*;

    #[test]
    fn resolve_from_search_path() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
        let cmd = resolve_command("story", &[dir]).expect("should resolve story");
        assert_eq!(cmd.name, "story");
    }

    #[test]
    fn resolve_not_found() {
        let err = resolve_command("nonexistent", &[]).unwrap_err();
        assert!(matches!(err, ResolveError::NotFound { .. }));
    }

    #[test]
    fn list_commands_finds_fixture() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
        let cmds = list_commands(&[dir]);
        assert!(cmds.iter().any(|c| c.name == "story"));
    }
}
