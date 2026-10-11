"""Azure Storage access for the CEL: SAS generation (admin side) and member access keys.

Layout of a CEL in one Storage Account:

* Table ``cel<community>``  - shared state, one partition per HEMS
  (rows ``status``, ``offers``, ``reservations``, ``settlement-<n>``) and the
  partition ``registry`` written by the administrator only.
* Queue ``<hems_id>``       - inbox of each HEMS.

Each member receives an **access key** (``CEL1.<base64url json>``) holding four
SAS tokens with the least privileges needed:

====  ======================================================  =================
key   grants                                                  scope
====  ======================================================  =================
tr    read the whole table                                    table, ``r``
tw    write its own partition only (upsert)                   table, ``rau``, pk = own id
qs    add messages to any queue (inboxes of the others)       account, queue/object, ``a``
qp    read and delete messages of its own inbox               own queue, ``rp``
====  ======================================================  =================

Everything is still signed end to end (Ed25519): Azure is an untrusted relay.
The SAS only limit what a member can *write* (no impersonation of partitions,
no reading of other inboxes) and can be renewed by issuing a new access key.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import re
from urllib.parse import quote

from .identity import is_valid_hems_id
from .messages import COMMUNITY_RE

TABLE_SAS_VERSION = "2019-02-02"
QUEUE_SAS_VERSION = "2020-12-06"
ACCESS_KEY_PREFIX = "CEL1."
REGISTRY_PARTITION = "registry"
SETTLEMENT_SLOTS = 8  # ring of settlement rows per HEMS
INBOX_TTL_S = 3600

_ACCOUNT_RE = re.compile(r"^[a-z0-9]{3,24}$")


class AccessKeyError(ValueError):
    """Malformed or inconsistent access key."""


def table_name(community_id: str) -> str:
    """Table holding the state of a community (letters and digits only)."""
    return ("cel" + re.sub(r"[^a-z0-9]", "", community_id.lower()))[:63]


def queue_name(hems_id: str) -> str:
    """Inbox queue of a HEMS (a hems id is a valid queue name)."""
    if not is_valid_hems_id(hems_id):
        raise AccessKeyError("invalid hems id")
    return hems_id


def default_endpoints(account: str) -> tuple[str, str]:
    """Public Azure endpoints of an account."""
    return f"https://{account}.table.core.windows.net", f"https://{account}.queue.core.windows.net"


def sas_time(dt: datetime) -> str:
    """SAS time format (UTC, second precision)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sign(account_key: str, string_to_sign: str) -> str:
    try:
        key = base64.b64decode(account_key, validate=True)
    except (binascii.Error, ValueError) as err:
        raise AccessKeyError("invalid storage account key") from err
    digest = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _query(params: list[tuple[str, str]]) -> str:
    return "&".join(f"{k}={quote(v, safe='')}" for k, v in params if v)


def table_sas(
    account: str,
    account_key: str,
    table: str,
    permissions: str,
    expiry: datetime,
    *,
    start_pk: str = "",
    end_pk: str = "",
    start: datetime | None = None,
) -> str:
    """Service SAS for a table, optionally limited to a partition key range."""
    st = sas_time(start) if start else ""
    se = sas_time(expiry)
    string_to_sign = "\n".join(
        [permissions, st, se, f"/table/{account}/{table.lower()}", "", "", "https,http", TABLE_SAS_VERSION, start_pk, "", end_pk, ""]
    )
    return _query(
        [
            ("sv", TABLE_SAS_VERSION),
            ("tn", table),
            ("st", st),
            ("se", se),
            ("sp", permissions),
            ("spr", "https,http"),
            ("spk", start_pk),
            ("epk", end_pk),
            ("sig", _sign(account_key, string_to_sign)),
        ]
    )


def queue_sas(account: str, account_key: str, queue: str, permissions: str, expiry: datetime, *, start: datetime | None = None) -> str:
    """Service SAS for one queue."""
    st = sas_time(start) if start else ""
    se = sas_time(expiry)
    string_to_sign = "\n".join([permissions, st, se, f"/queue/{account}/{queue}", "", "", "https,http", QUEUE_SAS_VERSION])
    return _query(
        [
            ("sv", QUEUE_SAS_VERSION),
            ("st", st),
            ("se", se),
            ("sp", permissions),
            ("spr", "https,http"),
            ("sig", _sign(account_key, string_to_sign)),
        ]
    )


def account_sas(
    account: str,
    account_key: str,
    services: str,
    resource_types: str,
    permissions: str,
    expiry: datetime,
    *,
    start: datetime | None = None,
) -> str:
    """Account SAS (services: q = queue, t = table; resource types: s, c, o)."""
    st = sas_time(start) if start else ""
    se = sas_time(expiry)
    string_to_sign = "\n".join(
        [account, permissions, services, resource_types, st, se, "", "https,http", QUEUE_SAS_VERSION, "", ""]
    )
    return _query(
        [
            ("sv", QUEUE_SAS_VERSION),
            ("ss", services),
            ("srt", resource_types),
            ("st", st),
            ("se", se),
            ("sp", permissions),
            ("spr", "https,http"),
            ("sig", _sign(account_key, string_to_sign)),
        ]
    )


@dataclass(frozen=True, slots=True)
class AccessKey:
    """Everything a HEMS needs to join the CEL, issued by the administrator."""

    cid: str  # community id
    id: str  # hems id the key was issued for
    apk: str  # administrator public key (trust anchor)
    tu: str  # table endpoint, e.g. https://<account>.table.core.windows.net
    qu: str  # queue endpoint
    tr: str  # SAS: read the table
    tw: str  # SAS: write own partition
    qs: str  # SAS: send to any inbox
    qp: str  # SAS: process own inbox
    exp: str  # expiry (ISO) for display

    @property
    def table(self) -> str:
        return table_name(self.cid)

    def encode(self) -> str:
        raw = json.dumps(asdict(self), separators=(",", ":"), sort_keys=True).encode()
        return ACCESS_KEY_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    @classmethod
    def decode(cls, text: str) -> AccessKey:
        text = (text or "").strip()
        if not text.startswith(ACCESS_KEY_PREFIX):
            raise AccessKeyError("not a CEL access key")
        body = text[len(ACCESS_KEY_PREFIX) :]
        try:
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            key = cls(**{f: data[f] for f in cls.__slots__})  # type: ignore[attr-defined]
        except (binascii.Error, ValueError, KeyError, TypeError) as err:
            raise AccessKeyError("corrupted CEL access key") from err
        if not COMMUNITY_RE.match(key.cid) or not is_valid_hems_id(key.id):
            raise AccessKeyError("invalid community or hems id in access key")
        for url in (key.tu, key.qu):
            if not url.startswith(("https://", "http://")):
                raise AccessKeyError("invalid endpoint in access key")
        return key
