"""Link rewriting on move, and editing by heading."""

from __future__ import annotations

import pytest

from remote_brain_mcp.errors import VaultError

ALPHA = (
    "---\ntype: Project\nbelongs_to: \"[[Hub]]\"\n---\n# Alpha\n"
    "See [[Hub#Status]], [[Hub|start page]], [[hub]] and ![[Hub]].\n"
    "| col | [[Hub\\|alias]] |\n"
    "```\n[[Hub]] stays in code\n```\n"
    "File link [[Hub.md]] and path link [[Areas/Hub]].\n"
)


FM_REASON = ("link in frontmatter not updated: the new name needs YAML quoting "
             "or the frontmatter cannot be read")


def text(vault, rel):
    return (vault.paths.root / rel).read_text(encoding="utf-8")


def test_rename_updates_every_link_form(make_vault):
    vault = make_vault({"Areas/Hub.md": "# Hub\n", "Projects/Alpha.md": ALPHA})
    result = vault.move_note("Areas/Hub.md", "Areas/Zentrale.md")
    updated = text(vault, "Projects/Alpha.md")
    assert 'belongs_to: "[[Zentrale]]"' in updated
    assert "[[Zentrale#Status]], [[Zentrale|start page]], [[Zentrale]] and ![[Zentrale]]." in updated
    assert "| col | [[Zentrale\\|alias]] |" in updated
    assert "[[Hub]] stays in code" in updated
    assert "File link [[Zentrale.md]] and path link [[Areas/Zentrale]]." in updated
    assert result["links_updated"] == [{"path": "Projects/Alpha.md", "links": 8}]
    # index follows: backlinks and frontmatter point to the new name
    assert next(b["path"] for b in vault.get_backlinks("Areas/Zentrale.md")["backlinks"]) == "Projects/Alpha.md"
    assert vault.find_by_frontmatter({"belongs_to": "Zentrale"})["results"][0]["path"] == "Projects/Alpha.md"


def test_move_to_other_folder_keeps_bare_links_and_fixes_path_links(make_vault):
    vault = make_vault({"Old/Hub.md": "# Hub\n", "a.md": "[[Hub]] and [[Old/Hub|x]]\n"})
    result = vault.move_note("Old/Hub.md", "New/Hub.md", create_folders=True)
    assert text(vault, "a.md") == "[[Hub]] and [[New/Hub|x]]\n"
    assert result["links_updated"] == [{"path": "a.md", "links": 1}]


def test_new_name_that_would_be_ambiguous_uses_full_path(make_vault):
    vault = make_vault({"A/Note.md": "x\n", "Projects/Topic.md": "y\n", "ref.md": "see [[Note]]\n"})
    vault.move_note("A/Note.md", "B/Topic.md", create_folders=True)
    assert text(vault, "ref.md") == "see [[B/Topic]]\n"


def test_ambiguous_old_name_is_not_guessed(make_vault):
    vault = make_vault({
        "Archive/Beta.md": "old\n", "Projects/Beta.md": "new\n",
        "ref.md": "[[Beta]] and [[Archive/Beta]]\n",
    })
    result = vault.move_note("Archive/Beta.md", "Archive/Beta old.md")
    assert text(vault, "ref.md") == "[[Beta]] and [[Archive/Beta old]]\n"
    assert result["links_not_updated"] == [
        {"link": "Beta", "reason": "ambiguous before the move: several files have this name"}
    ]


def test_folder_move_updates_path_links_inside_and_outside(make_vault):
    vault = make_vault({
        "F/a.md": "link to [[F/b]] and [[b]]\n",
        "F/b.md": "b\n",
        "x.md": "[[F/a]] [[a]]\n",
    })
    result = vault.move_note("F", "G")
    assert text(vault, "x.md") == "[[G/a]] [[a]]\n"
    assert text(vault, "G/a.md") == "link to [[G/b]] and [[b]]\n"
    assert {u["path"] for u in result["links_updated"]} == {"x.md", "G/a.md"}


def test_attachment_rename_updates_embeds(make_vault):
    vault = make_vault({"img/pic.png": b"\x89PNG", "n.md": "![[pic.png]] and ![[pic.png|200]]\n"})
    vault.move_note("img/pic.png", "img/photo.png")
    assert text(vault, "n.md") == "![[photo.png]] and ![[photo.png|200]]\n"


