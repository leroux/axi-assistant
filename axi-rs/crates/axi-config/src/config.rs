//! Centralized configuration loaded from environment variables.
//!
//! Leaf module — no project imports. All env vars, paths, constants live here.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// All configuration for the Axi bot, loaded once at startup.
#[derive(Clone)]
pub struct Config {
    // Discord
    pub discord_token: String,
    pub discord_guild_id: u64,
    pub allowed_user_ids: HashSet<u64>,

    // Paths
    pub bot_dir: PathBuf,
    pub bot_worktrees_dir: PathBuf,
    pub axi_user_data: PathBuf,
    pub default_cwd: PathBuf,
    pub log_dir: PathBuf,
    pub bridge_socket_path: PathBuf,
    pub master_session_path: PathBuf,
    pub config_path: PathBuf,
    pub schedules_path: PathBuf,
    pub history_path: PathBuf,
pub rate_limit_history_path: PathBuf,
    pub usage_history_path: PathBuf,
    pub mcp_servers_path: PathBuf,
    pub readme_content_path: PathBuf,

    // Feature flags
    pub flowcoder_enabled: bool,
    pub streaming_discord: bool,
    pub channel_status_enabled: bool,
    pub clean_tool_messages: bool,
    pub show_awaiting_input: bool,

    // Numeric
    pub max_awake_agents: usize,
    pub compact_threshold: f64,
    pub streaming_edit_interval: f64,
    pub query_timeout: Duration,
    pub interrupt_timeout: Duration,
    pub api_error_max_retries: u32,
    pub api_error_base_delay: Duration,
    pub day_boundary_hour: u32,
    pub schedule_timezone: String,

    // Category names
    pub active_category_name: String,
    pub axi_category_name: String,
    pub killed_category_name: String,

    // Agent
    pub master_agent_name: String,
    pub default_agent_type: String,
    pub idle_reminder_thresholds: Vec<Duration>,

    // Allowed CWDs
    pub allowed_cwds: Vec<PathBuf>,
    pub admin_allowed_cwds: Vec<PathBuf>,
}

impl std::fmt::Debug for Config {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Config")
            .field("discord_token", &"[REDACTED]")
            .field("discord_guild_id", &self.discord_guild_id)
            .field("master_agent_name", &self.master_agent_name)
            .field("bot_dir", &self.bot_dir)
            .field("axi_user_data", &self.axi_user_data)
            .finish_non_exhaustive()
    }
}

