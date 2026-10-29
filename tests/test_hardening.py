"""Regression tests for hostile input and edge cases found in review."""

from __future__ import annotations

import json
import os
import time
import unicodedata

import pytest

from remote_brain_mcp.config import Config, ConfigError
from remote_brain_mcp.errors import VaultError

from .conftest import TOKEN, write

SELF_ALIAS = "---\nx: &a [*a]\n---\n# Loop\nselfref body\n"
DEEP = "---\nx: " + "[" * 5000 + "]" * 5000 + "\n---\n# Deep\ndeep body\n"
LAUGHS = (
    "---\n"
    'a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]\n'
    + "".join(
        f"{chr(98 + i)}: &{chr(98 + i)} [" + ",".join([f"*{chr(97 + i)}"] * 9) + "]\n" for i in range(8)
    )
    + "---\n# Bomb\nbomb body\n"
)


def test_hostile_frontmatter_never_breaks_startup_or_search(make_vault):
    started = time.monotonic()
    vault = make_vault({"loop.md": SELF_ALIAS, "deep.md": DEEP, "bomb.md": LAUGHS, "ok.md": "fine\n"})
    assert time.monotonic() - started < 10
    for word, path in [("selfref", "loop.md"), ("deep", "deep.md"), ("bomb", "bomb.md")]:
        assert [h["path"] for h in vault.search(word)["hits"]] == [path]
    # frontmatter of those notes is ignored, not expanded
    assert vault.find_by_frontmatter({"x": "lol"})["results"] == []


def test_hostile_frontmatter_through_create_note(make_vault):
    vault = make_vault()
    started = time.monotonic()
    for name, text in [("a.md", SELF_ALIAS), ("b.md", DEEP), ("c.md", LAUGHS)]:
        vault.create_note(name, text)
    assert time.monotonic() - started < 10
    assert vault.get_status()["note_count"] == 3
    vault.scan()  # a later scan (or restart) must also survive
    make_vault()  # restart on the same vault and index


@pytest.mark.parametrize("token", ["change-me", "replace-with-a-long-random-secret",
                                   "x" * 20 + "your-token" + "x" * 20, "EXAMPLE" + "x" * 40])
def test_placeholder_tokens_are_refused(tmp_path, token):
    with pytest.raises(ConfigError):
        Config.from_env({"RB_TOKEN": token, "RB_VAULT_PATH": str(tmp_path)})


def test_decomposed_unicode_file_names_are_fully_usable(make_vault):
    nfd = unicodedata.normalize("NFD", "Folder Ä/Über.md")
    vault = make_vault({nfd: "# Über\numlaut body\n"}, RB_REQUIRE_HASH_FOR_EDITS="false")
    nfc = "Folder Ä/Über.md"
    assert vault.search("umlaut")["hits"][0]["path"] == nfc
    assert vault.read_note(nfc)["content"].startswith("# Über")
    vault.edit_note(nfc, "umlaut body", "changed body")
    assert vault.search("changed")["hits"][0]["path"] == nfc
    item = vault.move_to_trash(nfc)
    assert vault.search("changed")["hits"] == []
    vault.restore_from_trash(item["trash_id"])
    assert vault.read_note(nfc)["content"] == "# Über\nchanged body\n"
    vault.scan()
    assert vault.get_status()["note_count"] == 1


@pytest.mark.parametrize("char", ['"', "\\", "\x01"])
def test_response_cap_counts_json_escaping(make_vault, char):
    vault = make_vault({"esc.md": (char * 70 + "\n") * 1500}, RB_MAX_RESPONSE_CHARS="20000")
    result = vault.read_note("esc.md", line_count=2000)
    assert len(json.dumps(result, ensure_ascii=False)) <= 20000
    assert result["complete"] is False


def test_single_huge_line_is_flagged(make_vault):
    vault = make_vault({"one.md": "z" * 100_000}, RB_MAX_RESPONSE_CHARS="10000", RB_MAX_FILE_BYTES="200000")
    result = vault.read_note("one.md")
    assert result["line_truncated"] is True and result["complete"] is False
    assert len(json.dumps(result)) <= 10000


def test_restore_refuses_symlinked_folder_inside_trash_entry(make_vault, tmp_path):
    vault = make_vault({"Dir/a.md": "x\n"})
    item = vault.move_to_trash("Dir/a.md")
    entry_dir = vault.paths.trash_root / item["trash_id"] / "files"
    real = entry_dir / "Dir"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "a.md").write_text("planted\n")
    os.rename(real, tmp_path / "moved-away")
    os.symlink(outside, real)
    with pytest.raises(VaultError) as exc:
        vault.restore_from_trash(item["trash_id"])
    assert exc.value.code == "outside_vault"
    assert not (vault.paths.root / "Dir/a.md").exists()


def test_bad_frontmatter_is_reported_but_body_still_searchable(make_vault):
    vault = make_vault({"broken.md": "---\nkey: [unclosed\n---\nstill findable\n"})
    assert vault.search("findable")["hits"][0]["path"] == "broken.md"


def test_config_origins_list(tmp_path):
    config = Config.from_env({"RB_TOKEN": TOKEN, "RB_VAULT_PATH": str(tmp_path),
                              "RB_ALLOWED_ORIGINS": "https://a.example, https://b.example"})
    assert config.allowed_origins == ["https://a.example", "https://b.example"]


def test_write_helper_exists():
    assert callable(write)


def test_decomposed_exclude_pattern_still_hides_folder(make_vault):
    nfd = unicodedata.normalize("NFD", "Privát")
    vault = make_vault({f"{nfd}/secret.md": "hidden text\n"}, RB_EXCLUDE=nfd)
    with pytest.raises(VaultError) as exc:
        vault.read_note("Privát/secret.md")
    assert exc.value.code == "hidden_path"
    assert vault.search("hidden")["hits"] == []


def test_restore_refuses_symlinked_trash_folder(make_vault, tmp_path):
    vault = make_vault({"a.md": "x\n"})
    item = vault.move_to_trash("a.md")
    os.rename(vault.paths.trash_root, tmp_path / "real-trash")
    os.symlink(tmp_path / "real-trash", vault.paths.trash_root)
    with pytest.raises(VaultError) as exc:
        vault.restore_from_trash(item["trash_id"])
    assert exc.value.code == "outside_vault"
