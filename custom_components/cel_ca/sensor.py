"""Sensors of the CEL-CA integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import CelConfigEntry
from .entity import CelEntity, CelPeerEntity, async_track_participants
from .hub import CelHub, ms_to_iso

MAX_ATTR_ITEMS = 20


def _res_attrs(items: list, now: int, counterpart: str) -> list[dict[str, Any]]:
    out = []
    for r in sorted(items, key=lambda r: r.start)[:MAX_ATTR_ITEMS]:
        out.append(
            {
                "reservation_id": r.rid,
                "offer_id": r.oid,
                counterpart: getattr(r, counterpart),
                "power_w": r.power_w,
                "start": ms_to_iso(r.start),
                "end": ms_to_iso(r.end),
                "active_now": r.active_at(now),
                "group_id": r.group,
            }
        )
    return out


def _peer_name(hub: CelHub, hems_id: str) -> str:
    peer = hub.node.peers.get(hems_id)
    return peer.name if peer else hems_id


def _community_attrs(hub: CelHub) -> dict[str, Any]:
    offers = hub.node.best_offers()[:MAX_ATTR_ITEMS]
    return {
        "offers": [
            {
                "producer": o["producer"],
                "producer_name": o["producer_name"],
                "offer_id": o["oid"],
                "available_w": o["available_w"],
                "power_w": o["power_w"],
                "end": ms_to_iso(o["end"]),
                "reputation_pct": o["reputation_pct"],
            }
            for o in offers
        ]
    }


def _reserved_in_attrs(hub: CelHub) -> dict[str, Any]:
    now = hub.now_ms()
    items = _res_attrs(list(hub.node.ledger.active.values()), now, "producer")
    for item in items:
        item["producer_name"] = _peer_name(hub, item["producer"])
    return {"reservations": items}


def _reserved_out_attrs(hub: CelHub) -> dict[str, Any]:
    now = hub.now_ms()
    items = _res_attrs(list(hub.node.book.reservations.values()), now, "consumer")
    for item in items:
        item["consumer_name"] = _peer_name(hub, item["consumer"])
    return {"reservations": items}


def _offered_attrs(hub: CelHub) -> dict[str, Any]:
    now = hub.now_ms()
    return {
        "offers": [
            {**o, "start": ms_to_iso(o["start"]), "end": ms_to_iso(o["end"])}
            for o in hub.node.book.offer_book(now)
        ],
        "auto_offer": hub.node.config.auto_offer,
    }


def _peers_attrs(hub: CelHub) -> dict[str, Any]:
    return {
        "peers": [
            {
                "hems_id": p.hems_id,
                "name": p.name,
                "roles": sorted(p.roles),
                "online": p.online,
                "last_seen": ms_to_iso(p.last_seen) if p.last_seen else None,
                "reputation_pct": p.reputation_pct,
            }
            for p in sorted(hub.node.peers.values(), key=lambda p: p.name)
        ],
        "members": len(hub.node.registry.members),
    }


def _identity_attrs(hub: CelHub) -> dict[str, Any]:
    return {
        "public_key": hub.identity.public_b64,
        "community_id": hub.community_id,
        "registry_serial": hub.node.registry.serial,
        "dropped_messages": sum(hub.node.stats.values()),
        "access_key_expires": hub.access_key_expires.isoformat() if hub.access_key_expires else None,
    }


def _participants_attrs(hub: CelHub) -> dict[str, Any]:
    slot_ms = hub.node.slot_ms
    return {
        "slot_minutes": slot_ms // 60_000 or None,
        "next_slot_start": ms_to_iso(hub.node.market_time()) if slot_ms else None,
        "members": hub.totals.get("members", 0),
        "participants": [
            {**row, "last_seen": ms_to_iso(row["last_seen"]) if row["last_seen"] else None}
            for row in hub.participant_rows
        ],
    }


@dataclass(frozen=True, kw_only=True)
class CelSensorDescription(SensorEntityDescription):
    """Sensor description with accessors."""

    value_fn: Callable[[CelHub], Any]
    attrs_fn: Callable[[CelHub], dict[str, Any]] | None = None


_POWER = {
    "device_class": SensorDeviceClass.POWER,
    "state_class": SensorStateClass.MEASUREMENT,
    "native_unit_of_measurement": UnitOfPower.WATT,
}

SENSORS: tuple[CelSensorDescription, ...] = (
    CelSensorDescription(
        key="community_surplus",
        translation_key="community_surplus",
        value_fn=lambda hub: hub.node.community_available_w(),
        attrs_fn=_community_attrs,
        **_POWER,
    ),
    CelSensorDescription(
        key="reserved_in",
        translation_key="reserved_in",
        value_fn=lambda hub: hub.node.ledger.reserved_power_at(hub.now_ms()),
        attrs_fn=_reserved_in_attrs,
        **_POWER,
    ),
    CelSensorDescription(
        key="reserved_out",
        translation_key="reserved_out",
        value_fn=lambda hub: hub.node.book.reserved_power_at(hub.now_ms()),
        attrs_fn=_reserved_out_attrs,
        **_POWER,
    ),
    CelSensorDescription(
        key="offered_power",
        translation_key="offered_power",
        value_fn=lambda hub: hub.node.book.offered_power_at(hub.now_ms()),
        attrs_fn=_offered_attrs,
        **_POWER,
    ),
    CelSensorDescription(
        key="peers_online",
        translation_key="peers_online",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda hub: sum(1 for p in hub.node.peers.values() if p.online),
        attrs_fn=_peers_attrs,
    ),
    CelSensorDescription(
        key="compliance",
        translation_key="compliance",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda hub: hub.node.compliance_pct,
    ),
    CelSensorDescription(
        key="participants",
        translation_key="participants",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda hub: hub.totals.get("online", 0),
        attrs_fn=_participants_attrs,
    ),
    CelSensorDescription(
        key="community_offered",
        translation_key="community_offered",
        value_fn=lambda hub: hub.totals.get("offered_w", 0),
        **_POWER,
    ),
    CelSensorDescription(
        key="community_reserved",
        translation_key="community_reserved",
        value_fn=lambda hub: hub.totals.get("reserved_w", 0),
        **_POWER,
    ),
    CelSensorDescription(
        key="hems_id",
        translation_key="hems_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda hub: hub.hems_id,
        attrs_fn=_identity_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: CelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up sensors."""
    hub = entry.runtime_data
    async_add_entities(CelSensor(hub, d) for d in SENSORS)
    entry.async_on_unload(
        async_track_participants(
            hass, hub, async_add_entities, lambda pid: [CelPeerSensor(hub, pid, d) for d in PEER_SENSORS]
        )
    )


