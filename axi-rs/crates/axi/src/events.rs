//! Discord event handlers — message routing, interaction dispatch, reactions.
//!
//! Thin layer that routes Discord events to the appropriate handler. All agent
//! state lives in `BotState`; Discord-specific rendering lives here.

use std::collections::VecDeque;
use std::sync::Arc;

use serenity::all::{Message, Reaction};
use serenity::client::Context;
use serenity::model::application::Interaction;
use serenity::model::channel::MessageType;
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
        let _ = state
            .discord_client
            .send_message(msg.channel_id.get(), "*System:* Please use the server channels instead.")
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

    // Extract content (may download images)
    let raw_content = extract_message_content(msg).await;

    let content_preview_str = match &raw_content {
        crate::types::MessageContent::Text(t) => content_preview(t, 100),
        crate::types::MessageContent::Blocks(_) => "[blocks with images]".to_string(),
    };
    info!(
        "Message from {} in #{}: {}",
        msg.author.name,
        msg.channel_id,
        content_preview_str
    );

    // Handle text commands (// prefix)
    if msg.content.starts_with("// ") {
        let cmd = msg.content[3..].trim();
        handle_text_command(msg, &state, cmd).await;
        return;
    }

    // Look up agent for this channel
    let agent_name = if let Some(name) = state.agent_for_channel(msg.channel_id).await { name } else {
        debug!(
            "No agent mapped to channel {}, ignoring message",
            msg.channel_id
        );
        return;
    };

    // Add timestamp prefix
    let ts_prefix = chrono::Utc::now()
        .format("[%Y-%m-%d %H:%M:%S UTC] ")
        .to_string();
    let message_content = match raw_content {
        crate::types::MessageContent::Text(text) => {
            crate::types::MessageContent::Text(format!("{ts_prefix}{text}"))
        }
        crate::types::MessageContent::Blocks(mut blocks) => {
            // Prepend timestamp to first text block
            if let Some(first) = blocks.first_mut() {
                if first.get("type").and_then(|v| v.as_str()) == Some("text") {
                    if let Some(text) = first.get("text").and_then(|v| v.as_str()) {
                        first["text"] = serde_json::Value::String(format!("{ts_prefix}{text}"));
                    }
                }
            }
            crate::types::MessageContent::Blocks(blocks)
        }
    };

    // Mark agent as interactive (user-facing) and reorder channel
    state.scheduler().await.mark_interactive(&agent_name).await;
    crate::channels::mark_channel_active(&state.discord_client, msg.channel_id.get()).await;

    // Wake-or-queue the message
    let woke = crate::lifecycle::wake_or_queue(
        &state,
        &agent_name,
        message_content.clone(),
        None,
    )
    .await;

    if woke {
        // Agent is awake — send the query in a background task
        let state_ref = Arc::clone(&state);
        let name = agent_name.clone();
        let user_msg_channel = msg.channel_id.get();
        let user_msg_id = msg.id.get();
        let stream_handler = make_stream_handler(Arc::clone(&state));
        tokio::spawn(async move {
            let query_lock = {
                let sessions = state_ref.sessions.lock().await;
                sessions.get(&name).map(|s| s.query_lock.clone())
            };

            if let Some(query_lock) = query_lock {
                {
                    let _lock = query_lock.lock().await;

                    {
                        let mut sessions = state_ref.sessions.lock().await;
                        if let Some(session) = sessions.get_mut(&name) {
                            crate::lifecycle::reset_activity(session);
                        }
                    }

                    match tokio::time::timeout(
                        std::time::Duration::from_secs_f64(state_ref.query_timeout),
                        crate::messaging::process_message(
                            &state_ref,
                            &name,
                            &message_content,
                            &stream_handler,
                        ),
                    )
                    .await
                    {
                        Ok(Ok(())) => {
                            let mut sessions = state_ref.sessions.lock().await;
                            if let Some(session) = sessions.get_mut(&name) {
                                session.last_activity = chrono::Utc::now();
                                session.activity = crate::activity::ActivityState::default();
                            }
                            // Add ✅ reaction to the user's message
                            let _ = state_ref.discord_client.add_reaction(
                                user_msg_channel, user_msg_id, "\u{2705}",
                            ).await;
                        }
                        Ok(Err(e)) => {
                            warn!("Query error for '{}': {}", name, e);
                            crate::frontend::post_system(
                                &state_ref,
                                &name,
                                &format!("Error: {e}"),
                            )
                            .await;
                        }
                        Err(_) => {
                            crate::messaging::handle_query_timeout(&state_ref, &name).await;
                        }
                    }
                }
                // query_lock dropped — sleep_agent can now check is_processing correctly
                crate::messaging::process_message_queue(&state_ref, &name, &stream_handler).await;
                crate::lifecycle::post_awaiting_input(&state_ref, &name).await;
                crate::lifecycle::sleep_agent(&state_ref, &name, false).await;
            }
        });
    } else {
        // Check if this is a killed agent (session removed but channel mapping persists)
        let is_killed = {
            let sessions = state.sessions.lock().await;
            !sessions.contains_key(&agent_name)
        };
        if is_killed {
            let _ = state
                .discord_client
                .send_message(
                    msg.channel_id.get(),
                    &format!("*System:* Agent **{agent_name}** has been killed. Messages are no longer accepted."),
                )
                .await;
        } else {
            debug!(
                "Agent '{}' not awake, message queued",
                agent_name
            );
            // Add hourglass reaction to indicate message is queued
            let _ = state
                .discord_client
                .add_reaction(msg.channel_id.get(), msg.id.get(), "\u{23f3}")
                .await;
        }
    }
}

