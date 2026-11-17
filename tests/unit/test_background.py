"""Waiting for background writes: it returns once they're done, even for a task whose done callback hasn't run yet."""

import asyncio
from typing import cast

from unfret import background


def track(pending: set[asyncio.Task[None]], task: asyncio.Task[None]) -> None:
    """Keep ``task`` in ``pending`` until it finishes, as the stores do."""
    pending.add(task)
    task.add_done_callback(pending.discard)


async def test_drain_waits_for_the_writes() -> None:
    pending: set[asyncio.Task[None]] = set()
    written: list[int] = []

    async def write(n: int) -> None:
        await asyncio.sleep(0.01)
        written.append(n)

    for n in range(3):
        track(pending, asyncio.create_task(write(n)))

    await asyncio.wait_for(background.drain(pending), timeout=5)

    assert sorted(written) == [0, 1, 2]
    assert not pending


async def test_drain_returns_for_a_finished_task_before_its_callback_runs() -> None:
    # The old `while pending: await gather(...)` spun here forever: gathering a finished task never yields to the
    # loop, so the callback that takes it out of `pending` never ran.
    pending: set[asyncio.Task[None]] = set()
    finished = cast("asyncio.Task[None]", asyncio.get_running_loop().create_future())
    track(pending, finished)
    finished.set_result(None)  # its done callback is scheduled, not run

    await background.drain(pending)

    assert not pending


async def test_drain_skips_another_loops_task() -> None:
    pending: set[asyncio.Task[None]] = set()
    other = asyncio.new_event_loop()
    try:
        elsewhere = cast("asyncio.Task[None]", other.create_future())
        track(pending, elsewhere)

        await asyncio.wait_for(background.drain(pending), timeout=5)

        assert pending == {elsewhere}
    finally:
        other.close()
