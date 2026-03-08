use std::collections::{HashMap, HashSet, VecDeque};

use crate::error::ValidationError;
use crate::model::{BlockData, Flowchart};

/// Validate flowchart structure.
///
/// Checks:
/// - Exactly one start block
/// - At least one end block
/// - All connection source/target IDs reference existing blocks
/// - No orphaned blocks (unreachable from start)
/// - Branch blocks have exactly two outgoing connections (true/false)
pub fn validate(flowchart: &Flowchart) -> Result<(), Vec<ValidationError>> {
    let mut errors = Vec::new();

    // Find start blocks
    let start_ids: Vec<&str> = flowchart
        .blocks
        .iter()
        .filter(|(_, b)| matches!(b.data, BlockData::Start))
        .map(|(id, _)| id.as_str())
        .collect();

    match start_ids.len() {
        0 => errors.push(ValidationError::NoStartBlock),
        1 => {}
        _ => errors.push(ValidationError::MultipleStartBlocks(
            start_ids.iter().map(|s| (*s).to_owned()).collect(),
        )),
    }

    // Check for at least one end block
    let has_end = flowchart
        .blocks
        .values()
        .any(|b| matches!(b.data, BlockData::End | BlockData::Exit { .. }));
    if !has_end {
        errors.push(ValidationError::NoEndBlock);
    }

    // Check connection references
    for conn in &flowchart.connections {
        if !flowchart.blocks.contains_key(&conn.source_id) {
            errors.push(ValidationError::MissingBlock(conn.source_id.clone()));
        }
        if !flowchart.blocks.contains_key(&conn.target_id) {
            errors.push(ValidationError::MissingBlock(conn.target_id.clone()));
        }
    }

    // Build outgoing edges index
    let mut outgoing: HashMap<&str, Vec<usize>> = HashMap::new();
    for (i, conn) in flowchart.connections.iter().enumerate() {
        outgoing.entry(conn.source_id.as_str()).or_default().push(i);
    }

    // Check branch blocks have exactly 2 outgoing connections with true/false paths
    for (id, block) in &flowchart.blocks {
        if matches!(block.data, BlockData::Branch { .. }) {
            let edges = outgoing.get(id.as_str());
            let edge_count = edges.map_or(0, Vec::len);
            if edge_count != 2 {
                errors.push(ValidationError::InvalidBranchConnections(id.clone()));
            } else if let Some(edge_indices) = edges {
                let has_true = edge_indices
                    .iter()
                    .any(|&i| flowchart.connections[i].is_true_path == Some(true));
                let has_false = edge_indices
                    .iter()
                    .any(|&i| flowchart.connections[i].is_true_path == Some(false));
                if !has_true || !has_false {
                    errors.push(ValidationError::InvalidBranchConnections(id.clone()));
                }
            }
        }
    }

    // Check for orphaned blocks (BFS from start)
    if start_ids.len() == 1 {
        let mut reachable = HashSet::new();
        let mut queue = VecDeque::new();
        queue.push_back(start_ids[0]);
        reachable.insert(start_ids[0]);

        while let Some(current) = queue.pop_front() {
            if let Some(edge_indices) = outgoing.get(current) {
                for &i in edge_indices {
                    let target = flowchart.connections[i].target_id.as_str();
                    if reachable.insert(target) {
                        queue.push_back(target);
                    }
                }
            }
        }

        for id in flowchart.blocks.keys() {
            if !reachable.contains(id.as_str()) {
                errors.push(ValidationError::OrphanedBlock(id.clone()));
            }
        }
    }

    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors)
    }
}

#[cfg(test)]
mod tests {
    use crate::model::{Block, Connection};

    use super::*;

    fn block(data: BlockData) -> Block {
        Block {
            name: String::new(),
            data,
            extra: HashMap::new(),
        }
    }

    #[test]
    fn valid_simple_flowchart() {
        let fc = Flowchart {
            name: None,
            blocks: [
                ("s".into(), block(BlockData::Start)),
                ("e".into(), block(BlockData::End)),
            ]
            .into_iter()
            .collect(),
            connections: vec![Connection {
                source_id: "s".into(),
                target_id: "e".into(),
                is_true_path: None,
            }],
            sessions: None,
        };
        assert!(validate(&fc).is_ok());
    }

