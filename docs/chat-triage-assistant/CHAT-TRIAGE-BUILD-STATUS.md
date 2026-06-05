# Chat Triage Assistant — Build Status Journal

Single source of truth for task state. Orchestrator-owned.
Plan: chat-triage-implementation-plan.md
Spec: ~/.claude/skills/google-chat-mcp/chat-triage-workflow.md
Runbook (follow-ups): ~/.claude-orchestrator/gchat-triage/DEBUG-GUIDE.md
How it works (runtime): HOW-IT-WORKS.md
Status: todo -> dispatched -> built -> verified | blocked.
A task is DONE only when `verified` (independent verifier PASS + gate demo where applicable).

## Tasks

| Task | Wave | Status | Notes |
|---|---|---|---|
| T0.1 whoami + identity cache | A | verified | `_resolve_me_sync` -> `users/117216798078927891621`; me_display_name "Artem Onyshchenko" merged into config.json |
| T0.2 mentions_core.py | A | verified | Detection core; R8 raw-fetch; T1.5 self-sent filter added (drops messages I authored) |
| T1.2 store.py | A | verified | scripts/store.py; atomic JSON ledger; `.items` keyed by message-id (dedup) |
| T1.4 config + secrets | A | verified | scripts/config.py loader; config.json + secrets.env (chmod 600) live |
| T1.1 list_messages_for_me (MCP tool) | B | verified | Thin wrapper over mentions_core; google_chat.py + server.py |
| T1.3 collect_mentions.py | B | verified | Headless collector; R2 token lock, R3 dormant skip, R7 backoff, run-lock |
| T2.1 notify.py dispatcher | C | verified | Sender-plugin arch; dedupe via `last_notified`; quiet hours + mute; `--dry-run` |
| T2.2 GC Inbox channel | C | verified | Live space `spaces/AAQAH7kLhwc` "My Triage / Inbox" |
| T2.3 WSL cron wiring | C | verified | `*/5` flock-guarded `triage_cron.sh`; survives WSL restart via `/etc/wsl.conf`; installer `install_cron.sh` |
| T3.1 triage_brief | D | verified | Open-items digest from store.json (triage_session.triage_queue) |
| T3.2 triage protocol | D | built | triage_session.py (pure state) + triage_cli.py (`post` = sole net writer); skill `.claude/skills/chat-triage/SKILL.md`. NOT yet exercised on real data -> follow-up #2 |
| T3.3 /chat-triage command | D | verified | Skill invoked from a session opened in the repo |
| T4.1 Telegram sender | E | verified | Live bot `@expo_artem_chat_triage_bot`; token-leak-safe; `TELEGRAM_CHAT_ID=67273769` |
| T4.2 Windows toast sender | E | todo | NOT built; channel `enabled=false` |
| T4.3 escalations + daily digest | E | partial | Overdue-promise escalation fires (even in quiet hours); daily open-loops digest unverified |
| T1.5 self-sent filter | A | verified | Added 2026-06-04 G1; drops `sender == me` across all triggers. NOT in original plan |

## Gates

| Gate | After wave | State | Evidence |
|---|---|---|---|
| G1 read-only e2e | B | **PASSED** (2026-06-04) | Live collector -> store filled with messages-to-me; re-run -> 0 dups; identity resolves; no posting/notifications; digest shown. Self-sent leak found + fixed (T1.5), store rebuilt 124->71. |
| G2 hands-off notify | C | **PASSED** | notify.py live; GC Inbox + Telegram channels enabled; dedup via `last_notified` (78/82 items carry it -> notifications have fired); quiet-hours honored. Tests: test_notify, test_notify_telegram. |
| G3 multi-channel + escalation | E | **PARTIAL** | 2 of 3 channels live (GC Inbox + Telegram). Outstanding: windows_toast (T4.2) + full escalation/daily-digest (T4.3). |

> Gate numbering note: builder/runbook notes occasionally refer to "G1-G4" loosely;
> the canonical gate scheme defined here is G1-G3. G3 is the multi-channel gate and
> is only partially met (no Windows toast, escalation/daily-digest incomplete).

## Pending follow-ups (see DEBUG-GUIDE.md)

1. **Polish cron-fired notifications.** Root cause confirmed: all 82 store items have
   `sender_name` = raw `users/<id>` (display name never resolved) -> digests unreadable.
   Fix in collector seeding + `google_chat.get_user_display_name()` / `list_space_members` cache.
2. **Really test triage logic end-to-end.** All 82 items are `status=new`; the lifecycle
   (triaged / answered / snoozed / promise / closed) has never run on real data. Run
   `/chat-triage` through the confirmation gate on a real item.

## Known issues

- **Git integrity:** committed `scripts/triage_cli.py` (HEAD `fba732f`) imports `triage_session`,
  but `scripts/triage_session.py` was untracked -> a fresh HEAD checkout would `ImportError`.
  Resolved by the commit batch that tracks `triage_session.py` (+ tests). Re-verify with
  `git cat-file -e HEAD:scripts/triage_session.py`.
- `token.json.lock` was not gitignored -> added to `.gitignore`.

## Ground truth (verified 2026-06-05, re-confirm if touching)

- Runtime: `~/.claude-orchestrator/gchat-triage/` — config.json (enabled, cadence 5 min),
  secrets.env (chmod 600), store.json (82 items, all `status=new`), logs/.
- Code: `mentions_core.py` (root), `scripts/{collect_mentions,notify,store,config,triage_session,triage_cli}.py`,
  `scripts/{triage_cron,install_cron}.sh`. Skill: `.claude/skills/chat-triage/SKILL.md`.
- Channels: gc_inbox `spaces/AAQAH7kLhwc`; telegram bot live; windows_toast off.
- Full suite: **157 passed** (`PYTHONPATH=. uv run pytest -q`).
- Repo on `main`; *.env + token.json gitignored; token has read+write Chat + refresh_token.

## Canonical config.json schema

```json
{
  "version": 1,
  "me_user_id": "users/117216798078927891621",
  "me_display_name": "Artem Onyshchenko",
  "enabled": true,
  "mute_until": null,
  "poll_cadence_minutes": 5,
  "quiet_hours": { "start": "22:00", "end": "08:00", "tz": "Europe/Kiev" },
  "vip_senders": [],
  "urgency_keywords": ["urgent","blocker","prod","asap","deadline","eod"],
  "channels": {
    "gc_inbox": { "enabled": true, "space_name": "spaces/AAQAH7kLhwc" },
    "telegram": { "enabled": true },
    "windows_toast": { "enabled": false }
  },
  "spaces_allowlist": null,
  "spaces_blocklist": []
}
```

## Changelog
- 2026-06-04 — Journal created; all 15 tasks = todo; Wave A dispatched.
- 2026-06-04 — G1 PASSED on live account; T1.5 self-sent filter added; store rebuilt 124->71.
- 2026-06-05 — Phases 1-3 + cron complete; G2 PASSED, G3 PARTIAL; channels live (GC Inbox + Telegram);
  157 tests pass. Journal reconciled with verified runtime state. 2 follow-ups open (see DEBUG-GUIDE.md).
