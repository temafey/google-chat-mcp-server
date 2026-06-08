# Suggested Commands

## MCP server
- Run (stdio):            `uv run server.py`
- Auth CLI (headless):    `uv run python server.py --auth cli`
- Auth web (FastAPI):     `uv run python server.py --auth web --port 8000`
- Custom token path:      `uv run server.py --token-path /path/to/token.json`
- Disable token-saving:   `uv run server.py --disable-token-saving`
- FastMCP dev tooling:    `fastmcp dev server.py --with-editable .`
- Docker build:           `docker build -t google-chat-mcp-server:latest .`

## Tests (they EXIST now — CLAUDE.md's "no tests yet" is stale)
- All:   `uv run pytest`
- One:   `uv run pytest tests/test_notify.py -q`
- 16 files / ~266 test fns under `tests/`. `asyncio_mode=auto`.

## Triage subsystem
- Collector (headless): `uv run python scripts/collect_mentions.py`
- Notify dispatch:      `uv run python scripts/notify.py [--dry-run]`
- Triage CLI:           `uv run python scripts/triage_cli.py <list|show|post|triage|snooze|promise|pin|unpin|close|ignore> …`
  - `post` is the SOLE network writer in the triage CLI.
- Backfill sender names: `uv run python scripts/backfill_sender_names.py`
- Cron install:         `scripts/install_cron.sh`  (wires `*/5` `triage_cron.sh`, flock-guarded)

## Notes
- Always `uv run` (project env). `--quiet` is mandatory only in the MCP client launch config (stdout cleanliness).
- Git: LOCAL ONLY — never push/PR/remote (see user auto-memory). Linux shell, standard GNU coreutils.
