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
| B1 `analyze_mentions.py` | B | **verified** | A1,A2,A3,A4 | 54 tests; full suite 436 | PASS (after fix) | Orchestrating script: gate on `analyze.enabled`; select `new & priority=="unset" & not analyzed`; Tier-0 pre-filter; Tier-1 classify; run-lock + logging; honor caps/timeout/min_confidence; cache. First verifier pass found a **CRITICAL test-isolation bug** (tests wrote the live prod store via `store_path=None`) — see Incident below. Fixed via `tests/conftest.py` autouse net (`GCHAT_TRIAGE_STORE` env + `DEFAULT_STORE_PATH` patch) + explicit tmp paths; LOW deviation resolved (`item.update`→`store.set_fields`). Re-verified PASS: prod store items md5 identical before/after full suite, subprocess-safe, no KeyError regression, prod semantics preserved. |
| C1 Tier-2 thread escalation | C | **verified** (2026-06-12) | B1 | builder | independent | New READ-ONLY `gchat._list_thread_messages_sync` (`thread.name=".."` filter, explicit `orderBy=create_time ASC`, truncate `[-max:]`, oldest→newest). `analyze_mentions.py`: `select_handoff_items` (predicate: `analyzed_at` set ∧ `priority=="unset"` ∧ `thread_status is None` ∧ `thread_name` ∧ `message_name`), `_validate_summarize_result`, `_build_thread_ctx` (TARGET row by `message_name`; cache-only name resolve), `escalate_item`, `run_tier2` (lazy creds — offline stays offline w/ 0 handoff items). Integrated after Tier-1 loop, gated by `analyze.escalate_to_thread`. Writes via `store.set_fields` (NO status/history change). +29 tests → 83 in file; full suite **465 passed**. Verifier PASS on all 11 adversarial checks (read-only network = only `messages.list`; Tier-1 byte-identical to HEAD; graceful no-op on escalate_off/creds-None/fetch-raises/ok=False/bad-schema; post-escalation idempotency; prod store md5 `fb94254d…` identical pre/post suite). Hardening applied post-verify: explicit `orderBy=create_time ASC` so the most-recent-N slice is provably correct (was relying on unspecified API default). All changes UNSTAGED. |
| D1 cron integration | D | todo | C1 | — | — | `analyze.run_in_cron` -> `triage_cron.sh` chains `collect && analyze && notify`; analyze self-gates; `install_cron.sh` unaffected. |
| D2 notify/template unify | D | todo | C1 | — | — | Fix dead `"urgent"` token at `templates.py:158` (-> `high/normal/low`); **make AI `priority` drive digest ORDERING** (GATE-B1 finding: high item is currently buried under the attention-tier sort — `_priority_icon` shows the icon but ordering ignores priority); ensure `context_summary` escaped via `_esc` and surfaced; optional `msg_type` variant / quiet-hours bypass for `high` (user decides at GATE D). Icon map today: high/urgent→🔴, medium→🟡, normal→🟢, else ⚪. |
| D3 adapter router (optional) | D | todo | B1 | — | — | `codex`/`gemini` adapters behind `adapters.order`. Deferrable. |

## Gates

