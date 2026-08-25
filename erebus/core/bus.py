"""A tiny async pub/sub bus.

Every part of Erebus talks through this and nothing else, which is what makes
the pieces swappable: the visualiser does not know a microphone exists, and the
speech recogniser does not know a browser exists. They only know events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .state import State

log = logging.getLogger("erebus.bus")


@dataclass
class Event:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        # Envelope keys are written last so a payload field named "kind" or
        # "ts" can never shadow the event's own identity.
        return {**self.data, "kind": self.kind, "ts": self.ts}


class EventBus:
    """Fan-out to any number of subscribers, none of which can block a publisher.

    Each subscriber gets its own bounded queue. A subscriber that falls behind
    loses its oldest events rather than stalling the audio pipeline - dropping a
    stale amplitude frame is always better than making the assistant lag.
    """

    def __init__(self, queue_size: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_size = queue_size
        self._state = State.IDLE
        self._lock = asyncio.Lock()

    # -- state ---------------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    async def set_state(self, state: State, /, **data: Any) -> None:
        """Move the machine to `state` and tell everyone."""
        if state == self._state and not data:
            return
        self._state = state
        log.debug("state -> %s %s", state, data or "")
        await self.publish("state", state=str(state), **data)

    # -- pub/sub -------------------------------------------------------------

    async def publish(self, kind: str, /, **data: Any) -> None:
        event = Event(kind, data)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest and retry once; never block the producer.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def publish_soon(self, kind: str, /, **data: Any) -> None:
        """Publish from a non-async context (e.g. an audio callback thread)."""
        try:
            loop = self._loop
        except AttributeError:
            return
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self.publish(kind, **data))
        )

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the loop so audio threads can publish into it."""
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
