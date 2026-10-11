"""Config flow (identity + CEL access key), reauth, migration from MQTT, options."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cel_ca.const import (
    CONF_ACCESS_KEY,
    CONF_ADMIN_PUBLIC_KEY,
    CONF_COMMUNITY_ID,
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_SIGN,
    CONF_MIN_OFFER_W,
    CONF_NAME,
    CONF_OFFER_HORIZON_MIN,
    CONF_OFFER_MARGIN_W,
    CONF_PRIVATE_KEY,
    CONF_ROLES,
    CONF_SMOOTHING_S,
    CONF_UPDATE_THRESHOLD_PCT,
    DOMAIN,
    ISSUE_KEY_EXPIRING,
    SIGN_EXPORT_POSITIVE,
    SIGN_IMPORT_POSITIVE,
)
from custom_components.cel_ca.core.identity import Identity
from custom_components.cel_ca.core.storage_access import AccessKey

PROBE = "custom_components.cel_ca.config_flow.probe_access"
ENERGY = {CONF_ROLES: ["producer", "consumer"], CONF_GRID_POWER_ENTITY: "sensor.p1_meter_power", CONF_GRID_SIGN: SIGN_IMPORT_POSITIVE}


def key_for(hems_id: str, admin: Identity | None = None, cid: str = "cel-ca") -> str:
    admin = admin or Identity.generate()
    return AccessKey(cid, hems_id, admin.public_b64, "https://a.table.core.windows.net", "https://a.queue.core.windows.net", "tr", "tw", "qs", "qp", "2027-10-09").encode()


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_full_flow_with_access_key(hass: HomeAssistant, cel_env) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    hems_id = result["description_placeholders"]["hems_id"]
    assert len(result["description_placeholders"]["public_key"]) == 43  # shown up-front, one exchange with the admin
    admin = Identity.generate()
    with patch(PROBE, return_value=None) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NAME: "Maison", CONF_ACCESS_KEY: key_for(hems_id, admin, "quartier")}
        )
    assert probe.call_args.args[0].id == hems_id
    assert result["step_id"] == "energy"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENERGY)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.version == 2 and entry.unique_id == hems_id
    assert entry.data[CONF_COMMUNITY_ID] == "quartier"  # taken from the key
    assert entry.data[CONF_ADMIN_PUBLIC_KEY] == admin.public_b64
    assert Identity.from_private_b64(entry.data[CONF_PRIVATE_KEY]).hems_id == hems_id
    assert entry.options[CONF_MIN_OFFER_W] == 500


@pytest.mark.parametrize(
    ("make_key", "errors"),
    [
        (lambda hid: "garbage", {CONF_ACCESS_KEY: "invalid_access_key"}),
        (lambda hid: key_for(Identity.generate().hems_id), {CONF_ACCESS_KEY: "wrong_identity"}),
    ],
)
async def test_key_validation(hass: HomeAssistant, make_key, errors) -> None:
    result = await _start(hass)
    hems_id = result["description_placeholders"]["hems_id"]
    with patch(PROBE, return_value=None) as probe:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_NAME: "M", CONF_ACCESS_KEY: make_key(hems_id)})
    assert result["errors"] == errors
    probe.assert_not_called()


@pytest.mark.parametrize("error", ["cannot_connect", "invalid_auth", "inbox_missing"])
async def test_storage_errors_then_recover(hass: HomeAssistant, cel_env, error) -> None:
    result = await _start(hass)
    hems_id = result["description_placeholders"]["hems_id"]
    data = {CONF_NAME: "M", CONF_ACCESS_KEY: key_for(hems_id)}
    with patch(PROBE, return_value=(error, "detail")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["errors"] == {"base": error}
    assert result["description_placeholders"]["hems_id"] == hems_id  # same identity while retrying
    with patch(PROBE, return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["step_id"] == "energy"


async def test_restore_identity_and_duplicate_abort(hass: HomeAssistant, cel_env) -> None:
    ident = Identity.generate()
    data = {CONF_NAME: "M", CONF_ACCESS_KEY: key_for(ident.hems_id), CONF_PRIVATE_KEY: ident.private_b64}
    result = await _start(hass)
    with patch(PROBE, return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], ENERGY)
    await hass.async_block_till_done()
    assert result["result"].unique_id == ident.hems_id
    result = await _start(hass)
    with patch(PROBE, return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**data, CONF_PRIVATE_KEY: "short"})
    assert result["errors"] == {CONF_PRIVATE_KEY: "invalid_private_key"}


async def test_energy_step_requires_a_role(hass: HomeAssistant) -> None:
    result = await _start(hass)
    hems_id = result["description_placeholders"]["hems_id"]
    with patch(PROBE, return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_NAME: "M", CONF_ACCESS_KEY: key_for(hems_id)})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**ENERGY, CONF_ROLES: []})
    assert result["errors"] == {CONF_ROLES: "no_role"}


async def test_migration_from_mqtt_asks_for_an_access_key(hass: HomeAssistant, cel_env) -> None:
    """A v0.5 (MQTT) entry keeps its identity and only needs the new access key."""
    ident = Identity.generate()
    old = MockConfigEntry(
        domain=DOMAIN,
        title="Maison",
        unique_id=ident.hems_id,
        version=1,
        data={
            CONF_NAME: "Maison", CONF_COMMUNITY_ID: "cel-test", "host": "broker", "port": 8883, "tls": True,
            "ca_cert": None, "username": ident.hems_id, "password": "pw", CONF_ADMIN_PUBLIC_KEY: cel_env.admin.public_b64,
            CONF_PRIVATE_KEY: ident.private_b64, **ENERGY,
        },
    )
    old.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(old.entry_id)
    await hass.async_block_till_done()
    assert old.version == 2 and "host" not in old.data and "password" not in old.data
    assert old.data[CONF_PRIVATE_KEY] == ident.private_b64
    assert old.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    reauth = next(f for f in flows if f["context"]["source"] == config_entries.SOURCE_REAUTH)
    assert reauth["step_id"] == "reauth_confirm"

    cel_env.identities["Maison"] = ident
    await cel_env.publish_registry()
    with patch(PROBE, return_value=None):
        result = await hass.config_entries.flow.async_configure(
            reauth["flow_id"], {CONF_ACCESS_KEY: cel_env.access_key(ident.hems_id)}
        )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert old.state is ConfigEntryState.LOADED
    assert old.runtime_data.node.enrolled


async def test_reauth_rejects_a_key_for_another_identity(hass: HomeAssistant, cel_env) -> None:
    entry = cel_env.entry("alice")
    entry.add_to_hass(hass)
    await cel_env.publish_registry()
    assert await hass.config_entries.async_setup(entry.entry_id)
    result = await entry.start_reauth_flow(hass)
    with patch(PROBE, return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_KEY: key_for(Identity.generate().hems_id)})
    assert result["errors"] == {CONF_ACCESS_KEY: "wrong_identity"}
    with patch(PROBE, return_value=("invalid_auth", "x")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_KEY: cel_env.access_key(entry.unique_id)})
    assert result["errors"] == {"base": "invalid_auth"}
    new_key = cel_env.access_key(entry.unique_id, "2098-12-31")
    with patch(PROBE, return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ACCESS_KEY: new_key})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCESS_KEY] == new_key and entry.state is ConfigEntryState.LOADED
    assert hass.states.get(f"sensor.{entry.title}_hems_identifier").attributes["access_key_expires"] == "2098-12-31"


async def test_options_flow_reloads_with_new_settings(hass: HomeAssistant, cel_env) -> None:
    entry = cel_env.entry("alice")
    entry.add_to_hass(hass)
    await cel_env.publish_registry()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    new = {
        CONF_ROLES: ["consumer"],
        CONF_GRID_POWER_ENTITY: "sensor.other",
        CONF_GRID_SIGN: SIGN_EXPORT_POSITIVE,
        CONF_OFFER_MARGIN_W: 200,
        CONF_MIN_OFFER_W: 1000,
        CONF_OFFER_HORIZON_MIN: 30,
        CONF_UPDATE_THRESHOLD_PCT: 5,
        CONF_SMOOTHING_S: 120,
    }
    bad = await hass.config_entries.options.async_configure(result["flow_id"], {**new, CONF_ROLES: []})
    assert bad["errors"] == {CONF_ROLES: "no_role"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], new)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    hub = entry.runtime_data
    assert hub.node.config.roles == frozenset({"consumer"})
    assert hub.grid_entity == "sensor.other" and hub.export_positive
    assert hass.states.get("switch.alice_publish_surplus_automatically") is None


async def test_expiring_access_key_is_reported_then_replaced(hass: HomeAssistant, cel_env) -> None:
    """A month before expiry a repair asks for a new key; Reconfigure replaces it."""
    soon = (dt_util.utcnow().date() + timedelta(days=10)).isoformat()
    entry = cel_env.entry("alice")
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ACCESS_KEY: cel_env.access_key(entry.unique_id, soon)}
    )
    await cel_env.publish_registry()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issue_id = f"{ISSUE_KEY_EXPIRING}_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.translation_placeholders["expires"] == soon
    assert hass.states.get("sensor.alice_hems_identifier").attributes["access_key_expires"] == soon

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    with patch(PROBE, return_value=None):
        bad = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: key_for(Identity.generate().hems_id)}
        )
        assert bad["errors"] == {CONF_ACCESS_KEY: "wrong_identity"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: cel_env.access_key(entry.unique_id, "2099-01-01")}
        )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert hass.states.get("sensor.alice_hems_identifier").attributes["access_key_expires"] == "2099-01-01"


async def test_valid_access_key_raises_no_repair(hass: HomeAssistant, cel_env) -> None:
    entry = cel_env.entry("alice")
    entry.add_to_hass(hass)
    await cel_env.publish_registry()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_KEY_EXPIRING}_{entry.entry_id}") is None
