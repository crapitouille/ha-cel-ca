"""Slot market (community parameter slot_minutes) through Home Assistant."""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import HomeAssistant

from custom_components.cel_ca.const import DOMAIN

from .test_integration import set_grid, setup_entries, state, tick_all


async def test_reservation_uses_the_next_15_min_slot(hass: HomeAssistant, cel_env) -> None:
    entries = await setup_entries(hass, cel_env, "alice", "bob", register=False)
    await cel_env.publish_registry(params={"slot_minutes": 15})
    await hass.async_block_till_done()
    await set_grid(hass, "sensor.alice_grid", -3100)
    await tick_all(hass)

    participants = state(hass, "sensor.alice_participants_online")
    assert participants.attributes["slot_minutes"] == 15
    next_slot = datetime.fromisoformat(participants.attributes["next_slot_start"])
    assert next_slot.minute % 15 == 0 and next_slot.second == 0

    res = await hass.services.async_call(
        DOMAIN,
        "reserve",
        {"config_entry_id": entries["bob"].entry_id, "power_w": 2000, "duration_min": 60},
        blocking=True,
        return_response=True,
    )
    assert res["status"] == "accepted"
    start, end = datetime.fromisoformat(res["start"]), datetime.fromisoformat(res["end"])
    assert start == next_slot and (end - start).total_seconds() == 15 * 60  # one slot, not 60 min

    res = await hass.services.async_call(
        DOMAIN,
        "reserve_global",
        {"config_entry_id": entries["bob"].entry_id, "power_w": 500},
        blocking=True,
        return_response=True,
    )
    assert res["status"] == "accepted" and datetime.fromisoformat(res["start"]) == next_slot


async def test_continuous_market_without_parameter(hass: HomeAssistant, cel_env) -> None:
    await setup_entries(hass, cel_env, "alice")
    participants = state(hass, "sensor.alice_participants_online")
    assert participants.attributes["slot_minutes"] is None
    assert participants.attributes["next_slot_start"] is None
