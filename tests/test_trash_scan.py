from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from remote_brain_mcp.errors import VaultError

from .conftest import write


def test_trash_and_restore_file(make_vault):
    vault = make_vault({"Notes/a.md": "apple\n"})
    item = vault.move_to_trash("Notes/a.md")
    assert not (vault.paths.root / "Notes/a.md").exists()
    assert vault.search("apple")["hits"] == []
    listed = vault.list_trash()["items"]
    assert listed[0]["trash_id"] == item["trash_id"] and listed[0]["original_path"] == "Notes/a.md"
    assert item["expires_at"] is not None
    vault.restore_from_trash(item["trash_id"])
    assert (vault.paths.root / "Notes/a.md").read_text() == "apple\n"
    assert vault.search("apple")["hits"][0]["path"] == "Notes/a.md"
    assert vault.list_trash()["items"] == []
    assert not (vault.paths.trash_root / item["trash_id"]).exists()


def test_note_named_meta_json_does_not_collide(make_vault):
    vault = make_vault({"meta.json": "{\"user\": \"data\"}"})
    item = vault.move_to_trash("meta.json")
    vault.restore_from_trash(item["trash_id"])
    assert (vault.paths.root / "meta.json").read_text() == "{\"user\": \"data\"}"


def test_trash_folder_and_restore_to_other_path(make_vault):
    vault = make_vault({"F/a.md": "one\n", "F/sub/b.md": "two\n"})
    item = vault.move_to_trash("F")
    assert vault.search("two")["hits"] == []
    result = vault.restore_from_trash(item["trash_id"], target_path="Restored/F")
    assert result["restored_to"] == "Restored/F"
    assert vault.search("two")["hits"][0]["path"] == "Restored/F/sub/b.md"


def test_restore_refuses_existing_target_and_recreates_missing_folder(make_vault):
    vault = make_vault({"Deep/Er/a.md": "x\n"})
    item = vault.move_to_trash("Deep/Er/a.md")
    os.rmdir(vault.paths.root / "Deep/Er")
    os.rmdir(vault.paths.root / "Deep")
    write(vault.paths.root, "Other.md", "o\n")
    with pytest.raises(VaultError) as exc:
        vault.restore_from_trash(item["trash_id"], target_path="Other.md")
    assert exc.value.code == "already_exists"
    vault.restore_from_trash(item["trash_id"])
    assert (vault.paths.root / "Deep/Er/a.md").exists()


def test_tampered_meta_cannot_escape(make_vault, tmp_path):
    vault = make_vault({"a.md": "x\n"})
    item = vault.move_to_trash("a.md")
    meta_path = vault.paths.trash_root / item["trash_id"] / "meta.json"
    meta = json.loads(meta_path.read_text())
    for evil in ["../../outside.md", "/etc/passwd", ".rb-trash/x.md"]:
        meta["original_path"] = evil
        meta_path.write_text(json.dumps(meta))
        with pytest.raises(VaultError) as exc:
            vault.restore_from_trash(item["trash_id"])
        assert exc.value.code in ("outside_vault", "hidden_path", "not_found")
    assert not (tmp_path / "outside.md").exists()


def test_bad_trash_ids_are_rejected(make_vault):
    vault = make_vault({"a.md": "x\n"})
    for bad in ["../x", "20261029T141500Z-3f9a1c/../..", "", "nope"]:
        with pytest.raises(VaultError) as exc:
            vault.restore_from_trash(bad)
        assert exc.value.code == "invalid_argument"


def test_purge_removes_only_expired_valid_entries(make_vault, tmp_path):
    vault = make_vault({"a.md": "x\n", "b.md": "y\n"})
    old = vault.move_to_trash("a.md")
    new = vault.move_to_trash("b.md")
    # make "old" 100 days old
    meta_path = vault.paths.trash_root / old["trash_id"] / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["trashed_at"] = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    meta_path.write_text(json.dumps(meta))
    # foreign content in the trash folder must be left alone
    write(vault.paths.trash_root, "foreign-file.md", "keep\n")
    write(vault.paths.trash_root, "20200101T000000Z-aaaaaa/no-meta.md", "keep\n")
    # a symlink inside an expired entry must not be followed
    outside = tmp_path / "outside.md"
    outside.write_text("must survive\n")
    os.symlink(outside, vault.paths.trash_root / old["trash_id"] / "files" / "link.md")

    assert vault.purge_trash() == 1
    assert not (vault.paths.trash_root / old["trash_id"]).exists()
    assert (vault.paths.trash_root / new["trash_id"]).exists()
    assert (vault.paths.trash_root / "foreign-file.md").exists()
    assert (vault.paths.trash_root / "20200101T000000Z-aaaaaa/no-meta.md").exists()
    assert outside.read_text() == "must survive\n"


def test_purge_disabled_with_zero_retention(make_vault):
    vault = make_vault({"a.md": "x\n"}, RB_TRASH_RETENTION_DAYS="0")
    item = vault.move_to_trash("a.md")
    assert item["expires_at"] is None
    assert vault.purge_trash(datetime.now(UTC) + timedelta(days=10_000)) == 0


def test_trash_root_itself_is_never_addressable(make_vault):
    vault = make_vault({"a.md": "x\n"})
    vault.move_to_trash("a.md")
    with pytest.raises(VaultError) as exc:
        vault.move_to_trash(".rb-trash")
    assert exc.value.code == "hidden_path"
    with pytest.raises(VaultError):
        vault.move_to_trash(".")


def test_scan_picks_up_external_changes(make_vault):
    vault = make_vault({"a.md": "apple\n", "b.md": "banana\n"})
    root = vault.paths.root
    write(root, "c.md", "cherry\n")
    (root / "a.md").write_text("apricot\n")
    os.utime(root / "a.md", (time.time() + 5, time.time() + 5))
    os.unlink(root / "b.md")
    result = vault.scan()
    assert result == {"added": 1, "updated": 1, "removed": 1}
    assert vault.search("cherry")["hits"] and vault.search("apricot")["hits"]
    assert vault.search("banana")["hits"] == [] and vault.search("apple")["hits"] == []


def test_scan_reports_invalid_and_large_files(make_vault):
    vault = make_vault({"bin.md": b"\xff\xfe", "big.md": "x" * 5000, "ok.md": "fine\n"}, RB_MAX_FILE_BYTES="2048")
    reasons = {s["path"]: s["reason"] for s in vault.get_status()["skipped"]}
    assert reasons == {"bin.md": "not valid UTF-8", "big.md": "too large"}
    assert vault.get_status()["note_count"] == 1


def test_index_survives_restart(make_vault, tmp_path):
    vault = make_vault({"a.md": "persistent\n"})
    again = make_vault()
    assert again.search("persistent")["hits"][0]["path"] == "a.md"
    assert vault.index.get_meta("schema") == "1"
