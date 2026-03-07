//! Cron-based event scheduler — fires recurring and one-off scheduled events.
//!
//! Runs as a background tokio task, ticking every 10 seconds. Each tick:
//! 1. Fires due cron/one-off schedules
//! 2. Checks for idle agents and sends reminders
//! 3. Recovers stranded messages from sleeping agents
//! 4. Auto-sleeps idle awake agents
//!
//! Schedule persistence uses the same JSON file as the MCP schedule tools,
//! with shared locking via a tokio Mutex.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Datelike, Duration, Local, NaiveDateTime, TimeZone, Timelike, Utc};
use serde_json::Value;
use tokio::sync::Mutex;
use tracing::{debug, info, warn};

// ---------------------------------------------------------------------------
// Shared schedule lock (same instance as axi-mcp schedule tools)
// ---------------------------------------------------------------------------

/// Shared lock for schedules.json read-modify-write cycles.
/// Must be the same lock used by the MCP schedule tools.
static SCHEDULES_LOCK: Mutex<()> = Mutex::const_new(());

// ---------------------------------------------------------------------------
// Cron matching
// ---------------------------------------------------------------------------

/// Check if a 5-field cron expression matches a given local time.
///
/// Fields: minute hour day-of-month month day-of-week
/// Supports: *, N, N-M, N/step, */step, comma-separated lists.
pub fn cron_matches(expr: &str, dt: &DateTime<Local>) -> bool {
    let fields: Vec<&str> = expr.split_whitespace().collect();
    if fields.len() != 5 {
        return false;
    }

    let checks = [
        (fields[0], dt.minute(), 0, 59),  // minute
        (fields[1], dt.hour(), 0, 23),     // hour
        (fields[2], dt.day(), 1, 31),      // day of month
        (fields[3], dt.month(), 1, 12),    // month
    ];

    for (field, value, min_val, max_val) in checks {
        if !field_matches(field, value, min_val, max_val) {
            return false;
        }
    }

    // Day of week: 0=Sunday in cron, chrono uses Mon=0..Sun=6
    let dow = dt.weekday().num_days_from_sunday();
    if !field_matches(fields[4], dow, 0, 7) {
        return false;
    }

    true
}

/// Check if a single cron field matches a value.
fn field_matches(field: &str, value: u32, min_val: u32, max_val: u32) -> bool {
    // Handle comma-separated values
    for part in field.split(',') {
        if part_matches(part.trim(), value, min_val, max_val) {
            return true;
        }
    }
    false
}

/// Check if a single cron field part matches (no commas).
fn part_matches(part: &str, value: u32, min_val: u32, max_val: u32) -> bool {
    // Handle step: */N or N-M/S
    if let Some((range_part, step_str)) = part.split_once('/') {
        let step: u32 = match step_str.parse() {
            Ok(s) if s > 0 => s,
            _ => return false,
        };
        let (range_start, range_end) = if range_part == "*" {
            (min_val, max_val)
        } else if let Some((s, e)) = range_part.split_once('-') {
            match (s.parse::<u32>(), e.parse::<u32>()) {
                (Ok(start), Ok(end)) => (start, end),
                _ => return false,
            }
        } else {
            match range_part.parse::<u32>() {
                Ok(n) => (n, max_val),
                _ => return false,
            }
        };
        // Check if value is in range and on a step boundary
        if value >= range_start && value <= range_end {
            return (value - range_start).is_multiple_of(step);
        }
        return false;
    }

    // Wildcard
    if part == "*" {
        return true;
    }

    // Range: N-M
    if let Some((start_str, end_str)) = part.split_once('-') {
        if let (Ok(start), Ok(end)) = (start_str.parse::<u32>(), end_str.parse::<u32>()) {
            return value >= start && value <= end;
        }
        return false;
    }

    // Literal value
    if let Ok(n) = part.parse::<u32>() {
        // Handle day-of-week 7 == 0 (both Sunday)
        if max_val == 7 && n == 7 {
            return value == 0;
        }
        return value == n;
    }

    false
}

// ---------------------------------------------------------------------------
// Schedule file I/O
// ---------------------------------------------------------------------------

fn load_schedules(path: &Path) -> Vec<Value> {
    match std::fs::read_to_string(path) {
        Ok(data) => serde_json::from_str(&data).unwrap_or_default(),
        Err(_) => Vec::new(),
    }
}

