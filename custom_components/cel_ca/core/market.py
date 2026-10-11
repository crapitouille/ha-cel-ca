"""Offer / reservation bookkeeping.

* :class:`ProducerBook` - authoritative state of the offers published by the
  local HEMS and of the reservations granted on them.  The producer is the only
  arbiter of its own surplus, so no central server is needed.
* :class:`ConsumerLedger` - reservations the local HEMS obtained from peers.
* :class:`EnergyMeter` - integrates grid power samples for settlement.

All times are unix milliseconds, powers integer watts.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from .messages import MAX_OFFERS, MAX_RESERVATIONS

MIN_RESERVATION_MS = 60_000
SLOT_GRACE_MS = 30_000  # clock skew / latency tolerated when a slot has just started


def slot_start(t: int, slot_ms: int) -> int:
    """Start of the slot containing t."""
    return t // slot_ms * slot_ms


def next_slot_start(t: int, slot_ms: int) -> int:
    """Start of the first slot strictly after the one containing t."""
    return slot_start(t, slot_ms) + slot_ms
DEFAULT_MAX_RESERVATION_MS = 4 * 3600_000
MS_PER_HOUR = 3_600_000


@dataclass(slots=True)
class Offer:
    """Surplus offered by the local HEMS."""

    oid: str
    start: int
    end: int
    power_w: int
    live: bool = False  # automatic offer following the measured surplus

    def covers(self, t: int) -> bool:
        """True if t is within the offer window."""
        return self.start <= t < self.end


@dataclass(slots=True)
class Reservation:
    """A reservation, seen from either side."""

    rid: str
    oid: str
    producer: str
    consumer: str
    start: int
    end: int
    power_w: int
    min_power_w: int
    created: int
    status: str = "active"  # active | completed | cancelled
    group: str | None = None  # consumer side: id of a global (multi-producer) reservation

    def active_at(self, t: int) -> bool:
        """True if the reservation is active at time t."""
        return self.status == "active" and self.start <= t < self.end

    def energy_wh(self) -> int:
        """Reserved energy over the whole window."""
        return round(self.power_w * (self.end - self.start) / MS_PER_HOUR)

    def to_dict(self) -> dict[str, Any]:
        """JSON-able dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reservation:
        """Inverse of to_dict."""
        # Tolerant to states persisted by older versions (missing optional fields).
        return cls(**{k: data[k] for k in cls.__slots__ if k in data})  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class Decision:
    """Producer answer to a reservation request."""

    status: str  # accepted | partial | rejected
    power_w: int = 0
    start: int = 0
    end: int = 0
    reason: str = ""
    duplicate: bool = False


def max_concurrent(reservations: list[Reservation], start: int, end: int) -> int:
    """Maximum summed power of reservations over [start, end)."""
    if end <= start:
        return 0
    relevant = [r for r in reservations if r.status == "active" and r.start < end and r.end > start]
    if not relevant:
        return 0
    points = sorted({start} | {r.start for r in relevant if start < r.start < end})
    return max(sum(r.power_w for r in relevant if r.start <= p < r.end) for p in points)


def allocate_proportional(offers: list[dict[str, Any]], target_w: int, *, min_share_w: int = 50) -> list[tuple[dict[str, Any], int]]:
    """Split target_w across offers proportionally to their available power.

    Offers whose share would be below min_share_w are dropped (their share is
    redistributed). Shares are integers, never exceed an offer's availability
    and add up exactly to min(target_w, total available).
    """
    pool = [o for o in offers if o.get("available_w", 0) > 0]
    target = min(int(target_w), sum(o["available_w"] for o in pool))
    if target <= 0:
        return []
    while True:
        total = sum(o["available_w"] for o in pool)
        raw = [(o, target * o["available_w"] / total) for o in pool]
        small = [o for o, share in raw if share < min_share_w]
        if not small or len(pool) == 1:
            break
        pool.remove(min(small, key=lambda o: o["available_w"]))
        target = min(target, sum(o["available_w"] for o in pool))
    shares = [[o, min(int(share), o["available_w"])] for o, share in raw]
    remainder = target - sum(s for _, s in shares)
    for item in sorted(shares, key=lambda it: -it[0]["available_w"]):
        if remainder <= 0:
            break
        extra = min(remainder, item[0]["available_w"] - item[1])
        item[1] += extra
        remainder -= extra
    return [(o, share) for o, share in shares if share > 0]


