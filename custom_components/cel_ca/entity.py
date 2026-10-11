"""Base entities: the local HEMS node and the other participants of the CEL."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import MANUFACTURER
from .hub import CelHub


class CelEntity(Entity):
    """Entity bound to the local HEMS node."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: CelHub, description: EntityDescription) -> None:
        self.hub = hub
        self.entity_description = description
        self._attr_unique_id = f"{hub.hems_id}_{description.key}"
        self._attr_device_info = hub.own_device_info()

    async def async_added_to_hass(self) -> None:
        """Follow node updates."""
        self.async_on_remove(async_dispatcher_connect(self.hass, self.hub.signal, self.async_write_ha_state))


class CelPeerEntity(Entity):
    """Entity describing another participant of the CEL (one device per participant)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: CelHub, peer_id: str, description: EntityDescription) -> None:
        self.hub = hub
        self.peer_id = peer_id
        self.entity_description = description
        self._attr_unique_id = f"{hub.hems_id}_peer_{peer_id}_{description.key}"
        row = hub.participants.get(peer_id, {})
        self._attr_device_info = DeviceInfo(
            identifiers={hub.peer_device_identifier(peer_id)},
            name=row.get("name", peer_id),
            manufacturer=MANUFACTURER,
            model=f"CEL participant ({hub.community_id})",
            serial_number=peer_id,
            **hub.via_own_device(),
        )

    @property
    def row(self) -> dict[str, Any] | None:
        """Current snapshot of the participant."""
        return self.hub.participants.get(self.peer_id)

    @property
    def available(self) -> bool:
        """Unavailable once the participant left the registry."""
        return self.row is not None

    async def async_added_to_hass(self) -> None:
        """Follow node updates."""
        self.async_on_remove(async_dispatcher_connect(self.hass, self.hub.signal, self.async_write_ha_state))


@callback
def async_track_participants(
    hass: HomeAssistant,
    hub: CelHub,
    async_add_entities: Callable[[Iterable[Entity]], None],
    factory: Callable[[str], Iterable[Entity]],
) -> Callable[[], None]:
    """Create entities for every other participant, now and whenever one joins.

    Entities of departed participants are removed with their device by the hub.
    """
    added: set[str] = set()

    @callback
    def _sync() -> None:
        new = [pid for pid, row in hub.participants.items() if not row["is_self"] and pid not in added]
        if not new:
            return
        added.update(new)
        async_add_entities([entity for pid in new for entity in factory(pid)])

    @callback
    def _forget_departed() -> None:
        for pid in [p for p in added if p not in hub.participants]:
            added.discard(pid)

    @callback
    def _on_update() -> None:
        _forget_departed()
        _sync()

    _sync()
    return async_dispatcher_connect(hass, hub.signal, _on_update)