// ---------------------------------------------------------------------------
// Text commands (// prefix)
// ---------------------------------------------------------------------------

/// Handle `// <command>` text commands.
///
/// Uses `DiscordClient` (not serenity `ctx.http`) for consistency — all
/// non-interaction message sends go through the standalone REST client.
async fn handle_text_command(
    msg: &Message,
    state: &BotState,
    cmd: &str,
) {
    let ch = msg.channel_id.get();
    let dc = &state.discord_client;

    let agent_name = if let Some(name) = state.agent_for_channel(msg.channel_id).await { name } else {
        let _ = dc.send_message(ch, "No agent in this channel.").await;
        return;
    };

    let parts: Vec<&str> = cmd.splitn(2, ' ').collect();
    let command = parts[0].to_lowercase();

    match command.as_str() {
        "debug" => {
            let mut sessions = state.sessions.lock().await;
            if let Some(session) = sessions.get_mut(&agent_name) {
                session.debug = !session.debug;
                let status = if session.debug { "enabled" } else { "disabled" };
                let _ = dc.send_message(ch, &format!("Debug mode {status}.")).await;
            }
        }
        "status" => {
            let sessions = state.sessions.lock().await;
            if let Some(session) = sessions.get(&agent_name) {
                let awake = session.awake;
                let session_id = session.session_id.as_deref().unwrap_or("none");
                let status = format!(
                    "**{}** — {} | session: `{}`",
                    agent_name,
                    if awake { "awake" } else { "sleeping" },
                    session_id,
                );
                let _ = dc.send_message(ch, &status).await;
            }
        }
        "clear" => {
            let content = crate::types::MessageContent::Text("/clear".to_string());
            crate::lifecycle::wake_or_queue(state, &agent_name, content, None).await;
            let _ = dc.send_message(ch, "Sent /clear to agent.").await;
        }
        "compact" => {
            let content = crate::types::MessageContent::Text("/compact".to_string());
            crate::lifecycle::wake_or_queue(state, &agent_name, content, None).await;
            let _ = dc.send_message(ch, "Sent /compact to agent.").await;
        }
        "stop" => {
            crate::messaging::interrupt_session(state, &agent_name).await;
            let _ = dc.send_message(ch, "Agent interrupted.").await;
        }
        "flowchart" => {
            let cmd_args = parts.get(1).copied().unwrap_or("");
            if cmd_args.is_empty() {
                let _ = dc.send_message(ch, "Usage: `//flowchart <command> [args]`").await;
                return;
            }

            let fc_parts: Vec<&str> = cmd_args.splitn(2, ' ').collect();
            let fc_name = fc_parts[0].trim_start_matches('/');
            let fc_args = fc_parts.get(1).copied().unwrap_or("");
            let slash_content = if fc_args.is_empty() {
                format!("/{fc_name}")
            } else {
                format!("/{fc_name} {fc_args}")
            };

            let is_busy = {
                let sessions = state.sessions.lock().await;
                sessions.get(&agent_name).is_some_and(crate::lifecycle::is_processing)
            };

            if is_busy {
                let _ = dc.send_message(ch, &format!("Agent **{agent_name}** is busy.")).await;
                return;
            }

            let content = crate::types::MessageContent::Text(slash_content);
            crate::lifecycle::wake_or_queue(state, &agent_name, content, None).await;
        }
        _ => {
            let _ = dc.send_message(
                ch,
                &format!("Unknown command: `{command}`. Available: debug, status, clear, compact, stop, flowchart"),
            ).await;
        }
    }
}

