# Build User Profile

You are conducting a conversational interview to build the user's profile.
The profile directory is at `%(axi_user_data)s/profile/`.

## Before Starting

1. Read `%(axi_user_data)s/profile/USER_PROFILE.md` to see the current state.
2. Read any existing ref files in `%(axi_user_data)s/profile/refs/` to understand what's already populated.
3. If the profile already has substantial content, ask: "Want to update a specific section, or redo the whole thing?"
4. If it's mostly blank/skeleton, start fresh from Phase 1.

## Interview Flow

Follow a Telos-lite structure: Problems → Mission → Goals → Projects, plus practical sections.
Ask open-ended questions in plain text. Let the user describe things in their own words.
Ask 1-2 questions at a time — do NOT dump a wall of questions.
Write results after each phase so progress isn't lost if the session is interrupted.

### Phase 1: Mission & Throughline

Ask:
- What are you fundamentally trying to accomplish? What's the throughline across everything you do?
- What problems drive you? What do you keep coming back to even when nobody's asking you to?

Write to `refs/values.md` (philosophy/worldview) and `refs/mission.md` (concrete mission).

### Phase 2: Goals & Priorities

Ask:
- What are your current goals? What does success look like?
- How do these goals relate to each other? Are some foundational to others?
- What are the biggest obstacles or challenges you face?

Write to `refs/goals.md` and `refs/problems.md`.

### Phase 3: Projects & Work

Ask:
- What are you actively working on right now? What's the status of each?
- Do you collaborate with anyone? Who, and on what?
- What tools, languages, and systems do you use?

Write to `refs/projects.md`, `refs/collaborators.md`, and `refs/tech.md`.

### Phase 4: Daily Life & Preferences

Ask:
- What does your daily routine look like? Any foundational habits?
- What are your key preferences? (diet, timezone, OS, etc.)
- Any interests or hobbies that Axi should know about? (music, games, food, health, pets, etc.)

Write to `refs/daily-practices.md` and any domain-specific refs that emerge naturally (e.g. `refs/music.md`, `refs/food.md`, `refs/health.md`).
Don't force categories — only create ref files for topics the user actually discusses.

### Phase 5: Build the Index

Update `USER_PROFILE.md` to serve as a **dispatch table** for agents. The format is:

**Section 1: Identity & Context**

Start with the line: `For detailed context, read the relevant ref file when these topics come up:`

Then a bullet list where each entry is a ref file with keyword triggers:
`- **Label** (\`profile/refs/<name>.md\`) — comma-separated keywords and phrases that should trigger reading this file`

The keywords must be specific and actionable — actual words the user would say or topics that would come up in conversation. Use the user's actual project names, collaborator names, tool names, and topic-specific vocabulary. Not vague categories.

Good example: `- **Food** (\`profile/refs/food.md\`) — food, vegan, Tidbits, FoodLP, nutrition, snacks`
Bad example: `- **Food** (\`profile/refs/food.md\`) — sustenance, nourishment, dietary needs`

For each ref file created during the interview, add an entry with keywords drawn from what the user actually said.

**Section 2: Preferences**

Inline key-value pairs for quick-reference facts that agents need constantly:
- Diet, timezone, OS, languages/tools
- Any strong behavioral preferences (e.g. "Never use nonvegan examples unless the topic requires it.")

These go directly in USER_PROFILE.md because agents see them on every request without needing to read a separate file.

Show the user the final USER_PROFILE.md and ask for corrections.
Remind them they can run `/build-user-profile` again anytime to update specific sections.

## Writing Rules

- All ref files go in `%(axi_user_data)s/profile/refs/`
- The index goes in `%(axi_user_data)s/profile/USER_PROFILE.md`
- Ref file paths in USER_PROFILE.md must be relative: `profile/refs/values.md` (not absolute paths)
- Use the user's own words — synthesize, don't rephrase into corporate language
- Be conversational. This is a dialogue, not an interrogation.
- After writing each section, show it to the user and ask for corrections before moving on.

## Scope

This skill ONLY writes profile files. Do NOT modify bot code, extensions, schedules, or anything else.
