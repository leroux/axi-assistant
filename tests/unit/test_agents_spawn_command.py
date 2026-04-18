from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionStub:
    agent_type: str = "claude_code"
    startup_command: str | None = None
    startup_command_args: str = ""


def initial_agent_message(session: SessionStub, prompt: str, *, flowcoder_enabled: bool) -> str:
    if (
        session.agent_type == "flowcoder"
        and flowcoder_enabled
        and session.startup_command
    ):
        command = session.startup_command
        command_args = session.startup_command_args
        session.startup_command = None
        session.startup_command_args = ""
        slash = f"/{command.lstrip('/')}"
        if command_args:
            slash += f" {command_args}"
        if prompt:
            slash += f" {prompt}"
        return slash
    return prompt


def test_initial_agent_message_uses_flowchart_command_when_configured() -> None:
    session = SessionStub(
        agent_type="flowcoder",
        startup_command="prompt",
        startup_command_args='"prefix text"',
    )

    result = initial_agent_message(session, "Say exactly: READY", flowcoder_enabled=True)

    assert result == '/prompt "prefix text" Say exactly: READY'
    assert session.startup_command is None
    assert session.startup_command_args == ""


def test_initial_agent_message_leaves_normal_prompt_unchanged() -> None:
    session = SessionStub(agent_type="flowcoder")

    assert initial_agent_message(session, "Say exactly: READY", flowcoder_enabled=True) == "Say exactly: READY"


def test_initial_agent_message_non_flowcoder_keeps_startup_command() -> None:
    session = SessionStub(startup_command="prompt", startup_command_args="x")

    assert initial_agent_message(session, "Say exactly: READY", flowcoder_enabled=True) == "Say exactly: READY"
    assert session.startup_command == "prompt"
    assert session.startup_command_args == "x"
