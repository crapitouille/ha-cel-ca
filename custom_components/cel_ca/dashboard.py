"""Community dashboard generator (Lovelace YAML using only built-in cards)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .hub import CelHub

HOURS = 24

TABLE_TEMPLATE = """{%- set rows = state_attr('__ENTITY__', 'participants') or [] -%}
| | Participant | Proposé | Disponible | Réservé chez lui | Réservé par lui | Respect | Dernier contact |
|:-:|---|--:|--:|--:|--:|--:|---|
{%- for r in rows %}
| {{ '🟢' if r.online else '🔴' }} | {{ r.name }}{{ ' **(moi)**' if r.is_self }} | {{ r.offered_w }} W | {{ r.available_w }} W | {{ r.reserved_out_w }} W | {{ r.reserved_in_w }} W | {{ (r.reputation_pct ~ ' %') if r.reputation_pct is not none else '—' }} | {{ 'maintenant' if r.is_self and r.online else (relative_time(as_datetime(r.last_seen)) if r.last_seen else '—') }} |
{%- endfor %}

{{ rows | selectattr('online') | list | count }} / {{ rows | count }} participants en ligne"""


def _lookup(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


def build_dashboard(hass: HomeAssistant, hub: CelHub) -> dict[str, Any]:
    """Dashboard configuration with the real entity ids of this installation."""
    me = hub.hems_id

    def own(domain: str, key: str) -> str | None:
        return _lookup(hass, domain, f"{me}_{key}")

    def peer(domain: str, pid: str, key: str) -> str | None:
        return _lookup(hass, domain, f"{me}_peer_{pid}_{key}")

    peers = [row for row in hub.participant_rows if not row["is_self"]]
    participants_entity = own("sensor", "participants")

    def series(own_key: str, peer_key: str) -> list[dict[str, str]]:
        items = []
        if entity := own("sensor", own_key):
            items.append({"entity": entity, "name": f"{hub.entry.title} (moi)"})
        for row in peers:
            if entity := peer("sensor", row["hems_id"], peer_key):
                items.append({"entity": entity, "name": row["name"]})
        return items

    summary = [
        {"entity": entity, "name": name}
        for entity, name in (
            (participants_entity, "Participants en ligne"),
            (own("sensor", "community_offered"), "Surplus proposé"),
            (own("sensor", "community_surplus"), "Surplus disponible"),
            (own("sensor", "community_reserved"), "Puissance réservée"),
            (own("binary_sensor", "connected"), "Connexion CEL"),
        )
        if entity
    ]
    presence = [
        {"entity": entity, "name": row["name"]}
        for row in peers
        if (entity := peer("binary_sensor", row["hems_id"], "online"))
    ]

    cards: list[dict[str, Any]] = [
        {"type": "tile", "entity": item["entity"], "name": item["name"], "vertical": False} for item in summary
    ]
    if participants_entity:
        cards.append(
            {
                "type": "markdown",
                "title": "Participants",
                "content": TABLE_TEMPLATE.replace("__ENTITY__", participants_entity),
                "grid_options": {"columns": "full"},
            }
        )
    for title, own_key, peer_key in (
        ("Surplus proposé", "offered_power", "offered"),
        ("Puissance réservée (consommation)", "reserved_in", "reserved_in"),
        ("Puissance réservée (production)", "reserved_out", "reserved_out"),
    ):
        entities = series(own_key, peer_key)
        if entities:
            cards.append(
                {
                    "type": "history-graph",
                    "title": f"{title} — {HOURS} h",
                    "hours_to_show": HOURS,
                    "entities": entities,
                    "grid_options": {"columns": "full"},
                }
            )
    if presence:
        cards.append(
            {
                "type": "history-graph",
                "title": f"Présence des participants — {HOURS} h",
                "hours_to_show": HOURS,
                "entities": presence,
                "grid_options": {"columns": "full"},
            }
        )

    return {
        "title": f"Communauté {hub.community_id}",
        "views": [
            {
                "title": "Communauté",
                "path": "communaute",
                "icon": "mdi:home-group",
                "type": "sections",
                "max_columns": 4,
                "sections": [{"type": "grid", "cards": cards}],
            }
        ],
    }

