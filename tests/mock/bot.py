"""Deterministic Axi-shaped Discord bot used to validate the e2e framework.

Runs as a standalone process, polls channels via Discord REST API (no gateway
intents needed), and replies with canned responses that match the assertions
in tests/test_*_generated.py.

Usage:
    DISCORD_TOKEN=<bot-token> DISCORD_GUILD_ID=<guild-id> \\
        uv run python -m tests.mock.bot
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import sys
from dataclasses import dataclass, field

from discordquery import AsyncDiscordClient

SENTINEL = "awaiting input"
READY_MSG = "Axi ready"
MOCK_RESTART = "[MOCK_RESTART]"
MASTER_CHANNEL = "axi-master"
README_CHANNEL = "readme"
POLL_INTERVAL = 0.5
CHECKMARK = "\u2705"

_SAY_EXACTLY = re.compile(r"Say exactly[:\s]+(.+)", re.DOTALL)
_SPAWN = re.compile(
    r'Spawn an agent named "(?P<name>[^"]+)" with cwd "(?P<cwd>[^"]*)"'
    r' and prompt "(?P<prompt>[^"]*)"'
    r'(?: and resume="[^"]*")?'
    r'(?: and command="[^"]*")?'
    r'(?: and command_args="[^"]*")?'
    r'(?: and packs=\[[^\]]*\])?',
    re.DOTALL,
)
_KILL = re.compile(r'Kill the agent named "(?P<name>[^"]+)"')
_RESTART_AGENT = re.compile(r'Restart the agent named "(?P<name>[^"]+)"')
_SEND_TO = re.compile(
    r'Send a message to the agent "(?P<name>[^"]+)" saying: "(?P<msg>.*)"',
    re.DOTALL,
)


log = logging.getLogger("mock_bot")


@dataclass(slots=True)
class AgentRecord:
    name: str
    channel_id: str
    alive: bool = True


@dataclass(slots=True)
class BotState:
    agents: dict[str, AgentRecord] = field(default_factory=dict)
    debug_on: bool = False


@dataclass(slots=True)
class Channel:
    channel_id: str
    name: str
    cursor: str = "0"


class MockBot:
    def __init__(self, *, token: str, guild_id: str) -> None:
        self.token = token
        self.guild_id = guild_id
        self.client = AsyncDiscordClient(token, timeout=15.0)
        self.state = BotState()
        self.user_id: str | None = None
        self.master: Channel | None = None
        self.watched: dict[str, Channel] = {}
        self._stopping = asyncio.Event()

    async def startup(self) -> None:
        me = await self.client.get("/users/@me")
        self.user_id = str(me["id"])
        log.info("mock bot user_id=%s guild_id=%s", self.user_id, self.guild_id)
        await self._ensure_master()
        await self._ensure_readme()
        await self._post(self.master.channel_id, READY_MSG)

    async def _ensure_master(self) -> None:
        channel = await self._find_channel(MASTER_CHANNEL)
        if channel is None:
            channel = await self.client.create_channel(self.guild_id, MASTER_CHANNEL)
        ch_id = str(channel["id"])
        latest = await self._latest_id(ch_id)
        self.master = Channel(channel_id=ch_id, name=MASTER_CHANNEL, cursor=latest)
        self.watched[ch_id] = self.master

    async def _ensure_readme(self) -> None:
        channel = await self._find_channel(README_CHANNEL)
        if channel is None:
            channel = await self.client.create_channel(self.guild_id, README_CHANNEL)
        ch_id = str(channel["id"])
        msgs = await self.client.get_messages(ch_id, limit=5)
        total = sum(len(m.get("content", "")) for m in msgs)
        if total <= 20:
            await self._post(
                ch_id,
                "This is the mock bot's #readme channel. "
                "Auto-populated with at least twenty characters of content.",
            )

    async def _find_channel(self, name: str) -> dict | None:
        return await self.client.find_channel(self.guild_id, name)

    async def _latest_id(self, channel_id: str) -> str:
        msgs = await self.client.get_messages(channel_id, limit=1)
        return str(msgs[0]["id"]) if msgs else "0"

    async def _post(self, channel_id: str, content: str) -> dict:
        return await self.client.send_message(channel_id, content)

    async def run(self) -> None:
        await self.startup()
        while not self._stopping.is_set():
            try:
                await self._poll_once()
            except Exception:
                log.exception("poll loop error; continuing")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=POLL_INTERVAL)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopping.set()

    async def close(self) -> None:
        await self.client.aclose()

    async def _poll_once(self) -> None:
        for channel in list(self.watched.values()):
            try:
                msgs = await self.client.get_messages(
                    channel.channel_id, limit=100, after=channel.cursor
                )
            except Exception:
                log.exception("get_messages failed for %s", channel.name)
                continue
            if not msgs:
                continue
            msgs_sorted = sorted(msgs, key=lambda m: int(m["id"]))
            for msg in msgs_sorted:
                channel.cursor = str(msg["id"])
                author_id = str(msg.get("author", {}).get("id", ""))
                if author_id == self.user_id:
                    continue
                try:
                    await self._dispatch(channel, msg)
                except Exception:
                    log.exception("dispatch error on channel %s", channel.name)

    async def _dispatch(self, channel: Channel, msg: dict) -> None:
        content = msg.get("content", "") or ""
        if channel.name == MASTER_CHANNEL:
            await self._handle_master(channel, msg, content)
        else:
            await self._handle_agent(channel, msg, content)

    async def _handle_master(self, channel: Channel, msg: dict, content: str) -> None:
        if MOCK_RESTART in content:
            await self._do_reset(channel.channel_id)
            return

        with contextlib.suppress(Exception):
            await self.client.add_reaction(channel.channel_id, msg["id"], CHECKMARK)

        stripped = content.strip()

        if stripped == "/status":
            await self._post(
                channel.channel_id,
                f"Status: axi-master is online. {len(self.state.agents)} agents tracked.",
            )
            return
        if stripped == "/clear":
            await self._post(channel.channel_id, "Cleared conversation history.")
            return
        if stripped == "/debug":
            self.state.debug_on = not self.state.debug_on
            val = "on" if self.state.debug_on else "off"
            await self._post(channel.channel_id, f"Debug mode: {val}")
            return
        if stripped == "/compact":
            await self._post(
                channel.channel_id,
                "Compacted conversation. 1234 tokens reclaimed.",
            )
            return

        m = _SPAWN.search(content)
        if m:
            await self._do_spawn(channel.channel_id, m["name"], m["prompt"])
            return
        m = _KILL.search(content)
        if m:
            await self._do_kill(channel.channel_id, m["name"])
            return
        m = _RESTART_AGENT.search(content)
        if m:
            await self._do_restart_agent(channel.channel_id, m["name"])
            return
        m = _SEND_TO.search(content)
        if m:
            await self._do_send_to(channel.channel_id, m["name"], m["msg"])
            return

        m = _SAY_EXACTLY.search(content)
        if m:
            await self._post(channel.channel_id, m.group(1).strip())
            await self._post(channel.channel_id, SENTINEL)
            return

        await self._post(channel.channel_id, "ACK")
        await self._post(channel.channel_id, SENTINEL)

    async def _handle_agent(self, channel: Channel, msg: dict, content: str) -> None:
        record = self._record_for_channel(channel.channel_id)
        if record is None or not record.alive:
            await self._post(
                channel.channel_id,
                "This agent has been killed. No replies will be sent here.",
            )
            await self._post(channel.channel_id, SENTINEL)
            return
        m = _SAY_EXACTLY.search(content)
        if m:
            await self._post(channel.channel_id, m.group(1).strip())
        else:
            await self._post(channel.channel_id, f"[{record.name}] ACK")
        await self._post(channel.channel_id, SENTINEL)

    def _record_for_channel(self, channel_id: str) -> AgentRecord | None:
        for rec in self.state.agents.values():
            if rec.channel_id == channel_id:
                return rec
        return None

    async def _do_spawn(self, master_id: str, name: str, prompt: str) -> None:
        existing = self.state.agents.get(name)
        if existing and existing.alive:
            await self._post(master_id, f'Agent "{name}" already exists (live).')
            await self._post(master_id, SENTINEL)
            return
        chan_name = _channel_name(name)
        channel = await self._find_channel(chan_name)
        if channel is None:
            channel = await self.client.create_channel(self.guild_id, chan_name)
        ch_id = str(channel["id"])
        latest = await self._latest_id(ch_id)
        record = AgentRecord(name=name, channel_id=ch_id, alive=True)
        self.state.agents[name] = record
        self.watched[ch_id] = Channel(channel_id=ch_id, name=chan_name, cursor=latest)
        await self._post(master_id, f'Spawned agent "{name}".')
        await self._post(master_id, SENTINEL)
        m = _SAY_EXACTLY.search(prompt)
        if m:
            await self._post(ch_id, m.group(1).strip())
        else:
            await self._post(ch_id, f"[{name}] processing: {prompt}")
        await self._post(ch_id, SENTINEL)

    async def _do_kill(self, master_id: str, name: str) -> None:
        record = self.state.agents.get(name)
        if record is None:
            await self._post(master_id, f'No agent named "{name}" to kill.')
            await self._post(master_id, SENTINEL)
            return
        record.alive = False
        await self._post(master_id, f'Killed agent "{name}" (terminated).')
        await self._post(master_id, SENTINEL)

    async def _do_restart_agent(self, master_id: str, name: str) -> None:
        record = self.state.agents.get(name)
        if record is None:
            await self._post(master_id, f'No agent named "{name}" to restart.')
            await self._post(master_id, SENTINEL)
            return
        record.alive = True
        await self._post(master_id, f'Restarted agent "{name}".')
        await self._post(master_id, SENTINEL)

    async def _do_send_to(self, master_id: str, name: str, message: str) -> None:
        record = self.state.agents.get(name)
        if record is None:
            await self._post(master_id, f'No agent named "{name}".')
            await self._post(master_id, SENTINEL)
            return
        await self._post(record.channel_id, message)
        await self._post(master_id, f'Forwarded to "{name}".')
        await self._post(master_id, SENTINEL)

    async def _do_reset(self, master_id: str) -> None:
        for record in list(self.state.agents.values()):
            with contextlib.suppress(Exception):
                await self.client.request(
                    "DELETE", f"/channels/{record.channel_id}"
                )
            self.watched.pop(record.channel_id, None)
        self.state.agents.clear()
        self.state.debug_on = False
        await self._post(master_id, READY_MSG)


def _channel_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\-]", "-", raw.lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "agent"


async def _amain() -> int:
    token = os.environ.get("DISCORD_TOKEN")
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not token or not guild_id:
        sys.stderr.write("DISCORD_TOKEN and DISCORD_GUILD_ID are required\n")
        return 2
    logging.basicConfig(
        level=os.environ.get("MOCK_BOT_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = MockBot(token=token, guild_id=guild_id)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, bot.stop)
    try:
        await bot.run()
    finally:
        await bot.close()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
