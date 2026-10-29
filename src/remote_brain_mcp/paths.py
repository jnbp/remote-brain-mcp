"""Path confinement: every tool path is checked here before any file access."""

from __future__ import annotations

import fnmatch
import os
import stat
import unicodedata
from pathlib import Path

from .config import ALWAYS_HIDDEN, Config
from .errors import VaultError


class VaultPaths:
    """Turns untrusted relative paths into safe absolute paths inside the vault."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = Path(os.path.realpath(config.vault_path))
        self.trash_root = self.root / config.trash_dir

    # -- normalisation -------------------------------------------------------------

    def normalize(self, rel: str, allow_root: bool = False) -> str:
        """Return a clean relative POSIX path, or raise.

        Rejects absolute paths, `.` and `..` segments, empty segments and NUL bytes.
        A single trailing slash is tolerated. Unicode is normalised to NFC.
        """
        if not isinstance(rel, str):
            raise VaultError("invalid_argument", "path must be a string")
        rel = unicodedata.normalize("NFC", rel)
        if "\x00" in rel:
            raise VaultError("invalid_argument", "path must not contain NUL bytes")
        if rel in ("", ".", "./"):
            if allow_root:
                return ""
            raise VaultError("invalid_argument", "a path inside the vault is required")
        if rel.startswith("/"):
            raise VaultError("outside_vault", "use a path relative to the vault root", path=rel)
        rel = rel.removesuffix("/")
        parts = rel.split("/")
        for part in parts:
            if part == "":
                raise VaultError("invalid_argument", "path must not contain empty segments", path=rel)
            if part in (".", ".."):
                raise VaultError("outside_vault", "'.' and '..' are not allowed in paths", path=rel)
        return "/".join(parts)

    # -- hidden areas ---------------------------------------------------------------

    def is_hidden_name(self, name: str) -> bool:
        if name.startswith(".") or name in ALWAYS_HIDDEN:
            return True
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.config.exclude)

    def is_hidden(self, rel: str) -> bool:
        if rel == "":
            return False
        parts = rel.split("/")
        if any(self.is_hidden_name(p) for p in parts):
            return True
        # a pattern hides the matching path and everything below it
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            if any(fnmatch.fnmatchcase(prefix, pattern) for pattern in self.config.exclude):
                return True
        return False

    def is_note_name(self, name: str) -> bool:
        lower = name.lower()
        return any(lower.endswith(ext) for ext in self.config.text_extensions)

    # -- resolution -------------------------------------------------------------------

    def resolve(self, rel: str, *, allow_root: bool = False, allow_hidden: bool = False) -> tuple[str, Path]:
        """Normalise `rel` and return (rel, absolute path).

        Every existing component between the vault root and the target is checked
        with lstat: symlinks are never followed. The target itself may not exist.
        """
        clean = self.normalize(rel, allow_root=allow_root)
        if not allow_hidden and self.is_hidden(clean):
            raise VaultError("hidden_path", "this path is hidden or excluded", path=clean)
        absolute = self.locate(clean)
        self.check_chain(self.root, absolute, clean)
        return clean, absolute

    def locate(self, clean: str, base: Path | None = None) -> Path:
        """Map an NFC path to the name actually stored on disk.

        macOS and some sync tools store names in decomposed (NFD) form; tools always use
        NFC. Each missing component is looked up by comparing NFC forms.
        """
        current = base or self.root
        if clean == "":
            return current
        parts = clean.split("/")
        for position, part in enumerate(parts):
            candidate = current / part
            if not os.path.lexists(candidate):
                match = None
                try:
                    for name in os.listdir(current):
                        if unicodedata.normalize("NFC", name) == part:
                            match = name
                            break
                except (FileNotFoundError, NotADirectoryError, PermissionError):
                    pass
                if match is None:
                    return current.joinpath(*parts[position:])
                candidate = current / match
            current = candidate
        return current

    @staticmethod
    def check_chain(base: Path, absolute: Path, label: str) -> None:
        """Refuse if any existing component between `base` and `absolute` is a symlink."""
        current = base
        for part in absolute.relative_to(base).parts:
            current = current / part
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                return  # the rest does not exist yet
            if stat.S_ISLNK(st.st_mode):
                raise VaultError("outside_vault", "symlinks are not followed", path=label)

    def make_folders(self, rel_folder: str) -> Path:
        """Create missing folders below the vault root, reusing existing ones in any
        Unicode normalisation form. Refuses symlinks and files in the way."""
        current = self.root
        if rel_folder == "":
            return current
        for part in rel_folder.split("/"):
            current = self.locate(part, base=current)
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                try:
                    os.mkdir(current)
                except FileExistsError:
                    pass
                st = os.lstat(current)
            if stat.S_ISLNK(st.st_mode):
                raise VaultError("outside_vault", "symlinks are not followed", path=rel_folder)
            if not stat.S_ISDIR(st.st_mode):
                raise VaultError("already_exists", "a file is in the way of this folder", path=rel_folder)
        return current

    def assert_no_symlinks(self, clean: str) -> None:
        self.check_chain(self.root, self.locate(clean), clean)

    @staticmethod
    def to_rel_from(base: Path, absolute: Path) -> str:
        return unicodedata.normalize("NFC", absolute.relative_to(base).as_posix())

    def to_rel(self, absolute: Path) -> str:
        return unicodedata.normalize("NFC", absolute.relative_to(self.root).as_posix())