| Gate | After wave | State | Evidence |
|---|---|---|---|
| GATE A1 (unit + live smoke) | A | **PASSED** (2026-06-11) | Integrated A1+A2 live: probe `True`; real 1306-char CLASSIFY prompt → live haiku → schema-valid JSON. Deictic continuation correctly classified `type=continuation, context_sufficient=false` (validates the Tier-1→Tier-2 trigger, D-3). Config merges (`enabled=False`, `order=['claude']`); store fields default `None`; legacy items `.get()` falsy. Full suite **382 passed**. |
| GATE B1 (read-only-ish e2e) | B | **PASSED** (2026-06-12) | Live haiku run on a COPY (`/tmp/gate-b1`, fully isolated config+store+locks+logs) of the recovered 99-item store, `enabled=true`, `--limit 5`. Run 1: `selected=5 stored=5` — all got msg_type/priority/priority_reason/context_summary/context_confidence/analyzed_by=`claude/haiku`. Sound classifications: "extremely important" → `high`(🔴) conf 0.92; PO request → `normal`; social/fyi → `normal`(🟢). Run 2 (idempotency): advanced to a DIFFERENT 5 (`selected=5 stored=2 deferred=3`); original 5 byte-identical (analyzed_at unchanged) — already-analyzed never re-processed. The 3 DEFER items are all `msg_type=continuation, priority=unset, thread_status=None` = the exact Wave-C handoff predicate (live B1↔C1 proof). All 10 analyzed still `status==new`, history unchanged. Prod store untouched (99 items, md5 `fb94254dbaba` identical). Adapter-down/dry-run covered by the 54 unit tests. Digest renders priority icons via `notify._priority_icon` (high→🔴, normal→🟢). NOTE: priority does NOT yet drive digest ORDERING (high item buried under attention-tier sort) — that wiring is D2. |
| GATE C (Tier-2) | C | **PASSED** (2026-06-12) | Live haiku + REAL read-only Chat thread fetches on a COPY (`/tmp/gate-c`, isolated config+store+locks) of the current 99-item store, `enabled=true escalate_to_thread=true max_items=15`. Result: `selected=15 stored=6 deferred=9 escalated=9 thread_failed=0` — the full Tier-1-DEFER → live Tier-2 escalation flow fired end-to-end. The 9 escalated items each got msg_type/priority/priority_reason/context_summary/context_confidence/thread_status, all GENUINELY thread-grounded (release-workflow explanation, `master`-branch semantics, PR-ownership decision — i.e. messages meaningless in isolation, exactly Tier-2's purpose), not single-message restatements. `thread_status` correctly varied (`awaiting_me`/`awaiting_others`/`fyi`); priorities sensible (low-conf 0.35 `unclear` → `low`). Original EP-53867 ticket text is absent from the current store (earlier snapshot) so a different-but-equivalent real continuation demonstrated the same predicate. INVARIANTS: all 15 analyzed still `status==new`; history NOT appended/mutated (0/15 vs prod source — the `hist=1` is the collector's pre-existing `detected` entry, preserved byte-identically); writes via `store.set_fields`. Tier-2 network READ-ONLY (`messages.list` only). Prod store untouched (99 items, 0 analyzed, md5 `fb94254d…`). |
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

## Incident — test suite overwrote the live triage store (2026-06-11/12)

- **What:** `tests/test_analyze_mentions.py` made ~17 non-dry-run `run_analysis(..., store_path=None)`
  calls. `store._resolve_path(None)` → `DEFAULT_STORE_PATH` = the REAL prod store
  (`~/.claude-orchestrator/gchat-triage/store.json`), so every `pytest` run (B1 build + verification)
  overwrote live data with test fixtures. The independent B1 verifier caught it.
- **Damage:** live store knocked down to 3 synthetic test items. The `*/5` cron collector self-healed
  recent mentions (id-keyed upsert) but older untriaged `new` items + any lifecycle state were lost.
  Production `analyze` path was NOT implicated (cron logged `skip: analyze-disabled`) — corruption was
  purely pytest writing the prod path.
- **Recovery (user-approved "restore + merge"):** under a blocking `flock` on `collect.lock` (serialized
  with the cron), merged `store.json.prebackfill.bak` (Jun-5, 86 items incl 3 triaged) ∪ 13 cron-recovered
  recent real items, dropped the 3 test items → **99 items (3 triaged + 96 new)**, 0 test pollution.
  Safety copies: `store.json.pre-restore-<ts>.bak`, `store.json.restored-99items-safekeep`.
- **Root-cause fix (verified):** `tests/conftest.py` function-scoped autouse fixture redirects EVERY test
  away from prod via BOTH `GCHAT_TRIAGE_STORE` env (survives subprocess) AND `DEFAULT_STORE_PATH` patch;
  the 15 `store_path=None` calls made explicit tmp paths. Re-verified: full-suite run leaves prod `items`
  md5 byte-identical. **Rule going forward: never call `run_analysis`/`store.save` with `store_path=None`
  in tests; the conftest net is the backstop.**

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
- 2026-06-11/12 — B1 (`analyze_mentions.py` + 54 tests) built; first independent verifier returned FAIL
  on a CRITICAL test-isolation bug (pytest overwrote the live store via `store_path=None`). Incident
  contained; live store recovered to 99 items (restore Jun-5 backup ∪ cron-recovered recent, user-approved).
  Root cause fixed (`tests/conftest.py` autouse isolation net) + LOW deviation resolved
  (`item.update`→`store.set_fields`). Re-verified PASS: prod store byte-identical across full suite,
  subprocess-safe, no regression. Full suite **436 passed**. B1 = verified. Next: GATE B1 (live, on a copy).
- 2026-06-12 — GATE B1 PASSED. Live haiku on an isolated copy of the recovered 99-item store: 2 batches of 5,
  classifications sound (high/normal + continuation→DEFER for Wave C), idempotent, status/history untouched,
  prod store byte-identical. Surfaced D2 requirement: priority must drive digest ordering (not just icon).
  Recovery artifacts retained in the runtime dir: `store.json.pre-restore-<ts>.bak`,
  `store.json.restored-99items-safekeep`. Open (GATE B1): batch N items per haiku call vs one-call-per-item.
</content>
</invoke>
