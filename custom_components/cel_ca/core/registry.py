"""Membership registry of a CEL, signed by the CEL administrator.

The registry is the trust anchor of the network: it binds each HEMS id to its
public key, a display name, its roles and an optional metering point.  Every
HEMS is configured with the administrator public key and only accepts a
registry signed by that key, with a strictly increasing serial number
(anti-rollback).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .identity import IdentityError, b64u_decode, b64u_encode, hems_id_from_public_key
from .messages import MsgType, ProtocolError, validate_body


@dataclass(frozen=True, slots=True)
class Member:
    """A registered HEMS."""

    hems_id: str
    public_key: bytes
    name: str
    roles: frozenset[str]
    valid_until: int = 0  # unix ms, 0 = no expiry
    meter: str | None = None

    def is_valid(self, now_ms: int) -> bool:
        """Membership not expired."""
        return self.valid_until == 0 or now_ms < self.valid_until

    def to_body(self) -> dict[str, Any]:
        """Serialize for the registry body."""
        data: dict[str, Any] = {
            "id": self.hems_id,
            "pk": b64u_encode(self.public_key),
            "name": self.name,
            "roles": sorted(self.roles),
            "valid_until": self.valid_until,
        }
        if self.meter:
            data["meter"] = self.meter
        return data


@dataclass(slots=True)
class Registry:
    """Verified registry content."""

    serial: int = 0
    members: dict[str, Member] = field(default_factory=dict)
    revoked: frozenset[str] = frozenset()
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def slot_ms(self) -> int:
        """Market slot length in ms (0 = continuous market, no slots)."""
        return int(self.params.get("slot_minutes", 0)) * 60_000

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> Registry:
        """Build from a (signature-verified) registry body."""
        validate_body(MsgType.REGISTRY, body)
        members: dict[str, Member] = {}
        for m in body["members"]:
            try:
                pk = b64u_decode(m["pk"])
                derived = hems_id_from_public_key(pk)
            except IdentityError as err:
                raise ProtocolError("invalid member key") from err
            # The id MUST be derived from the key: nobody can register a
            # well-known id with a different key.
            if derived != m["id"]:
                raise ProtocolError(f"member id does not match key: {m['id']}")
            if m["id"] in members:
                raise ProtocolError(f"duplicate member: {m['id']}")
            members[m["id"]] = Member(
                hems_id=m["id"],
                public_key=pk,
                name=m["name"],
                roles=frozenset(m["roles"]),
                valid_until=m["valid_until"],
                meter=m.get("meter"),
            )
        return cls(
            serial=body["serial"],
            members=members,
            revoked=frozenset(body["revoked"]),
            params=dict(body.get("params", {})),
        )

    def to_body(self) -> dict[str, Any]:
        """Serialize to a registry body."""
        return build_registry_body(self.serial, self.members.values(), self.revoked, self.params)

    def get_active(self, hems_id: str, now_ms: int) -> Member | None:
        """Return the member if registered, not revoked and not expired."""
        if hems_id in self.revoked:
            return None
        member = self.members.get(hems_id)
        if member is None or not member.is_valid(now_ms):
            return None
        return member

    def has_role(self, hems_id: str, role: str, now_ms: int) -> bool:
        """True if the member is active and holds the role."""
        member = self.get_active(hems_id, now_ms)
        return member is not None and role in member.roles


def build_registry_body(
    serial: int, members: Iterable[Member], revoked: Iterable[str], params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a canonical registry body (used by the admin tool)."""
    body: dict[str, Any] = {
        "serial": serial,
        "members": [m.to_body() for m in sorted(members, key=lambda m: m.hems_id)],
        "revoked": sorted(set(revoked)),
    }
    if params:  # omitted when empty: registries stay readable by older HEMS
        body["params"] = dict(params)
    validate_body(MsgType.REGISTRY, body)
    return body
