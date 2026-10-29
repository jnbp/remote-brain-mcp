from __future__ import annotations

import hashlib
import os
import stat

import pytest

from remote_brain_mcp.errors import VaultError
from remote_brain_mcp.fsops import TEMP_PREFIX


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def no_temp_files(root) -> bool:
    return not any(name.startswith(TEMP_PREFIX) for _, _, names in os.walk(root) for name in names)


def test_create_note_and_index_immediately(make_vault):
    vault = make_vault()
    result = vault.create_note("New/Folder/note.md", "# Note\nunique-word\n")
    assert result["sha256"] == sha(b"# Note\nunique-word\n")
    assert vault.search("unique-word")["hits"][0]["path"] == "New/Folder/note.md"
    assert no_temp_files(vault.paths.root)


def test_create_note_refuses_existing_and_missing_folder(make_vault):
    vault = make_vault({"a.md": "old\n"})
    with pytest.raises(VaultError) as exc:
        vault.create_note("a.md", "new")
    assert exc.value.code == "already_exists"
    assert (vault.paths.root / "a.md").read_text() == "old\n"
    with pytest.raises(VaultError) as exc:
        vault.create_note("missing/b.md", "x", create_folders=False)
    assert exc.value.code == "not_found"
    with pytest.raises(VaultError) as exc:
        vault.create_note("image.png", "x")
    assert exc.value.code == "not_a_note"


def test_create_note_warns_about_duplicate_names(make_vault):
    vault = make_vault({"A/Topic.md": "x\n"})
    result = vault.create_note("B/topic.md", "y\n")
    assert "duplicate_name" in result["warnings"][0]


def test_edit_requires_hash_and_detects_conflict(make_vault):
    vault = make_vault({"a.md": "alpha beta gamma\n"})
    with pytest.raises(VaultError) as exc:
        vault.edit_note("a.md", "beta", "BETA")
    assert exc.value.code == "invalid_argument"
    with pytest.raises(VaultError) as exc:
        vault.edit_note("a.md", "beta", "BETA", expected_sha256="0" * 64)
    assert exc.value.code == "conflict"
    assert exc.value.details["current_sha256"] == sha(b"alpha beta gamma\n")
    current = vault.read_note("a.md")["sha256"]
    result = vault.edit_note("a.md", "beta", "BETA", expected_sha256=current)
    assert (vault.paths.root / "a.md").read_bytes() == b"alpha BETA gamma\n"
    assert result["sha256"] == sha(b"alpha BETA gamma\n")
    assert vault.search("BETA")["hits"][0]["path"] == "a.md"


def test_edit_hash_optional_when_configured(make_vault):
    vault = make_vault({"a.md": "one two\n"}, RB_REQUIRE_HASH_FOR_EDITS="false")
    vault.edit_note("a.md", "two", "three")
    assert (vault.paths.root / "a.md").read_text() == "one three\n"


def test_edit_no_match_and_overlapping_matches(make_vault):
    vault = make_vault({"a.md": "aaa\n"}, RB_REQUIRE_HASH_FOR_EDITS="false")
    with pytest.raises(VaultError) as exc:
        vault.edit_note("a.md", "zzz", "x")
    assert exc.value.code == "no_match"
    with pytest.raises(VaultError) as exc:
        vault.edit_note("a.md", "aa", "x")
    assert exc.value.code == "ambiguous_match" and exc.value.details["occurrences"] == 2
    assert (vault.paths.root / "a.md").read_text() == "aaa\n"


def test_edit_crlf_file_keeps_line_endings(make_vault):
    vault = make_vault({"w.md": b"first\r\nsecond\r\nthird\r\n"}, RB_REQUIRE_HASH_FOR_EDITS="false")
    vault.edit_note("w.md", "first\nsecond", "first\nSECOND")
    assert (vault.paths.root / "w.md").read_bytes() == b"first\r\nSECOND\r\nthird\r\n"


