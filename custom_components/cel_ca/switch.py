"""Switch enabling the automatic publication of the surplus."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CelConfigEntry
from .const import DOMAIN, ROLE_PRODUCER
from .entity import CelEntity

AUTO_OFFER = SwitchEntityDescription(key="auto_offer", translation_key="auto_offer")


async def async_setup_entry(
    hass: HomeAssistant, entry: CelConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Only producers get the switch."""
    hub = entry.runtime_data
    if ROLE_PRODUCER in hub.node.config.roles:
        async_add_entities([CelAutoOfferSwitch(hub, AUTO_OFFER)])
        return
    # The HEMS is no longer a producer: remove the orphan switch if any.
    registry = er.async_get(hass)
    unique_id = f"{hub.hems_id}_{AUTO_OFFER.key}"
    if entity_id := registry.async_get_entity_id("switch", DOMAIN, unique_id):
        registry.async_remove(entity_id)


class CelAutoOfferSwitch(CelEntity, SwitchEntity):
    """Publish the measured surplus automatically."""

    @property
    def is_on(self) -> bool:
        return self.hub.node.config.auto_offer

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hub.async_set_auto_offer(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hub.async_set_auto_offer(False)
        self.async_write_ha_state()