class ProducerBook:
    """Offers of the local HEMS and reservations granted on them."""

    def __init__(
        self,
        self_id: str,
        *,
        max_reservation_ms: int = DEFAULT_MAX_RESERVATION_MS,
    ) -> None:
        self.self_id = self_id
        self.max_reservation_ms = max_reservation_ms
        self.slot_ms = 0  # community slot length (0 = continuous market)
        self.offers: dict[str, Offer] = {}
        self.reservations: dict[tuple[str, str], Reservation] = {}
        self.recent: deque[Reservation] = deque(maxlen=200)  # finished ones

    # ---------------------------------------------------------------- queries
    def _of_offer(self, oid: str) -> list[Reservation]:
        return [r for r in self.reservations.values() if r.oid == oid and r.status == "active"]

    def available_w(self, oid: str, now: int) -> int:
        """Power still available on an offer from now to its end."""
        offer = self.offers.get(oid)
        if offer is None or offer.end <= now:
            return 0
        start = max(now, offer.start)
        return max(0, offer.power_w - max_concurrent(self._of_offer(oid), start, offer.end))

    def reserved_power_at(self, t: int) -> int:
        """Total power reserved by peers at time t."""
        return sum(r.power_w for r in self.reservations.values() if r.active_at(t))

    def offered_power_at(self, t: int) -> int:
        """Total offered power at time t."""
        return sum(o.power_w for o in self.offers.values() if o.covers(t))

    def offer_book(self, now: int) -> list[dict[str, Any]]:
        """Public offer book."""
        return [
            {
                "oid": o.oid,
                "start": o.start,
                "end": o.end,
                "power_w": o.power_w,
                "available_w": self.available_w(o.oid, now),
                "rolling": o.live and not self.slot_ms,
            }
            for o in sorted(self.offers.values(), key=lambda o: (o.start, o.oid))
            if o.end > now
        ]

    def reservation_book(self, now: int) -> list[dict[str, Any]]:
        """Public reservation book."""
        return [
            {
                "rid": r.rid,
                "oid": r.oid,
                "consumer": r.consumer,
                "start": r.start,
                "end": r.end,
                "power_w": r.power_w,
            }
            for r in sorted(self.reservations.values(), key=lambda r: (r.start, r.rid))
            if r.status == "active" and r.end > now
        ][:MAX_RESERVATIONS]

    # ---------------------------------------------------------------- offers
    def set_offer(
        self, oid: str, start: int, end: int, power_w: int, now: int, *, live: bool = False
    ) -> list[Reservation]:
        """Create or update an offer. Returns reservations that were curtailed."""
        if end <= start or power_w < 0:
            raise ValueError("invalid offer")
        if oid not in self.offers and len(self.offers) >= MAX_OFFERS:
            raise ValueError("too many offers")
        self.offers[oid] = Offer(oid, start, end, power_w, live)
        return self._enforce(oid, now)

    def remove_offer(self, oid: str, now: int) -> list[Reservation]:
        """Withdraw an offer; future reservations on it are cancelled."""
        if self.offers.pop(oid, None) is None:
            return []
        changed = []
        for r in self._of_offer(oid):
            if r.end > now:
                r.power_w = 0
                r.end = max(r.start, min(r.end, now))
                r.status = "cancelled"
                changed.append(r)
        self._archive()
        return changed

    def _enforce(self, oid: str, now: int) -> list[Reservation]:
        """Make reservations fit the (possibly reduced) offer, newest first."""
        offer = self.offers[oid]
        changed: dict[str, Reservation] = {}
        res = sorted(self._of_offer(oid), key=lambda r: r.created, reverse=True)
        for r in res:
            if r.end <= now:
                continue
            new_end = r.end if offer.live else min(r.end, offer.end)
            if new_end - max(r.start, now) <= 0:
                r.power_w, r.status, r.end = 0, "cancelled", max(r.start, min(r.end, now))
                changed[r.rid] = r
                continue
            if new_end != r.end:
                r.end = new_end
                changed[r.rid] = r
        for r in res:
            if r.status != "active" or r.end <= now:
                continue
            window_start = max(r.start, now)
            excess = max_concurrent(self._of_offer(oid), window_start, r.end) - offer.power_w
            if excess > 0:
                r.power_w -= min(excess, r.power_w)
                if r.power_w < r.min_power_w or r.power_w <= 0:
                    r.power_w, r.status = 0, "cancelled"
                    r.end = max(r.start, min(r.end, now))
                changed[r.rid] = r
        self._archive()
        return list(changed.values())

    # ---------------------------------------------------------- reservations
    def handle_request(
        self,
        consumer: str,
        rid: str,
        oid: str,
        start: int,
        end: int,
        power_w: int,
        min_power_w: int,
        now: int,
    ) -> Decision:
        """Arbitrate a reservation request (first come, first served)."""
        existing = self.reservations.get((consumer, rid)) or next(
            (r for r in self.recent if r.consumer == consumer and r.rid == rid), None
        )
        if existing is not None:  # idempotent re-delivery (QoS 1)
            if existing.status == "active":
                return Decision("accepted", existing.power_w, existing.start, existing.end, "duplicate", True)
            return Decision("rejected", reason="duplicate", duplicate=True)
        if consumer == self.self_id:
            return Decision("rejected", reason="self")
        offer = self.offers.get(oid)
        if offer is None or offer.end <= now:
            return Decision("rejected", reason="unknown_offer")
        if len([r for r in self.reservations.values() if r.status == "active"]) >= MAX_RESERVATIONS:
            return Decision("rejected", reason="book_full")
        if self.slot_ms:
            return self._handle_slot_request(consumer, rid, oid, offer, start, end, power_w, min_power_w, now)
        g_start = max(start, now, offer.start)
        # A live offer has a rolling horizon: its end keeps moving while the
        # surplus lasts, so reservations are not clipped to it (they are
        # cancelled if the surplus disappears).
        offer_end = g_start + self.max_reservation_ms if offer.live else offer.end
        g_end = min(end, offer_end, g_start + self.max_reservation_ms)
        if g_end - g_start < MIN_RESERVATION_MS:
            return Decision("rejected", reason="outside_offer")
        avail = offer.power_w - max_concurrent(self._of_offer(oid), g_start, g_end)
        grant = min(power_w, avail)
        if grant < max(1, min_power_w):
            return Decision("rejected", reason="insufficient")
        self.reservations[(consumer, rid)] = Reservation(
            rid, oid, self.self_id, consumer, g_start, g_end, grant, min_power_w, now
        )
        # A reservation already started is not "partial" just because its
        # start was moved to now.
        full = grant == power_w and g_end == end and g_start <= max(start, now)
        return Decision("accepted" if full else "partial", grant, g_start, g_end)

    def _handle_slot_request(
        self, consumer: str, rid: str, oid: str, offer: Offer, start: int, end: int, power_w: int, min_power_w: int, now: int
    ) -> Decision:
        """Slot market: a reservation is exactly one whole, not yet started slot."""
        slot = self.slot_ms
        if start % slot or end - start != slot:
            return Decision("rejected", reason="not_slot_aligned")
        if start + SLOT_GRACE_MS < now:
            return Decision("rejected", reason="slot_started")
        if start < offer.start or end > offer.end:
            return Decision("rejected", reason="outside_offer")
        avail = offer.power_w - max_concurrent(self._of_offer(oid), start, end)
        grant = min(power_w, avail)
        if grant < max(1, min_power_w):
            return Decision("rejected", reason="insufficient")
        self.reservations[(consumer, rid)] = Reservation(rid, oid, self.self_id, consumer, start, end, grant, min_power_w, now)
        return Decision("accepted" if grant == power_w else "partial", grant, start, end)

    def cancel(self, consumer: str, rid: str, now: int) -> Reservation | None:
        """Cancel on behalf of the consumer."""
        r = self.reservations.get((consumer, rid))
        if r is None or r.status != "active":
            return None
        r.status = "cancelled"
        r.end = max(r.start, min(r.end, now))
        self._archive()
        return r

    def cancel_consumer(self, consumer: str, now: int) -> list[Reservation]:
        """Cancel all reservations of a consumer (e.g. revoked member)."""
        out = []
        for r in list(self.reservations.values()):
            if r.consumer == consumer and r.status == "active":
                r.status, r.power_w = "cancelled", 0
                r.end = max(r.start, min(r.end, now))
                out.append(r)
        self._archive()
        return out

    def expire(self, now: int) -> list[Reservation]:
        """Complete finished reservations and drop finished offers."""
        done = []
        for r in self.reservations.values():
            if r.status == "active" and r.end <= now:
                r.status = "completed"
                done.append(r)
        for oid in [o.oid for o in self.offers.values() if o.end <= now]:
            del self.offers[oid]
        self._archive()
        return done

    def _archive(self) -> None:
        for key in [k for k, r in self.reservations.items() if r.status != "active"]:
            self.recent.append(self.reservations.pop(key))

    def reserved_energy_wh(self, start: int, end: int) -> float:
        """Energy reserved by all reservations (incl. finished) over a window."""
        total = 0.0
        for r in list(self.reservations.values()) + list(self.recent):
            if r.status == "cancelled":
                continue
            overlap = min(end, r.end) - max(start, r.start)
            if overlap > 0:
                total += r.power_w * overlap / MS_PER_HOUR
        return total

    # ------------------------------------------------------------ persistence
    def export(self) -> dict[str, Any]:
        """Persistable state."""
        return {
            "offers": [asdict(o) for o in self.offers.values()],
            "reservations": [r.to_dict() for r in self.reservations.values()],
        }

    def restore(self, data: dict[str, Any], now: int) -> None:
        """Restore persisted state."""
        for o in data.get("offers", []):
            offer = Offer(**o)
            if offer.end > now and not offer.live:
                self.offers[offer.oid] = offer
        for d in data.get("reservations", []):
            r = Reservation.from_dict(d)
            self.reservations[(r.consumer, r.rid)] = r


