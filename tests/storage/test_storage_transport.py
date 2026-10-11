"""Several HEMS cooperating through Azure Storage (Azurite), with the real protocol engine."""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp
import pytest

from custom_components.cel_ca.core.identity import Identity
from custom_components.cel_ca.core.messages import MsgType, encode_envelope
from custom_components.cel_ca.core.node import CelNode, NodeConfig
from custom_components.cel_ca.core.registry import Member, build_registry_body
from custom_components.cel_ca.core.storage_access import AccessKey
from custom_components.cel_ca.core.storage_transport import StorageTransport, probe_access
from custom_components.cel_ca.core.transport import TransportError

FAST = {"state_interval": 0.2, "inbox_interval": 0.2, "fast_interval": 0.05, "fast_window": 5}


async def wait_for(predicate, timeout: float = 8.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


class Cel:
    """A community on Azurite: admin, members, nodes."""

    def __init__(self, azurite, cid: str) -> None:
        self.azurite = azurite
        self.admin_store = azurite.admin()
        self.cid = cid
        self.admin = Identity.generate()
        self.ids: dict[str, Identity] = {}
        self.nodes: dict[str, CelNode] = {}
        self.serial = 0

    def publish_registry(self, names=None, revoked=(), params=None) -> None:
        names = list(self.ids) if names is None else names
        self.serial += 1
        members = [Member(self.ids[n].hems_id, self.ids[n].public_key, n, frozenset({"producer", "consumer"})) for n in names]
        body = build_registry_body(self.serial, members, revoked, params)
        self.admin_store.put_registry(self.cid, encode_envelope(self.admin, self.cid, MsgType.REGISTRY, body, int(time.time() * 1000)))

    async def add(self, name: str, **transport_kw) -> CelNode:
        ident = self.ids.setdefault(name, Identity.generate())
        key = self.admin_store.issue_access(self.cid, ident.hems_id, self.admin.public_b64)
        transport = StorageTransport(AccessKey.decode(key.encode()), **{**FAST, **transport_kw})
        node = CelNode(ident, self.cid, self.admin.public_key, transport, NodeConfig(name=name, request_timeout_s=10))
        self.nodes[name] = node
        return node

    async def stop(self) -> None:
        for node in self.nodes.values():
            await node.async_stop()


@pytest.fixture
async def cel(azurite, request):
    community = Cel(azurite, f"cel-{request.node.name[-20:].replace('_', '-').lower().strip('-')}"[:30].rstrip("-"))
    yield community
    await community.stop()


async def test_two_hems_reserve_over_azure_storage(cel) -> None:
    alice, bob = await cel.add("alice"), await cel.add("bob")
    cel.publish_registry()
    await alice.async_start()
    await bob.async_start()
    await wait_for(lambda: alice.enrolled and bob.enrolled)
    await wait_for(lambda: alice.id in bob.peers and bob.peers[alice.id].online)

    alice.record_grid_power(-3100)
    await alice.async_tick()
    await wait_for(lambda: bob.community_available_w() == 3000)

    started = time.monotonic()
    res = await bob.reserve(2000, 600_000)
    assert res["status"] == "accepted", res
    assert time.monotonic() - started < 5  # fast inbox polling after sending the request
    await wait_for(lambda: bob.community_available_w() == 1000)
    assert alice.book.reserved_power_at(alice.clock()) == 2000

    assert await bob.cancel_reservation(res["reservation_id"])
    await wait_for(lambda: alice.book.reserved_power_at(alice.clock()) == 0)

    # a clean stop empties the status row: the peer sees it offline
    await alice.async_stop()
    await wait_for(lambda: not bob.peers[alice.id].online)
    # the inbox was consumed (messages deleted after delivery)
    assert bob.transport.stats["delivered"] > 0


async def test_global_reservation_and_settlement_ring(cel) -> None:
    alice, carol, bob = await cel.add("alice"), await cel.add("carol"), await cel.add("bob")
    cel.publish_registry()
    for node in (alice, carol, bob):
        await node.async_start()
    await wait_for(lambda: all(n.enrolled for n in (alice, carol, bob)))
    alice.record_grid_power(-3100)
    carol.record_grid_power(-1100)
    await alice.async_tick()
    await carol.async_tick()
    await wait_for(lambda: bob.community_available_w() == 4000)
    res = await bob.reserve_global(2000, 600_000)
    assert res["status"] == "accepted" and {r["producer_name"]: r["power_w"] for r in res["reservations"]} == {"alice": 1500, "carol": 500}

    # settlements use a ring of rows: several in a row are all seen by peers
    for i in range(3):
        await alice._send(
            MsgType.SETTLEMENT,
            {"rid": f"r{i}", "role": "producer", "counterpart": bob.id, "reserved_wh": 10, "measured_wh": 10, "ratio_pct": 100},
            f"cel/{cel.cid}/node/{alice.id}/settlement",
        )
    await wait_for(lambda: len(bob.peers[alice.id].ratios) == 3)


async def test_revoked_member_inbox_is_gone(cel) -> None:
    alice, bob = await cel.add("alice"), await cel.add("bob")
    cel.publish_registry()
    await alice.async_start()
    await bob.async_start()
    await wait_for(lambda: bob.enrolled and alice.id in bob.peers)
    cel.admin_store.delete_queue(bob.id)
    cel.admin_store.delete_partition(cel.cid, bob.id)
    with pytest.raises(TransportError):
        await alice.transport.publish(f"cel/{cel.cid}/node/{bob.id}/inbox", b"x")


async def test_transport_guards(cel) -> None:
    alice = await cel.add("alice")
    transport: StorageTransport = alice.transport
    for topic in (f"cel/{cel.cid}/registry", "other/topic", f"cel/{cel.cid}/node/{alice.id}/unknown"):
        with pytest.raises(TransportError):
            await transport.publish(topic, b"x")
    other = Identity.generate().hems_id
    with pytest.raises(TransportError):
        await transport.publish(f"cel/{cel.cid}/node/{other}/offers", b"x")
    await transport.stop()


async def test_bad_access_key_is_reported(cel, azurite) -> None:
    alice = await cel.add("alice")
    good: AccessKey = alice.transport.access
    tampered = AccessKey(**{**{f: getattr(good, f) for f in AccessKey.__slots__}, "tr": good.tr.replace("sig=", "sig=AA")})
    async with aiohttp.ClientSession() as session:
        assert await probe_access(good, session) is None
        assert (await probe_access(tampered, session))[0] == "invalid_auth"
        nowhere = AccessKey(**{**{f: getattr(good, f) for f in AccessKey.__slots__}, "tu": "http://127.0.0.1:9/x"})
        assert (await probe_access(nowhere, session))[0] == "cannot_connect"
        ghost = Identity.generate().hems_id
        no_inbox = AccessKey(**{**{f: getattr(good, f) for f in AccessKey.__slots__}, "id": ghost})
        assert (await probe_access(no_inbox, session))[0] in ("inbox_missing", "invalid_auth")

    transport = StorageTransport(tampered, **FAST)
    await transport.start()
    await wait_for(lambda: transport.auth_failed)
    assert not transport.connected
    await transport.stop()


async def test_reconnects_after_outage(cel, azurite) -> None:
    """The transport retries until the storage is reachable, then re-publishes its presence."""
    alice, bob = await cel.add("alice"), await cel.add("bob")
    cel.publish_registry()
    real = bob.transport.access
    bob.transport.access = AccessKey(**{**{f: getattr(real, f) for f in AccessKey.__slots__}, "tu": "http://127.0.0.1:9/x"})
    import custom_components.cel_ca.core.storage_transport as st

    st.BACKOFF_MIN_S, saved = 0.2, st.BACKOFF_MIN_S
    try:
        await bob.async_start()
        await alice.async_start()
        await wait_for(lambda: bob.transport.last_error is not None)
        assert not bob.connected
        bob.transport.access = real
        await wait_for(lambda: bob.connected and bob.enrolled)
        await wait_for(lambda: bob.id in alice.peers and alice.peers[bob.id].online)
    finally:
        st.BACKOFF_MIN_S = saved


def test_rows_are_plain_signed_json(azurite) -> None:
    """What is stored in Azure is exactly the signed protocol envelope."""
    admin = azurite.admin()
    ident = Identity.generate()
    payload = encode_envelope(ident, "cel-raw", MsgType.STATUS, {"name": "x", "roles": ["producer"], "sw": "1"}, 1)
    admin.ensure_table("cel-raw")
    admin.upsert("cel-raw", ident.hems_id, "status", payload.decode())
    row = admin.rows("cel-raw", ident.hems_id)[0]
    assert json.loads(row["payload"])["sig"]
