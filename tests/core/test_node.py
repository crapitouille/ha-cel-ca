"""End-to-end protocol scenarios between several nodes on an in-memory broker."""

from __future__ import annotations

import json

import pytest

from custom_components.cel_ca.core.identity import Identity
from custom_components.cel_ca.core.messages import MsgType, ProtocolError, canonical_json, encode_envelope
from custom_components.cel_ca.core.node import CelNode, NodeConfig, node_topic
from custom_components.cel_ca.core.transport import InMemoryTransport, topic_matches

from .helpers import CID, T0, feed_export, make_community, member_of, registry_payload

MIN = 60


def events_of(node: CelNode) -> list[tuple[str, dict]]:
    log: list[tuple[str, dict]] = []
    node.on_event = lambda kind, data: log.append((kind, data))
    return log


async def community_with_surplus(export_w=3100, **kw):
    comm = await make_community(**kw)
    alice = comm.nodes["alice"]
    feed_export(alice, comm.clock, export_w)
    await comm.tick()
    return comm


def test_topic_matching() -> None:
    assert topic_matches("cel/x/node/+/status", "cel/x/node/abc/status")
    assert not topic_matches("cel/x/node/+/status", "cel/x/node/abc/offers")
    assert topic_matches("cel/#", "cel/x/node/abc/offers")
    assert not topic_matches("cel/x/+", "cel/x/node/a")


async def test_presence_and_registry_names() -> None:
    comm = await make_community()
    bob = comm.nodes["bob"]
    assert bob.enrolled
    assert {p.name for p in bob.peers.values() if p.online} == {"alice", "carol"}


async def test_live_offer_follows_surplus_and_is_visible() -> None:
    comm = await community_with_surplus(3100)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    live = [o for o in alice.book.offers.values() if o.live]
    assert len(live) == 1 and live[0].power_w == 3000  # 3100 - 100 margin
    assert bob.community_available_w() == 3000
    assert bob.best_offers()[0]["producer"] == alice.id


async def test_no_offer_below_minimum_and_offer_stops_after_grace() -> None:
    comm = await community_with_surplus(550)  # 450 W after margin < 500 W min
    assert not comm.nodes["alice"].book.offers
    comm = await community_with_surplus(3100)
    alice = comm.nodes["alice"]
    for i in range(3):
        feed_export(alice, comm.clock, 0, seconds=60)
        await alice.async_tick()
        assert bool(alice.book.offers) is (i < 2)
    assert comm.nodes["bob"].community_available_w() == 0


async def test_small_variations_do_not_republish_power() -> None:
    comm = await community_with_surplus(3100)
    alice = comm.nodes["alice"]
    feed_export(alice, comm.clock, 3300)  # +6.7% < 10% threshold
    await alice.async_tick()
    assert next(iter(alice.book.offers.values())).power_w == 3000
    feed_export(alice, comm.clock, 4100)
    await alice.async_tick()
    assert next(iter(alice.book.offers.values())).power_w == 4000


async def test_reserve_happy_path_and_books_everywhere() -> None:
    comm = await community_with_surplus(3100)
    alice, bob, carol = (comm.nodes[n] for n in ("alice", "bob", "carol"))
    a_events, b_events = events_of(alice), events_of(bob)
    res = await bob.reserve(2000, 15 * MIN * 1000)
    assert res["status"] == "accepted" and res["power_w"] == 2000 and res["producer"] == alice.id
    assert bob.ledger.reserved_power_at(comm.clock()) == 2000
    assert alice.book.reserved_power_at(comm.clock()) == 2000
    assert [k for k, _ in a_events] == ["request_received"]
    assert [k for k, _ in b_events] == ["granted"]
    # everyone sees the remaining capacity and the public reservation book
    assert carol.community_available_w() == 1000
    assert carol.peers[alice.id].reservations[0]["consumer"] == bob.id


async def test_live_offer_grants_beyond_rolling_horizon() -> None:
    comm = await community_with_surplus(3100)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    r = await bob.reserve(1000, 60 * MIN * 1000)  # longer than the 15 min horizon
    assert r["status"] == "accepted" and r["end"] - r["start"] == 3600_000
    assert bob.best_offers()[0]["rolling"] is True
    for _ in range(20):  # 20 minutes of steady surplus: the reservation lives on
        feed_export(alice, comm.clock, 3100)
        await comm.tick()
    assert r["reservation_id"] in bob.ledger.active
    assert bob.ledger.active[r["reservation_id"]].power_w == 1000


