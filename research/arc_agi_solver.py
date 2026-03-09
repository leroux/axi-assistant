"""
Self-modifying ARC-AGI-2 solver harness.

Three moving parts:
  .arc/knowledge.md   — pattern catalog: what types of transformations exist,
                         what reasoning works for each, what mistakes to avoid
  .arc/strategies.py  — hot-reloadable solver strategies (the agent rewrites these)
  this file           — fixed harness loop, grid utilities, validation

The loop:
  1. Analyze the puzzle's grid features (stable code, not self-modifying)
  2. Consult knowledge base for similar patterns
  3. Run solver strategy (hot-reloadable) to hypothesize a transformation rule
  4. Validate hypothesis against ALL training pairs
  5. If wrong: evolve strategy, reload, retry
  6. If right: apply to test input, update knowledge base

Over time the knowledge base grows and the strategies get smarter about
recognizing pattern types. The agent can also add new grid analysis functions
to the strategies module.
"""

import importlib.util
import json
import os
import shutil

from flowcoder import runtime

ARC_DIR = ".arc"
KNOWLEDGE_FILE = os.path.join(ARC_DIR, "knowledge.md")
STRATEGIES_FILE = os.path.join(ARC_DIR, "strategies.py")
STRATEGIES_BACKUP = os.path.join(ARC_DIR, "strategies.py.bak")

# ---------------------------------------------------------------------------
# Grid utilities (stable — these don't self-modify)
# ---------------------------------------------------------------------------

def grid_to_str(grid):
    """Render a grid as a readable string with color numbers."""
    return "\n".join(" ".join(str(c) for c in row) for row in grid)


