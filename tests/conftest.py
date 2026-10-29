from __future__ import annotations

from pathlib import Path

import pytest

from remote_brain_mcp.config import Config
from remote_brain_mcp.vault import Vault

TOKEN = "t" * 48


def write(root: Path, rel: str, text: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_bytes(text.encode("utf-8"))
    return path


@pytest.fixture
def make_vault(tmp_path: Path):
    def factory(files: dict[str, str | bytes] | None = None, **env: str) -> Vault:
        root = tmp_path / "vault"
        root.mkdir(exist_ok=True)
        for rel, text in (files or {}).items():
            write(root, rel, text)
        settings = {
            "RB_TOKEN": TOKEN,
            "RB_VAULT_PATH": str(root),
            "RB_DATA_PATH": str(tmp_path / "data"),
            "RB_MIN_FREE_BYTES": "0",
        }
        settings.update(env)
        vault = Vault(Config.from_env(settings))
        vault.startup()
        return vault

    return factory