async def test_manual_offer_clips_reservation_window() -> None:
    comm = await make_community()
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    await alice.publish_offer(2000, None, comm.clock() + 600_000)
    r = await bob.reserve(1000, 3600_000)
    assert r["status"] == "partial" and r["end"] == comm.clock() + 600_000


async def test_partial_and_competing_consumers() -> None:
    comm = await community_with_surplus(3100)
    bob, carol = comm.nodes["bob"], comm.nodes["carol"]
    assert (await bob.reserve(2000, 900_000))["status"] == "accepted"
    r = await carol.reserve(2000, 900_000, min_power_w=500)
    assert r["status"] == "partial" and r["power_w"] == 1000
    r = await carol.reserve(500, 900_000)
    assert r["status"] == "rejected" and r["reason"] == "no_offer"


async def test_reserve_targets_specific_producer() -> None:
    comm = await community_with_surplus(3100)
    carol = comm.nodes["carol"]
    feed_export(carol, comm.clock, 1100)
    await comm.tick()
    bob = comm.nodes["bob"]
    r = await bob.reserve(800, 600_000, producer=carol.id)
    assert r["status"] == "accepted" and r["producer"] == carol.id
    assert (await bob.reserve(100, 600_000, producer=bob.id))["reason"] == "no_offer"


async def test_cancel_frees_capacity() -> None:
    comm = await community_with_surplus(3100)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    r = await bob.reserve(3000, 900_000)
    assert await bob.cancel_reservation(r["reservation_id"])
    assert alice.book.reserved_power_at(comm.clock()) == 0
    assert comm.nodes["carol"].community_available_w() == 3000
    assert not await bob.cancel_reservation("unknown")


async def test_surplus_drop_curtails_consumer() -> None:
    comm = await community_with_surplus(3100)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    log = events_of(bob)
    r = await bob.reserve(3000, 900_000, min_power_w=1000)
    feed_export(alice, comm.clock, 2100)
    await alice.async_tick()
    assert bob.ledger.active[r["reservation_id"]].power_w == 2000
    assert log[-1][0] == "curtailed"
    for _ in range(3):
        feed_export(alice, comm.clock, 0)
        await alice.async_tick()
    assert r["reservation_id"] not in bob.ledger.active
    assert log[-1][0] == "cancelled" and log[-1][1]["reason"] == "surplus_ended"


async def test_manual_offer_future_window_and_withdraw() -> None:
    comm = await make_community()
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    start = comm.clock() + 3600_000
    oid = await alice.publish_offer(5000, start, start + 7200_000)
    assert bob.community_available_w() == 0  # not yet
    r = await bob.reserve(4000, 3600_000, start=start)
    assert r["status"] == "accepted" and r["start"] == start and r["offer_id"] == oid
    assert await alice.withdraw_offer(oid)
    assert r["reservation_id"] not in bob.ledger.active
    with pytest.raises(ValueError):
        await alice.publish_offer(100, None, comm.clock() - 1)


async def test_expiry_and_settlement_scores() -> None:
    comm = await community_with_surplus(3100)
    alice, bob, carol = (comm.nodes[n] for n in ("alice", "bob", "carol"))
    await bob.reserve(3000, 10 * MIN * 1000)
    for _ in range(11):
        alice.record_grid_power(-3100)
        bob.record_grid_power(1500)  # bob only consumes half of what he reserved
        comm.clock.advance(60)
        await comm.tick()
    assert alice.compliance_pct == 100
    assert bob.compliance_pct == 50
    assert carol.peers[bob.id].reputation_pct == 50
    assert carol.peers[alice.id].reputation_pct == 100
    assert not bob.ledger.active and not alice.book.reservations


async def test_settlement_skipped_without_measurements() -> None:
    comm = await community_with_surplus(3100)
    bob = comm.nodes["bob"]
    await bob.reserve(1000, 5 * MIN * 1000)
    comm.clock.advance(6 * MIN)
    await bob.async_tick()
    assert bob.compliance_pct is None and bob.stats["settlement_no_data"] == 1


async def test_request_timeout_and_late_answer_is_released() -> None:
    comm = await community_with_surplus(3100)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    bob.config.request_timeout_s = 0.05
    # alice stops processing her inbox (but stays "online")
    handler = alice.transport.on_message
    held = []

    async def hold(topic, payload):
        if topic.endswith("/inbox"):
            held.append((topic, payload))
            return
        await handler(topic, payload)

    alice.transport.on_message = hold
    r = await bob.reserve(1000, 600_000)
    assert r["status"] == "timeout"
    alice.transport.on_message = handler
    for topic, payload in held:
        await handler(topic, payload)
    # bob cancelled the late grant, so alice's capacity is back
    assert alice.book.reserved_power_at(comm.clock()) == 0
    assert bob.stats["late_response_cancelled"] == 1


