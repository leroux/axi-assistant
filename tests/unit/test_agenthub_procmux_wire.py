"""Tests for agenthub.procmux_wire — _TranslatingQueue and ProcmuxProcessConnection.

Tests _TranslatingQueue in isolation using real asyncio.Queue with procmux message types.
ProcmuxProcessConnection is already integration-tested in tests/test_bridge.py
(77 tests), so we focus on the translation layer here.
"""

from __future__ import annotations

import asyncio

import pytest

from agenthub.procmux_wire import _TranslatingQueue
from claudewire.types import ExitEvent, StderrEvent, StdoutEvent
from procmux.protocol import ExitMsg, StderrMsg, StdoutMsg

# ---------------------------------------------------------------------------
# _TranslatingQueue — get
# ---------------------------------------------------------------------------


class TestTranslatingQueueGet:
    @pytest.mark.asyncio
    async def test_translates_stdout(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        await raw.put(StdoutMsg(name="a1", data={"type": "msg", "n": 0}))
        result = await q.get()
        assert isinstance(result, StdoutEvent)
        assert result.name == "a1"
        assert result.data == {"type": "msg", "n": 0}

    @pytest.mark.asyncio
    async def test_translates_stderr(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        await raw.put(StderrMsg(name="a1", text="warning"))
        result = await q.get()
        assert isinstance(result, StderrEvent)
        assert result.name == "a1"
        assert result.text == "warning"

    @pytest.mark.asyncio
    async def test_translates_exit(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        await raw.put(ExitMsg(name="a1", code=42))
        result = await q.get()
        assert isinstance(result, ExitEvent)
        assert result.name == "a1"
        assert result.code == 42

    @pytest.mark.asyncio
    async def test_passes_none(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        await raw.put(None)
        assert await q.get() is None

    @pytest.mark.asyncio
    async def test_exit_with_none_code(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        await raw.put(ExitMsg(name="a1", code=None))
        result = await q.get()
        assert isinstance(result, ExitEvent)
        assert result.code is None


# ---------------------------------------------------------------------------
# _TranslatingQueue — put (reverse translation)
# ---------------------------------------------------------------------------


class TestTranslatingQueuePut:
    @pytest.mark.asyncio
    async def test_put_stdout(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        await q.put(StdoutEvent(name="a1", data={"type": "control_response"}))
        msg = raw.get_nowait()
        assert isinstance(msg, StdoutMsg)
        assert msg.name == "a1"
        assert msg.data == {"type": "control_response"}

    @pytest.mark.asyncio
    async def test_put_stderr(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        await q.put(StderrEvent(name="a1", text="err"))
        msg = raw.get_nowait()
        assert isinstance(msg, StderrMsg)
        assert msg.text == "err"

    @pytest.mark.asyncio
    async def test_put_exit(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        await q.put(ExitEvent(name="a1", code=1))
        msg = raw.get_nowait()
        assert isinstance(msg, ExitMsg)
        assert msg.code == 1

    @pytest.mark.asyncio
    async def test_put_none(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        await q.put(None)
        assert raw.get_nowait() is None


# ---------------------------------------------------------------------------
# _TranslatingQueue — get_nowait
# ---------------------------------------------------------------------------


class TestTranslatingQueueGetNowait:
    def test_stdout(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        raw.put_nowait(StdoutMsg(name="a1", data={"x": 1}))
        result = q.get_nowait()
        assert isinstance(result, StdoutEvent)
        assert result.data == {"x": 1}

    def test_stderr(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        raw.put_nowait(StderrMsg(name="a1", text="warn"))
        result = q.get_nowait()
        assert isinstance(result, StderrEvent)
        assert result.text == "warn"

    def test_exit(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        raw.put_nowait(ExitMsg(name="a1", code=0))
        result = q.get_nowait()
        assert isinstance(result, ExitEvent)
        assert result.code == 0

    def test_none(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        raw.put_nowait(None)
        assert q.get_nowait() is None

    def test_raises_on_empty(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)
        with pytest.raises(asyncio.QueueEmpty):
            q.get_nowait()


# ---------------------------------------------------------------------------
# _TranslatingQueue — round-trip
# ---------------------------------------------------------------------------


class TestTranslatingQueueRoundTrip:
    @pytest.mark.asyncio
    async def test_put_then_get_preserves_data(self) -> None:
        """put() reverse-translates, get() forward-translates — data round-trips."""
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)

        original = StdoutEvent(name="a1", data={"type": "response", "id": 42})
        await q.put(original)
        result = await q.get()
        assert result == original

    @pytest.mark.asyncio
    async def test_mixed_sequence(self) -> None:
        raw: asyncio.Queue = asyncio.Queue()
        q = _TranslatingQueue(raw)

        events = [
            StdoutEvent(name="a", data={"k": "v"}),
            StderrEvent(name="a", text="warn"),
            ExitEvent(name="a", code=0),
        ]
        for ev in events:
            await q.put(ev)
        for expected in events:
            assert await q.get() == expected
