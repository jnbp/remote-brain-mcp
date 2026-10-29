# remote-brain-mcp – Specification

Status: implemented in v0.1.0.

## 1. Purpose

`remote-brain-mcp` is a small, self-hosted MCP server that gives an AI agent safe,
fast and complete access to one folder of Markdown notes (a "second brain" or
"vault"). It runs as a Docker container next to the files, for example on a
Synology NAS, and speaks MCP over Streamable HTTP so it can be added to any MCP
client or MCP hub.

The agent gets full read and write access inside that one folder and no access
outside it. The agent can never delete anything directly: files go to a trash
folder and are purged by the server after a configurable retention period.

### Goals

- Fast full-text search over note contents (milliseconds, not minutes).
- Precise, safe edits: replace one passage, append, move, trash, restore.
- Understand Markdown-vault conventions: `[[wikilinks]]`, backlinks, YAML frontmatter.
- One folder, hard boundary, no credentials for the rest of the NAS.
- Light enough for a small NAS (target: under 256 MB RAM, no AI models).
- Easy for others to install: one image, one compose file, documented settings.

### Non-goals (v1)

- No AI models, embeddings or semantic search inside the server.
- No web UI. Configuration is done with environment variables.
- No general NAS administration (users, shares, containers, downloads).
- No multiple vaults per instance (run a second container instead).
- No OAuth. Authentication is a bearer token, optionally behind Cloudflare Access.

## 2. Why not a File Station-based MCP server

Existing Synology MCP servers access files through the DSM File Station web API.
For a note vault this has structural limits that cannot be fixed on the client side:

| Need | File Station API | remote-brain-mcp |
|---|---|---|
| Content search | DSM Search matches file names only, without wildcards, and returned empty results or error 600 in live tests. A client-side grep must download every file on each call (about one minute for roughly 100 notes in a live test). | SQLite FTS5 index kept next to the files; ranked results in milliseconds. |
| Edit a passage | Download the whole file, upload the whole file. | `edit_note` replaces one unique passage, guarded by SHA-256. |
| Safe writes | Overwrite with no conflict check; the caller has to compare metadata manually. | `expected_sha256` conflict check; atomic write (temp file plus rename). |
| Move | Moving into a missing folder silently renames the file. | Fails unless the target folder exists (or `create_folders` is set). |
| Delete | Hard delete or nothing. | `move_to_trash`, `list_trash`, `restore_from_trash`, timed purge. |
| Sessions | DSM session expires (error 106); needs a DSM account that can see all of its shares. | No DSM session. Access is limited to the mounted folder. |
| Vault awareness | None. | Wikilink resolution, backlinks, frontmatter queries. |

File Station servers remain the right tool for NAS administration. This server is
only for the notes.

## 3. Deployment

- Docker image based on `python:3.12-slim`, built for `linux/amd64` and
  `linux/arm64`, published to GitHub Container Registry (GHCR) by a GitHub Actions
  workflow. The image can also be built locally.
- The vault is mounted read-write at `/vault`.
- The index lives in a separate volume at `/data`, never inside the vault.
- The container runs as the user that owns the vault files, set with
  `user: "${PUID}:${PGID}"` in compose. The `/data` folder on the host must be
  writable by that user. New notes therefore keep the right owner for sync tools.
- Compose sets a memory limit (default `256m`); the image has a healthcheck on `/health`.
- The server listens on port `8080` inside the container. Two compose files:
  1. `compose.cloudflare.yaml`: `cloudflared` as a second service on a private Docker
     network (`http://remote-brain-mcp:8080`), no published port (recommended);
  2. `compose.yaml`: the server alone, published on `127.0.0.1:8080` by default
     (`RB_PUBLISH` changes it).

## 4. Configuration

All settings are environment variables. Defaults in brackets.

