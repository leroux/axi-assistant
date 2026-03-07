//! Discord event handlers — message routing, interaction dispatch, reactions.
//!
//! Thin layer that routes Discord events to the appropriate handler. All agent
//! state lives in the AgentHub; Discord-specific rendering lives here.

use std::collections::VecDeque;
use std::sync::Arc;

use serenity::all::{ChannelId, Message, Reaction};
use serenity::client::Context;
use serenity::model::application::Interaction;
use tracing::{debug, info, warn};

use crate::commands;
use crate::state::BotState;

// ---------------------------------------------------------------------------
// Message dedup
// ---------------------------------------------------------------------------

/// Simple bounded dedup set to prevent processing duplicate message deliveries
/// from Discord gateway reconnects.
pub struct MessageDedup {
    seen: VecDeque<u64>,
    capacity: usize,
}

impl MessageDedup {
    pub fn new(capacity: usize) -> Self {
        Self {
            seen: VecDeque::with_capacity(capacity),
            capacity,
        }
    }

    /// Returns true if this message ID has already been seen.
    pub fn check_and_insert(&mut self, id: u64) -> bool {
        if self.seen.contains(&id) {
            return true;
        }
        if self.seen.len() >= self.capacity {
            self.seen.pop_front();
        }
        self.seen.push_back(id);
        false
    }
}

// ---------------------------------------------------------------------------
// Message handling
// ---------------------------------------------------------------------------

/// Handle incoming Discord messages.
///
/// Mirrors the Python `on_message` handler: filters unauthorized users,
/// ignores own messages, deduplicates, routes to agents.
pub async fn handle_message(ctx: &Context, msg: &Message) {
    let data = ctx.data.read().await;
    let state = match data.get::<BotState>() {
        Some(s) => Arc::clone(s),
        None => return,
    };
    drop(data);

    // Don't process until startup is complete
    if !state
        .startup_complete
        .load(std::sync::atomic::Ordering::SeqCst)
    {
        return;
    }

    // Ignore own messages
    {
        let current_user = ctx.cache.current_user();
        if msg.author.id == current_user.id {
            return;
        }
    }

    // Only process regular messages and replies
    use serenity::model::channel::MessageType;
    if msg.kind != MessageType::Regular && msg.kind != MessageType::InlineReply {
        return;
    }

    // Ignore bots unless in allowed list
    if msg.author.bot && !state.config.allowed_user_ids.contains(&msg.author.id.get()) {
        return;
    }

    // DM messages — redirect
    if msg.guild_id.is_none() {
        if !state.config.allowed_user_ids.contains(&msg.author.id.get()) {
            return;
        }
        let _ = msg
            .channel_id
            .say(&ctx.http, "*System:* Please use the server channels instead.")
            .await;
        return;
    }

    // Only process from target guild
    if let Some(guild_id) = msg.guild_id {
        if guild_id.get() != state.config.discord_guild_id {
            return;
        }
    }

    // Only process from allowed users
    if !state.config.allowed_user_ids.contains(&msg.author.id.get()) {
        return;
    }

    // Extract content
    let content = extract_message_content(msg);

    info!(
        "Message from {} in #{}: {}",
        msg.author.name,
        msg.channel_id,
        content_preview(&content, 100)
    );

    // Look up agent for this channel
    let agent_name = match state.agent_for_channel(msg.channel_id).await {
        Some(name) => name,
        None => {
            debug!(
                "No agent mapped to channel {}, ignoring message",
                msg.channel_id
            );
            return;
        }
    };

    // Add timestamp prefix (matches Python behavior)
    let ts_prefix = chrono::Utc::now()
        .format("[%Y-%m-%d %H:%M:%S UTC] ")
        .to_string();
    let message_content = axi_hub::MessageContent::Text(format!("{}{}", ts_prefix, content));

    // Mark agent as interactive (user-facing)
    let hub = state.hub().await;
    hub.scheduler.mark_interactive(&agent_name).await;

    // Wake-or-queue the message
    let woke = axi_hub::lifecycle::wake_or_queue(
        &hub,
        &agent_name,
        message_content.clone(),
        None,
    )
    .await;

    if woke {
        // Agent is awake — send the query in a background task
        let hub_ref = hub.clone_ref();
        let name = agent_name.clone();
        let stream_handler = make_stream_handler(Arc::clone(&state));
        tokio::spawn(async move {
            let query_lock = {
                let sessions = hub_ref.sessions.lock().await;
                sessions.get(&name).map(|s| s.query_lock.clone())
            };

            if let Some(query_lock) = query_lock {
                let _lock = query_lock.lock().await;

                {
                    let mut sessions = hub_ref.sessions.lock().await;
                    if let Some(session) = sessions.get_mut(&name) {
                        axi_hub::lifecycle::reset_activity(session);
                    }
                }

                match tokio::time::timeout(
                    std::time::Duration::from_secs_f64(hub_ref.query_timeout),
                    axi_hub::messaging::process_message(
                        &hub_ref,
                        &name,
                        &message_content,
                        &stream_handler,
                    ),
                )
                .await
                {
                    Ok(Ok(())) => {
                        let mut sessions = hub_ref.sessions.lock().await;
                        if let Some(session) = sessions.get_mut(&name) {
                            session.last_activity = chrono::Utc::now();
                            session.activity = claudewire::events::ActivityState::default();
                        }
                    }
                    Ok(Err(e)) => {
                        warn!("Query error for '{}': {}", name, e);
                        hub_ref
                            .callbacks
                            .post_system(&name, &format!("Error: {}", e))
                            .await;
                    }
                    Err(_) => {
                        axi_hub::messaging::handle_query_timeout(&hub_ref, &name).await;
                    }
                }

                // Process queue then sleep
                axi_hub::messaging::process_message_queue(&hub_ref, &name, &stream_handler).await;
                axi_hub::lifecycle::sleep_agent(&hub_ref, &name, false).await;
            }
        });
    } else {
        debug!(
            "Agent '{}' not awake, message queued",
            agent_name
        );
    }
}

