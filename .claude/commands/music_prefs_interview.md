# Music Preferences Interview

You are conducting an interactive interview to build the user's music listening preferences profile.
The output file is at `%(axi_user_data)s/profile/refs/music-preferences.md`.

## Before Starting

1. Read the existing `music-preferences.md` file if it exists.
2. If it already has populated data (not just "Not yet populated" placeholders), tell the user what's currently set and ask: "Want to update a specific section, or redo the whole thing?"
3. If it's empty/template, start fresh from Phase 1.

## Interview Phases

Conduct these phases in order. Ask 1-2 questions at a time — do NOT dump a wall of questions.
Wait for the user to respond before moving to the next phase.
Write results to the file after each phase so progress isn't lost if the session is interrupted.

### Phase 1: Genre Exploration

The auto-dj system works with these genres:
ambient, drone, downtempo, lo-fi, minimal, IDM, deep house, minimal techno, dub, jazz fusion, glitch, breakbeat

Ask:
- Which of these genres do you gravitate toward most for everyday listening?
- Are there any you'd want to avoid entirely?
- Are there genres NOT on this list that you listen to regularly?

For each genre the user mentions positively, calibrate:
- Strong preference (0.8-1.0): "I love this, play it a lot"
- Moderate (0.5-0.7): "I like it sometimes / in the right context"
- Low (0.1-0.4): "Only occasionally / very specific moods"

Write the Genre Affinities section after this phase.

### Phase 2: Energy & Daily Rhythm

Show the user the current default energy curve (the table in the file) and ask:
- Does this match your actual day? When do you wake up, when do you do deep work, when do you wind down?
- Are there time blocks where the mood/energy feels wrong?
- What genres work best for your focus periods vs. creative periods vs. relaxation?

Update the Energy Curve table and Mood-to-Genre Mapping section after this phase.

### Phase 3: Characteristics & Discovery

Ask:
- Do you prefer instrumental music, or are vocals fine? Any vocal preferences (e.g. no lyrics, occasional vocals OK)?
- How much new music do you want to hear vs. familiar tracks? (Explain the 0.0-1.0 scale)
- Are there any hard avoids — specific sounds, vibes, or characteristics you never want?

Update Global Preferences and Anti-Preferences sections after this phase.

### Phase 4: Context & Nuance

Ask:
- Anything else about how you listen that I should know? Weekday vs. weekend differences? Seasonal preferences? Specific contexts where your taste shifts?
- Any specific artists, albums, or tracks that are reference points for your taste?

Write Context Notes after this phase.

### Phase 5: Confirm

- Show the user a brief summary of everything written
- Remind them they can run `/music-prefs` again anytime to update specific sections
- Write the final version of the file

## Writing Rules

- Keep the file in markdown format (not YAML frontmatter — it's a profile ref loaded into LLM prompts)
- Preserve the existing section structure (Genre Affinities, Energy Curve, etc.)
- Replace "Not yet populated" placeholders with real data
- The file should be self-contained and readable by both humans and LLMs
- Do not add sections that aren't in the template
- Genre names should match auto-dj vocabulary where possible (see list above), but additional genres are fine

## Scope

This skill ONLY writes the preferences file. Do NOT:
- Modify auto-dj plans or queue tracks
- Change any runtime behavior
- Create or modify extensions
- Write to any file other than music-preferences.md
