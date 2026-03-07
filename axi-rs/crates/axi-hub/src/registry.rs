//! Agent session registry — create, destroy, and look up sessions.

use tracing::info;

use crate::hub::AgentHub;
use crate::lifecycle;
use crate::types::AgentSession;

// ---------------------------------------------------------------------------
// Basic CRUD
// ---------------------------------------------------------------------------

pub async fn register_session(hub: &AgentHub, session: AgentSession) {
    let name = session.name.clone();
    let mut sessions = hub.sessions.lock().await;
    sessions.insert(name.clone(), session);
    info!("Session '{}' registered", name);
}

pub async fn unregister_session(hub: &AgentHub, name: &str) -> Option<AgentSession> {
    let mut sessions = hub.sessions.lock().await;
    let session = sessions.remove(name);
    if session.is_some() {
        info!("Session '{}' unregistered", name);
    }
    session
}

pub async fn get_session_names(hub: &AgentHub) -> Vec<String> {
    let sessions = hub.sessions.lock().await;
    sessions.keys().cloned().collect()
}

// ---------------------------------------------------------------------------
// End session
// ---------------------------------------------------------------------------

pub async fn end_session(hub: &AgentHub, name: &str) {
    let has_client = {
        let sessions = hub.sessions.lock().await;
        sessions
            .get(name)
            .is_some_and(|s| s.client.is_some())
    };

    if has_client {
        (hub.disconnect_client)(name).await;
        let mut sessions = hub.sessions.lock().await;
        if let Some(session) = sessions.get_mut(name) {
            session.client = None;
        }
        hub.scheduler.release_slot(name).await;
    }

    let mut sessions = hub.sessions.lock().await;
    sessions.remove(name);
    info!("Session '{}' ended", name);
}

// ---------------------------------------------------------------------------
// Rebuild / reset
// ---------------------------------------------------------------------------

pub async fn rebuild_session(
    hub: &AgentHub,
    name: &str,
    cwd: Option<String>,
    session_id: Option<String>,
    system_prompt: Option<serde_json::Value>,
    mcp_servers: Option<serde_json::Value>,
) -> AgentSession {
    // Collect old state before ending
    let (old_cwd, old_prompt, old_mcp, _old_frontend) = {
        let sessions = hub.sessions.lock().await;
        sessions
            .get(name)
            .map(|old| {
                (
                    old.cwd.clone(),
                    old.system_prompt.clone(),
                    old.mcp_servers.clone(),
                    None::<Box<dyn std::any::Any + Send + Sync>>, // Can't clone Box<dyn Any>
                )
            })
            .unwrap_or_default()
    };

    end_session(hub, name).await;

    let mut new_session = AgentSession::new(name.to_string());
    new_session.cwd = cwd.unwrap_or(old_cwd);
    new_session.system_prompt = system_prompt.or(old_prompt);
    new_session.mcp_servers = if mcp_servers.is_some() {
        mcp_servers
    } else {
        old_mcp
    };
    new_session.session_id = session_id;

    let mut sessions = hub.sessions.lock().await;
    sessions.insert(name.to_string(), new_session);

    // Return a reference-free copy of the state we need
    info!("Session '{}' rebuilt", name);

    // We need to return the session but it's behind the lock.
    // Create a new one with the same config.
    drop(sessions);
    let sessions = hub.sessions.lock().await;
    // Can't return a reference, caller should use hub.sessions.lock() to access
    // For now, return a fresh default — callers should use get_session after this
    let mut result = AgentSession::new(name.to_string());
    if let Some(s) = sessions.get(name) {
        result.cwd = s.cwd.clone();
        result.system_prompt = s.system_prompt.clone();
        result.mcp_servers = s.mcp_servers.clone();
        result.session_id = s.session_id.clone();
    }
    result
}

pub async fn reset_session(hub: &AgentHub, name: &str, cwd: Option<String>) -> AgentSession {
    let new_session = rebuild_session(hub, name, cwd.clone(), None, None, None).await;
    info!("Session '{}' reset (sleeping, cwd={:?})", name, cwd);
    new_session
}

// ---------------------------------------------------------------------------
// Reclaim
// ---------------------------------------------------------------------------

pub async fn reclaim_agent_name(hub: &AgentHub, name: &str) {
    let exists = {
        let sessions = hub.sessions.lock().await;
        sessions.contains_key(name)
    };
    if !exists {
        return;
    }
    info!(
        "Reclaiming agent name '{}' — terminating existing session",
        name
    );
    lifecycle::sleep_agent(hub, name, true).await;
    let mut sessions = hub.sessions.lock().await;
    sessions.remove(name);
}

// ---------------------------------------------------------------------------
// Spawn
// ---------------------------------------------------------------------------

/// Parameters for spawning a new agent session.
#[derive(Default)]
pub struct SpawnRequest {
    pub name: String,
    pub cwd: String,
    pub agent_type: Option<String>,
    pub resume: Option<String>,
    pub system_prompt: Option<serde_json::Value>,
    pub mcp_servers: Option<serde_json::Value>,
    pub mcp_server_names: Option<Vec<String>>,
}

pub async fn spawn_agent(hub: &AgentHub, req: SpawnRequest) -> AgentSession {
    std::fs::create_dir_all(&req.cwd).ok();

    let name = req.name;
    let cwd = req.cwd;
    let resume = req.resume;

    let mut session = AgentSession::new(name.clone());
    session.agent_type = req.agent_type.unwrap_or_else(|| "claude_code".to_string());
    session.cwd = cwd.clone();
    session.system_prompt = req.system_prompt;
    session.session_id = resume.clone();
    session.mcp_servers = req.mcp_servers;
    session.mcp_server_names = req.mcp_server_names;

    {
        let mut sessions = hub.sessions.lock().await;
        sessions.insert(name.clone(), session);
    }

    info!(
        "Agent '{}' registered (cwd={}, resume={:?})",
        name, cwd, resume
    );

    hub.callbacks.on_spawn(&name).await;

    // Return a copy of session state
    let sessions = hub.sessions.lock().await;
    let mut result = AgentSession::new(name.clone());
    if let Some(s) = sessions.get(&name) {
        result.cwd = s.cwd.clone();
        result.agent_type = s.agent_type.clone();
        result.session_id = s.session_id.clone();
        result.system_prompt = s.system_prompt.clone();
        result.mcp_servers = s.mcp_servers.clone();
        result.mcp_server_names = s.mcp_server_names.clone();
    }
    result
}

// ---------------------------------------------------------------------------
// Agent config persistence
// ---------------------------------------------------------------------------

/// Agent config stored alongside agent data.
#[derive(serde::Serialize, serde::Deserialize, Default)]
pub struct AgentConfig {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mcp_server_names: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub packs: Option<Vec<String>>,
}

/// Save agent config to `<cwd>/agent_config.json`.
pub fn save_agent_config(cwd: &str, config: &AgentConfig) {
    let path = std::path::Path::new(cwd).join("agent_config.json");
    if let Ok(json) = serde_json::to_string_pretty(config) {
        if let Err(e) = std::fs::write(&path, json) {
            tracing::warn!("Failed to save agent config to {}: {}", path.display(), e);
        }
    }
}

/// Load agent config from `<cwd>/agent_config.json`.
pub fn load_agent_config(cwd: &str) -> Option<AgentConfig> {
    let path = std::path::Path::new(cwd).join("agent_config.json");
    let data = std::fs::read_to_string(&path).ok()?;
    serde_json::from_str(&data).ok()
}
