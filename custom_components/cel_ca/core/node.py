"""CEL node: the protocol engine run by every HEMS.

The node is transport- and framework-agnostic.  The host (Home Assistant) must:

* feed grid power samples with :meth:`CelNode.record_grid_power`,
* call :meth:`CelNode.async_tick` periodically (e.g. every 10 s),
* use :meth:`reserve`, :meth:`cancel_reservation`, :meth:`publish_offer`,
  :meth:`withdraw_offer` for user actions.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
import logging
import time
from typing import Any

from .identity import Identity, hems_id_from_public_key
from .market import (
    MS_PER_HOUR,
    ConsumerLedger,
    EnergyMeter,
    PendingRequest,
    ProducerBook,
    Reservation,
    allocate_proportional,
    next_slot_start,
)
from .messages import (
    COMMAND_TYPES,
    INBOX_TYPES,
    MAX_OFFERS,
    STATE_TYPES,
    Envelope,
    MsgType,
    ProtocolError,
    encode_envelope,
    new_token,
    parse_envelope,
)
from .registry import Registry
from .transport import Transport, TransportError

_LOGGER = logging.getLogger(__name__)

SW_VERSION = "0.6.2"
MAX_BUFFERED = 512


def topic_root(cid: str) -> str:
    """Root topic of a community."""
    return f"cel/{cid}"


def node_topic(cid: str, hems_id: str, kind: str) -> str:
    """Topic of a node sub-channel (status/offers/reservations/settlement/inbox)."""
    return f"cel/{cid}/node/{hems_id}/{kind}"


_KIND_TYPES: dict[str, frozenset[MsgType]] = {
    "status": frozenset({MsgType.STATUS}),
    "offers": frozenset({MsgType.OFFER_BOOK}),
    "reservations": frozenset({MsgType.RESERVATION_BOOK}),
    "settlement": frozenset({MsgType.SETTLEMENT}),
    "inbox": INBOX_TYPES,
}
_RETAINED_KINDS = frozenset({"status", "offers", "reservations"})


@dataclass(slots=True)
class NodeConfig:
    """Tunable behaviour of a node."""

    name: str = "HEMS"
    roles: frozenset[str] = frozenset({"producer", "consumer"})
    heartbeat_ms: int = 60_000
    offline_after_ms: int = 180_000
    max_skew_ms: int = 120_000
    request_timeout_s: float = 10.0
    inbox_rate_per_min: int = 30
    # automatic surplus offer
    auto_offer: bool = True
    offer_margin_w: int = 100
    min_offer_w: int = 500
    offer_horizon_ms: int = 15 * 60_000
    update_threshold_pct: int = 10
    smoothing_ms: int = 60_000
    stop_after_ticks: int = 3


@dataclass(slots=True)
class Peer:
    """What we know about another HEMS."""

    hems_id: str
    name: str
    roles: frozenset[str]
    meter: str | None = None
    online: bool = False
    last_seen: int = 0
    offers: list[dict[str, Any]] = field(default_factory=list)
    reservations: list[dict[str, Any]] = field(default_factory=list)
    ratios: deque[int] = field(default_factory=lambda: deque(maxlen=50))

    @property
    def reputation_pct(self) -> int | None:
        """Average self-reported compliance of the peer."""
        return round(sum(self.ratios) / len(self.ratios)) if self.ratios else None

    def offers_at(self, t: int) -> list[dict[str, Any]]:
        """Offers of the peer covering t."""
        return [o for o in self.offers if o["start"] <= t < o["end"]]


class CelNode:
    """Protocol engine of one HEMS."""

    def __init__(
        self,
        identity: Identity,
        community_id: str,
        admin_public_key: bytes,
        transport: Transport,
        config: NodeConfig | None = None,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.identity = identity
        self.id = identity.hems_id
        self.cid = community_id
        self.admin_pk = admin_public_key
        self.admin_id = hems_id_from_public_key(admin_public_key)
        self.transport = transport
        self.config = config or NodeConfig()
        self.clock = clock or (lambda: int(time.time() * 1000))

        self.registry = Registry()
        self.registry_payload: bytes | None = None
        self.peers: dict[str, Peer] = {}
        self.book = ProducerBook(self.id)
        self.ledger = ConsumerLedger(self.id)
        self.meter = EnergyMeter()
        self.own_ratios: deque[int] = deque(maxlen=50)
        self.stats: Counter[str] = Counter()
        self.on_event: Callable[[str, dict[str, Any]], None] | None = None

        self._listeners: list[Callable[[], None]] = []
        self._last_ts: dict[tuple[str, MsgType], int] = {}
        self._nonces: dict[str, int] = {}
        self._rate: dict[str, deque[int]] = {}
        self._buffer: dict[str, bytes] = {}
        self._last_heartbeat = 0
        self._last_sent_ts = 0
        self._low_ticks = 0
        self._started = False

        transport.on_message = self._on_message
        transport.on_connect = self._on_connect
        transport.on_state = lambda _connected: self._notify()

    # ================================================================ public
    @property
    def status_topic(self) -> str:
        """Own status topic (cleared when the node stops)."""
        return node_topic(self.cid, self.id, "status")

    @property
    def will(self) -> tuple[str, bytes]:
        """Last will: clear the retained status."""
        return (self.status_topic, b"")

    @property
    def connected(self) -> bool:
        """Transport connected."""
        return self.transport.connected

    @property
    def enrolled(self) -> bool:
        """This HEMS is an active member of the registry."""
        return self.registry.get_active(self.id, self.clock()) is not None

    @property
    def slot_ms(self) -> int:
        """Community market slot in ms, from the signed registry (0 = continuous)."""
        return self.registry.slot_ms

    def market_time(self, now: int | None = None) -> int:
        """Instant offers are traded for: the next slot in slot mode, now otherwise."""
        now = self.clock() if now is None else now
        return next_slot_start(now, self.slot_ms) if self.slot_ms else now

    def _slot_window(self, start: int, now: int) -> tuple[int, int]:
        """The whole slot a reservation starting at `start` falls in (never the current one)."""
        slot = self.slot_ms
        aligned = -(-max(start, now) // slot) * slot
        if aligned <= now:
            aligned += slot
        return aligned, aligned + slot

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a state-change callback. Returns an unsubscribe function."""
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    async def async_start(self) -> None:
        """Start the transport."""
        self._started = True
        await self.transport.start()

    async def async_stop(self) -> None:
        """Clear our presence and stop."""
        if self.transport.connected:
            with contextlib.suppress(TransportError):
                await self.transport.publish(self.status_topic, b"", retain=True)
        for pending in self.ledger.pending.values():
            if pending.future and not pending.future.done():
                pending.future.cancel()
        await self.transport.stop()
        self._started = False

    def record_grid_power(self, import_w: float, t: int | None = None) -> None:
        """Feed a grid power sample (W, import positive / export negative)."""
        self.meter.add(self.clock() if t is None else t, int(round(import_w)))

    def community_available_w(self, t: int | None = None) -> int:
        """Surplus available from online peers (for the next slot in slot mode)."""
        now = self.market_time() if t is None else t
        return sum(
            o["available_w"]
            for p in self.peers.values()
            if p.online and "producer" in p.roles
            for o in p.offers_at(now)
        )

    def best_offers(self, t: int | None = None) -> list[dict[str, Any]]:
        """Available peer offers at t (default: market time), best first."""
        now = self.market_time() if t is None else t
        out = []
        for p in self.peers.values():
            if not (p.online and "producer" in p.roles):
                continue
            for o in p.offers_at(now):
                if o["available_w"] > 0:
                    out.append(
                        {
                            "producer": p.hems_id,
                            "producer_name": p.name,
                            "reputation_pct": p.reputation_pct,
                            **o,
                        }
                    )
        out.sort(key=lambda o: (-o["available_w"], -(o["reputation_pct"] or 0), o["producer"]))
        return out

    @property
    def compliance_pct(self) -> int | None:
        """Own average settlement ratio."""
        return round(sum(self.own_ratios) / len(self.own_ratios)) if self.own_ratios else None

    # ------------------------------------------------------------ community
    def participants(self, t: int | None = None) -> list[dict[str, Any]]:
        """State of every active member of the CEL (including this HEMS), sorted by name.

        Only data already exchanged by the protocol is used: presence, offer
        books, reservation books and settlement reports.
        """
        now = self.clock() if t is None else t
        mt = self.market_time(now)  # offers: what can be reserved (next slot in slot mode)
        # Reservations active now, from every known producer book.
        books: dict[str, list[dict[str, Any]]] = {
            pid: p.reservations for pid, p in self.peers.items() if p.online
        }
        books[self.id] = self.book.reservation_book(now)
        reserved_in: dict[str, int] = {}
        reserved_out: dict[str, int] = {}
        for producer, reservations in books.items():
            for r in reservations:
                if r["start"] <= now < r["end"]:
                    reserved_out[producer] = reserved_out.get(producer, 0) + r["power_w"]
                    reserved_in[r["consumer"]] = reserved_in.get(r["consumer"], 0) + r["power_w"]

        rows: list[dict[str, Any]] = []
        for member in self.registry.members.values():
            if self.registry.get_active(member.hems_id, now) is None:
                continue
            is_self = member.hems_id == self.id
            if is_self:
                online = self.connected
                last_seen = now if online else None
                offers = [o for o in self.book.offer_book(now) if o["start"] <= mt < o["end"]]
                reputation = self.compliance_pct
            else:
                peer = self.peers.get(member.hems_id)
                online = bool(peer and peer.online)
                last_seen = peer.last_seen if peer and peer.last_seen else None
                offers = peer.offers_at(mt) if peer and online else []
                reputation = peer.reputation_pct if peer else None
            rows.append(
                {
                    "hems_id": member.hems_id,
                    "name": member.name,
                    "roles": sorted(member.roles),
                    "is_self": is_self,
                    "online": online,
                    "last_seen": last_seen,
                    "offered_w": sum(o["power_w"] for o in offers),
                    "available_w": sum(o["available_w"] for o in offers),
                    "reserved_out_w": reserved_out.get(member.hems_id, 0),
                    "reserved_in_w": reserved_in.get(member.hems_id, 0),
                    "reputation_pct": reputation,
                }
            )
        rows.sort(key=lambda r: (r["name"].casefold(), r["hems_id"]))
        return rows

    def community_totals(self, t: int | None = None, participants: list[dict[str, Any]] | None = None) -> dict[str, int]:
        """Aggregated view of the community."""
        rows = participants if participants is not None else self.participants(t)
        return {
            "members": len(rows),
            "online": sum(1 for r in rows if r["online"]),
            "offered_w": sum(r["offered_w"] for r in rows),
            "available_w": sum(r["available_w"] for r in rows),
            "reserved_w": sum(r["reserved_out_w"] for r in rows),
        }

    # ---------------------------------------------------------------- offers
    async def publish_offer(
        self, power_w: int, start: int | None, end: int, *, oid: str | None = None
    ) -> str:
        """Publish (or update) a manual offer. Returns its id."""
        if "producer" not in self.config.roles:
            raise ValueError("this HEMS is not a producer")
        now = self.clock()
        start = now if start is None else start
        if end <= max(start, now) or power_w < 0:
            raise ValueError("invalid offer window or power")
        oid = oid or new_token("m")
        existing = self.book.offers.get(oid)
        if existing is not None and existing.live:
            raise ValueError("cannot override the live offer")
        if self.slot_ms:
            return await self._publish_slot_offers(oid, int(power_w), start, end, now)
        changed = self.book.set_offer(oid, start, end, int(power_w), now)
        await self._notify_changes(changed, "offer_reduced")
        await self._publish_books()
        self._notify()
        return oid

    async def _publish_slot_offers(self, base: str, power_w: int, start: int, end: int, now: int) -> str:
        """Slot mode: one offer per whole future slot of [start, end), ids <base>-<n>."""
        slot = self.slot_ms
        first = max(-(-start // slot) * slot, next_slot_start(now, slot))
        slots = list(range(first, -(-end // slot) * slot, slot))
        if not slots:
            raise ValueError("the window contains no future slot")
        own_parts = [o for o in self.book.offers if o.startswith(f"{base}-")]
        if len(self.book.offers) - len(own_parts) + len(slots) > MAX_OFFERS:
            raise ValueError("too many offers")
        changed: list[Reservation] = []
        wanted = {f"{base}-{i}" for i in range(len(slots))}
        for part in own_parts:
            if part not in wanted:
                changed += self.book.remove_offer(part, now)
        for i, slot_begin in enumerate(slots):
            changed += self.book.set_offer(f"{base}-{i}", slot_begin, slot_begin + slot, power_w, now)
        await self._notify_changes(changed, "offer_reduced")
        await self._publish_books()
        self._notify()
        return base

    async def withdraw_offer(self, oid: str) -> bool:
        """Withdraw an offer (reservations on it are cancelled); slot offers by their base id."""
        parts = [oid] if oid in self.book.offers else [o for o in self.book.offers if o.startswith(f"{oid}-")]
        if not parts:
            return False
        now = self.clock()
        changed = []
        for part in parts:
            changed += self.book.remove_offer(part, now)
        await self._notify_changes(changed, "offer_withdrawn")
        await self._publish_books()
        self._notify()
        return True

    # ---------------------------------------------------------- reservations
    async def reserve(
        self,
        power_w: int,
        duration_ms: int,
        *,
        min_power_w: int | None = None,
        producer: str | None = None,
        oid: str | None = None,
        start: int | None = None,
    ) -> dict[str, Any]:
        """Reserve surplus from a peer and wait for its answer.

        In slot mode the reservation is always exactly the next whole slot; to
        continue afterwards, the HEMS simply makes a new request.
        """
        now = self.clock()
        start = now if start is None else max(start, now)
        end = start + int(duration_ms)
        if self.slot_ms:  # always exactly the next whole slot
            start, end = self._slot_window(start, now)
        power_w = int(power_w)
        min_power = min(int(min_power_w or power_w), power_w)
        result: dict[str, Any] = {
            "status": "rejected",
            "reservation_id": None,
            "producer": producer,
            "offer_id": oid,
            "power_w": 0,
            "start": start,
            "end": end,
            "reason": "",
        }
        if "consumer" not in self.config.roles:
            return {**result, "reason": "not_consumer"}
        if power_w <= 0 or duration_ms <= 0:
            return {**result, "reason": "invalid_request"}
        if not self.connected:
            return {**result, "status": "error", "reason": "not_connected"}
        if not self.enrolled:
            return {**result, "reason": "not_enrolled"}

        candidates = [
            o
            for o in self.best_offers(start)
            if o["available_w"] >= min_power
            and (producer is None or o["producer"] == producer)
            and (oid is None or o["oid"] == oid)
        ]
        if not candidates:
            return {**result, "reason": "no_offer"}
        return await self._request(candidates[0], start, end, power_w, min_power, result)

    async def _request(
        self,
        target: dict[str, Any],
        start: int,
        end: int,
        power_w: int,
        min_power: int,
        base: dict[str, Any],
        *,
        group: str | None = None,
    ) -> dict[str, Any]:
        """Send one reservation request to a producer and wait for its answer."""
        rid = new_token("r")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        pending = PendingRequest(
            rid, target["producer"], target["oid"], start, end, power_w, min_power, self.clock(), future, group
        )
        self.ledger.pending[rid] = pending
        result = {
            **base,
            "reservation_id": rid,
            "producer": target["producer"],
            "producer_name": target["producer_name"],
            "offer_id": target["oid"],
        }
        body = {
            "rid": rid,
            "to": target["producer"],
            "oid": target["oid"],
            "start": start,
            "end": end,
            "power_w": power_w,
            "min_power_w": min_power,
        }
        if not await self._send(MsgType.RESERVE_REQUEST, body, node_topic(self.cid, target["producer"], "inbox")):
            self.ledger.pending.pop(rid, None)
            return {**result, "status": "error", "reason": "not_connected"}
        try:
            answer = await asyncio.wait_for(asyncio.shield(future), self.config.request_timeout_s)
        except TimeoutError:
            self.ledger.pending.pop(rid, None)
            self._emit("timeout", {**result, "status": "timeout", "reason": "no_response"})
            return {**result, "status": "timeout", "reason": "no_response"}
        return {**result, **answer}

    async def reserve_global(
        self,
        power_w: int,
        duration_ms: int,
        *,
        min_power_w: int | None = None,
        start: int | None = None,
        max_rounds: int = 2,
    ) -> dict[str, Any]:
        """Reserve a total power spread proportionally over several producers.

        All or nothing: if less than min_power_w (default: power_w) can be
        obtained, every reservation already granted is cancelled.
        """
        now = self.clock()
        start = now if start is None else max(start, now)
        end = start + int(duration_ms)
        if self.slot_ms:
            start, end = self._slot_window(start, now)
        power_w = int(power_w)
        min_power = min(int(min_power_w or power_w), power_w)
        group = new_token("g")
        result: dict[str, Any] = {
            "status": "rejected",
            "group_id": None,
            "requested_w": power_w,
            "min_power_w": min_power,
            "power_w": 0,
            "start": start,
            "end": end,
            "reservations": [],
            "reason": "",
        }
        if "consumer" not in self.config.roles:
            return {**result, "reason": "not_consumer"}
        if power_w <= 0 or duration_ms <= 0:
            return {**result, "reason": "invalid_request"}
        if not self.connected:
            return {**result, "status": "error", "reason": "not_connected"}
        if not self.enrolled:
            return {**result, "reason": "not_enrolled"}
        offers = self.best_offers(start)
        if sum(o["available_w"] for o in offers) < min_power:
            return {**result, "reason": "no_offer" if not offers else "insufficient"}

        granted: list[dict[str, Any]] = []
        failures: list[str] = []
        failed_producers: set[str] = set()
        for _round in range(max_rounds):
            missing = power_w - sum(g["power_w"] for g in granted)
            if missing <= 0:
                break
            # Producers that timed out or refused are not asked again in the top-up round.
            candidates = [o for o in self.best_offers(start) if o["producer"] not in failed_producers]
            plan = allocate_proportional(candidates, missing)
            if not plan:
                break
            answers = await asyncio.gather(
                *(self._request(offer, start, end, share, 1, {}, group=group) for offer, share in plan)
            )
            for answer in answers:
                if answer.get("status") in ("accepted", "partial") and answer.get("power_w", 0) > 0:
                    granted.append(answer)
                else:
                    failures.append(answer.get("reason") or answer.get("status", "rejected"))
                    if answer.get("producer"):
                        failed_producers.add(answer["producer"])

        total = sum(g["power_w"] for g in granted)
        if total < min_power:
            for g in granted:
                await self.cancel_reservation(g["reservation_id"])
            reason = "insufficient" if granted or not failures else failures[0]
            self._emit("group_rejected", {"role": "consumer", "group_id": group, "requested_w": power_w, "obtained_w": total, "reason": reason})
            return {**result, "reason": reason}

        reservations = [
            {
                "reservation_id": g["reservation_id"],
                "producer": g["producer"],
                "producer_name": g["producer_name"],
                "offer_id": g["offer_id"],
                "power_w": g["power_w"],
                "start": g["start"],
                "end": g["end"],
            }
            for g in granted
        ]
        status = "accepted" if total >= power_w else "partial"
        self._emit("group_granted", {"role": "consumer", "group_id": group, "requested_w": power_w, "power_w": total, "producers": len(reservations)})
        return {
            **result,
            "status": status,
            "group_id": group,
            "power_w": total,
            "start": min(g["start"] for g in granted),
            "end": max(g["end"] for g in granted),
            "reservations": reservations,
        }

    async def cancel_reservation(self, rid: str) -> bool:
        """Cancel one of our reservations, or every reservation of a group."""
        members = self.ledger.group_members(rid)
        if members:
            for member in members:
                await self.cancel_reservation(member.rid)
            return True
        r = self.ledger.cancel(rid, self.clock())
        if r is None:
            return False
        await self._send(
            MsgType.RESERVE_CANCEL,
            {"rid": rid, "to": r.producer},
            node_topic(self.cid, r.producer, "inbox"),
        )
        self._emit("cancelled", self._res_event(r, "consumer", "by_consumer"))
        self._notify()
        return True

    # ------------------------------------------------------------------ tick
    async def async_tick(self) -> None:
        """Periodic housekeeping; call every few seconds."""
        now = self.clock()
        books_dirty = False

        for r in self.book.expire(now):
            books_dirty = True
            self._emit("completed", self._res_event(r, "producer"))
            await self._settle(r, "producer", now)
        for r in self.ledger.expire(now):
            self._emit("completed", self._res_event(r, "consumer"))
            await self._settle(r, "consumer", now)

        books_dirty |= await self._update_live_offer(now)

        for peer in self.peers.values():
            if peer.online and now - peer.last_seen > self.config.offline_after_ms:
                peer.online = False
        cutoff = now - 2 * self.config.max_skew_ms
        self._nonces = {n: t for n, t in self._nonces.items() if t >= cutoff}

        if now - self._last_heartbeat >= self.config.heartbeat_ms:
            await self._publish_status()
            books_dirty = True
        if books_dirty:
            await self._publish_books()
        self._notify()

    async def _update_live_offer(self, now: int) -> bool:
        live = next((o for o in self.book.offers.values() if o.live), None)
        cfg = self.config
        if not cfg.auto_offer or "producer" not in cfg.roles:
            removed = False
            for offer in [o for o in self.book.offers.values() if o.live]:
                changed = self.book.remove_offer(offer.oid, now)
                await self._notify_changes(changed, "surplus_ended")
                removed = True
            return removed
        avg = self.meter.average_export_w(now - cfg.smoothing_ms, now) or 0
        power = max(0, avg - cfg.offer_margin_w) // 10 * 10
        if self.slot_ms:
            return await self._update_live_slot_offers(now, power)
        # Continuous market: drop live offers left by slot mode for future slots.
        for offer in [o for o in self.book.offers.values() if o.live and o.start > now]:
            changed = self.book.remove_offer(offer.oid, now)
            await self._notify_changes(changed, "surplus_ended")
        live = next((o for o in self.book.offers.values() if o.live), None)
        if power >= cfg.min_offer_w:
            self._low_ticks = 0
            end = now + cfg.offer_horizon_ms
            if live is None:
                self.book.set_offer(new_token("live"), now, end, power, now, live=True)
                return True
            threshold = live.power_w * cfg.update_threshold_pct / 100
            new_power = power if abs(power - live.power_w) > threshold else live.power_w
            changed = self.book.set_offer(live.oid, live.start, end, new_power, now, live=True)
            await self._notify_changes(changed, "surplus_reduced")
            return new_power != live.power_w or bool(changed)
        if live is None:
            return False
        self._low_ticks += 1
        if self._low_ticks < cfg.stop_after_ticks:
            return False
        self._low_ticks = 0
        changed = self.book.remove_offer(live.oid, now)
        await self._notify_changes(changed, "surplus_ended")
        return True

    async def _update_live_slot_offers(self, now: int, power: int) -> bool:
        """Slot mode: offer the measured surplus for the next slot only.

        The offer of the current slot (already traded) is only ever reduced,
        never increased, when the measured surplus drops.
        """
        cfg, slot = self.config, self.slot_ms
        ns = next_slot_start(now, slot)
        cs = ns - slot
        dirty = False
        live = {o.start: o for o in self.book.offers.values() if o.live}
        for start_, offer in list(live.items()):
            if start_ not in (cs, ns) or offer.end - offer.start != slot:  # leftovers (e.g. continuous mode)
                changed = self.book.remove_offer(offer.oid, now)
                await self._notify_changes(changed, "surplus_ended")
                del live[start_]
                dirty = True
        nxt, cur = live.get(ns), live.get(cs)
        if power >= cfg.min_offer_w:
            self._low_ticks = 0
            if nxt is None:
                self.book.set_offer(f"s{ns // 60_000}", ns, ns + slot, power, now, live=True)
                dirty = True
            elif abs(power - nxt.power_w) > nxt.power_w * cfg.update_threshold_pct / 100:
                changed = self.book.set_offer(nxt.oid, ns, ns + slot, power, now, live=True)
                await self._notify_changes(changed, "surplus_reduced")
                dirty = True
        elif nxt is not None:
            self._low_ticks += 1
            if self._low_ticks >= cfg.stop_after_ticks:
                self._low_ticks = 0
                changed = self.book.remove_offer(nxt.oid, now)
                await self._notify_changes(changed, "surplus_ended")
                dirty = True
        if cur is not None and power < cur.power_w * (1 - cfg.update_threshold_pct / 100):
            if power <= 0:
                changed = self.book.remove_offer(cur.oid, now)
            else:
                changed = self.book.set_offer(cur.oid, cs, cs + slot, power, now, live=True)
            await self._notify_changes(changed, "surplus_reduced")
            dirty = True
        return dirty

    # ============================================================ publishing
    async def _send(self, typ: MsgType, body: dict[str, Any], topic: str, *, retain: bool = False) -> bool:
        try:
            payload = encode_envelope(self.identity, self.cid, typ, body, self._next_ts())
            await self.transport.publish(topic, payload, retain=retain)
        except TransportError as err:
            _LOGGER.debug("Cannot publish %s: %s", typ, err)
            return False
        return True

    def _next_ts(self) -> int:
        """Strictly increasing timestamp, so that peers never see two state
        messages from us with the same ts (they would drop the second as stale)."""
        self._last_sent_ts = max(self.clock(), self._last_sent_ts + 1)
        return self._last_sent_ts

    async def _publish_status(self) -> None:
        self._last_heartbeat = self.clock()
        await self._send(
            MsgType.STATUS,
            {"name": self.config.name[:64], "roles": sorted(self.config.roles), "sw": SW_VERSION},
            self.status_topic,
            retain=True,
        )

    async def _publish_books(self) -> None:
        now = self.clock()
        if "producer" not in self.config.roles and not self.book.offers:
            return
        await self._send(
            MsgType.OFFER_BOOK,
            {"offers": self.book.offer_book(now)},
            node_topic(self.cid, self.id, "offers"),
            retain=True,
        )
        await self._send(
            MsgType.RESERVATION_BOOK,
            {"reservations": self.book.reservation_book(now)},
            node_topic(self.cid, self.id, "reservations"),
            retain=True,
        )

    async def _notify_changes(self, changed: list[Reservation], reason: str) -> None:
        for r in changed:
            status = "cancelled" if r.status == "cancelled" else "curtailed"
            await self._send(
                MsgType.RESERVATION_UPDATE,
                {"rid": r.rid, "to": r.consumer, "status": status, "power_w": r.power_w, "end": r.end, "reason": reason},
                node_topic(self.cid, r.consumer, "inbox"),
            )
            self._emit(status, self._res_event(r, "producer", reason))

    async def _settle(self, r: Reservation, role: str, now: int) -> None:
        """Measure how well a finished reservation was honoured and publish it."""
        duration = r.end - r.start
        if duration <= 0 or r.power_w <= 0:
            return
        if self.meter.coverage_ms(r.start, r.end) < duration / 2:
            self.stats["settlement_no_data"] += 1
            return
        imported, exported = self.meter.energy_wh(r.start, r.end, now=now)
        reserved = r.power_w * duration / MS_PER_HOUR
        if role == "producer":
            total = self.book.reserved_energy_wh(r.start, r.end) or reserved
            ratio = min(1.0, exported / total)
            measured = min(exported, total) * reserved / total
            counterpart = r.consumer
        else:
            # Our import is shared among all our reservations overlapping this window.
            total = self.ledger.reserved_energy_wh(r.start, r.end) or reserved
            ratio = min(1.0, imported / total)
            measured = min(imported, total) * reserved / total
            counterpart = r.producer
        ratio_pct = round(ratio * 100)
        self.own_ratios.append(ratio_pct)
        body = {
            "rid": r.rid,
            "role": role,
            "counterpart": counterpart,
            "reserved_wh": round(reserved),
            "measured_wh": round(measured),
            "ratio_pct": ratio_pct,
        }
        await self._send(MsgType.SETTLEMENT, body, node_topic(self.cid, self.id, "settlement"))
        self._emit("settled", body)

    async def _on_connect(self) -> None:
        root = topic_root(self.cid)
        for kind in ("status", "offers", "reservations", "settlement"):
            await self.transport.subscribe(f"{root}/node/+/{kind}")
        await self.transport.subscribe(node_topic(self.cid, self.id, "inbox"))
        await self.transport.subscribe(f"{root}/registry")
        await self._publish_status()
        await self._publish_books()
        self._notify()

    # ============================================================= receiving
    def _parse_topic(self, topic: str) -> tuple[str, str | None] | None:
        parts = topic.split("/")
        if len(parts) == 3 and parts[:2] == ["cel", self.cid] and parts[2] == "registry":
            return "registry", None
        if len(parts) == 5 and parts[:2] == ["cel", self.cid] and parts[2] == "node" and parts[4] in _KIND_TYPES:
            return parts[4], parts[3]
        return None

    def _drop(self, reason: str, topic: str) -> None:
        self.stats[reason] += 1
        _LOGGER.debug("Dropped message on %s: %s", topic, reason)

    async def _on_message(self, topic: str, payload: bytes) -> None:
        parsed = self._parse_topic(topic)
        if parsed is None:
            self._drop("bad_topic", topic)
            return
        kind, node_id = parsed
        now = self.clock()

        if not payload:
            if kind == "status" and node_id in self.peers:
                self.peers[node_id].online = False
                self.peers[node_id].offers = []
                self._notify()
            return

        try:
            env = parse_envelope(payload)
        except ProtocolError as err:
            self._drop(f"invalid:{err}", topic)
            return
        if env.cid != self.cid:
            self._drop("wrong_community", topic)
            return
        if kind == "registry":
            await self._handle_registry(env, payload, now)
            return
        if env.src == self.id:
            return  # our own retained state echoed back
        if env.typ not in _KIND_TYPES[kind]:
            self._drop("type_topic_mismatch", topic)
            return
        member = self.registry.get_active(env.src, now)
        if member is None:
            if kind in _RETAINED_KINDS and (len(self._buffer) < MAX_BUFFERED or topic in self._buffer):
                self._buffer[topic] = payload
            self._drop("unknown_sender", topic)
            return
        if not env.verify(member.public_key):
            self._drop("bad_signature", topic)
            return
        if env.ts > now + self.config.max_skew_ms:
            self._drop("future", topic)
            return
        if kind == "inbox":
            if node_id != self.id or env.body.get("to") != self.id:
                self._drop("not_for_me", topic)
                return
            if not self._rate_ok(env.src, now):
                self._drop("rate_limited", topic)
                return
        elif node_id != env.src:
            self._drop("topic_src_mismatch", topic)
            return

        if env.typ in STATE_TYPES:
            key = (env.src, env.typ)
            if env.ts <= self._last_ts.get(key, -1):
                self._drop("stale", topic)
                return
            self._last_ts[key] = env.ts
        elif env.typ in COMMAND_TYPES:
            if abs(now - env.ts) > self.config.max_skew_ms:
                self._drop("expired", topic)
                return
            nonce_key = f"{env.src}:{env.nonce}"
            if nonce_key in self._nonces:
                self._drop("replay", topic)
                return
            self._nonces[nonce_key] = env.ts

        peer = self._peer(member)
        await self._dispatch(env, peer, now)
        self._notify()

    def _rate_ok(self, src: str, now: int) -> bool:
        window = self._rate.setdefault(src, deque())
        while window and window[0] < now - 60_000:
            window.popleft()
        if len(window) >= self.config.inbox_rate_per_min:
            return False
        window.append(now)
        return True

    def _peer(self, member) -> Peer:
        peer = self.peers.get(member.hems_id)
        if peer is None:
            peer = Peer(member.hems_id, member.name, member.roles, member.meter)
            self.peers[member.hems_id] = peer
        else:  # the registry is authoritative for name and roles
            peer.name, peer.roles, peer.meter = member.name, member.roles, member.meter
        return peer

    async def _dispatch(self, env: Envelope, peer: Peer, now: int) -> None:
        body = env.body
        match env.typ:
            case MsgType.STATUS:
                peer.online = now - env.ts <= self.config.offline_after_ms
                peer.last_seen = env.ts
            case MsgType.OFFER_BOOK:
                peer.offers = body["offers"] if "producer" in peer.roles else []
            case MsgType.RESERVATION_BOOK:
                peer.reservations = body["reservations"]
            case MsgType.SETTLEMENT:
                peer.ratios.append(body["ratio_pct"])
            case MsgType.RESERVE_REQUEST:
                await self._handle_request(env, peer, now)
            case MsgType.RESERVE_RESPONSE:
                await self._handle_response(env, peer, now)
            case MsgType.RESERVE_CANCEL:
                r = self.book.cancel(env.src, body["rid"], now)
                if r is not None:
                    self._emit("cancelled", self._res_event(r, "producer", "by_consumer"))
                    await self._publish_books()
            case MsgType.RESERVATION_UPDATE:
                r = self.ledger.update(env.src, body, now)
                if r is not None:
                    kind = "cancelled" if r.status == "cancelled" else "curtailed"
                    self._emit(kind, self._res_event(r, "consumer", body["reason"]))
                    if r.group:
                        remaining = self.ledger.group_members(r.group)
                        self._emit(
                            "group_updated",
                            {
                                "role": "consumer",
                                "group_id": r.group,
                                "power_w": sum(m.power_w for m in remaining if m.active_at(now)),
                                "reservations": len(remaining),
                                "reason": body["reason"],
                            },
                        )

    async def _handle_request(self, env: Envelope, peer: Peer, now: int) -> None:
        b = env.body
        if "producer" not in self.config.roles:
            decision_status, power, start, end, reason, dup = "rejected", 0, 0, 0, "not_producer", False
        elif "consumer" not in peer.roles:
            decision_status, power, start, end, reason, dup = "rejected", 0, 0, 0, "not_consumer", False
        else:
            d = self.book.handle_request(
                env.src, b["rid"], b["oid"], b["start"], b["end"], b["power_w"], b["min_power_w"], now
            )
            decision_status, power, start, end, reason, dup = d.status, d.power_w, d.start, d.end, d.reason, d.duplicate
        await self._send(
            MsgType.RESERVE_RESPONSE,
            {"rid": b["rid"], "to": env.src, "status": decision_status, "power_w": power, "start": start, "end": end, "reason": reason},
            node_topic(self.cid, env.src, "inbox"),
        )
        if dup:
            return
        event = {
            "role": "producer",
            "reservation_id": b["rid"],
            "offer_id": b["oid"],
            "consumer": env.src,
            "consumer_name": peer.name,
            "requested_w": b["power_w"],
            "power_w": power,
            "start": start,
            "end": end,
            "status": decision_status,
            "reason": reason,
        }
        self._emit("request_received", event)
        if decision_status != "rejected":
            await self._publish_books()

    async def _handle_response(self, env: Envelope, peer: Peer, now: int) -> None:
        b = env.body
        pending = self.ledger.pending.get(b["rid"])
        if pending is None or pending.producer != env.src:
            if b["status"] != "rejected" and b["rid"] not in self.ledger.active:
                # Late answer after a timeout: release the producer's capacity.
                await self._send(
                    MsgType.RESERVE_CANCEL,
                    {"rid": b["rid"], "to": env.src},
                    node_topic(self.cid, env.src, "inbox"),
                )
                self.stats["late_response_cancelled"] += 1
            return
        del self.ledger.pending[b["rid"]]
        answer = {"status": b["status"], "power_w": b["power_w"], "reason": b["reason"]}
        if b["status"] != "rejected":
            r = self.ledger.grant(pending, b, now)
            answer.update(start=r.start, end=r.end)
            self._emit("granted", self._res_event(r, "consumer", b["status"]))
        else:
            self._emit(
                "rejected",
                {"role": "consumer", "reservation_id": b["rid"], "producer": env.src, "producer_name": peer.name, "reason": b["reason"]},
            )
        if pending.future is not None and not pending.future.done():
            pending.future.set_result(answer)

    async def _handle_registry(self, env: Envelope, payload: bytes, now: int) -> None:
        if env.src != self.admin_id or not env.verify(self.admin_pk):
            self._drop("registry_bad_signature", "registry")
            return
        if env.typ is not MsgType.REGISTRY:
            self._drop("registry_bad_type", "registry")
            return
        if env.ts > now + self.config.max_skew_ms:
            self._drop("registry_future", "registry")
            return
        if env.body["serial"] <= self.registry.serial:
            if env.body["serial"] < self.registry.serial:
                self._drop("registry_rollback", "registry")
            return
        try:
            registry = Registry.from_body(env.body)
        except ProtocolError as err:
            self._drop(f"registry_invalid:{err}", "registry")
            return
        self._apply_registry(registry, payload, now)
        await self._publish_books()
        # Messages received before we knew their sender can now be checked.
        buffered, self._buffer = self._buffer, {}
        for topic, data in buffered.items():
            await self._on_message(topic, data)
        self._emit("registry_updated", {"serial": registry.serial, "members": len(registry.members), "enrolled": self.enrolled})
        self._notify()

    def _apply_registry(self, registry: Registry, payload: bytes, now: int) -> None:
        self.registry = registry
        self.registry_payload = payload
        self.book.slot_ms = registry.slot_ms
        for pid in list(self.peers):
            member = registry.get_active(pid, now)
            if member is None:
                del self.peers[pid]
                self.book.cancel_consumer(pid, now)
                self.ledger.drop_producer(pid, now)
            else:
                self._peer(member)

    # ================================================================= utils
    def _res_event(self, r: Reservation, role: str, reason: str = "") -> dict[str, Any]:
        counterpart = r.consumer if role == "producer" else r.producer
        peer = self.peers.get(counterpart)
        return {
            "role": role,
            "reservation_id": r.rid,
            "offer_id": r.oid,
            "producer": r.producer,
            "consumer": r.consumer,
            "counterpart_name": peer.name if peer else counterpart,
            "power_w": r.power_w,
            "start": r.start,
            "end": r.end,
            "status": r.status,
            "reason": reason,
            "group_id": r.group,
        }

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, data)
            except Exception:  # pragma: no cover - host callback bug
                _LOGGER.exception("Event callback failed")

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # pragma: no cover - host callback bug
                _LOGGER.exception("Listener failed")

    # =========================================================== persistence
    def export_state(self) -> dict[str, Any]:
        """JSON-able state to persist across restarts."""
        return {
            "registry": self.registry_payload.decode("utf-8") if self.registry_payload else None,
            "book": self.book.export(),
            "ledger": self.ledger.export(),
            "own_ratios": list(self.own_ratios),
        }

    def restore_state(self, data: dict[str, Any] | None) -> None:
        """Restore persisted state (the registry signature is re-verified)."""
        if not data:
            return
        now = self.clock()
        raw = data.get("registry")
        if raw:
            try:
                env = parse_envelope(raw.encode("utf-8"))
                if env.cid == self.cid and env.src == self.admin_id and env.verify(self.admin_pk):
                    self._apply_registry(Registry.from_body(env.body), raw.encode("utf-8"), now)
            except ProtocolError:
                _LOGGER.warning("Ignoring invalid persisted registry")
        self.book.restore(data.get("book", {}), now)
        self.ledger.restore(data.get("ledger", {}))
        self.own_ratios.extend(int(x) for x in data.get("own_ratios", [])[-50:])

