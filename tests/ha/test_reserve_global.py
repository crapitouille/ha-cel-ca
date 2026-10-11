"""Global reservation spread over several producers, through the HA action."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.cel_ca.const import DOMAIN, EVENT_CEL

from .test_integration import set_grid, setup_entries, state, tick_all


async def _setup(hass: HomeAssistant, cel_env):
    entries = await setup_entries(hass, cel_env, "alice", "bob", "carol")
    await set_grid(hass, "sensor.alice_grid", -3100)  # 3000 W offered
    await set_grid(hass, "sensor.carol_grid", -1100)  # 1000 W offered
    await tick_all(hass)
    return entries


async def test_reserve_global_proportional_and_cancel_group(hass: HomeAssistant, cel_env) -> None:
    entries = await _setup(hass, cel_env)
    events = async_capture_events(hass, EVENT_CEL)
    res = await hass.services.async_call(
        DOMAIN,
        "reserve_global",
        {"config_entry_id": entries["bob"].entry_id, "power_w": 2000, "duration_min": 30},
        blocking=True,
        return_response=True,
    )
    assert res["status"] == "accepted" and res["power_w"] == 2000 and res["requested_w"] == 2000
    assert res["group_id"].startswith("g")
    shares = {r["producer_name"]: r["power_w"] for r in res["reservations"]}
    assert shares == {"alice": 1500, "carol": 500}
    assert all(r["end"].endswith("+00:00") for r in res["reservations"])
    await hass.async_block_till_done()

    assert state(hass, "sensor.bob_reserved_power_consumption").state == "2000"
    assert state(hass, "sensor.alice_reserved_power_production").state == "1500"
    assert state(hass, "sensor.carol_reserved_power_production").state == "500"
    # (in this test the three HEMS share one HA, so look the entity up by unique id)
    bob_in = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{cel_env.identities['bob'].hems_id}_reserved_in")
    attrs = hass.states.get(bob_in).attributes["reservations"]
    assert {a["group_id"] for a in attrs} == {res["group_id"]}
    assert any(e.data["type"] == "group_granted" and e.data["group_id"] == res["group_id"] for e in events)

    # the whole group is cancelled with its id
    await hass.services.async_call(
        DOMAIN,
        "cancel_reservation",
        {"config_entry_id": entries["bob"].entry_id, "reservation_id": res["group_id"]},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert state(hass, "sensor.bob_reserved_power_consumption").state == "0"
    assert state(hass, "sensor.alice_reserved_power_production").state == "0"
    assert state(hass, "sensor.carol_reserved_power_production").state == "0"


async def test_reserve_global_all_or_nothing(hass: HomeAssistant, cel_env) -> None:
    entries = await _setup(hass, cel_env)
    res = await hass.services.async_call(
        DOMAIN,
        "reserve_global",
        {"config_entry_id": entries["bob"].entry_id, "power_w": 6000, "min_power_w": 5000},
        blocking=True,
        return_response=True,
    )
    assert res["status"] == "rejected" and res["reason"] == "insufficient" and res["reservations"] == []
    await hass.async_block_till_done()
    assert state(hass, "sensor.alice_reserved_power_production").state == "0"
    res = await hass.services.async_call(
        DOMAIN,
        "reserve_global",
        {"config_entry_id": entries["bob"].entry_id, "power_w": 6000, "min_power_w": 3000},
        blocking=True,
        return_response=True,
    )
    assert res["status"] == "partial" and res["power_w"] == 4000