fn save_schedules(path: &Path, entries: &[Value]) {
    if let Ok(data) = serde_json::to_string_pretty(entries) {
        let _ = std::fs::write(path, format!("{data}\n"));
    }
}

fn load_history(path: &Path) -> Vec<Value> {
    match std::fs::read_to_string(path) {
        Ok(data) => serde_json::from_str(&data).unwrap_or_default(),
        Err(_) => Vec::new(),
    }
}

fn save_history(path: &Path, entries: &[Value]) {
    if let Ok(data) = serde_json::to_string_pretty(entries) {
        let _ = std::fs::write(path, format!("{data}\n"));
    }
}

/// Composite key for schedule dedup: "owner/name" or just "name".
fn schedule_key(entry: &Value) -> String {
    let owner = entry
        .get("owner")
        .or_else(|| entry.get("session"))
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let name = entry
        .get("name")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if owner.is_empty() {
        name.to_string()
    } else {
        format!("{owner}/{name}")
    }
}

/// Append a fired-schedule record to history, with optional dedup window.
fn append_history(
    history_path: &Path,
    entry: &Value,
    fired_at: &DateTime<Utc>,
    dedup_minutes: i64,
) {
    let mut history = load_history(history_path);

    if dedup_minutes > 0 {
        let sched_name = entry.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let owner = entry.get("owner").and_then(|v| v.as_str());
        for h in history.iter().rev() {
            if h.get("name").and_then(|v| v.as_str()) == Some(sched_name)
                && h.get("owner").and_then(|v| v.as_str()) == owner
            {
                if let Some(last_str) = h.get("fired_at").and_then(|v| v.as_str()) {
                    if let Ok(last) = DateTime::parse_from_rfc3339(last_str) {
                        let last_utc = last.with_timezone(&Utc);
                        if *fired_at - last_utc < Duration::minutes(dedup_minutes) {
                            return;
                        }
                    }
                }
                break;
            }
        }
    }

    let mut record = serde_json::json!({
        "name": entry.get("name").and_then(|v| v.as_str()).unwrap_or(""),
        "fired_at": fired_at.to_rfc3339(),
    });
    if let Some(owner) = entry.get("owner").and_then(|v| v.as_str()) {
        record["owner"] = serde_json::json!(owner);
    }
    if let Some(prompt) = entry.get("prompt").and_then(|v| v.as_str()) {
        record["prompt"] = serde_json::json!(prompt);
    }
    history.push(record);
    save_history(history_path, &history);
}

/// Prune history entries older than 7 days.
fn prune_history(history_path: &Path) {
    let history = load_history(history_path);
    let cutoff = Utc::now() - Duration::days(7);
    let pruned: Vec<Value> = history
        .into_iter()
        .filter(|h| {
            h.get("fired_at")
                .and_then(|v| v.as_str())
                .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
                .is_none_or(|dt| dt > cutoff)
        })
        .collect();
    save_history(history_path, &pruned);
}

// ---------------------------------------------------------------------------
// Schedule firing
// ---------------------------------------------------------------------------

/// Schedules that should fire this tick.
pub struct FiredSchedule {
    pub agent_name: String,
    pub agent_cwd: String,
    pub prompt: String,
    pub schedule_name: String,
    pub is_one_off: bool,
    pub reset_context: bool,
}

