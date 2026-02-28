"""LLM judge for evaluating bot responses in smoke tests.

Uses the `claude` CLI (claude-code-sdk) for inference, with file-based caching
to avoid redundant calls during reruns.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

CACHE_DIR = Path(__file__).parent.parent / ".llm_cache"
CLAUDE_CMD = "claude"  # Assumes `claude` is in PATH


def llm_call(prompt: str, model: str = "haiku", use_cache: bool = True) -> str:
    """Call an LLM via the claude CLI and return the text response.

    Args:
        prompt: The full prompt to send.
        model: Model name to use (default: haiku for speed/cost).
        use_cache: Whether to use file-based caching (default: True).

    Returns:
        The LLM's text response.
    """
    cache_key = hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()
    cache_file = CACHE_DIR / f"{cache_key}.json"

    if use_cache and cache_file.exists():
        cached = json.loads(cache_file.read_text())
        return cached["response"]

    # Call claude CLI: claude -p "prompt" --model <model> --output-format text
    # Unset CLAUDECODE env var to allow running inside a Claude Code session
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(
        [CLAUDE_CMD, "-p", prompt, "--model", model, "--output-format", "text"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {result.returncode}): {result.stderr}"
        )

    response = result.stdout.strip()

    # Cache the result
    if use_cache:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"model": model, "prompt": prompt, "response": response})
        )

    return response


def llm_assert(
    bot_output: str,
    criteria: str,
    context: str = "",
    model: str = "haiku",
) -> tuple[bool, str]:
    """Use an LLM to judge whether bot output meets criteria.

    Args:
        bot_output: The bot's actual output text.
        criteria: What the output should satisfy (natural language).
        context: Optional context about what was tested.
        model: Model to use for judging.

    Returns:
        (passed, explanation) — whether criteria were met and why.
    """
    prompt = f"""You are a QA test judge. Evaluate whether the bot's output meets the given criteria.

{f"Context: {context}" if context else ""}

Bot output:
---
{bot_output}
---

Criteria: {criteria}

Respond with exactly one of these formats:
PASS: <brief explanation>
FAIL: <brief explanation>"""

    response = llm_call(prompt, model=model)

    passed = response.upper().startswith("PASS")
    return passed, response
