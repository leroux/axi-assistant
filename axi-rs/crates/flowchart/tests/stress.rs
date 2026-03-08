//! Exhaustive stress tests for every walker function in isolation and key combos.
//!
//! Goals:
//! - Every public method of `GraphWalker` tested with edge cases
//! - Every `BlockData` variant tested for correct Action yield and variable storage
//! - Combos: multi-block sequences that chain variable → branch → action
//! - Property-based tests for interpolation, condition evaluation, and walker invariants

use std::collections::HashMap;

use flowchart::{
    Action, Block, BlockData, Connection, Flowchart, GraphWalker, VariableType,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn block(name: &str, data: BlockData) -> Block {
    Block {
        name: name.to_owned(),
        data,
        extra: HashMap::new(),
    }
}

fn flowchart(blocks: Vec<(&str, Block)>, connections: Vec<Connection>) -> Flowchart {
    Flowchart {
        name: None,
        blocks: blocks
            .into_iter()
            .map(|(id, b)| (id.to_owned(), b))
            .collect(),
        connections,
        sessions: None,
    }
}

fn conn(source: &str, target: &str) -> Connection {
    Connection {
        source_id: source.into(),
        target_id: target.into(),
        is_true_path: None,
    }
}

fn branch_conn(source: &str, target: &str, is_true: bool) -> Connection {
    Connection {
        source_id: source.into(),
        target_id: target.into(),
        is_true_path: Some(is_true),
    }
}

fn no_vars() -> HashMap<String, String> {
    HashMap::new()
}

fn vars(pairs: &[(&str, &str)]) -> HashMap<String, String> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
}

// Assertion helpers that return owned values to avoid temporary lifetime issues.

#[track_caller]
fn assert_query(action: &Action) -> (String, String) {
    match action {
        Action::Query {
            block_name, prompt, ..
        } => (block_name.clone(), prompt.clone()),
        other => panic!("expected Query, got {other:?}"),
    }
}

#[track_caller]
fn assert_bash(action: &Action) -> (String, String) {
    match action {
        Action::Bash {
            block_name,
            command,
            ..
        } => (block_name.clone(), command.clone()),
        other => panic!("expected Bash, got {other:?}"),
    }
}

#[track_caller]
fn assert_subcommand(action: &Action) -> String {
    match action {
        Action::SubCommand { block_name, .. } => block_name.clone(),
        other => panic!("expected SubCommand, got {other:?}"),
    }
}

#[track_caller]
fn assert_clear(action: &Action) -> String {
    match action {
        Action::Clear { block_name, .. } => block_name.clone(),
        other => panic!("expected Clear, got {other:?}"),
    }
}

#[track_caller]
fn assert_spawn(action: &Action) -> String {
    match action {
        Action::Spawn { block_name, .. } => block_name.clone(),
        other => panic!("expected Spawn, got {other:?}"),
    }
}

#[track_caller]
fn assert_wait(action: &Action) -> String {
    match action {
        Action::Wait { block_name, .. } => block_name.clone(),
        other => panic!("expected Wait, got {other:?}"),
    }
}

#[track_caller]
fn assert_done(action: &Action) {
    assert!(
        matches!(action, Action::Done { .. }),
        "expected Done, got {action:?}"
    );
}

#[track_caller]
fn assert_exit(action: &Action, expected_code: i32) {
    match action {
        Action::Exit { exit_code, .. } => {
            assert_eq!(*exit_code, expected_code, "wrong exit code");
        }
        other => panic!("expected Exit({expected_code}), got {other:?}"),
    }
}

#[track_caller]
fn assert_error(action: &Action) -> String {
    match action {
        Action::Error { message } => message.clone(),
        other => panic!("expected Error, got {other:?}"),
    }
}

#[track_caller]
fn assert_error_contains(action: &Action, needle: &str) {
    let msg = assert_error(action);
    assert!(
        msg.contains(needle),
        "error message {msg:?} does not contain {needle:?}"
    );
}

const fn is_done(action: &Action) -> bool {
    matches!(action, Action::Done { .. })
}

const fn is_query(action: &Action) -> bool {
    matches!(action, Action::Query { .. })
}

// ===========================================================================
// 1. start() — every possible first block after start
// ===========================================================================

mod start {
    use super::*;

    #[test]
    fn no_start_block_returns_done() {
        let fc = flowchart(vec![("e", block("End", BlockData::End))], vec![]);
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
    }

    #[test]
    fn start_with_no_outgoing_edge_returns_done() {
        let fc = flowchart(vec![("s", block("S", BlockData::Start))], vec![]);
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
    }