async def test_disconnect_will_marks_offline() -> None:
    comm = await community_with_surplus(3100)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    await alice.transport.drop()
    assert not bob.peers[alice.id].online
    assert bob.community_available_w() == 0
    assert (await bob.reserve(100, 600_000))["reason"] == "no_offer"
    assert (await alice.reserve(100, 600_000))["reason"] == "not_connected"


async def test_heartbeat_timeout_marks_offline() -> None:
    comm = await make_community()
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    alice.transport.on_message = None
    comm.clock.advance(200)
    await bob.async_tick()  # alice did not tick -> no heartbeat
    assert not bob.peers[alice.id].online


async def test_roles_are_enforced() -> None:
    comm = await make_community(roles={"alice": ("producer",), "bob": ("consumer",), "carol": ("consumer",)})
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    feed_export(bob, comm.clock, 5000)
    feed_export(alice, comm.clock, 3100)
    await comm.tick()
    assert not bob.book.offers  # consumer-only never offers
    assert (await alice.reserve(100, 600_000))["reason"] == "not_consumer"
    assert (await bob.reserve(1000, 600_000))["status"] == "accepted"
    with pytest.raises(ValueError):
        await bob.publish_offer(100, None, comm.clock() + 1000_000)


# ------------------------------------------------------------------ security
async def test_unregistered_node_is_ignored() -> None:
    comm = await community_with_surplus(3100)
    stranger = CelNode(Identity.generate(), CID, comm.admin.public_key, InMemoryTransport(comm.broker), NodeConfig(name="mallory"), clock=comm.clock)
    await stranger.async_start()
    feed_export(stranger, comm.clock, 9000)
    await stranger.async_tick()
    bob = comm.nodes["bob"]
    assert stranger.id not in bob.peers
    assert bob.stats["unknown_sender"] >= 1
    assert not stranger.enrolled
    assert (await stranger.reserve(100, 600_000))["reason"] == "not_enrolled"


async def test_forged_messages_are_dropped() -> None:
    comm = await community_with_surplus(3100)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    mallory = Identity.generate()
    body = {"offers": [{"oid": "fake", "start": comm.clock(), "end": comm.clock() + 10**6, "power_w": 99999, "available_w": 99999, "rolling": False}]}
    forged = encode_envelope(mallory, CID, MsgType.OFFER_BOOK, body, comm.clock() + 5, src=alice.id)
    await comm.broker.route(None, node_topic(CID, alice.id, "offers"), forged, True)
    assert bob.stats["bad_signature"] == 1
    assert bob.community_available_w() == 3000


async def test_member_cannot_publish_on_behalf_of_another() -> None:
    comm = await community_with_surplus(3100)
    alice, bob, carol = (comm.nodes[n] for n in ("alice", "bob", "carol"))
    body = {"offers": []}
    payload = encode_envelope(carol.identity, CID, MsgType.OFFER_BOOK, body, comm.clock() + 5)
    await comm.broker.route(None, node_topic(CID, alice.id, "offers"), payload, True)
    assert bob.stats["topic_src_mismatch"] == 1
    assert bob.community_available_w() == 3000


async def test_replay_and_stale_messages_rejected() -> None:
    comm = await community_with_surplus(3100)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    await bob.reserve(1000, 600_000)
    request = next(p for t, p, _ in bob.transport.published if t.endswith(f"{alice.id}/inbox"))
    await comm.broker.route(None, node_topic(CID, alice.id, "inbox"), request, False)
    assert alice.stats["replay"] == 1
    # an old retained offer book cannot overwrite a newer one
    old_book = next(p for t, p, _ in alice.transport.published if t.endswith("/offers"))
    feed_export(alice, comm.clock, 3100)
    await alice.async_tick()
    await comm.broker.route(None, node_topic(CID, alice.id, "offers"), old_book, True)
    assert bob.stats["stale"] >= 1
    # a command signed too long ago is expired
    comm.clock.advance(600)
    stale = encode_envelope(bob.identity, CID, MsgType.RESERVE_CANCEL, {"rid": "x1", "to": alice.id}, comm.clock() - 300_000)
    await comm.broker.route(None, node_topic(CID, alice.id, "inbox"), stale, False)
    assert alice.stats["expired"] == 1


