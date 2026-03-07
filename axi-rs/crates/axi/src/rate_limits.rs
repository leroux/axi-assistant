//! Rate limit tracking, parsing, usage recording, and quota management.

use std::collections::HashMap;
use std::io::Write;

use chrono::{DateTime, Utc};

use crate::types::{RateLimitQuota, SessionUsage};

// ---------------------------------------------------------------------------
// State holder
// ---------------------------------------------------------------------------

pub struct RateLimitTracker {
    pub rate_limited_until: Option<DateTime<Utc>>,
    pub session_usage: HashMap<String, SessionUsage>,
    pub rate_limit_quotas: HashMap<String, RateLimitQuota>,
    pub usage_history_path: Option<String>,
    pub rate_limit_history_path: Option<String>,
}

impl RateLimitTracker {
    pub fn new(
        usage_history_path: Option<String>,
        rate_limit_history_path: Option<String>,
    ) -> Self {
        Self {
            rate_limited_until: None,
            session_usage: HashMap::new(),
            rate_limit_quotas: HashMap::new(),
            usage_history_path,
            rate_limit_history_path,
        }
    }
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/// Parse wait duration from rate limit error text. Returns seconds.
pub fn parse_rate_limit_seconds(text: &str) -> u64 {
    let lower = text.to_lowercase();

    // "in 5 minutes", "after 300 seconds"
    let re1 = regex_lite::Regex::new(
        r"(?:in|after)\s+(\d+)\s*(seconds?|minutes?|mins?|hours?|hrs?)",
    )
    .unwrap();
    if let Some(caps) = re1.captures(&lower) {
        let value: u64 = caps[1].parse().unwrap_or(300);
        let unit = &caps[2];
        if unit.starts_with("min") {
            return value * 60;
        } else if unit.starts_with("hour") || unit.starts_with("hr") {
            return value * 3600;
        }
        return value;
    }

    // "retry after 300"
    let re2 = regex_lite::Regex::new(r"retry\s+after\s+(\d+)").unwrap();
    if let Some(caps) = re2.captures(&lower) {
        return caps[1].parse().unwrap_or(300);
    }

    // "300 seconds"
    let re3 = regex_lite::Regex::new(r"(\d+)\s*(?:seconds?|secs?)").unwrap();
    if let Some(caps) = re3.captures(&lower) {
        return caps[1].parse().unwrap_or(300);
    }

    // "5 minutes"
    let re4 = regex_lite::Regex::new(r"(\d+)\s*(?:minutes?|mins?)").unwrap();
    if let Some(caps) = re4.captures(&lower) {
        let val: u64 = caps[1].parse().unwrap_or(5);
        return val * 60;
    }

    300
}

pub fn format_time_remaining(seconds: u64) -> String {
    if seconds < 60 {
        format!("{seconds}s")
    } else if seconds < 3600 {
        let minutes = seconds / 60;
        let secs = seconds % 60;
        if secs > 0 {
            format!("{minutes}m {secs}s")
        } else {
            format!("{minutes}m")
        }
    } else {
        let hours = seconds / 3600;
        let minutes = (seconds % 3600) / 60;
        if minutes > 0 {
            format!("{hours}h {minutes}m")
        } else {
            format!("{hours}h")
        }
    }
}

// ---------------------------------------------------------------------------
// State accessors
// ---------------------------------------------------------------------------

pub fn is_rate_limited(tracker: &mut RateLimitTracker) -> bool {
    if let Some(until) = tracker.rate_limited_until {
        if Utc::now() >= until {
            tracker.rate_limited_until = None;
            return false;
        }
        return true;
    }
    false
}

pub fn rate_limit_remaining_seconds(tracker: &RateLimitTracker) -> u64 {
    tracker
        .rate_limited_until
        .map_or(0, |until| {
            let remaining = (until - Utc::now()).num_seconds();
            remaining.max(0) as u64
        })
}

// ---------------------------------------------------------------------------
// Usage recording
// ---------------------------------------------------------------------------

#[allow(clippy::too_many_arguments)]
pub fn record_session_usage(
    tracker: &mut RateLimitTracker,
    agent_name: &str,
    session_id: &str,
    cost_usd: f64,
    turns: u64,
    duration_ms: u64,
    input_tokens: u64,
    output_tokens: u64,
) {
    let now = Utc::now();

    let entry = tracker
        .session_usage
        .entry(session_id.to_string())
        .or_insert_with(|| {
            let mut u = SessionUsage::new(agent_name.to_string());
            u.first_query = Some(now);
            u
        });

    entry.queries += 1;
    entry.total_cost_usd += cost_usd;
    entry.total_turns += turns;
    entry.total_duration_ms += duration_ms;
    entry.total_input_tokens += input_tokens;
    entry.total_output_tokens += output_tokens;
    entry.last_query = Some(now);

    if let Some(ref path) = tracker.usage_history_path {
        let record = serde_json::json!({
            "ts": now.to_rfc3339(),
            "agent": agent_name,
            "session_id": session_id,
            "cost_usd": cost_usd,
            "turns": turns,
            "duration_ms": duration_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        });
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
            writeln!(f, "{}", serde_json::to_string(&record).unwrap_or_default()).ok();
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_seconds_basic() {
        assert_eq!(parse_rate_limit_seconds("retry after 300"), 300);
        assert_eq!(parse_rate_limit_seconds("in 5 minutes"), 300);
        assert_eq!(parse_rate_limit_seconds("after 2 hours"), 7200);
        assert_eq!(parse_rate_limit_seconds("wait 30 seconds"), 30);
        assert_eq!(parse_rate_limit_seconds("unknown text"), 300);
    }

    #[test]
    fn format_remaining() {
        assert_eq!(format_time_remaining(30), "30s");
        assert_eq!(format_time_remaining(90), "1m 30s");
        assert_eq!(format_time_remaining(3600), "1h");
        assert_eq!(format_time_remaining(3660), "1h 1m");
    }
}
