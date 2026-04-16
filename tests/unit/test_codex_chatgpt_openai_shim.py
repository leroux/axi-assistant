"""Tests for the ChatGPT OpenAI-compatible shim."""

import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import ModuleType


def load_shim() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "anthropic-codex-proxy" / "codex-chatgpt-openai-shim"
    loader = SourceFileLoader("codex_chatgpt_openai_shim", str(path))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_max_effort_maps_to_codex_xhigh() -> None:
    shim = load_shim()

    payload = shim.chat_to_responses(
        {
            "model": "gpt-5.4",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "max",
        }
    )

    assert payload["reasoning"] == {"effort": "xhigh"}


def test_regular_effort_passes_through() -> None:
    shim = load_shim()

    payload = shim.chat_to_responses(
        {
            "model": "gpt-5.4",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning": {"effort": "high"},
        }
    )

    assert payload["reasoning"] == {"effort": "high"}


def test_default_effort_is_xhigh() -> None:
    shim = load_shim()

    payload = shim.chat_to_responses(
        {
            "model": "gpt-5.4",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )

    assert payload["reasoning"] == {"effort": "xhigh"}


def test_reasoning_summary_text_extracts_text_parts() -> None:
    shim = load_shim()

    item = {
        "summary": [
            {"text": "First. "},
            {"content": [{"text": "Second."}]},
        ]
    }

    assert shim.reasoning_summary_text(item) == "First. Second."


def test_parse_completed_response_uses_reasoning_summary_when_no_output_text() -> None:
    shim = load_shim()

    raw = "".join(
        [
            "data: " + json.dumps({
                "type": "response.output_item.added",
                "item": {
                    "id": "rs_1",
                    "type": "reasoning",
                    "summary": [{"text": "Short reasoning summary."}],
                },
            }) + "\n\n",
            "data: " + json.dumps({
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "model": "gpt-5.4",
                    "created_at": 1710000000,
                    "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                },
            }) + "\n\n",
        ]
    ).encode("utf-8")

    parsed = shim.parse_completed_response(raw)

    assert parsed["choices"][0]["message"]["content"] == "Short reasoning summary."