def test_update_links_can_be_switched_off(make_vault):
    vault = make_vault({"Hub.md": "x\n", "a.md": "[[Hub]]\n"})
    result = vault.move_note("Hub.md", "Other.md", update_links=False)
    assert text(vault, "a.md") == "[[Hub]]\n"
    assert "links_updated" not in result


SECTIONS = (
    "# Title\n\n## Status\nold status\n\n## Backlog\n- a\n### Sub\nsub\n## End\nlast\n"
)


def test_replace_section_requires_hash_and_keeps_spacing(make_vault):
    vault = make_vault({"s.md": SECTIONS})
    with pytest.raises(VaultError) as exc:
        vault.edit_section("s.md", "Status", "new status")
    assert exc.value.code == "invalid_argument"
    sha = vault.read_note("s.md")["sha256"]
    result = vault.edit_section("s.md", "Status", "new status", expected_sha256=sha)
    assert text(vault, "s.md") == SECTIONS.replace("old status\n", "new status\n")
    assert result["heading_line"] == 3


def test_append_and_prepend_include_subsections(make_vault):
    vault = make_vault({"s.md": SECTIONS})
    vault.edit_section("s.md", "## Backlog", "- b", mode="append")
    vault.edit_section("s.md", "Backlog", "- first", mode="prepend")
    assert "## Backlog\n- first\n- a\n### Sub\nsub\n- b\n## End\n" in text(vault, "s.md")


def test_section_matching_rules(make_vault):
    vault = make_vault({
        "m.md": "## Status ##\nA\n### Status\nB\n```\n## Status\n```\n## Other\nC",
    }, RB_REQUIRE_HASH_FOR_EDITS="false")
    with pytest.raises(VaultError) as exc:
        vault.edit_section("m.md", "status", "x")
    assert exc.value.code == "ambiguous_match" and exc.value.details["lines"] == [1, 3]
    vault.edit_section("m.md", "### Status", "B2")
    # the code block belongs to the ### Status section; the heading inside it is ignored
    assert text(vault, "m.md") == "## Status ##\nA\n### Status\nB2\n\n## Other\nC"
    with pytest.raises(VaultError) as exc:
        vault.edit_section("m.md", "Missing", "x")
    assert exc.value.code == "no_match"
    vault.edit_section("m.md", "Other", "D", mode="append")  # last section without trailing newline
    assert text(vault, "m.md").endswith("## Other\nC\nD\n")


def test_section_edit_keeps_crlf(make_vault):
    vault = make_vault({"w.md": b"## A\r\none\r\n## B\r\ntwo\r\n"}, RB_REQUIRE_HASH_FOR_EDITS="false")
    vault.edit_section("w.md", "A", "uno\ndos")
    assert (vault.paths.root / "w.md").read_bytes() == b"## A\r\nuno\r\ndos\r\n\r\n## B\r\ntwo\r\n"


def test_section_edit_rejects_bad_mode(make_vault):
    vault = make_vault({"s.md": SECTIONS})
    with pytest.raises(VaultError):
        vault.edit_section("s.md", "Status", "x", mode="delete")



# ---- regression tests from review ----

def test_path_link_to_another_note_is_not_rewritten(make_vault):
    vault = make_vault({"a/Hub.md": "root\n", "x/a/Hub.md": "nested\n", "src.md": "[[a/Hub]] [[x/a/Hub]]\n"})
    vault.move_note("x/a/Hub.md", "x/a/Moved.md")
    assert text(vault, "src.md") == "[[a/Hub]] [[x/a/Moved]]\n"


def test_suffix_path_link_is_ambiguous_when_two_files_match(make_vault):
    vault = make_vault({"p/a/Hub.md": "1\n", "q/a/Hub.md": "2\n", "src.md": "[[a/Hub]]\n"})
    result = vault.move_note("p/a/Hub.md", "p/a/Other.md")
    assert text(vault, "src.md") == "[[a/Hub]]\n"
    assert result["links_not_updated"][0]["link"] == "a/Hub"


