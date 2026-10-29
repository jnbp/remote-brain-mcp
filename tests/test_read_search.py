from __future__ import annotations

import hashlib

import pytest

from remote_brain_mcp.errors import VaultError

NOTE = "---\ntype: Project\n---\n# Alpha\n\nline six\nStatus: café is open\nlast line\n"


def test_read_whole_note_returns_hash_and_complete(make_vault):
    vault = make_vault({"a.md": NOTE})
    result = vault.read_note("a.md")
    assert result["content"] == NOTE
    assert result["complete"] is True
    assert result["total_lines"] == 8
    assert result["sha256"] == hashlib.sha256(NOTE.encode()).hexdigest()


def test_read_range_reports_continuation(make_vault):
    vault = make_vault({"a.md": NOTE})
    result = vault.read_note("a.md", start_line=2, line_count=3)
    assert result["content"] == "type: Project\n---\n# Alpha\n"
    assert (result["start_line"], result["end_line"]) == (2, 4)
    assert result["complete"] is False
    assert result["next_start_line"] == 5


def test_read_beyond_end_is_invalid(make_vault):
    vault = make_vault({"a.md": "one\n"})
    with pytest.raises(VaultError) as exc:
        vault.read_note("a.md", start_line=5)
    assert exc.value.code == "invalid_argument"


def test_read_empty_note(make_vault):
    vault = make_vault({"e.md": ""})
    result = vault.read_note("e.md")
    assert result["content"] == "" and result["complete"] is True and result["total_lines"] == 0


def test_read_refuses_non_notes_large_and_binary(make_vault):
    vault = make_vault(
        {"img.png": b"\x89PNG", "bin.md": b"\xff\xfe\x00", "big.md": "x" * 5000},
        RB_MAX_FILE_BYTES="2048",
    )
    for path, code in [("img.png", "not_a_note"), ("bin.md", "not_a_note"), ("big.md", "too_large"),
                       ("missing.md", "not_found")]:
        with pytest.raises(VaultError) as exc:
            vault.read_note(path)
        assert exc.value.code == code, path


def test_large_read_is_cut_to_response_limit(make_vault):
    text = "".join(f"line {i} " + "y" * 80 + "\n" for i in range(2000))
    vault = make_vault({"long.md": text}, RB_MAX_RESPONSE_CHARS="5000")
    result = vault.read_note("long.md", line_count=2000)
    assert result["complete"] is False
    assert len(result["content"]) <= 5000
    assert result["next_start_line"] == result["end_line"] + 1


def test_search_words_ignores_case_and_accents_and_reports_line(make_vault):
    vault = make_vault({"a.md": NOTE, "b.md": "# Beta\nnothing here\n"})
    result = vault.search("CAFE")
    assert [h["path"] for h in result["hits"]] == ["a.md"]
    assert result["hits"][0]["line"] == 7
    assert result["complete"] is True


def test_search_phrase_prefix_and_substring(make_vault):
    vault = make_vault({"a.md": "The FrachtTicker dashboard\n", "b.md": "dash board\n"})
    assert [h["path"] for h in vault.search('"FrachtTicker dashboard"')["hits"]] == ["a.md"]
    assert sorted(h["path"] for h in vault.search("dash*")["hits"]) == ["a.md", "b.md"]
    assert vault.search("ticker")["hits"] == []
    assert [h["path"] for h in vault.search("ticker", mode="substring")["hits"]] == ["a.md"]


def test_search_finds_titles_frontmatter_and_paths(make_vault):
    vault = make_vault({"Projects/Gamma.md": "---\nstatus: Paused\n---\n# Gamma Title\nbody\n"})
    assert vault.search("Paused")["hits"][0]["path"] == "Projects/Gamma.md"
    assert vault.search("Title")["hits"][0]["path"] == "Projects/Gamma.md"
    assert vault.search("Projects")["hits"][0]["path"] == "Projects/Gamma.md"


@pytest.mark.parametrize("query", ['AND OR NOT', 'NEAR(a b)', 'a" OR "b', "col:value", "^start", "(x", "x*y"])
def test_search_never_passes_raw_syntax(make_vault, query):
    vault = make_vault({"a.md": "plain text with x and y\n"})
    try:
        result = vault.search(query)
        assert "hits" in result
    except VaultError as exc:
        assert exc.code == "invalid_argument"


def test_search_rejects_empty_and_short_substring(make_vault):
    vault = make_vault({"a.md": "x\n"})
    for query, mode in [("", "words"), ("   ", "words"), ("***", "words"), ("ab", "substring")]:
        with pytest.raises(VaultError) as exc:
            vault.search(query, mode=mode)
        assert exc.value.code == "invalid_argument"
    with pytest.raises(VaultError):
        vault.search("x", mode="fuzzy")


def test_search_path_prefix_and_limit(make_vault):
    files = {f"A/n{i}.md": "apple\n" for i in range(5)}
    files.update({"B/n.md": "apple\n", "AB/n.md": "apple\n"})
    vault = make_vault(files)
    result = vault.search("apple", path_prefix="A")
    assert {h["path"] for h in result["hits"]} == {f"A/n{i}.md" for i in range(5)}
    limited = vault.search("apple", limit=2)
    assert len(limited["hits"]) == 2 and limited["complete"] is False


def test_list_folder_types_and_hidden(make_vault):
    vault = make_vault({"a.md": "x", "img.png": b"\x89", "sub/b.md": "y", ".hidden/c.md": "z", "@eaDir/d.md": "w"})
    flat = vault.list_folder()
    assert [(e["path"], e["type"]) for e in flat["entries"]] == [("a.md", "note"), ("img.png", "file"),
                                                                 ("sub", "folder")]
    deep = [e["path"] for e in vault.list_folder(recursive=True)["entries"]]
    assert "sub/b.md" in deep and not any("hidden" in p or "eaDir" in p for p in deep)
    limited = vault.list_folder(recursive=True, limit=2)
    assert len(limited["entries"]) == 2 and limited["complete"] is False
