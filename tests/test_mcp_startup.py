import asyncio

import pytest

from openathlete.mcp.startup import BackgroundStartup, wait_for_startup


def test_background_startup_shares_one_task_and_returns_result():
    async def scenario():
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return 42

        startup = BackgroundStartup(factory)
        first = startup.start()
        second = startup.start()
        assert first is second
        assert await first == 42
        assert calls == 1
        await startup.close()

    asyncio.run(scenario())


def test_wait_for_startup_distinguishes_wait_timeout_from_task_timeout():
    async def scenario():
        pending = asyncio.create_task(asyncio.sleep(0.05))
        assert await wait_for_startup(pending, 0.001) is False
        assert await wait_for_startup(pending, 1) is True

        async def fail():
            raise TimeoutError("factory timeout")

        with pytest.raises(TimeoutError, match="factory timeout"):
            await wait_for_startup(asyncio.create_task(fail()), 1)

    asyncio.run(scenario())
