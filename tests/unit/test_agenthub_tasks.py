"""Tests for agenthub.tasks.BackgroundTaskSet."""

from __future__ import annotations

import asyncio
import logging

import pytest

from agenthub.tasks import BackgroundTaskSet


class TestBackgroundTaskSet:
    @pytest.mark.asyncio
    async def test_fire_and_forget_runs_coroutine(self) -> None:
        ts = BackgroundTaskSet()
        result = []

        async def work():
            result.append(42)

        task = ts.fire_and_forget(work())
        await task
        assert result == [42]

    @pytest.mark.asyncio
    async def test_task_removed_after_completion(self) -> None:
        ts = BackgroundTaskSet()

        async def work():
            pass

        task = ts.fire_and_forget(work())
        assert len(ts) == 1
        await task
        # Allow done callback to run
        await asyncio.sleep(0)
        assert len(ts) == 0

    @pytest.mark.asyncio
    async def test_len_tracks_active_tasks(self) -> None:
        ts = BackgroundTaskSet()
        assert len(ts) == 0

        gate = asyncio.Event()

        async def wait_for_gate():
            await gate.wait()

        t1 = ts.fire_and_forget(wait_for_gate())
        t2 = ts.fire_and_forget(wait_for_gate())
        assert len(ts) == 2

        gate.set()
        await t1
        await t2
        await asyncio.sleep(0)
        assert len(ts) == 0

    @pytest.mark.asyncio
    async def test_exception_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        ts = BackgroundTaskSet()

        async def fail():
            raise ValueError("test boom")

        task = ts.fire_and_forget(fail())
        # Wait for task to finish
        await asyncio.sleep(0.05)
        assert len(ts) == 0
        # Check that the exception was logged
        assert any("test boom" in r.message for r in caplog.records if r.levelno >= logging.ERROR)

    @pytest.mark.asyncio
    async def test_cancelled_task_no_error(self, caplog: pytest.LogCaptureFixture) -> None:
        ts = BackgroundTaskSet()

        async def hang():
            await asyncio.sleep(999)

        task = ts.fire_and_forget(hang())
        task.cancel()
        await asyncio.sleep(0.01)
        assert len(ts) == 0
        # No error log for cancelled tasks
        assert not any("failed" in r.message.lower() for r in caplog.records if r.levelno >= logging.ERROR)

    @pytest.mark.asyncio
    async def test_gc_protection(self) -> None:
        """Tasks aren't garbage collected before completion."""
        ts = BackgroundTaskSet()
        completed = []

        async def work(n):
            await asyncio.sleep(0.01)
            completed.append(n)

        tasks = [ts.fire_and_forget(work(i)) for i in range(5)]
        assert len(ts) == 5

        # Wait for all
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        assert sorted(completed) == [0, 1, 2, 3, 4]
        assert len(ts) == 0

    @pytest.mark.asyncio
    async def test_returns_task_object(self) -> None:
        ts = BackgroundTaskSet()

        async def work():
            return

        task = ts.fire_and_forget(work())
        assert isinstance(task, asyncio.Task)
        await task
