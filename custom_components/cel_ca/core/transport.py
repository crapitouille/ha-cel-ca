"""Transport abstraction (topic pub/sub with retained state) and an in-memory broker for tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging

_LOGGER = logging.getLogger(__name__)

MessageHandler = Callable[[str, bytes], Awaitable[None]]
ConnectHandler = Callable[[], Awaitable[None]]
StateHandler = Callable[[bool], None]


class TransportError(Exception):
    """Raised when a message cannot be sent."""


class Transport:
    """Base class of transports."""

    def __init__(self) -> None:
        self.on_message: MessageHandler | None = None
        self.on_connect: ConnectHandler | None = None
        self.on_state: StateHandler | None = None
        self.connected = False

    async def start(self) -> None:
        """Start the transport (connect in background)."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Stop the transport."""
        raise NotImplementedError

    async def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None:
        """Publish with QoS 1."""
        raise NotImplementedError

    async def subscribe(self, topic_filter: str) -> None:
        """Subscribe with QoS 1."""
        raise NotImplementedError

    def _set_connected(self, value: bool) -> None:
        if value != self.connected:
            self.connected = value
            if self.on_state:
                self.on_state(value)


def topic_matches(topic_filter: str, topic: str) -> bool:
    """MQTT wildcard matching (+ and #)."""
    f_parts = topic_filter.split("/")
    t_parts = topic.split("/")
    for i, part in enumerate(f_parts):
        if part == "#":
            return True
        if i >= len(t_parts):
            return False
        if part not in ("+", t_parts[i]):
            return False
    return len(f_parts) == len(t_parts)


class InMemoryBroker:
    """Tiny MQTT-semantics broker: retained messages, wildcards, last will."""

    def __init__(self) -> None:
        self.retained: dict[str, bytes] = {}
        self.clients: list[InMemoryTransport] = []
        self.log: list[tuple[str, bytes, bool]] = []

    async def route(self, sender: InMemoryTransport | None, topic: str, payload: bytes, retain: bool) -> None:
        self.log.append((topic, payload, retain))
        if retain:
            if payload:
                self.retained[topic] = payload
            else:
                self.retained.pop(topic, None)
        for client in list(self.clients):
            if client.connected and any(topic_matches(f, topic) for f in client.filters):
                await client.deliver(topic, payload)


class InMemoryTransport(Transport):
    """Transport connected to an :class:`InMemoryBroker`."""

    def __init__(self, broker: InMemoryBroker, *, will: tuple[str, bytes] | None = None) -> None:
        super().__init__()
        self.broker = broker
        self.will = will
        self.filters: list[str] = []
        self.published: list[tuple[str, bytes, bool]] = []

    async def start(self) -> None:
        self.broker.clients.append(self)
        self._set_connected(True)
        if self.on_connect:
            await self.on_connect()

    async def stop(self) -> None:
        await self.drop(send_will=False)

    async def drop(self, *, send_will: bool = True) -> None:
        """Simulate a connection loss (sends the last will)."""
        if self in self.broker.clients:
            self.broker.clients.remove(self)
        self.filters.clear()
        self._set_connected(False)
        if send_will and self.will:
            await self.broker.route(None, self.will[0], self.will[1], True)

    async def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None:
        if not self.connected:
            raise TransportError("not connected")
        self.published.append((topic, payload, retain))
        await self.broker.route(self, topic, payload, retain)

    async def subscribe(self, topic_filter: str) -> None:
        if not self.connected:
            raise TransportError("not connected")
        self.filters.append(topic_filter)
        for topic, payload in list(self.broker.retained.items()):
            if topic_matches(topic_filter, topic):
                await self.deliver(topic, payload)

    async def deliver(self, topic: str, payload: bytes) -> None:
        if self.on_message:
            try:
                await self.on_message(topic, payload)
            except Exception:  # pragma: no cover - mirrors the MQTT transport
                _LOGGER.exception("Error handling message on %s", topic)


async def drain() -> None:
    """Let pending tasks run (test helper)."""
    for _ in range(5):
        await asyncio.sleep(0)
