# Plan: Music Preferences Document + Axi Skill

## Deliverables

1. **Music preferences document** at `~/app-user-data/axi-assistant/profile/refs/music-preferences.md`
2. **Axi skill** (Discord slash command `/music-prefs`) that interactively populates it

---

## 1. Music Preferences Document

### Location & Format

**Path:** `~/app-user-data/axi-assistant/profile/refs/music-preferences.md`

Markdown with YAML frontmatter for machine-readable fields. This mirrors the existing profile refs pattern (plain .md files in `profile/refs/`). The frontmatter is parseable by any YAML library; the body provides human-readable context and nuance that an LLM can use but structured consumers can ignore.

**Why not pure JSON?** Profile refs are markdown files loaded into LLM system prompts (`prompts.py:75-82`). A markdown file with YAML frontmatter serves both audiences: structured data for auto-dj plan generation, and natural language for conversational context.

### Registration

Add to `USER_PROFILE.md` (line 13, after the Music entry):
```
- **Music preferences** (`profile/refs/music-preferences.md`) — listening preferences, genre affinities, energy curves, mood mappings, auto-dj context
```

### Schema

```yaml
---
# Music Preferences — structured data for auto-dj and Axi agents
# Last updated by /music-prefs skill

version: 1

# --- Genre Affinities ---
# Each genre gets a weight from 0.0 (never play) to 1.0 (strong preference).
# Genres not listed are neutral (implicit 0.5).
# Genre names should match auto-dj's vocabulary:
#   ambient, drone, downtempo, lo-fi, minimal, IDM, deep house,
#   minimal techno, dub, jazz fusion, glitch, breakbeat
# Additional genres can be added freely.
genres:
  ambient: 0.8
  drone: 0.6
  downtempo: 0.7
  lo-fi: 0.5
  minimal: 0.9
  IDM: 0.8
  deep house: 0.7
  minimal techno: 0.8
  dub: 0.6
  jazz fusion: 0.5
  glitch: 0.4
  breakbeat: 0.3
  # User may add more genres here

# --- Energy Preferences ---
# Baseline energy curve by time of day.
# Maps to auto-dj plan blocks (plan.py DEFAULT_PLAN_BLOCKS).
# energy: 0.0 (silence) to 1.0 (peak intensity)
energy_curve:
  - time: "00:00-05:00"
    energy: 0.05
    mood: sleep
    notes: ""
  - time: "05:00-07:00"
    energy: 0.10
    mood: contemplative
    notes: ""
  - time: "07:00-09:00"
    energy: 0.25
    mood: gentle
    notes: ""
  - time: "09:00-12:00"
    energy: 0.45
    mood: focused
    notes: ""
  - time: "12:00-13:00"
    energy: 0.50
    mood: midday
    notes: ""
  - time: "13:00-16:00"
    energy: 0.40
    mood: afternoon
    notes: ""
  - time: "16:00-18:00"
    energy: 0.50
    mood: creative
    notes: ""
  - time: "18:00-20:00"
    energy: 0.30
    mood: evening
    notes: ""
  - time: "20:00-22:00"
    energy: 0.15
    mood: night
    notes: ""
  - time: "22:00-23:59"
    energy: 0.05
    mood: sleep
    notes: ""

# --- Global Preferences ---
prefer_instrumental: true        # Bias toward instrumental tracks
preferred_bpm_range: [55, 140]   # Hard floor/ceiling for any block
weekday_vs_weekend: true         # Whether to vary plans by day type

# --- Mood-to-Genre Mapping ---
# When the user says a mood word, what genres fit?
# Auto-dj plan generation uses this to pick genres per block.
mood_genres:
  sleep: [drone, ambient]
  contemplative: [ambient, drone]
  gentle: [ambient, downtempo, lo-fi]
  focused: [minimal, IDM, deep house]
  midday: [jazz fusion, dub, downtempo]
  afternoon: [downtempo, IDM, minimal]
  creative: [deep house, minimal techno, dub]
  evening: [downtempo, lo-fi, jazz fusion]
  night: [ambient, drone, downtempo]

# --- Anti-Preferences ---
# Genres or characteristics to avoid or downweight.
avoid:
  genres: []          # e.g. ["pop", "country"]
  characteristics: [] # e.g. ["heavy vocals", "aggressive drops"]

# --- Discovery ---
# How much novelty vs. familiarity the user wants.
discovery_balance: 0.3  # 0.0 = only known tracks, 1.0 = maximize new discoveries

# --- Context Notes ---
# Freeform notes about listening context that inform plan generation.
context_notes: []
# e.g.:
# - "I like drone/ambient for meditation, not as background noise"
# - "Jazz fusion works for lunch but not deep work"
# - "Weekend mornings can be more adventurous than weekday mornings"
---
```

### Design Rationale

**Compatible with auto-dj:** The `genres` keys match `plan.py:19-30` genre vocabulary. The `energy_curve` entries map 1:1 to `DEFAULT_PLAN_BLOCKS`. The `mood_genres` mapping mirrors how `selector.py:154-229` scores genre overlap. Auto-dj plan generation can read this file to customize daily plans instead of using hardcoded defaults.

