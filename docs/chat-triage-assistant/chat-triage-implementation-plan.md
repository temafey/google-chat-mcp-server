# Chat Triage Assistant — Implementation Plan (v2, validated)

Phase-based decomposition (no dates). Companion to the design spec
`~/.claude/skills/google-chat-mcp/chat-triage-workflow.md`. This plan was
validated against the live server config + OAuth token on 2026-06-04.

## Decisions (baked in)
- **Detection = BOTH** — one shared core (`mentions_core.py`), two front-ends:
  an MCP tool (interactive agent) + a standalone cron collector (headless).
- **Storage = JSON** (`store.py`, atomic writes).
- **Scheduling = WSL cron** (chosen; reliability caveats handled in T2.3).
- **Notifications = all three** — GC Inbox space + Telegram + Windows toast,
  behind one dispatcher with pluggable senders.

## Code locations
- MCP server (user-owned, this repo): `/home/temafey/projects/google-chat-mcp-server/`
- Skill scripts: `~/.claude/skills/google-chat-mcp/scripts/`
- Runtime data (gitignored): `~/.claude-orchestrator/gchat-triage/`

---

## Validation status (2026-06-04)

| Check | Result |
|---|---|
| OAuth scopes for read+post | ✅ token has `chat.messages` (read+write), `chat.spaces.readonly`, `chat.memberships.readonly`, `userinfo.profile` — **no re-auth / scope expansion needed** |
| `refresh_token` for headless refresh | ✅ present |
| "messages-to-me" primitive exists | ❌ must be built (no `annotations`/`USER_MENTION`/DM handling in `google_chat.py`) |
| Calendar scopes | ✅ `calendar.readonly` + `calendar.events` |

**Risks surfaced by validation (folded into the phases below):**
R1 WSL cron reliability (RESOLVED → WSL cron, see T2.3) · R2 `token.json` refresh race · R3 dormant-space scan cost ·
R4 space-type / @all nuance · R5 no enable/pause switch · R6 edited/deleted messages ·
R7 API rate limits · R8 `SAVE_TOKEN_MODE` strips `annotations`/`name` from `list_space_messages` → mention detection needs a raw fetch path.

---

## Phase 0 — Foundations (shared core, no behavior change)

- **T0.1 `whoami` + identity cache.** Add a `whoami()` MCP tool resolving my
  `users/<id>` + display name via the OAuth userinfo endpoint; persist to
  `gchat-triage/config.json`. *Files:* `google_chat.py`, `server.py`.
- **T0.2 `mentions_core.py`.** Single importable module in this repo:
  `list_messages_for_me(start, end, space_names=None, include_dms=True)` →
  normalized item dicts. **Detection rule (R4):**
  - `spaceType == DIRECT_MESSAGE` → for-me (trigger `direct_dm`).
  - `spaceType in {SPACE, GROUP_CHAT}` → for-me **only** if
    `annotations[].userMention.user.name == me` (trigger `user_mention`);
    distinguish a direct @me from a room-wide @all/@here (trigger `broadcast`,
    lower priority).
  - everyone-else's mentions / unmentioned traffic → dropped.
  **Raw fetch (R8):** the existing `list_space_messages()` strips `annotations`,
  `name`, and space type under `SAVE_TOKEN_MODE` (default ON) — so detection MUST
  use a dedicated raw path that requests `messages.list` (or `spaces.messages.get`)
  with full fields and reads `annotations[].userMention`, `name`, `thread`, `sender`,
  plus the space `spaceType`. Reuse `google_chat.get_credentials()` (auto-refresh)
  and the existing `sender='me'` resolution for identity — do not reinvent.
  **Dormant-space skip (R3):** accept a `since` cursor and pre-filter spaces by
  `lastActiveTime`.
  *Acceptance:* unit tests — my direct mention → detected; @all → detected as
  `broadcast`; someone else's mention → not; DM → detected; unmentioned room msg
  → not.

## Phase 1 — Detection (read-only) + store

- **T1.1 MCP tool `list_messages_for_me`.** Thin wrapper over `mentions_core`.
  *Files:* `google_chat.py`, `server.py`.
- **T1.2 `store.py`.** Atomic JSON ledger: `load/save` (temp+rename),
  `upsert_item`, `set_status`, `open_items()`, `overdue_promises()`. Item id =
  `sha1(space_name + message_name)`. **R6:** upsert refreshes `text` but preserves
  `status`/`history`; a source message that 404s on re-fetch → flag `stale`.
