"""Producer arbitration, curtailment, consumer ledger and energy metering."""

from __future__ import annotations

import pytest

from custom_components.cel_ca.core.market import (
    ConsumerLedger,
    EnergyMeter,
    PendingRequest,
    ProducerBook,
    Reservation,
    max_concurrent,
)

MIN = 60_000
P, A, B = "hems-" + "p" * 24, "hems-" + "a" * 24, "hems-" + "b" * 24


def book_with_offer(power=3000, start=0, end=60 * MIN) -> ProducerBook:
    book = ProducerBook(P)
    book.set_offer("o1", start, end, power, now=0)
    return book


def test_max_concurrent_sweep() -> None:
    rs = [
        Reservation("1", "o", P, A, 0, 10, 1000, 1, 0),
        Reservation("2", "o", P, B, 5, 15, 1500, 1, 0),
        Reservation("3", "o", P, B, 15, 20, 400, 1, 0),
    ]
    assert max_concurrent(rs, 0, 20) == 2500
    assert max_concurrent(rs, 10, 20) == 1500
    assert max_concurrent(rs, 15, 20) == 400
    assert max_concurrent(rs, 20, 30) == 0


def test_full_grant_then_partial_then_reject() -> None:
    book = book_with_offer(3000)
    d1 = book.handle_request(A, "r1", "o1", 0, 30 * MIN, 2000, 2000, now=0)
    assert (d1.status, d1.power_w) == ("accepted", 2000)
    d2 = book.handle_request(B, "r2", "o1", 0, 30 * MIN, 2000, 500, now=0)
    assert (d2.status, d2.power_w) == ("partial", 1000)
    d3 = book.handle_request(B, "r3", "o1", 0, 30 * MIN, 500, 500, now=0)
    assert (d3.status, d3.reason) == ("rejected", "insufficient")
    assert book.available_w("o1", 0) == 0
    # after the first 30 minutes, capacity is back
    assert book.available_w("o1", 30 * MIN) == 3000


def test_no_overbooking_with_staggered_windows() -> None:
    book = book_with_offer(2000)
    assert book.handle_request(A, "r1", "o1", 0, 20 * MIN, 1500, 1500, 0).status == "accepted"
    # overlaps r1 on [10,20) -> only 500 left there
    d = book.handle_request(B, "r2", "o1", 10 * MIN, 40 * MIN, 1000, 1000, 0)
    assert d.status == "rejected"
    d = book.handle_request(B, "r3", "o1", 20 * MIN, 40 * MIN, 1000, 1000, 0)
    assert d.status == "accepted"
    for t in range(0, 60 * MIN, MIN):
        assert book.reserved_power_at(t) <= 2000


def test_request_idempotent_on_redelivery() -> None:
    book = book_with_offer(3000)
    d1 = book.handle_request(A, "r1", "o1", 0, 10 * MIN, 1000, 1000, 0)
    d2 = book.handle_request(A, "r1", "o1", 0, 10 * MIN, 1000, 1000, 0)
    assert d2.duplicate and d2.power_w == d1.power_w
    assert len(book.reservations) == 1
    # same rid from another consumer is a different reservation
    assert not book.handle_request(B, "r1", "o1", 0, 10 * MIN, 1000, 1000, 0).duplicate


@pytest.mark.parametrize(
    ("consumer", "oid", "start", "end", "reason"),
    [
        (P, "o1", 0, 10 * MIN, "self"),
        (A, "nope", 0, 10 * MIN, "unknown_offer"),
        (A, "o1", 60 * MIN, 70 * MIN, "outside_offer"),
        (A, "o1", 0, 30_000, "outside_offer"),
    ],
)
def test_request_rejections(consumer, oid, start, end, reason) -> None:
    book = book_with_offer()
    assert book.handle_request(consumer, "r", oid, start, end, 100, 100, 0).reason == reason


def test_window_is_clipped_to_offer_and_marked_partial() -> None:
    book = book_with_offer(3000, 0, 20 * MIN)
    d = book.handle_request(A, "r", "o1", 10 * MIN, 40 * MIN, 1000, 1000, 0)
    assert (d.status, d.start, d.end) == ("partial", 10 * MIN, 20 * MIN)


