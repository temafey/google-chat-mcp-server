# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Google Chat + Google Calendar MCP (Model Context Protocol) server that exposes Chat and Calendar operations as MCP tools AI assistants can invoke. The MCP transport is **stdio**, so stdout must remain clean for protocol messages. One OAuth token covers both APIs.

## Code Navigation & Research (Serena MCP — PREFERRED)

**For understanding or navigating this codebase, prefer the `serena` MCP tools over plain `Grep`/`Read`/`Glob`.** Serena is a language-server-backed semantic toolkit (configured in the gitignored `.mcp.json`, project rooted at this repo). It returns *symbols and their relationships* instead of raw text matches, which is faster, more precise, and far more token-efficient than reading whole files. Plain `Read`/`Grep` waste context by pulling in entire files when you only needed one function.

### When to use Serena (default for code research)

- **Exploring an unfamiliar file** → `mcp__serena__get_symbols_overview` (the recommended FIRST call — returns the file's classes/functions/methods without reading the body).
- **Finding a definition** → `mcp__serena__find_symbol` (name-path search: exact, suffix, or `substring_matching`; pass `include_body: true` only when you need the source). Prefer this over grepping for `def name`.
- **Impact analysis before editing** → `mcp__serena__find_referencing_symbols` (who calls/uses a symbol). Use this BEFORE changing any shared function/class — e.g. before touching `notify._esc`, `get_credentials`, or a template placeholder.
- **Pattern/text search** → `mcp__serena__search_for_pattern` (regex across files, with context lines and glob include/exclude). Use instead of `Grep` when you also want surrounding context or code-only filtering.
- **Navigation** → `mcp__serena__list_dir`, `mcp__serena__find_file`.
- **Symbol-aware editing** → `mcp__serena__replace_symbol_body`, `insert_after_symbol`, `insert_before_symbol`, `replace_regex` (edit by symbol identity, not brittle line offsets).
- **Cross-session project knowledge** → memory tools `write_memory` / `read_memory` / `list_memories` / `edit_memory` / `delete_memory` (Serena's own file-backed store — distinct from this agent's `MEMORY.md`).

Recommended flow for a research task: `get_symbols_overview` → `find_symbol` (with body) → `find_referencing_symbols` to map impact, then edit. Reach for `find_symbol` over reading a whole module; reach for `find_referencing_symbols` over grepping for every call site.

### When NOT to use Serena

- The exact file + line is already known → just `Read` that slice.
- Non-code files (Markdown, JSON, `.env`, logs) → `Read` / `Grep` (Serena's strength is symbol semantics).
- Library/framework API questions → `context7` / `docfork` (see global instructions), not Serena.
- Serena's first call after a session start may be slow (it boots a language server / `uvx` may fetch on first run); that latency is one-time, not a reason to fall back to `Grep` for the rest of the session.

## Commands

```bash
# Run the MCP server (normal/stdio mode)
uv run server.py

# Authenticate — CLI headless flow (prints URL, reads pasted redirect URL from stdin)
uv run python server.py --auth cli

# Authenticate — web browser flow (FastAPI OAuth callback server)
uv run python server.py --auth web --port 8000

# Custom token path
uv run server.py --token-path /path/to/token.json

# Disable SAVE_TOKEN_MODE (strips message fields to reduce tokens; enabled by default)
uv run server.py --disable-token-saving

# Debug with FastMCP dev tooling
fastmcp dev server.py --with-editable .

# Build Docker image
docker build -t google-chat-mcp-server:latest .
```

There are no tests yet (`tests/__init__.py` is empty).

## Architecture

```
server.py          ← Entry point: creates FastMCP instance, registers tools, parses CLI args
  ├── google_chat.py     ← Google Chat API layer: credential management, paginated API calls. Owns SCOPES + SAVE_TOKEN_MODE.
  ├── google_calendar.py ← Google Calendar API layer (calendarList, events CRUD, freebusy). Reuses google_chat.get_credentials and reads google_chat.SAVE_TOKEN_MODE live.
  ├── server_auth.py     ← FastAPI web OAuth server (/auth, /auth/callback, /auth/refresh, /status)
  └── auth_cli.py        ← Headless CLI OAuth flow
```

**Startup branching in `server.py`:**
- `--auth web` → `run_auth_server()` (FastAPI/uvicorn only, no MCP)
- `--auth cli` → `run_cli_auth()` (OAuth flow only, no MCP)
- default → `mcp.run()` starts the FastMCP stdio server

**`google_chat.py` key patterns:**
- `token_info` dict is the in-memory credential cache (credentials + last_refresh + token_path)
- `get_credentials()` loads from file or memory and auto-refreshes expired tokens
- `_user_display_name_cache` is a module-level dict avoiding repeated People API calls per sender
- `list_space_messages()` uses paginated `chat.spaces().messages().list()` with ISO date range filter; when `SAVE_TOKEN_MODE=True` (default), messages are stripped to `{sender, createTime, text, thread}`

**`google_calendar.py` key patterns:**
- Imports `google_chat` as a module (not `from … import SAVE_TOKEN_MODE`) so it reads the live `SAVE_TOKEN_MODE` flag instead of an import-time snapshot
- Each public function builds a fresh `build('calendar', 'v3', creds)` and runs it via `asyncio.to_thread` — googleapiclient services aren't documented as thread-safe
- `_parse_date_or_datetime()` accepts both `YYYY-MM-DD` (auto-UTC) and full RFC3339; date-only with `end_of_day=True` produces an inclusive end-of-day timestamp
- `_filter_event()` keeps only `id, summary, description, location, start, end, status, htmlLink, recurrence, recurringEventId, attendees, hangoutLink` and trims attendees to `{email, displayName, responseStatus, optional}` — same SAVE_TOKEN_MODE philosophy as Chat messages
- All write tools default `send_updates='none'` so accidental calls don't spray invitation emails

**MCP tools (in `server.py`):**
- Chat: `get_chat_spaces`, `get_space_messages`, `search_chat_messages`, `find_users_by_name`, `list_space_members`, `send_chat_message`, `upload_chat_attachment`
- Calendar: `get_calendars`, `get_calendar_events`, `get_calendar_event`, `create_calendar_event`, `update_calendar_event`, `delete_calendar_event`, `quick_add_calendar_event`, `get_calendar_freebusy`
- Demo stubs from scaffolding (`add`, `fetch_weather`, `get_ip_my_address`) have been removed in earlier commits

**`server_auth.py`:** Uses `oauth_flows` dict (state → `InstalledAppFlow`) to track in-progress OAuth exchanges.

## Required External Files

These are never committed; must exist at runtime:
- `credentials.json` — Google OAuth client secrets downloaded from Cloud Console
- `token.json` — generated after first auth flow; path configurable via `--token-path`

**Google API scopes** (single OAuth flow covers both Chat and Calendar):
- Chat: `chat.spaces.readonly`, `chat.messages`, `chat.memberships.readonly`, `userinfo.profile`
- Calendar: `calendar.readonly` (calendarList + freebusy), `calendar.events` (events CRUD)

⚠ Adding Calendar scopes to an existing project means **every existing `token.json` must be re-issued** — Google does not extend a token's scope set without a fresh consent flow. Old tokens keep working for Chat tools but return 403 from every `get_calendar_*` / `*_calendar_event` call.

## MCP Client Configuration

```json
{
  "mcpServers": {
    "google_chat": {
      "command": "uv",
      "args": [
        "--directory", "<REPO_PATH>",
        "run", "--quiet", "server.py",
        "--token-path", "<REPO_PATH>/token.json"
      ]
    }
  }
}
```

The `--quiet` flag on `uv run` is **mandatory** — it suppresses uv's own output so stdout carries only MCP stdio protocol messages.

## Key Notes

- `pyproject.toml` pins `fastmcp>=0.4.1,<0.5.0`; `requirements.txt` pins `fastmcp>=2.13.1` — these are incompatible. `uv` uses `pyproject.toml`, so `requirements.txt` is stale and should be ignored.
- The `src/mcp_gcp_chat_py/` package is unused scaffolding; all production code lives at the repo root.
- The `FastMCP("Demo")` server name is a scaffolding placeholder.
- Docker CI (`.github/workflows/docker-publish.yml`) builds multi-arch (`linux/amd64`, `linux/arm64`) images and pushes to `ghcr.io` on pushes to `main` or `v*` tags.