impl Config {
    /// Load configuration from environment. Call `dotenvy::dotenv()` first.
    pub fn from_env() -> Result<Self, ConfigError> {
        let bot_dir = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        let bot_worktrees_dir = bot_dir
            .parent().map_or_else(|| PathBuf::from("/home/ubuntu/axi-tests"), |p| p.join("axi-tests"));

        let home = dirs_home();
        let axi_user_data = env_path("AXI_USER_DATA", home.join("axi-user-data"));
        let log_dir = bot_dir.join("logs");

        let discord_token = resolve_discord_token(&bot_dir)?;
        let discord_guild_id: u64 = env_required("DISCORD_GUILD_ID")?
            .parse()
            .map_err(|_| ConfigError::InvalidValue("DISCORD_GUILD_ID".into()))?;

        let allowed_user_ids: HashSet<u64> = env_required("ALLOWED_USER_IDS")?
            .split(',')
            .filter_map(|s| s.trim().parse().ok())
            .collect();

        if allowed_user_ids.is_empty() {
            return Err(ConfigError::InvalidValue("ALLOWED_USER_IDS".into()));
        }

        let default_cwd = env_path("DEFAULT_CWD", bot_dir.clone());

        // Feature flags
        let flowcoder_enabled = env_bool("FLOWCODER_ENABLED", true);
        let streaming_discord = env_bool("STREAMING_DISCORD", false);
        let channel_status_enabled = env_bool("CHANNEL_STATUS_ENABLED", false);
        let clean_tool_messages = env_bool("CLEAN_TOOL_MESSAGES", false);
        let show_awaiting_input = env_bool("SHOW_AWAITING_INPUT", false);

        // Numeric
        let max_awake_agents = env_parse("MAX_AWAKE_AGENTS", 7);
        let compact_threshold = env_parse("COMPACT_THRESHOLD", 0.80);
        let streaming_edit_interval = env_parse("STREAMING_EDIT_INTERVAL", 1.5);
        let day_boundary_hour = env_parse("DAY_BOUNDARY_HOUR", 0);
        let schedule_timezone =
            std::env::var("SCHEDULE_TIMEZONE").unwrap_or_else(|_| "UTC".to_string());

        // Allowed CWDs
        let mut allowed_cwds: Vec<PathBuf> = env_path_list("ALLOWED_CWDS");
        allowed_cwds.push(real_path(&axi_user_data));
        allowed_cwds.push(real_path(&bot_dir));
        allowed_cwds.push(real_path(&bot_worktrees_dir));

        let admin_allowed_cwds = env_path_list("ADMIN_ALLOWED_CWDS");
        allowed_cwds.extend(admin_allowed_cwds.iter().cloned());

        Ok(Self {
            discord_token,
            discord_guild_id,
            allowed_user_ids,
            bot_dir: bot_dir.clone(),
            bot_worktrees_dir,
            axi_user_data: axi_user_data.clone(),
            default_cwd,
            log_dir: log_dir.clone(),
            bridge_socket_path: bot_dir.join(".bridge.sock"),
            master_session_path: bot_dir.join(".master_session_id"),
            config_path: bot_dir.join("config.json"),
            schedules_path: axi_user_data.join("schedules.json"),
            history_path: axi_user_data.join("schedule_history.json"),
            rate_limit_history_path: log_dir.join("rate_limit_history.jsonl"),
            usage_history_path: axi_user_data.join("usage_history.jsonl"),
            mcp_servers_path: axi_user_data.join("mcp_servers.json"),
            readme_content_path: bot_dir.join("readme_content.md"),
            flowcoder_enabled,
            streaming_discord,
            channel_status_enabled,
            clean_tool_messages,
            show_awaiting_input,
            max_awake_agents,
            compact_threshold,
            streaming_edit_interval,
            query_timeout: Duration::from_secs(43200),
            interrupt_timeout: Duration::from_secs(15),
            api_error_max_retries: 3,
            api_error_base_delay: Duration::from_secs(5),
            day_boundary_hour,
            schedule_timezone,
            active_category_name: "Active".to_string(),
            axi_category_name: "Axi".to_string(),
            killed_category_name: "Killed".to_string(),
            master_agent_name: "axi-master".to_string(),
            default_agent_type: std::env::var("DEFAULT_AGENT_TYPE")
                .unwrap_or_else(|_| "flowcoder".to_string()),
            idle_reminder_thresholds: vec![
                Duration::from_secs(30 * 60),
                Duration::from_secs(3 * 3600),
                Duration::from_secs(48 * 3600),
            ],
            allowed_cwds,
            admin_allowed_cwds,
        })
    }
}