    #[test]
    fn start_to_end_returns_done() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
    }

    #[test]
    fn start_to_prompt_yields_query() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        BlockData::Prompt {
                            prompt: "hello".into(),
                            output_variable: Some("out".into()),
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let a = w.start();
        let (name, prompt) = assert_query(&a);
        assert_eq!(name, "Ask");
        assert_eq!(prompt, "hello");
    }

    #[test]
    fn start_to_bash_yields_bash() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "b",
                    block(
                        "Run",
                        BlockData::Bash {
                            command: "ls".into(),
                            output_variable: None,
                            working_directory: None,
                            continue_on_error: None,
                            exit_code_variable: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "b"), conn("b", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let a = w.start();
        let (name, cmd) = assert_bash(&a);
        assert_eq!(name, "Run");
        assert_eq!(cmd, "ls");
    }

    #[test]
    fn start_to_variable_processes_silently_then_end() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "v",
                    block(
                        "Set",
                        BlockData::Variable {
                            variable_name: "x".into(),
                            variable_value: "42".into(),
                            variable_type: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "v"), conn("v", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("x").unwrap(), "42");
    }

    #[test]
    fn start_to_branch_true_path() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "b",
                    block("Check", BlockData::Branch { condition: "flag".into() }),
                ),
                (
                    "yes",
                    block(
                        "Yes",
                        BlockData::Prompt {
                            prompt: "truthy".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("no", block("No", BlockData::End)),
            ],
            vec![
                conn("s", "b"),
                branch_conn("b", "yes", true),
                branch_conn("b", "no", false),
                conn("yes", "no"),
            ],
        );
        let mut w = GraphWalker::new(fc, vars(&[("flag", "true")]));
        let a = w.start();
        let (name, _) = assert_query(&a);
        assert_eq!(name, "Yes");
    }

    #[test]
    fn start_to_branch_false_path() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "b",
                    block("Check", BlockData::Branch { condition: "flag".into() }),
                ),
                (
                    "yes",
                    block(
                        "Yes",
                        BlockData::Prompt {
                            prompt: "truthy".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("no", block("No", BlockData::End)),
            ],
            vec![
                conn("s", "b"),
                branch_conn("b", "yes", true),
                branch_conn("b", "no", false),
                conn("yes", "no"),
            ],
        );
        let mut w = GraphWalker::new(fc, vars(&[("flag", "false")]));
        assert_done(&w.start());
    }

    #[test]
    fn start_to_exit_yields_exit() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "x",
                    block("Die", BlockData::Exit { exit_code: Some(42) }),
                ),
            ],
            vec![conn("s", "x")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_exit(&w.start(), 42);
    }

    #[test]
    fn start_to_exit_default_code() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("x", block("Die", BlockData::Exit { exit_code: None })),
            ],
            vec![conn("s", "x")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_exit(&w.start(), 0);
    }

    #[test]
    fn start_to_refresh_yields_clear() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "r",
                    block(
                        "Reset",
                        BlockData::Refresh {
                            target_session: Some("main".into()),
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "r"), conn("r", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let a = w.start();
        let name = assert_clear(&a);
        assert_eq!(name, "Reset");
    }

    #[test]
    fn start_to_subcommand_yields_subcommand() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "c",
                    block(
                        "Run Sub",
                        BlockData::Command {
                            command_name: "deploy".into(),
                            arguments: Some("prod".into()),
                            inherit_variables: Some(true),
                            merge_output: Some(false),
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "c"), conn("c", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let a = w.start();
        match &a {
            Action::SubCommand {
                command_name,
                arguments,
                inherit_variables,
                merge_output,
                ..
            } => {
                assert_eq!(command_name, "deploy");
                assert_eq!(arguments, "prod");
                assert!(*inherit_variables);
                assert!(!(*merge_output));
            }
            other => panic!("expected SubCommand, got {other:?}"),
        }
    }

    #[test]
    fn start_to_spawn_yields_spawn() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "sp",
                    block(
                        "Agent",
                        BlockData::Spawn {
                            agent_name: Some("worker".into()),
                            command_name: Some("build".into()),
                            arguments: Some("{{target}}".into()),
                            inherit_variables: Some(true),
                            exit_code_variable: Some("rc".into()),
                            config_file: Some("agent.toml".into()),
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "sp"), conn("sp", "e")],
        );
        let mut w = GraphWalker::new(fc, vars(&[("target", "release")]));
        let a = w.start();
        match &a {
            Action::Spawn {
                agent_name,
                command_name,
                arguments,
                inherit_variables,
                exit_code_variable,
                config_file,
                ..
            } => {
                assert_eq!(agent_name.as_deref(), Some("worker"));
                assert_eq!(command_name.as_deref(), Some("build"));
                assert_eq!(arguments, "release");
                assert!(*inherit_variables);
                assert_eq!(exit_code_variable.as_deref(), Some("rc"));
                assert_eq!(config_file.as_deref(), Some("agent.toml"));
            }
            other => panic!("expected Spawn, got {other:?}"),
        }
    }

    #[test]
    fn start_to_wait_yields_wait() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("w", block("Sync", BlockData::Wait)),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "w"), conn("w", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let a = w.start();
        let name = assert_wait(&a);
        assert_eq!(name, "Sync");
    }

    #[test]
    fn start_chains_through_multiple_silent_blocks() {
        // start → var(x=1) → var(y=2) → var(z=3) → end
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "v1",
                    block(
                        "X",
                        BlockData::Variable {
                            variable_name: "x".into(),
                            variable_value: "1".into(),
                            variable_type: None,
                        },
                    ),
                ),
                (
                    "v2",
                    block(
                        "Y",
                        BlockData::Variable {
                            variable_name: "y".into(),
                            variable_value: "2".into(),
                            variable_type: None,
                        },
                    ),
                ),
                (
                    "v3",
                    block(
                        "Z",
                        BlockData::Variable {
                            variable_name: "z".into(),
                            variable_value: "3".into(),
                            variable_type: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![
                conn("s", "v1"),
                conn("v1", "v2"),
                conn("v2", "v3"),
                conn("v3", "e"),
            ],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("x").unwrap(), "1");
        assert_eq!(w.variables().get("y").unwrap(), "2");
        assert_eq!(w.variables().get("z").unwrap(), "3");
    }

    #[test]
    fn start_to_missing_block_returns_error() {
        let fc = flowchart(
            vec![("s", block("S", BlockData::Start))],
            vec![conn("s", "ghost")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_error_contains(&w.start(), "Block not found");
    }
}

// ===========================================================================
// 2. feed() — output variable storage and advancement
// ===========================================================================

mod feed {
    use super::*;

    #[test]
    fn stores_result_in_output_var() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        BlockData::Prompt {
                            prompt: "q".into(),
                            output_variable: Some("answer".into()),
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        w.feed("the response");
        assert_eq!(w.variables().get("answer").unwrap(), "the response");
    }

    #[test]
    fn no_output_var_does_not_store() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        BlockData::Prompt {
                            prompt: "q".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        assert_done(&w.feed("ignored"));
        assert!(w.variables().is_empty());
    }

    #[test]
    fn feed_advances_through_silent_blocks_to_next_action() {
        // prompt → var(x=1) → prompt → end
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "p1",
                    block(
                        "First",
                        BlockData::Prompt {
                            prompt: "q1".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                (
                    "v",
                    block(
                        "Set",
                        BlockData::Variable {
                            variable_name: "x".into(),
                            variable_value: "hello".into(),
                            variable_type: None,
                        },
                    ),
                ),
                (
                    "p2",
                    block(
                        "Second",
                        BlockData::Prompt {
                            prompt: "q2".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![
                conn("s", "p1"),
                conn("p1", "v"),
                conn("v", "p2"),
                conn("p2", "e"),
            ],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start(); // p1
        let a = w.feed("r1");
        let (name, _) = assert_query(&a);
        assert_eq!(name, "Second");
        assert_eq!(w.variables().get("x").unwrap(), "hello");
    }

    #[test]
    fn feed_returns_done_when_no_outgoing_edge() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        BlockData::Prompt {
                            prompt: "q".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
            ],
            vec![conn("s", "p")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        assert_done(&w.feed("r"));
    }

    #[test]
    fn feed_on_clear_advances() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "r",
                    block("Reset", BlockData::Refresh { target_session: None }),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "r"), conn("r", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        assert_done(&w.feed(""));
    }

    #[test]
    fn feed_on_wait_advances() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("w", block("Sync", BlockData::Wait)),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "w"), conn("w", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        assert_done(&w.feed(""));
    }

    #[test]
    fn feed_on_spawn_advances() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "sp",
                    block(
                        "Agent",
                        BlockData::Spawn {
                            agent_name: None,
                            command_name: Some("build".into()),
                            arguments: None,
                            inherit_variables: None,
                            exit_code_variable: None,
                            config_file: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "sp"), conn("sp", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        assert_done(&w.feed(""));
    }

    #[test]
    fn feed_stores_result_then_used_in_next_prompt() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "p1",
                    block(
                        "Get Name",
                        BlockData::Prompt {
                            prompt: "name?".into(),
                            output_variable: Some("name".into()),
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                (
                    "p2",
                    block(
                        "Greet",
                        BlockData::Prompt {
                            prompt: "Hello {{name}}!".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "p1"), conn("p1", "p2"), conn("p2", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        let a = w.feed("Alice");
        let (_, prompt) = assert_query(&a);
        assert_eq!(prompt, "Hello Alice!");
    }
}

// ===========================================================================
// 3. feed_bash() — stdout storage, exit code, continue_on_error
// ===========================================================================

mod feed_bash {
    use super::*;

    fn bash_then_end(
        output_var: Option<&str>,
        exit_code_var: Option<&str>,
        continue_on_error: Option<bool>,
    ) -> Flowchart {
        flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "b",
                    block(
                        "Run",
                        BlockData::Bash {
                            command: "cmd".into(),
                            output_variable: output_var.map(Into::into),
                            working_directory: None,
                            continue_on_error,
                            exit_code_variable: exit_code_var.map(Into::into),
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "b"), conn("b", "e")],
        )
    }

    #[test]
    fn stores_stdout_in_output_var_trimmed() {
        let fc = bash_then_end(Some("out"), None, None);
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        w.feed_bash("  hello world  \n", 0);
        assert_eq!(w.variables().get("out").unwrap(), "hello world");
    }

    #[test]
    fn empty_stdout_stored_as_empty() {
        let fc = bash_then_end(Some("out"), None, None);
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        w.feed_bash("", 0);
        assert_eq!(w.variables().get("out").unwrap(), "");
    }

    #[test]
    fn stores_exit_code_in_var() {
        let fc = bash_then_end(None, Some("rc"), Some(true));
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        w.feed_bash("", 42);
        assert_eq!(w.variables().get("rc").unwrap(), "42");
    }

    #[test]
    fn stores_both_stdout_and_exit_code() {
        let fc = bash_then_end(Some("out"), Some("rc"), Some(true));
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        w.feed_bash("output\n", 7);
        assert_eq!(w.variables().get("out").unwrap(), "output");
        assert_eq!(w.variables().get("rc").unwrap(), "7");
    }

    #[test]
    fn nonzero_exit_without_continue_returns_error() {
        let fc = bash_then_end(None, None, Some(false));
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        assert_error_contains(&w.feed_bash("fail", 1), "exit code 1");
    }

    #[test]
    fn nonzero_exit_default_continue_false_returns_error() {
        let fc = bash_then_end(None, None, None);
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        assert_error_contains(&w.feed_bash("fail", 127), "exit code 127");
    }

    #[test]
    fn nonzero_exit_with_continue_on_error_advances() {
        let fc = bash_then_end(None, None, Some(true));
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        assert_done(&w.feed_bash("fail", 1));
    }

    #[test]
    fn zero_exit_always_advances() {
        let fc = bash_then_end(None, None, None);
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        assert_done(&w.feed_bash("ok", 0));
    }

    #[test]
    fn exit_code_stored_even_on_continue_error() {
        let fc = bash_then_end(None, Some("rc"), Some(true));
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        w.feed_bash("", 99);
        assert_eq!(w.variables().get("rc").unwrap(), "99");
    }

    #[test]
    fn negative_exit_code() {
        let fc = bash_then_end(None, Some("rc"), Some(true));
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        w.feed_bash("", -1);
        assert_eq!(w.variables().get("rc").unwrap(), "-1");
    }

    #[test]
    fn bash_command_interpolation() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "b",
                    block(
                        "Run",
                        BlockData::Bash {
                            command: "deploy {{env}} $1".into(),
                            output_variable: None,
                            working_directory: None,
                            continue_on_error: None,
                            exit_code_variable: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "b"), conn("b", "e")],
        );
        let mut w = GraphWalker::new(fc, vars(&[("env", "staging"), ("$1", "fast")]));
        let a = w.start();
        let (_, cmd) = assert_bash(&a);
        assert_eq!(cmd, "deploy staging fast");
    }

    #[test]
    fn bash_working_directory_passed_through() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "b",
                    block(
                        "Run",
                        BlockData::Bash {
                            command: "ls".into(),
                            output_variable: None,
                            working_directory: Some("/tmp".into()),
                            continue_on_error: None,
                            exit_code_variable: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "b"), conn("b", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        match w.start() {
            Action::Bash { working_directory, .. } => {
                assert_eq!(working_directory.as_deref(), Some("/tmp"));
            }
            other => panic!("expected Bash, got {other:?}"),
        }
    }
}

// ===========================================================================
// 4. feed_subcommand() — result storage, child variable merge
// ===========================================================================

mod feed_subcommand {
    use super::*;

    fn subcmd_then_end() -> Flowchart {
        flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "c",
                    block(
                        "Sub",
                        BlockData::Command {
                            command_name: "child".into(),
                            arguments: None,
                            inherit_variables: None,
                            merge_output: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "c"), conn("c", "e")],
        )
    }

    #[test]
    fn merges_child_variables() {
        let fc = subcmd_then_end();
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        let child_vars = vars(&[("result", "success"), ("count", "5")]);
        w.feed_subcommand("{}", child_vars);
        assert_eq!(w.variables().get("result").unwrap(), "success");
        assert_eq!(w.variables().get("count").unwrap(), "5");
    }

    #[test]
    fn skips_positional_args_from_child() {
        let fc = subcmd_then_end();
        let mut w = GraphWalker::new(fc, vars(&[("$1", "parent_arg")]));
        let _ = w.start();
        let child_vars = vars(&[("$1", "child_arg"), ("$2", "child_arg2"), ("data", "ok")]);
        w.feed_subcommand("{}", child_vars);
        assert_eq!(w.variables().get("$1").unwrap(), "parent_arg");
        assert!(w.variables().get("$2").is_none());
        assert_eq!(w.variables().get("data").unwrap(), "ok");
    }

    #[test]
    fn child_vars_overwrite_parent_vars() {
        let fc = subcmd_then_end();
        let mut w = GraphWalker::new(fc, vars(&[("x", "old")]));
        let _ = w.start();
        w.feed_subcommand("{}", vars(&[("x", "new")]));
        assert_eq!(w.variables().get("x").unwrap(), "new");
    }

    #[test]
    fn empty_child_vars_is_fine() {
        let fc = subcmd_then_end();
        let mut w = GraphWalker::new(fc, vars(&[("x", "keep")]));
        let _ = w.start();
        assert_done(&w.feed_subcommand("{}", HashMap::new()));
        assert_eq!(w.variables().get("x").unwrap(), "keep");
    }

    #[test]
    fn subcommand_default_flags() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "c",
                    block(
                        "Sub",
                        BlockData::Command {
                            command_name: "test".into(),
                            arguments: None,
                            inherit_variables: None,
                            merge_output: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "c"), conn("c", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        match w.start() {
            Action::SubCommand {
                inherit_variables,
                merge_output,
                arguments,
                ..
            } => {
                assert!(!inherit_variables);
                assert!(!merge_output);
                assert!(arguments.is_empty());
            }
            other => panic!("expected SubCommand, got {other:?}"),
        }
    }
}

// ===========================================================================
// 5. variables() / variables_mut()
// ===========================================================================

mod variables_access {
    use super::*;

    #[test]
    fn initial_vars_visible() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "e")],
        );
        let w = GraphWalker::new(fc, vars(&[("x", "1"), ("y", "2")]));
        assert_eq!(w.variables().len(), 2);
        assert_eq!(w.variables().get("x").unwrap(), "1");
    }

    #[test]
    fn variables_mut_inject_before_branch() {
        // Inject a variable via variables_mut BEFORE feed → branch sees it
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        BlockData::Prompt {
                            prompt: "q".into(),
                            output_variable: Some("raw".into()),
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                (
                    "b",
                    block("Check", BlockData::Branch { condition: "ready".into() }),
                ),
                (
                    "yes",
                    block(
                        "Go",
                        BlockData::Prompt {
                            prompt: "go!".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("no", block("No", BlockData::End)),
            ],
            vec![
                conn("s", "p"),
                conn("p", "b"),
                branch_conn("b", "yes", true),
                branch_conn("b", "no", false),
                conn("yes", "no"),
            ],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start(); // Query

        // Inject "ready" BEFORE feed so branch sees it
        w.variables_mut().insert("ready".into(), "true".into());
        let a = w.feed("json response");

        // Should take true branch → "Go" prompt
        let (name, _) = assert_query(&a);
        assert_eq!(name, "Go");
    }

    #[test]
    fn variables_mut_inject_after_feed_is_too_late_for_branch() {
        // Demonstrate the timing issue: injecting AFTER feed means branch
        // already evaluated with missing variable
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        BlockData::Prompt {
                            prompt: "q".into(),
                            output_variable: Some("raw".into()),
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                (
                    "b",
                    block("Check", BlockData::Branch { condition: "ready".into() }),
                ),
                (
                    "yes",
                    block(
                        "Go",
                        BlockData::Prompt {
                            prompt: "go!".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("no", block("No", BlockData::End)),
            ],
            vec![
                conn("s", "p"),
                conn("p", "b"),
                branch_conn("b", "yes", true),
                branch_conn("b", "no", false),
                conn("yes", "no"),
            ],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        let a = w.feed("json response");
        // Injecting AFTER feed — branch already took the false path
        w.variables_mut().insert("ready".into(), "true".into());
        // Proves the branch went false (Done from "No" end block)
        assert_done(&a);
    }
}

// ===========================================================================
// 6. with_max_blocks() — safety limit
// ===========================================================================

mod safety_limit {
    use super::*;

    #[test]
    fn triggers_on_internal_block_loop() {
        // var → var (self-loop)
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "v",
                    block(
                        "Loop",
                        BlockData::Variable {
                            variable_name: "x".into(),
                            variable_value: "1".into(),
                            variable_type: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "v"), conn("v", "v")],
        );
        let mut w = GraphWalker::new(fc, no_vars()).with_max_blocks(5);
        assert_error_contains(&w.start(), "Safety limit");
    }

    #[test]
    fn triggers_on_prompt_loop() {
        // prompt → prompt (self-loop)
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        BlockData::Prompt {
                            prompt: "again".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "p")],
        );
        let mut w = GraphWalker::new(fc, no_vars()).with_max_blocks(3);
        let _ = w.start(); // 2 blocks: start + prompt
        let _ = w.feed("r1"); // 3rd block: prompt again
        let a = w.feed("r2"); // 4th block: over limit
        assert_error_contains(&a, "Safety limit");
    }

    #[test]
    fn limit_1_triggers_immediately_after_start() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "p",
                    block(
                        "Ask",
                        BlockData::Prompt {
                            prompt: "q".into(),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars()).with_max_blocks(1);
        // Start counts as block 1, prompt is block 2 → over limit
        assert_error_contains(&w.start(), "Safety limit");
    }

    #[test]
    fn counts_internal_blocks_toward_limit() {
        // start → var → var → var → prompt → end
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("v1", block("V1", BlockData::Variable { variable_name: "a".into(), variable_value: "1".into(), variable_type: None })),
                ("v2", block("V2", BlockData::Variable { variable_name: "b".into(), variable_value: "2".into(), variable_type: None })),
                ("v3", block("V3", BlockData::Variable { variable_name: "c".into(), variable_value: "3".into(), variable_type: None })),
                ("p", block("Ask", BlockData::Prompt { prompt: "q".into(), output_variable: None, session: None, output_schema: None })),
                ("e", block("E", BlockData::End)),
            ],
            vec![
                conn("s", "v1"), conn("v1", "v2"), conn("v2", "v3"),
                conn("v3", "p"), conn("p", "e"),
            ],
        );
        // Limit 4: start(1), v1(2), v2(3), v3(4), p(5) → over limit
        let mut w = GraphWalker::new(fc.clone(), no_vars()).with_max_blocks(4);
        assert_error_contains(&w.start(), "Safety limit");

        // Limit 5: exactly enough — prompt should yield
        let mut w = GraphWalker::new(fc, no_vars()).with_max_blocks(5);
        assert!(is_query(&w.start()));
    }
}

// ===========================================================================
// 7. Variable block — type coercion edge cases
// ===========================================================================

mod variable_block {
    use super::*;

    fn var_to_end(name: &str, value: &str, vtype: Option<VariableType>) -> Flowchart {
        flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "v",
                    block(
                        "Set",
                        BlockData::Variable {
                            variable_name: name.into(),
                            variable_value: value.into(),
                            variable_type: vtype,
                        },
                    ),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "v"), conn("v", "e")],
        )
    }

    #[test]
    fn string_type_stores_raw() {
        let fc = var_to_end("x", "hello world", Some(VariableType::String));
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("x").unwrap(), "hello world");
    }

    #[test]
    fn no_type_stores_raw() {
        let fc = var_to_end("x", "anything", None);
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("x").unwrap(), "anything");
    }

    #[test]
    fn number_integer() {
        let fc = var_to_end("x", "42", Some(VariableType::Number));
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("x").unwrap(), "42");
    }

    #[test]
    fn number_whole_float_truncated() {
        let fc = var_to_end("x", "3.0", Some(VariableType::Number));
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("x").unwrap(), "3");
    }

    #[test]
    fn number_fractional_preserved() {
        let fc = var_to_end("x", "3.14", Some(VariableType::Number));
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("x").unwrap(), "3.14");
    }

    #[test]
    fn number_negative() {
        let fc = var_to_end("x", "-7", Some(VariableType::Number));
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("x").unwrap(), "-7");
    }

    #[test]
    fn number_zero() {
        let fc = var_to_end("x", "0", Some(VariableType::Number));
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("x").unwrap(), "0");
    }

    #[test]
    fn number_invalid_returns_error() {
        let fc = var_to_end("x", "not_a_number", Some(VariableType::Number));
        let mut w = GraphWalker::new(fc, no_vars());
        assert_error_contains(&w.start(), "type coercion failed");
    }

    #[test]
    fn number_empty_string_returns_error() {
        let fc = var_to_end("x", "", Some(VariableType::Number));
        let mut w = GraphWalker::new(fc, no_vars());
        assert_error_contains(&w.start(), "type coercion failed");
    }

    #[test]
    fn boolean_true_variants() {
        for val in &["true", "TRUE", "True", "1", "yes", "YES"] {
            let fc = var_to_end("b", val, Some(VariableType::Boolean));
            let mut w = GraphWalker::new(fc, no_vars());
            assert_done(&w.start());
            assert_eq!(w.variables().get("b").unwrap(), "true", "input: {val}");
        }
    }

    #[test]
    fn boolean_false_variants() {
        for val in &["false", "FALSE", "0", "no", "NO", "anything_else", ""] {
            let fc = var_to_end("b", val, Some(VariableType::Boolean));
            let mut w = GraphWalker::new(fc, no_vars());
            assert_done(&w.start());
            assert_eq!(w.variables().get("b").unwrap(), "false", "input: {val}");
        }
    }

    #[test]
    fn json_valid() {
        let fc = var_to_end("j", r#"{"key": [1, 2]}"#, Some(VariableType::Json));
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("j").unwrap(), r#"{"key": [1, 2]}"#);
    }

    #[test]
    fn json_invalid_returns_error() {
        let fc = var_to_end("j", "not json {", Some(VariableType::Json));
        let mut w = GraphWalker::new(fc, no_vars());
        assert_error_contains(&w.start(), "type coercion failed");
    }

    #[test]
    fn json_scalar_valid() {
        let fc = var_to_end("j", "42", Some(VariableType::Json));
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("j").unwrap(), "42");
    }

    #[test]
    fn template_in_value() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                (
                    "v1",
                    block("V1", BlockData::Variable {
                        variable_name: "greeting".into(),
                        variable_value: "Hello".into(),
                        variable_type: None,
                    }),
                ),
                (
                    "v2",
                    block("V2", BlockData::Variable {
                        variable_name: "msg".into(),
                        variable_value: "{{greeting}} $1!".into(),
                        variable_type: None,
                    }),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "v1"), conn("v1", "v2"), conn("v2", "e")],
        );
        let mut w = GraphWalker::new(fc, vars(&[("$1", "World")]));
        assert_done(&w.start());
        assert_eq!(w.variables().get("msg").unwrap(), "Hello World!");
    }

    #[test]
    fn variable_overwrites_existing() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("v1", block("V1", BlockData::Variable { variable_name: "x".into(), variable_value: "first".into(), variable_type: None })),
                ("v2", block("V2", BlockData::Variable { variable_name: "x".into(), variable_value: "second".into(), variable_type: None })),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "v1"), conn("v1", "v2"), conn("v2", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("x").unwrap(), "second");
    }

    #[test]
    fn variable_self_reference() {
        // x = "a", then x = "{{x}}b" → "ab"
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("v1", block("V1", BlockData::Variable { variable_name: "x".into(), variable_value: "a".into(), variable_type: None })),
                ("v2", block("V2", BlockData::Variable { variable_name: "x".into(), variable_value: "{{x}}b".into(), variable_type: None })),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "v1"), conn("v1", "v2"), conn("v2", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
        assert_eq!(w.variables().get("x").unwrap(), "ab");
    }
}

