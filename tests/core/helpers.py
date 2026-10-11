"""Test helpers: a small simulated CEL on an in-memory broker."""

from __future__ import annotations

from dataclasses import dataclass, field

from custom_components.cel_ca.core.identity import Identity
from custom_components.cel_ca.core.messages import MsgType, encode_envelope
from custom_components.cel_ca.core.node import CelNode, NodeConfig
from custom_components.cel_ca.core.registry import Member, build_registry_body
from custom_components.cel_ca.core.transport import InMemoryBroker, InMemoryTransport

CID = "cel-test"
T0 = 1_790_000_000_000  # 2026-09-21


class FakeClock:
    """Controllable clock in unix ms."""

    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += int(seconds * 1000)


def member_of(identity: Identity, name: str, roles=("producer", "consumer"), **kw) -> Member:
    return Member(identity.hems_id, identity.public_key, name, frozenset(roles), **kw)


def registry_payload(admin: Identity, serial: int, members, revoked=(), ts: int = T0, cid: str = CID) -> bytes:
    body = build_registry_body(serial, members, revoked)
    return encode_envelope(admin, cid, MsgType.REGISTRY, body, ts)


@dataclass
class Community:
    broker: InMemoryBroker
    clock: FakeClock
    admin: Identity
    nodes: dict[str, CelNode] = field(default_factory=dict)
    identities: dict[str, Identity] = field(default_factory=dict)
    serial: int = 1

    async def publish_registry(self, members=None, revoked=(), serial: int | None = None) -> None:
        if members is None:
            members = [member_of(i, n) for n, i in self.identities.items()]
        self.serial = serial if serial is not None else self.serial + 1
        payload = registry_payload(self.admin, self.serial, members, revoked, ts=self.clock())
        await self.broker.route(None, f"cel/{CID}/registry", payload, True)

    async def tick(self) -> None:
        for node in self.nodes.values():
            await node.async_tick()


async def make_community(names=("alice", "bob", "carol"), roles=None, config_kw=None) -> Community:
    broker = InMemoryBroker()
    clock = FakeClock()
    admin = Identity.generate()
    comm = Community(broker, clock, admin)
    roles = roles or {}
    members = []
    for name in names:
        ident = Identity.generate()
        comm.identities[name] = ident
        members.append(member_of(ident, name, roles.get(name, ("producer", "consumer"))))
    payload = registry_payload(admin, 1, members, ts=clock())
    await broker.route(None, f"cel/{CID}/registry", payload, True)
    for name in names:
        ident = comm.identities[name]
        cfg = NodeConfig(name=name, roles=frozenset(roles.get(name, ("producer", "consumer"))), **(config_kw or {}))
        node_holder: dict = {}
        transport = InMemoryTransport(broker)
        node = CelNode(ident, CID, admin.public_key, transport, cfg, clock=clock)
        transport.will = node.will
        node_holder["n"] = node
        comm.nodes[name] = node
    for node in comm.nodes.values():
        await node.async_start()
    return comm


def feed_export(node: CelNode, clock: FakeClock, export_w: int, seconds: int = 60, step: int = 10) -> None:
    """Feed a constant export (negative import) for some seconds, advancing time."""
    clock.t += 1
    node.record_grid_power(-export_w)
    for _ in range(seconds // step):
        clock.advance(step)
        node.record_grid_power(-export_w)
