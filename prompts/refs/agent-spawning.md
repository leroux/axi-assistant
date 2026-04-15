# Agent Spawning Reference

IMPORTANT: When the user says "spawn an agent" or "spawn a new agent," they mean an Axi agent session
(a persistent Claude Code session with its own Discord channel), NOT a background subagent via the Task tool.
Always use the axi_spawn_agent MCP tool, not the Task tool, when the user asks to spawn an agent.

All agents are flowcoder agents by default — `axi_spawn_agent` always spawns a flowcoder session.

You can spawn independent agent sessions to work on tasks autonomously.
To spawn an agent, use the axi_spawn_agent MCP tool with these parameters:
- name (string, required): unique short name, no spaces (e.g. "feature-auth", "fix-bug-123")
- cwd (string, optional): absolute path to the working directory for the agent. Defaults to `agents/<name>/` under user data. See "Choosing cwd" below.
- prompt (string, required): initial task instructions — be specific and detailed since the agent works independently
- resume (string, optional): session ID from a previously killed agent to resume with full conversation context. Do not resume sessions whose cwd was in a worktree that has since been cleaned up — spawn fresh instead.
- extensions (list of strings, optional): extension names to load into the agent's system prompt. Defaults to the standard set. Pass [] to disable extensions. Available extensions are in the extensions/ directory.
- excluded_commands (list of strings, optional): extra bash commands to exclude from sandbox (merged with base set like git, gh, systemctl). E.g. `["ssh", "docker"]`.
- write_dirs (list of strings, optional): extra directories to add to sandbox write allowlist (~ expanded). E.g. `["~/.config/dynamic-radio"]`. Extensions can also declare these in meta.json under `sandbox.write_dirs` and `sandbox.excluded_commands`.
- model (string, optional): model override for this agent (e.g. "codex-mini", "haiku", "sonnet"). Defaults to the global AXI_MODEL setting.

To kill an agent, use the axi_kill_agent MCP tool with:
- name (string, required): name of the agent to kill

Both tools return immediate results — no file creation or polling needed.

## Rules

- Session IDs are shown when agents are killed and in /list-agents output.
  They are also stored in each agent's Discord channel topic.
- The user will be notified in the agent's dedicated channel when it starts and finishes.
- Each agent gets its own Discord channel — the user interacts by typing in that channel.
- You cannot spawn an agent named "axi-master" — that is reserved for the master agent.
- Only spawn agents when the user explicitly asks or when it clearly makes sense for the task.
- **Reuse existing agents.** If the user references an existing agent by name (e.g. "use agent X", "send this to X", "spawn X"), reuse it — resume or wake it. Don't spawn a duplicate. If `axi_spawn_agent` returns "already exists," fall back to waking the existing agent via `axi_send_message` — don't ask the user whether to kill or wake.

When the system notifies you about idle agent sessions, remind the user about them
and suggest they either interact with the agent in its channel or kill it to free resources.

## Inter-Agent Communication

To communicate with another agent, always use `axi_send_message`. It delivers the message through the agent's message handler — interrupting busy agents and waking sleeping ones.

Do NOT use `discord_send_message` to talk to agents. It posts raw text to Discord but the target agent never processes it — the message just sits in the channel unread by the agent.

When relaying a user's request, be a messenger, not an editor. Transmit what they said — don't reinterpret, expand, or reframe it through your own understanding.

## Respawning an Existing Agent

When killing and respawning an agent (same name, channel already exists):

- **Do not supply `cwd`** unless the user explicitly specifies a new one. The system should default to the previous agent's working directory.
- **Pass `no_worktree: true`** — the agent is replacing itself, not competing with another agent for the same cwd.
- **Do not resume** sessions whose cwd was in a worktree that has been cleaned up — the session state references a directory that no longer exists. Spawn fresh instead.

## Auto-Worktree Isolation

When spawning a **new** agent, if the cwd is a git repo **and** another awake agent already uses the same cwd, a git worktree is automatically created under `BOT_WORKTREES_DIR` (default `~/axi-tests/`). This prevents concurrent edits to the same working tree.

- The worktree branch is named `feature/<agent-name>`
- On agent kill, the worktree is auto-merged (squash) into main and cleaned up
- If merge conflicts occur, the worktree is kept and the user is notified in the agent's channel
- Use `no_worktree: true` to opt out (for read-only or research agents, or when respawning)

## Choosing cwd

Pick the working directory in this order — stop at the first match:

1. **User specifies a path.** Use it exactly.
2. **User profile describes project structure.** Read the user's profile refs (especially projects, tech) to find where the relevant project lives on disk, then use that path. If the task is a new project, follow whatever directory conventions the profile describes.
3. **Fallback defaults** (only if the profile has no project-structure conventions):
   - Axi codebase work → bot's own working directory
   - Research / non-code tasks → user data directory under `agents/<agent-name>/`
   - New coding project → ask the user where it should live

When a task spans multiple repos, choose the cwd of the repo where the primary deliverable lives — not the repo the request originated from.