def test_attachments_with_same_name_are_ambiguous(make_vault):
    vault = make_vault({"p/img.png": b"1", "q/img.png": b"2", "s.md": "![[img.png]]\n"})
    result = vault.move_note("p/img.png", "p/photo.png")
    assert text(vault, "s.md") == "![[img.png]]\n"
    assert result["links_not_updated"][0]["link"] == "img.png"


def test_links_in_inline_code_and_long_fences_stay(make_vault):
    note = "`[[Hub]]` and [[Hub]]\n````md\n```\n[[Hub]]\n```\n````\n~~~\n[[Hub]]\n~~~\n"
    vault = make_vault({"Hub.md": "x\n", "n.md": note})
    vault.move_note("Hub.md", "Zen.md")
    assert text(vault, "n.md") == note.replace("and [[Hub]]", "and [[Zen]]")


def test_section_boundaries_respect_long_fences(make_vault):
    note = "## S\nold\n````md\n```\n## Inner\n```\n````\nkeep\n## T\nt\n"
    vault = make_vault({"f.md": note}, RB_REQUIRE_HASH_FOR_EDITS="false")
    vault.edit_section("f.md", "S", "x")
    assert text(vault, "f.md") == "## S\nx\n\n## T\nt\n"


def test_heading_details(make_vault):
    note = "---\n# not a heading: yaml comment\ntype: Note\n---\n## C#\none\n## C\ntwo\n   ## Indented\nthree\n"
    vault = make_vault({"h.md": note}, RB_REQUIRE_HASH_FOR_EDITS="false")
    vault.edit_section("h.md", "C#", "ONE")
    vault.edit_section("h.md", "C", "TWO")
    vault.edit_section("h.md", "Indented", "THREE")
    # replace keeps one blank line before the next heading
    expected = note.replace("one\n", "ONE\n\n").replace("two\n", "TWO\n\n").replace("three", "THREE")
    assert text(vault, "h.md") == expected
    with pytest.raises(VaultError) as exc:
        vault.edit_section("h.md", "not a heading: yaml comment", "x")
    assert exc.value.code == "no_match"


def test_fresh_links_from_other_apps_are_found(make_vault):
    vault = make_vault({"Hub.md": "x\n"})
    (vault.paths.root / "new.md").write_text("[[Hub]]\n")  # written outside the server, not scanned yet
    vault.move_note("Hub.md", "Zen.md")
    assert text(vault, "new.md") == "[[Zen]]\n"


def test_notes_that_cannot_be_updated_are_reported(make_vault):
    vault = make_vault({"Hub.md": "x\n", "big.md": "[[Hub]]" + "y" * 1500 + "\n"}, RB_MAX_FILE_BYTES="1520")
    result = vault.move_note("Hub.md", "Hubbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.md")
    assert result["links_not_updated"] == [{"path": "big.md", "reason": "too_large"}]



# ---- second review ----

def test_move_to_root_with_taken_name_reports_instead_of_breaking(make_vault):
    vault = make_vault({"A/Note.md": "x\n", "Projects/Topic.md": "y\n", "ref.md": "[[Note]] [[A/Note]]\n"})
    result = vault.move_note("A/Note.md", "Topic.md")
    assert text(vault, "ref.md") == "[[Note]] [[A/Note]]\n"
    reasons = {e.get("link"): e["reason"] for e in result["links_not_updated"]}
    assert reasons["Note"] == reasons["A/Note"] == "no unambiguous link to the new location is possible"


@pytest.mark.parametrize("bad", ["Hub #2.md", "A|B.md", "x]] y.md", "D[1].md", "Folder#x/Hub.md"])
def test_names_that_break_wikilinks_are_refused(make_vault, bad):
    vault = make_vault({"Hub.md": "x\n", "ref.md": "[[Hub|alias]]\n"})
    with pytest.raises(VaultError) as exc:
        vault.move_note("Hub.md", bad, create_folders=True)
    assert exc.value.code == "invalid_argument"
    assert text(vault, "ref.md") == "[[Hub|alias]]\n"
    vault.move_note("Hub.md", bad, create_folders=True, update_links=False)  # allowed when asked


