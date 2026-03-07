//! Integration tests for `CliSession`.
//!
//! Tests using arbitrary subprocesses run always.
//! Tests using the real Claude CLI are gated behind the `integration` feature.

use claudewire::session::CliSession;
use tokio::process::Command;

// ---------------------------------------------------------------------------
// Subprocess tests (no Claude CLI needed)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn session_reads_ndjson_from_subprocess() {
    let mut cmd = Command::new("bash");
    cmd.arg("-c").arg(
        r#"echo '{"type":"system","subtype":"init"}'
echo '{"type":"result","subtype":"success","is_error":false,"duration_ms":0,"duration_api_ms":0,"num_turns":0,"session_id":"s","uuid":"u"}'
"#,
    );

    let mut session = CliSession::from_command(cmd, "test-ndjson".into(), None).unwrap();

    let msg1 = session.read_message().await.unwrap();
    assert_eq!(msg1["type"], "system");
    assert_eq!(msg1["subtype"], "init");

    let msg2 = session.read_message().await.unwrap();
    assert_eq!(msg2["type"], "result");

    // Process exits, should get None
    let msg3 = session.read_message().await;
    assert!(msg3.is_none());
    assert!(session.cli_exited());
}

#[tokio::test]
async fn session_detects_exit() {
    let mut cmd = Command::new("bash");
    cmd.arg("-c").arg("exit 42");

    let mut session = CliSession::from_command(cmd, "test-exit".into(), None).unwrap();

    let msg = session.read_message().await;
    assert!(msg.is_none());
    assert!(session.cli_exited());
    assert_eq!(session.exit_code(), Some(42));
}

#[tokio::test]
async fn session_stdin_write() {
    // `cat` echoes stdin to stdout — write JSON, read it back
    let mut cmd = Command::new("bash");
    cmd.arg("-c").arg("cat");

    let mut session = CliSession::from_command(cmd, "test-stdin".into(), None).unwrap();

    let msg = serde_json::json!({"type": "user", "content": "hello"});
    session.write(&msg.to_string()).await.unwrap();

    let response = session.read_message().await.unwrap();
    assert_eq!(response["type"], "user");
    assert_eq!(response["content"], "hello");

    session.stop().await;
}

#[tokio::test]
async fn session_stop_kills_process() {
    let mut cmd = Command::new("bash");
    cmd.arg("-c").arg("sleep 300");

    let mut session = CliSession::from_command(cmd, "test-stop".into(), None).unwrap();

    session.stop().await;
    assert!(session.cli_exited());

    // read_message after stop returns None
    let msg = session.read_message().await;
    assert!(msg.is_none());
}

#[tokio::test]
async fn session_stderr_callback() {
    use std::sync::{Arc, Mutex};

    let stderr_lines: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let lines_clone = stderr_lines.clone();

    let callback: claudewire::session::StderrCallback = Box::new(move |line| {
        lines_clone.lock().unwrap().push(line.to_string());
    });

    let mut cmd = Command::new("bash");
    cmd.arg("-c").arg("echo 'debug: starting up' >&2; echo 'warn: something' >&2");

    let mut session = CliSession::from_command(cmd, "test-stderr".into(), Some(callback)).unwrap();

    // Wait for process to finish
    while session.read_message().await.is_some() {}

    let lines = stderr_lines.lock().unwrap();
    assert!(lines.len() >= 2, "expected at least 2 stderr lines, got {}", lines.len());
    assert!(lines[0].contains("starting up"));
    assert!(lines[1].contains("something"));
}

// ---------------------------------------------------------------------------
// Real Claude CLI tests (gated behind `integration` feature)
// ---------------------------------------------------------------------------

#[cfg(feature = "integration")]
mod claude_cli {
    use super::*;
    use claudewire::config::Config;

    fn has_claude() -> bool {
        which::which("claude").is_ok()
    }

