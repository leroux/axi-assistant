//! Behavioral tests: walk every fixture flowchart with mock responses.
//!
//! Each test verifies:
//! - The walker starts and produces the expected first action
//! - The walker can traverse the graph (no structural errors)
//! - Linear flowcharts reach Done with the correct action sequence
//! - Looped flowcharts either reach Done/Exit or hit the block limit

use std::collections::HashMap;
use std::path::Path;

use flowchart::{parse_command, Action, GraphWalker};

// ---------------------------------------------------------------------------
// Test harness
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
enum ActionKind {
    Query,
    Bash,
    SubCommand,
    Clear,
    Spawn,
    Wait,
}

#[derive(Debug, Clone)]
struct ActionRecord {
    kind: ActionKind,
    block_name: String,
}

#[derive(Debug)]
enum Outcome {
    Done,
    Exit(i32),
    Error(String),
}

struct WalkResult {
    actions: Vec<ActionRecord>,
    outcome: Outcome,
}

/// Walk a flowchart to completion (or block limit) with mock responses.
///
/// For prompts with output_schema, generates JSON with "happy path" values
/// and destructures the fields into walker variables.
fn walk(name: &str, initial_vars: HashMap<String, String>, max_blocks: usize) -> WalkResult {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(format!("{name}.json"));
    let json = std::fs::read_to_string(&path).unwrap();
    let cmd = parse_command(&json).unwrap();

    // Collect output_schema info for prompts so we can destructure responses
    let schemas: HashMap<String, serde_json::Value> = cmd
        .flowchart
        .blocks
        .iter()
        .filter_map(|(id, block)| {
            if let flowchart::BlockData::Prompt {
                output_schema: Some(schema),
                ..
            } = &block.data
            {
                Some((id.clone(), schema.clone()))
            } else {
                None
            }
        })
        .collect();

    let mut walker = GraphWalker::new(cmd.flowchart, initial_vars).with_max_blocks(max_blocks);
    let mut action = walker.start();
    let mut actions = Vec::new();

    loop {
        match action {
            Action::Done { .. } => {
                return WalkResult {
                    actions,
                    outcome: Outcome::Done,
                };
            }
            Action::Error { message } => {
                return WalkResult {
                    actions,
                    outcome: Outcome::Error(message),
                };
            }
            Action::Exit { exit_code, .. } => {
                return WalkResult {
                    actions,
                    outcome: Outcome::Exit(exit_code),
                };
            }
            Action::Query {
                ref block_id,
                ref block_name,
                ref output_schema,
                ..
            } => {
                let record = ActionRecord {
                    kind: ActionKind::Query,
                    block_name: block_name.clone(),
                };
                actions.push(record);

                // Clone schema before we move action
                let schema = output_schema
                    .clone()
                    .or_else(|| schemas.get(block_id).cloned());
                let response = mock_prompt_response(schema.as_ref());

                action = walker.feed(&response);

                // Destructure JSON into variables if schema exists
                if schema.is_some() {
                    if let Ok(obj) = serde_json::from_str::<serde_json::Value>(&response) {
                        if let Some(map) = obj.as_object() {
                            for (k, v) in map {
                                let val = match v {
                                    serde_json::Value::Bool(b) => b.to_string(),
                                    serde_json::Value::Number(n) => n.to_string(),
                                    serde_json::Value::String(s) => s.clone(),
                                    other => other.to_string(),
                                };
                                walker.variables_mut().insert(k.clone(), val);
                            }
                        }
                    }
                }
            }
            Action::Bash {
                ref block_name,
                ref command,
                ref output_var,
                continue_on_error,
                ..
            } => {
                let record = ActionRecord {
                    kind: ActionKind::Bash,
                    block_name: block_name.clone(),
                };
                actions.push(record);

                let (stdout, exit_code) = mock_bash_response(command, output_var.as_deref(), &walker);
                let _ = continue_on_error;
                action = walker.feed_bash(&stdout, exit_code);
            }
            Action::SubCommand {
                ref block_name, ..
            } => {
                let record = ActionRecord {
                    kind: ActionKind::SubCommand,
                    block_name: block_name.clone(),
                };
                actions.push(record);
                action = walker.feed_subcommand("{}", HashMap::new());
            }
            Action::Clear {
                ref block_name, ..
            } => {
                let record = ActionRecord {
                    kind: ActionKind::Clear,
                    block_name: block_name.clone(),
                };
                actions.push(record);
                action = walker.feed("");
            }
            Action::Spawn {
                ref block_name, ..
            } => {
                let record = ActionRecord {
                    kind: ActionKind::Spawn,
                    block_name: block_name.clone(),
                };
                actions.push(record);
                action = walker.feed("");
            }
            Action::Wait {
                ref block_name, ..
            } => {
                let record = ActionRecord {
                    kind: ActionKind::Wait,
                    block_name: block_name.clone(),
                };
                actions.push(record);
                action = walker.feed("");
            }
        }
    }
}

