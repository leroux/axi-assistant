//! Integration tests: parse and validate every fixture flowchart.

use std::path::Path;

use flowchart::{parse_command, validate};

fn fixture(name: &str) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(format!("{name}.json"));
    std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("failed to read {}: {e}", path.display()))
}

/// Parse a fixture and assert it succeeds.
fn assert_parses(name: &str) {
    let json = fixture(name);
    let cmd = parse_command(&json).unwrap_or_else(|e| panic!("{name}.json failed to parse: {e}"));
    assert!(!cmd.flowchart.blocks.is_empty(), "{name}.json has no blocks");
}

/// Parse + validate a fixture and assert both succeed.
fn assert_valid(name: &str) {
    let json = fixture(name);
    let cmd = parse_command(&json).unwrap_or_else(|e| panic!("{name}.json failed to parse: {e}"));
    if let Err(errors) = validate(&cmd.flowchart) {
        let msgs: Vec<String> = errors.iter().map(ToString::to_string).collect();
        panic!("{name}.json validation failed:\n  {}", msgs.join("\n  "));
    }
}

// --- Parse tests: every fixture must deserialize ---

macro_rules! parse_test {
    ($name:ident) => {
        #[test]
        fn $name() {
            assert_parses(stringify!($name).replace('_', "-").as_str());
        }
    };
    ($name:ident, $file:expr) => {
        #[test]
        fn $name() {
            assert_parses($file);
        }
    };
}

mod parse {
    use super::*;

    parse_test!(all_examples);
    parse_test!(all_waves);
    parse_test!(autonomous_dev);
    parse_test!(code_quality_audit);
    parse_test!(create_dag);
    parse_test!(create_documents);
    parse_test!(create_waves);
    parse_test!(dependency_manager);
    parse_test!(documentation_generator);
    parse_test!(ex0_design_doc);
    parse_test!(ex1_do_until_done);
    parse_test!(ex2_testing_loop);
    parse_test!(ex3_improve_project);
    parse_test!(full_auto_dag);
    parse_test!(full_auto_waves);
    parse_test!(implement_phase);
    parse_test!(integration_validator);
    parse_test!(iterate_dag);
    parse_test!(mil);
    parse_test!(mill);
    parse_test!(mini_wave);
    parse_test!(mini_wave_run_tests);
    parse_test!(phase_planner);
    parse_test!(requirement_validator);
    parse_test!(research_mode);
    parse_test!(single_wave);
    parse_test!(single_wave_e2e_only);
    parse_test!(story);
    parse_test!(test_fix_loop);
    parse_test!(test_suite_builder);
    parse_test!(update_encyclopedia);
    parse_test!(validate_and_audit);
    parse_test!(wave_dev);
    parse_test!(wave_make_tests);
    parse_test!(wave_make_tests_e2e_only);
    parse_test!(wave_run_tests);
    parse_test!(wave_test);
    parse_test!(wave_e2e, "wave_e2e");
    parse_test!(wave_e2e_no_plan, "wave_e2e_no_plan");
}

// --- Validation tests: every fixture must pass structural validation ---

macro_rules! validate_test {
    ($name:ident) => {
        #[test]
        fn $name() {
            assert_valid(stringify!($name).replace('_', "-").as_str());
        }
    };
    ($name:ident, $file:expr) => {
        #[test]
        fn $name() {
            assert_valid($file);
        }
    };
}

mod valid {
    use super::*;

    validate_test!(all_examples);
    validate_test!(all_waves);
    validate_test!(autonomous_dev);
    validate_test!(code_quality_audit);
    validate_test!(create_dag);
    validate_test!(create_documents);
    validate_test!(create_waves);
    validate_test!(dependency_manager);
    validate_test!(documentation_generator);
    validate_test!(ex0_design_doc);
    validate_test!(ex1_do_until_done);
    validate_test!(ex2_testing_loop);
    validate_test!(ex3_improve_project);
    validate_test!(full_auto_dag);
    validate_test!(full_auto_waves);
    validate_test!(implement_phase);
    validate_test!(integration_validator);
    validate_test!(iterate_dag);
    validate_test!(mil);
    validate_test!(mill);
    validate_test!(mini_wave);
    validate_test!(mini_wave_run_tests);
    validate_test!(phase_planner);
    validate_test!(requirement_validator);
    validate_test!(research_mode);
    validate_test!(single_wave);
    validate_test!(single_wave_e2e_only);
    validate_test!(story);
    validate_test!(test_fix_loop);
    validate_test!(test_suite_builder);
    validate_test!(update_encyclopedia);
    validate_test!(validate_and_audit);
    validate_test!(wave_dev);
    validate_test!(wave_make_tests);
    validate_test!(wave_make_tests_e2e_only);
    validate_test!(wave_run_tests);
    validate_test!(wave_test);
    validate_test!(wave_e2e, "wave_e2e");
    validate_test!(wave_e2e_no_plan, "wave_e2e_no_plan");
}