// ===========================================================================
// 8. Branch block — condition evaluation edge cases
// ===========================================================================

mod branch_block {
    use super::*;

    /// Build: start → branch(condition) → true:prompt("T") → end, false:prompt("F") → end
    fn branch_flow(condition: &str) -> Flowchart {
        flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("b", block("Check", BlockData::Branch { condition: condition.into() })),
                (
                    "t",
                    block("T", BlockData::Prompt {
                        prompt: "true path".into(),
                        output_variable: None, session: None, output_schema: None,
                    }),
                ),
                (
                    "f",
                    block("F", BlockData::Prompt {
                        prompt: "false path".into(),
                        output_variable: None, session: None, output_schema: None,
                    }),
                ),
                ("e", block("E", BlockData::End)),
            ],
            vec![
                conn("s", "b"),
                branch_conn("b", "t", true),
                branch_conn("b", "f", false),
                conn("t", "e"),
                conn("f", "e"),
            ],
        )
    }

    fn assert_takes_path(fc: Flowchart, v: HashMap<String, String>, expected_name: &str) {
        let mut w = GraphWalker::new(fc, v);
        let a = w.start();
        let (name, _) = assert_query(&a);
        assert_eq!(name, expected_name);
    }

    #[test]
    fn truthy_string_takes_true_path() {
        assert_takes_path(branch_flow("flag"), vars(&[("flag", "yes")]), "T");
    }

    #[test]
    fn falsy_false_takes_false_path() {
        assert_takes_path(branch_flow("flag"), vars(&[("flag", "false")]), "F");
    }

    #[test]
    fn falsy_zero_takes_false_path() {
        assert_takes_path(branch_flow("flag"), vars(&[("flag", "0")]), "F");
    }

    #[test]
    fn falsy_no_takes_false_path() {
        assert_takes_path(branch_flow("flag"), vars(&[("flag", "no")]), "F");
    }

    #[test]
    fn falsy_empty_takes_false_path() {
        assert_takes_path(branch_flow("flag"), vars(&[("flag", "")]), "F");
    }

    #[test]
    fn missing_var_is_falsy() {
        assert_takes_path(branch_flow("flag"), no_vars(), "F");
    }

    #[test]
    fn negation_truthy_becomes_false() {
        assert_takes_path(branch_flow("!flag"), vars(&[("flag", "true")]), "F");
    }

    #[test]
    fn negation_falsy_becomes_true() {
        assert_takes_path(branch_flow("!flag"), vars(&[("flag", "false")]), "T");
    }

    #[test]
    fn negation_missing_becomes_true() {
        assert_takes_path(branch_flow("!flag"), no_vars(), "T");
    }

    #[test]
    fn numeric_equals() {
        assert_takes_path(branch_flow("x == 5"), vars(&[("x", "5")]), "T");
    }

    #[test]
    fn numeric_not_equals() {
        assert_takes_path(branch_flow("x != 5"), vars(&[("x", "3")]), "T");
    }

    #[test]
    fn numeric_less_than_true() {
        assert_takes_path(branch_flow("i < 10"), vars(&[("i", "3")]), "T");
    }

    #[test]
    fn numeric_less_than_false() {
        assert_takes_path(branch_flow("i < 10"), vars(&[("i", "10")]), "F");
    }

    #[test]
    fn numeric_greater_than() {
        assert_takes_path(branch_flow("count > 0"), vars(&[("count", "5")]), "T");
    }

    #[test]
    fn numeric_gte_equal_case() {
        assert_takes_path(branch_flow("x >= 5"), vars(&[("x", "5")]), "T");
    }

    #[test]
    fn numeric_lte_equal_case() {
        assert_takes_path(branch_flow("x <= 5"), vars(&[("x", "5")]), "T");
    }

    #[test]
    fn string_equality_quoted() {
        assert_takes_path(branch_flow(r#"status == "done""#), vars(&[("status", "done")]), "T");
    }

    #[test]
    fn string_equality_quoted_mismatch() {
        assert_takes_path(branch_flow(r#"status == "done""#), vars(&[("status", "running")]), "F");
    }

    #[test]
    fn string_inequality() {
        assert_takes_path(branch_flow(r#"status != "error""#), vars(&[("status", "ok")]), "T");
    }

    #[test]
    fn interpolated_condition() {
        // Condition is "{{x}} > 0" — interpolation happens before eval
        assert_takes_path(branch_flow("{{x}} > 0"), vars(&[("x", "5")]), "T");
    }

    #[test]
    fn branch_with_no_matching_path_returns_error() {
        // Branch with only true edge, condition is false → error
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("b", block("Check", BlockData::Branch { condition: "flag".into() })),
                ("t", block("T", BlockData::End)),
            ],
            vec![conn("s", "b"), branch_conn("b", "t", true)],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_error_contains(&w.start(), "no false path");
    }
}

// ===========================================================================
// 9. Prompt block — field passthrough
// ===========================================================================

mod prompt_block {
    use super::*;

    #[test]
    fn session_passed_through() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("p", block("Ask", BlockData::Prompt {
                    prompt: "q".into(),
                    output_variable: None,
                    session: Some("critic".into()),
                    output_schema: None,
                })),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        match w.start() {
            Action::Query { session, .. } => assert_eq!(session.as_deref(), Some("critic")),
            other => panic!("expected Query, got {other:?}"),
        }
    }

    #[test]
    fn output_schema_passed_through() {
        let schema = serde_json::json!({
            "type": "object",
            "properties": { "ready": { "type": "boolean" } }
        });
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("p", block("Ask", BlockData::Prompt {
                    prompt: "q".into(),
                    output_variable: Some("out".into()),
                    session: None,
                    output_schema: Some(schema.clone()),
                })),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        match w.start() {
            Action::Query { output_schema, output_var, .. } => {
                assert_eq!(output_schema.unwrap(), schema);
                assert_eq!(output_var.as_deref(), Some("out"));
            }
            other => panic!("expected Query, got {other:?}"),
        }
    }

    #[test]
    fn prompt_template_interpolation() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("p", block("Ask", BlockData::Prompt {
                    prompt: "Deploy $1 to {{env}} now".into(),
                    output_variable: None,
                    session: None,
                    output_schema: None,
                })),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "p"), conn("p", "e")],
        );
        let mut w = GraphWalker::new(fc, vars(&[("$1", "v2.0"), ("env", "prod")]));
        let a = w.start();
        let (_, prompt) = assert_query(&a);
        assert_eq!(prompt, "Deploy v2.0 to prod now");
    }

    #[test]
    fn prompt_block_id_passed_through() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("my_prompt_id", block("Ask", BlockData::Prompt {
                    prompt: "q".into(), output_variable: None, session: None, output_schema: None,
                })),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "my_prompt_id"), conn("my_prompt_id", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        match w.start() {
            Action::Query { block_id, .. } => assert_eq!(block_id, "my_prompt_id"),
            other => panic!("expected Query, got {other:?}"),
        }
    }
}

