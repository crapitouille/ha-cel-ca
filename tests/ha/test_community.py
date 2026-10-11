"""Community view: one device per participant, totals and the generated dashboard."""

from __future__ import annotations

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.template import Template
import pytest
import yaml

from custom_components.cel_ca.const import DOMAIN
from custom_components.cel_ca.core.identity import Identity
from custom_components.cel_ca.core.node import CelNode, NodeConfig
from custom_components.cel_ca.core.transport import InMemoryTransport
from custom_components.cel_ca.sensor import CelSensor

from .conftest import CID
from .test_integration import set_grid, setup_entries, state, tick_all


def device(hass: HomeAssistant, ident: str) -> dr.DeviceEntry | None:
    """Device of the integration with this identifier (same on every HA 2026 version)."""
    registry = dr.async_get(hass)
    devices = (
        d
        for entry in hass.config_entries.async_entries(DOMAIN)
        for d in dr.async_entries_for_config_entry(registry, entry.entry_id)
    )
    return next((d for d in devices if (DOMAIN, ident) in d.identifiers), None)


def entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


def peer_state(hass, env, viewer: str, peer: str, domain: str, key: str):
    uid = f"{env.identities[viewer].hems_id}_peer_{env.identities[peer].hems_id}_{key}"
    eid = entity_id(hass, domain, uid)
    assert eid is not None, uid
    return hass.states.get(eid)


async def external_node(env, name: str) -> CelNode:
    """A HEMS running in *another* Home Assistant, on the same broker."""
    node = CelNode(env.identities[name], CID, env.admin.public_key, InMemoryTransport(env.broker), NodeConfig(name=name))
    await node.async_start()
    return node


async def test_participant_devices_and_values(hass: HomeAssistant, cel_env) -> None:
    entries = await setup_entries(hass, cel_env, "alice", "bob", "carol")
    await set_grid(hass, "sensor.alice_grid", -3100)
    await tick_all(hass)
    await hass.services.async_call(
        DOMAIN, "reserve", {"config_entry_id": entries["bob"].entry_id, "power_w": 2000}, blocking=True, return_response=True
    )
    await hass.async_block_till_done()

    # carol sees alice and bob as participants, not herself
    assert peer_state(hass, cel_env, "carol", "alice", "sensor", "offered").state == "3000"
    assert peer_state(hass, cel_env, "carol", "alice", "sensor", "available").state == "1000"
    assert peer_state(hass, cel_env, "carol", "alice", "sensor", "reserved_out").state == "2000"
    assert peer_state(hass, cel_env, "carol", "bob", "sensor", "reserved_in").state == "2000"
    assert peer_state(hass, cel_env, "carol", "bob", "binary_sensor", "online").state == STATE_ON
    assert peer_state(hass, cel_env, "carol", "bob", "sensor", "reputation").state == "unknown"
    assert peer_state(hass, cel_env, "carol", "bob", "sensor", "last_seen").state not in ("unknown", "unavailable")
    carol_id = cel_env.identities["carol"].hems_id
    assert entity_id(hass, "sensor", f"{carol_id}_peer_{carol_id}_offered") is None

    # one device per participant, attached to the local HEMS device
    alice_dev = device(hass, f"{carol_id}:{cel_env.identities['alice'].hems_id}")
    carol_dev = device(hass, carol_id)
    assert alice_dev.name == "alice" and alice_dev.via_device_id == carol_dev.id

    # community totals, computed identically by every HEMS
    for name in ("alice", "bob", "carol"):
        hems = cel_env.identities[name].hems_id
        participants = hass.states.get(entity_id(hass, "sensor", f"{hems}_participants"))
        assert participants.state == "3"
        assert participants.attributes["members"] == 3
        rows = participants.attributes["participants"]
        assert [r["name"] for r in rows] == ["alice", "bob", "carol"]
        assert [r["is_self"] for r in rows] == [n == name for n in ("alice", "bob", "carol")]
        assert hass.states.get(entity_id(hass, "sensor", f"{hems}_community_offered")).state == "3000"
        assert hass.states.get(entity_id(hass, "sensor", f"{hems}_community_reserved")).state == "2000"


def test_large_attributes_are_not_recorded() -> None:
    assert {"participants", "offers", "reservations", "peers"} <= CelSensor._unrecorded_attributes


