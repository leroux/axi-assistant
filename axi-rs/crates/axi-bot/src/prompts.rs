//! System prompt construction and pack loading.
//!
//! Handles layered .md prompt files, pack system, and per-agent prompt assembly.
//! Mirrors the Python prompts.py module.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use tracing::{info, warn};

// ---------------------------------------------------------------------------
// System prompt preset (matches Claude Agent SDK format)
// ---------------------------------------------------------------------------

/// A system prompt preset for Claude Code agents.
#[derive(Debug, Clone)]
pub struct SystemPromptPreset {
    /// Always "`claude_code`" for preset type.
    pub preset: String,
    /// Appended to the default `claude_code` system prompt.
    pub append: String,
}

impl SystemPromptPreset {
    pub fn claude_code(append: String) -> Self {
        Self {
            preset: "claude_code".to_string(),
            append,
        }
    }

    /// Compute a short hash for change detection.
    pub fn hash(&self) -> String {
        use std::hash::{Hash, Hasher};
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        self.append.hash(&mut hasher);
        format!("{:016x}", hasher.finish())
    }
}

// ---------------------------------------------------------------------------
// Prompt file loading
// ---------------------------------------------------------------------------

fn load_prompt_file(path: &Path, variables: &HashMap<&str, &str>) -> String {
    match std::fs::read_to_string(path) {
        Ok(mut content) => {
            for (key, val) in variables {
                content = content.replace(&format!("%({key})s"), val);
            }
            content
        }
        Err(e) => {
            warn!("Failed to load prompt file {}: {}", path.display(), e);
            String::new()
        }
    }
}

// ---------------------------------------------------------------------------
// Pack system
// ---------------------------------------------------------------------------

/// Which packs each agent type gets by default.
pub const MASTER_PACKS: &[&str] = &["algorithm"];
pub const DEFAULT_SPAWNED_PACKS: &[&str] = &["algorithm"];
pub const AXI_DEV_PACKS: &[&str] = &["axi-dev"];

/// Load all available packs from the packs/ directory.
fn load_packs(packs_dir: &Path, variables: &HashMap<&str, &str>) -> HashMap<String, String> {
    let mut packs = HashMap::new();
    let entries = match std::fs::read_dir(packs_dir) {
        Ok(e) => e,
        Err(_) => return packs,
    };

    let mut names: Vec<String> = entries
        .filter_map(Result::ok)
        .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
        .filter_map(|e| e.file_name().into_string().ok())
        .collect();
    names.sort();

    for name in names {
        let prompt_path = packs_dir.join(&name).join("prompt.md");
        if prompt_path.is_file() {
            let content = load_prompt_file(&prompt_path, variables);
            if !content.is_empty() {
                info!("Loaded pack '{}' ({} chars)", name, content.len());
                packs.insert(name, content);
            }
        }
    }
    packs
}

/// Concatenate prompt text for the given pack names.
fn pack_prompt_text(packs: &HashMap<String, String>, pack_names: &[&str]) -> String {
    let parts: Vec<&str> = pack_names
        .iter()
        .filter_map(|name| {
            if let Some(text) = packs.get(*name) {
                Some(text.as_str())
            } else {
                warn!("Pack '{}' not found", name);
                None
            }
        })
        .collect();
    parts.join("\n\n")
}

// ---------------------------------------------------------------------------
// System prompt builder
// ---------------------------------------------------------------------------

/// Builder for system prompts, initialized from bot config paths.
pub struct PromptBuilder {
    soul: String,
    dev_context: String,
    packs: HashMap<String, String>,
    bot_dir: PathBuf,
    worktrees_dir: Option<PathBuf>,
    agent_context_prompt: String,
}

impl PromptBuilder {
    /// Create a new `PromptBuilder` loading SOUL.md, `dev_context.md`, and packs/.
    pub fn new(bot_dir: &Path, user_data_dir: &Path, worktrees_dir: Option<&Path>) -> Self {
        let variables: HashMap<&str, &str> = HashMap::from([
            ("axi_user_data", user_data_dir.to_str().unwrap_or("")),
            ("bot_dir", bot_dir.to_str().unwrap_or("")),
        ]);

        let soul = load_prompt_file(&bot_dir.join("SOUL.md"), &variables);
        let dev_context = load_prompt_file(&bot_dir.join("dev_context.md"), &variables);
        let packs = load_packs(&bot_dir.join("packs"), &variables);

        let agent_context_prompt = format!(
            "You are an agent session in the Axi system — a Discord-based personal assistant \
             for a single user. You communicate through a dedicated Discord text channel. \
             The user reads your messages there. Keep responses concise and well-formatted \
             for Discord (markdown, code blocks).\n\
             \n\
             Key context:\n\
             - The user's profile and preferences are in USER_PROFILE.md at {}/USER_PROFILE.md\n\
             - You are one of several agent sessions. The master agent (Axi) coordinates via #axi-master.\n\
             - Your working directory is set by whoever spawned you.\n\
             - The user's timezone is US/Pacific.\n\
             \n\
             Communication rules:\n\
             - Never guess or fabricate answers. If you lack context, say so and look it up.\n\
             - Do NOT use Skill or EnterWorktree tools — they are not supported in Discord.\n\
             - AskUserQuestion IS supported — questions will be posted to Discord.\n\
             - TodoWrite IS supported — use it to track progress. Do NOT narrate the todo list.\n\
             - EnterPlanMode and ExitPlanMode ARE supported — use plan mode for non-trivial tasks.",
            bot_dir.display()
        );

        Self {
            soul,
            dev_context,
            packs,
            bot_dir: bot_dir.to_path_buf(),
            worktrees_dir: worktrees_dir.map(Path::to_path_buf),
            agent_context_prompt,
        }
    }

