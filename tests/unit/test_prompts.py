"""Unit tests for pure functions in prompts.py."""

from pathlib import Path
from unittest.mock import patch

from axi import config
from axi.prompts import _is_axi_dev_cwd, compute_prompt_hash, make_spawned_agent_system_prompt


REPO_ROOT = Path(__file__).resolve().parents[2]


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

    def test_non_axi_cwd_excludes_dev_context(self) -> None:
        result = make_spawned_agent_system_prompt("/tmp/project")
        # Non-admin agents get SOUL but not dev context
        assert "Axi Assistant — Development Context" not in result["append"]

    def test_axi_dev_cwd_includes_dev_context(self) -> None:
        result = make_spawned_agent_system_prompt(config.BOT_DIR)
        # Admin agents get dev context
        assert "Axi Assistant — Development Context" in result["append"]

    def test_default_extensions_included(self) -> None:
        result = make_spawned_agent_system_prompt("/tmp/project")
        from axi.extensions import DEFAULT_EXTENSIONS

        # If default extensions are configured, their text should appear
        if DEFAULT_EXTENSIONS:
            assert len(result["append"]) > 0

    def test_custom_extensions(self) -> None:
        result = make_spawned_agent_system_prompt("/tmp/project", extensions=["algorithm"])
        # Extension text should be included if the extension exists
        assert len(result["append"]) > 0

    def test_empty_extensions_disables_extensions(self) -> None:
        result_no_ext = make_spawned_agent_system_prompt("/tmp/project", extensions=[])
        result_default = make_spawned_agent_system_prompt("/tmp/project")
        # Empty extensions should produce shorter or equal append
        assert len(result_no_ext["append"]) <= len(result_default["append"])

    def test_axi_dev_cwd_adds_admin_content(self) -> None:
        result_axi = make_spawned_agent_system_prompt(config.BOT_DIR, extensions=[])
        result_other = make_spawned_agent_system_prompt("/tmp/project", extensions=[])
        # Axi-dev cwd should include extra content (soul + dev context) not present for non-axi cwds
        assert len(result_axi["append"]) > len(result_other["append"])

    def test_repo_git_top_paths_are_rendered_in_admin_prompt(self) -> None:
        result = make_spawned_agent_system_prompt(config.BOT_DIR, extensions=[])
        expected = str(REPO_ROOT / "prompts" / "refs" / "agent-spawning.md")
        assert expected in result["append"]
        assert "/home/pride/coding-projects/personal-assistant/prompts/refs/agent-spawning.md" not in result["append"]
