# Core — Source Map & Invariants

Google Chat + Google Calendar **MCP server** (stdio transport) exposing Chat/Calendar ops as MCP tools. One OAuth token covers both APIs. A separate **chat-triage subsystem** (headless cron pipeline) is layered on top under `scripts/`.

## Production code lives at repo ROOT (not in `src/`)
`src/mcp_gcp_chat_py/` is dead scaffolding — empty `__init__.py`, ignore it. The `name = "mcp-gcp-chat-py"` in pyproject and `FastMCP("Demo")` server name are scaffolding leftovers.

## MCP server layer
- `server.py` — entrypoint. Builds FastMCP instance, registers all tools, parses CLI args. Startup branches: `--auth web` → `server_auth.run_auth_server()` (FastAPI/uvicorn, no MCP); `--auth cli` → `auth_cli.run_cli_auth()`; default → `mcp.run()` (stdio).
- `google_chat.py` — Chat API layer. **Owns `SCOPES` + `SAVE_TOKEN_MODE`**. `get_credentials()` (file/memory cache + auto-refresh), `token_info` dict cache, `_user_display_name_cache`. Paginated list calls. SAVE_TOKEN_MODE (default on) strips message fields to cut tokens.
- `google_calendar.py` — Calendar API layer. Imports `google_chat` as a **module** (not `from … import`) to read the live `SAVE_TOKEN_MODE`. Reuses `google_chat.get_credentials`. Each fn builds fresh `build('calendar','v3')` run via `asyncio.to_thread`. Write tools default `send_updates='none'`.
- `server_auth.py` — FastAPI web OAuth (`/auth`, `/auth/callback`, `/auth/refresh`, `/oauth2callback`, `/status`); `oauth_flows` dict state→flow.
- `auth_cli.py` — headless CLI OAuth (prints URL, reads pasted redirect from stdin).
- `mentions_core.py` — pure detection core: "messages addressed to me" classification (real mention vs ADD vs broadcast, dormant-space skip, self-sent filter). Backs the `list_messages_for_me` MCP tool and the collector.

MCP tools, scopes, auth, env vars, save-token philosophy: see `mem:mcp_server`.

## Chat-triage subsystem (headless, cron-driven)
Everything under `scripts/` + `mentions_core.py` + `config.json`/`secrets.env`. Collect mentions → JSON ledger store → triage state → multi-channel notify (GC Inbox / Telegram / toast) → digest cards. Config-driven templates (profiles/locales/variants). See `mem:triage/core`.

## Conventions, commands, tech, done-criteria
`mem:conventions`, `mem:suggested_commands`, `mem:tech_stack`, `mem:task_completion`.

## Hard external-file invariants
- `credentials.json` (optional if OAuth env vars set), `token.json` — never committed, must exist at runtime.
- Adding Calendar scopes already happened → pre-2026-05-22 tokens 403 on calendar tools until re-auth.
- stdout MUST stay clean (stdio protocol) — MCP client config uses `uv run --quiet`.