    /// Check if a CWD is within the axi-assistant codebase.
    fn is_axi_dev_cwd(&self, cwd: &str) -> bool {
        let bot_dir = self.bot_dir.to_string_lossy();
        if cwd.starts_with(bot_dir.as_ref()) {
            return true;
        }
        if let Some(wt) = &self.worktrees_dir {
            let wt_str = wt.to_string_lossy();
            if cwd.starts_with(wt_str.as_ref()) {
                return true;
            }
        }
        false
    }

    /// Build the master agent system prompt.
    pub fn master_prompt(&self) -> SystemPromptPreset {
        let mut append = format!("{}\n\n{}", self.soul, self.dev_context);
        let packs_text = pack_prompt_text(&self.packs, MASTER_PACKS);
        if !packs_text.is_empty() {
            append.push_str("\n\n");
            append.push_str(&packs_text);
        }
        SystemPromptPreset::claude_code(append)
    }

    /// Build a spawned agent system prompt.
    pub fn spawned_agent_prompt(
        &self,
        cwd: &str,
        packs: Option<&[&str]>,
        compact_instructions: Option<&str>,
    ) -> SystemPromptPreset {
        let mut append = if self.is_axi_dev_cwd(cwd) {
            format!("{}\n\n{}", self.soul, self.dev_context)
        } else {
            self.agent_context_prompt.clone()
        };

        // Resolve pack list
        let mut pack_names: Vec<&str> = packs
            .unwrap_or(DEFAULT_SPAWNED_PACKS)
            .to_vec();
        if self.is_axi_dev_cwd(cwd) {
            for p in AXI_DEV_PACKS {
                if !pack_names.contains(p) {
                    pack_names.push(p);
                }
            }
        }
        let packs_text = pack_prompt_text(&self.packs, &pack_names);
        if !packs_text.is_empty() {
            append.push_str("\n\n");
            append.push_str(&packs_text);
        }

        // Auto-load SYSTEM_PROMPT.md from CWD
        if let Some((cwd_prompt, mode)) = load_cwd_prompt(cwd) {
            if mode == "overwrite" {
                append = cwd_prompt;
            } else {
                append.push_str("\n\n");
                append.push_str(&cwd_prompt);
            }
        }

        // Compact instructions
        if let Some(instructions) = compact_instructions {
            append.push_str("\n\n# Context Compaction Instructions\n");
            append.push_str("When summarizing/compacting this conversation, prioritize preserving:\n");
            append.push_str("- ");
            append.push_str(instructions);
        }

        SystemPromptPreset::claude_code(append)
    }
}

