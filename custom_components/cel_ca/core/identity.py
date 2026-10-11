"""Cryptographic identity of a HEMS (Ed25519).

A HEMS identifier is *derived* from its public key, so it cannot be claimed by
anybody who does not hold the matching private key:

    hems_id = "hems-" + base32(sha256(public_key)[:15]).lower()   (24 chars)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

HEMS_ID_RE = re.compile(r"^hems-[a-z2-7]{24}$")
_B64U_RE = re.compile(r"^[A-Za-z0-9_-]*$")

KEY_LEN = 32
SIG_LEN = 64


class IdentityError(ValueError):
    """Raised for malformed keys or identifiers."""


def b64u_encode(data: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """Decode unpadded base64url, strictly."""
    if not isinstance(text, str) or not _B64U_RE.match(text):
        raise IdentityError("invalid base64url string")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as err:
        raise IdentityError("invalid base64url string") from err


def hems_id_from_public_key(public_key: bytes) -> str:
    """Derive the stable HEMS identifier from a raw Ed25519 public key."""
    if len(public_key) != KEY_LEN:
        raise IdentityError("public key must be 32 bytes")
    digest = hashlib.sha256(public_key).digest()[:15]
    return "hems-" + base64.b32encode(digest).decode("ascii").lower()


def is_valid_hems_id(value: object) -> bool:
    """Return True if value looks like a HEMS identifier."""
    return isinstance(value, str) and bool(HEMS_ID_RE.match(value))


def parse_public_key(text: str) -> bytes:
    """Parse a base64url raw public key and check it is a valid Ed25519 key."""
    raw = b64u_decode(text.strip())
    if len(raw) != KEY_LEN:
        raise IdentityError("public key must be 32 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as err:  # pragma: no cover - cryptography accepts any 32 bytes
        raise IdentityError("invalid Ed25519 public key") from err
    return raw


def verify_signature(public_key: bytes, signature: bytes, data: bytes) -> bool:
    """Verify an Ed25519 signature; never raises."""
    if len(signature) != SIG_LEN or len(public_key) != KEY_LEN:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, data)
    except (InvalidSignature, ValueError):
        return False
    return True


class Identity:
    """Private identity of the local HEMS (or of the CEL administrator)."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key
        self.public_key: bytes = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.hems_id: str = hems_id_from_public_key(self.public_key)

    @classmethod
    def generate(cls) -> Identity:
        """Create a brand new identity."""
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_b64(cls, text: str) -> Identity:
        """Load an identity from its base64url raw private key (32 bytes)."""
        raw = b64u_decode(text.strip())
        if len(raw) != KEY_LEN:
            raise IdentityError("private key must be 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    @property
    def private_b64(self) -> str:
        """Base64url raw private key - keep secret."""
        raw = self._key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return b64u_encode(raw)

    @property
    def public_b64(self) -> str:
        """Base64url raw public key."""
        return b64u_encode(self.public_key)

    def sign(self, data: bytes) -> bytes:
        """Sign data."""
        return self._key.sign(data)

    def __repr__(self) -> str:  # never leak the private key
        return f"Identity({self.hems_id})"
