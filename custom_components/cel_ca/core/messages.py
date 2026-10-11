"""Signed message envelope and body schemas of the CEL protocol (v1).

Every message is a UTF-8 JSON envelope::

    {"v": 1, "typ": "...", "cid": "<community>", "src": "<hems_id>",
     "ts": <unix ms>, "nonce": "<hex>", "body": {...}, "sig": "<b64url>"}

``sig`` is the Ed25519 signature of the canonical JSON of the envelope without
``sig`` (sorted keys, no whitespace, UTF-8).  Floats are forbidden anywhere so
the canonical form is unambiguous across implementations: powers are integer
watts, energies integer watt-hours, times integer milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import re
import secrets
from typing import Any

from . import PROTOCOL_VERSION
from .identity import (
    Identity,
    IdentityError,
    b64u_decode,
    b64u_encode,
    is_valid_hems_id,
    verify_signature,
)

MAX_PAYLOAD_BYTES = 16 * 1024
MAX_OFFERS = 16
MAX_RESERVATIONS = 64
MAX_MEMBERS = 500
MAX_POWER_W = 1_000_000
MAX_ENERGY_WH = 100_000_000
MAX_TS = 1 << 53

COMMUNITY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PK_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
NONCE_RE = re.compile(r"^[0-9a-f]{16,64}$")
ROLES = frozenset({"producer", "consumer"})


class ProtocolError(ValueError):
    """Raised when a payload does not follow the protocol."""


class MsgType(StrEnum):
    """Message types."""

    REGISTRY = "registry"
    STATUS = "status"
    OFFER_BOOK = "offer_book"
    RESERVATION_BOOK = "reservation_book"
    SETTLEMENT = "settlement"
    RESERVE_REQUEST = "reserve_request"
    RESERVE_RESPONSE = "reserve_response"
    RESERVE_CANCEL = "reserve_cancel"
    RESERVATION_UPDATE = "reservation_update"


# Messages that describe a *state* (kept in the shared table, may be old when
# received) versus *commands* (must be fresh and are replay-protected).
STATE_TYPES = frozenset(
    {MsgType.REGISTRY, MsgType.STATUS, MsgType.OFFER_BOOK, MsgType.RESERVATION_BOOK}
)
COMMAND_TYPES = frozenset(
    {
        MsgType.RESERVE_REQUEST,
        MsgType.RESERVE_RESPONSE,
        MsgType.RESERVE_CANCEL,
        MsgType.RESERVATION_UPDATE,
        MsgType.SETTLEMENT,
    }
)
INBOX_TYPES = frozenset(
    {
        MsgType.RESERVE_REQUEST,
        MsgType.RESERVE_RESPONSE,
        MsgType.RESERVE_CANCEL,
        MsgType.RESERVATION_UPDATE,
    }
)


def canonical_json(obj: Any) -> bytes:
    """Canonical JSON encoding used for signatures."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def new_nonce() -> str:
    """Random nonce."""
    return secrets.token_hex(12)


def new_token(prefix: str = "") -> str:
    """Random identifier usable as offer / reservation id."""
    return f"{prefix}{secrets.token_hex(8)}"


@dataclass(frozen=True, slots=True)
class Envelope:
    """A parsed (not yet authenticated) envelope."""

    v: int
    typ: MsgType
    cid: str
    src: str
    ts: int
    nonce: str
    body: dict[str, Any]
    sig: bytes

    def signing_bytes(self) -> bytes:
        """Bytes covered by the signature."""
        return canonical_json(
            {
                "v": self.v,
                "typ": str(self.typ),
                "cid": self.cid,
                "src": self.src,
                "ts": self.ts,
                "nonce": self.nonce,
                "body": self.body,
            }
        )

    def verify(self, public_key: bytes) -> bool:
        """Check the signature against a public key."""
        return verify_signature(public_key, self.sig, self.signing_bytes())


def encode_envelope(
    identity: Identity,
    cid: str,
    typ: MsgType,
    body: dict[str, Any],
    ts: int,
    *,
    src: str | None = None,
    nonce: str | None = None,
) -> bytes:
    """Build, validate and sign an envelope. Returns the payload bytes."""
    validate_body(typ, body)
    unsigned = {
        "v": PROTOCOL_VERSION,
        "typ": str(typ),
        "cid": cid,
        "src": src or identity.hems_id,
        "ts": ts,
        "nonce": nonce or new_nonce(),
        "body": body,
    }
    sig = identity.sign(canonical_json(unsigned))
    payload = canonical_json({**unsigned, "sig": b64u_encode(sig)})
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ProtocolError("payload too large")
    return payload


