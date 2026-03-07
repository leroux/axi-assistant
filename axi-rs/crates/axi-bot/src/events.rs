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

    // TODO: Look up agent for this channel via hub
    // TODO: Route message to agent or handle system commands
    // For now, just log that we received it
    debug!("Message routed (hub integration pending)");
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

    // TODO: Route to plan approval or question answer via hub
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
