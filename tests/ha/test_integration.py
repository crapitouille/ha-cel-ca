"""Two (or three) HEMS inside one Home Assistant, talking through the CEL."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.cel_ca.const import CONF_GRID_SIGN, DOMAIN, EVENT_CEL, SIGN_EXPORT_POSITIVE

from .conftest import CID


async def setup_entries(hass: HomeAssistant, cel_env, *names, register=True, **kw):
    entries = {n: cel_env.entry(n, **kw) for n in names}
    if register:
        await cel_env.publish_registry()
    for entry in entries.values():
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entries


def state(hass, entity_id):
    st = hass.states.get(entity_id)
    assert st is not None, entity_id
    return st


async def set_grid(hass: HomeAssistant, entity: str, watts: float, unit: str = UnitOfPower.WATT) -> None:
    hass.states.async_set(entity, str(watts), {"unit_of_measurement": unit, "device_class": "power"})
    await hass.async_block_till_done()


async def tick_all(hass: HomeAssistant) -> None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            await entry.runtime_data.async_tick()
    await hass.async_block_till_done()


async def test_setup_creates_entities_and_unload_clears_presence(hass: HomeAssistant, cel_env) -> None:
    entries = await setup_entries(hass, cel_env, "alice", "bob")
    assert state(hass, "binary_sensor.alice_cel_connection").state == STATE_ON
    assert state(hass, "binary_sensor.alice_enrolled").state == STATE_ON
    assert state(hass, "sensor.alice_hems_online").state == "1"
    ident = state(hass, "sensor.alice_hems_identifier")
    assert ident.state == cel_env.identities["alice"].hems_id
    assert ident.attributes["public_key"] == cel_env.identities["alice"].public_b64
    assert state(hass, "switch.alice_publish_surplus_automatically").state == STATE_ON

    alice_id = cel_env.identities["alice"].hems_id
    assert await hass.config_entries.async_unload(entries["alice"].entry_id)
    await hass.async_block_till_done()
    assert f"cel/{CID}/node/{alice_id}/status" not in cel_env.broker.retained
    assert state(hass, "sensor.bob_hems_online").state == "0"


async def test_surplus_reservation_end_to_end(hass: HomeAssistant, cel_env) -> None:
    await setup_entries(hass, cel_env, "alice", "bob")
    events = async_capture_events(hass, EVENT_CEL)
    await set_grid(hass, "sensor.alice_grid", -3100)
    await tick_all(hass)

    assert state(hass, "sensor.alice_offered_surplus").state == "3000"
    surplus = state(hass, "sensor.bob_community_surplus_available")
    assert surplus.state == "3000"
    assert surplus.attributes["offers"][0]["producer_name"] == "alice"

    bob_entry = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.title == "bob")
    result = await hass.services.async_call(
        DOMAIN,
        "reserve",
        {"config_entry_id": bob_entry.entry_id, "power_w": 2000, "duration_min": 30},
        blocking=True,
        return_response=True,
    )
    assert result["status"] == "accepted" and result["power_w"] == 2000
    assert result["producer_name"] == "alice" and result["end"].endswith("+00:00")
    await hass.async_block_till_done()

    assert state(hass, "sensor.bob_reserved_power_consumption").state == "2000"
    assert state(hass, "sensor.alice_reserved_power_production").state == "2000"
    assert state(hass, "sensor.bob_community_surplus_available").state == "1000"
    reserved = state(hass, "sensor.alice_reserved_power_production").attributes["reservations"]
    assert reserved[0]["consumer_name"] == "bob"
    kinds = {(e.data["type"], e.data["hems_id"]) for e in events}
    assert ("request_received", cel_env.identities["alice"].hems_id) in kinds
    assert ("granted", cel_env.identities["bob"].hems_id) in kinds

    await hass.services.async_call(
        DOMAIN,
        "cancel_reservation",
        {"config_entry_id": bob_entry.entry_id, "reservation_id": result["reservation_id"]},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert state(hass, "sensor.alice_reserved_power_production").state == "0"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "cancel_reservation",
            {"config_entry_id": bob_entry.entry_id, "reservation_id": result["reservation_id"]},
            blocking=True,
        )


async def test_service_without_loaded_entry(hass: HomeAssistant, cel_env) -> None:
    entries = await setup_entries(hass, cel_env, "solo")
    assert await hass.config_entries.async_unload(entries["solo"].entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "reserve", {"power_w": 100}, blocking=True, return_response=True)
    assert err.value.translation_key == "no_entry"


async def test_service_requires_entry_when_several(hass: HomeAssistant, cel_env) -> None:
    await setup_entries(hass, cel_env, "alice", "bob")
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "reserve", {"power_w": 100}, blocking=True, return_response=True)
    assert err.value.translation_key == "multiple_entries"
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "reserve", {"power_w": 100, "config_entry_id": "nope"}, blocking=True, return_response=True
        )
    assert err.value.translation_key == "entry_not_found"


async def test_reserve_without_offer_returns_rejection(hass: HomeAssistant, cel_env) -> None:
    await setup_entries(hass, cel_env, "solo")
    result = await hass.services.async_call(DOMAIN, "reserve", {"power_w": 100}, blocking=True, return_response=True)
    assert result["status"] == "rejected" and result["reason"] == "no_offer"


async def test_manual_offer_services(hass: HomeAssistant, cel_env) -> None:
    entries = await setup_entries(hass, cel_env, "alice", "bob")
    res = await hass.services.async_call(
        DOMAIN,
        "publish_offer",
        {"config_entry_id": entries["alice"].entry_id, "power_w": 4000, "duration_min": 60},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()
    assert state(hass, "sensor.bob_community_surplus_available").state == "4000"
    await hass.services.async_call(
        DOMAIN, "withdraw_offer", {"config_entry_id": entries["alice"].entry_id, "offer_id": res["offer_id"]}, blocking=True
    )
    await hass.async_block_till_done()
    assert state(hass, "sensor.bob_community_surplus_available").state == "0"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "withdraw_offer", {"config_entry_id": entries["alice"].entry_id, "offer_id": res["offer_id"]}, blocking=True
        )


async def test_auto_offer_switch(hass: HomeAssistant, cel_env) -> None:
    await setup_entries(hass, cel_env, "alice", "bob")
    await set_grid(hass, "sensor.alice_grid", -2600)
    await tick_all(hass)
    assert state(hass, "sensor.bob_community_surplus_available").state == "2500"
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.alice_publish_surplus_automatically"}, blocking=True
    )
    await hass.async_block_till_done()
    assert state(hass, "switch.alice_publish_surplus_automatically").state == STATE_OFF
    assert state(hass, "sensor.bob_community_surplus_available").state == "0"


async def test_sign_convention_and_kw_unit(hass: HomeAssistant, cel_env) -> None:
    await setup_entries(hass, cel_env, "alice", "bob", **{CONF_GRID_SIGN: SIGN_EXPORT_POSITIVE})
    await set_grid(hass, "sensor.alice_grid", 1.6, UnitOfPower.KILO_WATT)  # exporting 1.6 kW
    await set_grid(hass, "sensor.alice_grid", "unavailable")  # ignored
    await tick_all(hass)
    assert state(hass, "sensor.alice_offered_surplus").state == "1500"


async def test_not_enrolled_issue_lifecycle(hass: HomeAssistant, cel_env) -> None:
    cel_env.entry("alice")
    await cel_env.publish_registry(["alice"])
    await setup_entries(hass, cel_env, "mallory", register=False)
    registry = ir.async_get(hass)
    issues = [i for i in registry.issues.values() if i.domain == DOMAIN]
    assert len(issues) == 1 and issues[0].translation_key == "not_enrolled"
    assert issues[0].translation_placeholders["hems_id"] == cel_env.identities["mallory"].hems_id
    assert state(hass, "binary_sensor.mallory_enrolled").state == STATE_OFF

    await cel_env.publish_registry()  # the admin enrolls mallory
    await hass.async_block_till_done()
    assert not [i for i in registry.issues.values() if i.domain == DOMAIN]
    assert state(hass, "binary_sensor.mallory_enrolled").state == STATE_ON


async def test_reservation_survives_reload(hass: HomeAssistant, cel_env) -> None:
    entries = await setup_entries(hass, cel_env, "alice", "bob")
    await set_grid(hass, "sensor.alice_grid", -3100)
    await tick_all(hass)
    result = await hass.services.async_call(
        DOMAIN, "reserve", {"config_entry_id": entries["bob"].entry_id, "power_w": 1000}, blocking=True, return_response=True
    )
    assert result["status"] == "accepted"
    for entry in entries.values():
        assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state(hass, "sensor.bob_reserved_power_consumption").state == "1000"
    assert state(hass, "sensor.alice_reserved_power_production").state == "1000"
    assert state(hass, "binary_sensor.bob_enrolled").state == STATE_ON


async def test_diagnostics_redact_secrets(hass: HomeAssistant, hass_client, cel_env) -> None:
    entries = await setup_entries(hass, cel_env, "alice")
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entries["alice"])
    assert diag["entry"]["data"]["private_key"] == "**REDACTED**"
    assert diag["entry"]["data"]["access_key"] == "**REDACTED**"
    assert "connected" in diag["transport"]
    assert cel_env.identities["alice"].private_b64 not in str(diag)
    assert diag["enrolled"] is True


async def test_french_entity_ids(hass: HomeAssistant, cel_env) -> None:
    hass.config.language = "fr"
    await setup_entries(hass, cel_env, "maison")
    for entity_id in (
        "sensor.maison_surplus_disponible_dans_la_communaute",
        "sensor.maison_puissance_reservee_consommation",
        "sensor.maison_puissance_reservee_production",
        "sensor.maison_surplus_propose",
        "sensor.maison_hems_en_ligne",
        "sensor.maison_respect_des_engagements",
        "sensor.maison_identifiant_hems",
        "binary_sensor.maison_connexion_cel",
        "binary_sensor.maison_enrole",
        "switch.maison_publier_le_surplus_automatiquement",
    ):
        state(hass, entity_id)


async def test_state_is_saved_periodically_and_on_events(hass: HomeAssistant, cel_env, hass_storage) -> None:
    from unittest.mock import patch as _patch

    entries = await setup_entries(hass, cel_env, "alice", "bob")
    hub = entries["bob"].runtime_data
    key = f"{DOMAIN}.{entries['bob'].entry_id}"
    await set_grid(hass, "sensor.alice_grid", -3100)
    await tick_all(hass)
    # an event (reservation granted) schedules a debounced save
    with _patch.object(hub._store, "async_delay_save") as delayed:
        result = await hass.services.async_call(
            DOMAIN, "reserve", {"config_entry_id": entries["bob"].entry_id, "power_w": 1000}, blocking=True, return_response=True
        )
    assert delayed.called
    # ticks every 10 s must not postpone saving forever: explicit periodic save
    hass_storage.pop(key, None)
    await hub.async_tick()
    assert key not in hass_storage
    with _patch("custom_components.cel_ca.hub.time.monotonic", return_value=hub._last_save + 301):
        await hub.async_tick()
    await hass.async_block_till_done()
    saved = hass_storage[key]["data"]["node"]["ledger"]["reservations"]
    assert saved[0]["rid"] == result["reservation_id"]
