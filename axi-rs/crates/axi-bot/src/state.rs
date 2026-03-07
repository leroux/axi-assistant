//! Bot-local state shared across event handlers.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Instant;

use serenity::all::ChannelId;
use serenity::prelude::TypeMapKey;
use serde_json::{json, Value};
use tokio::sync::RwLock;
use tracing::warn;

use axi_config::{Config, DiscordClient};
use axi_hub::AgentHub;
use axi_mcp::tools::ToolContext;

use crate::bridge;
use crate::channels::{self, GuildInfrastructure};
use crate::prompts::PromptBuilder;

/// A pending permission question waiting for user response.
pub struct PendingQuestion {
    pub agent_name: String,
    pub channel_id: u64,
    pub message_id: String,
    pub request_id: String,
    pub question_type: QuestionType,
    pub options: Vec<String>,
    pub sender: tokio::sync::oneshot::Sender<QuestionAnswer>,
}

pub enum QuestionType {
    AskUser,
    PlanApproval,
}

pub enum QuestionAnswer {
    /// User selected an option (0-indexed)
    Selection(usize),
    /// User typed a custom text response
    Text(String),
    /// Plan approved
    Approved,
    /// Plan denied
    Denied,
}

/// Bot state stored in serenity's `TypeMap`.
pub struct BotState {
    pub config: Config,
    pub discord_client: DiscordClient,
    pub startup_complete: std::sync::atomic::AtomicBool,
    pub start_time: Instant,

    /// `AgentHub` — initialized during `on_ready`.
    pub hub: RwLock<Option<Arc<AgentHub>>>,

    /// Channel ID → agent name mapping, rebuilt on startup and updated on spawn/kill.
    pub channel_map: RwLock<HashMap<ChannelId, String>>,

    /// Agent name → channel ID (reverse lookup).
    pub agent_channels: RwLock<HashMap<String, ChannelId>>,

    /// Guild infrastructure (categories), set during `on_ready`.
    pub infra: RwLock<Option<GuildInfrastructure>>,

    /// Per-agent `BridgeTransport` storage.
    pub transports: bridge::TransportMap,

    /// Prompt builder for constructing system prompts.
    pub prompt_builder: PromptBuilder,

    /// Pending permission questions keyed by `message_id`.
    pub pending_questions: tokio::sync::Mutex<HashMap<String, PendingQuestion>>,
}

impl BotState {
    pub fn new(config: Config, discord_client: DiscordClient) -> Self {
        let prompt_builder = PromptBuilder::new(
            &config.bot_dir,
            &config.axi_user_data,
            Some(config.bot_worktrees_dir.as_path()),
        );
        Self {
            config,
            discord_client,
            startup_complete: std::sync::atomic::AtomicBool::new(false),
            start_time: Instant::now(),
            hub: RwLock::new(None),
            channel_map: RwLock::new(HashMap::new()),
            agent_channels: RwLock::new(HashMap::new()),
            infra: RwLock::new(None),
            transports: bridge::new_transport_map(),
            prompt_builder,
            pending_questions: tokio::sync::Mutex::new(HashMap::new()),
        }
    }

    /// Look up which agent owns a channel.
    pub async fn agent_for_channel(&self, channel_id: ChannelId) -> Option<String> {
        let map = self.channel_map.read().await;
        map.get(&channel_id).cloned()
    }

    /// Look up the channel for an agent.
    pub async fn channel_for_agent(&self, agent_name: &str) -> Option<ChannelId> {
        let map = self.agent_channels.read().await;
        map.get(agent_name).copied()
    }

    /// Register a channel-to-agent mapping.
    pub async fn register_channel(&self, channel_id: ChannelId, agent_name: &str) {
        let mut ch_map = self.channel_map.write().await;
        ch_map.insert(channel_id, agent_name.to_string());
        drop(ch_map);

        let mut ag_map = self.agent_channels.write().await;
        ag_map.insert(agent_name.to_string(), channel_id);
    }

    /// Remove a channel-to-agent mapping.
    pub async fn unregister_channel(&self, agent_name: &str) {
        let channel_id = {
            let ag_map = self.agent_channels.read().await;
            ag_map.get(agent_name).copied()
        };

        if let Some(ch_id) = channel_id {
            let mut ch_map = self.channel_map.write().await;
            ch_map.remove(&ch_id);
        }

        let mut ag_map = self.agent_channels.write().await;
        ag_map.remove(agent_name);
    }

    /// Get the hub, panics if not initialized.
    pub async fn hub(&self) -> Arc<AgentHub> {
        self.hub
            .read()
            .await
            .clone()
            .expect("AgentHub not initialized")
    }
}

// ---------------------------------------------------------------------------
// BotToolContext — wrapper providing ToolContext via Arc<BotState>
// ---------------------------------------------------------------------------

/// Wrapper that holds `Arc<BotState>` so `stream_handler()` can create
/// closures that keep the state alive.
pub struct BotToolContext {
    pub state: Arc<BotState>,
}

impl BotToolContext {
    pub const fn new(state: Arc<BotState>) -> Self {
        Self { state }
    }
}

