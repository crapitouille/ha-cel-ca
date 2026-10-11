"""The administration CLI against Azurite: the full onboarding of a member."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from custom_components.cel_ca.core.identity import Identity, parse_public_key
from custom_components.cel_ca.core.node import CelNode, NodeConfig
from custom_components.cel_ca.core.storage_access import AccessKey
from custom_components.cel_ca.core.storage_transport import StorageTransport

TOOL = Path(__file__).resolve().parents[2] / "tools" / "cel_admin.py"
FAST = {"state_interval": 0.2, "inbox_interval": 0.2, "fast_interval": 0.05}


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("cel_admin_cli", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli(tool, azurite, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CEL_STORAGE_KEY", azurite.key)

    def run(*args: str) -> tuple[int, str, str]:
        code = tool.main(["--dir", str(tmp_path), *args])
        out = capsys.readouterr()
        return code, out.out, out.err

    run("init", "--community", "cel-cli")
    code, out, err = run(
        "storage-target", "--account", azurite.account,
        "--table-endpoint", azurite.table_endpoint, "--queue-endpoint", azurite.queue_endpoint,
    )
    assert code == 0, err
    return run


async def wait_for(predicate, timeout=8.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


async def test_onboarding_of_a_member(cli, tmp_path) -> None:
    member = Identity.generate()
    code, out, _ = cli("enroll", "--pubkey", member.public_b64, "--id", member.hems_id, "--name", "Maison Dupont")
    assert code == 0 and "member-access" in out
    code, out, _ = cli("member-access", "--id", member.hems_id)
    assert code == 0
    key = AccessKey.decode(next(line for line in out.splitlines() if line.startswith("CEL1.")))
    admin_pk = json.loads((tmp_path / "cel_admin_key.json").read_text())["public_key"]
    assert key.id == member.hems_id and key.cid == "cel-cli" and key.apk == admin_pk
    code, out, _ = cli("publish")
    assert code == 0 and "1 member" in out

    # the member's HEMS, configured only with its access key, joins the CEL
    node = CelNode(member, key.cid, parse_public_key(key.apk), StorageTransport(key, **FAST), NodeConfig(name="dupont"))
    await node.async_start()
    try:
        await wait_for(lambda: node.enrolled)
        # community parameters reach it too
        assert cli("set-param", "--slot-minutes", "15")[0] == 0
        assert cli("publish")[0] == 0
        await wait_for(lambda: node.slot_ms == 15 * 60_000)
        # revocation: inbox and state rows deleted, HEMS no longer enrolled
        await wait_for(lambda: node.transport.stats["sent"] > 0)
        code, out, _ = cli("revoke", "--id", member.hems_id)
        assert code == 0 and "inbox" in out
        assert cli("publish")[0] == 0
        await wait_for(lambda: not node.enrolled)
        code, _, err = cli("member-access", "--id", member.hems_id)
        assert code == 1 and "revoked" in err
    finally:
        await node.async_stop()


def test_member_access_for_unknown_hems_warns(cli) -> None:
    ghost = Identity.generate().hems_id
    code, out, _ = cli("member-access", "--id", ghost)
    assert code == 0 and "not (yet) in the registry" in out
    assert cli("member-access", "--id", "nope")[0] == 1


def test_selftest_reports_emulator_limits(cli) -> None:
    code, _, err = cli("selftest")
    # Azurite does not enforce SAS partition ranges: exactly those two checks fail here
    assert code == 1
    failed = [line for line in err.splitlines() if line.startswith("KO")]
    assert len(failed) == 2 and all("CANNOT write" in line for line in failed)


def test_key_from_az_when_no_env(tool, azurite, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("CEL_STORAGE_KEY", raising=False)
    tool.main(["--dir", str(tmp_path), "init", "--community", "cel-az"])
    calls = []

    def fake_az(args):
        calls.append(args)
        return azurite.key + "\n"

    monkeypatch.setattr(tool, "run_az", fake_az)
    code = tool.main(
        ["--dir", str(tmp_path), "storage-target", "--account", azurite.account, "--resource-group", "rg-cel",
         "--table-endpoint", azurite.table_endpoint, "--queue-endpoint", azurite.queue_endpoint]
    )
    assert code == 0
    assert calls[0][:4] == ["storage", "account", "keys", "list"] and "rg-cel" in calls[0]
    reg = json.loads((tmp_path / "cel_registry.json").read_text())
    assert "key" not in json.dumps(reg["storage"]).lower()  # the account key is never stored
    # without resource group nor env: clear error
    tool.main(["--dir", str(tmp_path), "storage-target", "--account", azurite.account])
    assert "CEL_STORAGE_KEY" in capsys.readouterr().err


def test_admin_directory_of_v05_is_migrated(tool, azurite, tmp_path, monkeypatch, capsys) -> None:
    """The v0.5 (MQTT VM) admin directory keeps its key, members and serial."""
    monkeypatch.setenv("CEL_STORAGE_KEY", azurite.key)
    assert tool.main(["--dir", str(tmp_path), "init", "--community", "cel-old"]) == 0
    member = Identity.generate()
    assert tool.main(["--dir", str(tmp_path), "enroll", "--pubkey", member.public_b64, "--name", "Maison"]) == 0
    reg_path = tmp_path / "cel_registry.json"
    reg = json.loads(reg_path.read_text())
    reg["broker"] = {"resource_group": "rg-cel-old-broker", "vm": "cel-cel-old-broker", "host": "x.cloudapp.azure.com"}
    reg["serial"] = 1_790_000_000
    reg_path.write_text(json.dumps(reg))
    capsys.readouterr()

    args = ["--dir", str(tmp_path)]
    target = ["--account", azurite.account, "--table-endpoint", azurite.table_endpoint, "--queue-endpoint", azurite.queue_endpoint]
    assert tool.main([*args, "storage-target", *target]) == 0
    assert tool.main([*args, "member-access", "--id", member.hems_id]) == 0
    assert tool.main([*args, "publish"]) == 0
    reg = json.loads(reg_path.read_text())
    assert "broker" not in reg and reg["storage"]["account"] == azurite.account
    assert reg["serial"] > 1_790_000_000  # HEMS that saw the old registry accept the new one
    assert member.hems_id in reg["members"]
    capsys.readouterr()
    assert tool.main([*args, "list"]) == 0
    assert f"storage: {azurite.account}" in capsys.readouterr().out
