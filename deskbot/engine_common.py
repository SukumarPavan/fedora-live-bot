"""Shared engine plumbing: the context handed to engines and the asyncio host thread."""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Callable

from .audio_io import Player
from .bridge import Bridge
from .config import Settings
from .vision import Camera, Screen

log = logging.getLogger(__name__)


@dataclass
class EngineContext:
    cfg: Settings
    bridge: Bridge
    player: Player
    screen: Screen
    camera: Camera
    screen_on: Callable[[], bool]
    cam_on: Callable[[], bool]


class EngineHost:
    """Runs one engine on a private asyncio loop in a background thread."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.engine = None
        self._task: asyncio.Future | None = None
        self._thread: threading.Thread | None = None

    def start(self, engine) -> None:
        self.stop()
        self.engine = engine
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self._task = self.loop.create_task(self._guard(engine))
            ready.set()
            try:
                self.loop.run_until_complete(self._task)
            except asyncio.CancelledError:
                pass
            finally:
                pending = asyncio.all_tasks(self.loop)
                for t in pending:
                    t.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self.loop.close()

        self._thread = threading.Thread(target=run, daemon=True, name="engine")
        self._thread.start()
        ready.wait(5)

    async def _guard(self, engine) -> None:
        try:
            await engine.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("engine crashed")
            engine.bridge.notice.emit(f"My brain crashed: {e}")
            engine.bridge.emotion.emit("error", 0)

    def call(self, fn: Callable, *args) -> None:
        """Run fn(*args) on the engine loop (thread-safe)."""
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(fn, *args)

    def stop(self) -> None:
        if self.loop and self._task and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._task.cancel)
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self.loop = None
        self.engine = None