/// Create a stream handler that consumes SDK output and renders to Discord.
fn make_stream_handler(state: Arc<BotState>) -> crate::messaging::StreamHandlerFn {
    crate::claude_process::make_stream_handler(state)
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

/// Number emoji constants for matching reactions.
const EMOJI_NUMBERS: &[&str] = &["1\u{fe0f}\u{20e3}", "2\u{fe0f}\u{20e3}", "3\u{fe0f}\u{20e3}", "4\u{fe0f}\u{20e3}"];

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
    let message_id = reaction.message_id.get().to_string();
    debug!(
        "Reaction {} on message {} in channel {}",
        emoji, message_id, reaction.channel_id
    );

    // Check if this reaction resolves a pending question
    let pending = {
        let mut pending = state.pending_questions.lock().await;
        pending.remove(&message_id)
    };

    if let Some(question) = pending {
        let answer = match question.question_type {
            crate::state::QuestionType::AskUser => {
                // Number emoji → selection index
                if let Some(idx) = EMOJI_NUMBERS.iter().position(|e| *e == emoji) {
                    if idx < question.options.len() {
                        Some(crate::state::QuestionAnswer::Selection(idx))
                    } else {
                        None
                    }
                } else {
                    None
                }
            }
            crate::state::QuestionType::PlanApproval => {
                match emoji.as_str() {
                    "\u{2705}" | "\u{2714}\u{fe0f}" => {
                        Some(crate::state::QuestionAnswer::Approved)
                    }
                    "\u{274c}" | "\u{274e}" => {
                        Some(crate::state::QuestionAnswer::Denied)
                    }
                    _ => None,
                }
            }
        };

        if let Some(answer) = answer {
            info!(
                "Resolved pending question for agent '{}' (msg {})",
                question.agent_name, message_id
            );
            let _ = question.sender.send(answer);
        } else {
            // Unrecognized emoji — put the question back
            let mut pending = state.pending_questions.lock().await;
            pending.insert(message_id, question);
        }
        return;
    }

    // Fallback: legacy plan approval (for reactions on non-pending messages)
    let agent_name = match state.agent_for_channel(reaction.channel_id).await {
        Some(name) => name,
        None => return,
    };

    match emoji.as_str() {
        "\u{2705}" | "\u{2714}\u{fe0f}" => {
            info!("Plan approved for agent '{}' via reaction", agent_name);
            let content = crate::types::MessageContent::Text(
                "Plan approved. Proceed with implementation.".to_string(),
            );
            crate::lifecycle::wake_or_queue(&state, &agent_name, content, None).await;
        }
        "\u{274c}" | "\u{274e}" => {
            info!("Plan rejected for agent '{}' via reaction", agent_name);
            let content = crate::types::MessageContent::Text(
                "Plan rejected. Please revise your approach.".to_string(),
            );
            crate::lifecycle::wake_or_queue(&state, &agent_name, content, None).await;
        }
        _ => {}
    }
}

// ---------------------------------------------------------------------------
// Message content extraction
// ---------------------------------------------------------------------------

/// Image content types we support base64-encoding for the API.
const IMAGE_TYPES: &[&str] = &["image/png", "image/jpeg", "image/gif", "image/webp"];

/// Extract message content, downloading and base64-encoding image attachments.
///
/// Returns `MessageContent::Blocks` when images are present, `MessageContent::Text` otherwise.
pub async fn extract_message_content(msg: &Message) -> crate::types::MessageContent {
    let mut text_parts = Vec::new();
    let mut image_blocks: Vec<serde_json::Value> = Vec::new();

    // Add text content
    if !msg.content.is_empty() {
        text_parts.push(msg.content.clone());
    }

    // Process attachments
    for attachment in &msg.attachments {
        let content_type = attachment.content_type.as_deref().unwrap_or("");
        if IMAGE_TYPES.iter().any(|t| content_type.starts_with(t)) {
            // Download and base64-encode image
            match download_and_encode(&attachment.url).await {
                Ok(b64_data) => {
                    let media_type = content_type.split(';').next().unwrap_or(content_type);
                    image_blocks.push(serde_json::json!({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_data,
                        }
                    }));
                }
                Err(e) => {
                    warn!("Failed to download image {}: {}", attachment.filename, e);
                    let size_kb = attachment.size / 1024;
                    text_parts.push(format!(
                        "[Attachment: {} ({} KB, {}) — download failed]",
                        attachment.filename, size_kb, content_type
                    ));
                }
            }
        } else {
            let size_kb = attachment.size / 1024;
            text_parts.push(format!(
                "[Attachment: {} ({} KB, {})]",
                attachment.filename,
                size_kb,
                attachment.content_type.as_deref().unwrap_or("unknown type")
            ));
        }
    }

    // Add embed descriptions
    for embed in &msg.embeds {
        if let Some(desc) = &embed.description {
            text_parts.push(format!("[Embed: {desc}]"));
        }
    }

    if image_blocks.is_empty() {
        crate::types::MessageContent::Text(text_parts.join("\n"))
    } else {
        // Build blocks array: text first, then images
        let mut blocks = Vec::new();
        let text = text_parts.join("\n");
        if !text.is_empty() {
            blocks.push(serde_json::json!({"type": "text", "text": text}));
        }
        blocks.extend(image_blocks);
        crate::types::MessageContent::Blocks(blocks)
    }
}

/// Download a URL and return its content as base64-encoded string.
async fn download_and_encode(url: &str) -> Result<String, String> {
    use base64::Engine;
    let client = reqwest::Client::new();
    let resp = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("HTTP request failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status()));
    }
    let bytes = resp
        .bytes()
        .await
        .map_err(|e| format!("Failed to read body: {e}"))?;
    Ok(base64::engine::general_purpose::STANDARD.encode(&bytes))
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
