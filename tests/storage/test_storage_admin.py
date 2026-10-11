"""Administrator operations against Azurite."""

from __future__ import annotations

import pytest

from custom_components.cel_ca.core.identity import Identity
from custom_components.cel_ca.core.storage_access import AccessKey, table_name
from custom_components.cel_ca.core.storage_admin import StorageAdmin, StorageAdminError


def test_issue_access_registry_and_cleanup(azurite) -> None:
    admin = azurite.admin()
    member = Identity.generate()
    key = admin.issue_access("cel-adm", member.hems_id, "apk")
    assert AccessKey.decode(key.encode()) == key
    assert key.tu == azurite.table_endpoint and key.table == table_name("cel-adm")
    admin.issue_access("cel-adm", member.hems_id, "apk")  # idempotent
    admin.put_registry("cel-adm", b'{"r":1}')
    admin.upsert("cel-adm", member.hems_id, "status", "x")
    assert {(r["PartitionKey"], r["RowKey"]) for r in admin.rows("cel-adm")} >= {("registry", "registry"), (member.hems_id, "status")}
    assert admin.delete_partition("cel-adm", member.hems_id) == 1
    assert admin.rows("cel-adm", member.hems_id) == []
    admin.delete_queue(member.hems_id)
    admin.delete_queue(member.hems_id)  # idempotent


def test_selftest_against_emulator(azurite) -> None:
    results = {r.name: r for r in azurite.admin().selftest("cel-self", "apk")}
    must_pass = [
        "member reads the table",
        "member writes its own partition",
        "member sends to another inbox",
        "member reads its own inbox",
        "member CANNOT read another inbox",
    ]
    for name in must_pass:
        assert results[name].ok, (name, results[name].detail)
    # Azurite does not enforce SAS partition ranges (real Azure does): the
    # self-test is precisely what reveals it, so these lines are reported.
    assert "member CANNOT write another partition" in results
    assert "member CANNOT write the registry" in results


def test_wrong_key_and_unreachable(azurite) -> None:
    bad = StorageAdmin(azurite.account, "AAAA" + azurite.key[4:], azurite.table_endpoint, azurite.queue_endpoint)
    with pytest.raises(StorageAdminError):
        bad.ensure_table("cel-bad")
    nowhere = StorageAdmin(azurite.account, azurite.key, "http://127.0.0.1:9/x", "http://127.0.0.1:9/x")
    with pytest.raises(StorageAdminError):
        nowhere.ensure_table("cel-bad")
