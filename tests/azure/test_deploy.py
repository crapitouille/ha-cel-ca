"""deploy.sh end to end, with a fake az CLI and Azurite as the storage account."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from .helpers import ADMIN_TOOL, AZURE, fake_az_env, install_fake_az

COMMUNITY = "cel-dep"


@pytest.fixture
def ctx(tmp_path: Path, azurite) -> dict:
    bin_dir = tmp_path / "bin"
    install_fake_az(bin_dir)
    env = fake_az_env(tmp_path, bin_dir, azurite)
    admin_dir = tmp_path / "admin"
    subprocess.run([sys.executable, str(ADMIN_TOOL), "--dir", str(admin_dir), "init", "--community", COMMUNITY], check=True, capture_output=True, env=env)
    return {"env": env, "tmp": tmp_path, "admin": admin_dir}


def deploy(ctx: dict, *extra: str, **env_overrides: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(AZURE / "deploy.sh"), "--community", COMMUNITY, "--account", "devstoreaccount1",
         "--admin-dir", str(ctx["admin"]), "--python", sys.executable, *extra],
        capture_output=True, text=True, env={**ctx["env"], **env_overrides},
    )


def calls(ctx: dict) -> list[list[str]]:
    return [json.loads(line) for line in (ctx["tmp"] / "az-calls.jsonl").read_text().splitlines()]


def test_deploy_configures_the_cel(ctx) -> None:
    proc = deploy(ctx)
    reg = json.loads((ctx["admin"] / "cel_registry.json").read_text())
    assert "storage" in reg, proc.stdout + proc.stderr
    # storage configured with the deployment outputs, key fetched with az (never stored)
    assert reg["storage"]["account"] == "devstoreaccount1" and reg["storage"]["resource_group"] == "rg-cel-dep"
    assert reg["storage"]["table_endpoint"].startswith("http://127.0.0.1")
    assert "key" not in json.dumps(reg["storage"]).lower()
    dep = next(c for c in calls(ctx) if c[:3] == ["deployment", "group", "create"])
    assert "storageAccountName=devstoreaccount1" in dep and "location=switzerlandnorth" in dep
    # On Azurite the self-test reports exactly the two partition checks the emulator does not enforce
    assert proc.returncode != 0
    ko = [line for line in proc.stderr.splitlines() if line.startswith("KO")]
    assert len(ko) == 2 and all("CANNOT write" in line for line in ko), proc.stdout + proc.stderr


@pytest.mark.parametrize(
    ("overrides", "message"),
    [({"FAKE_AZ_NAME_TAKEN": "1"}, "already taken")],
)
def test_deploy_preflight(ctx, overrides, message) -> None:
    proc = deploy(ctx, **overrides)
    assert proc.returncode != 0 and message in proc.stderr
    assert not any(c[:3] == ["deployment", "group", "create"] for c in calls(ctx))


def test_existing_account_is_reused(ctx) -> None:
    proc = deploy(ctx, FAKE_AZ_ACCOUNT_EXISTS="1", FAKE_AZ_NAME_TAKEN="1")
    assert "already taken" not in proc.stderr
    assert any(c[:3] == ["deployment", "group", "create"] for c in calls(ctx))


@pytest.mark.parametrize(
    ("args", "message"),
    [(("--account", "Bad_Name"), "--account"), (("--community", "other"), "belongs to community")],
)
def test_deploy_argument_checks(ctx, args, message) -> None:
    base = ["--community", COMMUNITY, "--account", "devstoreaccount1", "--admin-dir", str(ctx["admin"]), "--python", sys.executable]
    for i in range(0, len(args), 2):
        base[base.index(args[i]) + 1] = args[i + 1]
    proc = subprocess.run(["bash", str(AZURE / "deploy.sh"), *base], capture_output=True, text=True, env=ctx["env"])
    assert proc.returncode != 0 and message in proc.stderr
