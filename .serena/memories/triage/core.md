# Triage Subsystem — Core

Headless cron pipeline that surfaces Google Chat messages addressed to the user, tracks their lifecycle, and dispatches digest notifications. Lives under `scripts/` + `mentions_core.py`; config in `config.json` (gitignored; `config.example.json` is the template) + `secrets.env` (chmod 600).

## Data flow
1. **Detect** — `mentions_core.py`: pure classifier. Real mention vs ADD-to-space vs broadcast (`_is_real_mention`, `_iter_user_mentions`, `_classify`), dormant-space skip (`_is_dormant`), self-sent filter (drops `sender == me`).
2. **Collect** — `scripts/collect_mentions.py`: headless collector. Token lock, dormant skip, exponential backoff (`_fetch_with_backoff`), run-lock (single instance), file logging. `--token-path`. Upserts into the store.
3. **Store** — `scripts/store.py`: atomic JSON ledger keyed by message-id (dedup). `STORE_VERSION`, `VALID_STATUSES`/`OPEN_STATUSES`. `upsert_item`, `set_status`, `mark_stale`, `open_items`, `overdue_promises`. PURE (no network).
4. **Triage state** — `scripts/triage_session.py`: PURE state machine over the store. `triage_queue` (priority tiers + sort), `set_triage`, `snooze`, `record_promise`, `record_response`, `pin`/`unpin`, `close_item`, `ignore_item`. Lifecycle doc: `docs/chat-triage-assistant/TRIAGE-LIFECYCLE.md`.
5. **Notify** — `scripts/notify.py`: dispatcher with Sender-plugin arch (`Sender`/`ConsoleSender`/`GCInboxSender`/`TelegramSender`). Dedup via `last_notified`, quiet hours + mute. `--dry-run`. `build_digest`/`render_card`. **Security: `_esc`/`_bold`/`_GCHAT_DEFANG` guard Chat formatting injection — run `find_referencing_symbols` before editing.**
6. **CLI** — `scripts/triage_cli.py`: human/skill entrypoint. `post` is the SOLE network writer; other subcommands mutate local state only.

## Config-driven digest templates
`scripts/templates.py` + `scripts/config.py`. Layout = profiles / locales / variants, NOT hardcoded. Locales `en`/`ru`/`uk` (plural declensions, localized months/relative dates). Switch via `config.json` `templates.active_profile` / `templates.locale`. `notify.render_card` delegates to `templates.render`. Security via `notify._esc` unchanged across template changes.

## Channels (state as of build journal)
GC Inbox (`spaces/AAQAH7kLhwc` "My Triage / Inbox") + Telegram (`@expo_artem_chat_triage_bot`, `TELEGRAM_CHAT_ID=67273769`) LIVE. Windows toast NOT built (`enabled=false`). Escalation of overdue promises fires (even in quiet hours); full daily-digest unverified.

## Cron wiring
`*/5` flock-guarded `triage_cron.sh`; installer `install_cron.sh`; survives WSL restart via `/etc/wsl.conf`.

## Source of truth / docs
`docs/chat-triage-assistant/`: `CHAT-TRIAGE-BUILD-STATUS.md` (task journal — DONE only when `verified`), `HOW-IT-WORKS.md`, `TRIAGE-LIFECYCLE.md`, `CRON-SETUP.md`, `ORCHESTRATOR-PROMPT.md`, implementation-plan, `BACKLOG-ai-summary-research-adapters.md`. Skill: `.claude/skills/chat-triage/`.