impl ToolContext for BotToolContext {
    fn channel_for_agent(
        &self,
        agent_name: &str,
    ) -> Pin<Box<dyn Future<Output = Option<u64>> + Send>> {
        let name = agent_name.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            let map = state.agent_channels.read().await;
            map.get(&name).map(|ch| ch.get())
        })
    }

    fn set_channel_status(
        &self,
        agent_name: &str,
        status: &str,
    ) -> Pin<Box<dyn Future<Output = ()> + Send>> {
        let name = agent_name.to_string();
        let status = status.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            let channel_id = {
                let map = state.agent_channels.read().await;
                map.get(&name).map(|ch| ch.get())
            };
            if let Some(ch_id) = channel_id {
                let normalized = channels::normalize_channel_name(&name);
                let new_name = if let Some(emoji) = channels::status_emoji(&status) {
                    format!("{emoji}-{normalized}")
                } else {
                    normalized
                };
                if let Err(e) = state.discord_client.edit_channel_name(ch_id, &new_name).await {
                    warn!("Failed to set channel status for {}: {}", name, e);
                }
            }
        })
    }

    fn clear_channel_status(
        &self,
        agent_name: &str,
    ) -> Pin<Box<dyn Future<Output = ()> + Send>> {
        let name = agent_name.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            let channel_id = {
                let map = state.agent_channels.read().await;
                map.get(&name).map(|ch| ch.get())
            };
            if let Some(ch_id) = channel_id {
                let normalized = channels::normalize_channel_name(&name);
                if let Err(e) = state.discord_client.edit_channel_name(ch_id, &normalized).await {
                    warn!("Failed to clear channel status for {}: {}", name, e);
                }
            }
        })
    }

    fn create_agent_channel(
        &self,
        agent_name: &str,
    ) -> Pin<Box<dyn Future<Output = Result<u64, String>> + Send>> {
        let name = agent_name.to_string();
        let state = self.state.clone();
        Box::pin(async move {
            let normalized = channels::normalize_channel_name(&name);

            // Get the active category
            let category_id = {
                let infra_lock = state.infra.read().await;
                infra_lock.as_ref().and_then(|i| i.active_category_id)
            };

            // Create channel via REST
            let result = state
                .discord_client
                .create_channel(state.config.discord_guild_id, &normalized, 0) // type 0 = text
                .await
                .map_err(|e| format!("Failed to create channel: {e}"))?;

            let channel_id = result["id"]
                .as_str()
                .and_then(|s| s.parse::<u64>().ok())
                .ok_or_else(|| "Missing channel id in response".to_string())?;

            // Move to category if available
            if let Some(cat_id) = category_id {
                let _ = state
                    .discord_client
                    .edit_channel_category(channel_id, cat_id.get())
                    .await;
            }

            Ok(channel_id)
        })
    }

    fn register_channel(
        &self,
        channel_id: u64,
        agent_name: &str,
    ) -> Pin<Box<dyn Future<Output = ()> + Send>> {
        let name = agent_name.to_string();
        let state = self.state.clone();
        let ch_id = ChannelId::new(channel_id);
        Box::pin(async move {
            state.register_channel(ch_id, &name).await;
        })
    }

    fn build_spawned_prompt(
        &self,
        cwd: &str,
        packs: Option<Vec<String>>,
        compact_instructions: Option<String>,
    ) -> Option<Value> {
        let pack_strs: Option<Vec<&str>> = packs
            .as_ref()
            .map(|v| v.iter().map(String::as_str).collect());
        let preset = self.state.prompt_builder.spawned_agent_prompt(
            cwd,
            pack_strs.as_deref(),
            compact_instructions.as_deref(),
        );
        Some(json!({
            "type": "custom_preset",
            "preset": preset.preset,
            "custom_instructions": preset.append,
        }))
    }

    fn build_mcp_servers(
        &self,
        _agent_name: &str,
        _cwd: &str,
        extra_mcp_server_names: Option<Vec<String>>,
    ) -> Option<Value> {
        // Load user-defined external MCP servers from mcp_servers.json.
        // Built-in MCP servers (utils, schedule, discord, axi tools) are
        // handled in-process via the bot's MCP framework — they don't need
        // to be passed via --mcp-config.
        let names = extra_mcp_server_names.unwrap_or_default();
        if names.is_empty() {
            return None;
        }
        let servers = axi_config::mcp::load_mcp_servers(&self.state.config.mcp_servers_path, &names);
        if servers.is_empty() {
            return None;
        }
        Some(json!(servers))
    }

    fn run_initial_prompt(
        &self,
        agent_name: &str,
        prompt: &str,
    ) {
        let state = self.state.clone();
        let name = agent_name.to_string();
        let prompt = prompt.to_string();
        tokio::spawn(async move {
            let hub = {
                let lock = state.hub.read().await;
                match lock.as_ref() {
                    Some(h) => h.clone(),
                    None => return,
                }
            };
            let stream_handler = bridge::make_stream_handler(state);
            let content = axi_hub::types::MessageContent::Text(prompt);
            let _ = axi_hub::messaging::process_message(
                &hub,
                &name,
                &content,
                &stream_handler,
            )
            .await;
        });
    }

    fn trigger_restart(&self) {
        std::process::exit(42);
    }

    fn master_agent_name(&self) -> &str {
        &self.state.config.master_agent_name
    }

    fn default_agent_cwd(&self, agent_name: &str) -> String {
        self.state
            .config
            .default_cwd
            .join(agent_name)
            .to_string_lossy()
            .to_string()
    }

    fn stream_handler(&self) -> axi_hub::messaging::StreamHandlerFn {
        bridge::make_stream_handler(self.state.clone())
    }
}

impl TypeMapKey for BotState {
    type Value = Arc<Self>;
}