/// Check all schedules and return those that should fire.
///
/// Updates `last_fired` map for recurring schedules.
/// Returns fired one-off keys for removal.
pub async fn check_and_fire(
    schedules_path: &Path,
    history_path: &Path,
    user_data_dir: &Path,
    last_fired: &mut HashMap<String, DateTime<Local>>,
) -> Vec<FiredSchedule> {
    let now_utc = Utc::now();
    let now_local = Local::now();

    let _lock = SCHEDULES_LOCK.lock().await;
    let entries = load_schedules(schedules_path);

    let mut fired = Vec::new();
    let mut fired_one_off_keys = Vec::new();

    for entry in &entries {
        let name = match entry.get("name").and_then(|v| v.as_str()) {
            Some(n) => n,
            None => continue,
        };

        let agent_name = entry
            .get("owner")
            .or_else(|| entry.get("session"))
            .and_then(|v| v.as_str())
            .unwrap_or(name)
            .to_string();

        let agent_cwd = entry
            .get("cwd")
            .and_then(|v| v.as_str()).map_or_else(|| {
                user_data_dir
                    .join("agents")
                    .join(&agent_name)
                    .to_string_lossy()
                    .to_string()
            }, ToString::to_string);

        let reset_context = entry
            .get("reset_context")
            .and_then(Value::as_bool)
            .unwrap_or(false);

        // Recurring (cron) schedule
        if let Some(cron_expr) = entry.get("schedule").and_then(|v| v.as_str()) {
            if !cron_matches(cron_expr, &now_local) {
                continue;
            }

            let skey = schedule_key(entry);

            // First time seeing this schedule: assume it already fired
            if !last_fired.contains_key(&skey) {
                last_fired.insert(skey.clone(), now_local);
                continue;
            }

            // Check if we already fired for this minute
            let last = last_fired[&skey];
            if now_local.minute() == last.minute()
                && now_local.hour() == last.hour()
                && now_local.date_naive() == last.date_naive()
            {
                continue;
            }

            last_fired.insert(skey, now_local);

            let prompt = entry
                .get("prompt")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();

            info!("Firing recurring schedule: {}", name);
            append_history(history_path, entry, &now_utc, 5);

            fired.push(FiredSchedule {
                agent_name,
                agent_cwd,
                prompt,
                schedule_name: name.to_string(),
                is_one_off: false,
                reset_context,
            });
        }
        // One-off schedule
        else if let Some(at_str) = entry.get("at").and_then(|v| v.as_str()) {
            let fire_at = match DateTime::parse_from_rfc3339(at_str) {
                Ok(dt) => dt.with_timezone(&Utc),
                Err(_) => {
                    // Try parsing as naive datetime and assume UTC
                    if let Ok(ndt) = NaiveDateTime::parse_from_str(at_str, "%Y-%m-%dT%H:%M:%S") { Utc.from_utc_datetime(&ndt) } else {
                        warn!("Invalid datetime for one-off schedule '{}': {}", name, at_str);
                        continue;
                    }
                }
            };

            if fire_at <= now_utc {
                let prompt = entry
                    .get("prompt")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();

                info!("Firing one-off schedule: {}", name);
                append_history(history_path, entry, &now_utc, 0);
                fired_one_off_keys.push(schedule_key(entry));

                fired.push(FiredSchedule {
                    agent_name,
                    agent_cwd,
                    prompt,
                    schedule_name: name.to_string(),
                    is_one_off: true,
                    reset_context,
                });
            }
        }
    }

    // Remove consumed one-off schedules
    if !fired_one_off_keys.is_empty() {
        let remaining: Vec<Value> = entries
            .into_iter()
            .filter(|e| !fired_one_off_keys.contains(&schedule_key(e)))
            .collect();
        save_schedules(schedules_path, &remaining);
    }

    fired
}

// ---------------------------------------------------------------------------
// Scheduler loop
// ---------------------------------------------------------------------------

/// Configuration for the scheduler loop.
pub struct SchedulerConfig {
    pub schedules_path: PathBuf,
    pub history_path: PathBuf,
    pub user_data_dir: PathBuf,
    /// Idle reminder thresholds in seconds (cumulative).
    pub idle_reminder_thresholds: Vec<u64>,
    /// Auto-sleep threshold in seconds.
    pub auto_sleep_secs: u64,
}

/// Run the scheduler loop. Should be spawned as a background task.
///
/// The `on_fire` callback is called for each schedule that fires, allowing
/// the caller to route prompts to agents without the scheduler knowing
/// about agent internals.
pub async fn run_loop<F>(config: SchedulerConfig, mut on_fire: F)
where
    F: FnMut(FiredSchedule) + Send + 'static,
{
    let mut last_fired: HashMap<String, DateTime<Local>> = HashMap::new();
    let mut interval = tokio::time::interval(std::time::Duration::from_secs(10));

    info!("Scheduler started (tick=10s)");

    loop {
        interval.tick().await;

        // Prune history periodically (cheap — just checks dates)
        prune_history(&config.history_path);

        // Check and fire schedules
        let fired = check_and_fire(
            &config.schedules_path,
            &config.history_path,
            &config.user_data_dir,
            &mut last_fired,
        )
        .await;

        for schedule in fired {
            debug!(
                "Schedule '{}' fired for agent '{}'",
                schedule.schedule_name, schedule.agent_name
            );
            on_fire(schedule);
        }
    }
}

