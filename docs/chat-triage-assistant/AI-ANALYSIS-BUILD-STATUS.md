# AI Message-Analysis — Build Status Journal

Single source of truth for the **pluggable AI message-analysis stage** added to the
Chat Triage Assistant. Orchestrator-owned. Modeled on `CHAT-TRIAGE-BUILD-STATUS.md`.

- Design authority: `ORCHESTRATOR-PROMPT-AI-ANALYSIS.md` (DESIGN section D-1..D-10).
- Boundary parent: `BACKLOG-ai-summary-research-adapters.md` (this is the first
  implementation of its *analysis* half; research mode stays backlog).
- Runtime/lifecycle context: `HOW-IT-WORKS.md`, `TRIAGE-LIFECYCLE.md`.
- Status values: `todo -> dispatched -> built -> verified | blocked`.
- A task is DONE only when `verified` (independent verifier PASS + gate demo where applicable).

## Tasks

| Task | Wave | Status | Depends-on | Builder result | Verifier verdict | Notes |
|---|---|---|---|---|---|---|
| A1 `analysis_adapters.py` | A | **verified** | — | 45 tests; live `claude -p` envelope verified | PASS (no defects) | `AnalysisAdapter` ABC + `ClaudeAdapter`; argv/no-shell/timeout/bounded; sanitized env strips triage secrets keeps `ANTHROPIC_API_KEY`; `ok=False` never raises; cached probe. Envelope `{...,"result":"<text>",...}` → inner JSON. |
| A2 `analysis_prompts.py` | A | **verified** | — | 59 tests | PASS (3 noted: 2 LOW + 1 MEDIUM residual) | Pure CLASSIFY+SUMMARIZE builders; delimiter-forging neutralized (`</message>`→`< /message>`). RESIDUAL: space-insertion neutralization is model-dependent (tracked, revisit GATE D). Minor D-5 SUMMARIZE-header wording drift. |
| A3 store fields | A | **verified** | — | 3 tests; 20 in test_store | PASS (no defects) | Added `msg_type/analyzed_at/analyzed_by/thread_status` (default `None`) between `context_confidence` and `status`; `set_fields` untouched (no status/history change). |
| A4 config block | A | **verified** | — | 4 tests; 23 in test_config | PASS (no defects) | `analyze` block in `DEFAULT_CONFIG`; `_deep_merge` already recurses (no logic change); defaults OFF. Note: `_persistable` keeps `analyze` (user-state, persists on first load — intentional). |
| B1 `analyze_mentions.py` | B | todo | A1,A2,A3,A4 | — | — | Orchestrating script: gate on `analyze.enabled`; select `new & priority=="unset" & not analyzed`; Tier-0 pre-filter; Tier-1 classify; `set_fields` (NO status change); run-lock + logging; honor caps/timeout/min_confidence; cache. NEW file. |
| C1 Tier-2 thread escalation | C | todo | B1 | — | — | On `context_sufficient=false`, fetch thread by `thread_name` (`gchat._list_messages_sync` + filter), SUMMARIZE, store summary + `thread_status`. Bounded by `thread_max_messages`. |
| D1 cron integration | D | todo | C1 | — | — | `analyze.run_in_cron` -> `triage_cron.sh` chains `collect && analyze && notify`; analyze self-gates; `install_cron.sh` unaffected. |
| D2 notify/template unify | D | todo | C1 | — | — | Fix dead `"urgent"` token (-> `high/normal/low`); ensure `context_summary` escaped via `_esc`; optional `msg_type` variant / quiet-hours bypass (user decides at GATE D). |
| D3 adapter router (optional) | D | todo | B1 | — | — | `codex`/`gemini` adapters behind `adapters.order`. Deferrable. |

## Gates

| Gate | After wave | State | Evidence |
|---|---|---|---|
| GATE A1 (unit + live smoke) | A | **PASSED** (2026-06-11) | Integrated A1+A2 live: probe `True`; real 1306-char CLASSIFY prompt → live haiku → schema-valid JSON. Deictic continuation correctly classified `type=continuation, context_sufficient=false` (validates the Tier-1→Tier-2 trigger, D-3). Config merges (`enabled=False`, `order=['claude']`); store fields default `None`; legacy items `.get()` falsy. Full suite **382 passed**. |
| GATE B1 (read-only-ish e2e) | B | pending | Run on a COPY of live store w/ `enabled=true` -> priority/msg_type/context_summary populated on real items; re-run idempotent; adapter-down -> graceful no-op; `status` unchanged; NO network writes. Demo digest-with-priorities. |
| GATE C (Tier-2) | C | pending | Real EP-53867 continuation -> Tier-1 flags insufficient -> Tier-2 escalates -> correct priority + thread summary. |
| GATE D (final) | D | pending | Enabled end-to-end on store copy: collect -> analyze (sets priority) -> notify (priority icon + detailed profile). Adapter failure degrades to today's LLM-free behavior. Full suite green. |

