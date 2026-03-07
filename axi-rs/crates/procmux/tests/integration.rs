//! Integration tests for procmux server + client.

use std::collections::HashMap;
use std::time::Duration;

use procmux::client::ProcmuxConnection;
use procmux::server::ProcmuxServer;

#[tokio::test]
async fn spawn_echo_and_read_stdout() {
    let socket_path = "/tmp/procmux-test-echo.sock";
    let _ = std::fs::remove_file(socket_path);

    // Start server in background
    let server = ProcmuxServer::new(socket_path);
    let server_handle = tokio::spawn(async move {
        server.run().await.unwrap();
    });

    // Give the server time to start
    tokio::time::sleep(Duration::from_millis(100)).await;

    // Connect client
    let conn = ProcmuxConnection::connect(socket_path).await.unwrap();

    // Register a process queue
    let mut rx = conn.register_process("test-echo").await;

    // Spawn echo process
    let result = conn
        .send_command(
            "spawn",
            "test-echo",
            vec![
                "bash".to_string(),
                "-c".to_string(),
                r#"echo '{"hello":"world"}'"#.to_string(),
            ],
            HashMap::new(),
            None,
        )
        .await
        .unwrap();

    assert!(result.ok);
    assert!(result.pid.is_some());

    // Subscribe to output
    let sub_result = conn.send_simple_command("subscribe", "test-echo").await.unwrap();
    assert!(sub_result.ok);

    // Read stdout
    let msg = tokio::time::timeout(Duration::from_secs(5), rx.recv())
        .await
        .unwrap()
        .unwrap();

    match msg {
        procmux::client::ProcessMsg::Stdout(stdout) => {
            assert_eq!(stdout.data["hello"], "world");
        }
        other => panic!("expected Stdout, got {:?}", other),
    }

    // Wait for exit
    let msg = tokio::time::timeout(Duration::from_secs(5), rx.recv())
        .await
        .unwrap()
        .unwrap();

    match msg {
        procmux::client::ProcessMsg::Exit(exit) => {
            // Exit code might be 0 or None depending on timing
        }
        other => panic!("expected Exit, got {:?}", other),
    }

    // Clean up
    conn.close();

    // Kill the server
    server_handle.abort();
    let _ = std::fs::remove_file(socket_path);
}

#[tokio::test]
async fn spawn_and_stdin_stdout_roundtrip() {
    let socket_path = "/tmp/procmux-test-cat.sock";
    let _ = std::fs::remove_file(socket_path);

    let server = ProcmuxServer::new(socket_path);
    let server_handle = tokio::spawn(async move {
        server.run().await.unwrap();
    });
    tokio::time::sleep(Duration::from_millis(100)).await;

    let conn = ProcmuxConnection::connect(socket_path).await.unwrap();
    let mut rx = conn.register_process("test-cat").await;

    // Spawn a cat process that reads stdin and echos it
    let result = conn
        .send_command(
            "spawn",
            "test-cat",
            vec![
                "bash".to_string(),
                "-c".to_string(),
                "while IFS= read -r line; do echo \"$line\"; done".to_string(),
            ],
            HashMap::new(),
            None,
        )
        .await
        .unwrap();

    assert!(result.ok);

    // Subscribe
    conn.send_simple_command("subscribe", "test-cat").await.unwrap();

    // Send some data via stdin
    conn.send_stdin(
        "test-cat",
        serde_json::json!({"type": "user", "content": "ping"}),
    )
    .await
    .unwrap();

    // Read the echoed output
    let msg = tokio::time::timeout(Duration::from_secs(5), rx.recv())
        .await
        .unwrap()
        .unwrap();

    match msg {
        procmux::client::ProcessMsg::Stdout(stdout) => {
            assert_eq!(stdout.data["type"], "user");
            assert_eq!(stdout.data["content"], "ping");
        }
        other => panic!("expected Stdout, got {:?}", other),
    }

    // Kill the process
    let kill_result = conn.send_simple_command("kill", "test-cat").await.unwrap();
    assert!(kill_result.ok);

    // Clean up
    conn.close();
    server_handle.abort();
    let _ = std::fs::remove_file(socket_path);
}

#[tokio::test]
async fn connection_lost_on_server_shutdown() {
    let socket_path = "/tmp/procmux-test-connlost.sock";
    let _ = std::fs::remove_file(socket_path);

    let server = ProcmuxServer::new(socket_path);
    let server_handle = tokio::spawn(async move {
        server.run().await.unwrap();
    });
    tokio::time::sleep(Duration::from_millis(100)).await;

    // Connect client and register a process queue
    let conn = ProcmuxConnection::connect(socket_path).await.unwrap();
    let mut rx = conn.register_process("test-proc").await;

    assert!(conn.is_alive());

    // Kill the server
    server_handle.abort();
    let _ = server_handle.await;

    // The demux loop should detect EOF and signal ConnectionLost
    let msg = tokio::time::timeout(Duration::from_secs(5), rx.recv())
        .await
        .unwrap()
        .unwrap();

    match msg {
        procmux::client::ProcessMsg::ConnectionLost => {}
        other => panic!("expected ConnectionLost, got {:?}", other),
    }

    // is_alive() should now return false
    assert!(!conn.is_alive());

    // A new connection should succeed after restarting the server
    let _ = std::fs::remove_file(socket_path);
    let server2 = ProcmuxServer::new(socket_path);
    let server_handle2 = tokio::spawn(async move {
        server2.run().await.unwrap();
    });
    tokio::time::sleep(Duration::from_millis(100)).await;

    let conn2 = ProcmuxConnection::connect(socket_path).await.unwrap();
    assert!(conn2.is_alive());

    // Verify the new connection works
    let status = conn2.send_simple_command("status", "").await.unwrap();
    assert!(status.ok);

    conn.close();
    conn2.close();
    server_handle2.abort();
    let _ = std::fs::remove_file(socket_path);
}

#[tokio::test]
async fn list_and_status() {
    let socket_path = "/tmp/procmux-test-list.sock";
    let _ = std::fs::remove_file(socket_path);

    let server = ProcmuxServer::new(socket_path);
    let server_handle = tokio::spawn(async move {
        server.run().await.unwrap();
    });
    tokio::time::sleep(Duration::from_millis(100)).await;

    let conn = ProcmuxConnection::connect(socket_path).await.unwrap();

    // Status
    let status = conn.send_simple_command("status", "").await.unwrap();
    assert!(status.ok);
    assert!(status.uptime_seconds.is_some());

    // List (empty)
    let list = conn.send_simple_command("list", "").await.unwrap();
    assert!(list.ok);
    let agents = list.agents.unwrap();
    assert!(agents.as_object().unwrap().is_empty());

    // Spawn a process
    conn.register_process("test-sleep").await;
    let spawn = conn
        .send_command(
            "spawn",
            "test-sleep",
            vec!["sleep".to_string(), "60".to_string()],
            HashMap::new(),
            None,
        )
        .await
        .unwrap();
    assert!(spawn.ok);

    // List (one process)
    let list = conn.send_simple_command("list", "").await.unwrap();
    assert!(list.ok);
    let agents = list.agents.unwrap();
    let agents = agents.as_object().unwrap();
    assert_eq!(agents.len(), 1);
    assert!(agents.contains_key("test-sleep"));
    assert_eq!(agents["test-sleep"]["status"], "running");

    // Kill
    conn.send_simple_command("kill", "test-sleep").await.unwrap();

    conn.close();
    server_handle.abort();
    let _ = std::fs::remove_file(socket_path);
}
