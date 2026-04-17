import os
from unittest.mock import patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "1")
os.environ.setdefault("DISCORD_GUILD_ID", "1")

from axi.axi_types import AgentSession
from axi.hub_wiring import _make_agent_options


class TestMakeAgentOptionsModelSelection:
    def test_session_model_override_is_resolved_for_runtime(self) -> None:
        session = AgentSession(name="test", cwd="/tmp", model="gpt-5.4")

        with patch("axi.agents.make_stderr_callback", return_value=None):
            options = _make_agent_options(session, resume_id=None)

        assert options.model is None
        assert options.env["ANTHROPIC_MODEL"] == "gpt-5.4"
        assert options.env["ANTHROPIC_BASE_URL"]
        assert options.env["ANTHROPIC_API_KEY"]

    def test_global_model_is_used_when_session_model_is_unset(self) -> None:
        session = AgentSession(name="test", cwd="/tmp")

        with (
            patch("axi.agents.make_stderr_callback", return_value=None),
            patch("axi.hub_wiring.config.get_model", return_value="sonnet"),
        ):
            options = _make_agent_options(session, resume_id=None)

        assert options.model == "sonnet"
        assert "ANTHROPIC_MODEL" not in options.env
