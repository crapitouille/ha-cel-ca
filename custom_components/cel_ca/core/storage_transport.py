"""Transport over an Azure Storage Account (Table for shared state, Queue for inboxes).

It reproduces the MQTT semantics the node relies on, by polling:

* retained topics (``status``, ``offers``, ``reservations``, ``registry``) are
  rows of the community table; every change of a row is delivered once;
* ``settlement`` broadcasts use a small ring of rows per HEMS;
* ``inbox`` messages are queue messages, deleted once delivered.

State is polled every ``state_interval`` seconds, the inbox every
``inbox_interval`` seconds, and every ``fast_interval`` second for a short
while after this HEMS sent a request (so answers arrive quickly).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import aiohttp

from .identity import is_valid_hems_id
from .storage_access import (
    INBOX_TTL_S,
    REGISTRY_PARTITION,
    SETTLEMENT_SLOTS,
    TABLE_SAS_VERSION,
    AccessKey,
    queue_name,
)
from .transport import Transport, TransportError, topic_matches

_LOGGER = logging.getLogger(__name__)

STATE_KINDS = ("status", "offers", "reservations")
JSON_HEADERS = {
    "Accept": "application/json;odata=nometadata",
    "Content-Type": "application/json",
    "DataServiceVersion": "3.0",
    "x-ms-version": TABLE_SAS_VERSION,
}
BACKOFF_MIN_S = 5
BACKOFF_MAX_S = 60


class StorageAuthError(TransportError):
    """The access key was refused (expired, revoked or wrong)."""


class StorageTransport(Transport):
    """Polling transport over Azure Table + Queue storage."""

    def __init__(
        self,
        access: AccessKey,
        *,
        session: aiohttp.ClientSession | None = None,
        state_interval: float = 15.0,
        inbox_interval: float = 5.0,
        fast_interval: float = 1.0,
        fast_window: float = 30.0,
        request_timeout: float = 20.0,
    ) -> None:
        super().__init__()
        self.access = access
        self.hems_id = access.id
        self.cid = access.cid
        self._session = session
        self._own_session = session is None
        self.state_interval = state_interval
        self.inbox_interval = inbox_interval
        self.fast_interval = fast_interval
        self.fast_window = fast_window
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self.filters: list[str] = []
        self._seen: dict[tuple[str, str], str] = {}
        self._seq = 0
        self._fast_until = 0.0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.last_error: str | None = None
        self.auth_failed = False
        self.stats = {"state_polls": 0, "inbox_polls": 0, "delivered": 0, "sent": 0}

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="cel_ca_storage")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        self._set_connected(False)
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def subscribe(self, topic_filter: str) -> None:
        if topic_filter not in self.filters:
            self.filters.append(topic_filter)

    async def publish(self, topic: str, payload: bytes, *, retain: bool = False) -> None:
        parts = topic.split("/")
        if len(parts) != 5 or parts[:3] != ["cel", self.cid, "node"]:
            raise TransportError(f"unsupported topic {topic}")
        target, kind = parts[3], parts[4]
        if kind == "inbox":
            await self._queue_put(target, payload)
            self._fast_until = time.monotonic() + self.fast_window
            self._wake.set()
            return
        if target != self.hems_id:
            raise TransportError("a HEMS can only write its own partition")
        if kind in STATE_KINDS:
            row = kind
        elif kind == "settlement":
            row = f"settlement-{self._seq % SETTLEMENT_SLOTS}"
            self._seq += 1
        else:
            raise TransportError(f"unsupported topic {topic}")
        await self._upsert(row, payload)

    # ------------------------------------------------------------- polling loop
    async def _run(self) -> None:
        backoff = BACKOFF_MIN_S
        while not self._stopping:
            try:
                await self._probe()
                self.last_error = None
                self.auth_failed = False
                self._set_connected(True)
                backoff = BACKOFF_MIN_S
                if self.on_connect:
                    await self.on_connect()
                await self._poll_forever()
            except asyncio.CancelledError:
                raise
            except StorageAuthError as err:
                self.auth_failed = True
                self.last_error = str(err)
                backoff = BACKOFF_MAX_S
                _LOGGER.error("CEL access key refused by Azure Storage: %s", err)
                if self.on_state:  # let the host react (e.g. ask the user for a new key)
                    self.on_state(False)
            except (aiohttp.ClientError, TimeoutError, TransportError) as err:
                self.last_error = str(err) or type(err).__name__
                _LOGGER.warning("CEL storage unreachable (%s), retrying in %ss", self.last_error, backoff)
            self._set_connected(False)
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def _poll_forever(self) -> None:
        next_state = next_inbox = 0.0
        while not self._stopping:
            now = time.monotonic()
            if now >= next_state:
                await self.poll_state()
                next_state = time.monotonic() + self.state_interval
            if now >= next_inbox:
                await self.poll_inbox()
                fast = time.monotonic() < self._fast_until
                next_inbox = time.monotonic() + (self.fast_interval if fast else self.inbox_interval)
            delay = max(0.05, min(next_state, next_inbox) - time.monotonic())
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)
            if self._wake.is_set():  # a request was sent: look for the answer soon
                next_inbox = min(next_inbox, time.monotonic() + self.fast_interval)

    # ------------------------------------------------------------------- REST
    async def _request(self, method: str, url: str, *, data: Any = None, headers: dict | None = None) -> tuple[int, bytes, dict]:
        assert self._session is not None
        async with self._session.request(method, url, data=data, headers=headers or {}, timeout=self._timeout) as resp:
            body = await resp.read()
            if resp.status in (401, 403):
                raise StorageAuthError(f"{method} {url.split('?')[0]}: HTTP {resp.status}")
            return resp.status, body, dict(resp.headers)

    def _table_url(self, suffix: str, sas: str, extra: str = "") -> str:
        return f"{self.access.tu}/{self.access.table}{suffix}?{extra}{sas}"

    async def _probe(self) -> None:
        status, body, _ = await self._request("GET", self._table_url("()", self.access.tr, "$top=1&"), headers=JSON_HEADERS)
        if status != 200:
            raise TransportError(f"table not reachable: HTTP {status} {body[:120]!r}")

    async def _upsert(self, row: str, payload: bytes) -> None:
        url = self._table_url(f"(PartitionKey='{self.hems_id}',RowKey='{row}')", self.access.tw)
        body = {"payload": payload.decode("utf-8")}
        try:
            status, text, _ = await self._request("PUT", url, data=_json(body), headers=JSON_HEADERS)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TransportError(str(err) or type(err).__name__) from err
        if status not in (200, 201, 204):
            raise TransportError(f"upsert {row}: HTTP {status} {text[:120]!r}")
        self.stats["sent"] += 1

    async def _queue_put(self, target: str, payload: bytes) -> None:
        url = f"{self.access.qu}/{queue_name(target)}/messages?messagettl={INBOX_TTL_S}&{self.access.qs}"
        message = base64.b64encode(payload).decode()
        body = f"<QueueMessage><MessageText>{message}</MessageText></QueueMessage>"
        try:
            status, text, _ = await self._request("POST", url, data=body.encode())
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TransportError(str(err) or type(err).__name__) from err
        if status == 404:
            raise TransportError(f"inbox of {target} does not exist (not enrolled?)")
        if status not in (200, 201):
            raise TransportError(f"queue put: HTTP {status} {text[:120]!r}")
        self.stats["sent"] += 1

    async def poll_state(self) -> None:
        """Read the whole community table and deliver changed rows."""
        self.stats["state_polls"] += 1
        extra = ""
        while True:
            status, body, headers = await self._request("GET", self._table_url("()", self.access.tr, extra), headers=JSON_HEADERS)
            if status != 200:
                raise TransportError(f"table query: HTTP {status}")
            for entity in _loads(body).get("value", []):
                await self._deliver_entity(entity)
            npk = headers.get("x-ms-continuation-NextPartitionKey")
            if not npk:
                break
            nrk = headers.get("x-ms-continuation-NextRowKey", "")
            extra = f"NextPartitionKey={npk}&NextRowKey={nrk}&"

    async def _deliver_entity(self, entity: dict[str, Any]) -> None:
        pk, rk = entity.get("PartitionKey", ""), entity.get("RowKey", "")
        payload = entity.get("payload")
        if not isinstance(payload, str):
            return
        if pk == REGISTRY_PARTITION and rk == "registry":
            topic = f"cel/{self.cid}/registry"
        elif is_valid_hems_id(pk) and (rk in STATE_KINDS or rk.startswith("settlement-")):
            topic = f"cel/{self.cid}/node/{pk}/{'settlement' if rk.startswith('settlement-') else rk}"
        else:
            return
        digest = hashlib.sha256(payload.encode()).hexdigest()
        if self._seen.get((pk, rk)) == digest:
            return
        self._seen[(pk, rk)] = digest
        await self._deliver(topic, payload.encode())

    async def poll_inbox(self) -> None:
        """Fetch, deliver and delete the messages of our inbox."""
        self.stats["inbox_polls"] += 1
        base = f"{self.access.qu}/{queue_name(self.hems_id)}/messages"
        while True:
            status, body, _ = await self._request("GET", f"{base}?numofmessages=32&visibilitytimeout=60&{self.access.qp}")
            if status != 200:
                raise TransportError(f"inbox read: HTTP {status}")
            messages = _parse_messages(body)
            for msg_id, receipt, text in messages:
                try:
                    payload = base64.b64decode(text, validate=True)
                except (binascii.Error, ValueError):
                    payload = b""
                if payload:
                    await self._deliver(f"cel/{self.cid}/node/{self.hems_id}/inbox", payload)
                await self._request(
                    "DELETE", f"{base}/{msg_id}?popreceipt={_quote(receipt)}&{self.access.qp}"
                )
            if len(messages) < 32:
                break

    async def _deliver(self, topic: str, payload: bytes) -> None:
        if self.on_message is None or not any(topic_matches(f, topic) for f in self.filters):
            return
        self.stats["delivered"] += 1
        try:
            await self.on_message(topic, payload)
        except Exception:  # one bad message must not stop the transport
            _LOGGER.exception("Error handling message on %s", topic)


def _json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode()


def _loads(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body or b"{}")
    except ValueError as err:
        raise TransportError("invalid table response") from err
    return data if isinstance(data, dict) else {}


def _quote(text: str) -> str:
    return quote(text, safe="")


def _parse_messages(body: bytes) -> list[tuple[str, str, str]]:
    if not body.strip():
        return []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as err:
        raise TransportError("invalid queue response") from err
    out = []
    for msg in root.findall("QueueMessage"):
        out.append((msg.findtext("MessageId", ""), msg.findtext("PopReceipt", ""), msg.findtext("MessageText", "")))
    return out


async def probe_access(access: AccessKey, session: aiohttp.ClientSession) -> tuple[str, str] | None:
    """Check an access key: read the table and peek our inbox. None if OK, else (error_key, detail)."""
    transport = StorageTransport(access, session=session)
    try:
        await transport._probe()
        status, body, _ = await transport._request(
            "GET", f"{access.qu}/{queue_name(access.id)}/messages?peekonly=true&{access.qp}"
        )
        if status == 404:
            return "inbox_missing", "inbox queue does not exist"
        if status != 200:
            return "cannot_connect", f"HTTP {status} {body[:80]!r}"
    except StorageAuthError as err:
        return "invalid_auth", str(err)
    except (aiohttp.ClientError, TimeoutError, TransportError) as err:
        return "cannot_connect", str(err) or type(err).__name__
    return None