| Variable | Meaning |
|---|---|
| `RB_TOKEN` / `RB_TOKEN_FILE` | Bearer token, at least 32 characters. `RB_TOKEN_FILE` reads it from a file (for example a Docker secret), so it is not visible in the container settings. If neither is set, the server reads `<RB_DATA_PATH>/token`, or creates it (mode 600) with a random 64-character token on first start and logs the token once. Placeholders and short tokens are refused. |
| `RB_ALLOWED_ORIGINS` | Optional comma list of allowed `Origin` headers [empty]. |
| `RB_ALLOWED_HOSTS` | Comma list of host names the server answers to. Requests with another `Host` header are refused (DNS-rebinding protection). [`localhost,127.0.0.1`] **Must include the public tunnel host name**, because the tunnel forwards it; the quick start says so prominently. |
| `RB_VAULT_PATH` | Vault root inside the container [`/vault`]. |
| `RB_DATA_PATH` | Index and state directory [`/data`]. |
| `RB_HOST` / `RB_PORT` | Bind address and port [`0.0.0.0` / `8080`]. |
| `RB_SCAN_INTERVAL_SECONDS` | How often the index checks for changed files [`60`]. |
| `RB_TRASH_DIR` | Trash folder name at the vault root [`.rb-trash`]. |
| `RB_TRASH_RETENTION_DAYS` | Days before trashed items are purged [`90`]. `0` disables purging. |
| `RB_TEXT_EXTENSIONS` | Files treated as notes (read, search, edit) [`.md,.txt`]. |
| `RB_EXCLUDE` | Extra glob patterns hidden from all tools [empty]. Always hidden: dot files and folders, `@eaDir`, `#recycle`, `#snapshot`. |
| `RB_MAX_FILE_BYTES` | Largest note that is indexed, read or produced by a write [`2097152`]. |
| `RB_MAX_REQUEST_BYTES` | Largest HTTP request body [`4194304`]. |
| `RB_MIN_FREE_BYTES` | Writes are refused when free space on the vault drops below this [`104857600`]. |
| `RB_MAX_RESPONSE_CHARS` | Upper bound for one tool response [`60000`]. |
| `RB_REQUIRE_HASH_FOR_EDITS` | If `true`, `edit_note` requires `expected_sha256` [`true`]. `replace_note` always requires it. |
| `RB_LOG_LEVEL` | [`info`]. Logs never contain note contents. |

## 5. Security

- **Authentication:** every MCP request needs `Authorization: Bearer <token>`,
  compared in constant time. `/health` is unauthenticated and returns only `ok`.
- **Host check:** requests whose `Host` header is not in `RB_ALLOWED_HOSTS` are refused. `/health` is exempt so the container healthcheck works on localhost.
- **Origin check:** requests with an `Origin` header are refused unless it is listed in
  `RB_ALLOWED_ORIGINS` (empty by default; server-side clients send no `Origin`).
- **Transport:** stateless Streamable HTTP with JSON responses. `GET /mcp` returns 405
  (no server-initiated stream).
- **Frontmatter parsing:** YAML aliases are refused; frontmatter over 64 KB, deeper than
  32 levels or with more than 10,000 items is ignored. A note that cannot be indexed is
  listed as skipped and never stops the scan or the server.
- **Second layer (optional, outside the server):** Cloudflare Access with a service
  token in front of the tunnel, so requests without the Access headers never reach
  the NAS. Requires that the MCP client can send custom headers.
- **Path confinement:** tool paths are relative POSIX paths. Absolute paths, `..`
  segments, empty segments and NUL bytes are rejected. Paths are normalised to
  Unicode NFC. Every path is resolved and must stay inside the vault root.
- **Symlinks are never followed.** Not by tools, not by the scanner, not by
  recursive listing, not by the trash or purge. A symlink inside the vault is
  ignored and reported in `get_status` as skipped.
- **Hidden areas:** always-hidden names and `RB_EXCLUDE` matches are invisible to
  all tools, cannot be written, and are never indexed. The trash folder is only
  reachable through the trash tools.