/// Load `SYSTEM_PROMPT.md` from an agent's working directory.
///
/// Returns (content, mode) where mode is "append" (default) or "overwrite".
fn load_cwd_prompt(cwd: &str) -> Option<(String, String)> {
    let path = Path::new(cwd).join("SYSTEM_PROMPT.md");
    let content = std::fs::read_to_string(&path).ok()?;
    let content = content.trim().to_string();
    if content.is_empty() {
        return None;
    }

    let mut mode = "append".to_string();

    // Check for <!-- mode: overwrite --> directive
    if let Some(start) = content.find("<!-- mode:") {
        if let Some(end) = content[start..].find("-->") {
            let directive = &content[start..start + end + 3];
            if directive.contains("overwrite") {
                mode = "overwrite".to_string();
            }
            let cleaned = format!(
                "{}{}",
                content[..start].trim(),
                content[start + end + 3..].trim()
            )
            .trim()
            .to_string();
            info!(
                "Loaded CWD system prompt from {} ({} chars, mode={})",
                path.display(),
                cleaned.len(),
                mode
            );
            return Some((cleaned, mode));
        }
    }

    info!(
        "Loaded CWD system prompt from {} ({} chars, mode={})",
        path.display(),
        content.len(),
        mode
    );
    Some((content, mode))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prompt_hash_deterministic() {
        let p1 = SystemPromptPreset::claude_code("hello world".to_string());
        let p2 = SystemPromptPreset::claude_code("hello world".to_string());
        assert_eq!(p1.hash(), p2.hash());

        let p3 = SystemPromptPreset::claude_code("different".to_string());
        assert_ne!(p1.hash(), p3.hash());
    }

    #[test]
    fn pack_text_concatenation() {
        let mut packs = HashMap::new();
        packs.insert("a".to_string(), "Pack A content".to_string());
        packs.insert("b".to_string(), "Pack B content".to_string());

        let text = pack_prompt_text(&packs, &["a", "b"]);
        assert!(text.contains("Pack A content"));
        assert!(text.contains("Pack B content"));
    }

    #[test]
    fn pack_missing_skipped() {
        let packs = HashMap::new();
        let text = pack_prompt_text(&packs, &["nonexistent"]);
        assert!(text.is_empty());
    }

    #[test]
    fn is_axi_dev_cwd_detection() {
        let dir = tempfile::tempdir().unwrap();
        let bot_dir = dir.path().join("axi-assistant");
        let wt_dir = dir.path().join("axi-tests");
        std::fs::create_dir_all(&bot_dir).unwrap();
        std::fs::create_dir_all(&wt_dir).unwrap();

        // Write minimal SOUL.md
        std::fs::write(bot_dir.join("SOUL.md"), "soul").unwrap();
        std::fs::write(bot_dir.join("dev_context.md"), "dev").unwrap();

        let builder = PromptBuilder::new(&bot_dir, dir.path(), Some(&wt_dir));

        assert!(builder.is_axi_dev_cwd(bot_dir.to_str().unwrap()));
        assert!(builder.is_axi_dev_cwd(
            &format!("{}/some/sub", wt_dir.to_str().unwrap())
        ));
        assert!(!builder.is_axi_dev_cwd("/tmp/other"));
    }

    #[test]
    fn master_prompt_includes_soul() {
        let dir = tempfile::tempdir().unwrap();
        let bot_dir = dir.path().join("bot");
        std::fs::create_dir_all(&bot_dir).unwrap();
        std::fs::write(bot_dir.join("SOUL.md"), "I am Axi").unwrap();
        std::fs::write(bot_dir.join("dev_context.md"), "Dev context here").unwrap();

        let builder = PromptBuilder::new(&bot_dir, dir.path(), None);
        let prompt = builder.master_prompt();

        assert!(prompt.append.contains("I am Axi"));
        assert!(prompt.append.contains("Dev context here"));
        assert_eq!(prompt.preset, "claude_code");
    }

    #[test]
    fn spawned_agent_non_dev_cwd() {
        let dir = tempfile::tempdir().unwrap();
        let bot_dir = dir.path().join("bot");
        std::fs::create_dir_all(&bot_dir).unwrap();
        std::fs::write(bot_dir.join("SOUL.md"), "Soul").unwrap();
        std::fs::write(bot_dir.join("dev_context.md"), "Dev").unwrap();

        let builder = PromptBuilder::new(&bot_dir, dir.path(), None);
        let prompt = builder.spawned_agent_prompt("/tmp/work", None, None);

        // Non-dev CWD should get the mini agent context, not the full soul
        assert!(!prompt.append.contains("Soul"));
        assert!(prompt.append.contains("agent session in the Axi system"));
    }

    #[test]
    fn spawned_agent_with_compact_instructions() {
        let dir = tempfile::tempdir().unwrap();
        let bot_dir = dir.path().join("bot");
        std::fs::create_dir_all(&bot_dir).unwrap();
        std::fs::write(bot_dir.join("SOUL.md"), "Soul").unwrap();
        std::fs::write(bot_dir.join("dev_context.md"), "Dev").unwrap();

        let builder = PromptBuilder::new(&bot_dir, dir.path(), None);
        let prompt = builder.spawned_agent_prompt(
            "/tmp/work",
            Some(&[]),
            Some("preserve the test results"),
        );

        assert!(prompt.append.contains("Context Compaction"));
        assert!(prompt.append.contains("preserve the test results"));
    }

    #[test]
    fn cwd_prompt_overwrite_mode() {
        let dir = tempfile::tempdir().unwrap();
        let cwd = dir.path();
        std::fs::write(
            cwd.join("SYSTEM_PROMPT.md"),
            "<!-- mode: overwrite -->\nCustom prompt only",
        )
        .unwrap();

        let result = load_cwd_prompt(cwd.to_str().unwrap());
        assert!(result.is_some());
        let (content, mode) = result.unwrap();
        assert_eq!(mode, "overwrite");
        assert!(content.contains("Custom prompt only"));
        assert!(!content.contains("<!-- mode:"));
    }

    #[test]
    fn cwd_prompt_append_mode() {
        let dir = tempfile::tempdir().unwrap();
        let cwd = dir.path();
        std::fs::write(cwd.join("SYSTEM_PROMPT.md"), "Extra instructions").unwrap();

        let result = load_cwd_prompt(cwd.to_str().unwrap());
        assert!(result.is_some());
        let (content, mode) = result.unwrap();
        assert_eq!(mode, "append");
        assert_eq!(content, "Extra instructions");
    }
}
