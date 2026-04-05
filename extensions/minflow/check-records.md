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

**Cards** are tasks inside a deck, ordered by priority — the top card is the next task to complete.

When starting work on a project, recommend the top card and offer to start on it — don't list all cards and ask the user to pick.

**Before recommending a card**, check if you already have this deck's context loaded:
1. Run `minflow deck get <id>` to fetch the deck with its text fields.
2. Read the `done`, `notes`, and `status` fields to understand what's been accomplished and what's pending.
3. Only then look at the top incomplete card and recommend it — with context from the text fields, not just the card title.

If the `minflow` CLI is not available, fall back to a RECORDS.md file in your working directory.