def test_started_request_is_not_partial_just_because_of_now() -> None:
    book = book_with_offer()
    d = book.handle_request(A, "r", "o1", 1 * MIN, 10 * MIN, 1000, 1000, now=2 * MIN)
    assert (d.status, d.start) == ("accepted", 2 * MIN)


def test_curtailment_is_lifo_and_respects_min_power() -> None:
    book = book_with_offer(3000)
    book.handle_request(A, "r1", "o1", 0, 30 * MIN, 1500, 1500, now=0)
    book.handle_request(B, "r2", "o1", 0, 30 * MIN, 1500, 500, now=1)
    changed = book.set_offer("o1", 0, 60 * MIN, 2200, now=5 * MIN)
    assert [c.rid for c in changed] == ["r2"]
    assert book.reservations[(B, "r2")].power_w == 700
    assert book.reservations[(A, "r1")].power_w == 1500
    # below r2's min power -> r2 cancelled, r1 untouched
    changed = book.set_offer("o1", 0, 60 * MIN, 1600, now=6 * MIN)
    assert [c.status for c in changed] == ["cancelled"]
    assert book.reservations[(A, "r1")].power_w == 1500
    # below r1's min power (all-or-nothing request) -> cancelled too
    changed = book.set_offer("o1", 0, 60 * MIN, 1000, now=7 * MIN)
    assert changed[0].rid == "r1" and changed[0].status == "cancelled"
    assert book.available_w("o1", 7 * MIN) == 1000


def test_curtailment_keeps_reservation_above_its_min_power() -> None:
    book = book_with_offer(3000)
    book.handle_request(A, "r1", "o1", 0, 30 * MIN, 2000, 500, now=0)
    changed = book.set_offer("o1", 0, 60 * MIN, 800, now=MIN)
    assert changed[0].power_w == 800 and changed[0].status == "active"


def test_shortened_offer_truncates_reservations() -> None:
    book = book_with_offer()
    book.handle_request(A, "r1", "o1", 0, 30 * MIN, 1000, 1000, now=0)
    changed = book.set_offer("o1", 0, 10 * MIN, 3000, now=5 * MIN)
    assert changed[0].end == 10 * MIN


def test_withdraw_cancels_and_expire_completes() -> None:
    book = book_with_offer()
    book.handle_request(A, "r1", "o1", 0, 10 * MIN, 1000, 1000, now=0)
    assert book.expire(10 * MIN)[0].status == "completed"
    assert not book.reservations and book.recent[-1].rid == "r1"
    book = book_with_offer()
    book.handle_request(A, "r1", "o1", 0, 10 * MIN, 1000, 1000, now=0)
    changed = book.remove_offer("o1", now=MIN)
    assert changed[0].status == "cancelled" and changed[0].end == MIN
    assert "o1" not in book.offers
    # an already finished reservation cannot be re-requested
    assert book.handle_request(A, "r1", "o1", 0, 10 * MIN, 1, 1, MIN).reason == "duplicate"


def test_consumer_cancel_only_own() -> None:
    book = book_with_offer()
    book.handle_request(A, "r1", "o1", 0, 10 * MIN, 1000, 1000, now=0)
    assert book.cancel(B, "r1", MIN) is None
    assert book.cancel(A, "r1", MIN).status == "cancelled"
    assert book.available_w("o1", MIN) == 3000


def test_producer_book_persistence_roundtrip() -> None:
    book = book_with_offer()
    book.set_offer("live1", 0, 10 * MIN, 500, 0, live=True)
    book.handle_request(A, "r1", "o1", 0, 10 * MIN, 1000, 1000, now=0)
    restored = ProducerBook(P)
    restored.restore(book.export(), now=MIN)
    assert set(restored.offers) == {"o1"}  # live offers are rebuilt from measurements
    assert restored.reservations[(A, "r1")].power_w == 1000