    #[test]
    fn no_start_block() {
        let fc = Flowchart {
            name: None,
            blocks: [("e".into(), block(BlockData::End))]
                .into_iter()
                .collect(),
            connections: vec![],
            sessions: None,
        };
        let errs = validate(&fc).unwrap_err();
        assert!(errs
            .iter()
            .any(|e| matches!(e, ValidationError::NoStartBlock)));
    }

    #[test]
    fn no_end_block() {
        let fc = Flowchart {
            name: None,
            blocks: [("s".into(), block(BlockData::Start))]
                .into_iter()
                .collect(),
            connections: vec![],
            sessions: None,
        };
        let errs = validate(&fc).unwrap_err();
        assert!(errs
            .iter()
            .any(|e| matches!(e, ValidationError::NoEndBlock)));
    }

    #[test]
    fn missing_connection_target() {
        let fc = Flowchart {
            name: None,
            blocks: [
                ("s".into(), block(BlockData::Start)),
                ("e".into(), block(BlockData::End)),
            ]
            .into_iter()
            .collect(),
            connections: vec![Connection {
                source_id: "s".into(),
                target_id: "missing".into(),
                is_true_path: None,
            }],
            sessions: None,
        };
        let errs = validate(&fc).unwrap_err();
        assert!(errs
            .iter()
            .any(|e| matches!(e, ValidationError::MissingBlock(id) if id == "missing")));
    }

    #[test]
    fn orphaned_block() {
        let fc = Flowchart {
            name: None,
            blocks: [
                ("s".into(), block(BlockData::Start)),
                ("orphan".into(), block(BlockData::End)),
                ("e".into(), block(BlockData::End)),
            ]
            .into_iter()
            .collect(),
            connections: vec![Connection {
                source_id: "s".into(),
                target_id: "e".into(),
                is_true_path: None,
            }],
            sessions: None,
        };
        let errs = validate(&fc).unwrap_err();
        assert!(errs
            .iter()
            .any(|e| matches!(e, ValidationError::OrphanedBlock(id) if id == "orphan")));
    }

    #[test]
    fn branch_needs_two_connections() {
        let fc = Flowchart {
            name: None,
            blocks: [
                ("s".into(), block(BlockData::Start)),
                (
                    "b".into(),
                    block(BlockData::Branch {
                        condition: "flag".into(),
                    }),
                ),
                ("e".into(), block(BlockData::End)),
            ]
            .into_iter()
            .collect(),
            connections: vec![
                Connection {
                    source_id: "s".into(),
                    target_id: "b".into(),
                    is_true_path: None,
                },
                Connection {
                    source_id: "b".into(),
                    target_id: "e".into(),
                    is_true_path: Some(true),
                },
                // Missing false path
            ],
            sessions: None,
        };
        let errs = validate(&fc).unwrap_err();
        assert!(errs
            .iter()
            .any(|e| matches!(e, ValidationError::InvalidBranchConnections(_))));
    }

    #[test]
    fn valid_branch() {
        let fc = Flowchart {
            name: None,
            blocks: [
                ("s".into(), block(BlockData::Start)),
                (
                    "b".into(),
                    block(BlockData::Branch {
                        condition: "flag".into(),
                    }),
                ),
                ("ok".into(), block(BlockData::End)),
                ("fail".into(), block(BlockData::End)),
            ]
            .into_iter()
            .collect(),
            connections: vec![
                Connection {
                    source_id: "s".into(),
                    target_id: "b".into(),
                    is_true_path: None,
                },
                Connection {
                    source_id: "b".into(),
                    target_id: "ok".into(),
                    is_true_path: Some(true),
                },
                Connection {
                    source_id: "b".into(),
                    target_id: "fail".into(),
                    is_true_path: Some(false),
                },
            ],
            sessions: None,
        };
        assert!(validate(&fc).is_ok());
    }
}
