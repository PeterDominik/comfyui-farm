"""In-process pub/sub for live dashboard updates (the /events websocket)."""

from __future__ import annotations

import asyncio


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _deliver(self, event: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:   # a stuck client; drop it rather than block the farm
                self._subs.discard(q)

    def publish(self, event: dict) -> None:
        """Safe to call from the event loop or from worker threads."""
        if self._loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._deliver(event)
        else:
            self._loop.call_soon_threadsafe(self._deliver, event)