@dataclass(slots=True)
class PendingRequest:
    """A request waiting for its producer's answer."""

    rid: str
    producer: str
    oid: str
    start: int
    end: int
    power_w: int
    min_power_w: int
    sent: int
    future: Any = field(default=None, repr=False)
    group: str | None = None


class ConsumerLedger:
    """Reservations obtained by the local HEMS."""

    def __init__(self, self_id: str) -> None:
        self.self_id = self_id
        self.pending: dict[str, PendingRequest] = {}
        self.active: dict[str, Reservation] = {}
        self.recent: deque[Reservation] = deque(maxlen=200)

    def reserved_power_at(self, t: int) -> int:
        """Power reserved from peers at time t."""
        return sum(r.power_w for r in self.active.values() if r.active_at(t))

    def reserved_from(self, producer: str, oid: str, t: int) -> int:
        """Power already reserved on a given offer at t."""
        return sum(
            r.power_w
            for r in self.active.values()
            if r.producer == producer and r.oid == oid and r.active_at(t)
        )

    def grant(self, pending: PendingRequest, body: dict[str, Any], now: int) -> Reservation:
        """Record an accepted/partial response."""
        r = Reservation(
            pending.rid,
            pending.oid,
            pending.producer,
            self.self_id,
            body["start"],
            body["end"],
            body["power_w"],
            pending.min_power_w,
            now,
            group=pending.group,
        )
        self.active[r.rid] = r
        return r

    def group_members(self, group: str) -> list[Reservation]:
        """Active reservations of a global reservation."""
        return [r for r in self.active.values() if r.group == group]

    def reserved_energy_wh(self, start: int, end: int) -> float:
        """Energy reserved by all our reservations (incl. finished) over a window."""
        total = 0.0
        for r in list(self.active.values()) + list(self.recent):
            if r.status == "cancelled":
                continue
            overlap = min(end, r.end) - max(start, r.start)
            if overlap > 0:
                total += r.power_w * overlap / MS_PER_HOUR
        return total

    def update(self, producer: str, body: dict[str, Any], now: int) -> Reservation | None:
        """Apply a producer update (curtailment / cancellation)."""
        r = self.active.get(body["rid"])
        if r is None or r.producer != producer:
            return None
        if body["status"] == "cancelled" or body["power_w"] <= 0:
            r.status = "cancelled"
            r.end = max(r.start, min(r.end, now))
        else:
            r.power_w = min(r.power_w, body["power_w"])
            r.end = max(r.start, min(r.end, body["end"]))
        self._archive()
        return r

    def cancel(self, rid: str, now: int) -> Reservation | None:
        """Cancel locally."""
        r = self.active.get(rid)
        if r is None:
            return None
        r.status = "cancelled"
        r.end = max(r.start, min(r.end, now))
        self._archive()
        return r

    def drop_producer(self, producer: str, now: int) -> list[Reservation]:
        """Forget reservations from a producer that left the CEL."""
        out = [r for r in self.active.values() if r.producer == producer]
        for r in out:
            r.status = "cancelled"
            r.end = max(r.start, min(r.end, now))
        self._archive()
        return out

    def expire(self, now: int) -> list[Reservation]:
        """Complete finished reservations."""
        done = [r for r in self.active.values() if r.end <= now]
        for r in done:
            r.status = "completed"
        self._archive()
        return done

    def _archive(self) -> None:
        for rid in [k for k, r in self.active.items() if r.status != "active"]:
            self.recent.append(self.active.pop(rid))

    def export(self) -> dict[str, Any]:
        """Persistable state."""
        return {"reservations": [r.to_dict() for r in self.active.values()]}

    def restore(self, data: dict[str, Any]) -> None:
        """Restore persisted state."""
        for d in data.get("reservations", []):
            r = Reservation.from_dict(d)
            self.active[r.rid] = r