def test_append_adds_separator_once(make_vault):
    vault = make_vault({"a.md": "no newline", "b.md": "has newline\n", "c.md": b"crlf\r\nend"})
    vault.append_to_note("a.md", "next\n")
    vault.append_to_note("b.md", "next\n")
    vault.append_to_note("c.md", "next")
    assert (vault.paths.root / "a.md").read_text() == "no newline\nnext\n"
    assert (vault.paths.root / "b.md").read_text() == "has newline\nnext\n"
    assert (vault.paths.root / "c.md").read_bytes() == b"crlf\r\nend\r\nnext"
    with pytest.raises(VaultError) as exc:
        vault.append_to_note("b.md", "x", expected_sha256="1" * 64)
    assert exc.value.code == "conflict"


def test_replace_requires_hash(make_vault):
    vault = make_vault({"a.md": "old\n"})
    with pytest.raises(VaultError):
        vault.replace_note("a.md", "new\n", expected_sha256=None)
    vault.replace_note("a.md", "new\n", expected_sha256=sha(b"old\n"))
    assert (vault.paths.root / "a.md").read_text() == "new\n"


def test_size_and_disk_limits(make_vault):
    vault = make_vault({"a.md": "x\n"}, RB_MAX_FILE_BYTES="1024", RB_REQUIRE_HASH_FOR_EDITS="false")
    with pytest.raises(VaultError) as exc:
        vault.append_to_note("a.md", "y" * 2000)
    assert exc.value.code == "too_large"
    with pytest.raises(VaultError) as exc:
        vault.create_note("big.md", "y" * 2000)
    assert exc.value.code == "too_large"
    assert (vault.paths.root / "a.md").read_text() == "x\n"

    full = make_vault({"b.md": "x\n"}, RB_MIN_FREE_BYTES=str(10**18))
    with pytest.raises(VaultError) as exc:
        full.create_note("c.md", "y")
    assert exc.value.code == "disk_full"


def test_writes_keep_file_mode(make_vault):
    vault = make_vault({"a.md": "x\n"}, RB_REQUIRE_HASH_FOR_EDITS="false")
    os.chmod(vault.paths.root / "a.md", 0o600)
    vault.edit_note("a.md", "x", "y")
    assert stat.S_IMODE(os.stat(vault.paths.root / "a.md").st_mode) == 0o600


def test_move_file_and_folder(make_vault):
    vault = make_vault({"a.md": "apple\n", "F/b.md": "banana\n", "F/sub/c.md": "cherry\n", "other.md": "o\n"})
    vault.move_note("a.md", "Moved/a.md", create_folders=True)
    assert vault.search("apple")["hits"][0]["path"] == "Moved/a.md"
    vault.move_note("F", "G")
    assert vault.search("cherry")["hits"][0]["path"] == "G/sub/c.md"
    assert not (vault.paths.root / "F").exists()


def test_move_refusals(make_vault):
    vault = make_vault({"a.md": "x\n", "b.md": "y\n", "F/c.md": "z\n"})
    for args, code in [
        (("a.md", "b.md"), "already_exists"),
        (("a.md", "missing/a.md"), "not_found"),
        (("F", "F/inner"), "invalid_argument"),
        (("nope.md", "x.md"), "not_found"),
        (("a.md", "a.md"), "invalid_argument"),
        (("a.md", ".hidden/a.md"), "hidden_path"),
    ]:
        with pytest.raises(VaultError) as exc:
            vault.move_note(*args)
        assert exc.value.code == code, args
    assert (vault.paths.root / "b.md").read_text() == "y\n"


def test_temp_files_are_cleaned_and_hidden(make_vault, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / f"{TEMP_PREFIX}leftover").write_text("partial")
    vault = make_vault({"a.md": "x\n"})
    assert not (root / f"{TEMP_PREFIX}leftover").exists()
    assert [e["path"] for e in vault.list_folder()["entries"]] == ["a.md"]