async def test_inbox_message_for_someone_else_is_dropped() -> None:
    comm = await community_with_surplus(3100)
    alice, bob, carol = (comm.nodes[n] for n in ("alice", "bob", "carol"))
    body = {"rid": "r1", "to": carol.id, "oid": "x", "start": comm.clock(), "end": comm.clock() + 600_000, "power_w": 1, "min_power_w": 1}
    payload = encode_envelope(bob.identity, CID, MsgType.RESERVE_REQUEST, body, comm.clock())
    await comm.broker.route(None, node_topic(CID, alice.id, "inbox"), payload, False)
    assert alice.stats["not_for_me"] == 1


async def test_inbox_rate_limit() -> None:
    comm = await community_with_surplus(3100)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    for i in range(40):
        payload = encode_envelope(bob.identity, CID, MsgType.RESERVE_CANCEL, {"rid": f"x{i}", "to": alice.id}, comm.clock())
        await comm.broker.route(None, node_topic(CID, alice.id, "inbox"), payload, False)
    assert alice.stats["rate_limited"] == 10


async def test_registry_must_be_signed_by_admin_and_monotonic() -> None:
    comm = await make_community()
    bob = comm.nodes["bob"]
    fake_admin = Identity.generate()
    evil = registry_payload(fake_admin, 99, [member_of(Identity.generate(), "evil")], ts=comm.clock())
    await comm.broker.route(None, f"cel/{CID}/registry", evil, True)
    assert bob.registry.serial == 1 and bob.stats["registry_bad_signature"] == 1
    await comm.publish_registry(serial=5)
    assert bob.registry.serial == 5
    await comm.publish_registry(serial=3)
    assert bob.registry.serial == 5 and bob.stats["registry_rollback"] == 1


async def test_registry_rejects_member_id_not_matching_key() -> None:
    comm = await make_community()
    alice = comm.nodes["alice"]
    body = json.loads(registry_payload(comm.admin, 2, [member_of(comm.identities["bob"], "bob")]))["body"]
    body["members"][0]["id"] = alice.id  # claim alice's id with bob's key
    payload = encode_envelope(comm.admin, CID, MsgType.REGISTRY, body, comm.clock())
    await comm.broker.route(None, f"cel/{CID}/registry", payload, True)
    assert alice.registry.serial == 1
    assert any(k.startswith("registry_invalid") for k in alice.stats)


async def test_revocation_removes_peer_and_cancels_reservations() -> None:
    comm = await community_with_surplus(3100)
    alice, bob, carol = (comm.nodes[n] for n in ("alice", "bob", "carol"))
    await bob.reserve(1000, 900_000)
    members = [member_of(comm.identities[n], n) for n in ("alice", "bob", "carol")]
    await comm.publish_registry(members=members, revoked=[bob.id])
    assert bob.id not in alice.peers and bob.id not in carol.peers
    assert alice.book.reserved_power_at(comm.clock()) == 0
    assert not bob.enrolled


async def test_expired_membership() -> None:
    comm = await make_community()
    members = [member_of(comm.identities["alice"], "alice", valid_until=comm.clock() + 1000)]
    members += [member_of(comm.identities[n], n) for n in ("bob", "carol")]
    await comm.publish_registry(members=members)
    assert comm.nodes["alice"].enrolled
    comm.clock.advance(2)
    assert not comm.nodes["alice"].enrolled


async def test_messages_before_registry_are_buffered() -> None:
    """Retained state received before the registry is verified afterwards."""
    comm = await community_with_surplus(3100)
    alice = comm.nodes["alice"]
    late = CelNode(comm.identities["bob"], CID, comm.admin.public_key, InMemoryTransport(comm.broker), NodeConfig(name="bob"), clock=comm.clock)
    # Simulate out-of-order delivery: offers first, registry later.
    registry = comm.broker.retained.pop(f"cel/{CID}/registry")
    await late.async_start()
    assert late.community_available_w() == 0
    await comm.broker.route(None, f"cel/{CID}/registry", registry, True)
    assert late.peers[alice.id].online
    assert late.community_available_w() == 3000


async def test_wrong_community_payload_is_dropped() -> None:
    comm = await community_with_surplus(3100)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    payload = encode_envelope(alice.identity, "other-cel", MsgType.OFFER_BOOK, {"offers": []}, comm.clock() + 10)
    await comm.broker.route(None, node_topic(CID, alice.id, "offers"), payload, True)
    assert bob.stats["wrong_community"] == 1


