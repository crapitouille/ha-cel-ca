"""Administrator operations on the CEL Storage Account (synchronous, standard library only).

Used by ``tools/cel_admin.py`` (Cloud Shell) and by the tests. The account key
never leaves the administrator: members only receive SAS tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import urllib.error
import urllib.request

from .identity import Identity
from .storage_access import (
    REGISTRY_PARTITION,
    TABLE_SAS_VERSION,
    AccessKey,
    account_sas,
    default_endpoints,
    queue_name,
    queue_sas,
    table_name,
    table_sas,
)

JSON_HEADERS = {
    "Accept": "application/json;odata=nometadata",
    "Content-Type": "application/json",
    "DataServiceVersion": "3.0",
    "x-ms-version": TABLE_SAS_VERSION,
}


class StorageAdminError(Exception):
    """An administrator operation failed."""


@dataclass(slots=True)
class CheckResult:
    """One line of the self-test."""

    name: str
    ok: bool
    detail: str = ""


class StorageAdmin:
    """Operations needing the account key."""

    def __init__(self, account: str, key: str, table_endpoint: str | None = None, queue_endpoint: str | None = None) -> None:
        self.account = account
        self.key = key
        default_t, default_q = default_endpoints(account)
        self.tu = (table_endpoint or default_t).rstrip("/")
        self.qu = (queue_endpoint or default_q).rstrip("/")

    # ------------------------------------------------------------- helpers
    def _admin_sas(self, minutes: int = 30) -> str:
        return account_sas(self.account, self.key, "qt", "sco", "rwdlacu", datetime.now(UTC) + timedelta(minutes=minutes))

    @staticmethod
    def http(method: str, url: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()
        except urllib.error.URLError as err:
            raise StorageAdminError(f"cannot reach {url.split('?')[0]}: {err.reason}") from err

    def _ok(self, what: str, status: int, body: bytes, *accepted: int) -> None:
        if status not in accepted:
            raise StorageAdminError(f"{what} failed: HTTP {status} {body[:200]!r}")

    # ---------------------------------------------------------------- setup
    def ensure_table(self, cid: str) -> None:
        status, body = self.http(
            "POST", f"{self.tu}/Tables?{self._admin_sas()}", json.dumps({"TableName": table_name(cid)}).encode(), JSON_HEADERS
        )
        self._ok("create table", status, body, 201, 204, 409)

    def ensure_queue(self, hems_id: str) -> None:
        status, body = self.http("PUT", f"{self.qu}/{queue_name(hems_id)}?{self._admin_sas()}")
        self._ok("create inbox", status, body, 201, 204)

    def delete_queue(self, hems_id: str) -> None:
        status, body = self.http("DELETE", f"{self.qu}/{queue_name(hems_id)}?{self._admin_sas()}")
        self._ok("delete inbox", status, body, 204, 404)

    def upsert(self, cid: str, pk: str, rk: str, payload: str) -> None:
        url = f"{self.tu}/{table_name(cid)}(PartitionKey='{pk}',RowKey='{rk}')?{self._admin_sas()}"
        status, body = self.http("PUT", url, json.dumps({"payload": payload}).encode(), JSON_HEADERS)
        self._ok("write", status, body, 200, 201, 204)

    def put_registry(self, cid: str, payload: bytes) -> None:
        self.upsert(cid, REGISTRY_PARTITION, "registry", payload.decode("utf-8"))

    def rows(self, cid: str, pk: str | None = None) -> list[dict]:
        flt = f"$filter=PartitionKey%20eq%20'{pk}'&" if pk else ""
        status, body = self.http("GET", f"{self.tu}/{table_name(cid)}()?{flt}{self._admin_sas()}", headers=JSON_HEADERS)
        self._ok("read table", status, body, 200)
        return json.loads(body).get("value", [])

    def delete_partition(self, cid: str, hems_id: str) -> int:
        count = 0
        for row in self.rows(cid, hems_id):
            url = f"{self.tu}/{table_name(cid)}(PartitionKey='{hems_id}',RowKey='{row['RowKey']}')?{self._admin_sas()}"
            status, body = self.http("DELETE", url, headers={**JSON_HEADERS, "If-Match": "*"})
            self._ok("delete row", status, body, 204, 404)
            count += 1
        return count

    # ---------------------------------------------------------------- access
    def issue_access(self, cid: str, hems_id: str, admin_public_key: str, *, days: int = 365) -> AccessKey:
        """Create the inbox of a member and return its access key."""
        self.ensure_table(cid)
        self.ensure_queue(hems_id)
        expiry = datetime.now(UTC) + timedelta(days=days)
        table = table_name(cid)
        return AccessKey(
            cid=cid,
            id=hems_id,
            apk=admin_public_key,
            tu=self.tu,
            qu=self.qu,
            tr=table_sas(self.account, self.key, table, "r", expiry),
            tw=table_sas(self.account, self.key, table, "rau", expiry, start_pk=hems_id, end_pk=hems_id),
            qs=account_sas(self.account, self.key, "q", "o", "a", expiry),
            qp=queue_sas(self.account, self.key, queue_name(hems_id), "rp", expiry),
            exp=expiry.date().isoformat(),
        )

    # -------------------------------------------------------------- selftest
    def selftest(self, cid: str, admin_public_key: str) -> list[CheckResult]:
        """Check the account and that member access keys are really restricted."""
        results: list[CheckResult] = []
        a, b = Identity.generate().hems_id, Identity.generate().hems_id
        table = table_name(cid)

        def check(name: str, expected: tuple[int, ...], status: int, body: bytes, note: str = "") -> None:
            ok = status in expected
            results.append(CheckResult(name, ok, "" if ok else f"HTTP {status} {note}{body[:80]!r}"))

        try:
            self.ensure_table(cid)
            results.append(CheckResult(f"table {table}", True))
            key_a = self.issue_access(cid, a, admin_public_key, days=1)
            self.ensure_queue(b)
            results.append(CheckResult("member inboxes", True))
            row = lambda pk: f"{self.tu}/{table}(PartitionKey='{pk}',RowKey='status')"  # noqa: E731
            body = json.dumps({"payload": "selftest"}).encode()
            check("member reads the table", (200,), *self.http("GET", f"{self.tu}/{table}()?$top=1&{key_a.tr}", headers=JSON_HEADERS))
            check("member writes its own partition", (204,), *self.http("PUT", f"{row(a)}?{key_a.tw}", body, JSON_HEADERS))
            check("member CANNOT write another partition", (403,), *self.http("PUT", f"{row(b)}?{key_a.tw}", body, JSON_HEADERS))
            check("member CANNOT write the registry", (403,), *self.http("PUT", f"{row(REGISTRY_PARTITION)}?{key_a.tw}", body, JSON_HEADERS))
            msg = b"<QueueMessage><MessageText>c2VsZnRlc3Q=</MessageText></QueueMessage>"
            check("member sends to another inbox", (201,), *self.http("POST", f"{self.qu}/{b}/messages?{key_a.qs}", msg))
            check("member reads its own inbox", (200,), *self.http("GET", f"{self.qu}/{a}/messages?{key_a.qp}"))
            check("member CANNOT read another inbox", (403,), *self.http("GET", f"{self.qu}/{b}/messages?{key_a.qp}"))
        except StorageAdminError as err:
            results.append(CheckResult("storage account", False, str(err)))
        finally:
            for hems in (a, b):
                try:
                    self.delete_partition(cid, hems)
                    self.delete_queue(hems)
                except StorageAdminError:
                    pass
        return results

