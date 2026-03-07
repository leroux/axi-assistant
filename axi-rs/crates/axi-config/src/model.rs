//! Model preference management — get/set the current Claude model.

use std::sync::Mutex;

use tracing::warn;

pub const VALID_MODELS: &[&str] = &["haiku", "sonnet", "opus"];

static CONFIG_LOCK: Mutex<()> = Mutex::new(());

/// Get the current model preference.
///
/// `AXI_MODEL` env var takes precedence over config file.
pub fn get_model(config_path: &std::path::Path) -> String {
    if let Ok(env_model) = std::env::var("AXI_MODEL") {
        let lower = env_model.to_lowercase();
        if VALID_MODELS.contains(&lower.as_str()) {
            return lower;
        }
    }

    let _lock = CONFIG_LOCK.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
    match load_config(config_path) {
        Some(config) => config
            .get("model")
            .and_then(|v| v.as_str())
            .unwrap_or("opus")
            .to_string(),
        None => "opus".to_string(),
    }
}

/// Set the model preference. Returns None on success, Some(error) on failure.
pub fn set_model(config_path: &std::path::Path, model: &str) -> Option<String> {
    let lower = model.to_lowercase();
    if !VALID_MODELS.contains(&lower.as_str()) {
        return Some(format!(
            "Invalid model '{}'. Valid options: {}",
            model,
            VALID_MODELS.join(", ")
        ));
    }

    let _lock = CONFIG_LOCK.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
    let mut config = load_config(config_path).unwrap_or_else(|| serde_json::json!({}));
    config["model"] = serde_json::Value::String(lower);
    save_config(config_path, &config);
    None
}

fn load_config(path: &std::path::Path) -> Option<serde_json::Value> {
    match std::fs::read_to_string(path) {
        Ok(data) => match serde_json::from_str(&data) {
            Ok(v) => Some(v),
            Err(e) => {
                warn!("Failed to parse config: {}", e);
                None
            }
        },
        Err(_) => None,
    }
}

fn save_config(path: &std::path::Path, config: &serde_json::Value) {
    match serde_json::to_string_pretty(config) {
        Ok(data) => {
            if let Err(e) = std::fs::write(path, data) {
                warn!("Failed to save config: {}", e);
            }
        }
        Err(e) => warn!("Failed to serialize config: {}", e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::NamedTempFile;

    #[test]
    fn get_model_default() {
        // Non-existent path → default "opus"
        let path = std::path::Path::new("/tmp/axi-test-nonexistent-config.json");
        assert_eq!(get_model(path), "opus");
    }

    #[test]
    fn set_and_get_model() {
        let mut f = NamedTempFile::new().unwrap();
        writeln!(f, "{{}}").unwrap();
        let path = f.path();

        assert!(set_model(path, "sonnet").is_none());
        assert_eq!(get_model(path), "sonnet");

        assert!(set_model(path, "invalid").is_some());
    }
}
