# MinFlow

MinFlow is the user's visual task management app with decks (project containers) and cards (tasks).

Use the `minflow` CLI to look up relevant tasks and context. **NEVER read or write the workspace JSON file directly.**

Key lookup commands:
- `minflow deck list` — list all decks (projects)
- `minflow deck get <id>` — get a deck with its cards and text fields
- `minflow card list <deck-id>` — list cards in a deck

**Decks** are project containers on a visual canvas. Each has a title, cards (tasks), and text fields:
- `description` — one-liner project overview
- `status` — lifecycle status (idea, planning, active, prototype, stable, paused, blocked, done)
- `done` — completed milestones narrative
- `notes` — open questions, references, misc context

Read deck text fields (via `minflow deck get`) to understand project context before making changes.

**Cards** are tasks inside a deck, ordered by priority — the top card is the next task to complete.

When starting work on a project, recommend the top card and offer to start on it — don't list all cards and ask the user to pick.

If the `minflow` CLI is not available, fall back to a RECORDS.md file in your working directory.

## Finding Your Deck

If no deck ID is provided, infer it from context — your agent name, working directory, and the user's project list. Run `minflow deck list` and match by title. Don't ask the user which deck to use unless there's genuine ambiguity.

**Be careful not to pick the wrong deck.** Verify your match makes sense before writing to it — updating the wrong deck corrupts another project's task history. If multiple decks could match, state your best guess and confirm with the user before proceeding.

## Conventions

- Cards are ordered by priority — the top card in a deck is the next task to complete
- **Priority = card order.** To change priority, use `minflow card reorder <deck-id> <card-id> <index>` (0 = top). Do NOT rename cards with priority labels — move them.
- **`--top` vs `--bottom` = now vs later.** Use `--top` when the card is more urgent than what's currently at the top. Use `--bottom` when it can wait. When adding a sequence with `--top`, add in reverse order (last step first) — `--top` is a stack (LIFO). With `--bottom`, add in natural order — it's a queue (FIFO).
- **Always break tasks into multiple cards** following the default progression: **plan -> implement -> test -> commit/push**. If a deck has only a single vague card, replace it with this breakdown. One card = one clear step.
- When a task involves multiple distinct feature areas, each area gets its own plan -> implement -> test cycle. Don't collapse an area into a single card just because there are many areas.
- Only mark a card done (`minflow card done`) when the outcome is **verified correct** — not just when you think you're finished. If anything unexpected happened during execution (wrong environment, errors you worked around, partial results, untested assumptions), the card is not done. Verify before completing.
- **Plan/design cards require explicit user approval.** Only mark them done when the user explicitly approves — e.g. "looks good", "approved", "go ahead and implement". Telling you to implement the plan counts as approval. Presenting a plan does not. When in doubt, ask.
- **When a new topic interrupts the current one.** Before engaging the new topic, push the current unfinished topic as a card with `--top` (it's urgent — you need to return to it next). When the new topic resolves, the interrupted topic will naturally be the next card. Only do this for substantive topics with open action items — not every minor conversational tangent.
- After completing a card, move to the next card in the deck. Discuss it briefly with the user before executing.
- When the user mentions "cards" and "decks" without context, they mean MinFlow
- When referencing MinFlow data, always cite the deck ID and card ID. Example: "card `mnj7p0wn` in deck `mm5jyp`"

## Flowchart Commands

MinFlow provides two auto-execution flowchart commands:
- **`/mill`** — Auto-execute deck cards, stopping when human approval is needed (e.g. plan review, ambiguous decisions).
- **`/mil`** — Auto-execute deck cards with minimal human approval — only stops for very complex/ambiguous plans or critical research findings.

Use these when you want to work through multiple cards in a deck without manually advancing each one.

## MinFlow and Core Files

MinFlow must never be referenced in core files (SOUL.md, soul.json, axi_codebase_context.md, bot.py, handlers.py, supervisor.py, prompts.py). MinFlow-specific instructions belong only in MinFlow extensions and MinFlow-specific flowcharts (mil.json, mill.json).
