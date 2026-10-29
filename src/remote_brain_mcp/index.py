"""SQLite index: full-text search (FTS5), wikilinks and frontmatter."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import VaultError
from .markdown import (
    extract_links,
    frontmatter_values,
    norm,
    normalize_value,
    parse_link_target,
    split_frontmatter,
    split_lines,
    title_of,
)

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE notes(
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    stem_norm TEXT NOT NULL,
    path_noext_norm TEXT NOT NULL,
    title TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    body_line_offset INTEGER NOT NULL,
    fm_json TEXT NOT NULL
);
CREATE INDEX notes_stem ON notes(stem_norm);
CREATE VIRTUAL TABLE fts_words USING fts5(path, title, fm, body, tokenize="unicode61 remove_diacritics 2");
CREATE VIRTUAL TABLE fts_tri USING fts5(path, title, fm, body, tokenize="trigram");
CREATE TABLE links(note_id INTEGER NOT NULL, target_norm TEXT NOT NULL, line INTEGER NOT NULL, text TEXT NOT NULL);
CREATE INDEX links_target ON links(target_norm);
CREATE INDEX links_note ON links(note_id);
CREATE TABLE fm(note_id INTEGER NOT NULL, field TEXT NOT NULL, value_norm TEXT NOT NULL);
CREATE INDEX fm_field ON fm(field, value_norm);
CREATE INDEX fm_note ON fm(note_id);
"""

_QUERY_TOKEN = re.compile(r'"([^"]*)"|(\S+)')


