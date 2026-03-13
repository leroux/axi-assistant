# Generate Behavioral Spec from Code

Analyzes a directory's source code and git history to produce a `SPEC.md` with:
- **Behaviors (B-codes)** — what the system does, in editable human-readable sentences
- **Invariants (I-codes)** — rules that must always hold, mined from past fix commits
- **Anchors** — links from spec entries to specific functions + commits

Incremental: if a `SPEC.md` already exists, it diffs against it and marks new/changed/removed entries.

**Usage:** `/gen-spec <directory>`
Example: `/gen-spec axi-py/axi` or `/gen-spec axi-rs/crates/axi/src`

You are generating a behavioral specification for the code in the target directory: `$ARGUMENTS`

If no argument is provided, ask the user which directory to focus on.

## Process

### Step 0: Initialize work log

Create `SPEC_LOG.md` in the target directory. This is a meticulous record of every decision, discovery, and judgment call made during spec generation — like a lab notebook.

Log format:
```markdown
# Spec Generation Log — {directory name}

Started: {timestamp}
Target: `{directory}`

## Decisions

| # | Timestamp | Step | Decision | Reasoning |
|---|-----------|------|----------|-----------|
| 1 | ... | Survey | Grouped channels.py under "Channel Management" not "Agent Lifecycle" | Channel creation is called from lifecycle but the logic is self-contained |

## Ambiguities Resolved

| # | Question | Answer (user) | Impact |
|---|----------|---------------|--------|
| 1 | "Is idle reminder part of Scheduling or Agent Lifecycle?" | Scheduling | Moved B7.4 from domain 2 to domain 7 |

## Commit Triage

| Hash | Message | Relevant? | Domain | Notes |
|------|---------|-----------|--------|-------|
| 3b091a9 | Fix interrupt_session: send SIGINT instead of kill | Yes | Interrupts | Diff confirms: changed signal.SIGKILL → signal.SIGINT |
| 44ed6ac | Remove .llm_cache from tracking | No | — | Housekeeping, no behavioral change |

## Coverage Gaps

| # | Observation | Status |
|---|-------------|--------|
| 1 | No test for typing indicator persistence across turns | Noted in I4.3 |

## Changes from Previous Spec

| # | Code | Change | Reason |
|---|------|--------|--------|
| 1 | B2.3 | NEW | Found new sleep→wake reconnect path |
| 2 | I5.1 | UNCHANGED | — |
```

Append to the log as you go through each step. Every subagent should return notes for the log, and the main session appends them. The log is the audit trail — if someone asks "why is this in the spec?" the answer should be in the log.

### Step 1: Discover the target

Resolve `$ARGUMENTS` relative to the repo root. List all source files in that directory (`.py`, `.rs`, or whatever is present). This is your scope — only read and anchor to files in this directory and its subdirectories.

### Step 2: Survey the code (subagent)

Spawn an Explore agent to survey the target directory. It should return:
- File list with module roles (one line each)
- Key function signatures per file
- Data flow sketch (how modules connect)
- Suggested behavioral domains with which files map to each

**Do not read source files in the main session.** The Explore agent does all the reading.

### Step 3: Confirm domains with user

Present the discovered domains to the user via AskUserQuestion:
- List each domain with a one-line description
- Note which files map to each domain
- Ask: "These are the domains I found. Add, remove, or adjust any?"

Wait for confirmation before proceeding. This avoids wasting parallel subagent work on wrong domains.

### Step 4: Mine git history (subagent)

Spawn a subagent to mine the full repo git history (not scoped to directory — files move):

```
git log --oneline --all --grep="[Ff]ix" --format="%h %s"
git log --oneline --all --format="%h %s" | head -100
```

The subagent should:
1. Read commit messages first; if vague, misleading, or clearly a squash, read the actual diff with `git show <hash>` to understand what changed
2. For squashed commits that bundle multiple fixes, extract each invariant separately
3. Determine which confirmed domains each fix is relevant to (by behavioral content, not file path)
4. Return a structured list: `{hash, summary, inferred_domains[], invariant_rule}`

Commit messages are often inaccurate — the diff is ground truth.

### Step 5: Check for existing SPEC.md

If `SPEC.md` already exists in the target directory:
1. Read it
2. Extract existing B-codes, I-codes, and anchors
3. Pass these to domain subagents so they can flag: **new** (not in existing spec), **changed** (behavior modified), **removed** (code deleted), **unchanged**
4. On output, preserve unchanged entries and mark new/changed/removed entries clearly

If no existing SPEC.md, generate from scratch.

### Step 6: Generate spec (subagents per domain)

For each confirmed domain, spawn a dedicated subagent with explicit inputs:
- **Files to read:** the specific source files for this domain (from step 2)
- **Relevant fix commits:** the hashes + summaries mapped to this domain (from step 4)
- **Existing spec entries:** any B/I codes from the existing SPEC.md for this domain (from step 5)
- **Output format:** the exact template below

Run domain agents in parallel where possible. The main session orchestrates and assembles — it should never hold more than one domain's worth of code in context at a time.

If a subagent is uncertain about scope or classification, it should flag the ambiguity and the main session should use AskUserQuestion to resolve it.

Each domain subagent returns:

```markdown
## N. Domain Name

### Behaviors
- B{N}.{M}: {what it does — human-readable, editable}

### Invariants
- I{N}.{M}: {what must always be true} [fix: {commit hash}]
  - Regression: {brief description of what went wrong}

### Anchors
- {file}:{function} @ {commit hash} — {brief context snippet}
```

Anchors pin to the commit where the behavior was last confirmed. When code moves or is refactored, the commit + context snippet lets you relocate it: search for the snippet in the current tree. The snippet should be a short, unique string from the code (a function signature, a distinctive comment, a key variable name) — enough to grep for, not a full block.

### Step 7: Assemble and write output

Collect all domain sections from subagents. Assemble into `SPEC.md` in the target directory.

Include a header:

```markdown
# Behavioral Specification — {directory name}

Generated: {date}
Source: `{target directory}`
Git range: {earliest commit}..{latest commit} (full repo history)

## How to use this spec

- **Behaviors** (B-codes) are editable — change what the system should do
- **Invariants** (I-codes) are regression guards — delete only if the underlying cause is eliminated
- **Anchors** link spec entries to code — update when code moves

---
```

End with a regression index:

```markdown
## Regression Index

| Code | Domain | Invariant | Fix Commit | Status |
|------|--------|-----------|------------|--------|
| I5.1 | Interrupts | Must use SIGINT not SIGKILL | 3b091a9 | Active |
| ... | ... | ... | ... | ... |
```

## Ambiguity handling

Use AskUserQuestion whenever you're unsure about:
- Whether a behavior belongs to the target directory's scope or is handled elsewhere
- How to classify a behavior (which domain, behavior vs invariant)
- Whether a fix commit is relevant to this directory's concerns
- The intent behind code that could be read multiple ways
- Whether two code paths are intentionally different or a bug

Don't guess. It's cheaper to ask than to produce a wrong spec entry that gets trusted later.

## Important notes

- Read actual code, don't guess from filenames
- Be specific in behaviors — "routes DM to active agent" not "handles messages"
- Invariants must describe the RULE, not the bug — "typing indicator must persist across turns" not "fixed typing bug"
- Anchors should reference function names where possible, not just files
- When a behavior spans multiple functions, list all of them
- Keep each behavior to one sentence — compound behaviors should be split
- If you find behaviors not in any domain above, add a new domain
- Only anchor to code in the target directory — don't reach into other directories
- Consolidate duplicate invariants — if the same rule was fixed multiple times, list one invariant with multiple fix commits
