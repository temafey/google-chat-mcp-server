# Conventions

## Module-level patterns
- **Live-flag reads across modules:** import the owning module, not the symbol — `import google_chat` then read `google_chat.SAVE_TOKEN_MODE`, never `from google_chat import SAVE_TOKEN_MODE` (would snapshot at import). Same applies to any mutable module global.
- **`SCOPES` is owned by `google_chat.py`.** NEVER pass `SCOPES` to `Credentials.from_authorized_user_file` — doing so makes later SCOPES expansion break refresh for ALL tools (known footgun). When expanding SCOPES also add them in the GCP Data Access tab or they're silently dropped from issued tokens.
- **SAVE_TOKEN_MODE philosophy:** trim API payloads to essential fields to save LLM tokens. Chat messages → `{sender, createTime, text, thread}`. Calendar events → fixed whitelist; attendees trimmed to `{email, displayName, responseStatus, optional}`.
- Calendar service objects: build a fresh `build('calendar','v3',creds)` per call, run via `asyncio.to_thread` (googleapiclient not documented thread-safe).
- All calendar WRITE tools default `send_updates='none'` (no accidental invite spam).

## Naming / style
- Module-private helpers prefixed `_` (e.g. `_esc`, `_classify`, `_fetch_with_backoff`, `_now_dt`). Heavy use.
- Module-level CONSTANTS in UPPER_SNAKE near top (`DEFAULT_LOOKBACK_HOURS`, `STORE_VERSION`, `VALID_STATUSES`).
- Time: store/compare as timezone-AWARE UTC; helpers `_to_iso`/`_iso_z`/`_coerce_aware_dt`/`_to_aware_utc`. ISO-8601 `Z` strings on the wire.
- stdio purity: triage scripts log to files (`_make_logger`, LOGS_DIRNAME), never pollute stdout when feeding MCP.

## Security-sensitive: triage notify escaping
`notify._esc` / `_bold` / `_GCHAT_DEFANG` guard Google-Chat formatting injection. Before touching any escaping helper or template placeholder, run `find_referencing_symbols` first (CLAUDE.md mandates this). Telegram token must never be logged (token-leak-safe dispatch).

## Editing
Symbol-based edits via Serena preferred. Triage state modules (`store.py`, `triage_session.py`) are PURE (no network); `triage_cli.py post` + `notify.py` senders are the only net writers.