**Extensible:** New fields (artists, specific albums, tempo preferences per genre) can be added without breaking existing consumers. The `context_notes` field captures nuance that doesn't fit structured fields.

**Not duplicating auto-dj state:** This file captures *preferences* (what the user likes). Auto-dj handles *runtime state* (play history, disliked tracks in SQLite at `track_db.py:40-42`, current plan). No overlap.

---

## 2. Axi Skill: `/music-prefs`

### Pattern

Follows the `/telos` pattern from `main.py:1766-1816`:
- Discord slash command registered on the bot
- Loads instruction file from `.claude/commands/music_prefs_interview.md`
- Injects instructions as a query into the agent session
- Agent conducts interactive conversation, writes results to the preferences file

### Implementation Files

1. **`main.py`** — Add `/music-prefs` slash command (same pattern as `/telos` at lines 1766-1816)
2. **`.claude/commands/music_prefs_interview.md`** — Interview instructions for Claude

### Slash Command (main.py)

```python
@bot.tree.command(
    name="music-prefs",
    description="Interactive music preferences interview — builds your listening profile for auto-dj.",
)
@app_commands.autocomplete(agent_name=agent_autocomplete)
async def music_prefs_cmd(interaction, agent_name=None):
    # Same lifecycle as /telos: resolve agent, acquire lock, wake if needed,
    # inject interview instructions, stream response.
    # Output path: ~/app-user-data/axi-assistant/profile/refs/music-preferences.md
```

### Interview Instructions (`.claude/commands/music_prefs_interview.md`)

The instruction file tells Claude how to conduct the interview. Key sections:

#### Conversation Flow

**Phase 1: Read existing state**
- Check if `music-preferences.md` already exists
- If yes, load it and tell the user what's currently set — offer to update specific sections rather than starting from scratch
- If no, start fresh

**Phase 2: Genre exploration** (2-3 questions)
- Present the auto-dj genre vocabulary and ask which genres the user gravitates toward
- Ask which genres to avoid entirely
- For each favored genre, calibrate intensity: "Do you want this a lot or just sometimes?"
- Write genre weights (0.0-1.0)

**Phase 3: Energy & mood** (2-3 questions)
- Show the default energy curve and ask if it matches their actual day
- Ask about specific time blocks: "The default has focused/minimal at 9am-noon — does that match your mornings?"
- Capture mood-to-genre overrides: "When you need to focus, what genres work?"

**Phase 4: Characteristics** (1-2 questions)
- Instrumental vs. vocal preference
- Discovery appetite: "Do you want mostly familiar tracks, or lots of new stuff?"
- Any hard avoids (characteristics, vibes, etc.)

**Phase 5: Context & nuance** (open-ended)
- "Anything else about how you listen? Specific contexts, exceptions, weekend vs weekday differences?"
- Capture as `context_notes`

**Phase 6: Write & confirm**
- Write the complete `music-preferences.md` file
- Show the user a summary of what was written
- Remind them they can run `/music-prefs` again anytime to update

#### Key Behaviors

- **Conversational, not interrogative.** Ask 1-2 questions at a time, not a wall of questions. Wait for responses between phases.
- **Respect existing data.** If the file already exists, offer targeted updates ("Want to adjust your genre weights, or is there a specific section to change?")
- **Use examples.** "Do you want IDM during deep work, or is that too glitchy for focus?"
- **Write incrementally.** Update the file after each phase so progress isn't lost if the session is interrupted.
- **Stay in scope.** This skill only writes the preferences file. It doesn't modify auto-dj plans, queue tracks, or change any runtime behavior.

### Output Path

The skill writes to `%(axi_user_data)s/profile/refs/music-preferences.md` using the `%(axi_user_data)s` variable from `extensions.py:38` for portability.

---

## 3. Integration with Existing Patterns

### Profile System

- New ref file registered in `USER_PROFILE.md` with trigger keywords
- `prompts.py:82` already rewrites `profile/refs/` paths to absolute — no changes needed
- Agents that read the user profile will see the music-preferences ref listed and can load it when music topics come up

### Auto-DJ Extension

- The auto-dj extension's `prompt.md` already describes plan generation and genre vocabulary (`extensions/auto-dj/prompt.md:108-111`)
- A future change (out of scope) could update the auto-dj plan generation scheduled task to read `music-preferences.md` instead of using hardcoded defaults
- The preferences document is designed to be compatible with this — genre names match, energy curve maps to plan blocks

### Extension System

No new extension needed. This is a standalone skill (slash command + instruction file), not a prompt fragment that all agents need. The auto-dj extension already exists and can reference the preferences file when needed.

### What This Does NOT Do

- Does not build an agentic track selection system
- Does not modify the auto-dj daemon or its selector
- Does not create feedback loops or automated preference learning
- Does not change how plans are generated (that's a separate integration)

---

## File Changes Summary

| File | Action | Description |
|------|--------|-------------|
| `~/app-user-data/axi-assistant/profile/refs/music-preferences.md` | Create | New preferences document |
| `~/app-user-data/axi-assistant/profile/USER_PROFILE.md` | Edit line 13 | Add music-preferences ref entry |
| `axi/main.py` | Add ~80 lines after line 1816 | `/music-prefs` slash command |
| `.claude/commands/music_prefs_interview.md` | Create | Interview instructions |
