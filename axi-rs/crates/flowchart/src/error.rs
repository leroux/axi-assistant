use thiserror::Error;

#[derive(Debug, Error)]
pub enum ParseError {
    #[error("JSON parse error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("Invalid command file: {0}")]
    InvalidCommand(String),
}

#[derive(Debug, Error)]
pub enum ValidationError {
    #[error("No start block found")]
    NoStartBlock,

    #[error("Multiple start blocks found: {0:?}")]
    MultipleStartBlocks(Vec<String>),

    #[error("No end block found")]
    NoEndBlock,

    #[error("Connection references missing block: {0}")]
    MissingBlock(String),

    #[error("Orphaned block (unreachable from start): {0}")]
    OrphanedBlock(String),

    #[error("Branch block '{0}' must have exactly 2 outgoing connections (true/false)")]
    InvalidBranchConnections(String),
}