def parse_envelope(payload: bytes) -> Envelope:
    """Parse and structurally validate an envelope (signature not checked)."""
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ProtocolError("payload too large")
    try:
        data = json.loads(payload.decode("utf-8"), parse_float=_reject_float)
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ProtocolError("invalid JSON") from err
    if not isinstance(data, dict):
        raise ProtocolError("envelope must be an object")
    expected = {"v", "typ", "cid", "src", "ts", "nonce", "body", "sig"}
    if set(data) != expected:
        raise ProtocolError("unexpected envelope fields")
    if data["v"] != PROTOCOL_VERSION or isinstance(data["v"], bool):
        raise ProtocolError("unsupported protocol version")
    try:
        typ = MsgType(data["typ"])
    except ValueError as err:
        raise ProtocolError("unknown message type") from err
    cid = data["cid"]
    if not isinstance(cid, str) or not COMMUNITY_RE.match(cid):
        raise ProtocolError("invalid community id")
    src = data["src"]
    if not is_valid_hems_id(src):
        raise ProtocolError("invalid source id")
    ts = _int(data, "ts", 0, MAX_TS)
    nonce = data["nonce"]
    if not isinstance(nonce, str) or not NONCE_RE.match(nonce):
        raise ProtocolError("invalid nonce")
    body = data["body"]
    if not isinstance(body, dict):
        raise ProtocolError("body must be an object")
    validate_body(typ, body)
    try:
        sig = b64u_decode(data["sig"])
    except IdentityError as err:
        raise ProtocolError("invalid signature encoding") from err
    return Envelope(PROTOCOL_VERSION, typ, cid, src, ts, nonce, body, sig)


def _reject_float(_: str) -> Any:
    raise ProtocolError("floats are not allowed")


# --------------------------------------------------------------------------
# Body validation helpers
# --------------------------------------------------------------------------


def _int(d: dict[str, Any], key: str, lo: int, hi: int) -> int:
    val = d.get(key)
    if not isinstance(val, int) or isinstance(val, bool) or not lo <= val <= hi:
        raise ProtocolError(f"invalid integer field '{key}'")
    return val


def _str(d: dict[str, Any], key: str, max_len: int, pattern: re.Pattern | None = None) -> str:
    val = d.get(key)
    if not isinstance(val, str) or len(val) > max_len:
        raise ProtocolError(f"invalid string field '{key}'")
    if pattern is not None and not pattern.match(val):
        raise ProtocolError(f"invalid format for field '{key}'")
    return val


def _hems(d: dict[str, Any], key: str) -> str:
    val = d.get(key)
    if not is_valid_hems_id(val):
        raise ProtocolError(f"invalid hems id field '{key}'")
    return val


def _keys(d: dict[str, Any], required: set[str], optional: set[str] = frozenset()) -> None:
    keys = set(d)
    if not required <= keys or not keys <= (required | optional):
        raise ProtocolError("unexpected or missing fields")


def _list(d: dict[str, Any], key: str, max_len: int) -> list[Any]:
    val = d.get(key)
    if not isinstance(val, list) or len(val) > max_len:
        raise ProtocolError(f"invalid list field '{key}'")
    return val


def _window(d: dict[str, Any]) -> None:
    start = _int(d, "start", 0, MAX_TS)
    end = _int(d, "end", 0, MAX_TS)
    if end <= start:
        raise ProtocolError("end must be after start")


def _roles(d: dict[str, Any]) -> None:
    roles = _list(d, "roles", 2)
    if not all(isinstance(r, str) and r in ROLES for r in roles) or len(set(roles)) != len(roles):
        raise ProtocolError("invalid roles")


SLOT_MINUTES = frozenset({5, 10, 15, 20, 30, 60})  # divisors of an hour: slots align on full hours


def _v_params(p: Any) -> None:
    if not isinstance(p, dict):
        raise ProtocolError("invalid params")
    _keys(p, set(), {"slot_minutes"})
    if "slot_minutes" in p:
        value = p["slot_minutes"]
        if isinstance(value, bool) or value not in SLOT_MINUTES:
            raise ProtocolError("invalid slot_minutes")


def _v_registry(b: dict[str, Any]) -> None:
    _keys(b, {"serial", "members", "revoked"}, {"params"})
    if "params" in b:
        _v_params(b["params"])
    _int(b, "serial", 1, MAX_TS)
    for m in _list(b, "members", MAX_MEMBERS):
        if not isinstance(m, dict):
            raise ProtocolError("invalid member")
        _keys(m, {"id", "pk", "name", "roles", "valid_until"}, {"meter"})
        _hems(m, "id")
        _str(m, "pk", 64, PK_RE)
        _str(m, "name", 64)
        _roles(m)
        _int(m, "valid_until", 0, MAX_TS)
        if "meter" in m:
            _str(m, "meter", 64)
    for r in _list(b, "revoked", MAX_MEMBERS):
        if not is_valid_hems_id(r):
            raise ProtocolError("invalid revoked id")


def _v_status(b: dict[str, Any]) -> None:
    _keys(b, {"name", "roles", "sw"})
    _str(b, "name", 64)
    _roles(b)
    _str(b, "sw", 32)