def grid_features(grid):
    """Extract basic features for pattern matching."""
    h, w = len(grid), len(grid[0]) if grid else 0
    flat = [c for row in grid for c in row]
    colors = sorted(set(flat))
    color_counts = {c: flat.count(c) for c in colors}
    bg = max(color_counts, key=color_counts.get)  # most common = background

    # Symmetry checks
    h_sym = all(grid[r] == grid[r][::-1] for r in range(h))
    v_sym = all(grid[r] == grid[h - 1 - r] for r in range(h // 2))

    return {
        "height": h,
        "width": w,
        "num_colors": len(colors),
        "colors": colors,
        "background_color": bg,
        "color_counts": color_counts,
        "h_symmetric": h_sym,
        "v_symmetric": v_sym,
        "is_square": h == w,
    }


def pair_features(train_pairs):
    """Compare input/output features across training pairs."""
    analyses = []
    for pair in train_pairs:
        inp_f = grid_features(pair["input"])
        out_f = grid_features(pair["output"])
        analyses.append({
            "input": inp_f,
            "output": out_f,
            "size_changes": out_f["height"] != inp_f["height"] or out_f["width"] != inp_f["width"],
            "color_count_changes": out_f["num_colors"] != inp_f["num_colors"],
            "new_colors": [c for c in out_f["colors"] if c not in inp_f["colors"]],
            "removed_colors": [c for c in inp_f["colors"] if c not in out_f["colors"]],
        })
    return analyses


def validate_rule(rule_description, train_pairs, runtime_ref):
    """Ask the agent to apply the hypothesized rule to each training input
    and compare against expected output. Returns (pass, details)."""
    all_correct = True
    details = []

    for i, pair in enumerate(train_pairs):
        runtime_ref.clear()
        predicted = runtime_ref.query(
            f"Apply this transformation rule to the input grid. "
            f"Output ONLY the resulting grid as a JSON 2D array, nothing else.\n\n"
            f"Rule: {rule_description}\n\n"
            f"Input grid:\n{grid_to_str(pair['input'])}",
            output_schema={
                "type": "object",
                "properties": {"grid": {"type": "array"}},
                "required": ["grid"],
            },
        )

        correct = predicted["grid"] == pair["output"]
        details.append({
            "pair": i,
            "correct": correct,
            "expected": pair["output"],
            "predicted": predicted["grid"],
        })
        if not correct:
            all_correct = False

    return all_correct, details


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

def load_knowledge():
    if os.path.exists(KNOWLEDGE_FILE):
        with open(KNOWLEDGE_FILE) as f:
            return f.read()
    return ""


def save_knowledge(content):
    os.makedirs(ARC_DIR, exist_ok=True)
    with open(KNOWLEDGE_FILE, "w") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Strategy module (hot-reloadable)
# ---------------------------------------------------------------------------

DEFAULT_STRATEGIES = '''\
"""
Solver strategies for ARC-AGI-2 puzzles.
This module gets rewritten by the agent as it learns what works.
"""


def classify(features, knowledge):
    """Given pair_features and knowledge base, guess the pattern type.
    Returns a short label like 'rotation', 'flood-fill', 'color-map', etc.
    Returns None if unknown — the agent will reason from scratch."""
    return None


def build_analysis_prompt(puzzle_str, features, classification, knowledge):
    """Build the prompt that asks the agent to find the transformation rule.
    This is the main thing that evolves — better prompts find rules faster."""
    prompt = """Look at these input/output grid pairs. Find the transformation rule.

Think step by step:
1. What objects or patterns exist in each input?
2. How does each output relate to its input?
3. What is the EXACT rule that transforms every input to its output?

Be precise — the rule must work for ALL training pairs.

"""
    prompt += puzzle_str

    if classification:
        prompt += f"\\n\\nThis looks like a {classification}-type puzzle."

    if knowledge:
        prompt += f"\\n\\nRelevant patterns from past puzzles:\\n{knowledge}"

    prompt += "\\n\\nState the transformation rule clearly and concisely."
    return prompt
'''


def load_strategies():
    os.makedirs(ARC_DIR, exist_ok=True)
    if not os.path.exists(STRATEGIES_FILE):
        with open(STRATEGIES_FILE, "w") as f:
            f.write(DEFAULT_STRATEGIES)

    spec = importlib.util.spec_from_file_location("strategies", STRATEGIES_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Main harness
# ---------------------------------------------------------------------------

def format_puzzle(puzzle):
    """Format puzzle for display in prompts."""
    parts = []
    for i, pair in enumerate(puzzle["train"]):
        parts.append(f"Training pair {i + 1}:")
        parts.append(f"Input:\n{grid_to_str(pair['input'])}")
        parts.append(f"Output:\n{grid_to_str(pair['output'])}")
        parts.append("")
    for i, case in enumerate(puzzle["test"]):
        parts.append(f"Test input {i + 1}:")
        parts.append(grid_to_str(case["input"]))
    return "\n".join(parts)


def main():
    args = runtime.start()
    puzzle = json.loads(args["puzzle"])  # ARC-AGI JSON format
    puzzle_id = args.get("id", "unknown")

    knowledge = load_knowledge()
    features = pair_features(puzzle["train"])
    puzzle_str = format_puzzle(puzzle)

    best_rule = None
    best_details = None

    for attempt in range(5):
        strategies = load_strategies()

        # Classify the pattern type
        classification = strategies.classify(features, knowledge)
        if classification:
            print(f"Pattern classified as: {classification}")

        # Build the analysis prompt (this is what evolves)
        prompt = strategies.build_analysis_prompt(
            puzzle_str, features, classification, knowledge,
        )

        # Find the rule
        runtime.clear()
        hypothesis = runtime.query(
            prompt,
            output_schema={
                "type": "object",
                "properties": {
                    "rule": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": ["rule"],
            },
        )

        print(f"Attempt {attempt + 1} hypothesis: {hypothesis['rule']}")

        # Validate against training pairs
        correct, details = validate_rule(hypothesis["rule"], puzzle["train"], runtime)

        passed = sum(1 for d in details if d["correct"])
        total = len(details)
        print(f"Validation: {passed}/{total} training pairs correct")

        if correct:
            best_rule = hypothesis["rule"]
            best_details = details
            break

        # Track best so far
        if best_details is None or passed > sum(1 for d in best_details if d["correct"]):
            best_rule = hypothesis["rule"]
            best_details = details

        # --- EVOLVE: rewrite strategies based on failure ---
        if attempt < 4:
            shutil.copy(STRATEGIES_FILE, STRATEGIES_BACKUP)

            with open(STRATEGIES_FILE) as f:
                current_strategies = f.read()

            runtime.clear()
            new_code = runtime.query(
                f"""You are improving an ARC-AGI-2 puzzle solver. The current strategy
failed to find the right rule.

PUZZLE (training pairs):
{puzzle_str}

ATTEMPTED RULE: {hypothesis['rule']}
REASONING: {hypothesis.get('reasoning', '(none)')}

VALIDATION FAILURES:
{json.dumps([d for d in details if not d['correct']], indent=2, default=str)}

CURRENT strategies.py:
```python
{current_strategies}
```

Rewrite strategies.py to improve the solver. Ideas:
- Make build_analysis_prompt more specific about what to look for
- Add pattern-specific hints to classify()
- Break down the reasoning differently
- Add steps like "count objects first" or "check for symmetry"

Output ONLY the Python code. Keep the same interface:
  classify(features, knowledge) -> str|None
  build_analysis_prompt(puzzle_str, features, classification, knowledge) -> str"""
            )

            try:
                # Test-compile before saving
                compile(new_code, STRATEGIES_FILE, "exec")
                with open(STRATEGIES_FILE, "w") as f:
                    f.write(new_code)
                print("Strategies rewritten, hot-reloading...")
            except SyntaxError as e:
                print(f"Rewrite had syntax error ({e}), keeping previous version")
                shutil.copy(STRATEGIES_BACKUP, STRATEGIES_FILE)

    # --- APPLY TO TEST INPUT ---
    test_outputs = []
    for i, case in enumerate(puzzle["test"]):
        runtime.clear()
        result = runtime.query(
            f"Apply this transformation rule to the input grid. "
            f"Output ONLY the resulting grid as a JSON 2D array.\n\n"
            f"Rule: {best_rule}\n\n"
            f"Input grid:\n{grid_to_str(case['input'])}",
            output_schema={
                "type": "object",
                "properties": {"grid": {"type": "array"}},
                "required": ["grid"],
            },
        )
        test_outputs.append(result["grid"])

    # --- UPDATE KNOWLEDGE BASE ---
    runtime.clear()
    updated_knowledge = runtime.query(
        f"""You are updating a knowledge base for an ARC-AGI-2 puzzle solver.

PUZZLE ID: {puzzle_id}

PUZZLE:
{puzzle_str}

RULE FOUND: {best_rule}
VALIDATION: {sum(1 for d in best_details if d['correct'])}/{len(best_details)} correct
ATTEMPTS NEEDED: {attempt + 1}

GRID FEATURES: {json.dumps(features, indent=2, default=str)}

CURRENT KNOWLEDGE BASE:
{knowledge if knowledge else "(empty)"}

Update the knowledge base. Organize by pattern type. For this puzzle, record:
- Pattern type (e.g., rotation, reflection, color-mapping, flood-fill, scaling, ...)
- Key grid features that signal this pattern type
- What reasoning approach worked (or what to try differently)
- Common pitfalls for this pattern type

Keep entries concise. One puzzle = 2-5 lines. Don't repeat the full puzzle.
Output the complete updated knowledge base."""
    )

    save_knowledge(updated_knowledge)
    print(f"Knowledge base updated ({KNOWLEDGE_FILE})")

    runtime.finish({
        "puzzle_id": puzzle_id,
        "rule": best_rule,
        "test_outputs": test_outputs,
        "attempts": attempt + 1,
    })


main()