/// Generate a mock prompt response. If there's an output_schema, produce JSON
/// with "happy path" values that will cause loops to exit.
fn mock_prompt_response(schema: Option<&serde_json::Value>) -> String {
    let Some(schema) = schema else {
        return "Mock response.".to_owned();
    };

    let Some(props) = schema.get("properties").and_then(|p| p.as_object()) else {
        return "Mock response.".to_owned();
    };

    let mut obj = serde_json::Map::new();
    for (key, prop) in props {
        let type_str = prop.get("type").and_then(|t| t.as_str()).unwrap_or("string");
        let value = match type_str {
            "boolean" => serde_json::Value::Bool(happy_path_bool(key)),
            "integer" | "number" => serde_json::Value::Number(serde_json::Number::from(0)),
            "array" => serde_json::Value::Array(vec![]),
            "object" => serde_json::Value::Object(serde_json::Map::new()),
            _ => serde_json::Value::String(format!("mock_{key}")),
        };
        obj.insert(key.clone(), value);
    }

    serde_json::Value::Object(obj).to_string()
}

/// Determine the "happy path" boolean value for a schema field.
/// Fields indicating problems/issues/failures → false (no problems).
/// Fields indicating completion/success/readiness → true (done).
fn happy_path_bool(field_name: &str) -> bool {
    let lower = field_name.to_lowercase();

    // Negative indicators → false = "no problems"
    let negative_patterns = [
        "problem", "issue", "fail", "missing", "required", "continue",
        "gap", "fresh", "dotest",
    ];
    for pat in &negative_patterns {
        if lower.contains(pat) {
            return false;
        }
    }

    // Positive indicators → true = "done/passed/ready"
    true
}

/// Generate mock bash output and exit code.
fn mock_bash_response(command: &str, output_var: Option<&str>, walker: &GraphWalker) -> (String, i32) {
    // Detect increment patterns like: echo $(({{var}}+1))
    if command.contains("$((") && command.contains("+1))") {
        // Try to extract the current value and increment it
        if let Some(output_var) = output_var {
            if let Some(current) = walker.variables().get(output_var) {
                if let Ok(n) = current.parse::<i64>() {
                    return ((n + 1).to_string(), 0);
                }
            }
        }
        // Default: return "1" for first increment
        return ("1".to_owned(), 0);
    }

    // Detect "count" commands that output a number
    if command.contains("wc -l") || command.contains("| wc") {
        return ("3".to_owned(), 0);
    }

    // Detect echo with a literal number
    if command.starts_with("echo ") && !command.contains("$") && !command.contains("{") {
        let num_str = command.trim_start_matches("echo ").trim().trim_matches('"').trim_matches('\'');
        if num_str.parse::<i64>().is_ok() {
            return (num_str.to_owned(), 0);
        }
    }

    // Default: exit code 0, generic output
    ("ok".to_owned(), 0)
}

// ---------------------------------------------------------------------------
// Assertion helpers
// ---------------------------------------------------------------------------

impl WalkResult {
    fn assert_done(&self, fixture: &str) {
        assert!(
            matches!(self.outcome, Outcome::Done),
            "{fixture}: expected Done, got {:?}", self.outcome
        );
    }

    fn assert_done_or_exit(&self, fixture: &str) {
        assert!(
            matches!(self.outcome, Outcome::Done | Outcome::Exit(_)),
            "{fixture}: expected Done or Exit, got {:?}", self.outcome
        );
    }

    fn assert_no_structural_error(&self, fixture: &str) {
        if let Outcome::Error(msg) = &self.outcome {
            // Safety limit is OK — it means the graph is valid but looped
            assert!(
                msg.contains("Safety limit"),
                "{fixture}: structural error: {msg}"
            );
        }
    }