// ===========================================================================
// 10. Exit block
// ===========================================================================

mod exit_block {
    use super::*;

    #[test]
    fn exit_with_explicit_code() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("x", block("Bye", BlockData::Exit { exit_code: Some(1) })),
            ],
            vec![conn("s", "x")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_exit(&w.start(), 1);
    }

    #[test]
    fn exit_with_default_zero() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("x", block("Bye", BlockData::Exit { exit_code: None })),
            ],
            vec![conn("s", "x")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_exit(&w.start(), 0);
    }

    #[test]
    fn exit_negative_code() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("x", block("Bye", BlockData::Exit { exit_code: Some(-1) })),
            ],
            vec![conn("s", "x")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_exit(&w.start(), -1);
    }

    #[test]
    fn exit_passes_block_name() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("x", block("Fatal Error", BlockData::Exit { exit_code: Some(99) })),
            ],
            vec![conn("s", "x")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        match w.start() {
            Action::Exit { block_name, .. } => assert_eq!(block_name, "Fatal Error"),
            other => panic!("expected Exit, got {other:?}"),
        }
    }
}

// ===========================================================================
// 11-15. Combos — multi-block sequences testing real patterns
// ===========================================================================

mod combos {
    use super::*;

    #[test]
    fn variable_then_branch_uses_variable() {
        // var(ready=true) → branch(ready) → true: end
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("v", block("Set", BlockData::Variable {
                    variable_name: "ready".into(), variable_value: "true".into(), variable_type: None,
                })),
                ("b", block("Check", BlockData::Branch { condition: "ready".into() })),
                ("yes", block("Yes", BlockData::End)),
                ("no", block("No", BlockData::Prompt {
                    prompt: "not ready".into(), output_variable: None, session: None, output_schema: None,
                })),
            ],
            vec![
                conn("s", "v"), conn("v", "b"),
                branch_conn("b", "yes", true),
                branch_conn("b", "no", false),
            ],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        assert_done(&w.start());
    }

    #[test]
    fn bash_output_then_branch_success() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("b", block("Run", BlockData::Bash {
                    command: "test".into(), output_variable: None, working_directory: None,
                    continue_on_error: Some(true), exit_code_variable: Some("rc".into()),
                })),
                ("check", block("Check", BlockData::Branch { condition: "rc == 0".into() })),
                ("ok", block("OK", BlockData::End)),
                ("fail", block("Fail", BlockData::Prompt {
                    prompt: "failed".into(), output_variable: None, session: None, output_schema: None,
                })),
            ],
            vec![
                conn("s", "b"), conn("b", "check"),
                branch_conn("check", "ok", true),
                branch_conn("check", "fail", false),
            ],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start(); // Bash
        assert_done(&w.feed_bash("ok", 0)); // rc=0 → true → end
    }

    #[test]
    fn bash_output_then_branch_failure() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("b", block("Run", BlockData::Bash {
                    command: "test".into(), output_variable: None, working_directory: None,
                    continue_on_error: Some(true), exit_code_variable: Some("rc".into()),
                })),
                ("check", block("Check", BlockData::Branch { condition: "rc == 0".into() })),
                ("ok", block("OK", BlockData::End)),
                ("fail", block("Fail", BlockData::Prompt {
                    prompt: "failed".into(), output_variable: None, session: None, output_schema: None,
                })),
            ],
            vec![
                conn("s", "b"), conn("b", "check"),
                branch_conn("check", "ok", true),
                branch_conn("check", "fail", false),
            ],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        let a = w.feed_bash("error", 1); // rc=1 → false → prompt
        let (name, _) = assert_query(&a);
        assert_eq!(name, "Fail");
    }

    #[test]
    fn prompt_output_branch_done() {
        // prompt(output=status) → branch(status == "done") → true: end
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("p1", block("Ask", BlockData::Prompt {
                    prompt: "status?".into(), output_variable: Some("status".into()),
                    session: None, output_schema: None,
                })),
                ("check", block("Check", BlockData::Branch { condition: r#"status == "done""#.into() })),
                ("ok", block("OK", BlockData::End)),
                ("again", block("Retry", BlockData::Prompt {
                    prompt: "try again".into(), output_variable: None, session: None, output_schema: None,
                })),
            ],
            vec![
                conn("s", "p1"), conn("p1", "check"),
                branch_conn("check", "ok", true),
                branch_conn("check", "again", false),
                conn("again", "ok"),
            ],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        assert_done(&w.feed("done"));
    }

    #[test]
    fn prompt_output_branch_retry() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("p1", block("Ask", BlockData::Prompt {
                    prompt: "status?".into(), output_variable: Some("status".into()),
                    session: None, output_schema: None,
                })),
                ("check", block("Check", BlockData::Branch { condition: r#"status == "done""#.into() })),
                ("ok", block("OK", BlockData::End)),
                ("again", block("Retry", BlockData::Prompt {
                    prompt: "try again".into(), output_variable: None, session: None, output_schema: None,
                })),
            ],
            vec![
                conn("s", "p1"), conn("p1", "check"),
                branch_conn("check", "ok", true),
                branch_conn("check", "again", false),
                conn("again", "ok"),
            ],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start();
        let a = w.feed("working");
        let (name, _) = assert_query(&a);
        assert_eq!(name, "Retry");
    }

    #[test]
    fn chain_bash_prompt_subcommand() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("b", block("Build", BlockData::Bash {
                    command: "make".into(), output_variable: Some("build_out".into()),
                    working_directory: None, continue_on_error: None, exit_code_variable: None,
                })),
                ("p", block("Review", BlockData::Prompt {
                    prompt: "Review: {{build_out}}".into(), output_variable: Some("review".into()),
                    session: None, output_schema: None,
                })),
                ("c", block("Deploy", BlockData::Command {
                    command_name: "deploy".into(), arguments: Some("{{review}}".into()),
                    inherit_variables: Some(true), merge_output: None,
                })),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "b"), conn("b", "p"), conn("p", "c"), conn("c", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());

        let a = w.start();
        let (name, _) = assert_bash(&a);
        assert_eq!(name, "Build");

        let a = w.feed_bash("compiled successfully", 0);
        let (name, prompt) = assert_query(&a);
        assert_eq!(name, "Review");
        assert_eq!(prompt, "Review: compiled successfully");

        let a = w.feed("looks good");
        let name = assert_subcommand(&a);
        assert_eq!(name, "Deploy");
        match &a {
            Action::SubCommand { arguments, .. } => assert_eq!(arguments, "looks good"),
            _ => unreachable!(),
        }

        assert_done(&w.feed_subcommand("{}", HashMap::new()));
    }

    #[test]
    fn branch_different_action_types_each_path() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("b", block("Check", BlockData::Branch { condition: "fast".into() })),
                ("bash", block("Quick", BlockData::Bash {
                    command: "quick-deploy".into(), output_variable: None, working_directory: None,
                    continue_on_error: None, exit_code_variable: None,
                })),
                ("prompt", block("Careful", BlockData::Prompt {
                    prompt: "review first".into(), output_variable: None, session: None, output_schema: None,
                })),
                ("e", block("E", BlockData::End)),
            ],
            vec![
                conn("s", "b"),
                branch_conn("b", "bash", true),
                branch_conn("b", "prompt", false),
                conn("bash", "e"),
                conn("prompt", "e"),
            ],
        );

        // true path → bash
        let mut w = GraphWalker::new(fc.clone(), vars(&[("fast", "true")]));
        let a = w.start();
        let (name, _) = assert_bash(&a);
        assert_eq!(name, "Quick");

        // false path → prompt
        let mut w = GraphWalker::new(fc, vars(&[("fast", "false")]));
        let a = w.start();
        let (name, _) = assert_query(&a);
        assert_eq!(name, "Careful");
    }

    #[test]
    fn counter_loop_with_bash_increment() {
        // var(i=0) → bash(inc) → branch(i < 3) → true: bash, false: end
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("init", block("Init", BlockData::Variable {
                    variable_name: "i".into(), variable_value: "0".into(),
                    variable_type: Some(VariableType::Number),
                })),
                ("inc", block("Inc", BlockData::Bash {
                    command: "echo $(({{i}}+1))".into(), output_variable: Some("i".into()),
                    working_directory: None, continue_on_error: None, exit_code_variable: None,
                })),
                ("check", block("Check", BlockData::Branch { condition: "i < 3".into() })),
                ("e", block("E", BlockData::End)),
            ],
            vec![
                conn("s", "init"), conn("init", "inc"), conn("inc", "check"),
                branch_conn("check", "inc", true),
                branch_conn("check", "e", false),
            ],
        );
        let mut w = GraphWalker::new(fc, no_vars());

        assert!(is_query(&w.start()) || matches!(w.start(), Action::Bash { .. }));
        // Re-create walker since start() was called
        let fc2 = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("init", block("Init", BlockData::Variable {
                    variable_name: "i".into(), variable_value: "0".into(),
                    variable_type: Some(VariableType::Number),
                })),
                ("inc", block("Inc", BlockData::Bash {
                    command: "echo $(({{i}}+1))".into(), output_variable: Some("i".into()),
                    working_directory: None, continue_on_error: None, exit_code_variable: None,
                })),
                ("check", block("Check", BlockData::Branch { condition: "i < 3".into() })),
                ("e", block("E", BlockData::End)),
            ],
            vec![
                conn("s", "init"), conn("init", "inc"), conn("inc", "check"),
                branch_conn("check", "inc", true),
                branch_conn("check", "e", false),
            ],
        );
        let mut w = GraphWalker::new(fc2, no_vars());
        let a = w.start();
        assert_bash(&a); // i=0, bash

        let a = w.feed_bash("1", 0); // i=1, 1<3 → true → bash
        assert_bash(&a);

        let a = w.feed_bash("2", 0); // i=2, 2<3 → true → bash
        assert_bash(&a);

        let a = w.feed_bash("3", 0); // i=3, 3<3 → false → end
        assert_done(&a);

        assert_eq!(w.variables().get("i").unwrap(), "3");
    }

    #[test]
    fn spawn_wait_sequence() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("sp1", block("Agent1", BlockData::Spawn {
                    agent_name: Some("a1".into()), command_name: Some("build".into()),
                    arguments: None, inherit_variables: None,
                    exit_code_variable: None, config_file: None,
                })),
                ("sp2", block("Agent2", BlockData::Spawn {
                    agent_name: Some("a2".into()), command_name: Some("test".into()),
                    arguments: None, inherit_variables: None,
                    exit_code_variable: None, config_file: None,
                })),
                ("w", block("Sync", BlockData::Wait)),
                ("e", block("E", BlockData::End)),
            ],
            vec![
                conn("s", "sp1"), conn("sp1", "sp2"),
                conn("sp2", "w"), conn("w", "e"),
            ],
        );
        let mut w = GraphWalker::new(fc, no_vars());

        let a = w.start();
        assert_eq!(assert_spawn(&a), "Agent1");

        let a = w.feed("");
        assert_eq!(assert_spawn(&a), "Agent2");

        let a = w.feed("");
        assert_eq!(assert_wait(&a), "Sync");

        assert_done(&w.feed(""));
    }

    #[test]
    fn clear_then_prompt_in_same_session() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("r", block("Reset", BlockData::Refresh { target_session: Some("main".into()) })),
                ("p", block("Ask", BlockData::Prompt {
                    prompt: "fresh start".into(), output_variable: None,
                    session: Some("main".into()), output_schema: None,
                })),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "r"), conn("r", "p"), conn("p", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let a = w.start();
        assert_eq!(assert_clear(&a), "Reset");
        let a = w.feed("");
        assert_eq!(assert_query(&a).0, "Ask");
        assert_done(&w.feed("response"));
    }

    #[test]
    fn nested_branches_tt() {
        let fc = nested_branch_flow();
        let mut w = GraphWalker::new(fc, vars(&[("a", "true"), ("b", "true")]));
        assert_done(&w.start()); // a=T, b=T → end
    }

    #[test]
    fn nested_branches_tf() {
        let fc = nested_branch_flow();
        let mut w = GraphWalker::new(fc, vars(&[("a", "true"), ("b", "false")]));
        assert_done(&w.start()); // a=T, b=F → end
    }

    #[test]
    fn nested_branches_f() {
        let fc = nested_branch_flow();
        let mut w = GraphWalker::new(fc, vars(&[("a", "false")]));
        assert_done(&w.start()); // a=F → end
    }

    fn nested_branch_flow() -> Flowchart {
        flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("b1", block("Check A", BlockData::Branch { condition: "a".into() })),
                ("b2", block("Check B", BlockData::Branch { condition: "b".into() })),
                ("tt", block("TT", BlockData::End)),
                ("tf", block("TF", BlockData::End)),
                ("f", block("F", BlockData::End)),
            ],
            vec![
                conn("s", "b1"),
                branch_conn("b1", "b2", true),
                branch_conn("b1", "f", false),
                branch_conn("b2", "tt", true),
                branch_conn("b2", "tf", false),
            ],
        )
    }

    #[test]
    fn subcommand_merges_vars_used_in_next_prompt() {
        let fc = flowchart(
            vec![
                ("s", block("S", BlockData::Start)),
                ("c", block("Sub", BlockData::Command {
                    command_name: "analyze".into(), arguments: None,
                    inherit_variables: None, merge_output: None,
                })),
                ("p", block("Report", BlockData::Prompt {
                    prompt: "Results: {{analysis}}".into(), output_variable: None,
                    session: None, output_schema: None,
                })),
                ("e", block("E", BlockData::End)),
            ],
            vec![conn("s", "c"), conn("c", "p"), conn("p", "e")],
        );
        let mut w = GraphWalker::new(fc, no_vars());
        let _ = w.start(); // SubCommand
        let a = w.feed_subcommand("{}", vars(&[("analysis", "all tests pass")]));
        let (_, prompt) = assert_query(&a);
        assert_eq!(prompt, "Results: all tests pass");
    }

    #[test]
    fn long_linear_chain() {
        let n = 20;
        let mut blocks = Vec::new();
        let mut conns_vec = Vec::new();

        blocks.push(("s".to_string(), block("S", BlockData::Start)));
        for i in 0..n {
            let id = format!("p{i}");
            let b = block(
                &format!("Prompt {i}"),
                BlockData::Prompt {
                    prompt: format!("step {i}"),
                    output_variable: None,
                    session: None,
                    output_schema: None,
                },
            );
            blocks.push((id, b));
        }
        blocks.push(("e".to_string(), block("E", BlockData::End)));

        // Build connections
        conns_vec.push(conn("s", "p0"));
        for i in 0..n - 1 {
            let src = format!("p{i}");
            let tgt = format!("p{}", i + 1);
            conns_vec.push(Connection {
                source_id: src,
                target_id: tgt,
                is_true_path: None,
            });
        }
        conns_vec.push(Connection {
            source_id: format!("p{}", n - 1),
            target_id: "e".into(),
            is_true_path: None,
        });

        let fc = Flowchart {
            name: None,
            blocks: blocks.into_iter().collect(),
            connections: conns_vec,
            sessions: None,
        };
        let mut w = GraphWalker::new(fc, no_vars());

        let mut a = w.start();
        for i in 0..n {
            let (name, _) = assert_query(&a);
            assert_eq!(name, format!("Prompt {i}"));
            a = w.feed("ok");
        }
        assert_done(&a);
    }
}