def test_consumer_ledger_update_rules() -> None:
    ledger = ConsumerLedger(A)
    pending = PendingRequest("r1", P, "o1", 0, 10 * MIN, 1000, 500, 0)
    ledger.grant(pending, {"start": 0, "end": 10 * MIN, "power_w": 1000}, 0)
    assert ledger.reserved_power_at(MIN) == 1000
    assert ledger.update(B, {"rid": "r1", "status": "curtailed", "power_w": 1, "end": 1}, MIN) is None
    ledger.update(P, {"rid": "r1", "status": "curtailed", "power_w": 600, "end": 5 * MIN}, MIN)
    assert ledger.active["r1"].power_w == 600 and ledger.active["r1"].end == 5 * MIN
    # a producer cannot increase a reservation through an update
    ledger.update(P, {"rid": "r1", "status": "curtailed", "power_w": 5000, "end": 50 * MIN}, MIN)
    assert ledger.active["r1"].power_w == 600 and ledger.active["r1"].end == 5 * MIN
    ledger.update(P, {"rid": "r1", "status": "cancelled", "power_w": 0, "end": MIN}, 2 * MIN)
    assert "r1" not in ledger.active


def test_energy_meter_integration() -> None:
    m = EnergyMeter(max_gap_ms=60 * MIN)
    m.add(0, -2000)  # export 2 kW
    m.add(30 * MIN, 1000)  # import 1 kW
    m.add(60 * MIN, 1000)
    imp, exp = m.energy_wh(0, 60 * MIN)
    assert exp == pytest.approx(1000) and imp == pytest.approx(500)
    assert m.average_export_w(0, 30 * MIN) == 2000
    assert m.average_export_w(0, 60 * MIN) == 1000
    assert m.coverage_ms(0, 60 * MIN) == 60 * MIN


def test_energy_meter_gap_and_ordering() -> None:
    m = EnergyMeter(max_gap_ms=5 * MIN)
    m.add(0, -1000)
    m.add(0, -9999)  # not newer -> ignored
    m.add(60 * MIN, -1000)
    assert m.coverage_ms(0, 60 * MIN) == 5 * MIN  # the lonely sample is held 5 min max
    assert m.average_export_w(100 * MIN, 110 * MIN) is None  # stale data is not surplus
    assert EnergyMeter().average_export_w(0, 1) is None


# ------------------------------------------------------- proportional split
from custom_components.cel_ca.core.market import allocate_proportional  # noqa: E402


def _offers(*avail: int) -> list[dict]:
    return [{"producer": f"p{i}", "oid": f"o{i}", "available_w": a} for i, a in enumerate(avail)]


@pytest.mark.parametrize(
    ("avail", "target", "expected"),
    [
        ((3000, 1000), 2000, [1500, 500]),       # proportional 3:1
        ((3000, 1000), 4000, [3000, 1000]),      # everything available
        ((3000, 1000), 9000, [3000, 1000]),      # capped by availability
        ((1000, 1000, 1000), 1000, [334, 333, 333]),  # exact total, remainder to the largest
        ((5000, 20), 1000, [1000]),              # a share < 50 W is dropped and redistributed
        ((0, 800), 500, [500]),                  # empty offers ignored
    ],
)
def test_allocate_proportional(avail, target, expected) -> None:
    plan = allocate_proportional(_offers(*avail), target)
    shares = [s for _, s in plan]
    assert shares == expected
    assert sum(shares) == min(target, sum(avail))
    assert all(s <= o["available_w"] for o, s in plan)


def test_allocate_nothing() -> None:
    assert allocate_proportional([], 1000) == []
    assert allocate_proportional(_offers(0), 1000) == []


def test_consumer_ledger_group_and_energy() -> None:
    ledger = ConsumerLedger(A)
    for rid, producer, power in (("r1", P, 1000), ("r2", B, 3000)):
        pending = PendingRequest(rid, producer, "o", 0, 10 * MIN, power, 1, 0, None, "g1")
        ledger.grant(pending, {"start": 0, "end": 10 * MIN, "power_w": power}, 0)
    assert {r.rid for r in ledger.group_members("g1")} == {"r1", "r2"}
    assert ledger.reserved_energy_wh(0, 10 * MIN) == pytest.approx(4000 / 6)


def test_reservation_from_older_persisted_state() -> None:
    old = {"rid": "r", "oid": "o", "producer": P, "consumer": A, "start": 0, "end": 1, "power_w": 1, "min_power_w": 1, "created": 0, "status": "active"}
    assert Reservation.from_dict(old).group is None
