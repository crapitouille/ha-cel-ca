"""CEL-CA: cooperation of Home Assistant HEMS within a Communauté Électrique Locale."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_ACCESS_KEY, DOMAIN
from .hub import CelHub
from .services import async_setup_services

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type CelConfigEntry = ConfigEntry[CelHub]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain services."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CelConfigEntry) -> bool:
    """Start one HEMS node."""
    if not entry.data.get(CONF_ACCESS_KEY):
        # Entry migrated from the MQTT version: the identity is kept, only the
        # access key to the Azure Storage of the CEL is needed.
        raise ConfigEntryAuthFailed("A CEL access key is required")
    hub = CelHub(hass, entry)
    await hub.async_start()
    entry.runtime_data = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CelConfigEntry) -> bool:
    """Stop the node."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


_MQTT_KEYS = ("host", "port", "tls", "ca_cert", "username", "password")


async def async_migrate_entry(hass: HomeAssistant, entry: CelConfigEntry) -> bool:
    """v1 (MQTT broker) -> v2 (Azure Storage): drop the broker settings, keep the identity."""
    if entry.version == 1:
        data = {k: v for k, v in entry.data.items() if k not in _MQTT_KEYS}
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return entry.version == 2


async def _async_reload(hass: HomeAssistant, entry: CelConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
