# Task Completion Checklist

No configured linter/formatter/type-checker in pyproject (no ruff/black/mypy config present). So:

1. **Run tests:** `uv run pytest` (or the relevant `tests/test_*.py`). ~266 test fns, async auto-mode. Add/extend tests for new behavior — this repo expects test coverage (16 test files mirror the triage modules: store, config, notify, notify_card, notify_telegram, templates, search, write, members, directory_resolution, mentions_core, collect_mentions, triage_cli, triage_session, token_lock).
2. **stdout cleanliness:** if you touched MCP-server code paths, confirm nothing prints to stdout outside the MCP protocol (breaks stdio transport). Use file logging.
3. **Scope/credential changes:** if SCOPES changed, every existing `token.json` needs re-auth (`uv run python server.py --auth cli`) and the GCP Data Access tab must list the new scopes.
4. **Git:** LOCAL ONLY. Commit + merge into local `main`. NEVER push/PR/remote. NO `Co-Authored-By: Claude` trailer (global CLAUDE.md). Conventional Commits with DDD scope (e.g. `feat(triage): …`).
5. **Triage build status:** if working the triage subsystem, the source-of-truth task journal is `docs/chat-triage-assistant/CHAT-TRIAGE-BUILD-STATUS.md` (todo→dispatched→built→verified|blocked). A task is DONE only when `verified`.