def test_folder_move_cannot_expose_excluded_files(make_vault):
    vault = make_vault({"Work/Private/s.md": "secret\n", "Work/ok.md": "fine\n"}, RB_EXCLUDE="Work/Private/*")
    with pytest.raises(VaultError) as exc:
        vault.move_note("Work", "Job")
    assert exc.value.code == "hidden_path"
    assert (vault.paths.root / "Work/Private/s.md").exists()
    with pytest.raises(VaultError) as exc:
        vault.move_to_trash("Work")  # would take hidden files along
    assert exc.value.code == "hidden_path"


def test_restore_cannot_change_visibility(make_vault):
    vault = make_vault({"Work/a.md": "a\n"}, RB_EXCLUDE="Job/*")
    item = vault.move_to_trash("Work")
    with pytest.raises(VaultError) as exc:
        vault.restore_from_trash(item["trash_id"], target_path="Job")
    assert exc.value.code == "hidden_path"
    vault.restore_from_trash(item["trash_id"])
    assert vault.read_note("Work/a.md")["content"] == "a\n"


def test_existing_link_is_not_hijacked_by_a_new_exact_path(make_vault):
    vault = make_vault({"x/a/Hub.md": "real\n", "Other.md": "o\n", "ref.md": "[[a/Hub]]\n"})
    assert vault.resolve_link("a/Hub")["matches"] == ["x/a/Hub.md"]
    result = vault.move_note("Other.md", "a/Hub.md", create_folders=True)
    assert text(vault, "ref.md") == "[[x/a/Hub]]\n"
    assert {"path": "ref.md", "links": 1} in result["links_updated"]
    assert vault.resolve_link("a/Hub")["matches"] == ["a/Hub.md"]  # exact path first


def test_extension_change_keeps_links_working(make_vault):
    vault = make_vault({"Hub.md": "x\n", "ref.md": "[[Hub.md]] [[Hub]]\n"})
    vault.move_note("Hub.md", "Zen.txt")
    assert text(vault, "ref.md") == "[[Zen.txt]] [[Zen]]\n"


def test_trailing_backslash_link_is_rewritten_like_the_index_reads_it(make_vault):
    vault = make_vault({"Hub.md": "x\n", "ref.md": "[[Hub\\]]\n"})
    vault.move_note("Hub.md", "Zen.md")
    assert text(vault, "ref.md") == "[[Zen\\]]\n"


def test_large_vault_move_is_fast(make_vault):
    import time
    files = {f"n/{i:04d}.md": "[[Hub]] [[missing/path]] [[n/0001]]\n" for i in range(2000)}
    files["Hub.md"] = "hub\n"
    vault = make_vault(files)
    started = time.monotonic()
    result = vault.move_note("Hub.md", "Zen.md")
    assert time.monotonic() - started < 20
    assert len(result["links_updated"]) == 2000


# ---- third review ----

@pytest.mark.parametrize("pattern", ["Work/Private", "*/Private", "Work/Private/*"])
def test_folder_exclusion_covers_everything_below(make_vault, pattern):
    vault = make_vault({"Work/Private/sec.md": "secret\n", "Work/ok.md": "fine\n", "Inbox/x.md": "x\n"},
                       RB_EXCLUDE=pattern)
    for call in (lambda: vault.read_note("Work/Private/sec.md"),
                 lambda: vault.move_note("Work/Private/sec.md", "leak.md"),
                 lambda: vault.create_note("Work/Private/new.md", "x")):
        with pytest.raises(VaultError) as exc:
            call()
        assert exc.value.code == "hidden_path"
    item = vault.move_to_trash("Inbox/x.md")
    with pytest.raises(VaultError) as exc:
        vault.restore_from_trash(item["trash_id"], target_path="Work/Private/x.md")
    assert exc.value.code == "hidden_path"
    assert vault.search("secret")["hits"] == []
    listed = [e["path"] for e in vault.list_folder(".", recursive=True)["entries"]]
    assert "Work/Private/sec.md" not in listed  # with "Work/Private/*" only the folder name shows


