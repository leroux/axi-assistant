"""Unit tests for pure functions in prompts.py."""

from unittest.mock import patch

from axi import config
from axi.prompts import _is_axi_dev_cwd, compute_prompt_hash, make_spawned_agent_system_prompt


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


class TestIsAxiDevCwd:
    """Tests for _is_axi_dev_cwd — controls admin prompt injection."""

    def test_bot_dir_is_axi_dev(self) -> None:
        assert _is_axi_dev_cwd(config.BOT_DIR) is True

    def test_bot_dir_subpath_is_axi_dev(self) -> None:
        assert _is_axi_dev_cwd(config.BOT_DIR + "/subdir") is True

    def test_worktree_dir_is_axi_dev(self) -> None:
        if config.BOT_WORKTREES_DIR:
            assert _is_axi_dev_cwd(config.BOT_WORKTREES_DIR + "/some-worktree") is True

    def test_unrelated_path_is_not_axi_dev(self) -> None:
        assert _is_axi_dev_cwd("/home/user/other-project") is False

    def test_empty_string_is_not_axi_dev(self) -> None:
        assert _is_axi_dev_cwd("") is False

    def test_none_worktrees_dir(self) -> None:
        with patch.object(config, "BOT_WORKTREES_DIR", None):
            assert _is_axi_dev_cwd("/home/user/other-project") is False
            # BOT_DIR should still work
            assert _is_axi_dev_cwd(config.BOT_DIR) is True


class TestMakeSpawnedAgentSystemPrompt:
    """Tests for make_spawned_agent_system_prompt — assembles agent prompts."""

    def test_returns_preset_type(self) -> None:
        result = make_spawned_agent_system_prompt("/tmp/project")
        assert result["type"] == "preset"
        assert result["preset"] == "claude_code"

    def test_non_axi_cwd_uses_mini_context(self) -> None:
        result = make_spawned_agent_system_prompt("/tmp/project")
        # Non-admin agents get the mini context, not the full soul
        assert "agent session in the Axi system" in result["append"]

    def test_axi_dev_cwd_uses_full_context(self) -> None:
        result = make_spawned_agent_system_prompt(config.BOT_DIR)
        # Admin agents do NOT get the mini context
        assert "agent session in the Axi system" not in result["append"]

    def test_default_packs_included(self) -> None:
        result = make_spawned_agent_system_prompt("/tmp/project")
        # Default spawned packs should be included
        from axi.prompts import DEFAULT_SPAWNED_PACKS

        for pack in DEFAULT_SPAWNED_PACKS:
            assert pack.lower() in result["append"].lower()

    def test_custom_packs(self) -> None:
        result = make_spawned_agent_system_prompt("/tmp/project", packs=["algorithm"])
        assert "algorithm" in result["append"].lower()

    def test_empty_packs_disables_packs(self) -> None:
        result_no_packs = make_spawned_agent_system_prompt("/tmp/project", packs=[])
        result_default = make_spawned_agent_system_prompt("/tmp/project")
        # Empty packs should produce shorter append
        assert len(result_no_packs["append"]) <= len(result_default["append"])

    def test_axi_dev_cwd_adds_axi_dev_packs(self) -> None:
        result_axi = make_spawned_agent_system_prompt(config.BOT_DIR, packs=[])
        result_other = make_spawned_agent_system_prompt("/tmp/project", packs=[])
        # Axi-dev cwd should include extra pack content not present for non-axi cwds
        assert len(result_axi["append"]) > len(result_other["append"])
