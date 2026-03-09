"""
Self-modifying code reviewer.

Starts generic. Each review, it observes the project's patterns and accumulates
project-specific rules. Over time it tunes itself to the codebase. Rules persist
in .flowcoder/review-rules.md — human-readable, prunable.

This can't be a static JSON graph because:
  - The prompt content changes between runs (rules accumulate)
  - The decision of what to learn is data-dependent
  - The modification target is the program's own future behavior

Usage: /review <git diff>
"""

import os

from flowcoder import runtime

RULES_FILE = ".flowcoder/review-rules.md"


def load_rules():
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f:
            return f.read()
    return ""


def save_rules(content):
    os.makedirs(os.path.dirname(RULES_FILE), exist_ok=True)
    with open(RULES_FILE, "w") as f:
        f.write(content)


def main():
    args = runtime.start()
    diff = args["diff"]
    rules = load_rules()

    # --- REVIEW (the actual work) ---
    rules_section = f"\n\nProject-specific rules (learned from prior reviews):\n{rules}" if rules else ""

    review = runtime.query(
        f"Review this diff. Be specific and actionable.{rules_section}\n\n{diff}"
    )

    # --- SELF-MODIFY (learn from what we just saw) ---
    runtime.clear()
    learning = runtime.query(
        f"""You just reviewed code for a project. Look at the diff for project-specific
patterns — naming conventions, error handling style, architectural patterns,
import conventions, test patterns — things that are THIS project's way of doing things,
not general best practices.

Diff:
{diff}

Current rules:
{rules if rules else "(none yet)"}

If you see clear project-specific patterns not already captured, add them.
If an existing rule is wrong based on new evidence, remove or update it.
If nothing to change, return the rules unchanged.

Return ONLY the updated rules as a markdown list. One rule per line, "- " prefix.""",
        output_schema={
            "type": "object",
            "properties": {
                "rules": {"type": "string"},
                "changed": {"type": "boolean"},
                "changelog": {"type": "string"},
            },
            "required": ["rules", "changed"],
        },
    )

    if learning["changed"]:
        save_rules(learning["rules"])
        print(f"Rules updated: {learning.get('changelog', '(no summary)')}")

    runtime.finish(review)


main()