## Pre-flight findings (verified 2026-06-11 via Serena + pytest)

Ground-truth symbols from the design re-confirmed against the **current (dirty) working
tree**. All CONFIRMED except minor name drift; one baseline issue to resolve before Wave A:

- **CONFIRMED:** `store.set_fields(store, item_id_, now=None, **fields)` writes verbatim,
  no status change, no history (`scripts/store.py:274`). `_new_item_skeleton`
  (`store.py:102`) has `priority:"unset"`, `priority_reason`, `context_summary`,
  `context_confidence`, `quoted`, `thread_name`, `pinned`; lacks `msg_type`,
  `analyzed_at`, `analyzed_by`, `thread_status` (A3 adds them).
- **CONFIRMED:** `mentions_core._normalize` now populates `quoted` (via
  `gchat._compact_quote`) + `thread_name`. `gchat._list_messages_sync(creds, space_name,
  start_iso, end_iso) -> List[Dict]` (`google_chat.py:957`) and `gchat._compact_quote`
  (`google_chat.py:708`) exist.
- **CONFIRMED:** `notify.Sender(ABC)` (`notify.py:293`) = `name`/`enabled(cfg)`/`send(...)`.
  `notify._esc(text, mode)` modes `tg_html`/`gchat`/`plain` (`notify.py:602`).
  `context_summary` is escaped: it flows as `summary_src` through `notify._esc` in
  `templates.py` (render line ~340). `notify._is_new_candidate` requires
  `status=="new"` (`notify.py:250`).
- **CONFIRMED:** `config.DEFAULT_CONFIG` (`config.py:44`), `_deep_merge` (`config.py:95`),
  `load_config()` (`config.py:209`), `is_active(cfg, now)` (`config.py:270`) exist; NO
  `analyze` key yet (A4 adds it). `in_quiet_hours` lives in `notify.py:211` (not config).
- **CONFIRMED:** dead `"urgent"` token at `templates.py:158`
  (`{"when": {"priority": ["urgent","high"]}, ...}`); `triage_session._VALID_PRIORITIES =
  ("high","normal","low")` (`triage_session.py:153`). `set_triage` transitions
  new->triaged via `store.set_status` — correctly NOT used by analyze.
- **DRIFT (doc only):** the variant matcher is `_variant_matches` /
  `_variant_profile_name` (`templates.py:206/225`), not `_matches_when` as the prompt
  names it. Behavior is as described.
- **BASELINE ISSUE (must resolve before Wave A):** test baseline is **270 passed,
  1 failed** — NOT the "247 green" the prompt claims. Failure is
  `tests/test_mentions_core.py::test_direct_mention_in_space_detected`: it asserts the
  pre-`quoted` normalized field set and the uncommitted quote-reply WIP added `quoted`.
  The tree carries related WIP (`google_chat.py` +212, `notify.py`, `store.py` +6,
  `server.py` +61, `mentions_core.py` +5, two test files) = the **quote-reply
  passthrough** groundwork this feature depends on. A green baseline is required for the
  verifier protocol (verifiers diff pytest before/after). Resolution chosen with user:
  _TBD_.

## Open questions (surface at the listed gate)

- **GATE B1:** batch several new items into one haiku call for cost, or one-call-per-item?
- **GATE C:** summary staleness — when a thread grows, re-analyze?
- **GATE D:** should `priority=high` bypass quiet hours like an escalation?
- **GATE D:** keep `claude` only, or wire `codex`/`gemini` router now (D3)?

## Changelog

- 2026-06-11 — Journal created; all tasks = `todo`; ground-truth re-confirmed via Serena;
  pre-flight baseline issue (1 stale test from quote-reply WIP) flagged for user decision.
- 2026-06-11 — Baseline made green (271→fixed stale `quoted` test) and committed locally as two
  honest commits (`docs(claude)` Serena guidance; `feat(triage)` quote-reply + list_chat_authors
  + notify tuning). Wave A dispatched (4 parallel clean builders, disjoint files).
- 2026-06-11 — Wave A all 4 verified by independent adversarial verifiers; full suite **382 passed**
  (+111). GATE A1 PASSED (integrated live haiku smoke). Tracked residual: A2 space-insertion
  injection neutralization is model-dependent — revisit at GATE D. Open: A2 whitespace-only-quoted
  test naming (LOW), SUMMARIZE header wording vs D-5 (LOW) — cosmetic, not blocking.
</content>
</invoke>
