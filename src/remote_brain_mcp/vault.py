"""Vault operations behind every MCP tool."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config
from .errors import VaultError
from .fsops import (
    TEMP_PREFIX,
    atomic_write,
    ensure_free_space,
    make_folders,
    remove_temp_files,
    rename_noreplace,
)
from .index import Index
from .markdown import (
    count_overlapping,
    extract_links,
    frontmatter_link_targets,
    frontmatter_shape,
    headings,
    link_safe_path,
    norm,
    rewrite_links,
    split_frontmatter,
    split_lines,
    yaml_safe_link_text,
)
from .paths import VaultPaths

log = logging.getLogger("remote_brain_mcp")

# Metadata that sync tools and the NAS create; it may go to the trash with its folder.
SYSTEM_JUNK_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", ".localized"})
SYSTEM_JUNK_DIRS = frozenset({"@eaDir"})

TRASH_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
MAX_READ_LINES = 2000


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Vault:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.paths = VaultPaths(config)
        self.index = Index(config.data_path / "index.sqlite", config.text_extensions)
        self.lock = threading.RLock()  # serialises writes, trash, purge and index updates
        self.skipped: dict[str, str] = {}
        self.last_scan_finished: float | None = None
        self.last_purge: float | None = None

    # ================================================================== helpers

    def _limit(self, value: Any, name: str, default: int, maximum: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise VaultError("invalid_argument", f"{name} must be a positive integer")
        return min(value, maximum)

    def _fit(self, result: dict[str, Any], key: str) -> dict[str, Any]:
        """Drop list items from the end until the JSON fits the response limit."""
        items = result[key]
        while items and len(json.dumps(result, ensure_ascii=False)) > self.config.max_response_chars:
            items.pop()
            result["complete"] = False
        return result

    def _note_path(self, rel: str) -> tuple[str, Path]:
        clean, absolute = self.paths.resolve(rel)
        if not self.paths.is_note_name(absolute.name):
            raise VaultError(
                "not_a_note",
                f"only files ending in {', '.join(self.config.text_extensions)} can be read or edited",
                path=clean,
            )
        return clean, absolute

    def _read_existing_note(self, rel: str) -> tuple[str, Path, bytes, str, os.stat_result]:
        clean, absolute = self._note_path(rel)
        try:
            st = os.lstat(absolute)
        except FileNotFoundError as exc:
            raise VaultError("not_found", "note not found", path=clean) from exc
        if not stat.S_ISREG(st.st_mode):
            raise VaultError("not_a_note", "this path is not a regular file", path=clean)
        if st.st_size > self.config.max_file_bytes:
            raise VaultError("too_large", "note is larger than RB_MAX_FILE_BYTES", path=clean, size=st.st_size)
        raw = absolute.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VaultError("not_a_note", "note is not valid UTF-8 text", path=clean) from exc
        return clean, absolute, raw, text, st

    def _check_size(self, data: bytes, clean: str) -> None:
        if len(data) > self.config.max_file_bytes:
            raise VaultError(
                "too_large", "the result would exceed RB_MAX_FILE_BYTES", path=clean, size=len(data)
            )

    def _check_hash(self, expected: str | None, raw: bytes, clean: str, required: bool) -> None:
        if expected is None:
            if required:
                raise VaultError(
                    "invalid_argument",
                    "expected_sha256 is required: read the note first and pass its sha256",
                    path=clean,
                )
            return
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise VaultError("invalid_argument", "expected_sha256 must be 64 hex characters")
        current = _sha(raw)
        if expected.lower() != current:
            raise VaultError(
                "conflict", "the note changed since it was read", path=clean, current_sha256=current
            )

    def _index_file(self, clean: str, absolute: Path) -> None:
        """Re-index one note after a write made by the server (caller holds self.lock)."""
        try:
            st = os.lstat(absolute)
        except FileNotFoundError:
            self.index.remove(clean)
            return
        if not stat.S_ISREG(st.st_mode) or not self.paths.is_note_name(absolute.name):
            self.index.remove(clean)
            return
        if st.st_size > self.config.max_file_bytes:
            self.index.remove(clean)
            self.skipped[clean] = "too large"
            return
        raw = absolute.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.index.remove(clean)
            self.skipped[clean] = "not valid UTF-8"
            return
        try:
            self.index.upsert(clean, text, raw, st.st_mtime_ns, st.st_size)
        except Exception as exc:  # noqa: BLE001 - one bad note must never break the server
            log.warning("could not index %s: %s", clean, type(exc).__name__)
            self.index.remove(clean)
            self.skipped[clean] = f"could not be indexed ({type(exc).__name__})"
            return
        self.skipped.pop(clean, None)

    def _index_tree(self, clean: str, absolute: Path) -> None:
        """Re-index a moved or restored file or folder (caller holds self.lock)."""
        st = os.lstat(absolute)
        if stat.S_ISDIR(st.st_mode):
            for dirpath, dirnames, filenames in os.walk(absolute, followlinks=False):
                dirnames[:] = [d for d in dirnames if not self.paths.is_hidden_name(d)]
                for name in filenames:
                    full = Path(dirpath) / name
                    rel = self.paths.to_rel(full)
                    if not self.paths.is_hidden(rel) and self.paths.is_note_name(name):
                        self._index_file(rel, full)
        else:
            self._index_file(clean, absolute)

    def _is_indexable(self, rel: str) -> bool:
        full = self.paths.locate(rel)
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            return False
        return (
            stat.S_ISREG(st.st_mode)
            and self.paths.is_note_name(full.name)
            and not self.paths.is_hidden(rel)
            and st.st_size <= self.config.max_file_bytes
        )

    def _warnings_for(self, clean: str) -> list[str]:
        if not self.paths.is_note_name(clean):
            return []
        others = self.index.other_paths_with_stem(clean)
        if others:
            return [f"duplicate_name: another note has the same name ({others[0]}); wikilinks become ambiguous"]
        return []

    def _write_result(self, clean: str, data: bytes, warnings: list[str] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"path": clean, "sha256": _sha(data), "size": len(data)}
        if warnings:
            result["warnings"] = warnings
        return result

    # ================================================================== reading

    def get_status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "version": __version__,
            "note_count": self.index.note_count(),
            "last_scan": _iso(self.last_scan_finished) if self.last_scan_finished else None,
            "index_age_seconds": round(now - self.last_scan_finished) if self.last_scan_finished else None,
            "scan_interval_seconds": self.config.scan_interval_seconds,
            "skipped": [{"path": p, "reason": r} for p, r in sorted(self.skipped.items())][:200],
            "trash_items": len(self._trash_entries()),
            "trash_retention_days": self.config.trash_retention_days,
        }

    def list_folder(self, path: str = ".", recursive: bool = False, limit: int | None = None) -> dict[str, Any]:
        limit = self._limit(limit, "limit", 500, 5000)
        clean, absolute = self.paths.resolve(path, allow_root=True)
        try:
            st = os.lstat(absolute)
        except FileNotFoundError as exc:
            raise VaultError("not_found", "folder not found", path=clean) from exc
        if not stat.S_ISDIR(st.st_mode):
            raise VaultError("invalid_argument", "this path is not a folder", path=clean)

        entries: list[dict[str, Any]] = []
        complete = True
        stack = [absolute]
        while stack and complete:
            folder = stack.pop()
            try:
                children = sorted(os.scandir(folder), key=lambda e: e.name.casefold())
            except OSError:
                continue
            subfolders = []
            for entry in children:
                if self.paths.is_hidden_name(entry.name) or entry.name.startswith(TEMP_PREFIX):
                    continue
                rel = self.paths.to_rel(Path(entry.path))
                if self.paths.is_hidden(rel):
                    continue
                est = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(est.st_mode):
                    continue
                if stat.S_ISDIR(est.st_mode):
                    kind = "folder"
                    subfolders.append(Path(entry.path))
                elif stat.S_ISREG(est.st_mode):
                    kind = "note" if self.paths.is_note_name(entry.name) else "file"
                else:
                    continue
                if len(entries) >= limit:
                    complete = False
                    break
                item = {"path": rel, "type": kind, "modified": _iso(est.st_mtime)}
                if kind != "folder":
                    item["size"] = est.st_size
                entries.append(item)
            if recursive:
                stack.extend(reversed(subfolders))
        result = {"path": clean or ".", "entries": entries, "complete": complete}
        return self._fit(result, "entries")

    def read_note(self, path: str, start_line: int | None = None, line_count: int | None = None) -> dict[str, Any]:
        start = self._limit(start_line, "start_line", 1, 10**9)
        count = self._limit(line_count, "line_count", 400, MAX_READ_LINES)
        clean, _absolute, raw, text, st = self._read_existing_note(path)
        lines = split_lines(text)
        total = len(lines)
        if total and start > total:
            raise VaultError("invalid_argument", f"start_line is beyond the end of the note ({total} lines)")
        end = min(total, start - 1 + count)
        selected = lines[start - 1 : end]
        budget = self.config.max_response_chars - 1000

        def encoded(text: str) -> int:
            return len(json.dumps(text, ensure_ascii=False))

        content = "".join(selected)
        while encoded(content) > budget and len(selected) > 1:
            selected = selected[: max(1, len(selected) // 2)]
            content = "".join(selected)
        line_truncated = False
        if encoded(content) > budget:  # a single huge line: cut it to fit
            low, high = 0, len(content)
            while low < high:
                middle = (low + high + 1) // 2
                if encoded(content[:middle]) <= budget:
                    low = middle
                else:
                    high = middle - 1
            content = content[:low]
            line_truncated = True
        end = start - 1 + len(selected) if total else 0
        complete = start == 1 and end == total and len(content) == len("".join(lines))
        result: dict[str, Any] = {
            "path": clean,
            "content": content,
            "start_line": start if total else 0,
            "end_line": end,
            "total_lines": total,
            "size": st.st_size,
            "modified": _iso(st.st_mtime),
            "sha256": _sha(raw),
            "complete": complete,
        }
        if line_truncated:
            result["line_truncated"] = True
        if not complete and end < total:
            result["next_start_line"] = end + 1
        return result

    def search(
        self, query: str, path_prefix: str | None = None, limit: int | None = None, mode: str = "words"
    ) -> dict[str, Any]:
        limit = self._limit(limit, "limit", 20, 100)
        prefix = self.paths.normalize(path_prefix, allow_root=True) if path_prefix else ""
        hits, complete = self.index.search(query, mode, prefix, limit)
        age = round(time.time() - self.last_scan_finished) if self.last_scan_finished else None
        return self._fit({"query": query, "mode": mode, "hits": hits, "index_age_seconds": age,
                          "complete": complete}, "hits")

    def resolve_link(self, link: str) -> dict[str, Any]:
        matches = self.index.resolve(link)
        return {"link": link, "matches": matches, "ambiguous": len(matches) > 1}

    def get_backlinks(self, path: str) -> dict[str, Any]:
        clean, _ = self._note_path(path)
        links, ambiguous = self.index.backlinks(clean)
        result = {"path": clean, "backlinks": links, "ambiguous_name": ambiguous, "complete": True}
        return self._fit(result, "backlinks")

    def find_by_frontmatter(
        self, filters: dict[str, Any], path_prefix: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        limit = self._limit(limit, "limit", 100, 1000)
        prefix = self.paths.normalize(path_prefix, allow_root=True) if path_prefix else ""
        results, complete = self.index.find_by_frontmatter(filters, prefix, limit)
        return self._fit({"results": results, "complete": complete}, "results")

    # ================================================================== writing

    def create_note(self, path: str, content: str, create_folders: bool = True) -> dict[str, Any]:
        if not isinstance(content, str):
            raise VaultError("invalid_argument", "content must be a string")
        clean, absolute = self._note_path(path)
        data = content.encode("utf-8")
        self._check_size(data, clean)
        with self.lock:
            parent_rel = clean.rsplit("/", 1)[0] if "/" in clean else ""
            if not absolute.parent.is_dir():
                if not create_folders:
                    raise VaultError("not_found", "the target folder does not exist", path=parent_rel)
                self.paths.make_folders(parent_rel)
            self.paths.assert_no_symlinks(clean)
            ensure_free_space(self.paths.root, self.config.min_free_bytes, len(data))
            atomic_write(absolute, data, noreplace=True)
            self._index_file(clean, absolute)
            return self._write_result(clean, data, self._warnings_for(clean))

    def edit_note(
        self, path: str, old_text: str, new_text: str, expected_sha256: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(old_text, str) or old_text == "":
            raise VaultError("invalid_argument", "old_text must be a non-empty string")
        if not isinstance(new_text, str):
            raise VaultError("invalid_argument", "new_text must be a string")
        with self.lock:
            clean, absolute, raw, text, _ = self._read_existing_note(path)
            self._check_hash(expected_sha256, raw, clean, self.config.require_hash_for_edits)
            old, new = old_text, new_text
            count = count_overlapping(text, old)
            if count == 0 and "\r\n" in text and "\n" in old:
                old = old.replace("\r\n", "\n").replace("\n", "\r\n")
                new = new.replace("\r\n", "\n").replace("\n", "\r\n")
                count = count_overlapping(text, old)
            if count == 0:
                raise VaultError("no_match", "old_text was not found in the note", path=clean)
            if count > 1:
                raise VaultError(
                    "ambiguous_match",
                    f"old_text occurs {count} times; include more surrounding text",
                    path=clean,
                    occurrences=count,
                )
            data = text.replace(old, new, 1).encode("utf-8")
            self._check_size(data, clean)
            ensure_free_space(self.paths.root, self.config.min_free_bytes, len(data))
            atomic_write(absolute, data, noreplace=False, mode_from=absolute)
            self._index_file(clean, absolute)
            return self._write_result(clean, data)

    def append_to_note(self, path: str, text: str, expected_sha256: str | None = None) -> dict[str, Any]:
        if not isinstance(text, str) or text == "":
            raise VaultError("invalid_argument", "text must be a non-empty string")
        with self.lock:
            clean, absolute, raw, _, _ = self._read_existing_note(path)
            self._check_hash(expected_sha256, raw, clean, required=False)
            separator = b""
            if raw and not raw.endswith(b"\n"):
                separator = b"\r\n" if b"\r\n" in raw else b"\n"
            data = raw + separator + text.encode("utf-8")
            self._check_size(data, clean)
            ensure_free_space(self.paths.root, self.config.min_free_bytes, len(data))
            atomic_write(absolute, data, noreplace=False, mode_from=absolute)
            self._index_file(clean, absolute)
            return self._write_result(clean, data)

    def replace_note(self, path: str, content: str, expected_sha256: str) -> dict[str, Any]:
        if not isinstance(content, str):
            raise VaultError("invalid_argument", "content must be a string")
        with self.lock:
            clean, absolute, raw, _, _ = self._read_existing_note(path)
            self._check_hash(expected_sha256, raw, clean, required=True)
            data = content.encode("utf-8")
            self._check_size(data, clean)
            ensure_free_space(self.paths.root, self.config.min_free_bytes, len(data))
            atomic_write(absolute, data, noreplace=False, mode_from=absolute)
            self._index_file(clean, absolute)
            return self._write_result(clean, data)

    def _strip_note_ext(self, rel: str) -> str:
        lower = rel.lower()
        for ext in self.config.text_extensions:
            if lower.endswith(ext):
                return rel[: -len(ext)]
        return rel

    def _moved_items(self, src_rel: str, src: Path, dst_rel: str, is_dir: bool) -> list[tuple[str, str]]:
        """(old rel, new rel) for every visible file that a move will relocate."""
        if not is_dir:
            return [(src_rel, dst_rel)]
        items = []
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            dirnames[:] = [d for d in dirnames if not self.paths.is_hidden_name(d)]
            for name in filenames:
                if self.paths.is_hidden_name(name) or name.startswith(TEMP_PREFIX):
                    continue
                old = self.paths.to_rel(Path(dirpath) / name)
                items.append((old, dst_rel + old[len(src_rel):]))
        return items

    def _all_visible_files(self) -> list[str]:
        files = []
        for dirpath, dirnames, filenames in os.walk(self.paths.root, followlinks=False):
            base = Path(dirpath)
            dirnames[:] = [
                d for d in dirnames
                if not self.paths.is_hidden_name(d) and not os.path.islink(base / d)
                and not self.paths.is_hidden(self.paths.to_rel(base / d))
            ]
            for name in filenames:
                full = base / name
                if self.paths.is_hidden_name(name) or name.startswith(TEMP_PREFIX) or os.path.islink(full):
                    continue
                rel = self.paths.to_rel(full)
                if not self.paths.is_hidden(rel):
                    files.append(rel)
        return files

    def _link_keys(self, rel: str) -> tuple[str, str]:
        """(bare key, path key) under which wikilinks address this file."""
        name = rel.rsplit("/", 1)[-1]
        if self.paths.is_note_name(name):
            return norm(self._strip_note_ext(name)), norm(self._strip_note_ext(rel))
        return norm(name), norm(rel)

    def _resolver(self, files: list[str]):
        """Return a cached function mapping a link target to (status, [files]).

        Rules (same as resolve_link): a path link matches an exact path first, then a unique
        path suffix; a bare link matches a unique file name.
        """
        by_bare: dict[str, list[str]] = {}
        by_path: dict[str, list[str]] = {}
        by_suffix: dict[str, list[str]] = {}
        for rel in files:
            bare, path_key = self._link_keys(rel)
            by_bare.setdefault(bare, []).append(rel)
            by_path.setdefault(path_key, []).append(rel)
            parts = path_key.split("/")
            for cut in range(1, len(parts)):
                by_suffix.setdefault("/".join(parts[cut:]), []).append(rel)
        cache: dict[str, tuple[str, list[str]]] = {}

        def resolve(target: str) -> tuple[str, list[str]]:
            key = norm(self._strip_note_ext(target))
            if key in cache:
                return cache[key]
            if "/" in key:
                matches = by_path.get(key) or by_suffix.get(key, [])
            else:
                matches = by_bare.get(key, [])
            if len(matches) > 1:
                notes = [m for m in matches if self.paths.is_note_name(m)]
                if len(notes) == 1 and len(notes) < len(matches):
                    matches = notes  # [[Hub]] means Hub.md, not a file named "Hub"
            status = "ok" if len(matches) == 1 else "ambiguous" if matches else "none"
            cache[key] = (status, matches)
            return cache[key]

        return resolve

    def _link_forms(self, target: str, desired: str) -> list[str]:
        """Candidate link texts for `desired`, in the style of the original `target`."""
        name = desired.rsplit("/", 1)[-1]
        is_note = self.paths.is_note_name(name)
        had_ext = norm(self._strip_note_ext(target)) != norm(target)
        if is_note and not had_ext:
            bare, full = self._strip_note_ext(name), self._strip_note_ext(desired)
        else:
            bare, full = name, desired
        forms = [full, bare] if "/" in target else [bare, full]
        return [form for form in forms if link_safe_path(form)]

    def _rewrite_links_after_move(
        self, moved: list[tuple[str, str]], before: list[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Keep every link pointing at the same file it pointed at before the move.

        Each link in an affected note is resolved against the files before and after the
        move. If the result changed, it is replaced by the shortest form that resolves to
        the original target again; if no such form exists, it is left alone and reported.
        Caller holds self.lock.
        """
        moved_map = dict(moved)
        resolve_before = self._resolver(before)
        resolve_after = self._resolver([moved_map.get(rel, rel) for rel in before])
        keys = [self._link_keys(rel) for pair in moved for rel in pair]
        sources = self.index.link_sources({k[0] for k in keys}, {k[1] for k in keys})
        not_updated: list[dict[str, str]] = []
        problems: dict[str, str] = {}
        written: set[str] = set()
        not_checked_fm: set[str] = set()

        def new_target_for(target: str) -> str | None:
            status, matches = resolve_before(target)
            if status == "ambiguous":
                if any(m in moved_map for m in matches):
                    problems[target] = "ambiguous before the move: several files have this name"
                return None
            if status != "ok":
                return None  # a link that pointed nowhere is left as it is
            desired = moved_map.get(matches[0], matches[0])
            if resolve_after(target) == ("ok", [desired]):
                return None
            for form in self._link_forms(target, desired):
                if resolve_after(form) == ("ok", [desired]):
                    written.add(form)
                    return form
            problems[target] = "no unambiguous link to the new location is possible"
            return None

        updated = []
        for source_old in sorted(sources):
            source = moved_map.get(source_old, source_old)
            try:
                clean, absolute, _raw, text, _ = self._read_existing_note(source)
                fm_data, _body, fm_lines = split_frontmatter(text)
                lines = split_lines(text)
                fm_text, body_text = "".join(lines[:fm_lines]), "".join(lines[fm_lines:])
                fm_readable = bool(fm_data) or fm_lines <= 2 or not fm_text.strip("-\r\n ")

                def frontmatter_target_for(target: str, source: str = source,
                                           fm_readable: bool = fm_readable) -> str | None:
                    form = new_target_for(target)
                    if form is None:
                        return None
                    if not fm_readable or not yaml_safe_link_text(form):
                        problems[target] = (
                            "link in frontmatter not updated: the new name needs YAML quoting "
                            "or the frontmatter cannot be read"
                        )
                        not_checked_fm.add(source)
                        return None
                    return form

                new_fm, fm_count = rewrite_links(fm_text, frontmatter_target_for)
                new_body, body_count = rewrite_links(body_text, new_target_for)
                new_text, count = new_fm + new_body, fm_count + body_count
                if not count:
                    continue
                # Self-check: a rewrite must not add, drop or swallow links, nor break frontmatter.
                if len(extract_links(new_text)) != len(extract_links(text)):
                    not_updated.append({"path": source, "reason": "rewrite would change other links"})
                    continue
                old_fm, new_fm = split_frontmatter(text)[0], split_frontmatter(new_text)[0]
                new_targets = frontmatter_link_targets(new_fm)
                introduced = set(new_targets) - set(frontmatter_link_targets(old_fm))
                if frontmatter_shape(old_fm) != frontmatter_shape(new_fm) or not introduced <= written:
                    not_updated.append({"path": source, "reason": "rewrite would break the frontmatter"})
                    continue
                data = new_text.encode("utf-8")
                self._check_size(data, clean)
                ensure_free_space(self.paths.root, self.config.min_free_bytes, len(data))
                atomic_write(absolute, data, noreplace=False, mode_from=absolute)
            except VaultError as exc:
                not_updated.append({"path": source, "reason": exc.code})
                continue
            except OSError as exc:
                not_updated.append({"path": source, "reason": f"write failed ({exc.strerror})"})
                continue
            self._index_file(clean, absolute)
            updated.append({"path": clean, "links": count})
        for target, reason in sorted(problems.items()):
            not_updated.append({"link": target, "reason": reason})
        unchecked = sorted(
            path for path, reason in self.skipped.items()
            if reason in ("too large", "not valid UTF-8") or reason.startswith("could not be indexed")
        )
        if unchecked:
            not_updated.append({
                "reason": "notes the index skips (too large or not UTF-8) were not checked for links",
                "count": str(len(unchecked)),
                "examples": ", ".join(unchecked[:10]),
            })
        return updated, not_updated

    def _assert_visibility_kept(self, source: Path, old_rel: str, new_rel: str) -> None:
        """Refuse a folder move or restore that would expose excluded files (or hide visible
        ones), because exclude patterns can match full paths."""
        if not stat.S_ISDIR(os.lstat(source).st_mode):
            return
        for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
            for name in dirnames + filenames:
                rel_old = old_rel + "/" + self.paths.to_rel_from(source, Path(dirpath) / name)
                rel_new = new_rel + rel_old[len(old_rel):]
                if self.paths.is_hidden(rel_old) != self.paths.is_hidden(rel_new):
                    raise VaultError(
                        "hidden_path",
                        "this move would change which files are hidden by RB_EXCLUDE",
                        path=rel_old,
                    )

    def edit_section(
        self,
        path: str,
        heading: str,
        content: str,
        mode: str = "replace",
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Replace, append to or prepend to the text under one heading."""
        if mode not in ("replace", "append", "prepend"):
            raise VaultError("invalid_argument", "mode must be 'replace', 'append' or 'prepend'")
        if not isinstance(heading, str) or not heading.strip().lstrip("#").strip():
            raise VaultError("invalid_argument", "heading must name a heading, e.g. 'Status' or '## Status'")
        if not isinstance(content, str):
            raise VaultError("invalid_argument", "content must be a string")
        wanted = heading.strip()
        level_wanted = len(wanted) - len(wanted.lstrip("#")) or None
        title_wanted = norm(wanted.lstrip("#").strip())
        with self.lock:
            clean, absolute, raw, text, _ = self._read_existing_note(path)
            self._check_hash(expected_sha256, raw, clean,
                             required=mode == "replace" and self.config.require_hash_for_edits)
            lines = split_lines(text)
            found = headings(lines, start=split_frontmatter(text)[2])
            matches = [h for h in found if norm(h[2]) == title_wanted and (level_wanted in (None, h[1]))]
            if not matches:
                raise VaultError("no_match", "no heading with this title", path=clean, heading=wanted)
            if len(matches) > 1:
                raise VaultError("ambiguous_match", "several headings have this title; add the level, e.g. '## Status'",
                                 path=clean, lines=[m[0] + 1 for m in matches])
            index, level, _ = matches[0]
            end = next((h[0] for h in found if h[0] > index and h[1] <= level), len(lines))
            eol = "\r\n" if "\r\n" in text else "\n"
            block = content.replace("\r\n", "\n").replace("\n", eol)
            if block and not block.endswith(eol):
                block += eol
            block_lines = split_lines(block)
            if not lines[index].endswith("\n"):
                lines[index] += eol
            body = lines[index + 1 : end]

            def blank(line: str) -> bool:
                return line.strip() == ""

            if mode == "replace":
                lead = [eol] if body and blank(body[0]) else []
                tail = [eol] if end < len(lines) and block_lines and not blank(block_lines[-1]) else []
                new_body = lead + block_lines + tail
            elif mode == "append":
                last = len(body)
                while last > 0 and blank(body[last - 1]):
                    last -= 1
                if last > 0 and not body[last - 1].endswith("\n"):
                    body[last - 1] += eol
                new_body = body[:last] + block_lines + body[last:]
            else:  # prepend
                start = 1 if body and blank(body[0]) else 0
                new_body = body[:start] + block_lines + body[start:]
            data = "".join(lines[: index + 1] + new_body + lines[end:]).encode("utf-8")
            self._check_size(data, clean)
            ensure_free_space(self.paths.root, self.config.min_free_bytes, len(data))
            atomic_write(absolute, data, noreplace=False, mode_from=absolute)
            self._index_file(clean, absolute)
            result = self._write_result(clean, data)
            result["heading_line"] = index + 1
            return result

    def move_note(
        self, path: str, new_path: str, create_folders: bool = False, update_links: bool = True
    ) -> dict[str, Any]:
        src_rel, src = self.paths.resolve(path)
        dst_rel, dst = self.paths.resolve(new_path)
        with self.lock:
            try:
                st = os.lstat(src)
            except FileNotFoundError as exc:
                raise VaultError("not_found", "source not found", path=src_rel) from exc
            if stat.S_ISLNK(st.st_mode):
                raise VaultError("outside_vault", "symlinks are not followed", path=src_rel)
            is_dir = stat.S_ISDIR(st.st_mode)
            if src_rel == dst_rel:
                raise VaultError("invalid_argument", "source and target are the same", path=src_rel)
            if is_dir and dst_rel.startswith(src_rel + "/"):
                raise VaultError("invalid_argument", "a folder cannot be moved into itself", path=src_rel)
            dst_stem = self._strip_note_ext(dst_rel.rsplit("/", 1)[-1])
            if update_links and (not link_safe_path(dst_rel) or dst_stem != dst_stem.strip()):
                raise VaultError(
                    "invalid_argument",
                    "the new path cannot be written into a wikilink (it contains [ ] | # ^ ` \\, "
                    "control characters or spaces at the start or end of a name); choose another "
                    "name or pass update_links=false",
                    path=dst_rel,
                )
            self._assert_visibility_kept(src, src_rel, dst_rel)
            parent_rel = dst_rel.rsplit("/", 1)[0] if "/" in dst_rel else ""
            if not dst.parent.is_dir():
                if not create_folders:
                    raise VaultError("not_found", "the target folder does not exist", path=parent_rel)
                self.paths.make_folders(parent_rel)
            self.paths.assert_no_symlinks(dst_rel)
            moved: list[tuple[str, str]] = []
            before: list[str] = []
            if update_links:
                self.scan()  # catch links written by other apps since the last scan
                moved = self._moved_items(src_rel, src, dst_rel, is_dir)
                before = self._all_visible_files()
            rename_noreplace(src, dst)
            if is_dir:
                self.index.remove_prefix(src_rel)
                for key in [k for k in self.skipped if k.startswith(src_rel + "/")]:
                    self.skipped.pop(key)
            else:
                self.index.remove(src_rel)
                self.skipped.pop(src_rel, None)
            self._index_tree(dst_rel, dst)
            result: dict[str, Any] = {"from": src_rel, "to": dst_rel, "type": "folder" if is_dir else "file"}
            if update_links:
                updated, not_updated = self._rewrite_links_after_move(moved, before)
                result["links_updated"] = updated
                if not_updated:
                    result["links_not_updated"] = not_updated
            warnings = [] if is_dir else self._warnings_for(dst_rel)
            if warnings:
                result["warnings"] = warnings
            return result

    # ================================================================== trash

    def _trash_root(self) -> Path:
        root = self.paths.trash_root
        try:
            st = os.lstat(root)
        except FileNotFoundError:
            os.mkdir(root)
            return root
        if not stat.S_ISDIR(st.st_mode):
            raise VaultError("outside_vault", "the trash folder is not a real folder")
        return root

    def _read_meta(self, entry: Path) -> dict[str, Any] | None:
        meta_path = entry / "meta.json"
        try:
            mst = os.lstat(meta_path)
            if not stat.S_ISREG(mst.st_mode) or mst.st_size > 65536:
                return None
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            trashed = datetime.fromisoformat(meta["trashed_at"])
            if trashed.tzinfo is None:
                return None
            if not isinstance(meta.get("original_path"), str):
                return None
            meta["_trashed"] = trashed
            return meta
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _trash_entries(self) -> list[tuple[str, Path, dict[str, Any]]]:
        root = self.paths.trash_root
        try:
            if not stat.S_ISDIR(os.lstat(root).st_mode):
                return []
            names = os.listdir(root)
        except FileNotFoundError:
            return []
        entries = []
        for name in names:
            if not TRASH_ID.match(name):
                continue
            entry = root / name
            if not stat.S_ISDIR(os.lstat(entry).st_mode):
                continue
            meta = self._read_meta(entry)
            if meta is not None:
                entries.append((name, entry, meta))
        entries.sort(key=lambda e: e[0], reverse=True)
        return entries

    def _expires(self, trashed: datetime) -> str | None:
        if self.config.trash_retention_days <= 0:
            return None
        return (trashed + timedelta(days=self.config.trash_retention_days)).isoformat(timespec="seconds")

    def move_to_trash(self, path: str) -> dict[str, Any]:
        clean, absolute = self.paths.resolve(path)
        with self.lock:
            try:
                st = os.lstat(absolute)
            except FileNotFoundError as exc:
                raise VaultError("not_found", "nothing found at this path", path=clean) from exc
            if stat.S_ISLNK(st.st_mode):
                raise VaultError("outside_vault", "symlinks are not followed", path=clean)
            is_dir = stat.S_ISDIR(st.st_mode)
            if is_dir:
                for dirpath, dirnames, filenames in os.walk(absolute, followlinks=False):
                    dirnames[:] = [d for d in dirnames if d not in SYSTEM_JUNK_DIRS]
                    junk = {f for f in filenames if f in SYSTEM_JUNK_FILES or f.startswith("._")}
                    for name in dirnames + [f for f in filenames if f not in junk]:
                        inner = clean + "/" + self.paths.to_rel_from(absolute, Path(dirpath) / name)
                        if self.paths.is_hidden(inner):
                            raise VaultError(
                                "hidden_path",
                                "this folder contains hidden or excluded files; they cannot be trashed "
                                "through this server",
                                path=clean,
                            )
            root = self._trash_root()
            now = datetime.now(UTC).replace(microsecond=0)
            trash_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
            entry = root / trash_id
            os.mkdir(entry)
            parent_rel = clean.rsplit("/", 1)[0] if "/" in clean else ""
            os.mkdir(entry / "files")
            if parent_rel:
                make_folders(entry / "files", parent_rel)
            target = entry / "files" / clean
            try:
                rename_noreplace(absolute, target)
            except BaseException:
                shutil.rmtree(entry, ignore_errors=True)
                raise
            meta = {
                "version": 1,
                "original_path": clean,
                "type": "folder" if is_dir else "file",
                "trashed_at": now.isoformat(),
            }
            try:
                atomic_write(entry / "meta.json", json.dumps(meta, ensure_ascii=False).encode("utf-8"),
                             noreplace=True)
            except BaseException:
                rename_noreplace(target, absolute)  # put it back
                shutil.rmtree(entry, ignore_errors=True)
                raise
            if is_dir:
                self.index.remove_prefix(clean)
                for key in [k for k in self.skipped if k.startswith(clean + "/")]:
                    self.skipped.pop(key)
            else:
                self.index.remove(clean)
                self.skipped.pop(clean, None)
            return {
                "trash_id": trash_id,
                "original_path": clean,
                "type": meta["type"],
                "trashed_at": meta["trashed_at"],
                "expires_at": self._expires(now),
            }

    def list_trash(self, limit: int | None = None) -> dict[str, Any]:
        limit = self._limit(limit, "limit", 200, 2000)
        entries = self._trash_entries()
        items = [
            {
                "trash_id": name,
                "original_path": meta["original_path"],
                "type": meta.get("type", "file"),
                "trashed_at": meta["trashed_at"],
                "expires_at": self._expires(meta["_trashed"]),
            }
            for name, _, meta in entries[:limit]
        ]
        return self._fit({"items": items, "complete": len(entries) <= limit}, "items")

    def restore_from_trash(self, trash_id: str, target_path: str | None = None) -> dict[str, Any]:
        if not isinstance(trash_id, str) or not TRASH_ID.match(trash_id):
            raise VaultError("invalid_argument", "trash_id has the wrong format")
        with self.lock:
            entry = self.paths.trash_root / trash_id
            try:
                if not stat.S_ISDIR(os.lstat(self.paths.trash_root).st_mode):
                    raise VaultError("outside_vault", "the trash folder is not a real folder")
                if not stat.S_ISDIR(os.lstat(entry).st_mode):
                    raise FileNotFoundError
            except FileNotFoundError as exc:
                raise VaultError("not_found", "no trash entry with this id", trash_id=trash_id) from exc
            meta = self._read_meta(entry)
            if meta is None:
                raise VaultError("not_found", "the trash entry has no valid meta.json", trash_id=trash_id)
            # original_path is untrusted input: it goes through the same checks as any path.
            original_rel = self.paths.normalize(meta["original_path"])
            if self.paths.is_hidden(original_rel):
                raise VaultError("hidden_path", "the stored original path is hidden", path=original_rel)
            source = entry / "files" / original_rel
            self.paths.check_chain(entry, source, original_rel)
            try:
                sst = os.lstat(source)
            except FileNotFoundError as exc:
                raise VaultError("not_found", "the trashed content is missing", trash_id=trash_id) from exc
            if stat.S_ISLNK(sst.st_mode):
                raise VaultError("outside_vault", "symlinks are not followed", trash_id=trash_id)
            dst_rel, dst = self.paths.resolve(target_path if target_path else original_rel)
            self._assert_visibility_kept(source, original_rel, dst_rel)
            parent_rel = dst_rel.rsplit("/", 1)[0] if "/" in dst_rel else ""
            self.paths.make_folders(parent_rel)
            self.paths.assert_no_symlinks(dst_rel)
            rename_noreplace(source, dst)
            # Remove only our own structure: meta.json and the now empty folders.
            os.unlink(entry / "meta.json")
            for dirpath, dirnames, filenames in os.walk(entry, topdown=False, followlinks=False):
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
            self._index_tree(dst_rel, dst)
            result: dict[str, Any] = {"trash_id": trash_id, "restored_to": dst_rel}
            warnings = self._warnings_for(dst_rel) if self.paths.is_note_name(dst_rel) else []
            if warnings:
                result["warnings"] = warnings
            return result

    def purge_trash(self, now: datetime | None = None) -> int:
        """Remove expired trash entries. The only place where the server deletes files."""
        if self.config.trash_retention_days <= 0:
            return 0
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=self.config.trash_retention_days)
        removed = 0
        with self.lock:
            for name, entry, meta in self._trash_entries():
                if meta["_trashed"] < cutoff:
                    if stat.S_ISLNK(os.lstat(entry).st_mode):
                        continue
                    shutil.rmtree(entry)  # does not follow symlinks
                    removed += 1
                    log.info("purged trash entry %s", name)
            self.last_purge = time.time()
        return removed

    # ================================================================== scanning

    def startup(self) -> None:
        removed = remove_temp_files(self.paths.root)
        if removed:
            log.info("removed %d leftover temporary files", removed)
        self.scan()

    def scan(self) -> dict[str, int]:
        """Bring the index in line with the files. Reads without the lock, stores with it."""
        found: dict[str, tuple[int, int]] = {}
        real_paths: dict[str, Path] = {}
        skipped: dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(self.paths.root, followlinks=False):
            base = Path(dirpath)
            keep = []
            for name in dirnames:
                full = base / name
                rel = self.paths.to_rel(full)
                if self.paths.is_hidden_name(name) or self.paths.is_hidden(rel):
                    continue
                if os.path.islink(full):
                    skipped[rel] = "symlink (not followed)"
                    continue
                keep.append(name)
            dirnames[:] = keep
            for name in filenames:
                full = base / name
                rel = self.paths.to_rel(full)
                if self.paths.is_hidden_name(name) or self.paths.is_hidden(rel):
                    continue
                try:
                    st = os.lstat(full)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    skipped[rel] = "symlink (not followed)"
                    continue
                if not stat.S_ISREG(st.st_mode) or not self.paths.is_note_name(name):
                    continue
                if st.st_size > self.config.max_file_bytes:
                    skipped[rel] = "too large"
                    continue
                found[rel] = (st.st_mtime_ns, st.st_size)
                real_paths[rel] = full

        known = self.index.state()
        added = updated = removed = 0
        for rel, signature in found.items():
            if known.get(rel) == signature:
                continue
            full = real_paths[rel]
            try:
                st = os.lstat(full)  # signature first: a later change makes it mismatch below
                raw = full.read_bytes()
            except OSError:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped[rel] = "not valid UTF-8"
                with self.lock:
                    self.index.remove(rel)
                continue
            with self.lock:
                # Store only if the file did not change while we read it.
                try:
                    now_st = os.lstat(full)
                except FileNotFoundError:
                    continue
                if (now_st.st_mtime_ns, now_st.st_size) != (st.st_mtime_ns, st.st_size) or len(raw) != st.st_size:
                    continue
                try:
                    self.index.upsert(rel, text, raw, st.st_mtime_ns, st.st_size)
                except Exception as exc:  # noqa: BLE001 - one bad note must never stop the scan
                    log.warning("could not index %s: %s", rel, type(exc).__name__)
                    self.index.remove(rel)
                    skipped[rel] = f"could not be indexed ({type(exc).__name__})"
                    continue
            if rel in known:
                updated += 1
            else:
                added += 1
        for rel in known:
            if rel in found:
                continue
            with self.lock:
                # A server write may have created the file after the walk: keep it then.
                if rel not in skipped and self._is_indexable(rel):
                    continue
                self.index.remove(rel)
                removed += 1
        self.skipped = skipped
        self.last_scan_finished = time.time()
        if added or updated or removed:
            log.info("scan: %d added, %d updated, %d removed", added, updated, removed)
        return {"added": added, "updated": updated, "removed": removed}
