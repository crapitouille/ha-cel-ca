"""Identity, canonical encoding and envelope validation."""

from __future__ import annotations

import json

import pytest

from custom_components.cel_ca.core.identity import (
    Identity,
    IdentityError,
    b64u_decode,
    b64u_encode,
    hems_id_from_public_key,
    is_valid_hems_id,
    parse_public_key,
)
from custom_components.cel_ca.core.messages import (
    MAX_PAYLOAD_BYTES,
    MsgType,
    ProtocolError,
    canonical_json,
    encode_envelope,
    parse_envelope,
    validate_body,
)

from .helpers import CID, T0


def test_identity_roundtrip_and_id_derivation() -> None:
    ident = Identity.generate()
    again = Identity.from_private_b64(ident.private_b64)
    assert again.hems_id == ident.hems_id
    assert again.public_key == ident.public_key
    assert is_valid_hems_id(ident.hems_id)
    assert ident.hems_id == hems_id_from_public_key(ident.public_key)
    assert len(ident.hems_id) == len("hems-") + 24
    assert ident.private_b64 not in repr(ident)


def test_ids_are_unique() -> None:
    assert len({Identity.generate().hems_id for _ in range(200)}) == 200


@pytest.mark.parametrize("bad", ["", "abc", "!!!!", "a" * 44, b64u_encode(b"x" * 31)])
def test_parse_public_key_rejects_garbage(bad: str) -> None:
    with pytest.raises(IdentityError):
        parse_public_key(bad)


def test_b64u_strict() -> None:
    assert b64u_decode(b64u_encode(b"\x00\xff" * 5)) == b"\x00\xff" * 5
    with pytest.raises(IdentityError):
        b64u_decode("ab+/")


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"b": 1, "a": [2, {"d": 1, "c": 2}]}) == canonical_json(
        {"a": [2, {"c": 2, "d": 1}], "b": 1}
    )


def _status(ident: Identity, **kw) -> bytes:
    return encode_envelope(ident, CID, MsgType.STATUS, {"name": "x", "roles": ["producer"], "sw": "1"}, T0, **kw)


def test_envelope_sign_and_verify() -> None:
    ident = Identity.generate()
    env = parse_envelope(_status(ident))
    assert env.src == ident.hems_id and env.typ is MsgType.STATUS
    assert env.verify(ident.public_key)
    assert not env.verify(Identity.generate().public_key)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["body"].update(name="evil"),
        lambda d: d.update(ts=d["ts"] + 1),
        lambda d: d.update(cid="other"),
        lambda d: d.update(nonce="0" * 24),
    ],
)
def test_tampering_breaks_signature(mutate) -> None:
    ident = Identity.generate()
    data = json.loads(_status(ident))
    mutate(data)
    env = parse_envelope(canonical_json(data))
    assert not env.verify(ident.public_key)


def test_spoofed_src_fails_verification() -> None:
    victim, attacker = Identity.generate(), Identity.generate()
    payload = _status(attacker, src=victim.hems_id)
    env = parse_envelope(payload)
    assert env.src == victim.hems_id
    assert not env.verify(victim.public_key)


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b'{"v":1}',
        b"\xff\xfe",
    ],
)
def test_parse_rejects_malformed(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        parse_envelope(payload)


def test_parse_rejects_float_and_extra_fields_and_version() -> None:
    ident = Identity.generate()
    data = json.loads(_status(ident))
    for mutate in (
        lambda d: d.update(ts=1.5),
        lambda d: d.update(extra=1),
        lambda d: d.update(v=2),
        lambda d: d.update(v=True),
        lambda d: d.update(typ="nope"),
        lambda d: d.update(src="hems-UPPER"),
        lambda d: d["body"].update(extra=1),
    ):
        copy = json.loads(json.dumps(data))
        mutate(copy)
        with pytest.raises(ProtocolError):
            parse_envelope(json.dumps(copy).encode())


def test_float_rejected_even_inside_json_text() -> None:
    ident = Identity.generate()
    text = _status(ident).decode().replace(f'"ts":{T0}', f'"ts":{T0}.0')
    with pytest.raises(ProtocolError):
        parse_envelope(text.encode())


def test_oversized_payload_rejected() -> None:
    with pytest.raises(ProtocolError):
        parse_envelope(b" " * (MAX_PAYLOAD_BYTES + 1))


@pytest.mark.parametrize(
    ("typ", "body"),
    [
        (MsgType.OFFER_BOOK, {"offers": [{"oid": "o1", "start": 10, "end": 5, "power_w": 1, "available_w": 1, "rolling": False}]}),
        (MsgType.OFFER_BOOK, {"offers": [{"oid": "o1", "start": 1, "end": 5, "power_w": 1, "available_w": 2, "rolling": False}]}),
        (MsgType.OFFER_BOOK, {"offers": [{"oid": "o 1", "start": 1, "end": 5, "power_w": 1, "available_w": 1, "rolling": False}]}),
        (MsgType.OFFER_BOOK, {"offers": [{"oid": "o1", "start": 1, "end": 5, "power_w": -1, "available_w": 0, "rolling": False}]}),
        (MsgType.RESERVE_REQUEST, {"rid": "r", "to": "x", "oid": "o", "start": 1, "end": 2, "power_w": 1, "min_power_w": 1}),
        (MsgType.STATUS, {"name": "x", "roles": ["admin"], "sw": "1"}),
        (MsgType.STATUS, {"name": "x", "roles": ["producer", "producer"], "sw": "1"}),
        (MsgType.SETTLEMENT, {"rid": "r", "role": "producer", "counterpart": "x", "reserved_wh": 1, "measured_wh": 1, "ratio_pct": 101}),
    ],
)
def test_body_validation_rejects(typ, body) -> None:
    with pytest.raises(ProtocolError):
        validate_body(typ, body)


def test_valid_offer_book_and_rolling_flag() -> None:
    offer = {"oid": "o1", "start": 1, "end": 5, "power_w": 2, "available_w": 1, "rolling": True}
    validate_body(MsgType.OFFER_BOOK, {"offers": [offer]})
    with pytest.raises(ProtocolError):
        validate_body(MsgType.OFFER_BOOK, {"offers": [{**offer, "rolling": 1}]})


def test_request_min_power_cannot_exceed_power() -> None:
    to = Identity.generate().hems_id
    body = {"rid": "r1", "to": to, "oid": "o1", "start": 1, "end": 2, "power_w": 100, "min_power_w": 101}
    with pytest.raises(ProtocolError):
        validate_body(MsgType.RESERVE_REQUEST, body)
    body["min_power_w"] = 100
    validate_body(MsgType.RESERVE_REQUEST, body)


def test_encode_refuses_invalid_body() -> None:
    with pytest.raises(ProtocolError):
        encode_envelope(Identity.generate(), CID, MsgType.STATUS, {"name": 1}, T0)


def test_sw_version_matches_manifest() -> None:
    """The version announced to peers (and shown as firmware in HA) follows the manifest."""
    import json
    from pathlib import Path

    from custom_components.cel_ca.core.node import SW_VERSION

    manifest = json.loads((Path(__file__).resolve().parents[2] / "custom_components" / "cel_ca" / "manifest.json").read_text())
    assert SW_VERSION == manifest["version"]