    fn assert_action_count(&self, min: usize, fixture: &str) {
        assert!(
            self.actions.len() >= min,
            "{fixture}: expected at least {min} actions, got {}",
            self.actions.len()
        );
    }

    fn assert_first_action(&self, kind: &str, fixture: &str) {
        assert!(
            !self.actions.is_empty(),
            "{fixture}: no actions produced"
        );
        let actual = match self.actions[0].kind {
            ActionKind::Query => "query",
            ActionKind::Bash => "bash",
            ActionKind::SubCommand => "subcommand",
            ActionKind::Clear => "clear",
            ActionKind::Spawn => "spawn",
            ActionKind::Wait => "wait",
        };
        assert_eq!(actual, kind, "{fixture}: first action was {actual}, expected {kind}");
    }

    fn action_names(&self) -> Vec<&str> {
        self.actions.iter().map(|a| a.block_name.as_str()).collect()
    }

    fn action_kinds(&self) -> Vec<&str> {
        self.actions
            .iter()
            .map(|a| match a.kind {
                ActionKind::Query => "query",
                ActionKind::Bash => "bash",
                ActionKind::SubCommand => "subcommand",
                ActionKind::Clear => "clear",
                ActionKind::Spawn => "spawn",
                ActionKind::Wait => "wait",
            })
            .collect()
    }
}

fn no_vars() -> HashMap<String, String> {
    HashMap::new()
}

fn vars(pairs: &[(&str, &str)]) -> HashMap<String, String> {
    pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
}

// ---------------------------------------------------------------------------
// Linear flowcharts — walk to completion, verify full sequence
// ---------------------------------------------------------------------------

#[test]
fn story_walks_three_prompts_to_done() {
    let r = walk("story", vars(&[("$1", "dragons")]), 100);
    r.assert_done("story");
    assert_eq!(r.action_names(), vec!["Write Draft", "Critique", "Refine"]);
    assert_eq!(r.action_kinds(), vec!["query", "query", "query"]);
}

#[test]
fn documentation_generator_walks_prompts_to_done() {
    let r = walk("documentation-generator", vars(&[("$1", "my-project")]), 100);
    r.assert_done("documentation-generator");
    // All actions should be prompts
    assert!(r.action_kinds().iter().all(|k| *k == "query"));
    r.assert_action_count(3, "documentation-generator");
}

#[test]
fn phase_planner_walks_to_done() {
    let r = walk("phase-planner", vars(&[("$1", "DESIGN_DOC.md")]), 100);
    r.assert_done("phase-planner");
    r.assert_first_action("query", "phase-planner");
}

#[test]
fn test_suite_builder_walks_to_done() {
    let r = walk("test-suite-builder", vars(&[("$1", "my-project")]), 100);
    r.assert_done("test-suite-builder");
    r.assert_first_action("query", "test-suite-builder");
}

#[test]
fn ex0_design_doc_walks_to_done() {
    let r = walk("ex0-design-doc", vars(&[("$1", "build a web app")]), 100);
    r.assert_done("ex0-design-doc");
    r.assert_first_action("query", "ex0-design-doc");
    // Should have prompt then bash (git tag)
    assert!(r.action_kinds().contains(&"bash"));
}

#[test]
fn create_dag_walks_subcommands_to_done() {
    let r = walk("create-dag", vars(&[("$1", "build a web app")]), 100);
    r.assert_done("create-dag");
    // create-dag chains: subcommand, subcommand, bash, end
    assert!(r.action_kinds().contains(&"subcommand"));
    assert!(r.action_kinds().contains(&"bash"));
}

#[test]
fn create_documents_walks_to_done() {
    let r = walk("create-documents", vars(&[("$1", "my-project")]), 100);
    r.assert_done("create-documents");
    r.assert_first_action("bash", "create-documents");
}

#[test]
fn update_encyclopedia_walks_to_done() {
    let r = walk("update-encyclopedia", vars(&[("$1", "topic")]), 200);
    r.assert_done("update-encyclopedia");
    r.assert_action_count(3, "update-encyclopedia");
}