// ===========================================================================
// Property-based tests
// ===========================================================================

mod proptest_interpolation {
    use super::*;
    use proptest::prelude::*;

    proptest! {
        /// Plain text with no {{ or $ passes through unchanged.
        #[test]
        fn plain_text_unchanged(s in "[a-zA-Z0-9 ,.:;!?]{0,200}") {
            let result = flowchart::interpolate::interpolate(&s, &no_vars());
            prop_assert_eq!(result, s);
        }

        /// {{var}} with the variable set always resolves (no leftover braces).
        #[test]
        fn known_var_substituted(
            name in "[a-zA-Z_][a-zA-Z0-9_]{0,20}",
            value in "[a-zA-Z0-9]{1,50}",
        ) {
            let template = format!("{{{{{name}}}}}");
            let v = vars(&[(&name, &value)]);
            let result = flowchart::interpolate::interpolate(&template, &v);
            prop_assert!(!result.contains("{{"), "leftover {{ in {result:?}");
            prop_assert!(!result.contains("}}"), "leftover }} in {result:?}");
            // For non-float values, result should equal the value directly
            if value.parse::<f64>().is_err() || !value.contains('.') {
                prop_assert_eq!(result, value);
            }
        }

        /// Missing variable produces empty string.
        #[test]
        fn missing_var_empty(name in "[a-zA-Z_][a-zA-Z0-9_]{0,20}") {
            let template = format!("{{{{{name}}}}}");
            let result = flowchart::interpolate::interpolate(&template, &no_vars());
            prop_assert_eq!(result, "");
        }

        /// $N with variable set produces the value.
        #[test]
        fn positional_arg_substituted(n in 1u32..100, value in "[a-zA-Z0-9]{1,30}") {
            let key = format!("${n}");
            let template = format!("${n}");
            let v = vars(&[(&key, &value)]);
            let result = flowchart::interpolate::interpolate(&template, &v);
            prop_assert_eq!(result, value);
        }

        /// Interpolation with no variables never panics.
        #[test]
        fn no_panic(template in "\\PC{0,200}") {
            let _ = flowchart::interpolate::interpolate(&template, &no_vars());
        }
    }
}

