"""
Self-modifying hot-reloading harness.

The trick: separate the stable harness (this file) from the strategy module
(strategy.py). The harness never changes. The strategy module gets rewritten
by the agent mid-run and hot-reloaded between iterations.

    harness.py  — fixed loop: load → run → evaluate → evolve → reload
    strategy.py — the part that changes: steps, prompts, logic

First run, strategy.py might be a naive approach. By the end of the run,
the agent has rewritten it based on what actually worked. Next run starts
from the evolved version.

The harness catches import/runtime errors from bad rewrites and rolls back.
"""

import importlib
import os
import shutil

from flowcoder import runtime

STRATEGY_FILE = ".flowcoder/strategy.py"
BACKUP_FILE = ".flowcoder/strategy.py.bak"

DEFAULT_STRATEGY = '''\
"""Auto-generated strategy module. The agent will rewrite this."""


def steps():
    """Return the ordered list of step functions to execute."""
    return [analyze, execute, verify]


def analyze(rt, ctx):
    """Understand the task."""
    ctx["plan"] = rt.query(f"Break this task into steps:\\n{ctx[\'task\']}")


def execute(rt, ctx):
    """Do the work."""
    rt.clear()
    ctx["result"] = rt.query(f"Execute this plan:\\n{ctx[\'plan\']}")


def verify(rt, ctx):
    """Check the work."""
    rt.clear()
    ctx["verdict"] = rt.query(
        f"Did this result accomplish the task?\\n"
        f"Task: {ctx[\'task\']}\\nResult: {ctx[\'result\']}\\n"
        f"Reply pass/fail with a one-line reason.",
    )
'''


def load_strategy():
    """Import (or reimport) the strategy module from the file."""
    os.makedirs(os.path.dirname(STRATEGY_FILE), exist_ok=True)

    if not os.path.exists(STRATEGY_FILE):
        with open(STRATEGY_FILE, "w") as f:
            f.write(DEFAULT_STRATEGY)

    import importlib.util

    spec = importlib.util.spec_from_file_location("strategy", STRATEGY_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    args = runtime.start()
    task = args["task"]

    for attempt in range(5):
        # Load current strategy (hot-reload on iterations 2+)
        strategy = load_strategy()
        step_fns = strategy.steps()

        # Run all steps
        ctx = {"task": task}
        failed = False
        for step in step_fns:
            try:
                step(runtime, ctx)
            except Exception as e:
                print(f"Step {step.__name__} crashed: {e}")
                failed = True
                break

        if failed:
            # Roll back to backup if we have one
            if os.path.exists(BACKUP_FILE):
                shutil.copy(BACKUP_FILE, STRATEGY_FILE)
                print("Rolled back to previous strategy")
            break

        # Evaluate: is the result good enough?
        runtime.clear()
        verdict = runtime.query(
            f"Task: {task}\n\nResult: {ctx.get('result', '(none)')}\n\n"
            f"Verification: {ctx.get('verdict', '(none)')}\n\n"
            f"Is this good enough to ship? Reply yes or no with a reason.",
            output_schema={
                "type": "object",
                "properties": {
                    "good_enough": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["good_enough", "reason"],
            },
        )

        print(f"Attempt {attempt + 1}: {'pass' if verdict['good_enough'] else 'fail'} — {verdict['reason']}")

        if verdict["good_enough"]:
            runtime.finish(ctx.get("result", ""))
            return

        # --- HOT RELOAD: rewrite the strategy ---
        shutil.copy(STRATEGY_FILE, BACKUP_FILE)

        with open(STRATEGY_FILE) as f:
            current_code = f.read()

        runtime.clear()
        new_code = runtime.query(
            f"""You are improving a Python strategy module. The current version
didn't produce good enough results for the task.

TASK: {task}
RESULT QUALITY: {verdict['reason']}

CURRENT strategy.py:
```python
{current_code}
```

Rewrite strategy.py to address the quality issues. You can:
- Change prompts to be more specific
- Add new steps (e.g. a research step before execution)
- Remove unhelpful steps
- Change the order of steps
- Add context-gathering (read files, check conventions)

Rules:
- Keep the same interface: steps() returns a list of functions, each takes (rt, ctx)
- rt is the runtime (rt.query(), rt.clear())
- ctx is a dict passed through all steps
- Output ONLY the Python code, no markdown fences or explanation"""
        )

        # Write the evolved strategy
        with open(STRATEGY_FILE, "w") as f:
            f.write(new_code)

        print(f"Strategy rewritten, hot-reloading...")

    runtime.finish(ctx.get("result", ""))


main()