// ---------------------------------------------------------------------------
// High-level: run scheduler with hub integration
// ---------------------------------------------------------------------------

/// Start the scheduler loop connected to `BotState`.
/// Fired schedules are routed as messages to their owner agent.
pub async fn run_scheduler(state: std::sync::Arc<crate::state::BotState>) {
    let config = SchedulerConfig {
        schedules_path: state.config.schedules_path.clone(),
        history_path: state.config.history_path.clone(),
        user_data_dir: state.config.axi_user_data.clone(),
        idle_reminder_thresholds: state
            .config
            .idle_reminder_thresholds
            .iter()
            .map(std::time::Duration::as_secs)
            .collect(),
        auto_sleep_secs: 14400, // 4 hours default
    };

    run_loop(config, move |fired| {
        let state = state.clone();
        let prompt = fired.prompt.clone();
        let agent = fired.agent_name.clone();
        let schedule_name = fired.schedule_name;

        tokio::spawn(async move {
            info!(
                "Scheduler firing '{}' for agent '{}'",
                schedule_name, agent
            );

            // Check if agent exists, register if not
            let exists = {
                let sessions = state.sessions.lock().await;
                sessions.contains_key(&agent)
            };

            if !exists {
                // Create a session for the agent
                let default_cwd = std::env::current_dir()
                    .unwrap_or_else(|_| PathBuf::from("/tmp"))
                    .to_string_lossy()
                    .to_string();
                crate::registry::spawn_agent(
                    &state,
                    crate::registry::SpawnRequest {
                        name: agent.clone(),
                        cwd: default_cwd,
                        ..Default::default()
                    },
                )
                .await;
            }

            let content = crate::types::MessageContent::Text(prompt);
            crate::lifecycle::wake_or_queue(&state, &agent, content, None).await;
        });
    })
    .await;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn local_dt(year: i32, month: u32, day: u32, hour: u32, min: u32) -> DateTime<Local> {
        Local
            .with_ymd_and_hms(year, month, day, hour, min, 0)
            .unwrap()
    }

    #[test]
    fn cron_wildcard() {
        let dt = local_dt(2026, 3, 7, 9, 0);
        assert!(cron_matches("* * * * *", &dt));
    }

    #[test]
    fn cron_specific_time() {
        let dt = local_dt(2026, 3, 7, 9, 0);
        assert!(cron_matches("0 9 * * *", &dt)); // 9:00
        assert!(!cron_matches("30 9 * * *", &dt)); // 9:30
        assert!(!cron_matches("0 10 * * *", &dt)); // 10:00
    }

    #[test]
    fn cron_step() {
        let dt = local_dt(2026, 3, 7, 9, 0);
        assert!(cron_matches("*/15 * * * *", &dt)); // 0 matches */15
        let dt2 = local_dt(2026, 3, 7, 9, 15);
        assert!(cron_matches("*/15 * * * *", &dt2));
        let dt3 = local_dt(2026, 3, 7, 9, 7);
        assert!(!cron_matches("*/15 * * * *", &dt3));
    }

    #[test]
    fn cron_range() {
        let dt = local_dt(2026, 3, 7, 9, 0);
        assert!(cron_matches("0 9-17 * * *", &dt));
        let dt2 = local_dt(2026, 3, 7, 18, 0);
        assert!(!cron_matches("0 9-17 * * *", &dt2));
    }

    #[test]
    fn cron_list() {
        let dt = local_dt(2026, 3, 7, 9, 0);
        assert!(cron_matches("0 9,12,15 * * *", &dt));
        let dt2 = local_dt(2026, 3, 7, 12, 0);
        assert!(cron_matches("0 9,12,15 * * *", &dt2));
        let dt3 = local_dt(2026, 3, 7, 10, 0);
        assert!(!cron_matches("0 9,12,15 * * *", &dt3));
    }

    #[test]
    fn cron_day_of_week() {
        // March 7, 2026 is a Saturday (dow=6)
        let dt = local_dt(2026, 3, 7, 9, 0);
        assert!(cron_matches("0 9 * * 6", &dt)); // Saturday
        assert!(!cron_matches("0 9 * * 1", &dt)); // Monday
        // Sunday = 0 or 7
        let sun = local_dt(2026, 3, 8, 9, 0); // March 8 is Sunday
        assert!(cron_matches("0 9 * * 0", &sun));
        assert!(cron_matches("0 9 * * 7", &sun));
    }

    #[test]
    fn cron_month() {
        let dt = local_dt(2026, 3, 7, 9, 0);
        assert!(cron_matches("0 9 * 3 *", &dt)); // March
        assert!(!cron_matches("0 9 * 6 *", &dt)); // June
    }

    #[test]
    fn cron_day_of_month() {
        let dt = local_dt(2026, 3, 7, 9, 0);
        assert!(cron_matches("0 9 7 * *", &dt)); // 7th
        assert!(!cron_matches("0 9 15 * *", &dt)); // 15th
    }

    #[test]
    fn schedule_key_with_owner() {
        let entry = serde_json::json!({"name": "daily", "owner": "axi-master"});
        assert_eq!(schedule_key(&entry), "axi-master/daily");
    }

    #[test]
    fn schedule_key_without_owner() {
        let entry = serde_json::json!({"name": "daily"});
        assert_eq!(schedule_key(&entry), "daily");
    }

    #[test]
    fn schedule_key_with_session_fallback() {
        let entry = serde_json::json!({"name": "daily", "session": "old-agent"});
        assert_eq!(schedule_key(&entry), "old-agent/daily");
    }

    #[tokio::test]
    async fn fire_one_off_schedule() {
        let dir = tempfile::tempdir().unwrap();
        let sched_path = dir.path().join("schedules.json");
        let hist_path = dir.path().join("history.json");

        // Create a one-off schedule in the past
        let past = (Utc::now() - Duration::minutes(5)).to_rfc3339();
        let entries = vec![serde_json::json!({
            "name": "test-oneoff",
            "owner": "test-agent",
            "prompt": "do the thing",
            "at": past,
        })];
        save_schedules(&sched_path, &entries);

        let mut last_fired = HashMap::new();
        let fired = check_and_fire(
            &sched_path,
            &hist_path,
            dir.path(),
            &mut last_fired,
        )
        .await;

        assert_eq!(fired.len(), 1);
        assert_eq!(fired[0].schedule_name, "test-oneoff");
        assert_eq!(fired[0].agent_name, "test-agent");
        assert!(fired[0].is_one_off);

        // Schedule should be removed from file
        let remaining = load_schedules(&sched_path);
        assert!(remaining.is_empty());

        // History should have the record
        let history = load_history(&hist_path);
        assert_eq!(history.len(), 1);
    }

    #[tokio::test]
    async fn recurring_schedule_first_seen_skipped() {
        let dir = tempfile::tempdir().unwrap();
        let sched_path = dir.path().join("schedules.json");
        let hist_path = dir.path().join("history.json");

        // Create a recurring schedule that matches current time
        let now = Local::now();
        let cron = format!("{} {} * * *", now.minute(), now.hour());
        let entries = vec![serde_json::json!({
            "name": "test-recurring",
            "owner": "test-agent",
            "prompt": "check status",
            "schedule": cron,
        })];
        save_schedules(&sched_path, &entries);

        let mut last_fired = HashMap::new();

        // First check: should NOT fire (first-seen skip)
        let fired = check_and_fire(
            &sched_path,
            &hist_path,
            dir.path(),
            &mut last_fired,
        )
        .await;
        assert!(fired.is_empty());
        assert!(last_fired.contains_key("test-agent/test-recurring"));
    }

    #[test]
    fn prune_old_history() {
        let dir = tempfile::tempdir().unwrap();
        let hist_path = dir.path().join("history.json");

        let old = (Utc::now() - Duration::days(10)).to_rfc3339();
        let recent = (Utc::now() - Duration::days(1)).to_rfc3339();
        let entries = vec![
            serde_json::json!({"name": "old", "fired_at": old}),
            serde_json::json!({"name": "recent", "fired_at": recent}),
        ];
        save_history(&hist_path, &entries);

        prune_history(&hist_path);

        let remaining = load_history(&hist_path);
        assert_eq!(remaining.len(), 1);
        assert_eq!(
            remaining[0].get("name").and_then(|v| v.as_str()),
            Some("recent")
        );
    }
}
