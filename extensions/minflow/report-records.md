Use the `minflow` CLI to update task records. **NEVER read or write the workspace JSON file directly.**

Key update commands:
- `minflow card done <deck-id> <card-id>` — mark a card complete
- `minflow card add <deck-id> "<text>"` — add a new card
- `minflow card update <deck-id> <card-id> --text "<text>"` — update card text
- `minflow deck update <id> [--status x] [--done x] [--notes x]` — update deck fields
- `minflow undo` / `minflow redo` — undo/redo last mutation

**Card conventions:**
- Keep card names short — they render on small visual cards in the GUI.
- **Always break tasks into multiple cards**: plan -> implement -> test -> commit/push. If a deck has only a single vague card, replace it with this breakdown. One card = one clear step.
- When a task involves multiple distinct feature areas, each area gets its own plan -> implement -> test cycle.
- When work is completed that has no matching card, create one (already completed) so all work is tracked.
- After completing a card, proactively discuss the next card with the user but do NOT start executing until confirmed.
- Update deck text fields (`done`, `notes`, `status`) when milestones are completed or context changes.

**Single-card check** — after updating records, check if the deck now has only one remaining incomplete card. If that card is vague (e.g. "implement feature X"), replace it with the standard breakdown:
1. Run `minflow card list <deck-id>` to count incomplete cards.
2. If only one remains and its text is a broad task, replace it with plan -> implement -> test -> commit/push cards.
3. Remove the original vague card.

If the `minflow` CLI is not available, fall back to a RECORDS.md file in your working directory.
