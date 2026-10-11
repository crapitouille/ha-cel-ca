"""Our dependency-free SAS generation must match the official Azure SDKs."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import pytest

from custom_components.cel_ca.core.identity import Identity
from custom_components.cel_ca.core.storage_access import (
    AccessKey,
    AccessKeyError,
    account_sas,
    queue_name,
    queue_sas,
    table_name,
    table_sas,
)

KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
ACCOUNT = "devstoreaccount1"
EXP = datetime(2027, 10, 9, 12, 0, tzinfo=UTC)
START = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _q(token: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(token).items()}


def test_table_sas_matches_sdk() -> None:
    sdk = pytest.importorskip("azure.data.tables")
    from azure.core.credentials import AzureNamedKeyCredential

    ours = _q(table_sas(ACCOUNT, KEY, "celtest", "rau", EXP, start_pk="hems-x", end_pk="hems-x", start=START))
    ref = _q(
        sdk.generate_table_sas(
            AzureNamedKeyCredential(ACCOUNT, KEY), "celtest", permission=sdk.TableSasPermissions(read=True, add=True, update=True),
            expiry=EXP, start=START, protocol="https,http", start_pk="hems-x", end_pk="hems-x",
        )
    )
    assert ours["sig"] == ref["sig"]
    assert {k: ours[k] for k in ("sp", "spk", "epk", "se", "st", "sv")} == {k: ref[k] for k in ("sp", "spk", "epk", "se", "st", "sv")}


def test_queue_sas_matches_sdk() -> None:
    pytest.importorskip("azure.storage.queue")
    from azure.storage.queue._shared_access_signature import QueueSharedAccessSignature

    from custom_components.cel_ca.core.storage_access import QUEUE_SAS_VERSION

    signer = QueueSharedAccessSignature(ACCOUNT, KEY)
    signer.x_ms_version = QUEUE_SAS_VERSION  # sign with the same service version as us
    ref = _q(signer.generate_queue("hems-abc", permission="rp", expiry=EXP, start=START, protocol="https,http"))
    ours = _q(queue_sas(ACCOUNT, KEY, "hems-abc", "rp", EXP, start=START))
    assert ref["sv"] == ours["sv"] == QUEUE_SAS_VERSION
    assert ours["sig"] == ref["sig"]


def test_account_sas_matches_sdk() -> None:
    pytest.importorskip("azure.storage.queue")
    from azure.storage.queue._shared.shared_access_signature import SharedAccessSignature

    from custom_components.cel_ca.core.storage_access import QUEUE_SAS_VERSION

    ref = _q(SharedAccessSignature(ACCOUNT, KEY, x_ms_version=QUEUE_SAS_VERSION).generate_account("q", "o", "a", EXP, start=START, protocol="https,http"))
    ours = _q(account_sas(ACCOUNT, KEY, "q", "o", "a", EXP, start=START))
    assert ours["sig"] == ref["sig"] and ours["ss"] == ref["ss"] == "q"


def test_names() -> None:
    assert table_name("cel-ca") == "celcelca"
    assert table_name("Quartier_Nord-2") == "celquartiernord2"
    ident = Identity.generate()
    assert queue_name(ident.hems_id) == ident.hems_id
    with pytest.raises(AccessKeyError):
        queue_name("../etc")


def test_access_key_roundtrip_and_errors() -> None:
    ident = Identity.generate()
    key = AccessKey("cel-ca", ident.hems_id, "apk", "https://a.table.core.windows.net", "https://a.queue.core.windows.net", "tr", "tw", "qs", "qp", "2027-10-09")
    text = key.encode()
    assert text.startswith("CEL1.") and AccessKey.decode(text) == key and AccessKey.decode(f"  {text}\n") == key
    assert key.table == "celcelca"
    for bad in ("", "hello", "CEL1.!!!", "CEL1." + text[5:-10], key.encode().replace("CEL1.", "CEL2.")):
        with pytest.raises(AccessKeyError):
            AccessKey.decode(bad)
    wrong = AccessKey("Bad Cid", ident.hems_id, "a", "https://x", "https://y", "", "", "", "", "")
    with pytest.raises(AccessKeyError):
        AccessKey.decode(wrong.encode())
    with pytest.raises(AccessKeyError):
        table_sas(ACCOUNT, "not base64!", "t", "r", EXP)