mod proptest_condition {
    use super::*;
    use proptest::prelude::*;

    proptest! {
        /// Negation inverts truthiness.
        #[test]
        fn negation_inverts(val in prop::bool::ANY) {
            let s = if val { "true" } else { "false" };
            let v = vars(&[("x", s)]);
            let direct = flowchart::condition::evaluate("x", &v);
            let negated = flowchart::condition::evaluate("!x", &v);
            prop_assert_ne!(direct, negated);
        }

        /// x == x is always true for any numeric value.
        #[test]
        fn numeric_self_equality(n in -1000i64..1000) {
            let s = n.to_string();
            let v = vars(&[("x", &s)]);
            let cond = format!("x == {n}");
            prop_assert!(flowchart::condition::evaluate(&cond, &v));
        }

        /// x < x is always false.
        #[test]
        fn numeric_not_less_than_self(n in -1000i64..1000) {
            let s = n.to_string();
            let v = vars(&[("x", &s)]);
            let cond = format!("x < {n}");
            prop_assert!(!flowchart::condition::evaluate(&cond, &v));
        }

        /// x <= x is always true.
        #[test]
        fn numeric_lte_self(n in -1000i64..1000) {
            let s = n.to_string();
            let v = vars(&[("x", &s)]);
            let cond = format!("x <= {n}");
            prop_assert!(flowchart::condition::evaluate(&cond, &v));
        }

        /// x >= x is always true.
        #[test]
        fn numeric_gte_self(n in -1000i64..1000) {
            let s = n.to_string();
            let v = vars(&[("x", &s)]);
            let cond = format!("x >= {n}");
            prop_assert!(flowchart::condition::evaluate(&cond, &v));
        }

        /// If a < b then !(a >= b).
        #[test]
        fn lt_implies_not_gte(a in -1000i64..1000, b in -1000i64..1000) {
            let v = vars(&[("a", &a.to_string()), ("b", &b.to_string())]);
            let lt = flowchart::condition::evaluate(&format!("a < {b}"), &v);
            let gte = flowchart::condition::evaluate(&format!("a >= {b}"), &v);
            prop_assert_ne!(lt, gte);
        }

        /// Condition evaluation never panics on arbitrary input.
        #[test]
        fn no_panic(cond in "\\PC{0,100}") {
            let _ = flowchart::condition::evaluate(&cond, &no_vars());
        }
    }
}

