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
async def test_busy_message_then_stop_clears_queued_followup(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")

    await session.query_lock.acquire()

    busy_message = FakeMessage(1, channel, "hi", author)
    await main.on_message(busy_message)

    assert len(session.message_queue) == 1
    assert session.message_queue[0][0] == "hi"

    stop_message = FakeMessage(2, channel, "/stop", author)
    handled = await main._handle_text_command(stop_message, session, session.name)

    assert handled is True
    assert len(session.message_queue) == 0
    agents.send_system.assert_any_await(channel, "Interrupt signal sent to **axi-master**. Cleared 1 queued message.")


@pytest.mark.asyncio
async def test_axi_master_busy_queue_replaces_older_message(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")

    await session.query_lock.acquire()

    first = FakeMessage(3, channel, "older queued", author)
    await main.on_message(first)
    second = FakeMessage(4, channel, "newer queued", author)
    await main.on_message(second)

    assert len(session.message_queue) == 1
    assert session.message_queue[0][0] == "newer queued"
    agents.remove_reaction.assert_any_await(first, "📨")
    agents.send_system.assert_any_await(
        channel,
        "Agent **axi-master** is busy — message queued (position 1). Interrupting current task. Replaced older queued message.",
    )


@pytest.mark.asyncio
async def test_axi_master_busy_queue_keeps_only_latest_across_many_messages(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")

    await session.query_lock.acquire()

    for idx, text in enumerate(["one", "two", "three", "four"], start=10):
        await main.on_message(FakeMessage(idx, channel, text, author))

    assert len(session.message_queue) == 1
    assert session.message_queue[0][0] == "four"


@pytest.mark.asyncio
async def test_axi_master_skip_reports_latest_message_contract(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")

    await session.query_lock.acquire()
    await main.on_message(FakeMessage(20, channel, "older queued", author))
    await main.on_message(FakeMessage(21, channel, "newer queued", author))

    handled = await main._handle_text_command(FakeMessage(22, channel, "/skip", author), session, session.name)

    assert handled is True
    agents.send_system.assert_any_await(
        channel,
        "Skipped current query for **axi-master**. Latest message will continue processing.",
    )


@pytest.mark.asyncio
async def test_axi_master_stop_clears_latest_message(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")

    await session.query_lock.acquire()
    await main.on_message(FakeMessage(30, channel, "older queued", author))
    await main.on_message(FakeMessage(31, channel, "newer queued", author))

    handled = await main._handle_text_command(FakeMessage(32, channel, "/stop", author), session, session.name)

    assert handled is True
    assert len(session.message_queue) == 0
    agents.send_system.assert_any_await(channel, "Interrupt signal sent to **axi-master**. Cleared 1 queued message.")


@pytest.mark.asyncio
async def test_stop_clears_queue_before_interrupt_finishes(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")
    interrupt_started = asyncio.Event()
    release_interrupt = asyncio.Event()

    async def _slow_interrupt(_session: AgentSession) -> None:
        interrupt_started.set()
        await release_interrupt.wait()

    main._interrupt_agent = _slow_interrupt  # type: ignore[assignment]

    await session.query_lock.acquire()
    await main.on_message(FakeMessage(40, channel, "queued followup", author))
    assert len(session.message_queue) == 1

    stop_task = asyncio.create_task(main._handle_text_command(FakeMessage(41, channel, "/stop", author), session, session.name))
    await interrupt_started.wait()

    assert len(session.message_queue) == 0

    release_interrupt.set()
    handled = await stop_task
    assert handled is True


@pytest.mark.asyncio
async def test_non_master_busy_queue_remains_fifo(session: AgentSession) -> None:
    session.name = "other-agent"
    agents.agents.clear()
    agents.agents[session.name] = session
    agents.channel_to_agent[12345] = session.name

    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "other-agent")

    await session.query_lock.acquire()

    first = FakeMessage(5, channel, "older queued", author)
    await main.on_message(first)
    second = FakeMessage(6, channel, "newer queued", author)
    await main.on_message(second)

    assert len(session.message_queue) == 2
    assert session.message_queue[0][0] == "older queued"
    assert session.message_queue[1][0] == "newer queued"


@pytest.mark.asyncio
async def test_stop_drops_followup_instead_of_running_it(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")

    await session.query_lock.acquire()

    first = FakeMessage(10, channel, "first queued", author)
    await main.on_message(first)
    assert len(session.message_queue) == 1

    stop_message = FakeMessage(11, channel, "/stop", author)
    await main._handle_text_command(stop_message, session, session.name)
    assert len(session.message_queue) == 0

    session.query_lock.release()
    await agents.process_message_queue(session)

    agents.process_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_queue_interrupt_is_deduplicated_per_session(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")
    calls = 0
    release = asyncio.Event()

    async def _slow_interrupt(_session: AgentSession) -> None:
        nonlocal calls
        calls += 1
        await release.wait()

    main._interrupt_agent = _slow_interrupt  # type: ignore[assignment]

    await session.query_lock.acquire()
    t1 = asyncio.create_task(main.on_message(FakeMessage(60, channel, "one", author)))
    t2 = asyncio.create_task(main.on_message(FakeMessage(61, channel, "two", author)))
    await asyncio.sleep(0)

    assert len(session.message_queue) == 1
    release.set()
    await asyncio.gather(t1, t2)
    assert calls == 2


@pytest.mark.asyncio
async def test_stop_prevents_queue_drain_if_processing_starts(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")

    await session.query_lock.acquire()
    await main.on_message(FakeMessage(50, channel, "queued followup", author))
    session.query_lock.release()

    session.state.stop_requested = True
    await agents.process_message_queue(session)

    agents.process_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_slash_stop_is_normalized_and_clears_queue(session: AgentSession) -> None:
    author = FakeAuthor(_allowed_user_id())
    channel = FakeTextChannel(12345, "axi-master")

    await session.query_lock.acquire()

    busy_message = FakeMessage(20, channel, "queued followup", author)
    await main.on_message(busy_message)
    assert len(session.message_queue) == 1

    plain_stop = FakeMessage(21, channel, "/stop", author)
    await main.on_message(plain_stop)

    assert len(session.message_queue) == 0
    agents.process_message.assert_not_awaited()
    agents.send_system.assert_any_await(channel, "Interrupt signal sent to **axi-master**. Cleared 1 queued message.")