async def test_garbage_payload_does_not_crash() -> None:
    comm = await make_community()
    bob = comm.nodes["bob"]
    await comm.broker.route(None, node_topic(CID, comm.nodes["alice"].id, "offers"), b"\x00garbage", True)
    await comm.broker.route(None, f"cel/{CID}/something/else", canonical_json({}), False)
    assert any(k.startswith("invalid") for k in bob.stats)


async def test_state_persistence_roundtrip() -> None:
    comm = await community_with_surplus(3100)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    r = await bob.reserve(1000, 900_000)
    state_a, state_b = alice.export_state(), bob.export_state()
    json.dumps(state_a), json.dumps(state_b)
    a2 = CelNode(alice.identity, CID, comm.admin.public_key, InMemoryTransport(comm.broker), alice.config, clock=comm.clock)
    a2.restore_state(state_a)
    assert a2.registry.serial == 1 and a2.enrolled
    assert a2.book.reserved_power_at(comm.clock()) == 1000
    b2 = CelNode(bob.identity, CID, comm.admin.public_key, InMemoryTransport(comm.broker), bob.config, clock=comm.clock)
    b2.restore_state(state_b)
    assert r["reservation_id"] in b2.ledger.active
    # a tampered persisted registry is ignored
    state_a["registry"] = state_a["registry"].replace('"serial":1', '"serial":7')
    a3 = CelNode(alice.identity, CID, comm.admin.public_key, InMemoryTransport(comm.broker), alice.config, clock=comm.clock)
    a3.restore_state(state_a)
    assert a3.registry.serial == 0


async def test_stop_clears_presence() -> None:
    comm = await make_community()
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    await alice.async_stop()
    assert not bob.peers[alice.id].online
    assert node_topic(CID, alice.id, "status") not in comm.broker.retained


async def test_clock_skew_future_message_dropped() -> None:
    comm = await make_community()
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    payload = encode_envelope(alice.identity, CID, MsgType.STATUS, {"name": "a", "roles": ["producer"], "sw": "1"}, T0 + 10**7)
    await comm.broker.route(None, node_topic(CID, alice.id, "status"), payload, True)
    assert bob.stats["future"] == 1


# ------------------------------------------------------------ community view
async def test_participants_view_is_consistent_everywhere() -> None:
    comm = await community_with_surplus(3100)
    alice, bob, carol = (comm.nodes[n] for n in ("alice", "bob", "carol"))
    await bob.reserve(2000, 900_000)
    views = {n: {r["name"]: r for r in node.participants()} for n, node in comm.nodes.items()}
    for name, view in views.items():
        assert list(view) == ["alice", "bob", "carol"]  # sorted, self included
        assert view[name]["is_self"] and sum(r["is_self"] for r in view.values()) == 1
        assert view["alice"]["offered_w"] == 3000
        assert view["alice"]["available_w"] == 1000
        assert view["alice"]["reserved_out_w"] == 2000
        assert view["bob"]["reserved_in_w"] == 2000
        assert view["carol"]["reserved_in_w"] == 0 and view["carol"]["offered_w"] == 0
        assert all(r["online"] for r in view.values())
    assert carol.community_totals() == {"members": 3, "online": 3, "offered_w": 3000, "available_w": 1000, "reserved_w": 2000}
    assert alice.community_totals() == carol.community_totals()


async def test_participants_offline_revoked_and_reputation() -> None:
    comm = await community_with_surplus(3100)
    alice, bob, carol = (comm.nodes[n] for n in ("alice", "bob", "carol"))
    await bob.reserve(3000, 10 * MIN * 1000)
    for _ in range(11):
        alice.record_grid_power(-3100)
        bob.record_grid_power(1500)
        comm.clock.advance(60)
        await comm.tick()
    view = {r["name"]: r for r in carol.participants()}
    assert view["alice"]["reputation_pct"] == 100 and view["bob"]["reputation_pct"] == 50
    assert {r["name"]: r for r in bob.participants()}["bob"]["reputation_pct"] == 50
    # an offline producer no longer counts as offering
    await alice.transport.drop()
    view = {r["name"]: r for r in carol.participants()}
    assert not view["alice"]["online"] and view["alice"]["offered_w"] == 0
    assert carol.community_totals()["online"] == 2
    # a revoked member disappears from the view
    members = [member_of(comm.identities[n], n) for n in ("alice", "bob", "carol")]
    await comm.publish_registry(members=members, revoked=[bob.id])
    assert [r["name"] for r in carol.participants()] == ["alice", "carol"]