/// Create a stream handler that consumes SDK output and renders to Discord.
fn make_stream_handler(state: Arc<BotState>) -> axi_hub::messaging::StreamHandlerFn {
    crate::bridge::make_stream_handler(state)
}

// ---------------------------------------------------------------------------
// Interaction handling
// ---------------------------------------------------------------------------

/// Handle Discord interaction events (slash commands, buttons, etc.).
pub async fn handle_interaction(ctx: &Context, interaction: Interaction) {
    match interaction {
        Interaction::Command(command) => {
            commands::handle_command(ctx, &command).await;
        }
        Interaction::Autocomplete(_autocomplete) => {
            // TODO: agent name autocomplete
            debug!("Autocomplete interaction (not yet implemented)");
        }
        _ => {
            debug!("Unhandled interaction type");
        }
    }
}

// ---------------------------------------------------------------------------
// Reaction handling
// ---------------------------------------------------------------------------

/// Handle reaction add events — plan approval and question answers.
pub async fn handle_reaction_add(ctx: &Context, reaction: &Reaction) {
    let data = ctx.data.read().await;
    let state = match data.get::<BotState>() {
        Some(s) => Arc::clone(s),
        None => return,
    };
    drop(data);

    // Ignore own reactions
    if let Some(user_id) = reaction.user_id {
        let current_user = ctx.cache.current_user();
        if user_id == current_user.id {
            return;
        }
    }

    // Only process from allowed users in target guild
    if let Some(guild_id) = reaction.guild_id {
        if guild_id.get() != state.config.discord_guild_id {
            return;
        }
    }

    if let Some(user_id) = reaction.user_id {
        if !state.config.allowed_user_ids.contains(&user_id.get()) {
            return;
        }
    }

    let emoji = reaction.emoji.to_string();
    debug!(
        "Reaction {} on message {} in channel {}",
        emoji, reaction.message_id, reaction.channel_id
    );

    // Look up agent for this channel
    let agent_name = match state.agent_for_channel(reaction.channel_id).await {
        Some(name) => name,
        None => return,
    };

    // Plan approval: checkmark = approve, X = reject
    match emoji.as_str() {
        "\u{2705}" | "\u{2714}\u{fe0f}" => {
            // Checkmark — approve plan
            info!(
                "Plan approved for agent '{}' via reaction",
                agent_name
            );
            // Send approval message to agent
            let hub = state.hub().await;
            let content = axi_hub::MessageContent::Text(
                "Plan approved. Proceed with implementation.".to_string(),
            );
            axi_hub::lifecycle::wake_or_queue(&hub, &agent_name, content, None).await;
        }
        "\u{274c}" | "\u{274e}" => {
            // X mark — reject plan
            info!(
                "Plan rejected for agent '{}' via reaction",
                agent_name
            );
            let hub = state.hub().await;
            let content = axi_hub::MessageContent::Text(
                "Plan rejected. Please revise your approach.".to_string(),
            );
            axi_hub::lifecycle::wake_or_queue(&hub, &agent_name, content, None).await;
        }
        _ => {
            // Other reactions — could be question answers (1️⃣, 2️⃣, etc.)
            debug!(
                "Unhandled reaction {} for agent '{}'",
                emoji, agent_name
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Message content extraction
// ---------------------------------------------------------------------------

/// Extract text content from a Discord message, including attachments.
pub fn extract_message_content(msg: &Message) -> String {
    let mut parts = Vec::new();

    // Add text content
    if !msg.content.is_empty() {
        parts.push(msg.content.clone());
    }

    // Add attachment descriptions
    for attachment in &msg.attachments {
        let size_kb = attachment.size / 1024;
        parts.push(format!(
            "[Attachment: {} ({} KB, {})]",
            attachment.filename,
            size_kb,
            attachment
                .content_type
                .as_deref()
                .unwrap_or("unknown type")
        ));
    }

    // Add embed descriptions
    for embed in &msg.embeds {
        if let Some(desc) = &embed.description {
            parts.push(format!("[Embed: {}]", desc));
        }
    }

    parts.join("\n")
}

/// Short preview of message content for logging.
fn content_preview(content: &str, max_len: usize) -> String {
    if content.len() <= max_len {
        content.replace('\n', " ")
    } else {
        format!("{}...", &content[..max_len].replace('\n', " "))
    }
}

// ---------------------------------------------------------------------------
// Discord message sending helpers
// ---------------------------------------------------------------------------

/// Send a system message to a channel (prefixed with *System:*).
pub async fn send_system(ctx: &Context, channel_id: ChannelId, text: &str) {
    let msg = format!("*System:* {}", text);
    if let Err(e) = channel_id.say(&ctx.http, &msg).await {
        warn!("Failed to send system message to {}: {}", channel_id, e);
    }
}

/// Send a long message, splitting at Discord's 2000 char limit.
pub async fn send_long(ctx: &Context, channel_id: ChannelId, text: &str) {
    const MAX_LEN: usize = 1900; // Leave room for formatting

    if text.len() <= MAX_LEN {
        let _ = channel_id.say(&ctx.http, text).await;
        return;
    }

    // Split on newlines, grouping into chunks that fit
    let mut chunk = String::new();
    for line in text.lines() {
        if chunk.len() + line.len() + 1 > MAX_LEN {
            if !chunk.is_empty() {
                let _ = channel_id.say(&ctx.http, &chunk).await;
                chunk.clear();
            }
        }
        if !chunk.is_empty() {
            chunk.push('\n');
        }
        chunk.push_str(line);
    }
    if !chunk.is_empty() {
        let _ = channel_id.say(&ctx.http, &chunk).await;
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_message_dedup() {
        let mut dedup = MessageDedup::new(3);
        assert!(!dedup.check_and_insert(1));
        assert!(!dedup.check_and_insert(2));
        assert!(dedup.check_and_insert(1)); // duplicate
        assert!(!dedup.check_and_insert(3));
        assert!(!dedup.check_and_insert(4)); // evicts 1
        assert!(!dedup.check_and_insert(1)); // 1 was evicted, so not a dup
    }

    #[test]
    fn test_content_preview() {
        assert_eq!(content_preview("hello", 10), "hello");
        assert_eq!(content_preview("hello world long text", 10), "hello worl...");
        assert_eq!(content_preview("line1\nline2", 20), "line1 line2");
    }
}