#[test]
fn mini_wave_walks_to_done() {
    let r = walk("mini-wave", vars(&[("$1", "1")]), 200);
    r.assert_done("mini-wave");
    // Should have subcommands and refreshes
    assert!(r.action_kinds().contains(&"subcommand"));
}

// --- Subcommand-only flowcharts ---

#[test]
fn all_examples_walks_subcommands_to_done() {
    let r = walk("all-examples", vars(&[("$1", "build a web app")]), 100);
    r.assert_done("all-examples");
    assert_eq!(r.action_kinds(), vec!["subcommand", "subcommand", "subcommand", "subcommand"]);
}

#[test]
fn full_auto_dag_walks_to_done() {
    let r = walk("full-auto-dag", vars(&[("$1", "build a web app")]), 100);
    r.assert_done("full-auto-dag");
    assert!(r.action_kinds().iter().all(|k| *k == "subcommand"));
}

#[test]
fn full_auto_waves_walks_to_done() {
    let r = walk("full-auto-waves", vars(&[("$1", "build a web app")]), 100);
    r.assert_done("full-auto-waves");
    assert!(r.action_kinds().iter().all(|k| *k == "subcommand"));
}

#[test]
fn single_wave_walks_to_done() {
    let r = walk("single-wave", vars(&[("$1", "1")]), 100);
    r.assert_done("single-wave");
    assert!(r.action_kinds().iter().all(|k| *k == "subcommand"));
}

#[test]
fn single_wave_e2e_only_walks_to_done() {
    let r = walk("single-wave-e2e-only", vars(&[("$1", "1")]), 100);
    r.assert_done("single-wave-e2e-only");
}

// ---------------------------------------------------------------------------
// Branched flowcharts — verify walker handles branches correctly
// ---------------------------------------------------------------------------

#[test]
fn code_quality_audit_happy_path() {
    // Happy path: audit finds no issues → skip fixes → end
    let r = walk("code-quality-audit", no_vars(), 200);
    r.assert_no_structural_error("code-quality-audit");
    r.assert_first_action("query", "code-quality-audit");
    // Should reach done since hasIssues → false
    r.assert_done("code-quality-audit");
}

#[test]
fn dependency_manager_happy_path() {
    let r = walk("dependency-manager", no_vars(), 200);
    r.assert_no_structural_error("dependency-manager");
    r.assert_first_action("query", "dependency-manager");
}

#[test]
fn implement_phase_walks() {
    let r = walk("implement-phase", vars(&[("$1", "PHASE_1.md")]), 200);
    r.assert_no_structural_error("implement-phase");
    r.assert_first_action("query", "implement-phase");
}

#[test]
fn requirement_validator_walks() {
    let r = walk("requirement-validator", vars(&[("$1", "DESIGN_DOC.md")]), 200);
    r.assert_no_structural_error("requirement-validator");
    r.assert_first_action("query", "requirement-validator");
}

#[test]
fn integration_validator_walks() {
    let r = walk("integration-validator", no_vars(), 200);
    r.assert_no_structural_error("integration-validator");
    r.assert_first_action("query", "integration-validator");
}

#[test]
fn validate_and_audit_walks() {
    let r = walk("validate-and-audit", vars(&[("$1", "build a web app")]), 200);
    r.assert_no_structural_error("validate-and-audit");
}

#[test]
fn create_waves_walks() {
    let r = walk("create-waves", vars(&[("$1", "build a web app")]), 200);
    r.assert_no_structural_error("create-waves");
    r.assert_first_action("query", "create-waves");
}

// ---------------------------------------------------------------------------
// Looped flowcharts — verify loop structure and exit conditions
// ---------------------------------------------------------------------------

#[test]
fn ex1_do_until_done_loops_and_exits() {
    let r = walk("ex1-do-until-done", vars(&[("$1", "implement feature X")]), 200);
    r.assert_no_structural_error("ex1-do-until-done");
    r.assert_first_action("query", "ex1-do-until-done");
    // Should have refreshes (loop resets session)
    assert!(r.action_kinds().contains(&"clear"), "ex1: expected clear actions in loop");
}

#[test]
fn ex2_testing_loop_loops_and_exits() {
    let r = walk("ex2-testing-loop", vars(&[("$1", "my-project")]), 200);
    r.assert_no_structural_error("ex2-testing-loop");
    r.assert_first_action("query", "ex2-testing-loop");
}

