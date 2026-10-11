"""Binary sensors of the CEL-CA integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CelConfigEntry
from .entity import CelEntity, CelPeerEntity, async_track_participants
from .hub import CelHub


@dataclass(frozen=True, kw_only=True)
class CelBinaryDescription(BinarySensorEntityDescription):
    """Binary sensor description."""

    value_fn: Callable[[CelHub], bool]


BINARY_SENSORS = (
    CelBinaryDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda hub: hub.node.connected,
    ),
    CelBinaryDescription(
        key="enrolled",
        translation_key="enrolled",
        value_fn=lambda hub: hub.node.enrolled,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: CelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up binary sensors."""
    hub = entry.runtime_data
    async_add_entities(CelBinarySensor(hub, d) for d in BINARY_SENSORS)
    entry.async_on_unload(
        async_track_participants(hass, hub, async_add_entities, lambda pid: [CelPeerOnline(hub, pid, PEER_ONLINE)])
    )


PEER_ONLINE = BinarySensorEntityDescription(
    key="online", translation_key="peer_online", device_class=BinarySensorDeviceClass.CONNECTIVITY
)


class CelPeerOnline(CelPeerEntity, BinarySensorEntity):
    """Presence of another participant."""

    @property
    def is_on(self) -> bool | None:
        row = self.row
        return row["online"] if row else None


class CelBinarySensor(CelEntity, BinarySensorEntity):
    """CEL binary sensor."""

    entity_description: CelBinaryDescription

    @property
    def is_on(self) -> bool:
        return self.entity_description.value_fn(self.hub)
