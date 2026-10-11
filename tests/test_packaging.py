"""Distribution through HACS: repository layout, manifests, brand images, versions."""

from __future__ import annotations

import json
from pathlib import Path
import re
import struct
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "cel_ca"
REPO_URL = "https://github.com/crapitouille/ha-cel-ca"

# Keys accepted by HACS in hacs.json (custom_components/hacs/utils/validate.py).
HACS_JSON_KEYS = {
    "content_in_root",
    "country",
    "filename",
    "hacs",
    "hide_default_branch",
    "homeassistant",
    "persistent_directory",
    "render_readme",
    "zip_release",
    "name",
}


def manifest() -> dict:
    return json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))


def png_size(path: Path) -> tuple[int, int, int]:
    """(width, height, colour type) read from the PNG header."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height, _depth, colour = struct.unpack(">IIBB", data[16:26])
    return width, height, colour


def test_single_integration_in_custom_components() -> None:
    """HACS manages only the first sub-directory of custom_components."""
    dirs = [p.name for p in (ROOT / "custom_components").iterdir() if p.is_dir() and p.name != "__pycache__"]
    assert dirs == ["cel_ca"]


def test_hacs_json() -> None:
    data = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert data["name"]
    assert set(data) <= HACS_JSON_KEYS
    assert re.fullmatch(r"20\d\d\.\d+\.\d+", data["homeassistant"])
    assert data.get("country") in (None, "CH")
    assert not data.get("zip_release"), "releases are installed from the tag, no zip asset is built"


def test_manifest_for_hacs_and_hassfest() -> None:
    m = manifest()
    for key in ("domain", "name", "codeowners", "documentation", "issue_tracker", "version"):
        assert m[key], key
    assert m["domain"] == "cel_ca" and m["config_flow"] is True
    assert m["documentation"] == REPO_URL and m["issue_tracker"] == f"{REPO_URL}/issues"
    assert all(owner.startswith("@") for owner in m["codeowners"])
    # hassfest: domain and name first, then the other keys sorted
    keys = list(m)
    assert keys[:2] == ["domain", "name"] and keys[2:] == sorted(keys[2:])


def test_versions_are_consistent() -> None:
    from custom_components.cel_ca.core.node import SW_VERSION

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest()["version"] == pyproject["project"]["version"] == SW_VERSION


@pytest.mark.parametrize(("name", "size"), [("icon.png", 256), ("icon@2x.png", 512)])
def test_brand_images(name: str, size: int) -> None:
    """Home Assistant 2026.3+ and the HACS 'brands' check read custom_components/<domain>/brand/."""
    width, height, colour = png_size(INTEGRATION / "brand" / name)
    assert (width, height) == (size, size)
    assert colour == 6, "RGBA expected (transparent background)"


def test_license_is_osi_approved() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")


def test_readme_buttons_point_to_this_repository() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    owner, repo = REPO_URL.rsplit("/", 2)[-2:]
    assert (
        f"https://my.home-assistant.io/redirect/hacs_repository/?owner={owner}&repository={repo}&category=integration"
        in readme
    )
    assert "https://my.home-assistant.io/redirect/config_flow_start/?domain=cel_ca" in readme


def test_github_workflows() -> None:
    workflows = ROOT / ".github" / "workflows"
    validate = (workflows / "validate.yml").read_text(encoding="utf-8")
    assert "hacs/action@" in validate and "category: integration" in validate
    assert "home-assistant/actions/hassfest@" in validate
    release = (workflows / "release.yml").read_text(encoding="utf-8")
    assert "manifest.json" in release and "gh release create" in release