@dataclass(frozen=True, kw_only=True)
class CelPeerSensorDescription(SensorEntityDescription):
    """Sensor of another participant, read from the community snapshot."""

    value_fn: Callable[[dict[str, Any]], Any]


PEER_SENSORS: tuple[CelPeerSensorDescription, ...] = (
    CelPeerSensorDescription(key="offered", translation_key="peer_offered", value_fn=lambda r: r["offered_w"], **_POWER),
    CelPeerSensorDescription(key="available", translation_key="peer_available", value_fn=lambda r: r["available_w"], **_POWER),
    CelPeerSensorDescription(key="reserved_out", translation_key="peer_reserved_out", value_fn=lambda r: r["reserved_out_w"], **_POWER),
    CelPeerSensorDescription(key="reserved_in", translation_key="peer_reserved_in", value_fn=lambda r: r["reserved_in_w"], **_POWER),
    CelPeerSensorDescription(
        key="reputation",
        translation_key="peer_reputation",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda r: r["reputation_pct"],
    ),
    CelPeerSensorDescription(
        key="last_seen",
        translation_key="peer_last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: dt_util.utc_from_timestamp(r["last_seen"] / 1000) if r["last_seen"] else None,
    ),
)


class CelPeerSensor(CelPeerEntity, SensorEntity):
    """Sensor of another participant."""

    entity_description: CelPeerSensorDescription

    @property
    def native_value(self) -> Any:
        row = self.row
        return self.entity_description.value_fn(row) if row else None


class CelSensor(CelEntity, SensorEntity):
    """CEL sensor."""

    entity_description: CelSensorDescription
    # The participants table can be large and is already visible live: keep it out of the recorder.
    _unrecorded_attributes = frozenset({"participants", "offers", "reservations", "peers"})

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.hub)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.hub) if fn else None
