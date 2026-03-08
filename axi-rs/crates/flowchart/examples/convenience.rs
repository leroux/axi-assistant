//! Example: dispatch loop pattern for running a flowchart.
//!
//! This shows how a consumer would use the `GraphWalker` state machine.
//! In a real application, the Query/Bash/SubCommand/Clear handlers would
//! call into actual agent sessions, shell processes, etc.

use std::collections::HashMap;

use flowchart::{parse_command, Action, GraphWalker};

fn main() {
    let json = include_str!("../tests/fixtures/story.json");
    let cmd = parse_command(json).expect("failed to parse command");

    let mut vars: HashMap<String, String> = HashMap::new();
    vars.insert("$1".into(), "a brave knight and a clever dragon".into());

    let mut walker = GraphWalker::new(cmd.flowchart, vars);
    let mut action = walker.start();

    loop {
        match action {
            Action::Done { .. } => {
                println!("Flowchart complete!");
                break;
            }
            Action::Error { message } => {
                eprintln!("Error: {message}");
                break;
            }
            Action::Query {
                block_name, prompt, ..
            } => {
                println!("--- [{block_name}] ---");
                let truncated: String = prompt.chars().take(100).collect();
                println!("Prompt: {truncated}");
                // In a real app: send prompt to agent, get response
                let mock_response = format!("Mock response for: {block_name}");
                println!("Response: {mock_response}");
                action = walker.feed(&mock_response);
            }
            Action::Bash {
                block_name,
                command,
                ..
            } => {
                println!("--- [{block_name}] bash ---");
                println!("Command: {command}");
                // In a real app: run the shell command
                action = walker.feed_bash("mock output", 0);
            }
            Action::SubCommand {
                block_name,
                command_name,
                ..
            } => {
                println!("--- [{block_name}] sub-command: {command_name} ---");
                // In a real app: resolve & run the sub-command's flowchart
                action = walker.feed_subcommand("{}", HashMap::new());
            }
            Action::Clear { block_name, .. } => {
                println!("--- [{block_name}] clear session ---");
                action = walker.feed("");
            }
            Action::Exit {
                block_name,
                exit_code,
                ..
            } => {
                println!("--- [{block_name}] exit with code {exit_code} ---");
                break;
            }
            Action::Spawn {
                block_name,
                agent_name,
                ..
            } => {
                println!("--- [{block_name}] spawn agent: {} ---", agent_name.as_deref().unwrap_or("default"));
                action = walker.feed("");
            }
            Action::Wait { block_name, .. } => {
                println!("--- [{block_name}] wait for spawned agents ---");
                action = walker.feed("");
            }
        }
    }

    println!("\nFinal variables:");
    for (k, v) in walker.variables() {
        let display = if v.len() > 80 { &v[..80] } else { v };
        println!("  {k} = {display}");
    }
}
