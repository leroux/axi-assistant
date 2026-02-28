"""Unit tests for prompts.compute_prompt_hash()."""

from prompts import compute_prompt_hash


class TestComputePromptHash:
    def test_none_returns_none(self) -> None:
        assert compute_prompt_hash(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert compute_prompt_hash("") is None

    def test_nonempty_string(self) -> None:
        result = compute_prompt_hash("You are a helpful assistant.")
        assert result is not None
        assert len(result) == 16  # first 16 hex chars of sha256

    def test_preset_dict_with_append(self) -> None:
        prompt = {"type": "preset", "preset": "claude_code", "append": "Custom instructions."}
        result = compute_prompt_hash(prompt)
        assert result is not None
        assert len(result) == 16

    def test_preset_dict_empty_append(self) -> None:
        prompt = {"type": "preset", "preset": "claude_code", "append": ""}
        assert compute_prompt_hash(prompt) is None

    def test_determinism(self) -> None:
        text = "Same prompt text"
        assert compute_prompt_hash(text) == compute_prompt_hash(text)

    def test_different_inputs_differ(self) -> None:
        a = compute_prompt_hash("prompt A")
        b = compute_prompt_hash("prompt B")
        assert a != b
