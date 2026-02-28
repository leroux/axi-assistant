"""Unit tests for agents.lifecycle — is_awake, is_processing, count_awake_agents."""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.lifecycle import count_awake_agents, is_awake, is_processing
from axi_types import AgentSession


class TestIsAwake:
    def test_no_client(self) -> None:
        session = AgentSession(name="test")
        assert not is_awake(session)

    def test_with_client(self) -> None:
        session = AgentSession(name="test")
        session.client = MagicMock()  # type: ignore[assignment]
        assert is_awake(session)

    def test_flowcoder_no_process(self) -> None:
        session = AgentSession(name="test", agent_type="flowcoder")
        assert not is_awake(session)

    def test_flowcoder_with_process(self) -> None:
        session = AgentSession(name="test", agent_type="flowcoder")
        session.flowcoder_process = MagicMock()  # type: ignore[assignment]
        assert is_awake(session)


class TestIsProcessing:
    def test_not_processing(self) -> None:
        session = AgentSession(name="test")
        assert not is_processing(session)

    def test_flowcoder_running(self) -> None:
        session = AgentSession(name="test", agent_type="flowcoder")
        proc = MagicMock()
        proc.is_running = True
        session.flowcoder_process = proc  # type: ignore[assignment]
        assert is_processing(session)

    def test_flowcoder_not_running(self) -> None:
        session = AgentSession(name="test", agent_type="flowcoder")
        proc = MagicMock()
        proc.is_running = False
        session.flowcoder_process = proc  # type: ignore[assignment]
        assert not is_processing(session)


class TestCountAwakeAgents:
    def test_empty(self) -> None:
        from agents import state

        original = dict(state.agents)
        state.agents.clear()
        try:
            assert count_awake_agents() == 0
        finally:
            state.agents.update(original)

    def test_sleeping_agents(self) -> None:
        from agents import state

        original = dict(state.agents)
        state.agents.clear()
        state.agents["a"] = AgentSession(name="a")
        state.agents["b"] = AgentSession(name="b")
        try:
            assert count_awake_agents() == 0
        finally:
            state.agents.clear()
            state.agents.update(original)

    def test_awake_agents(self) -> None:
        from agents import state

        original = dict(state.agents)
        state.agents.clear()
        s1 = AgentSession(name="a")
        s1.client = MagicMock()  # type: ignore[assignment]
        s2 = AgentSession(name="b")
        state.agents["a"] = s1
        state.agents["b"] = s2
        try:
            assert count_awake_agents() == 1
        finally:
            state.agents.clear()
            state.agents.update(original)