#[test]
fn ex3_improve_project_walks() {
    // $1 is used as max iteration count (variable type: int), must be numeric
    let r = walk("ex3-improve-project", vars(&[("$1", "3")]), 300);
    r.assert_no_structural_error("ex3-improve-project");
}

#[test]
fn all_waves_loop_increments() {
    // all-waves loops: init waveNum=0, bash getFinalWaveNum, loop(waveNum <= finalWaveNum)
    let r = walk("all-waves", vars(&[("$1", "1")]), 200);
    r.assert_no_structural_error("all-waves");
    // Should have subcommand (single-wave) and bash (increment) actions
    assert!(r.action_kinds().contains(&"subcommand"), "all-waves: expected subcommand");
    assert!(r.action_kinds().contains(&"bash"), "all-waves: expected bash");
}

#[test]
fn iterate_dag_walks() {
    let r = walk("iterate-dag", vars(&[("$1", "build a web app")]), 200);
    r.assert_no_structural_error("iterate-dag");
    r.assert_first_action("bash", "iterate-dag");
}

#[test]
fn test_fix_loop_walks() {
    let r = walk("test-fix-loop", no_vars(), 200);
    r.assert_no_structural_error("test-fix-loop");
    r.assert_first_action("bash", "test-fix-loop");
}

#[test]
fn research_mode_walks() {
    let r = walk("research-mode", vars(&[("$1", "how does X work?")]), 300);
    r.assert_no_structural_error("research-mode");
    r.assert_first_action("bash", "research-mode");
}

#[test]
fn autonomous_dev_walks() {
    let r = walk("autonomous-dev", vars(&[("$1", "build a web app")]), 500);
    r.assert_no_structural_error("autonomous-dev");
    r.assert_first_action("query", "autonomous-dev");
}

// ---------------------------------------------------------------------------
// Wave flowcharts (dev/test cycles)
// ---------------------------------------------------------------------------

#[test]
fn wave_dev_walks() {
    let r = walk("wave-dev", vars(&[("$1", "1")]), 300);
    r.assert_no_structural_error("wave-dev");
    r.assert_first_action("clear", "wave-dev");
}

#[test]
fn wave_test_walks() {
    let r = walk("wave-test", vars(&[("$1", "1")]), 300);
    r.assert_no_structural_error("wave-test");
}

#[test]
fn wave_run_tests_walks() {
    let r = walk("wave-run-tests", vars(&[("$1", "1")]), 300);
    r.assert_no_structural_error("wave-run-tests");
    r.assert_first_action("bash", "wave-run-tests");
}

#[test]
fn wave_make_tests_walks() {
    let r = walk("wave-make-tests", vars(&[("$1", "1")]), 300);
    r.assert_no_structural_error("wave-make-tests");
    r.assert_first_action("clear", "wave-make-tests");
}

#[test]
fn wave_make_tests_e2e_only_walks() {
    let r = walk("wave-make-tests-e2e-only", vars(&[("$1", "1")]), 300);
    r.assert_no_structural_error("wave-make-tests-e2e-only");
}

#[test]
fn wave_e2e_walks() {
    let r = walk("wave_e2e", vars(&[("$1", "1")]), 500);
    r.assert_no_structural_error("wave_e2e");
}

#[test]
fn wave_e2e_no_plan_walks() {
    let r = walk("wave_e2e_no_plan", vars(&[("$1", "1")]), 500);
    r.assert_no_structural_error("wave_e2e_no_plan");
}

// ---------------------------------------------------------------------------
// Mini-wave and special flowcharts
// ---------------------------------------------------------------------------

#[test]
fn mini_wave_run_tests_walks() {
    let r = walk("mini-wave-run-tests", vars(&[("$1", "1")]), 200);
    r.assert_no_structural_error("mini-wave-run-tests");
    r.assert_first_action("query", "mini-wave-run-tests");
}

#[test]
fn mil_walks_to_exit() {
    let r = walk("mil", no_vars(), 200);
    r.assert_no_structural_error("mil");
    // mil has an exit block
    r.assert_done_or_exit("mil");
}

#[test]
fn mill_walks_to_exit() {
    let r = walk("mill", no_vars(), 200);
    r.assert_no_structural_error("mill");
    r.assert_done_or_exit("mill");
}
