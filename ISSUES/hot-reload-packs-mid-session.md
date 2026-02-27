# Hot-reload packs on running agent sessions

**Type:** Enhancement idea
**Date:** 2026-02-27
**Status:** Idea

## Problem

Pack prompts are baked into the Claude CLI subprocess at launch via `--append-system-prompt`. The SDK has no protocol message to update the system prompt mid-session. The only way to change packs is to restart the bot (which recreates all agents).

This means:
- Editing a pack's `prompt.md` has no effect on running agents
- Adding a new pack requires a full restart
- You can't experiment with pack changes on a live agent

## SDK Constraints

The Claude Agent SDK passes the system prompt as a CLI argument when spawning the subprocess (`subprocess_cli.py` line 171-182). `client.query()` only writes user messages to stdin.

Mid-session controls that DO exist: `set_permission_mode`, `set_model`, `interrupt`.
System prompt update: **not supported**.

## Possible Approaches

### 1. Sleep-wake cycle (works today)
Kill the client (`session.client = None`), update `session.system_prompt`, let it re-wake on next message. This starts a fresh CLI subprocess with the new prompt. Conversation history is preserved if the session ID is reused via `--resume`, but unclear whether the CLI honors the new system prompt on resume or uses the original session's prompt.

**Open question:** Does `--resume` with a different `--append-system-prompt` use the new prompt or the original?

### 2. `/reload-packs` command
A slash command that:
1. Calls `_load_packs()` to re-scan the packs directory
2. Rebuilds `_PACKS` dict and `MASTER_SYSTEM_PROMPT`
3. Optionally sleep-wakes specified agents (or all) with updated prompts

### 3. Upstream SDK feature request
Request `set_system_prompt` as a control protocol message, similar to `set_permission_mode` and `set_model`. This would be the clean solution but depends on Anthropic.

## Priority

Low. Bot restarts are fast (~4 seconds) and reload everything. This only matters if pack iteration becomes frequent enough that restarts are disruptive.
