//! Unified bot state — Discord, agent orchestration, and MCP.

use std::collections::HashMap;
use std::sync::atomic::AtomicBool;
use std::sync::Arc;
use std::time::Instant;

use serenity::all::ChannelId;
use serenity::prelude::TypeMapKey;
use tokio::sync::{Mutex, RwLock};

use axi_config::{Config, DiscordClient};

use crate::claude_process;
use crate::channels::GuildInfrastructure;
use crate::procmux_wire::ProcmuxProcessConnection;
use crate::prompts::PromptBuilder;
use crate::rate_limits::RateLimitTracker;
use crate::slots::Scheduler;
use crate::tasks::BackgroundTaskSet;
use crate::types::AgentSession;

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

/// All bot state — Discord layer + agent orchestration + config.
pub struct BotState {
    pub config: Config,
    pub discord_client: DiscordClient,
    pub startup_complete: AtomicBool,
    pub start_time: Instant,

    // --- Discord layer ---
    /// Channel ID → agent name mapping.
    pub channel_map: RwLock<HashMap<ChannelId, String>>,
    /// Agent name → channel ID (reverse lookup).
    pub agent_channels: RwLock<HashMap<String, ChannelId>>,
    /// Guild infrastructure (categories), set during `on_ready`.
    pub infra: RwLock<Option<GuildInfrastructure>>,
    /// Per-agent `CliSession` storage.
    pub transports: claude_process::TransportMap,
    /// Prompt builder for constructing system prompts.
    pub prompt_builder: PromptBuilder,
    /// Pending permission questions keyed by `message_id`.
    pub pending_questions: Mutex<HashMap<String, PendingQuestion>>,

    // --- Agent orchestration (formerly AgentHub) ---
    pub sessions: Arc<Mutex<HashMap<String, AgentSession>>>,
    /// Late-initialized during startup (needs Arc<BotState> for callbacks).
    pub scheduler: RwLock<Option<Arc<Scheduler>>>,
    pub rate_limits: Arc<Mutex<RateLimitTracker>>,
    pub tasks: BackgroundTaskSet,
    pub wake_lock: Mutex<()>,
    pub process_conn: Arc<Mutex<Option<ProcmuxProcessConnection>>>,
    pub query_timeout: f64,
    pub max_retries: u32,
    pub retry_base_delay: f64,
    pub slot_timeout: f64,
    pub shutdown_requested: AtomicBool,
}

impl BotState {
    pub fn new(config: Config, discord_client: DiscordClient) -> Self {
        let prompt_builder = PromptBuilder::new(
            &config.bot_dir,
            &config.axi_user_data,
            Some(config.bot_worktrees_dir.as_path()),
        );
        let rate_limits = RateLimitTracker::new(
            Some(config.usage_history_path.to_string_lossy().to_string()),
            Some(config.rate_limit_history_path.to_string_lossy().to_string()),
        );

        Self {
            query_timeout: config.query_timeout.as_secs_f64(),
            max_retries: config.api_error_max_retries,
            retry_base_delay: config.api_error_base_delay.as_secs_f64(),
            config,
            discord_client,
            startup_complete: AtomicBool::new(false),
            start_time: Instant::now(),
            channel_map: RwLock::new(HashMap::new()),
            agent_channels: RwLock::new(HashMap::new()),
            infra: RwLock::new(None),
            transports: claude_process::new_transport_map(),
            prompt_builder,
            pending_questions: Mutex::new(HashMap::new()),
            sessions: Arc::new(Mutex::new(HashMap::new())),
            scheduler: RwLock::new(None),
            rate_limits: Arc::new(Mutex::new(rate_limits)),
            tasks: BackgroundTaskSet::new(),
            wake_lock: Mutex::new(()),
            process_conn: Arc::new(Mutex::new(None)),
            slot_timeout: 300.0,
            shutdown_requested: AtomicBool::new(false),
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

    /// Get the scheduler. Panics if not yet initialized (before startup).
    pub async fn scheduler(&self) -> Arc<Scheduler> {
        self.scheduler
            .read()
            .await
            .clone()
            .expect("Scheduler not initialized")
    }
}

impl TypeMapKey for BotState {
    type Value = Arc<Self>;
}