    fn haiku_config() -> Config {
        Config {
            model: "haiku".into(),
            permission_mode: "plan".into(),
            verbose: true,
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn spawn_claude_gets_system_init() {
        if !has_claude() {
            eprintln!("skipping: claude not on PATH");
            return;
        }

        let config = haiku_config();
        let mut session = CliSession::spawn(&config, "test-init".into(), None).unwrap();

        // First message should be system.init
        let msg = tokio::time::timeout(
            std::time::Duration::from_secs(30),
            session.read_message(),
        )
        .await
        .expect("timed out waiting for system.init")
        .expect("stream ended before system.init");

        assert_eq!(msg["type"], "system");
        assert_eq!(msg["subtype"], "init");

        session.stop().await;
    }

    #[tokio::test]
    async fn spawn_claude_full_query() {
        if !has_claude() {
            eprintln!("skipping: claude not on PATH");
            return;
        }

        let config = haiku_config();
        let mut session = CliSession::spawn(&config, "test-query".into(), None).unwrap();

        // Wait for system.init
        let init = tokio::time::timeout(
            std::time::Duration::from_secs(30),
            session.read_message(),
        )
        .await
        .expect("timed out")
        .expect("no init");
        assert_eq!(init["type"], "system");

        // Send a simple query
        let user_msg = serde_json::json!({
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": "Reply with exactly: PONG"},
            "parent_tool_use_id": null,
        });
        session.write(&user_msg.to_string()).await.unwrap();

        // Collect messages until result
        let mut got_assistant = false;
        let mut got_result = false;
        let mut result_text = String::new();

        let deadline = std::time::Duration::from_secs(60);
        let start = std::time::Instant::now();

        while start.elapsed() < deadline {
            let msg = match tokio::time::timeout(
                std::time::Duration::from_secs(30),
                session.read_message(),
            )
            .await
            {
                Ok(Some(m)) => m,
                Ok(None) => break,
                Err(_) => break,
            };

            let msg_type = msg["type"].as_str().unwrap_or("");
            match msg_type {
                "assistant" => got_assistant = true,
                "result" => {
                    got_result = true;
                    result_text = msg["result"].as_str().unwrap_or("").to_string();
                    break;
                }
                _ => {}
            }
        }

        assert!(got_assistant, "expected an assistant message");
        assert!(got_result, "expected a result message");
        assert!(
            result_text.contains("PONG"),
            "expected result to contain PONG, got: {result_text}"
        );

        session.stop().await;
    }

    #[tokio::test]
    async fn spawn_claude_bare_events_filtered() {
        if !has_claude() {
            eprintln!("skipping: claude not on PATH");
            return;
        }

        let config = haiku_config();
        let mut session = CliSession::spawn(&config, "test-bare".into(), None).unwrap();

        // Wait for init
        let init = tokio::time::timeout(
            std::time::Duration::from_secs(30),
            session.read_message(),
        )
        .await
        .expect("timed out")
        .expect("no init");
        assert_eq!(init["type"], "system");

        // Send query
        let user_msg = serde_json::json!({
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": "Say hi"},
            "parent_tool_use_id": null,
        });
        session.write(&user_msg.to_string()).await.unwrap();

        // Verify no bare stream events leak through
        let bare_types = ["message_start", "message_delta", "message_stop",
                         "content_block_start", "content_block_delta", "content_block_stop"];

        let deadline = std::time::Duration::from_secs(60);
        let start = std::time::Instant::now();

        while start.elapsed() < deadline {
            let msg = match tokio::time::timeout(
                std::time::Duration::from_secs(30),
                session.read_message(),
            )
            .await
            {
                Ok(Some(m)) => m,
                Ok(None) => break,
                Err(_) => break,
            };

            let msg_type = msg["type"].as_str().unwrap_or("");
            assert!(
                !bare_types.contains(&msg_type),
                "bare stream event leaked through: {msg_type}"
            );

            if msg_type == "result" {
                break;
            }
        }

        session.stop().await;
    }
}
