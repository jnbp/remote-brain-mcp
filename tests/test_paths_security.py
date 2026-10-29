from __future__ import annotations

import os

import pytest

from remote_brain_mcp.config import Config, ConfigError
from remote_brain_mcp.errors import VaultError

from .conftest import TOKEN, write


def code_of(excinfo) -> str:
    return excinfo.value.code


@pytest.mark.parametrize(
    "path, code",
    [
        ("/etc/passwd", "outside_vault"),
        ("../outside.md", "outside_vault"),
        ("a/../../outside.md", "outside_vault"),
        ("a/./b.md", "outside_vault"),
        ("a//b.md", "invalid_argument"),
        ("a\x00b.md", "invalid_argument"),
        (".git/config.md", "hidden_path"),
        (".rb-trash/x.md", "hidden_path"),
        ("@eaDir/x.md", "hidden_path"),
        ("#recycle/x.md", "hidden_path"),
        ("folder/.hidden.md", "hidden_path"),
    ],
)
def test_bad_paths_are_rejected_for_reads_and_writes(make_vault, path, code):
    vault = make_vault({"ok.md": "x\n"})
    with pytest.raises(VaultError) as exc:
        vault.read_note(path)
    assert code_of(exc) == code
    with pytest.raises(VaultError) as exc:
        vault.create_note(path, "data")
    assert code_of(exc) == code


def test_symlinked_folder_pointing_outside_is_never_followed(make_vault, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, "secret.md", "TOPSECRET\n")
    vault = make_vault({"ok.md": "hello\n"})
    os.symlink(outside, vault.paths.root / "link")
    vault.scan()

    with pytest.raises(VaultError) as exc:
        vault.read_note("link/secret.md")
    assert code_of(exc) == "outside_vault"
    with pytest.raises(VaultError) as exc:
        vault.create_note("link/new.md", "x")
    assert code_of(exc) == "outside_vault"
    assert vault.search("TOPSECRET")["hits"] == []


def test_symlink_content_never_reaches_search_or_listing(make_vault, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, "secret.md", "TOPSECRET\n")
    vault = make_vault({"ok.md": "hello\n"})
    os.symlink(outside, vault.paths.root / "link")
    os.symlink(outside / "secret.md", vault.paths.root / "file-link.md")
    vault.scan()
    assert vault.search("TOPSECRET")["hits"] == []
    listed = [e["path"] for e in vault.list_folder(recursive=True)["entries"]]
    assert listed == ["ok.md"]
    skipped = {s["path"] for s in vault.get_status()["skipped"]}
    assert {"link", "file-link.md"} <= skipped
    with pytest.raises(VaultError) as exc:
        vault.read_note("file-link.md")
    assert code_of(exc) == "outside_vault"


def test_move_and_trash_refuse_symlinks(make_vault, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    vault = make_vault({"ok.md": "x\n"})
    os.symlink(outside, vault.paths.root / "link")
    with pytest.raises(VaultError) as exc:
        vault.move_note("ok.md", "link/ok.md")
    assert code_of(exc) == "outside_vault"
    with pytest.raises(VaultError) as exc:
        vault.move_to_trash("link")
    assert code_of(exc) == "outside_vault"
    assert (vault.paths.root / "ok.md").exists()


def test_exclude_patterns_hide_paths(make_vault):
    vault = make_vault({"Private/a.md": "secret\n", "b.md": "public\n"}, RB_EXCLUDE="Private")
    assert vault.search("secret")["hits"] == []
    with pytest.raises(VaultError) as exc:
        vault.read_note("Private/a.md")
    assert code_of(exc) == "hidden_path"


def test_config_requires_long_token(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_env({"RB_TOKEN": "short", "RB_VAULT_PATH": str(tmp_path)})
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    config = Config.from_env({"RB_TOKEN_FILE": str(token_file), "RB_VAULT_PATH": str(tmp_path)})
    assert config.token == TOKEN


def test_config_rejects_unsafe_trash_name(tmp_path):
    with pytest.raises(ConfigError):
        Config.from_env({"RB_TOKEN": TOKEN, "RB_VAULT_PATH": str(tmp_path), "RB_TRASH_DIR": "trash"})
    with pytest.raises(ConfigError):
        Config.from_env({"RB_TOKEN": TOKEN, "RB_VAULT_PATH": str(tmp_path), "RB_TRASH_DIR": "../x"})
