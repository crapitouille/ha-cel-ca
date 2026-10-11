"""The CEL administration tool produces registries accepted by the nodes."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from custom_components.cel_ca.core.identity import Identity, parse_public_key
from custom_components.cel_ca.core.node import CelNode, NodeConfig
from custom_components.cel_ca.core.transport import InMemoryBroker, InMemoryTransport

TOOL = Path(__file__).resolve().parents[2] / "tools" / "cel_admin.py"


@pytest.fixture(scope="module")
def admin_tool():
    spec = importlib.util.spec_from_file_location("cel_admin", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(tool, tmp_path: Path, *args: str) -> int:
    return tool.main(["--dir", str(tmp_path), *args])


async def test_full_admin_cycle(admin_tool, tmp_path: Path, capsys) -> None:
    assert run(admin_tool, tmp_path, "init", "--community", "cel-ca") == 0
    assert (tmp_path / "cel_admin_key.json").stat().st_mode & 0o077 == 0
    assert run(admin_tool, tmp_path, "init", "--community", "cel-ca") == 1  # never overwrite the key
    admin_pk = json.loads((tmp_path / "cel_admin_key.json").read_text())["public_key"]

    alice, bob = Identity.generate(), Identity.generate()
    assert run(admin_tool, tmp_path, "enroll", "--pubkey", alice.public_b64, "--id", alice.hems_id, "--name", "Alice") == 0
    assert run(admin_tool, tmp_path, "enroll", "--pubkey", bob.public_b64, "--name", "Bob", "--roles", "consumer", "--meter", "CH1000") == 0
    # cross-check of id vs key
    assert run(admin_tool, tmp_path, "enroll", "--pubkey", bob.public_b64, "--id", alice.hems_id, "--name", "X") == 1
    assert run(admin_tool, tmp_path, "enroll", "--pubkey", "garbage", "--name", "X") == 1
    assert run(admin_tool, tmp_path, "enroll", "--pubkey", bob.public_b64, "--name", "X", "--roles", "admin") == 1
    assert run(admin_tool, tmp_path, "sign") == 0
    payload = (tmp_path / "registry.signed.json").read_bytes()

    broker = InMemoryBroker()
    await broker.route(None, "cel/cel-ca/registry", payload, True)
    node = CelNode(bob, "cel-ca", parse_public_key(admin_pk), InMemoryTransport(broker), NodeConfig(name="bob", roles=frozenset({"consumer"})))
    await node.async_start()
    assert node.enrolled
    assert node.registry.members[bob.hems_id].meter == "CH1000"
    assert node.registry.members[bob.hems_id].roles == frozenset({"consumer"})

    assert run(admin_tool, tmp_path, "revoke", "--id", bob.hems_id) == 0
    assert run(admin_tool, tmp_path, "revoke", "--id", "hems-unknown") == 1
    assert run(admin_tool, tmp_path, "sign") == 0
    await broker.route(None, "cel/cel-ca/registry", (tmp_path / "registry.signed.json").read_bytes(), True)
    assert not node.enrolled

    capsys.readouterr()
    assert run(admin_tool, tmp_path, "list") == 0
    out = capsys.readouterr().out
    assert "REVOKED" in out and "Alice" in out
    # without a storage account, publish explains what to do
    assert run(admin_tool, tmp_path, "publish") == 1
    assert "storage-target" in capsys.readouterr().err


async def test_set_param_slot_minutes(admin_tool, tmp_path: Path, capsys) -> None:
    assert run(admin_tool, tmp_path, "init", "--community", "cel-ca") == 0
    admin_pk = json.loads((tmp_path / "cel_admin_key.json").read_text())["public_key"]
    bob = Identity.generate()
    run(admin_tool, tmp_path, "enroll", "--pubkey", bob.public_b64, "--name", "Bob")
    assert run(admin_tool, tmp_path, "set-param", "--slot-minutes", "7") == 1
    assert run(admin_tool, tmp_path, "set-param", "--slot-minutes", "15") == 0
    assert run(admin_tool, tmp_path, "sign") == 0
    broker = InMemoryBroker()
    await broker.route(None, "cel/cel-ca/registry", (tmp_path / "registry.signed.json").read_bytes(), True)
    node = CelNode(bob, "cel-ca", parse_public_key(admin_pk), InMemoryTransport(broker), NodeConfig(name="bob"))
    await node.async_start()
    assert node.slot_ms == 15 * 60_000
    capsys.readouterr()
    run(admin_tool, tmp_path, "list")
    assert "slot: 15 min" in capsys.readouterr().out
    # 0 goes back to the continuous market, and the registry no longer carries params
    assert run(admin_tool, tmp_path, "set-param", "--slot-minutes", "0") == 0
    assert run(admin_tool, tmp_path, "sign") == 0
    assert '"params"' not in (tmp_path / "registry.signed.json").read_text()
    await broker.route(None, "cel/cel-ca/registry", (tmp_path / "registry.signed.json").read_bytes(), True)
    assert node.slot_ms == 0