impl Config {
    /// Create a minimal `Config` suitable for integration tests.
    ///
    /// All paths point into `base_dir`, feature flags use sensible defaults,
    /// and the Discord token / guild ID are placeholders.
    pub fn for_test(base_dir: &Path) -> Self {
        Self {
            discord_token: "test-token".to_string(),
            discord_guild_id: 1,
            allowed_user_ids: HashSet::from([1]),
            bot_dir: base_dir.to_path_buf(),
            bot_worktrees_dir: base_dir.join("worktrees"),
            axi_user_data: base_dir.join("user-data"),
            default_cwd: base_dir.to_path_buf(),
            log_dir: base_dir.join("logs"),
            bridge_socket_path: base_dir.join(".bridge.sock"),
            master_session_path: base_dir.join(".master_session_id"),
            config_path: base_dir.join("config.json"),
            schedules_path: base_dir.join("schedules.json"),
            history_path: base_dir.join("schedule_history.json"),
            rate_limit_history_path: base_dir.join("rate_limit_history.jsonl"),
            usage_history_path: base_dir.join("usage_history.jsonl"),
            mcp_servers_path: base_dir.join("mcp_servers.json"),
            readme_content_path: base_dir.join("readme_content.md"),
            flowcoder_enabled: false,
            streaming_discord: true,
            channel_status_enabled: false,
            clean_tool_messages: false,
            show_awaiting_input: false,
            max_awake_agents: 7,
            compact_threshold: 0.80,
            streaming_edit_interval: 1.5,
            query_timeout: Duration::from_secs(300),
            interrupt_timeout: Duration::from_secs(15),
            api_error_max_retries: 3,
            api_error_base_delay: Duration::from_secs(5),
            day_boundary_hour: 0,
            schedule_timezone: "UTC".to_string(),
            active_category_name: "Active".to_string(),
            axi_category_name: "Axi".to_string(),
            killed_category_name: "Killed".to_string(),
            master_agent_name: "axi-master".to_string(),
            default_agent_type: "flowcoder".to_string(),
            idle_reminder_thresholds: vec![Duration::from_secs(1800)],
            allowed_cwds: vec![base_dir.to_path_buf()],
            admin_allowed_cwds: Vec::new(),
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("required environment variable {0} not set")]
    Missing(String),
    #[error("invalid value for {0}")]
    InvalidValue(String),
    #[error("Discord token resolution failed: {0}")]
    TokenError(String),
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn dirs_home() -> PathBuf {
    std::env::var("HOME").map_or_else(|_| PathBuf::from("/tmp"), PathBuf::from)
}

fn env_required(key: &str) -> Result<String, ConfigError> {
    std::env::var(key).map_err(|_| ConfigError::Missing(key.to_string()))
}

fn env_bool(key: &str, default: bool) -> bool {
    std::env::var(key)
        .map(|v| matches!(v.to_lowercase().as_str(), "1" | "true" | "yes"))
        .unwrap_or(default)
}

fn env_parse<T: std::str::FromStr>(key: &str, default: T) -> T {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn env_path(key: &str, default: PathBuf) -> PathBuf {
    std::env::var(key).map(PathBuf::from).unwrap_or(default)
}

fn env_path_list(key: &str) -> Vec<PathBuf> {
    std::env::var(key)
        .unwrap_or_default()
        .split(':')
        .filter(|s| !s.is_empty())
        .map(|s| real_path(&PathBuf::from(shellexpand::tilde(s).into_owned())))
        .collect()
}

fn real_path(p: &Path) -> PathBuf {
    std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf())
}

/// Resolve Discord token from env or test slot reservation.
fn resolve_discord_token(bot_dir: &Path) -> Result<String, ConfigError> {
    if let Ok(token) = std::env::var("DISCORD_TOKEN") {
        return Ok(token);
    }

    let instance_name = bot_dir
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();

    let config_dir = dirs_home().join(".config/axi");
    let slots_path = config_dir.join(".test-slots.json");
    let config_path = config_dir.join("test-config.json");

    let slots_data = std::fs::read_to_string(&slots_path).map_err(|e| {
        ConfigError::TokenError(format!(
            "DISCORD_TOKEN not set and cannot read {}: {}",
            slots_path.display(),
            e
        ))
    })?;

    let slots: serde_json::Value =
        serde_json::from_str(&slots_data).map_err(|e| ConfigError::TokenError(e.to_string()))?;

    let slot = slots
        .get(&instance_name)
        .ok_or_else(|| {
            ConfigError::TokenError(format!(
                "No slot for '{}' in {}",
                instance_name,
                slots_path.display()
            ))
        })?;

    let token_id = slot
        .get("token_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| ConfigError::TokenError("slot missing token_id".into()))?;

    let config_data = std::fs::read_to_string(&config_path).map_err(|e| {
        ConfigError::TokenError(format!("Cannot read {}: {}", config_path.display(), e))
    })?;

    let config: serde_json::Value =
        serde_json::from_str(&config_data).map_err(|e| ConfigError::TokenError(e.to_string()))?;

    config
        .get("bots")
        .and_then(|b| b.get(token_id))
        .and_then(|b| b.get("token"))
        .and_then(|t| t.as_str())
        .map(ToString::to_string)
        .ok_or_else(|| {
            ConfigError::TokenError(format!("Cannot resolve token for bot '{token_id}'"))
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_bool_defaults() {
        // Should use default when env var not set
        assert!(!env_bool("AXI_TEST_UNSET_BOOL_12345", false));
        assert!(env_bool("AXI_TEST_UNSET_BOOL_12345", true));
    }

    #[test]
    fn env_parse_defaults() {
        assert_eq!(env_parse::<u32>("AXI_TEST_UNSET_NUM_12345", 42), 42);
    }
}
