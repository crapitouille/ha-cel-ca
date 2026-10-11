#!/usr/bin/env python3
"""CEL administration tool: admin key, member enrolment, access keys, signed registry.

The CEL is hosted on an Azure Storage Account (Table + Queue). Typical use, from
the repository root in Azure Cloud Shell:

    python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin init --community cel-ca
    python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin storage-target --account <name> --resource-group <rg>
    python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin selftest
    python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin enroll --pubkey <key> --id <hems-id> --name "Maison Dupont"
    python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin member-access --id <hems-id>     # prints the CEL1. access key
    python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin publish
    python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin revoke --id <hems-id>            # then publish
    python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin set-param --slot-minutes 15     # then publish
    python3 tools/cel_admin.py --dir ~/clouddrive/cel-admin list

The storage account key is read from $CEL_STORAGE_KEY or fetched with the Azure
CLI (az storage account keys list) each time it is needed; it is never stored.

Files (in --dir):
    cel_admin_key.json   administrator private key (KEEP SECRET, offline backup!)
    cel_registry.json    members, revocations, parameters, storage target and last serial
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "cel_ca"))

from core.identity import (
    Identity,
    IdentityError,
    hems_id_from_public_key,
    is_valid_hems_id,
    parse_public_key,
)
from core.messages import COMMUNITY_RE, SLOT_MINUTES, MsgType, encode_envelope
from core.registry import Member, build_registry_body
from core.storage_admin import StorageAdmin, StorageAdminError

KEY_FILE = "cel_admin_key.json"
REGISTRY_FILE = "cel_registry.json"
SIGNED_FILE = "registry.signed.json"
DAY_MS = 86_400_000


class AdminError(Exception):
    """User-facing error."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_private(path: Path, data: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_admin(directory: Path) -> Identity:
    path = directory / KEY_FILE
    if not path.exists():
        raise AdminError(f"{path} not found - run 'init' first")
    return Identity.from_private_b64(json.loads(path.read_text(encoding="utf-8"))["private_key"])


