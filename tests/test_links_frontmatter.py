from __future__ import annotations

import unicodedata

import pytest

from remote_brain_mcp.errors import VaultError

FILES = {
    "Hub.md": "# Hub\n",
    "Projects/Alpha.md": (
        "---\ntype: Project\nstatus: Active\nbelongs_to: \"[[Hub]]\"\nrelated_to:\n  - \"[[Beta]]\"\n"
        "  - \"[[Gamma|g]]\"\ndone: false\n---\n# Alpha\nSee [[Hub#Status]] and ![[Beta]].\n"
        "```\n[[NotALink]]\n```\n| [[Hub\\|alias]] |\n"
    ),
    "Projects/Beta.md": "---\ntype: Project\nstatus: Paused\n---\nlinks to [[Projects/Alpha]]\n",
    "Archive/Beta.md": "---\ntype: Note\n---\nold beta\n",
    "Café.md": "# Café\n",
}


def test_resolve_link_forms(make_vault):
    vault = make_vault(FILES)
    assert vault.resolve_link("Hub")["matches"] == ["Hub.md"]
    assert vault.resolve_link("[[hub#Status]]")["matches"] == ["Hub.md"]
    assert vault.resolve_link("![[Hub|x]]")["matches"] == ["Hub.md"]
    assert vault.resolve_link("Hub.md")["matches"] == ["Hub.md"]
    assert vault.resolve_link("Projects/Alpha")["matches"] == ["Projects/Alpha.md"]
    beta = vault.resolve_link("Beta")
    assert beta["ambiguous"] is True and beta["matches"] == ["Archive/Beta.md", "Projects/Beta.md"]
    assert vault.resolve_link("Missing")["matches"] == []
    assert vault.resolve_link(unicodedata.normalize("NFD", "Café"))["matches"] == ["Café.md"]  # NFD finds NFC
    with pytest.raises(VaultError):
        vault.resolve_link("  ")


def test_backlinks_ignore_code_blocks_and_handle_tables(make_vault):
    vault = make_vault(FILES)
    lines = [(b["path"], b["line"]) for b in vault.get_backlinks("Hub.md")["backlinks"]]
    assert ("Projects/Alpha.md", 4) in lines  # frontmatter belongs_to
    assert ("Projects/Alpha.md", 11) in lines  # [[Hub#Status]]
    assert ("Projects/Alpha.md", 15) in lines  # table link with escaped pipe
    assert vault.resolve_link("NotALink")["matches"] == []
    alpha = vault.get_backlinks("Projects/Alpha.md")["backlinks"]
    assert [b["path"] for b in alpha] == ["Projects/Beta.md"]
    assert vault.get_backlinks("Projects/Beta.md")["ambiguous_name"] is True


def test_find_by_frontmatter(make_vault):
    vault = make_vault(FILES)
    found = vault.find_by_frontmatter({"type": "project"})
    assert [r["path"] for r in found["results"]] == ["Projects/Alpha.md", "Projects/Beta.md"]
    assert [r["path"] for r in vault.find_by_frontmatter({"belongs_to": "[[Hub]]"})["results"]] == [
        "Projects/Alpha.md"
    ]
    assert [r["path"] for r in vault.find_by_frontmatter({"related_to": "Gamma"})["results"]] == [
        "Projects/Alpha.md"
    ]
    assert [r["path"] for r in vault.find_by_frontmatter({"done": False})["results"]] == ["Projects/Alpha.md"]
    combined = vault.find_by_frontmatter({"type": "Project", "status": "Paused"})
    assert [r["path"] for r in combined["results"]] == ["Projects/Beta.md"]
    assert combined["results"][0]["fields"] == {"type": "Project", "status": "Paused"}
    assert vault.find_by_frontmatter({"type": "Note"}, path_prefix="Projects")["results"] == []
    with pytest.raises(VaultError):
        vault.find_by_frontmatter({})
