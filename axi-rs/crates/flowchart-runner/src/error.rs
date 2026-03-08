use flowchart::ResolveError;

#[derive(Debug, thiserror::Error)]
pub enum ExecutionError {
    #[error("Command not found: {0}")]
    CommandNotFound(#[from] ResolveError),

    #[error("Max recursion depth ({0}) exceeded")]
    MaxDepth(usize),

    #[error("Max blocks ({0}) exceeded")]
    MaxBlocks(usize),

    #[error("Session error: {0}")]
    Session(String),

    #[error("Missing required argument: {name} (position {position})")]
    MissingArgument { name: String, position: usize },

    #[error("Bash failed: {0}")]
    Bash(String),

    #[error("{0}")]
    Other(String),
}
