# Chat Triage Assistant — Build Status Journal

Single source of truth for task state. Orchestrator-owned.
Plan: chat-triage-implementation-plan.md
Spec: ~/.claude/skills/google-chat-mcp/chat-triage-workflow.md
Status: todo → dispatched → built → verified | blocked.
A task is DONE only when `verified` (independent verifier PASS + gate demo where applicable).

## Tasks

| Task | Wave | Status | Depends-on | Builder result | Verifier verdict | Notes |
|---|---|---|---|---|---|---|
| T0.1 whoami + identity cache | A | todo | — | — | — | google_chat.py + server.py; persists me_user_id/me_display_name into config.json (schema owned by T1.4) |
| T0.2 mentions_core.py | A | todo | — | — | — | New module at repo root; R8 raw-fetch; reuses _resolve_me_sync + get_credentials |
| T1.2 store.py | A | todo | — | — | — | scripts/store.py; atomic JSON ledger; R6 stale-flag |
| T1.4 config + secrets | A | todo | — | — | — | scripts/config.py loader + config.json template + secrets.env template; R5 enabled/mute_until |
| T1.1 list_messages_for_me (MCP tool) | B | todo | T0.2 | — | — | Thin wrapper; google_chat.py + server.py |
| T1.3 collect_mentions.py | B | todo | T0.2, T1.2, T1.4 | — | — | Headless collector; R2 token lock, R3 dormant skip, R7 backoff, run-lock |
| T2.1 notify.py dispatcher | C | todo | G1 | — | — | Sender-plugin arch; dedupe last_notified; quiet hours + mute |
| T2.2 GC Inbox channel | C | todo | G1 | — | — | Dedicated GC space; USER INPUT needed (space id or confirm auto-create) |
| T2.3 WSL cron wiring | C | todo | T1.3, T2.1 | — | — | flock crontab; cron auto-start caveats |
| T3.1 triage_brief | D | todo | G2 | — | — | Compact open-items digest from store.json |
| T3.2 triage protocol | D | todo | G2 | — | — | Agentic reasoning protocol; confirm-before-post |
| T3.3 /gchat:triage command | D | todo | T3.2 | — | — | Optional command launcher |
| T4.1 Telegram sender | E | todo | G2 | — | — | USER INPUT needed (bot_token + chat_id) |
| T4.2 Windows toast sender | E | todo | G2 | — | — | USER INPUT needed (path to existing popup mechanism) |
| T4.3 escalations + daily digest | E | todo | T4.1, T4.2 | — | — | Overdue-promise reminders + open-loops digest |

## Gates

| Gate | After wave | State | Demo criteria |
|---|---|---|---|
| G1 read-only e2e | B | pending | Collector on live account → store.json filled with real messages-to-me; re-run → 0 dups; identity resolves; NO posting, NO notifications; digest shown to user |
| G2 hands-off notify | C | pending | Mention detection + one GC-Inbox notification per item; dedup verified; quiet-hours/mute honored |
| G3 multi-channel + escalation | E | pending | P1 item → all three channels; overdue promise → escalation; daily digest fires once |

## Open inputs (raise at the gate, don't block early)

- G2/T2.2: GC Inbox space spaces/<id> (or confirm spaces.create auto-create).
- Wave E/T4.1: Telegram bot_token + chat_id (BotFather).
- Wave E/T4.2: path to existing Windows-toast popup mechanism.
- Tuning (defaults OK): VIP senders, urgency keywords, cadence 10 min, quiet hours 22:00–08:00 Europe/Kiev.
- Scheduler: resolved → WSL cron.

## Ground truth (2026-06-04, re-confirm if touching)

- google_chat.py: get_credentials() L131 (auto-refresh), _resolve_me_sync(creds) L510 (→ users/<id> via userinfo; REUSE), list_space_messages() L262, SAVE_TOKEN_MODE=True L83 strips to {sender,createTime,text,thread} → R8 raw fetch required.
- server.py: tool pattern @mcp.tool() async def. No whoami yet.
- scripts/ and ~/.claude-orchestrator/gchat-triage/ do not exist yet — Wave A creates them.
- Repo on main; *.env + token.json gitignored; token.json present, read+write Chat scopes + refresh_token (no re-auth).

## Canonical config.json schema (T1.4 owns; T0.1 only merges me_user_id/me_display_name)

```json
{
  "version": 1,
  "me_user_id": null,
  "me_display_name": null,
  "enabled": true,
  "mute_until": null,
  "poll_cadence_minutes": 10,
  "quiet_hours": { "start": "22:00", "end": "08:00", "tz": "Europe/Kiev" },
  "vip_senders": [],
  "urgency_keywords": ["urgent","blocker","prod","asap","deadline","eod"],
  "channels": {
    "gc_inbox": { "enabled": false, "space_name": null },
    "telegram": { "enabled": false },
    "windows_toast": { "enabled": false }
  },
  "spaces_allowlist": null,
  "spaces_blocklist": []
}
```

## Changelog
- 2026-06-04 — Journal created; all 15 tasks = todo; Wave A dispatched.
