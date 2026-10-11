"""Diagnostics (secrets redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import CelConfigEntry
from .const import CONF_ACCESS_KEY, CONF_PRIVATE_KEY

TO_REDACT = {CONF_PRIVATE_KEY, CONF_ACCESS_KEY}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: CelConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    hub = entry.runtime_data
    node = hub.node
    now = hub.now_ms()
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT), "options": dict(entry.options)},
        "hems_id": hub.hems_id,
        "public_key": hub.identity.public_b64,
        "connected": node.connected,
        "enrolled": node.enrolled,
        "registry": {
            "serial": node.registry.serial,
            "members": sorted(node.registry.members),
            "revoked": sorted(node.registry.revoked),
        },
        "peers": {
            pid: {"name": p.name, "online": p.online, "last_seen": p.last_seen, "offers": p.offers, "reputation_pct": p.reputation_pct}
            for pid, p in node.peers.items()
        },
        "offers": node.book.offer_book(now),
        "reservations_out": node.book.reservation_book(now),
        "reservations_in": [r.to_dict() for r in node.ledger.active.values()],
        "compliance_pct": node.compliance_pct,
        "dropped": dict(node.stats),
        "transport": {
            "connected": node.connected,
            "last_error": getattr(node.transport, "last_error", None),
            "auth_failed": getattr(node.transport, "auth_failed", False),
            "stats": dict(getattr(node.transport, "stats", {})),
        },
    }