class EnergyMeter:
    """Sample-and-hold integration of grid power (import positive, W)."""

    def __init__(self, retention_ms: int = 6 * MS_PER_HOUR, max_gap_ms: int = 5 * 60_000) -> None:
        self.retention_ms = retention_ms
        self.max_gap_ms = max_gap_ms  # a sample is not held longer than this
        self.samples: deque[tuple[int, int]] = deque()

    def add(self, t: int, import_w: int) -> None:
        """Add a sample (ignored if not newer than the last one)."""
        if self.samples and t <= self.samples[-1][0]:
            return
        self.samples.append((t, int(import_w)))
        while self.samples and self.samples[0][0] < t - self.retention_ms:
            self.samples.popleft()

    def _segments(self, start: int, end: int, now: int | None = None):
        items = list(self.samples)
        for i, (t, w) in enumerate(items):
            nxt = items[i + 1][0] if i + 1 < len(items) else (now if now is not None else end)
            seg_end = min(nxt, t + self.max_gap_ms)
            a, b = max(start, t), min(end, seg_end)
            if b > a:
                yield a, b, w

    def energy_wh(self, start: int, end: int, *, now: int | None = None) -> tuple[float, float]:
        """Return (import_wh, export_wh) over [start, end)."""
        imp = exp = 0.0
        for a, b, w in self._segments(start, end, now):
            e = w * (b - a) / MS_PER_HOUR
            if w >= 0:
                imp += e
            else:
                exp -= e
        return imp, exp

    def coverage_ms(self, start: int, end: int) -> int:
        """Duration of [start, end) covered by samples."""
        return sum(b - a for a, b, _ in self._segments(start, end, end))

    def average_export_w(self, start: int, end: int) -> int | None:
        """Average export power over a window (None without data)."""
        covered = 0
        exported = 0.0
        for a, b, w in self._segments(start, end, end):
            covered += b - a
            exported += max(0, -w) * (b - a)
        if covered == 0:
            # A single fresh sample (e.g. right after start-up) still counts.
            last = next((s for s in reversed(self.samples) if s[0] <= end), None)
            if last is not None and end - last[0] <= self.max_gap_ms:
                return max(0, -last[1])
            return None
        return int(exported / covered)
