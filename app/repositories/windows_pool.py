"""Keep psycopg's selector loop separate from Windows subprocess-capable Proactor."""
import asyncio
import threading
from collections.abc import Callable
from typing import Any


class SelectorRepository:
    """Forward async database operations to one owned selector-loop thread."""
    def __init__(self) -> None:
        self.loop = asyncio.SelectorEventLoop()
        self.thread = threading.Thread(target=self._run, name="rag-db-loop", daemon=True)
        self.repository = None

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        self.loop.close()

    async def open(self, factory: Callable[[], Any]) -> None:
        self.thread.start()
        async def initialize():
            self.repository = factory()
            await self.repository.open()
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(initialize(), self.loop))

    def __getattr__(self, name: str):
        async def forward(*args, **kwargs):
            async def execute():
                return await getattr(self.repository, name)(*args, **kwargs)
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(execute(), self.loop))
        return forward

    async def close(self) -> None:
        try:
            if self.repository is not None:
                await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self.repository.close(), self.loop))
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            await asyncio.to_thread(self.thread.join, 6)