async def test_participants_empty_without_registry() -> None:
    comm = await make_community()
    lonely = CelNode(Identity.generate(), CID, Identity.generate().public_key, InMemoryTransport(comm.broker), NodeConfig(), clock=comm.clock)
    assert lonely.participants() == []
    assert lonely.community_totals()["members"] == 0


# ------------------------------------------------------- global reservation
async def two_producers(comm_export=(3100, 1100)):
    comm = await make_community(names=("alice", "bob", "carol", "dave"))
    for name, export in zip(("alice", "carol"), comm_export, strict=True):
        feed_export(comm.nodes[name], comm.clock, export)
    await comm.tick()
    return comm


async def test_global_reservation_is_proportional() -> None:
    comm = await two_producers()  # alice offers 3000 W, carol 1000 W
    alice, bob, carol = (comm.nodes[n] for n in ("alice", "bob", "carol"))
    log = events_of(bob)
    res = await bob.reserve_global(2000, 30 * MIN * 1000)
    assert res["status"] == "accepted" and res["power_w"] == 2000
    by_producer = {r["producer_name"]: r["power_w"] for r in res["reservations"]}
    assert by_producer == {"alice": 1500, "carol": 500}
    assert alice.book.reserved_power_at(comm.clock()) == 1500
    assert carol.book.reserved_power_at(comm.clock()) == 500
    assert {r.group for r in bob.ledger.active.values()} == {res["group_id"]}
    assert "group_granted" in [k for k, _ in log]
    # the community view adds everything up
    assert {r["name"]: r for r in comm.nodes["dave"].participants()}["bob"]["reserved_in_w"] == 2000


async def test_global_reservation_beyond_a_single_producer() -> None:
    comm = await two_producers()
    bob = comm.nodes["bob"]
    assert (await bob.reserve(4000, 600_000))["reason"] == "no_offer"  # no single producer can
    res = await bob.reserve_global(4000, 600_000)
    assert res["status"] == "accepted" and res["power_w"] == 4000 and len(res["reservations"]) == 2


async def test_global_reservation_partial_above_minimum() -> None:
    comm = await two_producers()
    bob = comm.nodes["bob"]
    res = await bob.reserve_global(6000, 600_000, min_power_w=3500)
    assert res["status"] == "partial" and res["power_w"] == 4000


async def test_global_reservation_all_or_nothing() -> None:
    comm = await two_producers()
    alice, bob, carol = (comm.nodes[n] for n in ("alice", "bob", "carol"))
    res = await bob.reserve_global(6000, 600_000)  # min defaults to the full power
    assert res["status"] == "rejected" and res["reason"] == "insufficient"
    assert not bob.ledger.active
    # a producer refuses (not reachable): what was granted by the others is released
    bob.config.request_timeout_s = 0.05
    handler = carol.transport.on_message

    async def deaf(topic, payload):
        if not topic.endswith("/inbox"):
            await handler(topic, payload)

    carol.transport.on_message = deaf
    res = await bob.reserve_global(4000, 600_000)
    assert res["status"] == "rejected" and res["reason"] == "insufficient"
    assert alice.book.reserved_power_at(comm.clock()) == 0 and not bob.ledger.active


async def test_global_reservation_tops_up_when_a_producer_is_short() -> None:
    comm = await two_producers((3100, 1100))
    alice, bob, carol, dave = (comm.nodes[n] for n in ("alice", "bob", "carol", "dave"))
    # dave already took most of carol's surplus, but bob's view is stale
    stale = {pid: list(p.offers) for pid, p in bob.peers.items()}
    await dave.reserve(800, 600_000, producer=carol.id)
    for pid, offers in stale.items():
        bob.peers[pid].offers = offers
    res = await bob.reserve_global(3000, 600_000, min_power_w=3000)
    assert res["status"] == "accepted" and res["power_w"] == 3000
    assert alice.book.reserved_power_at(comm.clock()) + carol.book.reserved_power_at(comm.clock()) == 3800


