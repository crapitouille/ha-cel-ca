"""Services (actions) of the CEL-CA integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DURATION_MIN,
    ATTR_MIN_POWER_W,
    ATTR_OFFER_ID,
    ATTR_POWER_W,
    ATTR_PRODUCER_ID,
    ATTR_RESERVATION_ID,
    ATTR_START,
    DOMAIN,
    SERVICE_CANCEL,
    SERVICE_GENERATE_DASHBOARD,
    SERVICE_PUBLISH_OFFER,
    SERVICE_RESERVE,
    SERVICE_RESERVE_GLOBAL,
    SERVICE_WITHDRAW_OFFER,
)
from .dashboard import build_dashboard
from .hub import CelHub, datetime_to_ms

_POWER = vol.All(vol.Coerce(int), vol.Range(min=1, max=1_000_000))
_ENTRY = vol.Optional(ATTR_CONFIG_ENTRY_ID)

RESERVE_SCHEMA = vol.Schema(
    {
        _ENTRY: cv.string,
        vol.Required(ATTR_POWER_W): _POWER,
        vol.Optional(ATTR_MIN_POWER_W): _POWER,
        vol.Optional(ATTR_DURATION_MIN, default=15): vol.All(vol.Coerce(int), vol.Range(min=1, max=240)),
        vol.Optional(ATTR_PRODUCER_ID): cv.string,
        vol.Optional(ATTR_OFFER_ID): cv.string,
        vol.Optional(ATTR_START): cv.datetime,
    }
)
RESERVE_GLOBAL_SCHEMA = vol.Schema(
    {
        _ENTRY: cv.string,
        vol.Required(ATTR_POWER_W): _POWER,
        vol.Optional(ATTR_MIN_POWER_W): _POWER,
        vol.Optional(ATTR_DURATION_MIN, default=15): vol.All(vol.Coerce(int), vol.Range(min=1, max=240)),
        vol.Optional(ATTR_START): cv.datetime,
    }
)
CANCEL_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required(ATTR_RESERVATION_ID): cv.string})
PUBLISH_SCHEMA = vol.Schema(
    {
        _ENTRY: cv.string,
        vol.Required(ATTR_POWER_W): vol.All(vol.Coerce(int), vol.Range(min=0, max=1_000_000)),
        vol.Optional(ATTR_DURATION_MIN, default=60): vol.All(vol.Coerce(int), vol.Range(min=1, max=24 * 60)),
        vol.Optional(ATTR_START): cv.datetime,
        vol.Optional(ATTR_OFFER_ID): cv.string,
    }
)
WITHDRAW_SCHEMA = vol.Schema({_ENTRY: cv.string, vol.Required(ATTR_OFFER_ID): cv.string})
DASHBOARD_SCHEMA = vol.Schema({_ENTRY: cv.string})


def _get_hub(hass: HomeAssistant, call: ServiceCall) -> CelHub:
    entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_found")
    elif len(entries) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_entry" if not entries else "multiple_entries",
        )
    return entries[0].runtime_data


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register services."""

    async def reserve(call: ServiceCall) -> ServiceResponse:
        hub = _get_hub(hass, call)
        start = call.data.get(ATTR_START)
        result = await hub.node.reserve(
            call.data[ATTR_POWER_W],
            call.data[ATTR_DURATION_MIN] * 60_000,
            min_power_w=call.data.get(ATTR_MIN_POWER_W),
            producer=call.data.get(ATTR_PRODUCER_ID),
            oid=call.data.get(ATTR_OFFER_ID),
            start=datetime_to_ms(start) if start else None,
        )
        return hub.reservation_result(result)

    async def reserve_global(call: ServiceCall) -> ServiceResponse:
        hub = _get_hub(hass, call)
        start = call.data.get(ATTR_START)
        result = await hub.node.reserve_global(
            call.data[ATTR_POWER_W],
            call.data[ATTR_DURATION_MIN] * 60_000,
            min_power_w=call.data.get(ATTR_MIN_POWER_W),
            start=datetime_to_ms(start) if start else None,
        )
        return hub.reservation_result(result)

    async def cancel(call: ServiceCall) -> ServiceResponse:
        hub = _get_hub(hass, call)
        ok = await hub.node.cancel_reservation(call.data[ATTR_RESERVATION_ID])
        if not ok:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_reservation")
        return {"cancelled": True}

    async def publish_offer(call: ServiceCall) -> ServiceResponse:
        hub = _get_hub(hass, call)
        start_dt = call.data.get(ATTR_START)
        start = datetime_to_ms(start_dt) if start_dt else hub.now_ms()
        end = start + call.data[ATTR_DURATION_MIN] * 60_000
        try:
            oid = await hub.node.publish_offer(call.data[ATTR_POWER_W], start, end, oid=call.data.get(ATTR_OFFER_ID))
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_offer", translation_placeholders={"error": str(err)}
            ) from err
        hub.schedule_save()
        return {"offer_id": oid}

    async def withdraw_offer(call: ServiceCall) -> ServiceResponse:
        hub = _get_hub(hass, call)
        if not await hub.node.withdraw_offer(call.data[ATTR_OFFER_ID]):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_offer")
        hub.schedule_save()
        return {"withdrawn": True}

    async def generate_dashboard(call: ServiceCall) -> ServiceResponse:
        hub = _get_hub(hass, call)
        hub.refresh_community()
        # The response *is* the dashboard: copy it from Developer tools and paste
        # it as-is into a dashboard's raw configuration editor.
        return build_dashboard(hass, hub)

    registrations: list[tuple[str, Any, vol.Schema, SupportsResponse]] = [
        (SERVICE_RESERVE, reserve, RESERVE_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_RESERVE_GLOBAL, reserve_global, RESERVE_GLOBAL_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_CANCEL, cancel, CANCEL_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_PUBLISH_OFFER, publish_offer, PUBLISH_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_WITHDRAW_OFFER, withdraw_offer, WITHDRAW_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_GENERATE_DASHBOARD, generate_dashboard, DASHBOARD_SCHEMA, SupportsResponse.ONLY),
    ]
    for name, handler, schema, response in registrations:
        hass.services.async_register(DOMAIN, name, handler, schema=schema, supports_response=response)
