"""
Self-tuning prompt evolver.

Given a task + examples of good output, iteratively rewrites its own prompt
until outputs match. Saves the evolved prompt for future runs. Next invocation
starts from the already-tuned prompt and keeps improving.

Shape of self-modification: the INSTRUCTION ITSELF changes, not just data
appended to a fixed template. A JSON flowchart can't do this — the prompt
text is baked into the block definition.

Usage: /evolve task="summarize" examples='[{"in": "...", "out": "..."}]'
"""

import json
import os

from flowcoder import runtime

PROMPTS_DIR = ".flowcoder/evolved-prompts"


def load_prompt(task_name):
    path = os.path.join(PROMPTS_DIR, f"{task_name}.md")
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return None


def save_prompt(task_name, prompt):
    os.makedirs(PROMPTS_DIR, exist_ok=True)
    with open(os.path.join(PROMPTS_DIR, f"{task_name}.md"), "w") as f:
        f.write(prompt)


def main():
    args = runtime.start()
    task_name = args["task"]
    examples = json.loads(args["examples"])  # [{"in": "...", "out": "..."}, ...]
    target_input = args.get("input")  # optional: run the tuned prompt on this

    prompt = load_prompt(task_name) or f"Complete this {task_name} task."

    for attempt in range(5):
        # Test the current prompt against all examples
        results = []
        for ex in examples:
            runtime.clear()
            output = runtime.query(f"{prompt}\n\nInput:\n{ex['in']}")
            results.append({
                "input": ex["in"],
                "expected": ex["out"],
                "got": output,
            })

        # Judge: does the prompt produce the right outputs?
        runtime.clear()
        verdict = runtime.query(
            f"""You are evaluating a prompt's effectiveness. The prompt was tested
against {len(examples)} examples. For each, compare the actual output to the
expected output. They don't need to match exactly — judge whether the actual
output captures the same meaning, style, and level of detail.

Prompt being tested:
{prompt}

Results:
{json.dumps(results, indent=2)}

How many examples pass? What specifically goes wrong on failures?""",
            output_schema={
                "type": "object",
                "properties": {
                    "pass_count": {"type": "integer"},
                    "total": {"type": "integer"},
                    "all_pass": {"type": "boolean"},
                    "failure_analysis": {"type": "string"},
                },
                "required": ["pass_count", "total", "all_pass", "failure_analysis"],
            },
        )

        print(f"Attempt {attempt + 1}: {verdict['pass_count']}/{verdict['total']} pass")

        if verdict["all_pass"]:
            break

        # Rewrite the prompt based on what went wrong
        runtime.clear()
        prompt = runtime.query(
            f"""Rewrite this prompt to fix the failures described below.

Current prompt:
{prompt}

Failure analysis:
{verdict['failure_analysis']}

Failed examples:
{json.dumps([r for r in results if r['expected'] != r['got']], indent=2)}

Write an improved prompt. Output ONLY the new prompt text, nothing else.
Keep it concise. Don't add meta-commentary."""
        )

        print(f"Prompt rewritten, retrying...")

    # Save the evolved prompt
    save_prompt(task_name, prompt)
    print(f"Prompt saved to {PROMPTS_DIR}/{task_name}.md")

    # If there's a real input to process, run the tuned prompt on it
    if target_input:
        runtime.clear()
        result = runtime.query(f"{prompt}\n\nInput:\n{target_input}")
        runtime.finish(result)
    else:
        runtime.finish(f"Prompt tuned. {verdict['pass_count']}/{verdict['total']} examples pass.")


main()
