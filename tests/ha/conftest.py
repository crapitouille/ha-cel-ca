"""Fixtures for Home Assistant level tests.

The MQTT transport is replaced by an in-memory broker shared by all config
entries, so several HEMS can live in the same test Home Assistant instance.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
import time
from unittest.mock import patch

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
from custom_components.cel_ca.core.storage_access import AccessKey
from custom_components.cel_ca.core.transport import InMemoryBroker, InMemoryTransport

CID = "cel-ha"


@pytest.fixture(autouse=True)
def no_deprecated_ha_api(caplog) -> Generator[None]:
    """Fail on any Home Assistant deprecation report about this integration.

    For custom integrations HA only logs these as warnings, then turns them into
    errors in a later release (as 2026.10 did for DeviceInfo.via_device).
    """
    yield
    reports = [
        r.getMessage()
        for r in caplog.get_records("call")
        if r.name == "homeassistant.helpers.frame" and "cel_ca" in r.getMessage()
    ]
    assert not reports, reports


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom_components in every test."""
    return


@dataclass
class CelEnv:
    broker: InMemoryBroker
    admin: Identity
    identities: dict[str, Identity] = field(default_factory=dict)
    serial: int = 0

    async def publish_registry(self, names: list[str] | None = None, revoked=(), params: dict | None = None) -> None:
        names = list(self.identities) if names is None else names
        self.serial += 1
        members = [
            Member(self.identities[n].hems_id, self.identities[n].public_key, n, frozenset({"producer", "consumer"}))
            for n in names
        ]
        body = build_registry_body(self.serial, members, revoked, params)
        payload = encode_envelope(self.admin, CID, MsgType.REGISTRY, body, int(time.time() * 1000))
        await self.broker.route(None, f"cel/{CID}/registry", payload, True)

    def access_key(self, hems_id: str, exp: str = "2099-01-01") -> str:
        """A syntactically valid access key (the transport is in-memory in these tests)."""
        return AccessKey(
            CID, hems_id, self.admin.public_b64, "https://t.example", "https://q.example", "tr", "tw", "qs", "qp", exp
        ).encode()

    def entry(self, name: str, *, grid: str | None = None, roles=("producer", "consumer"), **data) -> MockConfigEntry:
        ident = self.identities.setdefault(name, Identity.generate())
        return MockConfigEntry(
            domain=DOMAIN,
            title=name,
            unique_id=ident.hems_id,
            version=2,
            data={
                CONF_NAME: name,
                CONF_COMMUNITY_ID: CID,
                CONF_ACCESS_KEY: self.access_key(ident.hems_id),
                CONF_ADMIN_PUBLIC_KEY: self.admin.public_b64,
                CONF_PRIVATE_KEY: ident.private_b64,
                CONF_ROLES: list(roles),
                CONF_GRID_POWER_ENTITY: grid or f"sensor.{name}_grid",
                CONF_GRID_SIGN: SIGN_IMPORT_POSITIVE,
                **data,
            },
            options=dict(DEFAULT_OPTIONS),
        )


@pytest.fixture
def cel_env() -> Generator[CelEnv]:
    """Shared in-memory broker and admin; create_transport is patched."""
    env = CelEnv(InMemoryBroker(), Identity.generate())

    async def fake_transport(hass, entry):
        return InMemoryTransport(env.broker)

    with patch("custom_components.cel_ca.hub.create_transport", side_effect=fake_transport):
        yield env