- **Size and disk limits:** request bodies are capped by `RB_MAX_REQUEST_BYTES`;
  no write may produce a file larger than `RB_MAX_FILE_BYTES`; writes are refused
  below `RB_MIN_FREE_BYTES` free space.
- **Search input** is never passed to FTS5 as raw syntax (see §6).
- **No hard delete by the agent:** no tool deletes files. The only deletion is the
  server's purge of expired trash entries (§6).
- **Logging:** tool name, path, duration and result status only.

## 6. Tools

All tools return JSON. Paths are relative to the vault root. Every response that can
be cut short carries `complete: true|false`.

Two kinds of files exist: **notes** (extensions in `RB_TEXT_EXTENSIONS`) and
**other files** (images, PDFs and so on). `list_folder`, `move_note`,
`move_to_trash` and `restore_from_trash` work on any file or folder. Reading,
searching and writing tools work on notes only and return `not_a_note` otherwise.

### Reading and finding

| Tool | Parameters | Returns |
|---|---|---|
| `get_status` | – | note count, index age, last scan, skipped files with reasons, trash item count, version. |
| `list_folder` | `path="."`, `recursive=false`, `limit=500` | entries with `path`, `type` (`note`, `file`, `folder`), `size`, `modified`; `complete`. |
| `read_note` | `path`, `start_line=1`, `line_count=400` (max 2000) | `content` (requested lines), `start_line`, `end_line`, `total_lines`, `size`, `modified`, `sha256` of the whole file, `complete` (true only if the whole file was returned). |
| `search` | `query`, `path_prefix=None`, `limit=20` (max 100), `mode="words"` or `"substring"` | hits with `path`, `title`, `snippet`, `line` (first matching line), `score`; `index_age_seconds`; `complete`. |
| `resolve_link` | `link`, for example `Project Alpha` or `[[Project Alpha#Status]]` | matching note paths; `ambiguous: true` if more than one. |
| `get_backlinks` | `path` | notes that link to it, with the linking line. |
| `find_by_frontmatter` | `filters` (field → value; a list field matches if it contains the value; `[[X]]` and `X` are equal), `path_prefix=None`, `limit=100` | `path`, `title` and the filtered fields. |

Search details:

- **Query handling:** the server splits the query into terms itself and quotes each
  term before building the FTS5 expression. Supported: plain words (all must match),
  `"quoted phrases"`, and a trailing `*` for prefix search. Anything else is treated
  as literal text. An empty or unusable query returns `invalid_argument`.
- `words` mode uses an FTS5 table with the `unicode61` tokenizer (diacritics folded)
  and BM25 ranking.
- `substring` mode uses a trigram FTS5 table and finds parts of words (for example
  `book` inside `Notebook`). Terms need at least 3 characters.
- Indexed fields: path, title (first H1, else file name), frontmatter text, body.

Wikilink rules (used by `resolve_link`, `get_backlinks` and the index):

- Recognised forms: `[[Name]]`, `[[Name|alias]]`, `[[Name#Heading]]`,
  `[[Name#^block]]`, `[[folder/Name]]` and embeds `![[Name]]`.
- A link matches a note whose file name without extension equals `Name`,
  compared case-insensitively after NFC normalisation. `[[folder/Name]]` matches by
  path suffix. More than one match is reported as ambiguous, never guessed.

### Writing