def test_quotes_in_new_name_never_break_frontmatter(make_vault):
    vault = make_vault({"Hub.md": "x\n", "ref.md": '---\nup: "[[Hub]]"\n---\nbody [[Hub]]\n'})
    result = vault.move_note("Hub.md", 'Zen "x".md')
    assert text(vault, "ref.md") == '---\nup: "[[Hub]]"\n---\nbody [[Zen "x"]]\n'
    assert {"link": "Hub", "reason": FM_REASON} in result["links_not_updated"]
    assert vault.find_by_frontmatter({"up": "Hub"})["results"][0]["path"] == "ref.md"


@pytest.mark.parametrize("bad", ["Ze`n.md", "Ze\nn.md", "Zen\\.md", " Zen.md", "Zen .md", "Dir /Zen.md"])
def test_more_unsafe_names_are_refused_without_side_effects(make_vault, bad):
    vault = make_vault({"Hub.md": "x\n", "ref.md": "[[Hub]] and [[Other]]\n", "Other.md": "o\n"})
    with pytest.raises(VaultError) as exc:
        vault.move_note("Hub.md", "New/" + bad, create_folders=True)
    assert exc.value.code == "invalid_argument"
    assert not (vault.paths.root / "New").exists()  # no folders left behind
    assert text(vault, "ref.md") == "[[Hub]] and [[Other]]\n"


def test_skipped_notes_are_summarised_once(make_vault):
    files = {f"big{i}.md": "z" * 3000 for i in range(15)}
    files.update({"Hub.md": "x\n", "ref.md": "[[Hub]]\n"})
    vault = make_vault(files, RB_MAX_FILE_BYTES="2048")
    result = vault.move_note("Hub.md", "Zen.md")
    summary = [e for e in result["links_not_updated"] if "count" in e]
    assert len(summary) == 1 and summary[0]["count"] == "15"
    assert len(summary[0]["examples"].split(", ")) == 10


def test_note_wins_over_extensionless_file_with_same_name(make_vault):
    vault = make_vault({"Hub.md": "x\n", "files/Hub": b"raw", "ref.md": "[[Hub]]\n"})
    vault.move_note("Hub.md", "Zen.md")
    assert text(vault, "ref.md") == "[[Zen]]\n"



# ---- fourth review ----

def test_frontmatter_structure_must_survive_a_rewrite(make_vault):
    note = """---\ntags: ["[[Hub]]", "b"]\nr2: ['[[Hub]]', c]\n---\nbody [[Hub]]\n"""
    vault = make_vault({"Hub.md": "x\n", "ref.md": note})
    result = vault.move_note("Hub.md", 'x", "y.md')
    assert text(vault, "ref.md") == note.replace("body [[Hub]]", 'body [[x", "y]]')
    assert {"link": "Hub", "reason": FM_REASON} in result["links_not_updated"]
    plain = make_vault({"Hub2.md": "x\n", "ok.md": '---\ntags: ["[[Hub2]]", "b"]\n---\n'})
    plain.move_note("Hub2.md", "Neu.md")
    assert text(plain, "ok.md") == '---\ntags: ["[[Neu]]", "b"]\n---\n'


def test_quoted_fence_inside_code_does_not_end_the_block(make_vault):
    note = "```md\n> ```\n> [[Hub]] in code\n> ```\n```\nreal [[Hub]]\n"
    vault = make_vault({"Hub.md": "x\n", "n.md": note})
    vault.move_note("Hub.md", "Neu.md")
    assert text(vault, "n.md") == note.replace("real [[Hub]]", "real [[Neu]]")


def test_list_fence_like_line_does_not_hide_later_links(make_vault):
    note = "- ```\n- next\n\n[[Hub]] live para\n"
    vault = make_vault({"Hub.md": "x\n", "n.md": note})
    vault.move_note("Hub.md", "Neu.md")
    assert "[[Neu]] live para" in text(vault, "n.md")


def test_heading_inside_code_with_quoted_fence_is_ignored(make_vault):
    note = "## Real\nold\n```md\n> ```\n# Fake\n> ```\n```\n## Next\nn\n"
    vault = make_vault({"s.md": note})
    vault.edit_section("s.md", "## Real", "NEW", mode="append")
    assert text(vault, "s.md") == note.replace("```\n## Next", "```\nNEW\n## Next")


