# Flowchart Commands Reference

Flowchart commands are structured procedures (JSON flowcharts) that agents execute step by step. When you type `/command-name args` in a message, it's automatically wrapped in the task lifecycle via `/soul-flow`.

## How It Works

- Regular messages are wrapped in `/soul` (classify → route → hooks)
- `/command` messages are wrapped in `/soul-flow` (pre-task hook → execute command → post-task hook → completion check → record reporting → status update)
- `/soul` and `/soul-flow` themselves pass through unwrapped (they ARE the wrappers)
- `//raw message` bypasses all wrapping — sent directly to the agent

## Core Commands

- **`/soul`** — Core message handler. Classifies incoming messages as tasks or conversation, runs extension hooks (pre_task, execute, post_task, post_respond), and manages the full task lifecycle including record reporting and status updates.
- **`/soul-flow`** — Task lifecycle wrapper. Wraps any flowchart command with pre/post hooks, completion checks, record reporting, and status updates. You rarely invoke this directly — it's applied automatically when you run any `/command`.

## Extension Commands

Extensions can add their own flowchart commands in `extensions/<name>/commands/`. These are symlinked into `commands/` at startup so they're discoverable alongside core commands.

Notable extension commands (when the relevant extension is loaded):
- **`/mill`** — Auto-execute MinFlow deck cards, stopping when human approval is needed (e.g. plan review, ambiguous decisions).
- **`/mil`** — Auto-execute MinFlow deck cards with minimal human approval — only stops for very complex/ambiguous plans or critical research findings.

## Listing Available Commands

Use the `/flowchart-list` Discord slash command to see all available flowchart commands and their descriptions.