| Tool | Parameters | Behaviour |
|---|---|---|
| `create_note` | `path`, `content`, `create_folders=true` | Fails with `already_exists` if the file exists. |
| `edit_note` | `path`, `old_text`, `new_text`, `expected_sha256` (required by default) | `old_text` must occur exactly once (overlapping matches counted). Returns the new `sha256`. |
| `append_to_note` | `path`, `text`, `expected_sha256=None` | Appends; adds a newline first if the file does not end with one. Appending never removes content, so the hash is optional. |
| `replace_note` | `path`, `content`, `expected_sha256` (required) | Whole-file rewrite for restructuring. |
| `move_note` | `path`, `new_path`, `create_folders=false`, `update_links=true` | Any file or folder. Fails if the target exists, if the target folder is missing and `create_folders` is false, or if a folder would be moved into itself. With `update_links`, wikilinks to every moved file are rewritten (see below). |
| `edit_section` | `path`, `heading`, `content`, `mode="replace"` / `"append"` / `"prepend"`, `expected_sha256` (required for `replace` by default) | Edits the body under one ATX heading. `heading` is a title (`Status`) or includes the level (`## Status`); matching is case-insensitive, headings in code blocks are ignored. The section ends at the next heading of the same or a higher level, so subsections belong to it. No match: `no_match`; several: `ambiguous_match` with line numbers. `replace` keeps one blank line before the next heading. Headings inside the frontmatter, fenced code blocks (CommonMark fence rules) are ignored; ATX headings may be indented up to 3 spaces. Line endings of the file are kept. |

Write rules:

- Content is UTF-8. Text is written exactly as given.
- A note that is skipped by the index (not valid UTF-8, or larger than
  `RB_MAX_FILE_BYTES`) cannot be edited: `not_a_note` or `too_large`.
- If `create_note` or `move_note` produces a file name (without extension) that
  already exists elsewhere in the vault, the write succeeds but the response carries
  a `duplicate_name` warning, because wikilinks to that name become ambiguous.
- **Line endings in `edit_note`:** `old_text` is first matched as given. If that finds
  nothing and the file uses CRLF line endings, `old_text` and `new_text` are
  converted to CRLF and matched again. The file's line endings are never changed.
- Every write goes to a temporary file in the same folder (a hidden dot file), is
  flushed, then renamed over the target, so sync tools never see half-written files.
  The original file mode is copied to the new file. Leftover temporary files are
  hidden from all tools and removed at start.
- "Fails if the target exists" is enforced atomically (no-replace rename, for example
  `renameat2(RENAME_NOREPLACE)` or link plus unlink), so a file created by a sync
  tool at the same moment is never overwritten. This applies to `create_note`,
  `move_note` and `restore_from_trash`.
- If `expected_sha256` is given and differs from the current file, the write is
  refused with `conflict`, including the current hash.
- After a write the index entry for that file is updated immediately.

Link rewriting on move:

- Before planning, the server rescans the vault so links written by other apps are included.
- Rule: every link keeps pointing at the file it pointed at before the move. Each link in an
  affected note is resolved against all visible files before and after the move (path links:
  exact path first, then a unique path suffix; bare links: a unique file name – the same rules
  as `resolve_link`). If the result changed, the link is replaced by the shortest form in the
  original style that resolves to the original target again; if none exists, it is left
  unchanged and reported.
