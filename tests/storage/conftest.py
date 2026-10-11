"""Azurite (official Azure Storage emulator) fixture.

Skipped when Azurite is not installed: `npm install -g azurite` (CI does it).
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

import pytest

ACCOUNT = "devstoreaccount1"
KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="


def _azurite_binary() -> str | None:
    for candidate in (os.environ.get("AZURITE"), shutil.which("azurite"), "/tmp/azurite/node_modules/.bin/azurite"):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Azurite:
    table_endpoint: str
    queue_endpoint: str
    account: str = ACCOUNT
    key: str = KEY

    def admin(self):
        from custom_components.cel_ca.core.storage_admin import StorageAdmin

        return StorageAdmin(self.account, self.key, self.table_endpoint, self.queue_endpoint)


def start_azurite(workdir: Path | None = None) -> tuple[subprocess.Popen, Azurite] | None:
    binary = _azurite_binary()
    if binary is None:
        return None
    import pytest_socket

    pytest_socket.enable_socket()
    tport, qport = _free_port(), _free_port()
    proc = subprocess.Popen(
        [binary, "--silent", "--skipApiVersionCheck", "--inMemoryPersistence",
         "--blobPort", str(_free_port()), "--queuePort", str(qport), "--tablePort", str(tport)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", tport), 0.2), socket.create_connection(("127.0.0.1", qport), 0.2):
                return proc, Azurite(f"http://127.0.0.1:{tport}/{ACCOUNT}", f"http://127.0.0.1:{qport}/{ACCOUNT}")
        except OSError:
            time.sleep(0.1)
    proc.kill()
    return None


@pytest.fixture(scope="session")
def azurite_server(tmp_path_factory) -> Generator[Azurite]:
    started = start_azurite(tmp_path_factory.mktemp("azurite"))
    if started is None:
        pytest.skip("Azurite not installed or did not start")
    proc, server = started
    yield server
    proc.kill()
    proc.wait(10)


@pytest.fixture
def azurite(azurite_server) -> Azurite:
    """Allow localhost sockets (the HA test plugin blocks them by default)."""
    import pytest_socket

    pytest_socket.enable_socket()
    return azurite_server
