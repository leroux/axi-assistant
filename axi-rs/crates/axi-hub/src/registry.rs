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
            .map(|s| s.client.is_some())
            .unwrap_or(false)
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

pub async fn spawn_agent(
    hub: &AgentHub,
    name: String,
    cwd: String,
    agent_type: Option<String>,
    resume: Option<String>,
    system_prompt: Option<serde_json::Value>,
    mcp_servers: Option<serde_json::Value>,
) -> AgentSession {
    std::fs::create_dir_all(&cwd).ok();

    let mut session = AgentSession::new(name.clone());
    session.agent_type = agent_type.unwrap_or_else(|| "claude_code".to_string());
    session.cwd = cwd.clone();
    session.system_prompt = system_prompt;
    session.session_id = resume.clone();
    session.mcp_servers = mcp_servers;

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
    }
    result
}
