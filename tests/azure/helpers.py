"""Helpers for the Azure deployment tests: a fake Azure CLI backed by Azurite."""

from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
AZURE = ROOT / "azure"
ADMIN_TOOL = ROOT / "tools" / "cel_admin.py"

FAKE_AZ = r'''#!__PYTHON__
"""Fake Azure CLI used by the tests: logs every call, answers like az would."""
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_AZ_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")

def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default

if args[:2] == ["account", "show"]:
    print(json.dumps({"id": "00000000-sub", "name": "Test subscription"}))
elif args[:3] == ["storage", "account", "show"]:
    sys.exit(0 if os.environ.get("FAKE_AZ_ACCOUNT_EXISTS") == "1" else 3)
elif args[:3] == ["storage", "account", "check-name"]:
    print(json.dumps({"nameAvailable": os.environ.get("FAKE_AZ_NAME_TAKEN") != "1"}))
elif args[:2] == ["group", "create"]:
    pass
elif args[:3] == ["deployment", "group", "create"]:
    params = dict(p.split("=", 1) for p in args[args.index("--parameters") + 1:] if "=" in p and not p.startswith("-"))
    assert os.path.isfile(opt("--template-file"))
    print(json.dumps({"properties": {"outputs": {
        "account": {"value": params["storageAccountName"]},
        "tableEndpoint": {"value": os.environ["FAKE_AZ_TABLE"] + "/"},
        "queueEndpoint": {"value": os.environ["FAKE_AZ_QUEUE"] + "/"}}}}))
elif args[:4] == ["storage", "account", "keys", "list"]:
    print(os.environ["FAKE_AZ_KEY"])
else:
    print("fake az: unsupported " + " ".join(args[:3]), file=sys.stderr)
    sys.exit(3)
'''


def install_fake_az(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    az = bin_dir / "az"
    az.write_text(FAKE_AZ.replace("__PYTHON__", sys.executable), encoding="utf-8")
    az.chmod(0o755)
    return az


def fake_az_env(tmp_path: Path, bin_dir: Path, azurite) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("CEL_STORAGE_KEY", None)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        FAKE_AZ_LOG=str(tmp_path / "az-calls.jsonl"),
        FAKE_AZ_TABLE=azurite.table_endpoint,
        FAKE_AZ_QUEUE=azurite.queue_endpoint,
        FAKE_AZ_KEY=azurite.key,
    )
    return env
