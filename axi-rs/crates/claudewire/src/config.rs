//! Claude CLI configuration — single source of truth for CLI flags and env vars.
//!
//! `Config` is a plain data struct. It knows nothing about Axi's preferences —
//! the caller sets every field explicitly. `to_cli_args()` and `to_env()` are
//! the only places that know about Claude CLI flag names and env var names.

use std::collections::HashMap;

use serde_json::Value;

/// SDK version string — matches claude-code-sdk for protocol compatibility.
const SDK_VERSION: &str = "0.1.39";

/// MCP server configuration for the `--mcp-config` flag.
///
/// SDK and external servers are kept separate so callers can selectively
/// include or exclude SDK servers depending on their capabilities.
#[derive(Debug, Clone, Default)]
pub struct McpServers {
    pub external: Option<Value>,
    pub sdk: Option<Value>,
}

/// All the data needed to invoke a Claude CLI process.
///
/// This is a plain data bag — no policy, no smart defaults.
/// The caller (bridge, flowcoder, tests) sets every field it cares about.
/// `Default` gives you an empty config (all None/false/empty).
#[derive(Debug, Clone, Default)]
pub struct Config {
    pub model: String,
    /// Appended to Claude's default prompt via `--append-system-prompt`.
    pub append_system_prompt: Option<String>,
    pub permission_mode: String,
    pub setting_sources: Vec<String>,
    pub mcp_servers: McpServers,
    pub disallowed_tools: Vec<String>,
    pub allowed_tools: Vec<String>,
    pub max_thinking_tokens: Option<u32>,
    pub effort: Option<String>,
    pub sandbox_enabled: bool,
    pub auto_allow_bash_if_sandboxed: bool,
    pub resume: Option<String>,
    pub include_partial_messages: bool,
    pub verbose: bool,
    pub debug_to_stderr: bool,
    /// Use `--print` mode (non-interactive pipe mode). Required for standalone
    /// engine binaries that don't use the SDK entrypoint.
    pub print_mode: bool,
    /// Re-emit user messages from stdin back on stdout for acknowledgment.
    /// Only works with `--print` + stream-json.
    pub replay_user_messages: bool,
}

impl Config {
    /// Build CLI argv for spawning the Claude CLI process.
    ///
    /// This is THE place that knows about Claude CLI flag names.
    /// Returns args starting with "claude" as argv[0].
    pub fn to_cli_args(&self) -> Vec<String> {
        let mut args = vec!["claude".into()];

        if self.print_mode {
            args.push("--print".into());
        }

        args.extend([
            "--output-format".into(),
            "stream-json".into(),
            "--input-format".into(),
            "stream-json".into(),
        ]);

        if self.replay_user_messages {
            args.push("--replay-user-messages".into());
        }

        if self.verbose {
            args.push("--verbose".into());
        }

        if self.include_partial_messages {
            args.push("--include-partial-messages".into());
        }

        if self.debug_to_stderr {
            args.push("--debug-to-stderr".into());
        }

        // Route permission prompts through the control protocol (stdio)
        // so the bot can auto-approve or ask the user via Discord.
        args.extend(["--permission-prompt-tool".into(), "stdio".into()]);

        if !self.model.is_empty() {
            args.extend(["--model".into(), self.model.clone()]);
        }

        if !self.setting_sources.is_empty() {
            args.extend([
                "--setting-sources".into(),
                self.setting_sources.join(","),
            ]);
        }

        if !self.permission_mode.is_empty() {
            args.extend(["--permission-mode".into(), self.permission_mode.clone()]);
        }

        if let Some(prompt) = &self.append_system_prompt
            && !prompt.is_empty()
        {
            args.extend(["--append-system-prompt".into(), prompt.clone()]);
        }

        // MCP servers — merge SDK + external into one --mcp-config
        {
            let mut merged = serde_json::Map::new();

            if let Some(sdk) = &self.mcp_servers.sdk
                && let Some(obj) = sdk.as_object()
            {
                for (k, v) in obj {
                    merged.insert(k.clone(), v.clone());
                }
            }

            if let Some(ext) = &self.mcp_servers.external
                && let Some(obj) = ext.as_object()
            {
                for (k, v) in obj {
                    merged.insert(k.clone(), v.clone());
                }
            }

            if !merged.is_empty() {
                let config = serde_json::json!({"mcpServers": merged});
                if let Ok(json) = serde_json::to_string(&config) {
                    args.extend(["--mcp-config".into(), json]);
                }
            }
        }

        if !self.disallowed_tools.is_empty() {
            args.extend([
                "--disallowed-tools".into(),
                self.disallowed_tools.join(","),
            ]);
        }

        if !self.allowed_tools.is_empty() {
            args.extend(["--allowedTools".into(), self.allowed_tools.join(",")]);
        }

        if let Some(tokens) = self.max_thinking_tokens {
            args.extend(["--max-thinking-tokens".into(), tokens.to_string()]);
        }

        if let Some(effort) = &self.effort {
            args.extend(["--effort".into(), effort.clone()]);
        }

        // Sandbox config → --settings JSON
        if self.sandbox_enabled {
            let settings = serde_json::json!({
                "sandbox": {
                    "enabled": true,
                    "autoAllowBashIfSandboxed": self.auto_allow_bash_if_sandboxed,
                }
            });
            if let Ok(json) = serde_json::to_string(&settings) {
                args.extend(["--settings".into(), json]);
            }
        }

        if let Some(session_id) = &self.resume {
            args.extend(["--resume".into(), session_id.clone()]);
        }

        args
    }

