"""Static checks of the Azure deployment: Bicep template and deploy script."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from .helpers import AZURE

BICEP = shutil.which("bicep")
SHELLCHECK = shutil.which("shellcheck")


@pytest.mark.skipif(BICEP is None, reason="bicep CLI not installed")
def test_bicep_builds_and_is_secure(tmp_path: Path) -> None:
    proc = subprocess.run([BICEP, "build", str(AZURE / "main.bicep"), "--outfile", str(tmp_path / "main.json")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "Warning" not in proc.stderr and "Error" not in proc.stderr, proc.stderr
    template = json.loads((tmp_path / "main.json").read_text())
    assert [r["type"] for r in template["resources"]] == ["Microsoft.Storage/storageAccounts"]  # nothing else to run
    props = template["resources"][0]["properties"]
    assert props["minimumTlsVersion"] == "TLS1_2"
    assert props["supportsHttpsTrafficOnly"] is True
    assert props["allowBlobPublicAccess"] is False
    assert template["resources"][0]["sku"]["name"] == "Standard_LRS"
    assert template["parameters"]["location"]["defaultValue"] == "switzerlandnorth"
    assert set(template["outputs"]) == {"account", "tableEndpoint", "queueEndpoint"}


@pytest.mark.skipif(BICEP is None, reason="bicep CLI not installed")
def test_bicep_lint_clean() -> None:
    proc = subprocess.run([BICEP, "lint", str(AZURE / "main.bicep")], capture_output=True, text=True)
    assert proc.returncode == 0 and not proc.stdout.strip() and not proc.stderr.strip(), proc.stdout + proc.stderr


@pytest.mark.skipif(SHELLCHECK is None, reason="shellcheck not installed")
def test_deploy_script_passes_shellcheck() -> None:
    proc = subprocess.run([SHELLCHECK, "-S", "warning", str(AZURE / "deploy.sh")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


def test_no_vm_leftovers() -> None:
    """The broker VM is gone: no cloud-init, no PowerShell VM script, no MQTT dependency."""
    root = AZURE.parent
    assert not (AZURE / "cloud-init.yaml").exists() and not (AZURE / "deploy.ps1").exists()
    assert not (root / "broker").exists()
    manifest = json.loads((root / "custom_components" / "cel_ca" / "manifest.json").read_text())
    assert manifest["requirements"] == []
    assert not (root / "custom_components" / "cel_ca" / "core" / "mqtt_transport.py").exists()
