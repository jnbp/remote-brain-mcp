"""End-to-end tests through real HTTP: auth, host check, protocol compatibility, tool calls."""

from __future__ import annotations

import json
import socket
import threading
import time

import anyio
import httpx
import pytest
import uvicorn

from remote_brain_mcp.config import Config
from remote_brain_mcp.server import build_app
from remote_brain_mcp.vault import Vault

from .conftest import TOKEN, write

EXPECTED_TOOLS = {
    "get_status", "list_folder", "read_note", "search", "resolve_link", "get_backlinks",
    "find_by_frontmatter", "create_note", "edit_note", "append_to_note", "replace_note",
    "move_note", "edit_section", "move_to_trash", "list_trash", "restore_from_trash",
}
ACCEPT = "application/json, text/event-stream"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "vault"
    write(root, "Projects/Alpha.md", "---\ntype: Project\n---\n# Alpha\nThe launch date is in spring.\n")
    config = Config.from_env({
        "RB_TOKEN": TOKEN,
        "RB_VAULT_PATH": str(root),
        "RB_DATA_PATH": str(tmp_path / "data"),
        "RB_MIN_FREE_BYTES": "0",
        "RB_ALLOWED_HOSTS": "127.0.0.1,brain.example.org",
    })
    vault = Vault(config)
    vault.startup()
    port = free_port()
    uv = uvicorn.Server(uvicorn.Config(build_app(config, vault), host="127.0.0.1", port=port,
                                       log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", vault
    uv.should_exit = True
    thread.join(timeout=5)


def rpc(base: str, method: str, params: dict, *, token: str | None = TOKEN, host: str | None = None,
        msg_id: int = 1) -> httpx.Response:
    headers = {"Accept": ACCEPT, "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if host is not None:
        headers["Host"] = host
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
    return httpx.post(f"{base}/mcp", headers=headers, json=body, timeout=10)


INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}


def test_health_is_open(server):
    base, _ = server
    response = httpx.get(f"{base}/health")
    assert response.status_code == 200 and response.text == "ok"


@pytest.mark.parametrize("token", [None, "wrong" * 10, TOKEN + "x", TOKEN[:-1]])
def test_requests_without_valid_token_are_refused(server, token):
    base, _ = server
    response = rpc(base, "initialize", INIT, token=token)
    assert response.status_code == 401
    assert "www-authenticate" in response.headers


def test_other_auth_schemes_are_refused(server):
    base, _ = server
    headers = {"Accept": ACCEPT, "Content-Type": "application/json", "Authorization": f"Basic {TOKEN}"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT}
    assert httpx.post(f"{base}/mcp", headers=headers, json=body).status_code == 401


def error_payload(text: str) -> dict:
    """The SDK prefixes tool errors with 'Error executing tool <name>: '; the JSON follows."""
    return json.loads(text[text.index("{"):])


def test_unknown_host_is_refused(server):
    base, _ = server
    response = rpc(base, "initialize", INIT, host="evil.example.com")
    assert response.status_code >= 400
    allowed = rpc(base, "initialize", INIT, host="brain.example.org")
    assert allowed.status_code == 200


def test_initialize_with_older_protocol_and_call_tools(server):
    base, _ = server
    init = rpc(base, "initialize", INIT)
    assert init.status_code == 200, init.text
    assert init.json()["result"]["protocolVersion"] == "2025-06-18"

    tools = rpc(base, "tools/list", {}, msg_id=2)
    assert tools.status_code == 200, tools.text
    names = {tool["name"] for tool in tools.json()["result"]["tools"]}
    assert names == EXPECTED_TOOLS

    found = rpc(base, "tools/call", {"name": "search", "arguments": {"query": "launch"}}, msg_id=3)
    result = found.json()["result"]
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["hits"][0]["path"] == "Projects/Alpha.md"

    failed = rpc(base, "tools/call", {"name": "read_note", "arguments": {"path": "../etc/passwd"}}, msg_id=4)
    error = failed.json()["result"]
    assert error["isError"] is True
    assert error_payload(error["content"][0]["text"])["error"] == "outside_vault"


def test_official_client_end_to_end(server):
    base, vault = server
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def scenario() -> None:
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10) as client,
            streamable_http_client(f"{base}/mcp", http_client=client) as (read, write_stream),
            ClientSession(read, write_stream) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            assert {t.name for t in listed.tools} == EXPECTED_TOOLS
            created = await session.call_tool(
                "create_note", {"path": "Inbox/new.md", "content": "# New\nfresh idea\n"}
            )
            assert not created.is_error, created
            read_result = await session.call_tool("read_note", {"path": "Inbox/new.md"})
            sha = json.loads(read_result.content[0].text)["sha256"]
            edited = await session.call_tool("edit_note", {
                "path": "Inbox/new.md", "old_text": "fresh", "new_text": "better",
                "expected_sha256": sha,
            })
            assert not edited.is_error, edited
            hits = await session.call_tool("search", {"query": "better idea"})
            assert json.loads(hits.content[0].text)["hits"][0]["path"] == "Inbox/new.md"

    anyio.run(scenario)
    assert (vault.paths.root / "Inbox/new.md").read_text() == "# New\nbetter idea\n"


def test_get_stream_is_not_offered(server):
    base, _ = server
    response = httpx.get(f"{base}/mcp", headers={"Authorization": f"Bearer {TOKEN}", "Accept": "text/event-stream"},
                         timeout=5)
    assert response.status_code == 405


def test_origin_header_is_refused_unless_configured(server):
    base, _ = server
    headers = {"Accept": ACCEPT, "Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}",
               "Origin": "https://evil.example"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT}
    assert httpx.post(f"{base}/mcp", headers=headers, json=body, timeout=5).status_code == 403
