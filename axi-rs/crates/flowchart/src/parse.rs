use crate::error::ParseError;
use crate::model::Command;

/// Parse a command file from a JSON string.
pub fn parse_command(json: &str) -> Result<Command, ParseError> {
    let cmd: Command = serde_json::from_str(json)?;
    if cmd.name.is_empty() {
        return Err(ParseError::InvalidCommand(
            "command name must not be empty".into(),
        ));
    }
    Ok(cmd)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_story_fixture() {
        let json = include_str!("../tests/fixtures/story.json");
        let cmd = parse_command(json).expect("should parse story.json");
        assert_eq!(cmd.name, "story");
        assert_eq!(cmd.arguments.len(), 1);
        assert_eq!(cmd.arguments[0].name, "topic");

        let fc = &cmd.flowchart;
        assert_eq!(fc.blocks.len(), 5);
        assert_eq!(fc.connections.len(), 4);

        let sessions = fc.sessions.as_ref().expect("should have sessions");
        assert_eq!(sessions.len(), 2);
        assert!(sessions.contains_key("default"));
        assert!(sessions.contains_key("critic"));
    }

    #[test]
    fn parse_minimal_flowchart() {
        let json = r#"{
            "name": "minimal",
            "flowchart": {
                "blocks": {
                    "s": {"type": "start", "name": "Begin"},
                    "e": {"type": "end", "name": "Done"}
                },
                "connections": [
                    {"source_id": "s", "target_id": "e"}
                ]
            }
        }"#;
        let cmd = parse_command(json).expect("should parse minimal");
        assert_eq!(cmd.flowchart.blocks.len(), 2);
    }

    #[test]
    fn parse_connection_block_id_aliases() {
        let json = r#"{
            "name": "alias-test",
            "flowchart": {
                "blocks": {
                    "s": {"type": "start", "name": "Begin"},
                    "e": {"type": "end", "name": "Done"}
                },
                "connections": [
                    {"source_block_id": "s", "target_block_id": "e"}
                ]
            }
        }"#;
        let cmd = parse_command(json).expect("should parse with block_id aliases");
        assert_eq!(cmd.flowchart.connections[0].source_id, "s");
        assert_eq!(cmd.flowchart.connections[0].target_id, "e");
    }

    #[test]
    fn parse_variable_type_int_alias() {
        let json = r#"{
            "name": "int-test",
            "flowchart": {
                "blocks": {
                    "s": {"type": "start", "name": "Begin"},
                    "v": {"type": "variable", "name": "Set", "variable_name": "x", "variable_value": "1", "variable_type": "int"},
                    "e": {"type": "end", "name": "Done"}
                },
                "connections": [
                    {"source_id": "s", "target_id": "v"},
                    {"source_id": "v", "target_id": "e"}
                ]
            }
        }"#;
        let cmd = parse_command(json).expect("should parse with int alias");
        let block = &cmd.flowchart.blocks["v"];
        match &block.data {
            crate::model::BlockData::Variable { variable_type, .. } => {
                assert_eq!(variable_type.as_ref(), Some(&crate::model::VariableType::Number));
            }
            _ => panic!("Expected Variable block"),
        }
    }

    #[test]
    fn parse_empty_name_fails() {
        let json = r#"{
            "name": "",
            "flowchart": {
                "blocks": {},
                "connections": []
            }
        }"#;
        let err = parse_command(json).unwrap_err();
        assert!(matches!(err, ParseError::InvalidCommand(_)));
    }
}