- A move with `update_links` is refused if the new path cannot be written into a wikilink:
  `[ ] | # ^ \` and backtick, control characters, or spaces at the start or end of a name.
- Links inside frontmatter are only rewritten if the frontmatter can be read and the new link
  text consists of letters, digits, spaces and `- _ . / ( ) +` (no YAML quoting needed);
  otherwise they stay unchanged and are reported. Links in the body follow the normal rules.
- Before writing, each rewritten note is checked: the number of links must not change, the
  frontmatter must parse to the same structure (only link targets may differ), and every new link
  target in the parsed frontmatter must be exactly a target the server wrote. Otherwise the note is left unchanged and
  reported (for example a name with quotes inside a quoted frontmatter value).
- A link that pointed nowhere before may point at the moved file afterwards (as in Obsidian).
- A folder move or a restore is refused (`hidden_path`) if it would change which files are
  hidden by `RB_EXCLUDE` path patterns.
- Notes the index skipped (too large, not UTF-8) are listed as not checked.
- Applies to notes and other files (e.g. `![[pic.png]]`), and to links anywhere in a note,
  including frontmatter, but not inside fenced code blocks or inline code. Alias, heading/block part and embed marker
  are kept.
- Bare links (`[[Name]]`) change only if the file name changes. If the new name already exists
  elsewhere, the link gets the full path (`[[Folder/Name]]`) to stay unambiguous.
- Path links (`[[Folder/Name]]`) are rewritten to the new path.
- Ambiguous links are left unchanged and listed in `links_not_updated`, as are notes that could
  not be updated (too large, not UTF-8, write failure). The move itself is never undone.
- The response lists every changed note with the number of links changed.

### Trash

| Tool | Parameters | Behaviour |
|---|---|---|
| `move_to_trash` | `path` | Moves a file or folder into the trash. Returns `trash_id` and `expires_at`. |
| `list_trash` | `limit=200` | `trash_id`, `original_path`, `trashed_at`, `expires_at`. |
| `restore_from_trash` | `trash_id`, `target_path=None` | Restores to the original path or to `target_path`. Missing parent folders are recreated. Fails if the target exists. |

Trash layout and rules:

- Each entry is a folder `<RB_TRASH_DIR>/<id>/` containing `meta.json` (original
  path, trashed time) and `files/<original path>` with the trashed content. File
  names inside `files/` can never collide with `meta.json`.
- `<id>` is a UTC timestamp plus a random suffix, for example
  `20261029T141500Z-3f9a1c`.
- `original_path` from `meta.json` is treated as untrusted input and passes the same
  path checks as tool parameters before a restore.
- The purge job runs hourly. It only removes entries whose folder name is a valid
  `<id>` and whose `meta.json` is valid and has a `trashed_at` value older than the
  retention period.
  Anything else in the trash folder is left alone. Purging removes symlinks as
  links and never follows them.
- Trashed files are never indexed.

## 7. Index and concurrency

- SQLite database in `RB_DATA_PATH`, rebuilt from scratch if missing or if its
  schema version changes.
- Full scan at start; afterwards a scan every `RB_SCAN_INTERVAL_SECONDS` compares
  modification time and size to find new, changed and removed notes.
- Tables: notes (path, title, size, mtime, sha256), FTS5 words, FTS5 trigram,
  links (source, target text, line), frontmatter (path, field, value).
- Files larger than `RB_MAX_FILE_BYTES` or not valid UTF-8 are listed in
  `get_status` as skipped and are not searchable.
- **Concurrency:** the server is a single process. All writes, trash operations,
  purges and index updates run under one write lock. The scanner reads files
  without the lock, then takes it and only stores an entry if the file's
  modification time and size still match what it read. A slow scan therefore never
  overwrites a newer entry written by the server.
- Known limitation: change detection uses modification time and size. An external
  edit that keeps the size and lands in the same timestamp tick is picked up only
  with the next change. Writes through the server are always indexed at once.

## 8. Responses and errors

- Responses stay under `RB_MAX_RESPONSE_CHARS`. When a list or text is cut,
  `complete` is `false` and the response says how to fetch the rest (next
  `start_line`, or a narrower query).
- Error codes: `not_found`, `already_exists`, `outside_vault`, `hidden_path`,
  `not_a_note`, `conflict`, `ambiguous_match`, `no_match`, `too_large`,
  `disk_full`, `invalid_argument`, `unauthorized`.

## 9. Technology

- Python 3.12, the official MCP Python SDK (Streamable HTTP transport), SQLite with
  FTS5 from the standard library, PyYAML (safe loader) for frontmatter.
- No other runtime services and no outbound network access at runtime.
- Tests with pytest against a temporary vault, plus an end-to-end test through an
  MCP client over HTTP.

## 10. Documentation (for the public repository)

- `README.md`: what it is, why, quick start with Docker Compose, all settings,
  security notes, Synology Container Manager guide, Cloudflare Tunnel guide,
  tool reference.
- `compose.yaml`, `compose.cloudflare.yaml` and `.env.example` with placeholders only.
- No personal paths, domains, names or tokens anywhere in the repository.
- License: MIT.

## 11. Notes

MCP hubs such as MCPHub can send custom headers to Streamable HTTP servers
(`headers` field), so both the bearer token and Cloudflare Access service-token
headers work.
