"""Background writes that run after an analysis has answered, and waiting for them (tests and shutdown)."""

import asyncio


async def drain(pending: set[asyncio.Task[None]]) -> None:
    """
    Wait for the running event loop's unfinished tasks in ``pending``, then drop the finished ones.

    A task leaves ``pending`` in a done callback, which the event loop runs a moment after the task finishes. Waiting
    "while ``pending``" with ``asyncio.gather`` could catch a task in that moment: gathering only finished tasks
    completes without yielding to the loop, so the callback never got its turn and the wait spun forever (the CI test
    runs hung on it now and then). Only unfinished tasks are waited for here. Another loop's tasks, which this loop
    can't wait for, are skipped.

    :param pending: The module's set of background tasks.
    """
    loop = asyncio.get_running_loop()
    while waiting := [task for task in pending if task.get_loop() is loop and not task.done()]:
        await asyncio.gather(*waiting, return_exceptions=True)
    pending.difference_update([task for task in pending if task.done()])