async def test_cancel_whole_group_and_group_update_event() -> None:
    comm = await two_producers()
    alice, bob, carol = (comm.nodes[n] for n in ("alice", "bob", "carol"))
    log = events_of(bob)
    res = await bob.reserve_global(2000, 30 * MIN * 1000)
    # alice's surplus drops: bob is told the new total of the group
    feed_export(alice, comm.clock, 1100)
    await alice.async_tick()
    update = [d for k, d in log if k == "group_updated"][-1]
    assert update["group_id"] == res["group_id"] and update["power_w"] == 1000 + 500
    assert await bob.cancel_reservation(res["group_id"])
    assert not bob.ledger.active
    assert alice.book.reserved_power_at(comm.clock()) == 0 and carol.book.reserved_power_at(comm.clock()) == 0


async def test_global_reservation_guards() -> None:
    comm = await make_community(roles={"alice": ("producer",)})
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    assert (await alice.reserve_global(100, 600_000))["reason"] == "not_consumer"
    assert (await bob.reserve_global(100, 600_000))["reason"] == "no_offer"
    assert (await bob.reserve_global(0, 600_000))["reason"] == "invalid_request"


async def test_consumer_settlement_shared_between_simultaneous_reservations() -> None:
    comm = await two_producers()
    alice, bob, carol = (comm.nodes[n] for n in ("alice", "bob", "carol"))
    await bob.reserve_global(2000, 10 * MIN * 1000)
    for _ in range(11):
        alice.record_grid_power(-3100)
        carol.record_grid_power(-1100)
        bob.record_grid_power(1000)  # only half of the 2000 W reserved in total
        comm.clock.advance(60)
        await comm.tick()
    assert bob.compliance_pct == 50  # not 100 % for the 500 W part nor 67 % for the 1500 W part


# ------------------------------------------------------------- slot market
SLOT = 15 * MIN * 1000