def fold(text: str) -> str:
    """Case- and diacritic-insensitive form, used to find the matching line."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "/%"


class Index:
    def __init__(self, db_path: Path, text_extensions: tuple[str, ...]) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.text_extensions = text_extensions
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    # -- schema --------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        with self.lock:
            try:
                row = self.conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            except sqlite3.OperationalError:
                row = None
            if row and row[0] == SCHEMA_VERSION:
                return
            tables = [
                r[0]
                for r in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                    "AND name NOT LIKE '%_data' AND name NOT LIKE '%_idx' AND name NOT LIKE '%_content' "
                    "AND name NOT LIKE '%_docsize' AND name NOT LIKE '%_config'"
                )
            ]
            for table in tables:
                self.conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            self.conn.executescript(_SCHEMA)
            self.conn.execute("INSERT INTO meta(key, value) VALUES('schema', ?)", (SCHEMA_VERSION,))

    # -- helpers ---------------------------------------------------------------------

    def _strip_ext(self, text: str) -> str:
        lower = text.lower()
        for ext in self.text_extensions:
            if lower.endswith(ext):
                return text[: -len(ext)]
        return text

    def _note_id(self, rel: str) -> int | None:
        row = self.conn.execute("SELECT id FROM notes WHERE path=?", (rel,)).fetchone()
        return row[0] if row else None

    def _delete_id(self, note_id: int) -> None:
        self.conn.execute("DELETE FROM fts_words WHERE rowid=?", (note_id,))
        self.conn.execute("DELETE FROM fts_tri WHERE rowid=?", (note_id,))
        self.conn.execute("DELETE FROM links WHERE note_id=?", (note_id,))
        self.conn.execute("DELETE FROM fm WHERE note_id=?", (note_id,))
        self.conn.execute("DELETE FROM notes WHERE id=?", (note_id,))

    # -- writes -----------------------------------------------------------------------

    def upsert(self, rel: str, text: str, raw: bytes, mtime_ns: int, size: int) -> None:
        frontmatter, body, fm_lines = split_frontmatter(text)
        name = PurePosixPath(rel).name
        stem = self._strip_ext(name)
        title = title_of(body, stem)
        fm_text = " ".join(
            f"{key}: {' '.join(str(v) for v in (val if isinstance(val, list) else [val]))}"
            for key, val in frontmatter.items()
        )
        links = extract_links(text)
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                old = self._note_id(rel)
                if old is not None:
                    self._delete_id(old)
                cursor = self.conn.execute(
                    "INSERT INTO notes(path, stem_norm, path_noext_norm, title, size, mtime_ns, sha256, "
                    "body_line_offset, fm_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        rel,
                        norm(stem),
                        norm(self._strip_ext(rel)),
                        title,
                        size,
                        mtime_ns,
                        hashlib.sha256(raw).hexdigest(),
                        fm_lines,
                        json.dumps(frontmatter, ensure_ascii=False, default=str),
                    ),
                )
                note_id = cursor.lastrowid
                for table in ("fts_words", "fts_tri"):
                    self.conn.execute(
                        f"INSERT INTO {table}(rowid, path, title, fm, body) VALUES(?,?,?,?,?)",
                        (note_id, rel, title, fm_text, body),
                    )
                self.conn.executemany(
                    "INSERT INTO links(note_id, target_norm, line, text) VALUES(?,?,?,?)",
                    [(note_id, norm(self._strip_ext(link.target)), link.line, link.text) for link in links],
                )
                rows = []
                for key, value in frontmatter.items():
                    for item in frontmatter_values(value):
                        rows.append((note_id, norm(str(key)), item))
                self.conn.executemany("INSERT INTO fm(note_id, field, value_norm) VALUES(?,?,?)", rows)
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def remove(self, rel: str) -> None:
        with self.lock:
            note_id = self._note_id(rel)
            if note_id is not None:
                self.conn.execute("BEGIN")
                self._delete_id(note_id)
                self.conn.execute("COMMIT")

    def remove_prefix(self, rel_folder: str) -> None:
        with self.lock:
            ids = [
                r[0]
                for r in self.conn.execute(
                    "SELECT id FROM notes WHERE path LIKE ? ESCAPE '\\'", (_like_prefix(rel_folder),)
                )
            ]
            if ids:
                self.conn.execute("BEGIN")
                for note_id in ids:
                    self._delete_id(note_id)
                self.conn.execute("COMMIT")

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))

    # -- reads ------------------------------------------------------------------------

    def state(self) -> dict[str, tuple[int, int]]:
        with self.lock:
            return {
                path: (mtime_ns, size)
                for path, mtime_ns, size in self.conn.execute("SELECT path, mtime_ns, size FROM notes")
            }

    def get_meta(self, key: str) -> str | None:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def note_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]

    def other_paths_with_stem(self, rel: str) -> list[str]:
        stem = norm(self._strip_ext(PurePosixPath(rel).name))
        with self.lock:
            return [
                r[0]
                for r in self.conn.execute("SELECT path FROM notes WHERE stem_norm=? AND path<>?", (stem, rel))
            ]

    def stem_count(self, stem_norm: str) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM notes WHERE stem_norm=?", (stem_norm,)).fetchone()[0]

    def link_sources(self, bare_keys: set[str], path_keys: set[str]) -> set[str]:
        """Paths of notes containing a link to any of the given targets (normalised forms)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT n.path, l.target_norm FROM links l JOIN notes n ON n.id = l.note_id"
            ).fetchall()
        sources = set()
        for path, target in rows:
            if target in bare_keys or (
                "/" in target and any(key == target or key.endswith("/" + target) for key in path_keys)
            ):
                sources.add(path)
        return sources

    @staticmethod
    def build_match(query: str, mode: str) -> tuple[str, list[str]]:
        """Turn user text into a safe FTS5 expression. Returns (expression, plain terms)."""
        if not isinstance(query, str) or not query.strip():
            raise VaultError("invalid_argument", "query must be a non-empty string")
        parts: list[str] = []
        plain: list[str] = []
        for match in _QUERY_TOKEN.finditer(query):
            if match.group(1) is not None:
                term, prefix = match.group(1).strip(), False
            else:
                term = match.group(2)
                prefix = mode == "words" and term.endswith("*") and len(term.rstrip("*")) > 0
                term = term.rstrip("*") if prefix else term
                term = term.replace('"', "")
            if not term:
                continue
            if mode == "words" and not any(ch.isalnum() for ch in term):
                continue
            if mode == "substring" and len(term) < 3:
                raise VaultError("invalid_argument", "substring search needs terms of at least 3 characters")
            parts.append('"' + term.replace('"', '""') + '"' + ("*" if prefix else ""))
            plain.append(term)
        if not parts:
            raise VaultError("invalid_argument", "the query contains no searchable words")
        return " ".join(parts), plain

    def search(self, query: str, mode: str, path_prefix: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        if mode not in ("words", "substring"):
            raise VaultError("invalid_argument", "mode must be 'words' or 'substring'")
        expression, terms = self.build_match(query, mode)
        table = "fts_words" if mode == "words" else "fts_tri"
        sql = (
            f"SELECT n.path, n.title, snippet({table}, -1, '[', ']', '…', 12), "
            f"bm25({table}, 2.0, 4.0, 1.5, 1.0), n.id, n.body_line_offset "
            f"FROM {table} JOIN notes n ON n.id = {table}.rowid WHERE {table} MATCH ?"
        )
        params: list[Any] = [expression]
        if path_prefix:
            sql += " AND (n.path = ? OR n.path LIKE ? ESCAPE '\\')"
            params += [path_prefix, _like_prefix(path_prefix)]
        sql += " ORDER BY 4 LIMIT ?"
        params.append(limit + 1)
        with self.lock:
            try:
                rows = self.conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                raise VaultError("invalid_argument", f"query could not be used: {exc}") from exc
            hits = []
            folded_terms = [fold(t) for t in terms]
            for path, title, snippet, score, note_id, offset in rows[:limit]:
                body = self.conn.execute("SELECT body FROM fts_words WHERE rowid=?", (note_id,)).fetchone()
                line = None
                if body:
                    for number, text in enumerate(split_lines(body[0]), start=1):
                        folded = fold(text)
                        if any(term in folded for term in folded_terms):
                            line = number + offset
                            break
                hits.append(
                    {"path": path, "title": title, "snippet": snippet, "line": line, "score": round(-score, 4)}
                )
        return hits, len(rows) <= limit

    def resolve(self, link: str) -> list[str]:
        if not isinstance(link, str) or not link.strip():
            raise VaultError("invalid_argument", "link must be a non-empty string")
        text = link.strip()
        text = text.removeprefix("!")
        if text.startswith("[[") and text.endswith("]]"):
            text = text[2:-2]
        target = norm(self._strip_ext(parse_link_target(text)))
        if not target:
            raise VaultError("invalid_argument", "link has no target")
        with self.lock:
            if "/" in target:
                rows = self.conn.execute("SELECT path FROM notes WHERE path_noext_norm = ?", (target,)).fetchall()
                if not rows:  # no exact path: fall back to a path suffix
                    rows = self.conn.execute(
                        "SELECT path FROM notes WHERE path_noext_norm LIKE ? ESCAPE '\\'",
                        ("%/" + target.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"),),
                    ).fetchall()
            else:
                rows = self.conn.execute("SELECT path FROM notes WHERE stem_norm = ?", (target,)).fetchall()
        return sorted(r[0] for r in rows)

    def backlinks(self, rel: str) -> tuple[list[dict[str, Any]], bool]:
        with self.lock:
            row = self.conn.execute(
                "SELECT stem_norm, path_noext_norm FROM notes WHERE path=?", (rel,)
            ).fetchone()
            if row is None:
                raise VaultError("not_found", "note not found in the index", path=rel)
            stem, noext = row
            ambiguous = (
                self.conn.execute("SELECT COUNT(*) FROM notes WHERE stem_norm=?", (stem,)).fetchone()[0] > 1
            )
            candidates = self.conn.execute(
                "SELECT n.path, l.line, l.text, l.target_norm FROM links l JOIN notes n ON n.id = l.note_id "
                "WHERE l.target_norm = ? OR l.target_norm LIKE '%/%' ORDER BY n.path, l.line",
                (stem,),
            ).fetchall()
        result = []
        for path, line, text, target in candidates:
            if target == stem or ("/" in target and (noext == target or noext.endswith("/" + target))):
                result.append({"path": path, "line": line, "text": text})
        return result, ambiguous

    def find_by_frontmatter(
        self, filters: dict[str, Any], path_prefix: str, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        if not isinstance(filters, dict) or not filters:
            raise VaultError("invalid_argument", "filters must be a non-empty object of field: value")
        sql = "SELECT n.path, n.title, n.fm_json FROM notes n WHERE 1=1"
        params: list[Any] = []
        for field, value in filters.items():
            values = frontmatter_values(value) if isinstance(value, list) else [normalize_value(str(value))]
            if isinstance(value, bool):
                values = [norm(str(value))]
            if not values:
                raise VaultError("invalid_argument", f"filter value for {field!r} is empty")
            for item in values:
                sql += " AND EXISTS(SELECT 1 FROM fm WHERE fm.note_id = n.id AND fm.field = ? AND fm.value_norm = ?)"
                params += [norm(str(field)), item]
        if path_prefix:
            sql += " AND (n.path = ? OR n.path LIKE ? ESCAPE '\\')"
            params += [path_prefix, _like_prefix(path_prefix)]
        sql += " ORDER BY n.path LIMIT ?"
        params.append(limit + 1)
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        results = []
        wanted = {norm(str(f)) for f in filters}
        for path, title, fm_json in rows[:limit]:
            data = json.loads(fm_json)
            fields = {k: v for k, v in data.items() if norm(str(k)) in wanted}
            results.append({"path": path, "title": title, "fields": fields})
        return results, len(rows) <= limit
