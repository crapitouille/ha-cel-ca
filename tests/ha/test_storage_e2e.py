"""Home Assistant end to end over Azure Storage (Azurite): real transport, two HEMS."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cel_ca.const import (
    CONF_ACCESS_KEY,
    CONF_ADMIN_PUBLIC_KEY,
    CONF_COMMUNITY_ID,
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_SIGN,
    CONF_NAME,
    CONF_PRIVATE_KEY,
    CONF_ROLES,
    DEFAULT_OPTIONS,
    DOMAIN,
    SIGN_IMPORT_POSITIVE,
)
from custom_components.cel_ca.core.identity import Identity
from custom_components.cel_ca.core.messages import MsgType, encode_envelope
from custom_components.cel_ca.core.registry import Member, build_registry_body

from ..storage.conftest import start_azurite

CID = "cel-e2e"


@pytest.fixture
def azurite_ha(tmp_path):
    started = start_azurite()
    if started is None:
        pytest.skip("Azurite not installed")
    proc, server = started
    with (
        patch("custom_components.cel_ca.hub.STATE_POLL_S", 0.2),
        patch("custom_components.cel_ca.hub.INBOX_POLL_S", 0.2),
    ):
        yield server
    proc.kill()
    proc.wait(10)


async def wait_for(hass, predicate, timeout=10.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.1)
            await hass.async_block_till_done()


def make_entry(admin_store, admin: Identity, name: str, ident: Identity) -> MockConfigEntry:
    key = admin_store.issue_access(CID, ident.hems_id, admin.public_b64)
    return MockConfigEntry(
        domain=DOMAIN, title=name, unique_id=ident.hems_id, version=2,
        data={
            CONF_NAME: name, CONF_COMMUNITY_ID: CID, CONF_ACCESS_KEY: key.encode(), CONF_ADMIN_PUBLIC_KEY: admin.public_b64,
            CONF_PRIVATE_KEY: ident.private_b64, CONF_ROLES: ["producer", "consumer"],
            CONF_GRID_POWER_ENTITY: f"sensor.{name}_grid", CONF_GRID_SIGN: SIGN_IMPORT_POSITIVE,
        },
        options=dict(DEFAULT_OPTIONS),
    )


async def test_two_hems_in_ha_over_azure_storage(hass: HomeAssistant, azurite_ha) -> None:
    admin_store = azurite_ha.admin()
    admin = Identity.generate()
    ids = {"alice": Identity.generate(), "bob": Identity.generate()}
    entries = {n: make_entry(admin_store, admin, n, i) for n, i in ids.items()}
    members = [Member(i.hems_id, i.public_key, n, frozenset({"producer", "consumer"})) for n, i in ids.items()]
    admin_store.put_registry(CID, encode_envelope(admin, CID, MsgType.REGISTRY, build_registry_body(1, members, ()), int(time.time() * 1000)))

    for entry in entries.values():
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    alice_hub, bob_hub = entries["alice"].runtime_data, entries["bob"].runtime_data
    await wait_for(hass, lambda: alice_hub.node.enrolled and bob_hub.node.enrolled)
    await wait_for(hass, lambda: alice_hub.hems_id in bob_hub.node.peers and bob_hub.node.peers[alice_hub.hems_id].online)

    hass.states.async_set("sensor.alice_grid", "-3100", {"unit_of_measurement": "W"})
    await alice_hub.async_tick()
    await wait_for(hass, lambda: bob_hub.node.community_available_w() == 3000)

    res = await hass.services.async_call(
        DOMAIN, "reserve", {"config_entry_id": entries["bob"].entry_id, "power_w": 2000}, blocking=True, return_response=True
    )
    assert res["status"] == "accepted", res
    await wait_for(hass, lambda: alice_hub.node.book.reserved_power_at(alice_hub.now_ms()) == 2000)

    for entry in entries.values():
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_refused_key_at_runtime_starts_reauth(hass: HomeAssistant, azurite_ha) -> None:
    admin_store = azurite_ha.admin()
    admin = Identity.generate()
    ident = Identity.generate()
    entry = make_entry(admin_store, admin, "alice", ident)
    entry.add_to_hass(hass)
    # simulate an expired / revoked key: same format, wrong signature
    from custom_components.cel_ca.core.storage_access import AccessKey

    good = AccessKey.decode(entry.data[CONF_ACCESS_KEY])
    bad = AccessKey(**{**{f: getattr(good, f) for f in AccessKey.__slots__}, "tr": good.tr.replace("sig=", "sig=AA")})
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ACCESS_KEY: bad.encode()})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await wait_for(hass, lambda: any(f["context"]["source"] == config_entries.SOURCE_REAUTH for f in hass.config_entries.flow.async_progress()))
    assert not entry.runtime_data.node.connected
    assert await hass.config_entries.async_unload(entry.entry_id)