def _v_offer_book(b: dict[str, Any]) -> None:
    _keys(b, {"offers"})
    for o in _list(b, "offers", MAX_OFFERS):
        if not isinstance(o, dict):
            raise ProtocolError("invalid offer")
        _keys(o, {"oid", "start", "end", "power_w", "available_w", "rolling"})
        if not isinstance(o["rolling"], bool):
            raise ProtocolError("invalid rolling flag")
        _str(o, "oid", 40, TOKEN_RE)
        _window(o)
        power = _int(o, "power_w", 0, MAX_POWER_W)
        if _int(o, "available_w", 0, MAX_POWER_W) > power:
            raise ProtocolError("available_w exceeds power_w")


def _v_reservation_book(b: dict[str, Any]) -> None:
    _keys(b, {"reservations"})
    for r in _list(b, "reservations", MAX_RESERVATIONS):
        if not isinstance(r, dict):
            raise ProtocolError("invalid reservation")
        _keys(r, {"rid", "oid", "consumer", "start", "end", "power_w"})
        _str(r, "rid", 40, TOKEN_RE)
        _str(r, "oid", 40, TOKEN_RE)
        _hems(r, "consumer")
        _window(r)
        _int(r, "power_w", 1, MAX_POWER_W)


def _v_settlement(b: dict[str, Any]) -> None:
    _keys(b, {"rid", "role", "counterpart", "reserved_wh", "measured_wh", "ratio_pct"})
    _str(b, "rid", 40, TOKEN_RE)
    if b.get("role") not in ROLES:
        raise ProtocolError("invalid role")
    _hems(b, "counterpart")
    _int(b, "reserved_wh", 0, MAX_ENERGY_WH)
    _int(b, "measured_wh", 0, MAX_ENERGY_WH)
    _int(b, "ratio_pct", 0, 100)


def _v_reserve_request(b: dict[str, Any]) -> None:
    _keys(b, {"rid", "to", "oid", "start", "end", "power_w", "min_power_w"})
    _str(b, "rid", 40, TOKEN_RE)
    _hems(b, "to")
    _str(b, "oid", 40, TOKEN_RE)
    _window(b)
    power = _int(b, "power_w", 1, MAX_POWER_W)
    if _int(b, "min_power_w", 1, MAX_POWER_W) > power:
        raise ProtocolError("min_power_w exceeds power_w")


RESPONSE_STATUSES = frozenset({"accepted", "partial", "rejected"})
UPDATE_STATUSES = frozenset({"curtailed", "cancelled"})


def _v_reserve_response(b: dict[str, Any]) -> None:
    _keys(b, {"rid", "to", "status", "power_w", "start", "end", "reason"})
    _str(b, "rid", 40, TOKEN_RE)
    _hems(b, "to")
    if b.get("status") not in RESPONSE_STATUSES:
        raise ProtocolError("invalid status")
    _int(b, "power_w", 0, MAX_POWER_W)
    _int(b, "start", 0, MAX_TS)
    _int(b, "end", 0, MAX_TS)
    _str(b, "reason", 40)
    if b["status"] != "rejected":
        _window(b)
        if b["power_w"] < 1:
            raise ProtocolError("granted power must be positive")


def _v_reserve_cancel(b: dict[str, Any]) -> None:
    _keys(b, {"rid", "to"})
    _str(b, "rid", 40, TOKEN_RE)
    _hems(b, "to")


def _v_reservation_update(b: dict[str, Any]) -> None:
    _keys(b, {"rid", "to", "status", "power_w", "end", "reason"})
    _str(b, "rid", 40, TOKEN_RE)
    _hems(b, "to")
    if b.get("status") not in UPDATE_STATUSES:
        raise ProtocolError("invalid status")
    _int(b, "power_w", 0, MAX_POWER_W)
    _int(b, "end", 0, MAX_TS)
    _str(b, "reason", 40)


_VALIDATORS = {
    MsgType.REGISTRY: _v_registry,
    MsgType.STATUS: _v_status,
    MsgType.OFFER_BOOK: _v_offer_book,
    MsgType.RESERVATION_BOOK: _v_reservation_book,
    MsgType.SETTLEMENT: _v_settlement,
    MsgType.RESERVE_REQUEST: _v_reserve_request,
    MsgType.RESERVE_RESPONSE: _v_reserve_response,
    MsgType.RESERVE_CANCEL: _v_reserve_cancel,
    MsgType.RESERVATION_UPDATE: _v_reservation_update,
}


def validate_body(typ: MsgType, body: dict[str, Any]) -> None:
    """Validate a body for a given message type (raises ProtocolError)."""
    if not isinstance(body, dict):
        raise ProtocolError("body must be an object")
    _check_no_float(body)
    _VALIDATORS[typ](body)


def _check_no_float(obj: Any, depth: int = 0) -> None:
    if depth > 6:
        raise ProtocolError("body too deeply nested")
    if isinstance(obj, float):
        raise ProtocolError("floats are not allowed")
    if isinstance(obj, dict):
        for v in obj.values():
            _check_no_float(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _check_no_float(v, depth + 1)
