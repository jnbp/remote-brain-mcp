"""Error type shared by all vault operations."""

from __future__ import annotations

import json
from typing import Any


class VaultError(Exception):
    """An anticipated failure with a stable machine-readable code.

    Codes: not_found, already_exists, outside_vault, hidden_path, not_a_note,
    conflict, ambiguous_match, no_match, too_large, disk_full, invalid_argument,
    unauthorized.
    """

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_json(self) -> str:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        payload.update(self.details)
        return json.dumps(payload, ensure_ascii=False)