def run_az(args: list[str]) -> str:
    """Run the Azure CLI and return stdout."""
    az = shutil.which("az")
    if az is None:
        raise AdminError("Azure CLI 'az' not found (use Azure Cloud Shell, or set CEL_STORAGE_KEY)")
    proc = subprocess.run([az, *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AdminError(f"az {' '.join(args[:3])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def storage_admin(reg: dict[str, Any]) -> StorageAdmin:
    """Administrator access to the storage account of the CEL."""
    target = reg.get("storage")
    if not target:
        raise AdminError("no storage account configured - run 'storage-target' first")
    key = os.environ.get("CEL_STORAGE_KEY")
    if not key:
        if not target.get("resource_group"):
            raise AdminError("set CEL_STORAGE_KEY or give --resource-group to 'storage-target'")
        key = run_az(
            [
                "storage", "account", "keys", "list", "--account-name", target["account"],
                "--resource-group", target["resource_group"], "--query", "[0].value", "-o", "tsv",
            ]
        ).strip()
    return StorageAdmin(target["account"], key, target.get("table_endpoint"), target.get("queue_endpoint"))


def load_registry(directory: Path) -> dict[str, Any]:
    path = directory / REGISTRY_FILE
    if not path.exists():
        raise AdminError(f"{path} not found - run 'init' first")
    return json.loads(path.read_text(encoding="utf-8"))


def save_registry(directory: Path, data: dict[str, Any]) -> None:
    path = directory / REGISTRY_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def cmd_init(args: argparse.Namespace) -> str:
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    if not COMMUNITY_RE.match(args.community):
        raise AdminError("invalid community id (2-32 chars: a-z, 0-9, - and _)")
    if (directory / KEY_FILE).exists():
        raise AdminError(f"{KEY_FILE} already exists - refusing to overwrite the admin key")
    admin = Identity.generate()
    _write_private(directory / KEY_FILE, {"private_key": admin.private_b64, "public_key": admin.public_b64})
    save_registry(directory, {"community": args.community, "serial": 0, "members": {}, "revoked": []})
    return (
        f"CEL '{args.community}' initialised.\n"
        f"Administrator public key (give it to every member):\n  {admin.public_b64}\n"
        f"Back up {KEY_FILE} offline: losing it means re-configuring every HEMS."
    )


def cmd_enroll(args: argparse.Namespace) -> str:
    directory = Path(args.dir)
    reg = load_registry(directory)
    try:
        pk = parse_public_key(args.pubkey)
    except IdentityError as err:
        raise AdminError(f"invalid public key: {err}") from err
    hems_id = hems_id_from_public_key(pk)
    if args.id and args.id != hems_id:
        raise AdminError(f"the public key belongs to {hems_id}, not {args.id}")
    roles = sorted({r.strip() for r in args.roles.split(",") if r.strip()})
    if not roles or not set(roles) <= {"producer", "consumer"}:
        raise AdminError("roles must be producer and/or consumer")
    if not 0 < len(args.name) <= 64:
        raise AdminError("name must be 1-64 characters")
    valid_until = _now_ms() + args.valid_days * DAY_MS if args.valid_days else 0
    entry = {"pk": args.pubkey.strip(), "name": args.name, "roles": roles, "valid_until": valid_until}
    if args.meter:
        entry["meter"] = args.meter
    reg["members"][hems_id] = entry
    reg["revoked"] = [r for r in reg["revoked"] if r != hems_id]
    save_registry(directory, reg)
    return (
        f"Enrolled {args.name}: {hems_id}.\n"
        f"Next: 'member-access --id {hems_id}' (access key for the member), then 'publish'."
    )


def cmd_revoke(args: argparse.Namespace) -> str:
    directory = Path(args.dir)
    reg = load_registry(directory)
    if args.id not in reg["members"]:
        raise AdminError(f"unknown member {args.id}")
    if args.id not in reg["revoked"]:
        reg["revoked"].append(args.id)
    save_registry(directory, reg)
    msg = f"Revoked {args.id}. Run 'publish' to distribute."
    if reg.get("storage") and not args.keep_access:
        store = storage_admin(reg)
        store.delete_queue(args.id)
        rows = store.delete_partition(reg["community"], args.id)
        msg += f" Its inbox and {rows} state row(s) were deleted from the storage."
    return msg


def cmd_set_param(args: argparse.Namespace) -> str:
    directory = Path(args.dir)
    reg = load_registry(directory)
    params = dict(reg.get("params") or {})
    if args.slot_minutes is not None:
        if args.slot_minutes == 0:
            params.pop("slot_minutes", None)
        elif args.slot_minutes not in SLOT_MINUTES:
            raise AdminError(f"slot minutes must be 0 (continuous) or one of {sorted(SLOT_MINUTES)}")
        else:
            params["slot_minutes"] = args.slot_minutes
    reg["params"] = params
    save_registry(directory, reg)
    shown = ", ".join(f"{k}={v}" for k, v in params.items()) or "none (continuous market)"
    return f"Community parameters: {shown}. Run 'publish' to distribute them."


def cmd_list(args: argparse.Namespace) -> str:
    reg = load_registry(Path(args.dir))
    params = reg.get("params") or {}
    lines = [f"Community {reg['community']} - registry serial {reg['serial']}"]
    lines.append(f"  slot: {params['slot_minutes']} min" if params.get("slot_minutes") else "  slot: none (continuous market)")
    storage = reg.get("storage")
    lines.append(f"  storage: {storage['account']}" if storage else "  storage: not configured (run 'storage-target' or azure/deploy.sh)")
    for hems_id, m in sorted(reg["members"].items(), key=lambda kv: kv[1]["name"]):
        state = "REVOKED" if hems_id in reg["revoked"] else "active"
        lines.append(f"  {hems_id}  {m['name']:<30} {','.join(m['roles']):<18} {state}")
    return "\n".join(lines)


def build_signed_registry(directory: Path) -> tuple[str, bytes]:
    """Increment the serial and sign the registry."""
    admin = load_admin(directory)
    reg = load_registry(directory)
    members = [
        Member(
            hems_id,
            parse_public_key(m["pk"]),
            m["name"],
            frozenset(m["roles"]),
            m.get("valid_until", 0),
            m.get("meter"),
        )
        for hems_id, m in reg["members"].items()
    ]
    # The serial is also bumped past the current time in seconds so that a
    # restored backup of this file can never produce a lower serial.
    reg["serial"] = max(reg["serial"] + 1, int(time.time()))
    body = build_registry_body(reg["serial"], members, reg["revoked"], reg.get("params") or None)
    payload = encode_envelope(admin, reg["community"], MsgType.REGISTRY, body, _now_ms())
    save_registry(directory, reg)
    return reg["community"], payload


def cmd_sign(args: argparse.Namespace) -> str:
    directory = Path(args.dir)
    _, payload = build_signed_registry(directory)
    (directory / SIGNED_FILE).write_bytes(payload)
    return f"Signed registry written to {directory / SIGNED_FILE} (use 'publish' to send it to the storage)."


def cmd_publish(args: argparse.Namespace) -> str:
    directory = Path(args.dir)
    reg = load_registry(directory)
    store = storage_admin(reg)
    community, payload = build_signed_registry(directory)
    store.put_registry(community, payload)
    serial = load_registry(directory)["serial"]
    return f"Registry #{serial} published ({len(reg['members'])} member(s)). HEMS apply it within ~15 s."


def cmd_storage_target(args: argparse.Namespace) -> str:
    directory = Path(args.dir)
    reg = load_registry(directory)
    target: dict[str, Any] = {"account": args.account}
    if args.resource_group:
        target["resource_group"] = args.resource_group
    if args.table_endpoint:
        target["table_endpoint"] = args.table_endpoint
    if args.queue_endpoint:
        target["queue_endpoint"] = args.queue_endpoint
    reg["storage"] = target
    reg.pop("broker", None)  # v0.5 (MQTT VM) target, replaced by the storage account
    store = storage_admin(reg)  # fetches the key: fails early if anything is wrong
    store.ensure_table(reg["community"])
    save_registry(directory, reg)
    return f"Storage account '{args.account}' ready for CEL '{reg['community']}'."


def cmd_member_access(args: argparse.Namespace) -> str:
    directory = Path(args.dir)
    reg = load_registry(directory)
    if not is_valid_hems_id(args.id):
        raise AdminError("invalid HEMS id")
    if args.id in reg["revoked"]:
        raise AdminError(f"{args.id} is revoked")
    admin = load_admin(directory)
    key = storage_admin(reg).issue_access(reg["community"], args.id, admin.public_b64, days=args.days)
    name = reg["members"].get(args.id, {}).get("name")
    warning = "" if name else "\nWarning: this HEMS is not (yet) in the registry - run 'enroll' and 'publish' too."
    return (
        f"Access key for {name or args.id} (valid until {key.exp}).\n"
        f"Give it to the member, to paste in Home Assistant > WattKeeper > 'CEL access key':\n\n"
        f"{key.encode()}\n{warning}"
    )


def cmd_selftest(args: argparse.Namespace) -> str:
    directory = Path(args.dir)
    reg = load_registry(directory)
    results = storage_admin(reg).selftest(reg["community"], load_admin(directory).public_b64)
    lines = [f"{'OK ' if r.ok else 'KO '} {r.name}{'  -> ' + r.detail if r.detail else ''}" for r in results]
    if not all(r.ok for r in results):
        raise AdminError("self-test failed:\n" + "\n".join(lines))
    return "\n".join([*lines, "All checks passed."])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CEL-CA administration tool")
    parser.add_argument("--dir", default=".", help="directory holding the admin files")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the admin key and an empty registry")
    p.add_argument("--community", required=True)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("enroll", help="add or update a member")
    p.add_argument("--pubkey", required=True, help="public key shown by the HEMS")
    p.add_argument("--id", help="HEMS id shown by the HEMS (cross-check)")
    p.add_argument("--name", required=True)
    p.add_argument("--roles", default="producer,consumer")
    p.add_argument("--meter", help="metering point id (optional)")
    p.add_argument("--valid-days", type=int, default=0, help="membership duration, 0 = unlimited")
    p.set_defaults(func=cmd_enroll)

    p = sub.add_parser("revoke", help="revoke a member (and delete its inbox and state rows)")
    p.add_argument("--id", required=True)
    p.add_argument("--keep-access", action="store_true", help="do not touch the storage")
    p.set_defaults(func=cmd_revoke)

    sub.add_parser("list", help="list members").set_defaults(func=cmd_list)

    p = sub.add_parser("set-param", help="set community parameters (signed into the registry)")
    p.add_argument("--slot-minutes", type=int, help="market slot: 5, 10, 15, 20, 30 or 60 min; 0 = continuous")
    p.set_defaults(func=cmd_set_param)

    sub.add_parser("sign", help="sign the registry to a file").set_defaults(func=cmd_sign)
    sub.add_parser("publish", help="sign the registry and write it to the storage").set_defaults(func=cmd_publish)

    p = sub.add_parser("storage-target", help="use an Azure Storage Account for the CEL")
    p.add_argument("--account", required=True, help="storage account name")
    p.add_argument("--resource-group", help="resource group (to fetch the key with az)")
    p.add_argument("--table-endpoint", help="custom endpoint (emulator / sovereign cloud)")
    p.add_argument("--queue-endpoint", help="custom endpoint (emulator / sovereign cloud)")
    p.set_defaults(func=cmd_storage_target)

    p = sub.add_parser("member-access", help="create the inbox of a member and print its access key")
    p.add_argument("--id", required=True)
    p.add_argument("--days", type=int, default=365, help="validity of the access key (default 365)")
    p.set_defaults(func=cmd_member_access)

    sub.add_parser("selftest", help="check the storage account and the access restrictions").set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        print(args.func(args))
    except (AdminError, StorageAdminError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