async def test_participant_joins_goes_online_and_is_revoked(hass: HomeAssistant, cel_env) -> None:
    await setup_entries(hass, cel_env, "alice")
    alice = cel_env.identities["alice"].hems_id
    # a neighbour is enrolled: his device appears, offline
    cel_env.identities["voisin"] = Identity.generate()
    await cel_env.publish_registry()
    await hass.async_block_till_done()
    assert peer_state(hass, cel_env, "alice", "voisin", "binary_sensor", "online").state == STATE_OFF
    assert state(hass, "sensor.alice_participants_online").attributes["members"] == 2

    # he connects and offers surplus
    node = await external_node(cel_env, "voisin")
    node.record_grid_power(-2600)
    await node.async_tick()
    await hass.async_block_till_done()
    assert peer_state(hass, cel_env, "alice", "voisin", "binary_sensor", "online").state == STATE_ON
    assert peer_state(hass, cel_env, "alice", "voisin", "sensor", "offered").state == "2500"
    assert state(hass, "sensor.alice_participants_online").state == "2"

    # he is revoked: device and entities are removed
    voisin = cel_env.identities["voisin"].hems_id
    await cel_env.publish_registry(revoked=[voisin])
    await hass.async_block_till_done()
    assert device(hass, f"{alice}:{voisin}") is None
    assert entity_id(hass, "sensor", f"{alice}_peer_{voisin}_offered") is None
    assert state(hass, "sensor.alice_participants_online").attributes["members"] == 1

    # and re-enrolled later: entities come back
    await cel_env.publish_registry()
    await hass.async_block_till_done()
    assert entity_id(hass, "sensor", f"{alice}_peer_{voisin}_offered") is not None
    await node.async_stop()


async def test_generated_dashboard(hass: HomeAssistant, cel_env) -> None:
    await setup_entries(hass, cel_env, "maison")
    cel_env.identities["voisin"] = Identity.generate()
    await cel_env.publish_registry()
    node = await external_node(cel_env, "voisin")
    node.record_grid_power(-3100)
    await node.async_tick()
    await hass.async_block_till_done()

    response = await hass.services.async_call(DOMAIN, "generate_dashboard", {}, blocking=True, return_response=True)
    # the response is directly a dashboard configuration (copy / paste into the raw editor)
    assert set(response) == {"title", "views"}
    dashboard = yaml.safe_load(yaml.safe_dump(response, allow_unicode=True))
    assert dashboard == response
    cards = dashboard["views"][0]["sections"][0]["cards"]
    # every referenced entity exists
    referenced = []
    for card in cards:
        if "entity" in card:
            referenced.append(card["entity"])
        referenced += [e["entity"] for e in card.get("entities", [])]
    assert referenced and all(hass.states.get(e) is not None for e in referenced), referenced
    graphs = {c["title"]: c for c in cards if c["type"] == "history-graph"}
    offered = graphs["Surplus proposé — 24 h"]["entities"]
    assert [e["name"] for e in offered] == ["maison (moi)", "voisin"]
    assert len(graphs["Présence des participants — 24 h"]["entities"]) == 1

    # the markdown table renders with the real data
    markdown = next(c for c in cards if c["type"] == "markdown")
    rendered = Template(markdown["content"], hass).async_render(parse_result=False)
    assert "| 🟢 | maison **(moi)** |" in rendered
    assert rendered.count("maintenant") == 1  # own row; peers show the real delay
    tiles = [c for c in cards if c["type"] == "tile"]
    assert [t["name"] for t in tiles] == ["Participants en ligne", "Surplus proposé", "Surplus disponible", "Puissance réservée", "Connexion CEL"]
    assert "| 🟢 | voisin | 3000 W | 3000 W |" in rendered
    assert "2 / 2 participants en ligne" in rendered
    await node.async_stop()


async def test_french_participant_entity_ids(hass: HomeAssistant, cel_env) -> None:
    hass.config.language = "fr"
    cel_env.identities["Maison Dupont"] = Identity.generate()
    await setup_entries(hass, cel_env, "maison")
    await hass.async_block_till_done()
    for entity in (
        "binary_sensor.maison_dupont_en_ligne",
        "sensor.maison_dupont_surplus_propose",
        "sensor.maison_dupont_surplus_disponible",
        "sensor.maison_dupont_puissance_reservee_production",
        "sensor.maison_dupont_puissance_reservee_consommation",
        "sensor.maison_dupont_respect_des_engagements",
        "sensor.maison_dupont_dernier_contact",
        "sensor.maison_participants_en_ligne",
        "sensor.maison_surplus_total_propose",
        "sensor.maison_puissance_reservee_dans_la_communaute",
    ):
        state(hass, entity)


@pytest.mark.parametrize("count", [0, 1])
async def test_dashboard_without_peers(hass: HomeAssistant, cel_env, count: int) -> None:
    await setup_entries(hass, cel_env, "solo", register=bool(count))
    response = await hass.services.async_call(DOMAIN, "generate_dashboard", {}, blocking=True, return_response=True)
    cards = response["views"][0]["sections"][0]["cards"]
    assert cards
    assert any(c["type"] == "markdown" for c in cards)  # the table is always there (possibly empty)
