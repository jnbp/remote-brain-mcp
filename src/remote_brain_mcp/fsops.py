"""Low-level file operations: atomic writes and no-replace renames."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import secrets
import shutil
import stat
from pathlib import Path

from .errors import VaultError

TEMP_PREFIX = ".rbm-tmp-"
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1

_libc = None
try:  # renameat2 is available in glibc >= 2.28
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    _renameat2 = _libc.renameat2
    _renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    _renameat2.restype = ctypes.c_int
except (OSError, AttributeError):
    _renameat2 = None


def rename_noreplace(src: Path, dst: Path) -> None:
    """Rename `src` to `dst`, failing with `already_exists` if `dst` exists.

    Uses renameat2(RENAME_NOREPLACE) so the check and the rename are one atomic
    step. Falls back to link+unlink for files on file systems without support.
    """
    if _renameat2 is not None:
        result = _renameat2(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE)
        if result == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise VaultError("already_exists", "the target already exists")
        if err not in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP):
            raise OSError(err, os.strerror(err), str(src))
    # Fallback without kernel support.
    if os.path.lexists(dst):
        raise VaultError("already_exists", "the target already exists")
    if stat.S_ISDIR(os.lstat(src).st_mode):
        os.rename(src, dst)  # best effort: a directory cannot be hard-linked
        return
    try:
        os.link(src, dst, follow_symlinks=False)
    except FileExistsError as exc:
        raise VaultError("already_exists", "the target already exists") from exc
    os.unlink(src)


def atomic_write(target: Path, data: bytes, *, noreplace: bool, mode_from: Path | None = None) -> None:
    """Write `data` to `target` via a temporary file in the same folder.

    With `noreplace=True` the final rename fails if the target exists.
    The file mode of `mode_from` (usually the old version) is copied.
    """
    folder = target.parent
    tmp = folder / f"{TEMP_PREFIX}{secrets.token_hex(8)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode_from is not None:
            try:
                os.chmod(tmp, stat.S_IMODE(os.lstat(mode_from).st_mode))
            except FileNotFoundError:
                pass
        if noreplace:
            rename_noreplace(tmp, target)
        else:
            os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def ensure_free_space(folder: Path, min_free_bytes: int, needed: int) -> None:
    if min_free_bytes <= 0:
        return
    free = shutil.disk_usage(folder).free
    if free - needed < min_free_bytes:
        raise VaultError("disk_full", "not enough free space on the vault drive", free_bytes=free)


def make_folders(root: Path, rel_folder: str) -> None:
    """Create `rel_folder` below `root` one level at a time, refusing symlinks."""
    current = root
    if rel_folder == "":
        return
    for part in rel_folder.split("/"):
        current = current / part
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


def remove_temp_files(root: Path) -> int:
    """Remove leftover temporary files from interrupted writes. Never follows symlinks."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            if name.startswith(TEMP_PREFIX):
                path = Path(dirpath) / name
                if stat.S_ISREG(os.lstat(path).st_mode):
                    os.unlink(path)
                    removed += 1
    return removed
