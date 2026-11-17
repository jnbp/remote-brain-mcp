"""check_vault and recent_changes."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from remote_brain_mcp.errors import VaultError

FILES = {
    "Hub.md": "---\ntype: Area\n---\n# Hub\n[[Alpha]] [[Missing]] ![[pic.png]] [[Beta]]\n",
    "Projects/Alpha.md": "---\ntype: Project\nstatus: Active\nbelongs_to: \"[[Hub]]\"\n---\n# Alpha\n",
    "Projects/Gamma.md": "---\ntype: Project\n---\n# Gamma\n```\n[[NotALink]]\n```\n",
    "Archive/Beta.md": "---\ntype: Note\n---\nold\n",
    "Projects/Beta.md": "no frontmatter\n",
    "Broken.md": "---\nkey: [unclosed\n---\nbody\n",
    "img/pic.png": b"\x89PNG",
}


def kinds(result):
    return [(i["kind"], i["path"]) for i in result["issues"]]


def test_check_vault_finds_every_kind(make_vault):
    vault = make_vault(FILES)
    result = vault.check_vault(required_fields={"*": ["type"], "Project": ["status", "belongs_to"]})
    found = kinds(result)
    assert ("broken_link", "Hub.md") in found
    assert [i["link"] for i in result["issues"] if i["kind"] == "broken_link"] == ["missing"]
    ambiguous = [i for i in result["issues"] if i["kind"] == "ambiguous_link"]
    assert ambiguous[0]["matches"] == ["Archive/Beta.md", "Projects/Beta.md"]
    assert ("duplicate_name", "Archive/Beta.md") in found
    assert ("unreadable_frontmatter", "Broken.md") in found
    missing = {i["path"]: i["missing"] for i in result["issues"] if i["kind"] == "missing_fields"}
    assert missing == {"Projects/Gamma.md": ["status", "belongs_to"], "Projects/Beta.md": ["type"]}
    assert result["counts"]["broken_link"] == 1 and result["complete"] is True
    # the attachment link and the link inside a code block are not reported
    assert all(i.get("link") not in ("pic.png", "notalink") for i in result["issues"])


def test_check_vault_prefix_list_rules_and_limit(make_vault):
    vault = make_vault(FILES)
    result = vault.check_vault(path_prefix="Projects", required_fields=["type"])
    assert {p for _, p in kinds(result)} <= {"Projects/Beta.md", "Archive/Beta.md"}
    assert result["notes_checked"] == 3
    limited = vault.check_vault(limit=1)
    assert len(limited["issues"]) == 1 and limited["complete"] is False
    with pytest.raises(VaultError):
        vault.check_vault(required_fields="type")
    with pytest.raises(VaultError):
        vault.check_vault(required_fields={"Project": [1]})


def test_check_vault_reports_skipped_files(make_vault):
    vault = make_vault({"big.md": "x" * 3000, "ok.md": "fine\n"}, RB_MAX_FILE_BYTES="2048")
    assert ("skipped", "big.md") in kinds(vault.check_vault())


def test_recent_changes_since_and_hours(make_vault):
    vault = make_vault({"old.md": "old\n", "Inbox/new.md": "new\n"})
    old_time = time.time() - 3 * 86400
    os.utime(vault.paths.root / "old.md", (old_time, old_time))
    vault.scan()
    recent = vault.recent_changes()
    assert [c["path"] for c in recent["changes"]] == ["Inbox/new.md"]
    since = (datetime.now(UTC) - timedelta(days=5)).date().isoformat()
    assert {c["path"] for c in vault.recent_changes(since=since)["changes"]} == {"old.md", "Inbox/new.md"}
    assert vault.recent_changes(hours=200, path_prefix="Inbox")["changes"][0]["path"] == "Inbox/new.md"
    vault.create_note("Inbox/newer.md", "x\n")
    assert vault.recent_changes()["changes"][0]["path"] == "Inbox/newer.md"  # newest first


def test_recent_changes_lists_trash_and_validates(make_vault):
    vault = make_vault({"a.md": "x\n"})
    item = vault.move_to_trash("a.md")
    assert vault.recent_changes()["trashed"][0]["trash_id"] == item["trash_id"]
    with pytest.raises(VaultError):
        vault.recent_changes(since="yesterday")
    with pytest.raises(VaultError):
        vault.recent_changes(since="2026-01-01", hours=3)


def test_review_fixes(make_vault):
    vault = make_vault({
        "empty.md": "---\n\n---\nbody\n",
        "comment.md": "---\n# just a comment\n---\n",
        "lower.md": "---\ntype: project\n---\n",
    })
    result = vault.check_vault(required_fields={"Project": ["status"]})
    assert kinds(result) == [("missing_fields", "lower.md")]  # empty frontmatter is fine, type is case-insensitive


def test_recent_changes_respects_response_limit(make_vault):
    vault = make_vault({f"n{i:02d}.md": "x\n" for i in range(60)}, RB_MAX_RESPONSE_CHARS="2000")
    for i in range(60):
        vault.move_to_trash(f"n{i:02d}.md")
    result = vault.recent_changes()
    assert len(json.dumps(result, ensure_ascii=False)) <= 2000 and result["complete"] is False
    small = make_vault()
    assert small.recent_changes(limit=5)["complete"] is False  # 60 trashed items, limit 5
