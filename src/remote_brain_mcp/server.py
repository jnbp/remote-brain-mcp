"""MCP server: tool definitions, bearer-token guard and background jobs."""

from __future__ import annotations

import functools
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Config
from .errors import VaultError
from .vault import Vault

log = logging.getLogger("remote_brain_mcp")

INSTRUCTIONS = """\
Access to one folder of Markdown notes (a "second brain").
Paths are relative to the vault root, for example "Projects/Alpha/Alpha.md".
Typical flow: `search` (or `resolve_link` / `find_by_frontmatter`) to find notes, `read_note` to read
them (it returns `sha256`), then `edit_note` with that `expected_sha256` for a precise change, or
`append_to_note` to add text. Nothing is deleted: `move_to_trash` keeps items restorable with
`restore_from_trash` until they expire. Responses carry `complete`; if it is false, fetch the rest
(for example with `next_start_line`).
"""


def _tool(fn: Callable[..., dict[str, Any]], name: str) -> Callable[..., str]:
    """Wrap a vault call: JSON output, VaultError -> ToolError, one log line without content."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        started = time.monotonic()
        target = kwargs.get("path") or kwargs.get("trash_id") or ""
        try:
            result = fn(*args, **kwargs)
        except VaultError as exc:
            log.info("tool=%s path=%s status=%s ms=%d", name, target, exc.code,
                     (time.monotonic() - started) * 1000)
            raise ToolError(exc.to_json()) from exc
        except OSError as exc:  # never leak absolute server paths
            log.warning("tool=%s path=%s status=os_error errno=%s", name, target, exc.errno)
            raise ToolError(VaultError("io_error", exc.strerror or "file system error").to_json()) from exc
        log.info("tool=%s path=%s status=ok ms=%d", name, target, (time.monotonic() - started) * 1000)
        return json.dumps(result, ensure_ascii=False)

    return wrapper


def build_mcp(vault: Vault) -> MCPServer:
    mcp = MCPServer(name="remote-brain-mcp", version=__version__, instructions=INSTRUCTIONS)

    @mcp.tool(structured_output=False, description="Vault overview: note count, index age, skipped files, trash size, version.")
    def get_status() -> str:
        return _tool(vault.get_status, "get_status")()

    @mcp.tool(structured_output=False, description=(
        "List a folder. type is 'note' (searchable text), 'file' (other files) or 'folder'. "
        "Use recursive=true for the whole tree. Hidden and excluded paths are never shown."
    ))
    def list_folder(path: str = ".", recursive: bool = False, limit: int = 500) -> str:
        return _tool(vault.list_folder, "list_folder")(path=path, recursive=recursive, limit=limit)

    @mcp.tool(structured_output=False, description=(
        "Read a note, optionally a range of lines (line_count up to 2000). Returns sha256 of the whole "
        "file: pass it as expected_sha256 when editing. complete=false means only part was returned; "
        "continue with next_start_line."
    ))
    def read_note(path: str, start_line: int = 1, line_count: int = 400) -> str:
        return _tool(vault.read_note, "read_note")(path=path, start_line=start_line, line_count=line_count)

    @mcp.tool(structured_output=False, description=(
        "Full-text search over note contents, titles, frontmatter and paths, ranked by relevance. "
        "mode='words' (default): all words must match, supports \"exact phrases\" and prefix* terms, "
        "ignores case and accents. mode='substring': finds parts of words (terms of 3+ characters). "
        "path_prefix limits the search to a folder. Each hit has the first matching line number."
    ))
    def search(query: str, path_prefix: str | None = None, limit: int = 20, mode: str = "words") -> str:
        return _tool(vault.search, "search")(query=query, path_prefix=path_prefix, limit=limit, mode=mode)

    @mcp.tool(structured_output=False, description=(
        "Find the note a wikilink points to, e.g. 'Project Alpha' or '[[Project Alpha#Status]]'. "
        "Returns all matching paths; ambiguous=true if more than one note has that name."
    ))
    def resolve_link(link: str) -> str:
        return _tool(vault.resolve_link, "resolve_link")(link=link)

    @mcp.tool(structured_output=False, description="List the notes that link to this note with [[wikilinks]], with the linking line.")
    def get_backlinks(path: str) -> str:
        return _tool(vault.get_backlinks, "get_backlinks")(path=path)

    @mcp.tool(structured_output=False, description=(
        "Find notes by YAML frontmatter, e.g. {\"type\": \"Project\", \"status\": \"Active\"}. "
        "A list field matches if it contains the value; [[X]] and X are treated as equal."
    ))
    def find_by_frontmatter(filters: dict[str, Any], path_prefix: str | None = None, limit: int = 100) -> str:
        return _tool(vault.find_by_frontmatter, "find_by_frontmatter")(
            filters=filters, path_prefix=path_prefix, limit=limit
        )

    @mcp.tool(structured_output=False, description=(
        "Create a new note. Fails if the file already exists. Missing folders are created unless "
        "create_folders=false. Warns if another note already has the same name."
    ))
    def create_note(path: str, content: str, create_folders: bool = True) -> str:
        return _tool(vault.create_note, "create_note")(path=path, content=content, create_folders=create_folders)

    @mcp.tool(structured_output=False, description=(
        "Replace exactly one passage in a note. old_text must occur exactly once (include enough context). "
        "Pass expected_sha256 from read_note; the edit is refused with 'conflict' if the note changed."
    ))
    def edit_note(path: str, old_text: str, new_text: str, expected_sha256: str | None = None) -> str:
        return _tool(vault.edit_note, "edit_note")(
            path=path, old_text=old_text, new_text=new_text, expected_sha256=expected_sha256
        )

    @mcp.tool(structured_output=False, description=(
        "Append text to the end of a note (a line break is added first if needed). "
        "expected_sha256 is optional."
    ))
    def append_to_note(path: str, text: str, expected_sha256: str | None = None) -> str:
        return _tool(vault.append_to_note, "append_to_note")(path=path, text=text, expected_sha256=expected_sha256)

    @mcp.tool(structured_output=False, description=(
        "Rewrite a whole note, for restructuring. expected_sha256 from read_note is required."
    ))
    def replace_note(path: str, content: str, expected_sha256: str) -> str:
        return _tool(vault.replace_note, "replace_note")(path=path, content=content, expected_sha256=expected_sha256)

    @mcp.tool(structured_output=False, description=(
        "Move or rename a note, file or folder. Fails if the target exists, or if the target folder is "
        "missing and create_folders=false. With update_links=true (default) every [[wikilink]] to the "
        "moved notes is updated, including links in frontmatter; aliases and headings are kept."
    ))
    def move_note(path: str, new_path: str, create_folders: bool = False, update_links: bool = True) -> str:
        return _tool(vault.move_note, "move_note")(
            path=path, new_path=new_path, create_folders=create_folders, update_links=update_links
        )

    @mcp.tool(structured_output=False, description=(
        "Edit the text under one heading. heading is the title ('Status') or with level ('## Status'). "
        "mode='replace' swaps the section body (needs expected_sha256 from read_note by default), "
        "'append' adds to the end of the section, 'prepend' to its start. The section ends at the next "
        "heading of the same or a higher level."
    ))
    def edit_section(path: str, heading: str, content: str, mode: str = "replace",
                     expected_sha256: str | None = None) -> str:
        return _tool(vault.edit_section, "edit_section")(
            path=path, heading=heading, content=content, mode=mode, expected_sha256=expected_sha256
        )

    @mcp.tool(structured_output=False, description=(
        "Move a note, file or folder to the trash. It can be restored with restore_from_trash until it "
        "expires. Nothing is deleted directly."
    ))
    def move_to_trash(path: str) -> str:
        return _tool(vault.move_to_trash, "move_to_trash")(path=path)

    @mcp.tool(structured_output=False, description="List items in the trash with their trash_id, original path and expiry.")
    def list_trash(limit: int = 200) -> str:
        return _tool(vault.list_trash, "list_trash")(limit=limit)

    @mcp.tool(structured_output=False, description=(
        "Restore an item from the trash to its original path, or to target_path. Fails if the target exists."
    ))
    def restore_from_trash(trash_id: str, target_path: str | None = None) -> str:
        return _tool(vault.restore_from_trash, "restore_from_trash")(trash_id=trash_id, target_path=target_path)

    return mcp


class BearerGuard:
    """ASGI wrapper: /health is open; everything else needs the bearer token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == "/health":
            await self._respond(send, 200, b"ok", b"text/plain")
            return
        supplied = b""
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                scheme, _, credentials = value.partition(b" ")
                if scheme.lower() == b"bearer":
                    supplied = credentials.strip()
                break
        if not supplied or not hmac.compare_digest(supplied, self.token):
            await self._respond(
                send, 401, b'{"error":"unauthorized"}', b"application/json",
                extra=[(b"www-authenticate", b'Bearer realm="remote-brain-mcp"')],
            )
            return
        if scope.get("method") == "GET":
            # Stateless JSON mode has no server-initiated stream; the MCP spec allows 405 here.
            await self._respond(send, 405, b'{"error":"method_not_allowed"}', b"application/json",
                                extra=[(b"allow", b"POST")])
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _respond(send: Send, status: int, body: bytes, content_type: bytes,
                       extra: list[tuple[bytes, bytes]] | None = None) -> None:
        headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode())]
        headers += extra or []
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def build_app(config: Config, vault: Vault) -> ASGIApp:
    mcp = build_mcp(vault)
    hosts: list[str] = []
    for host in config.allowed_hosts:
        hosts += [host, f"{host}:*"]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=config.allowed_origins
    )
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=config.max_request_bytes,
        transport_security=security,
        host=config.host,
    )
    return BearerGuard(inner, config.token)


def start_background_jobs(vault: Vault, stop: threading.Event) -> threading.Thread:
    def loop() -> None:
        while not stop.wait(vault.config.scan_interval_seconds):
            try:
                vault.scan()
                if vault.last_purge is None or time.time() - vault.last_purge >= 3600:
                    vault.purge_trash()
            except Exception:  # keep the loop alive; the next run tries again
                log.exception("background job failed")

    thread = threading.Thread(target=loop, name="remote-brain-scanner", daemon=True)
    thread.start()
    return thread