@pytest.mark.parametrize("pattern", ["/Work/Private", "Work/Private/", "/Work/Private/"])
def test_gitignore_style_patterns(make_vault, pattern):
    vault = make_vault({"Work/Private/s.md": "secret\n"}, RB_EXCLUDE=pattern)
    with pytest.raises(VaultError) as exc:
        vault.read_note("Work/Private/s.md")
    assert exc.value.code == "hidden_path"


def test_comment_markers_in_names_are_refused(make_vault):
    vault = make_vault({"Hub.md": "x\n"})
    with pytest.raises(VaultError):
        vault.move_note("Hub.md", "50%% off.md")


def test_errors_do_not_reveal_server_paths(make_vault):
    vault = make_vault({"a.md": "x\n", "b.md": "y\n"})
    with pytest.raises(VaultError) as exc:
        vault.move_note("a.md", "b.md")
    assert str(vault.paths.root) not in exc.value.to_json()



# ---- fifth review ----

def test_quoting_inside_a_link_target_is_caught(make_vault):
    note = "---\na: '[[Old]]'\n---\n[[Old]]\n"
    vault = make_vault({"Old.md": "x\n", "ref.md": note})
    result = vault.move_note("Old.md", "y''z.md")
    assert text(vault, "ref.md") == note.replace("\n[[Old]]\n", "\n[[y''z]]\n")
    assert {"link": "Old", "reason": FM_REASON} in result["links_not_updated"]


def test_keys_that_would_merge_are_caught(make_vault):
    note = '---\n"[[Old]]": 1\n"[[New]]": 2\n---\n'
    vault = make_vault({"Old.md": "x\n", "ref.md": note})
    vault.move_note("Old.md", "New.md")
    assert text(vault, "ref.md") == note


@pytest.mark.parametrize("hidden", ["P/secret.key", "P/sub/x.key", "P/.env"])
def test_trash_refuses_folders_with_hidden_files(make_vault, hidden):
    vault = make_vault({"P/a.md": "a\n", hidden: "secret\n"}, RB_EXCLUDE="*.key")
    with pytest.raises(VaultError) as exc:
        vault.move_to_trash("P")
    assert exc.value.code == "hidden_path"
    assert (vault.paths.root / hidden).exists()


def test_trash_allows_system_junk(make_vault):
    vault = make_vault({"P/a.md": "a\n", "P/.DS_Store": b"x", "P/@eaDir/thumb": b"y", "P/._a.md": b"z"})
    vault.move_to_trash("P")
    assert not (vault.paths.root / "P").exists()



# ---- sixth review ----

@pytest.mark.parametrize("frontmatter", [
    "base: &b x\nother: *b\nup: '[[Old]]'\n",  # aliases: not readable for the server
    "- '[[Old]]'\n",  # top-level list
])
def test_unreadable_frontmatter_is_never_rewritten(make_vault, frontmatter):
    note = f"---\n{frontmatter}---\nbody [[Old]]\n"
    vault = make_vault({"Old.md": "x\n", "ref.md": note})
    result = vault.move_note("Old.md", "Neu.md")
    assert text(vault, "ref.md") == note.replace("body [[Old]]", "body [[Neu]]")
    assert {"link": "Old", "reason": FM_REASON} in result["links_not_updated"]


def test_frontmatter_links_only_get_yaml_safe_names(make_vault):
    note = "---\nup: '[[Old]]'\n---\nbody [[Old]]\n"
    vault = make_vault({"Old.md": "x\n", "ref.md": note})
    vault.move_note("Old.md", "y'z.md")
    assert text(vault, "ref.md") == "---\nup: '[[Old]]'\n---\nbody [[y'z]]\n"
    vault2 = make_vault({"Alt.md": "x\n", "r2.md": "---\nup: \"[[Alt]]\"\n---\n"})
    vault2.move_note("Alt.md", "Größe (2026) - Plan_v2.md")
    assert text(vault2, "r2.md") == "---\nup: \"[[Größe (2026) - Plan_v2]]\"\n---\n"


def test_folder_named_like_junk_file_is_checked(make_vault):
    vault = make_vault({"F/desktop.ini/.env": "secret\n", "F/a.md": "a\n"})
    with pytest.raises(VaultError) as exc:
        vault.move_to_trash("F")
    assert exc.value.code == "hidden_path"
