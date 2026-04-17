"""Focused repro tests for Axi stop + queued-message behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from axi import agents, channels, config, main
from axi.axi_types import AgentSession, discord_state


class FakeTextChannel:
    def __init__(self, channel_id: int, name: str = "axi-master") -> None:
        self.id = channel_id
        self.name = name
        self.type = discord.ChannelType.text


class FakeAuthor:
    def __init__(self, user_id: int, name: str = "moosh", *, bot: bool = False) -> None:
        self.id = user_id
        self.name = name
        self.bot = bot


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id


class FakeMessage:
    def __init__(self, message_id: int, channel: FakeTextChannel, content: str, author: FakeAuthor) -> None:
        self.id = message_id
        self.channel = channel
        self.content = content
        self.author = author
        self.guild = FakeGuild(config.DISCORD_GUILD_ID)
        self.type = discord.MessageType.default
        self.created_at = discord.utils.utcnow()
        self.attachments = []
        self.reactions: list[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji: str, _user: object) -> None:
        if emoji in self.reactions:
            self.reactions.remove(emoji)


@pytest.fixture(autouse=True)
def _reset_axi_state(monkeypatch: pytest.MonkeyPatch) -> None:
    agents.agents.clear()
    agents.channel_to_agent.clear()
    main._seen_message_ids.clear()
    main._startup_complete = True
    fake_bot = SimpleNamespace(user=SimpleNamespace(id=999999), process_commands=AsyncMock())
    monkeypatch.setattr(main, "bot", fake_bot)
    monkeypatch.setattr(channels, "mark_channel_active", lambda _channel_id: None)
    monkeypatch.setattr(channels, "is_killed_channel", lambda _channel: False)
    monkeypatch.setattr(main, "TextChannel", FakeTextChannel)


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> AgentSession:
    session = AgentSession(name="axi-master")
    session.client = object()
    session.cwd = config.BOT_DIR
    session.frontend_state = discord_state(session)
    agents.agents[session.name] = session
    agents.channel_to_agent[12345] = session.name

    async def _extract_message_content(message: FakeMessage) -> str:
        return message.content

    monkeypatch.setattr(agents, "extract_message_content", _extract_message_content)
    monkeypatch.setattr(agents, "wrap_content_with_flowchart", lambda content, _session: content)
    monkeypatch.setattr(agents, "is_processing", lambda _session: True)
    monkeypatch.setattr(agents, "is_awake", lambda _session: True)
    monkeypatch.setattr(agents, "count_awake_agents", lambda: 1)
    monkeypatch.setattr(agents, "scheduler", SimpleNamespace(mark_interactive=lambda _name: None, should_yield=lambda _name: False))
    monkeypatch.setattr(main.scheduler, "should_yield", lambda _name: False)
    monkeypatch.setattr(main, "_interrupt_agent", AsyncMock())
    monkeypatch.setattr(agents, "graceful_interrupt", AsyncMock(return_value=True))
    monkeypatch.setattr(agents, "process_message", AsyncMock())
    monkeypatch.setattr(agents, "wake_agent", AsyncMock())
    monkeypatch.setattr(agents, "sleep_agent", AsyncMock())
    monkeypatch.setattr(agents, "send_system", AsyncMock())
    monkeypatch.setattr(agents, "remove_reaction", AsyncMock())
    monkeypatch.setattr(agents, "add_reaction", AsyncMock())
    monkeypatch.setattr(agents, "get_active_trace_tag", lambda _name: "[trace=testtrace]")
    monkeypatch.setattr(main, "_resolve_agent", AsyncMock(return_value=(session.name, session)))
    return session


def _allowed_user_id() -> int:
    return next(iter(config.ALLOWED_USER_IDS), 1)


@pytest.mark.asyncio
async def test_busy_message_then_stop_preserves_queued_followup(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")

    await session.query_lock.acquire()

    busy_message = FakeMessage(1, channel, "hi", author)
    await main.on_message(busy_message)

    assert len(session.message_queue) == 1
    assert session.message_queue[0][0] == "hi"

    stop_message = FakeMessage(2, channel, "//stop", author)
    handled = await main._handle_text_command(stop_message, session, session.name)

    assert handled is True
    assert len(session.message_queue) == 1
    agents.send_system.assert_any_await(channel, "Interrupt signal sent to **axi-master**. Preserved 1 queued message.")


@pytest.mark.asyncio
async def test_queued_followup_runs_after_stop(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")

    await session.query_lock.acquire()

    first = FakeMessage(10, channel, "first queued", author)
    await main.on_message(first)
    assert len(session.message_queue) == 1

    stop_message = FakeMessage(11, channel, "//stop", author)
    await main._handle_text_command(stop_message, session, session.name)
    assert len(session.message_queue) == 1

    session.query_lock.release()
    await agents.process_message_queue(session)

    agents.process_message.assert_awaited()
    processed_content = agents.process_message.await_args_list[-1].args[1]
    assert processed_content == "first queued"


@pytest.mark.asyncio
async def test_plain_slash_stop_is_normalized_to_text_command(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")

    await session.query_lock.acquire()

    busy_message = FakeMessage(20, channel, "queued followup", author)
    await main.on_message(busy_message)
    assert len(session.message_queue) == 1

    plain_stop = FakeMessage(21, channel, "/stop", author)
    await main.on_message(plain_stop)

    assert len(session.message_queue) == 1
    agents.process_message.assert_not_awaited()
    agents.send_system.assert_any_await(channel, "Interrupt signal sent to **axi-master**. Preserved 1 queued message.")
