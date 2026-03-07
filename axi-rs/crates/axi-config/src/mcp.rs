//! MCP server config loading from `mcp_servers.json`.

use std::collections::HashMap;
use std::path::Path;

use serde_json::Value;
use tracing::warn;

/// Load named MCP server configs from `mcp_servers.json`.
///
/// Returns a map of {name: `config_dict`} for each requested name found.
/// Unknown names are logged and skipped.
pub fn load_mcp_servers(path: &Path, names: &[String]) -> HashMap<String, Value> {
    if names.is_empty() {
        return HashMap::new();
    }

    let data = if let Ok(d) = std::fs::read_to_string(path) { d } else {
        warn!("mcp_servers.json not found at {}", path.display());
        return HashMap::new();
    };

    let registry: Value = match serde_json::from_str(&data) {
        Ok(v) => v,
        Err(e) => {
            warn!("Failed to parse mcp_servers.json: {}", e);
            return HashMap::new();
        }
    };

    let mut result = HashMap::new();
    for name in names {
        match registry.get(name) {
            Some(config) => {
                result.insert(name.clone(), config.clone());
            }
            None => {
                warn!("MCP server '{}' not found in mcp_servers.json", name);
            }
        }
    }
    result
}
