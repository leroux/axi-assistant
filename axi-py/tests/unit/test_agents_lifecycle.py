"""Unit tests for agents.lifecycle — is_awake, is_processing, count_awake_agents."""

from __future__ import annotations

from unittest.mock import MagicMock

from axi.agents import count_awake_agents, is_awake, is_processing
from axi.axi_types import AgentSession


class TestIsAwake:
    def test_no_client(self) -> None:
        session = AgentSession(name="test")
        assert not is_awake(session)

    def test_with_client(self) -> None:
        session = AgentSession(name="test")
        session.client = MagicMock()  # type: ignore[assignment]
        assert is_awake(session)

    def test_flowcoder_no_client(self) -> None:
        session = AgentSession(name="test", agent_type="flowcoder")
        assert not is_awake(session)

    def test_flowcoder_with_client(self) -> None:
        """Flowcoder agents use client (via BridgeTransport) like standard agents."""
        session = AgentSession(name="test", agent_type="flowcoder")
        session.client = MagicMock()  # type: ignore[assignment]
        assert is_awake(session)


class TestIsProcessing:
    def test_not_processing(self) -> None:
        session = AgentSession(name="test")
        assert not is_processing(session)


class TestCountAwakeAgents:
    def test_empty(self) -> None:
        from axi import agents as state

        original = dict(state.agents)
        state.agents.clear()
        try:
            assert count_awake_agents() == 0
        finally:
            state.agents.update(original)

    def test_sleeping_agents(self) -> None:
        from axi import agents as state

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
        from axi import agents as state

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
