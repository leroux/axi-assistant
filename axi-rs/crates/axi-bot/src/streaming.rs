//! Response streaming to Discord — live-edit messages during agent responses.
//!
//! During an agent's response stream, text deltas are accumulated and
//! periodically flushed to Discord via REST message edits. This gives
//! the user real-time feedback without waiting for the full response.

use std::time::Instant;

use axi_config::DiscordClient;
use tracing::{debug, warn};

/// Generate a trace ID for this request.
///
/// If OpenTelemetry is active and has a valid trace context, uses the OTel
/// trace ID (first 16 hex chars). Otherwise generates a random 16-char hex ID.
fn generate_trace_id() -> String {
    use opentelemetry::trace::TraceContextExt;
    let ctx = opentelemetry::Context::current();
    let span_ref = ctx.span();
    let trace_id = span_ref.span_context().trace_id();
    if trace_id != opentelemetry::trace::TraceId::INVALID {
        // Use first 16 chars of the 32-char hex trace ID
        format!("{trace_id}")[..16].to_string()
    } else {
        // No OTel context — generate random ID
        let bytes: [u8; 8] = rand_bytes();
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }
}

/// Generate 8 random bytes using a simple approach (no extra dep needed).
fn rand_bytes() -> [u8; 8] {
    let mut buf = [0u8; 8];
    // Use timestamp + thread ID as entropy source
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let tid = std::thread::current().id();
    let seed = now ^ (format!("{tid:?}").len() as u128 * 0x517c_c1b7_2722_0a95);
    for (i, byte) in buf.iter_mut().enumerate() {
        *byte = ((seed >> (i * 8)) & 0xFF) as u8;
    }
    buf
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/// Block cursor to indicate "still typing".
const STREAMING_CURSOR: &str = "\u{2588}";

/// Max message length before splitting into a new message.
const MSG_LIMIT: usize = 1900;

/// Minimum interval between edits in seconds (rate limit protection).
const EDIT_INTERVAL: f64 = 0.8;

// ---------------------------------------------------------------------------
// Live-edit state
// ---------------------------------------------------------------------------

/// Tracks a single Discord message being live-edited during streaming.
pub struct LiveEditState {
    pub channel_id: u64,
    pub message_id: Option<String>,
    pub content: String,
    pub last_edit_time: Instant,
    pub edit_pending: bool,
    pub finalized: bool,
}

impl LiveEditState {
    pub fn new(channel_id: u64) -> Self {
        Self {
            channel_id,
            message_id: None,
            content: String::new(),
            last_edit_time: Instant::now().checked_sub(std::time::Duration::from_secs(10)).unwrap(), // allow immediate first edit
            edit_pending: false,
            finalized: false,
        }
    }
}

/// Mutable state for a single response stream.
pub struct StreamContext {
    pub text_buffer: String,
    pub live_edit: Option<LiveEditState>,
    /// Channel ID for non-streaming fallback (post full text when done).
    pub channel_id: Option<u64>,
    pub got_result: bool,
    pub hit_rate_limit: bool,
    pub flush_count: u32,
    pub last_flushed_msg_id: Option<String>,
    pub last_flushed_channel_id: Option<u64>,
    pub last_flushed_content: String,

    // Thinking indicator
    pub thinking_msg_id: Option<String>,

    // Tool progress messages (to delete after tool completes)
    pub tool_progress_msg_ids: Vec<(u64, String)>, // (channel_id, msg_id)
    pub current_tool_name: Option<String>,

    // Tool input accumulation (for TodoWrite detection)
    pub tool_input_json: String,

    // Timing
    pub start_time: Instant,

    // Trace ID for this request (first 16 hex chars of a UUID)
    pub trace_id: String,

    // Debug mode
    pub debug: bool,

    // Clean tool messages feature
    pub clean_tool_messages: bool,
}

impl StreamContext {
    pub fn new(channel_id: Option<u64>, streaming_enabled: bool) -> Self {
        Self {
            text_buffer: String::new(),
            live_edit: if streaming_enabled {
                channel_id.map(LiveEditState::new)
            } else {
                None
            },
            channel_id,
            got_result: false,
            hit_rate_limit: false,
            flush_count: 0,
            last_flushed_msg_id: None,
            last_flushed_channel_id: None,
            last_flushed_content: String::new(),
            thinking_msg_id: None,
            tool_progress_msg_ids: Vec::new(),
            current_tool_name: None,
            tool_input_json: String::new(),
            trace_id: generate_trace_id(),
            debug: false,
            clean_tool_messages: false,
            start_time: Instant::now(),
        }
    }
}

// ---------------------------------------------------------------------------
// Thinking indicator
// ---------------------------------------------------------------------------

/// Show a "thinking..." indicator message.
pub async fn show_thinking(
    ctx: &mut StreamContext,
    discord: &DiscordClient,
    _agent_name: &str,
) {
    let channel_id = match &ctx.live_edit {
        Some(le) => le.channel_id,
        None => return,
    };

    match discord.send_message(channel_id, "*thinking...*").await {
        Ok(resp) => {
            ctx.thinking_msg_id = resp
                .get("id")
                .and_then(|v| v.as_str())
                .map(ToString::to_string);
        }
        Err(e) => {
            warn!("Failed to post thinking indicator: {}", e);
        }
    }
}

/// Hide the "thinking..." indicator message.
pub async fn hide_thinking(
    ctx: &mut StreamContext,
    discord: &DiscordClient,
    _agent_name: &str,
) {
    let channel_id = match &ctx.live_edit {
        Some(le) => le.channel_id,
        None => return,
    };

    if let Some(msg_id) = ctx.thinking_msg_id.take() {
        if let Ok(id) = msg_id.parse::<u64>() {
            let _ = discord.delete_message(channel_id, id).await;
        }
    }
}

// ---------------------------------------------------------------------------
// Tool progress
// ---------------------------------------------------------------------------

/// Show a temporary progress message for a tool call.
pub async fn show_tool_progress(
    ctx: &mut StreamContext,
    discord: &DiscordClient,
    agent_name: &str,
    tool_name: &str,
) {
    if !ctx.clean_tool_messages {
        return;
    }
    let channel_id = match &ctx.live_edit {
        Some(le) => le.channel_id,
        None => return,
    };

    let display_name = claudewire::events::tool_display(tool_name);
    let content = format!("*{display_name}...*");
    match discord.send_message(channel_id, &content).await {
        Ok(resp) => {
            if let Some(msg_id) = resp.get("id").and_then(|v| v.as_str()) {
                ctx.tool_progress_msg_ids
                    .push((channel_id, msg_id.to_string()));
                debug!(
                    "TOOL_PROGRESS[{}] {} msg_id={}",
                    agent_name, tool_name, msg_id
                );
            }
        }
        Err(e) => {
            warn!("Failed to post tool progress for {}: {}", tool_name, e);
        }
    }
}

/// Delete all tool progress messages.
pub async fn delete_tool_progress(
    ctx: &mut StreamContext,
    discord: &DiscordClient,
    _agent_name: &str,
) {
    for (channel_id, msg_id) in ctx.tool_progress_msg_ids.drain(..) {
        if let Ok(id) = msg_id.parse::<u64>() {
            let _ = discord.delete_message(channel_id, id).await;
        }
    }
}

// ---------------------------------------------------------------------------
// Timing suffix
// ---------------------------------------------------------------------------

/// Append a timing suffix (e.g., "-# 3.2s") to the last flushed message.
pub async fn append_timing(
    ctx: &StreamContext,
    discord: &DiscordClient,
    _agent_name: &str,
) {
    let elapsed = ctx.start_time.elapsed().as_secs_f64();
    if elapsed < 0.5 {
        return; // skip for very fast responses
    }

    let channel_id = match ctx.last_flushed_channel_id {
        Some(id) => id,
        None => return,
    };
    let msg_id = match &ctx.last_flushed_msg_id {
        Some(id) => match id.parse::<u64>() {
            Ok(n) => n,
            Err(_) => return,
        },
        None => return,
    };

    let timing = if elapsed < 60.0 {
        format!("{elapsed:.1}s")
    } else {
        let mins = elapsed as u64 / 60;
        let secs = elapsed as u64 % 60;
        format!("{mins}m{secs}s")
    };

    let trace_tag = if ctx.trace_id.is_empty() {
        String::new()
    } else {
        format!(" [trace={}]", ctx.trace_id)
    };
    let new_content = format!("{}\n-# {timing}{trace_tag}", ctx.last_flushed_content);
    let _ = discord.edit_message(channel_id, msg_id, &new_content).await;
}

// ---------------------------------------------------------------------------
// Live-edit operations
// ---------------------------------------------------------------------------

/// Post a new message and record its ID in the live-edit state.
async fn live_edit_post(
    le: &mut LiveEditState,
    content: &str,
    discord: &DiscordClient,
    agent_name: &str,
) {
    match discord.send_message(le.channel_id, content).await {
        Ok(resp) => {
            le.message_id = resp
                .get("id")
                .and_then(|v| v.as_str())
                .map(ToString::to_string);
            le.content = content.to_string();
            le.last_edit_time = Instant::now();
            le.edit_pending = false;
            debug!(
                "LIVE_EDIT_POST[{}] msg_id={:?} len={}",
                agent_name,
                le.message_id,
                content.len()
            );
        }
        Err(e) => {
            warn!("LIVE_EDIT_POST[{}] failed: {}", agent_name, e);
        }
    }
}

/// Edit the current live-edit message with new content.
async fn live_edit_update(
    le: &mut LiveEditState,
    content: &str,
    discord: &DiscordClient,
    agent_name: &str,
) {
    let msg_id = match &le.message_id {
        Some(id) => match id.parse::<u64>() {
            Ok(n) => n,
            Err(_) => return,
        },
        None => return,
    };

    match discord
        .edit_message(le.channel_id, msg_id, content)
        .await
    {
        Ok(_) => {
            le.content = content.to_string();
            le.last_edit_time = Instant::now();
            le.edit_pending = false;
            debug!(
                "LIVE_EDIT_UPDATE[{}] msg_id={} len={}",
                agent_name,
                msg_id,
                content.len()
            );
        }
        Err(e) => {
            let err_str = e.to_string();
            if err_str.contains("429") {
                warn!(
                    "LIVE_EDIT_UPDATE[{}] rate limited, backing off",
                    agent_name
                );
                le.last_edit_time =
                    Instant::now() + std::time::Duration::from_secs(2);
                le.edit_pending = true;
            } else {
                warn!("LIVE_EDIT_UPDATE[{}] edit failed: {}", agent_name, e);
            }
        }
    }
}

/// Called on each `text_delta`. Posts or edits the message if enough time has passed.
pub async fn live_edit_tick(
    ctx: &mut StreamContext,
    discord: &DiscordClient,
    agent_name: &str,
) {
    let le = match &mut ctx.live_edit {
        Some(le) if !le.finalized => le,
        _ => return,
    };

    let text = ctx.text_buffer.trim_start().to_string();
    if text.is_empty() {
        return;
    }

    // First message: post immediately
    if le.message_id.is_none() {
        let content = format!("{text}{STREAMING_CURSOR}");
        live_edit_post(le, &content, discord, agent_name).await;
        return;
    }

    // Content exceeds limit: finalize current, start new
    if text.len() > MSG_LIMIT {
        let split_at = text[..MSG_LIMIT]
            .rfind('\n')
            .unwrap_or(MSG_LIMIT);
        let final_content = &text[..split_at];
        live_edit_update(le, final_content, discord, agent_name).await;

        // Reset for new message
        ctx.text_buffer = text[split_at..].trim_start_matches('\n').to_string();
        le.message_id = None;
        le.content.clear();
        le.edit_pending = false;

        let remainder = ctx.text_buffer.trim_start().to_string();
        if !remainder.is_empty() {
            let content = format!("{remainder}{STREAMING_CURSOR}");
            live_edit_post(le, &content, discord, agent_name).await;
        }
        return;
    }

    // Throttled edit
    if le.last_edit_time.elapsed().as_secs_f64() >= EDIT_INTERVAL {
        let content = format!("{text}{STREAMING_CURSOR}");
        live_edit_update(le, &content, discord, agent_name).await;
    }
}

/// Finalize the current live-edit message: remove cursor, post any remaining content.
///
/// When streaming is disabled (no `live_edit`), posts the full accumulated text
/// as one or more regular messages.
pub async fn live_edit_finalize(
    ctx: &mut StreamContext,
    discord: &DiscordClient,
    agent_name: &str,
) {
    let le = match &mut ctx.live_edit {
        Some(le) => le,
        None => {
            // Non-streaming mode: post accumulated text as regular message(s)
            let text = ctx.text_buffer.trim().to_string();
            if text.is_empty() {
                return;
            }
            let channel_id = match ctx.channel_id {
                Some(id) => id,
                None => return,
            };
            for chunk in split_message(&text) {
                if let Ok(resp) = discord.send_message(channel_id, &chunk).await {
                    ctx.last_flushed_msg_id = resp
                        .get("id")
                        .and_then(|v| v.as_str())
                        .map(ToString::to_string);
                    ctx.last_flushed_channel_id = Some(channel_id);
                    ctx.last_flushed_content = chunk;
                }
            }
            ctx.text_buffer.clear();
            ctx.flush_count += 1;
            debug!("NON_STREAM_POST[{}] len={}", agent_name, text.len());
            return;
        }
    };

    let text = ctx.text_buffer.trim_start().to_string();

    if le.message_id.is_some() && !text.is_empty() {
        let chunks = split_message(&text);
        if chunks.len() == 1 {
            live_edit_update(le, &chunks[0], discord, agent_name).await;
            ctx.last_flushed_msg_id = le.message_id.clone();
            ctx.last_flushed_channel_id = Some(le.channel_id);
            ctx.last_flushed_content = chunks[0].clone();
        } else {
            // First chunk into existing message
            live_edit_update(le, &chunks[0], discord, agent_name).await;
            // Remaining chunks as new messages
            for chunk in &chunks[1..] {
                if let Ok(resp) = discord.send_message(le.channel_id, chunk).await {
                    ctx.last_flushed_msg_id = resp
                        .get("id")
                        .and_then(|v| v.as_str())
                        .map(ToString::to_string);
                    ctx.last_flushed_channel_id = Some(le.channel_id);
                    ctx.last_flushed_content = chunk.clone();
                }
            }
        }
    } else if le.message_id.is_none() && !text.is_empty() {
        // Never posted — send normally
        for chunk in split_message(&text) {
            if let Ok(resp) = discord.send_message(le.channel_id, &chunk).await {
                ctx.last_flushed_msg_id = resp
                    .get("id")
                    .and_then(|v| v.as_str())
                    .map(ToString::to_string);
                ctx.last_flushed_channel_id = Some(le.channel_id);
                ctx.last_flushed_content = chunk;
            }
        }
    }

    // Reset for next text block
    le.message_id = None;
    le.content.clear();
    le.edit_pending = false;
    ctx.text_buffer.clear();
    ctx.flush_count += 1;
}

// ---------------------------------------------------------------------------
// Message splitting
// ---------------------------------------------------------------------------

/// Split a message into chunks that fit within Discord's 2000 char limit.
pub fn split_message(text: &str) -> Vec<String> {
    const MAX_LEN: usize = 1900;

    if text.len() <= MAX_LEN {
        return vec![text.to_string()];
    }

    let mut chunks = Vec::new();
    let mut remaining = text;

    while !remaining.is_empty() {
        if remaining.len() <= MAX_LEN {
            chunks.push(remaining.to_string());
            break;
        }

        // Try to split on a newline
        let split_at = remaining[..MAX_LEN]
            .rfind('\n')
            .unwrap_or(MAX_LEN);

        chunks.push(remaining[..split_at].to_string());
        remaining = remaining[split_at..].trim_start_matches('\n');
    }

    chunks
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_short_message() {
        let chunks = split_message("hello world");
        assert_eq!(chunks, vec!["hello world"]);
    }

    #[test]
    fn split_long_message() {
        let long = "x".repeat(3000);
        let chunks = split_message(&long);
        assert!(chunks.len() >= 2);
        for chunk in &chunks {
            assert!(chunk.len() <= 1900);
        }
        // Total content preserved
        let total: String = chunks.join("");
        assert_eq!(total.len(), 3000);
    }

    #[test]
    fn split_on_newlines() {
        let mut text = String::new();
        for i in 0..100 {
            text.push_str(&format!("Line {} with some content here\n", i));
        }
        let chunks = split_message(&text);
        assert!(chunks.len() >= 2);
        for chunk in &chunks {
            assert!(chunk.len() <= 1900);
        }
    }

    #[test]
    fn stream_context_creation() {
        let ctx = StreamContext::new(Some(123), true);
        assert!(ctx.live_edit.is_some());
        assert_eq!(ctx.live_edit.as_ref().unwrap().channel_id, 123);
        assert!(!ctx.got_result);

        let ctx_no_stream = StreamContext::new(Some(123), false);
        assert!(ctx_no_stream.live_edit.is_none());
    }
}
