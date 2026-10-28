import asyncio


async def wait_for_startup(task, timeout_s):
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout_s)
    except asyncio.TimeoutError:
        if task.done():
            task.result()
            return True
        return False
    return True


class BackgroundStartup:
    def __init__(self, factory, *, ready=None, cancel_on_close=False):
        self.factory = factory
        self.ready = ready
        self.cancel_on_close = cancel_on_close
        self.task = None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.factory())
        return self.ready if self.ready is not None else self.task

    async def close(self):
        pending = []
        if self.cancel_on_close:
            if self.ready is not None and not self.ready.done():
                self.ready.cancel()
            if self.task is not None and not self.task.done():
                self.task.cancel()
        if self.ready is not None and self.cancel_on_close:
            pending.append(self.ready)
        if self.task is not None:
            pending.append(self.task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