    /// Build environment variables for the Claude CLI process.
    ///
    /// This is THE place that knows about SDK env var names.
    /// Sets what claude-code-sdk's `SubprocessCLITransport.connect()` sets.
    pub fn to_env(&self) -> HashMap<String, String> {
        let mut env = HashMap::new();

        for key in &[
            "ANTHROPIC_API_KEY",
            "HOME",
            "PATH",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "NODE_PATH",
            "TERM",
        ] {
            if let Ok(val) = std::env::var(key) {
                env.insert(key.to_string(), val);
            }
        }

        // SDK control protocol — without these, Claude CLI auto-denies
        // tool permissions in pipe mode.
        env.insert("CLAUDE_CODE_ENTRYPOINT".into(), "sdk-py".into());
        env.insert("CLAUDE_AGENT_SDK_VERSION".into(), SDK_VERSION.into());

        // Disable internal compaction prompts
        env.insert("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE".into(), "100".into());

        env
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_config_is_empty() {
        let cfg = Config::default();
        assert!(cfg.model.is_empty());
        assert!(cfg.permission_mode.is_empty());
        assert!(cfg.append_system_prompt.is_none());
        assert!(cfg.max_thinking_tokens.is_none());
        assert!(cfg.effort.is_none());
        assert!(!cfg.sandbox_enabled);
        assert!(!cfg.verbose);
        assert!(!cfg.debug_to_stderr);
        assert!(cfg.disallowed_tools.is_empty());
    }

    #[test]
    fn empty_config_produces_minimal_args() {
        let args = Config::default().to_cli_args();
        assert_eq!(args[0], "claude");
        assert!(args.contains(&"--output-format".into()));
        assert!(args.contains(&"--input-format".into()));
        // Nothing optional should be present
        assert!(!args.contains(&"--verbose".into()));
        assert!(!args.contains(&"--model".into()));
        assert!(!args.contains(&"--effort".into()));
        assert!(!args.contains(&"--settings".into()));
        assert!(!args.contains(&"--print".into()));
    }

    #[test]
    fn model_flag() {
        let cfg = Config { model: "opus".into(), ..Default::default() };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--model").unwrap();
        assert_eq!(args[idx + 1], "opus");
    }

    #[test]
    fn empty_model_omitted() {
        let args = Config::default().to_cli_args();
        assert!(!args.contains(&"--model".into()));
    }

    #[test]
    fn append_system_prompt() {
        let cfg = Config {
            append_system_prompt: Some("You are Axi.".into()),
            ..Default::default()
        };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--append-system-prompt").unwrap();
        assert_eq!(args[idx + 1], "You are Axi.");
    }

    #[test]
    fn empty_append_prompt_omitted() {
        let cfg = Config {
            append_system_prompt: Some(String::new()),
            ..Default::default()
        };
        let args = cfg.to_cli_args();
        assert!(!args.contains(&"--append-system-prompt".into()));
    }

    #[test]
    fn no_prompt_omitted() {
        let args = Config::default().to_cli_args();
        assert!(!args.contains(&"--append-system-prompt".into()));
        assert!(!args.contains(&"--system-prompt".into()));
    }

    #[test]
    fn permission_mode() {
        let cfg = Config { permission_mode: "plan".into(), ..Default::default() };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--permission-mode").unwrap();
        assert_eq!(args[idx + 1], "plan");
    }

    #[test]
    fn empty_permission_mode_omitted() {
        let args = Config::default().to_cli_args();
        assert!(!args.contains(&"--permission-mode".into()));
    }

    #[test]
    fn setting_sources() {
        let cfg = Config {
            setting_sources: vec!["local".into(), "project".into()],
            ..Default::default()
        };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--setting-sources").unwrap();
        assert_eq!(args[idx + 1], "local,project");
    }

    #[test]
    fn disallowed_tools() {
        let cfg = Config {
            disallowed_tools: vec!["Task".into(), "WebSearch".into()],
            ..Default::default()
        };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--disallowed-tools").unwrap();
        assert_eq!(args[idx + 1], "Task,WebSearch");
    }

    #[test]
    fn allowed_tools() {
        let cfg = Config {
            allowed_tools: vec!["Read".into(), "Bash".into()],
            ..Default::default()
        };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--allowedTools").unwrap();
        assert_eq!(args[idx + 1], "Read,Bash");
    }

    #[test]
    fn thinking_tokens() {
        let cfg = Config { max_thinking_tokens: Some(128_000), ..Default::default() };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--max-thinking-tokens").unwrap();
        assert_eq!(args[idx + 1], "128000");
    }

    #[test]
    fn effort() {
        let cfg = Config { effort: Some("high".into()), ..Default::default() };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--effort").unwrap();
        assert_eq!(args[idx + 1], "high");
    }

    #[test]
    fn sandbox_settings() {
        let cfg = Config {
            sandbox_enabled: true,
            auto_allow_bash_if_sandboxed: true,
            ..Default::default()
        };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--settings").unwrap();
        let settings: Value = serde_json::from_str(&args[idx + 1]).unwrap();
        assert_eq!(settings["sandbox"]["enabled"], true);
        assert_eq!(settings["sandbox"]["autoAllowBashIfSandboxed"], true);
    }

    #[test]
    fn sandbox_disabled_omits_settings() {
        let args = Config::default().to_cli_args();
        assert!(!args.contains(&"--settings".into()));
    }

    #[test]
    fn resume() {
        let cfg = Config { resume: Some("sess-abc".into()), ..Default::default() };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--resume").unwrap();
        assert_eq!(args[idx + 1], "sess-abc");
    }

    #[test]
    fn verbose_and_debug() {
        let cfg = Config { verbose: true, debug_to_stderr: true, ..Default::default() };
        let args = cfg.to_cli_args();
        assert!(args.contains(&"--verbose".into()));
        assert!(args.contains(&"--debug-to-stderr".into()));
    }

    #[test]
    fn include_partial_messages() {
        let cfg = Config { include_partial_messages: true, ..Default::default() };
        let args = cfg.to_cli_args();
        assert!(args.contains(&"--include-partial-messages".into()));
    }

    #[test]
    fn no_print_flag() {
        // SDK doesn't use --print; --input-format stream-json is sufficient
        let cfg = Config { verbose: true, include_partial_messages: true, ..Default::default() };
        let args = cfg.to_cli_args();
        assert!(!args.contains(&"--print".into()));
        assert!(!args.contains(&"-p".into()));
    }

    #[test]
    fn mcp_external_only() {
        let cfg = Config {
            mcp_servers: McpServers {
                external: Some(serde_json::json!({"myserver": {"command": "node"}})),
                sdk: None,
            },
            ..Default::default()
        };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--mcp-config").unwrap();
        let mcp: Value = serde_json::from_str(&args[idx + 1]).unwrap();
        assert!(mcp["mcpServers"]["myserver"].is_object());
    }

    #[test]
    fn mcp_sdk_only() {
        let cfg = Config {
            mcp_servers: McpServers {
                external: None,
                sdk: Some(serde_json::json!({"utils": {"type": "sdk"}})),
            },
            ..Default::default()
        };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--mcp-config").unwrap();
        let mcp: Value = serde_json::from_str(&args[idx + 1]).unwrap();
        assert_eq!(mcp["mcpServers"]["utils"]["type"], "sdk");
    }

    #[test]
    fn mcp_merged() {
        let cfg = Config {
            mcp_servers: McpServers {
                external: Some(serde_json::json!({"ext": {"command": "node"}})),
                sdk: Some(serde_json::json!({"utils": {"type": "sdk"}})),
            },
            ..Default::default()
        };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--mcp-config").unwrap();
        let mcp: Value = serde_json::from_str(&args[idx + 1]).unwrap();
        assert!(mcp["mcpServers"]["ext"].is_object());
        assert!(mcp["mcpServers"]["utils"].is_object());
    }

    #[test]
    fn mcp_external_overrides_sdk_same_name() {
        let cfg = Config {
            mcp_servers: McpServers {
                sdk: Some(serde_json::json!({"shared": {"type": "sdk"}})),
                external: Some(serde_json::json!({"shared": {"command": "custom"}})),
            },
            ..Default::default()
        };
        let args = cfg.to_cli_args();
        let idx = args.iter().position(|a| a == "--mcp-config").unwrap();
        let mcp: Value = serde_json::from_str(&args[idx + 1]).unwrap();
        assert_eq!(mcp["mcpServers"]["shared"]["command"], "custom");
        assert!(mcp["mcpServers"]["shared"].get("type").is_none());
    }

    #[test]
    fn no_mcp_omitted() {
        let args = Config::default().to_cli_args();
        assert!(!args.contains(&"--mcp-config".into()));
    }

    #[test]
    fn to_env_sdk_vars() {
        let env = Config::default().to_env();
        assert_eq!(env.get("CLAUDE_CODE_ENTRYPOINT").unwrap(), "sdk-py");
        assert_eq!(env.get("CLAUDE_AGENT_SDK_VERSION").unwrap(), SDK_VERSION);
        assert_eq!(env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE").unwrap(), "100");
    }

    #[test]
    fn to_env_passes_system_vars() {
        let env = Config::default().to_env();
        assert!(env.contains_key("HOME"));
        assert!(env.contains_key("PATH"));
    }

    #[test]
    fn to_env_no_claudecode() {
        let env = Config::default().to_env();
        assert!(!env.contains_key("CLAUDECODE"));
    }

    #[test]
    fn full_axi_config_round_trip() {
        // Simulate what the bridge actually builds — every field explicit
        let cfg = Config {
            model: "sonnet".into(),
            append_system_prompt: Some("You are Axi.".into()),
            permission_mode: "default".into(),
            setting_sources: vec!["local".into()],
            disallowed_tools: vec!["Task".into()],
            max_thinking_tokens: Some(128_000),
            effort: Some("high".into()),
            sandbox_enabled: true,
            auto_allow_bash_if_sandboxed: true,
            verbose: true,
            include_partial_messages: true,
            debug_to_stderr: true,
            resume: Some("sess-123".into()),
            ..Default::default()
        };
        let args = cfg.to_cli_args();

        assert!(args.contains(&"--output-format".into()));
        assert!(args.contains(&"--input-format".into()));
        assert!(args.contains(&"--verbose".into()));
        assert!(args.contains(&"--include-partial-messages".into()));
        assert!(args.contains(&"--debug-to-stderr".into()));
        assert!(args.contains(&"--model".into()));
        assert!(args.contains(&"--setting-sources".into()));
        assert!(args.contains(&"--permission-mode".into()));
        assert!(args.contains(&"--append-system-prompt".into()));
        assert!(args.contains(&"--disallowed-tools".into()));
        assert!(args.contains(&"--max-thinking-tokens".into()));
        assert!(args.contains(&"--effort".into()));
        assert!(args.contains(&"--settings".into()));
        assert!(args.contains(&"--resume".into()));
        assert!(!args.contains(&"--print".into()));
    }
}
