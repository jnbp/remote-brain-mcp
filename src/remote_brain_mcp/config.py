"""Configuration from environment variables."""

from __future__ import annotations

import os
import secrets
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

MIN_TOKEN_LENGTH = 32

# Names that are always hidden, in addition to anything starting with a dot.
ALWAYS_HIDDEN = frozenset({"@eaDir", "#recycle", "#snapshot"})


class ConfigError(Exception):
    """Raised when the configuration is invalid; the server refuses to start."""


def _int(env: dict[str, str], name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _list(env: dict[str, str], name: str, default: str) -> list[str]:
    raw = env.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _token_from_data(data_path: Path) -> tuple[str, bool]:
    """Read <data>/token, or create it with a new random token (returns token, created)."""
    path = data_path / "token"
    try:
        return path.read_text(encoding="utf-8").strip(), False
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    token = secrets.token_hex(32)
    try:
        data_path.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip(), False
    except OSError as exc:
        raise ConfigError(f"no RB_TOKEN set and cannot create {path}: {exc}") from exc
    return token, True


@dataclass(frozen=True)
class Config:
    token: str
    vault_path: Path
    data_path: Path
    host: str = "0.0.0.0"
    port: int = 8080
    allowed_hosts: list[str] = field(default_factory=lambda: ["localhost", "127.0.0.1"])
    allowed_origins: list[str] = field(default_factory=list)
    scan_interval_seconds: int = 60
    trash_dir: str = ".rb-trash"
    trash_retention_days: int = 90
    text_extensions: tuple[str, ...] = (".md", ".txt")
    exclude: tuple[str, ...] = ()
    max_file_bytes: int = 2 * 1024 * 1024
    max_request_bytes: int = 4 * 1024 * 1024
    min_free_bytes: int = 100 * 1024 * 1024
    max_response_chars: int = 60_000
    require_hash_for_edits: bool = True
    log_level: str = "info"
    token_generated: bool = False  # True if the server created a new token file at this start

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)

        data_path = Path(env.get("RB_DATA_PATH", "/data"))
        token = env.get("RB_TOKEN", "").strip()
        token_file = env.get("RB_TOKEN_FILE", "").strip()
        token_generated = False
        if token_file:
            try:
                token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(f"cannot read RB_TOKEN_FILE: {exc}") from exc
        elif not token:
            token, token_generated = _token_from_data(data_path)
        token = token.strip()
        lowered = token.lower()
        if any(marker in lowered for marker in ("replace", "change-me", "changeme", "example", "your-token")):
            raise ConfigError("RB_TOKEN still contains the placeholder from the example; set a random value")
        if len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(
                f"RB_TOKEN (or RB_TOKEN_FILE) must be set to a random value of at least "
                f"{MIN_TOKEN_LENGTH} characters"
            )

        trash_dir = env.get("RB_TRASH_DIR", ".rb-trash").strip()
        if not trash_dir.startswith(".") or "/" in trash_dir or trash_dir in (".", ".."):
            raise ConfigError("RB_TRASH_DIR must be a single folder name starting with a dot")

        extensions = tuple(
            e.lower() if e.startswith(".") else "." + e.lower()
            for e in _list(env, "RB_TEXT_EXTENSIONS", ".md,.txt")
        )
        if not extensions:
            raise ConfigError("RB_TEXT_EXTENSIONS must not be empty")

        require_hash = env.get("RB_REQUIRE_HASH_FOR_EDITS", "true").strip().lower()
        if require_hash not in ("true", "false", "1", "0", "yes", "no"):
            raise ConfigError("RB_REQUIRE_HASH_FOR_EDITS must be true or false")

        vault_path = Path(env.get("RB_VAULT_PATH", "/vault"))
        if not vault_path.is_dir():
            raise ConfigError(f"RB_VAULT_PATH {vault_path} is not a folder")

        return cls(
            token=token,
            vault_path=vault_path,
            data_path=data_path,
            host=env.get("RB_HOST", "0.0.0.0"),
            port=_int(env, "RB_PORT", 8080, 1),
            allowed_hosts=_list(env, "RB_ALLOWED_HOSTS", "localhost,127.0.0.1"),
            allowed_origins=_list(env, "RB_ALLOWED_ORIGINS", ""),
            scan_interval_seconds=_int(env, "RB_SCAN_INTERVAL_SECONDS", 60, 5),
            trash_dir=trash_dir,
            trash_retention_days=_int(env, "RB_TRASH_RETENTION_DAYS", 90, 0),
            text_extensions=extensions,
            exclude=tuple(
                unicodedata.normalize("NFC", p.strip("/")) for p in _list(env, "RB_EXCLUDE", "") if p.strip("/")
            ),
            max_file_bytes=_int(env, "RB_MAX_FILE_BYTES", 2 * 1024 * 1024, 1024),
            max_request_bytes=_int(env, "RB_MAX_REQUEST_BYTES", 4 * 1024 * 1024, 1024),
            min_free_bytes=_int(env, "RB_MIN_FREE_BYTES", 100 * 1024 * 1024, 0),
            max_response_chars=_int(env, "RB_MAX_RESPONSE_CHARS", 60_000, 2_000),
            require_hash_for_edits=require_hash in ("true", "1", "yes"),
            log_level=env.get("RB_LOG_LEVEL", "info").strip().lower(),
            token_generated=token_generated,
        )