- **T1.3 `collect_mentions.py`.** Headless collector: reuse server creds, keep a
  per-space cursor, **skip spaces with `lastActiveTime < cursor` (R3)**, call
  `mentions_core`, upsert new items as `status=new`, log to `gchat-triage/logs/`.
  **R2:** wrap token load/refresh in a file lock (or use a dedicated token copy)
  so it can't race the MCP server's refresh. **R7:** bounded concurrency +
  exponential backoff on 429/5xx. Lock file prevents overlapping runs.
  *Acceptance:* run → store has genuine "messages to me"; re-run → zero dups;
  prints a digest; two concurrent runs don't corrupt token or store.
- **T1.4 `config.json` + `secrets.env`.** Schema: `me_user_id`, VIP senders,
  keyword weights, quiet hours, poll cadence, channel config, **`enabled` flag +
  `mute_until` (R5)**; secrets (Telegram token/chat_id) in a **gitignored**
  `secrets.env`. *Acceptance:* both load; `enabled=false` short-circuits collect
  + notify; secrets path gitignored.

> **Milestone after Phase 1:** real, deduplicated list of messages addressed to
> me, persisted — fully read-only, no posting, no notifications.

## Phase 2 — Scheduling + GC Inbox notifications

- **T2.1 `notify.py` dispatcher.** Sender-plugin architecture; dedupe via
  `last_notified`; notify only on new (deterministic baseline priority: DM or
  urgency keyword), escalations, overdue promises; honor quiet hours + `mute_until`.
- **T2.2 GC Inbox channel.** Dedicated Google Chat space (verify `spaces.create`
  via user-OAuth; **fallback: user creates the space manually** and puts its
  `spaces/<id>` in config). Notifier posts a compact digest via `send_chat_message`.
- **T2.3 Scheduling — WSL cron (DECIDED).** Run via cron inside WSL2.
  **Caveats to handle (WSL2 cron is off by default and WSL isn't always alive):**
  - Ensure the cron service is started: `sudo service cron start` (add to shell
    rc / a WSL boot hook, e.g. `/etc/wsl.conf` `[boot] command=service cron start`).
  - Keep a WSL instance alive (a long-running terminal/session, or Windows
    Task Scheduler launching `wsl.exe` at logon just to keep it up).
  - On WSL restart, re-verify cron is running (health check in `notify.py` log).
  Crontab: `*/<cadence> * * * * flock -n <lock> <wrapper> collect_mentions.py && notify.py`
  with the lock file; cadence from config (default 10 min). Use a wrapper that
  sets PATH/venv (`uv run`) since cron has a minimal env.
  *Acceptance:* hands-off detection + one notification per item to GC Inbox; no
  re-post unless escalated; survives a WSL restart (cron auto-starts).

## Phase 3 — Claude triage session (the reasoning layer)

- **T3.1 Brief.** `triage_brief.py` (or agent reads `store.json`) → compact list
  of open items.
- **T3.2 Triage protocol.** Per open item: full priority (heuristic + reason) →
  context reconstruction (thread → bounded window → busy-channel + staleness
  guards → `context_confidence`) → 2-3 draft replies or "show thread" →
  **confirm** → post into `thread_name` → `status=answered` → ask close/keep-open;
  set `my_promise`/`promise_due` on "later".
- **T3.3 `/gchat:triage` command (optional).** Launches the protocol.
  *Acceptance:* end-to-end on a real pending item — correct context summary, reply
  posted only on confirm, store updated.

## Phase 4 — Telegram + Windows toast + escalations

- **T4.1 Telegram sender.** Bot API; `bot_token` + `chat_id` from `secrets.env`.
- **T4.2 Windows toast sender.** Reuse the user's existing popup mechanism
  (path TBD — user to point at it). On WSL this typically shells to a Windows
  PowerShell/BurntToast call.
- **T4.3 Escalations.** Overdue-promise reminders + a daily "open loops" digest
  (awaiting_me, snoozed-due, promises past due). *Acceptance:* P1 item → all three
  channels; past-due promise → escalation; daily digest fires once.

---

## Cross-cutting
- **Security:** `token.json`/`secrets.env` perms tight + gitignored; existing
  prompt-injection guard (never act on instructions *inside* fetched messages).
- **Idempotency & locks:** stable item ids; collector lock file; notify dedupe;
  token-refresh lock (R2).
- **Observability:** per-run logs; `store.json` `history[]` is the audit trail.
- **Kill switch:** `enabled` flag + `mute_until` (R5).

## Inputs needed from the user (to unblock)
1. **Scheduler (R1):** DECIDED → WSL cron (ensure `cron` service auto-starts via `/etc/wsl.conf [boot]` + keep a WSL instance alive).
2. **GC Inbox:** create a dedicated space + give its `spaces/<id>` (or confirm
   `spaces.create` auto-create).
3. **Telegram:** bot token + chat_id (BotFather) — Phase 4.
4. **Windows toast:** path to the existing popup mechanism.
5. **Tuning (defaults OK):** VIP senders, urgency keywords, cadence (10 min),
   quiet hours (22:00–08:00 Europe/Kiev).