mod proptest_walker {
    use super::*;
    use proptest::prelude::*;

    /// Generate a linear flowchart with N prompt blocks.
    fn linear_prompts(n: usize) -> Flowchart {
        let mut blocks = Vec::new();
        let mut conns = Vec::new();

        blocks.push(("s".to_string(), block("S", BlockData::Start)));
        let mut prev = "s".to_string();
        for i in 0..n {
            let id = format!("p{i}");
            let b = block(
                &format!("P{i}"),
                BlockData::Prompt {
                    prompt: format!("step {i}"),
                    output_variable: None,
                    session: None,
                    output_schema: None,
                },
            );
            blocks.push((id.clone(), b));
            conns.push(Connection {
                source_id: prev.clone(),
                target_id: id.clone(),
                is_true_path: None,
            });
            prev = id;
        }
        blocks.push(("e".to_string(), block("E", BlockData::End)));
        conns.push(Connection {
            source_id: prev,
            target_id: "e".into(),
            is_true_path: None,
        });

        Flowchart {
            name: None,
            blocks: blocks.into_iter().collect(),
            connections: conns,
            sessions: None,
        }
    }

    proptest! {
        /// A linear chain of N prompts always terminates with Done after N feeds.
        #[test]
        fn linear_always_terminates(n in 0usize..30) {
            let fc = linear_prompts(n);
            let mut w = GraphWalker::new(fc, no_vars());
            let mut a = w.start();
            for _ in 0..n {
                if is_done(&a) { break; }
                if is_query(&a) {
                    a = w.feed("ok");
                } else {
                    panic!("unexpected action: {a:?}");
                }
            }
            prop_assert!(is_done(&a), "expected Done after {n} feeds, got {a:?}");
        }

        /// Any initial variables are accessible via prompt interpolation.
        #[test]
        fn initial_vars_persist(
            key in "[a-zA-Z_][a-zA-Z0-9_]{0,10}",
            val in "[a-zA-Z0-9]{1,20}",
        ) {
            let fc = flowchart(
                vec![
                    ("s", block("S", BlockData::Start)),
                    (
                        "p",
                        block("Ask", BlockData::Prompt {
                            prompt: format!("{{{{{key}}}}}"),
                            output_variable: None,
                            session: None,
                            output_schema: None,
                        }),
                    ),
                    ("e", block("E", BlockData::End)),
                ],
                vec![conn("s", "p"), conn("p", "e")],
            );
            let v = vars(&[(&key, &val)]);
            let mut w = GraphWalker::new(fc, v);
            let a = w.start();
            match &a {
                Action::Query { prompt, .. } => {
                    // For non-float values, prompt should equal the value
                    if val.parse::<f64>().is_err() || !val.contains('.') {
                        prop_assert_eq!(prompt, &val);
                    }
                }
                other => panic!("expected Query, got {other:?}"),
            }
        }
    }
}