async def slot_community(export=3100, at_minute=7):
    """Community with 15-min slots; the clock is put `at_minute` into a slot."""
    comm = await make_community()
    comm.clock.t = (comm.clock.t // SLOT + 1) * SLOT + at_minute * 60_000 - 60_000
    members = [member_of(comm.identities[n], n) for n in ("alice", "bob", "carol")]
    body_params = {"slot_minutes": 15}
    from custom_components.cel_ca.core.registry import build_registry_body

    comm.serial += 1
    body = build_registry_body(comm.serial, members, (), body_params)
    payload = encode_envelope(comm.admin, CID, MsgType.REGISTRY, body, comm.clock())
    await comm.broker.route(None, f"cel/{CID}/registry", payload, True)
    feed_export(comm.nodes["alice"], comm.clock, export)
    await comm.tick()
    return comm


def next_slot(t: int) -> int:
    return (t // SLOT + 1) * SLOT


async def test_slot_params_reach_every_node() -> None:
    comm = await slot_community()
    for node in comm.nodes.values():
        assert node.slot_ms == SLOT and node.book.slot_ms == SLOT
        assert node.market_time() == next_slot(comm.clock())


async def test_live_offer_covers_exactly_the_next_slot() -> None:
    comm = await slot_community()
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    ns = next_slot(comm.clock())
    offers = alice.book.offer_book(comm.clock())
    assert [(o["start"], o["end"], o["power_w"], o["rolling"]) for o in offers] == [(ns, ns + SLOT, 3000, False)]
    assert bob.community_available_w() == 3000  # measured at the next slot
    assert bob.community_available_w(comm.clock()) == 0  # nothing tradable in the current slot
    # next slot begins: a new offer for the following one appears, the traded one stays
    comm.clock.t = ns + 60_000
    feed_export(alice, comm.clock, 3100)
    await alice.async_tick()
    starts = sorted(o["start"] for o in alice.book.offer_book(comm.clock()))
    assert starts == [ns, ns + SLOT]


async def test_reservation_is_always_the_next_whole_slot() -> None:
    comm = await slot_community(at_minute=14)  # 1 minute before the boundary
    bob = comm.nodes["bob"]
    ns = next_slot(comm.clock())
    res = await bob.reserve(2000, 3 * 3600_000)  # requested duration is ignored in slot mode
    assert res["status"] == "accepted"
    assert (res["start"], res["end"]) == (ns, ns + SLOT)
    # a start inside a future slot is moved to that slot's start... but only offered slots can be reserved
    res = await bob.reserve(500, SLOT, start=ns + SLOT + 60_000)
    assert res["reason"] == "no_offer"


async def test_producer_rejects_misaligned_or_started_slots() -> None:
    comm = await slot_community()
    alice = comm.nodes["alice"]
    book = alice.book
    oid = next(iter(book.offers))
    ns = next_slot(comm.clock())
    now = comm.clock()
    assert book.handle_request("hems-" + "x" * 24, "r1", oid, ns + 1, ns + SLOT + 1, 100, 100, now).reason == "not_slot_aligned"
    assert book.handle_request("hems-" + "x" * 24, "r2", oid, ns, ns + 2 * SLOT, 100, 100, now).reason == "not_slot_aligned"
    later = ns + 5 * 60_000
    assert book.handle_request("hems-" + "x" * 24, "r3", oid, ns, ns + SLOT, 100, 100, later).reason == "slot_started"
    assert book.handle_request("hems-" + "x" * 24, "r4", oid, ns, ns + SLOT, 100, 100, ns + 10_000).status == "accepted"


async def test_new_request_for_the_following_slot_is_the_renewal() -> None:
    comm = await slot_community()
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    first = await bob.reserve(2000, SLOT)
    comm.clock.t = first["start"] + 60_000  # we are now in the reserved slot
    feed_export(alice, comm.clock, 3100)
    await comm.tick()
    assert bob.ledger.reserved_power_at(comm.clock()) == 2000
    second = await bob.reserve(2000, SLOT)  # "renewing" = simply asking again
    assert second["status"] == "accepted" and second["start"] == first["end"]


async def test_current_slot_offer_only_decreases() -> None:
    comm = await slot_community()
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    res = await bob.reserve(2500, SLOT, min_power_w=500)
    comm.clock.t = res["start"] + 60_000
    feed_export(alice, comm.clock, 5100)  # more surplus now: the traded slot is not increased
    await alice.async_tick()
    cur = next(o for o in alice.book.offers.values() if o.start == res["start"])
    assert cur.power_w == 3000
    feed_export(alice, comm.clock, 1600)  # less surplus: the traded slot is reduced, bob curtailed
    await alice.async_tick()
    cur = next(o for o in alice.book.offers.values() if o.start == res["start"])
    assert cur.power_w == 1500
    assert bob.ledger.active[res["reservation_id"]].power_w == 1500


async def test_manual_offer_is_split_into_slots_and_withdrawn_as_a_whole() -> None:
    comm = await slot_community(export=0)
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    ns = next_slot(comm.clock())
    oid = await alice.publish_offer(4000, comm.clock(), ns + 3 * SLOT)
    parts = sorted((o.start, o.end) for o in alice.book.offers.values() if o.oid.startswith(oid))
    assert parts == [(ns + i * SLOT, ns + (i + 1) * SLOT) for i in range(3)]
    res = await bob.reserve(1000, SLOT, start=ns + SLOT)
    assert res["status"] == "accepted" and res["start"] == ns + SLOT
    assert await alice.withdraw_offer(oid)
    assert not alice.book.offers and res["reservation_id"] not in bob.ledger.active
    with pytest.raises(ValueError):
        await alice.publish_offer(1000, comm.clock(), comm.clock() + 60_000)  # no whole future slot


async def test_global_reservation_in_slot_mode() -> None:
    comm = await slot_community()
    carol = comm.nodes["carol"]
    feed_export(carol, comm.clock, 1100)
    await comm.tick()
    res = await comm.nodes["bob"].reserve_global(2000, 3600_000)
    ns = next_slot(comm.clock())
    assert res["status"] == "accepted" and (res["start"], res["end"]) == (ns, ns + SLOT)
    assert {r["producer_name"]: r["power_w"] for r in res["reservations"]} == {"alice": 1500, "carol": 500}


async def test_back_to_continuous_market() -> None:
    comm = await slot_community()
    alice, bob = comm.nodes["alice"], comm.nodes["bob"]
    await comm.publish_registry()  # new registry without params
    assert bob.slot_ms == 0 and alice.book.slot_ms == 0
    feed_export(alice, comm.clock, 3100)
    await alice.async_tick()
    res = await bob.reserve(1000, 600_000)
    assert res["status"] == "accepted" and res["end"] - res["start"] == 600_000


def test_slot_params_validation() -> None:
    from custom_components.cel_ca.core.messages import validate_body

    base = {"serial": 1, "members": [], "revoked": []}
    validate_body(MsgType.REGISTRY, {**base, "params": {"slot_minutes": 15}})
    validate_body(MsgType.REGISTRY, {**base, "params": {}})
    for bad in ({"slot_minutes": 7}, {"slot_minutes": True}, {"slot_minutes": "15"}, {"other": 1}, []):
        with pytest.raises(ProtocolError):
            validate_body(MsgType.REGISTRY, {**base, "params": bad})
