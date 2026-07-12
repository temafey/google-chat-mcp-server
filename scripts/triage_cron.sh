#!/usr/bin/env bash
#
# triage_cron.sh — cron wrapper for the Google Chat Triage Assistant (T2.3).
#
# Pipeline: collect → analyze → notify (all three chained with &&).
#
#   collect_mentions.py   — read-only; fetches new @mentions from Chat API and
#                           writes them to the local triage store.
#   analyze_mentions.py   — opt-in AI stage; gated by config key
#                           analyze.run_in_cron (default: False).  Invoked with
#                           --cron so it self-skips (exit 0) when that flag is
#                           False, ensuring notify always runs.  When enabled,
#                           it calls an LLM CLI (claude / codex / gemini) as a
#                           subprocess — those CLIs must be authenticated in the
#                           cron environment.  analyze writes ONLY the local
#                           triage store; Tier-2 thread reads are read-only
#                           messages.list calls; it performs NO Chat writes.
#   notify.py             — sends digest / inbox messages; the only step that
#                           writes to Google Chat, and only when gated by config.
#
# NOTE: with &&-chaining a catastrophic analyze exit≠0 (rare; soft failures are
# counted and returned as exit 0) would skip notify for that cycle.  This is
# intentional: it signals something badly wrong; the next cron tick recovers.
#
# cron hands us a MINIMAL environment (no PATH, no venv, no PYTHONPATH), so
# this wrapper reconstructs everything the Python scripts need.
#
# Overlap protection at the cron level is provided by the flock(1) in the
# installed crontab line.
#
# Usage:
#   triage_cron.sh           # one collect+analyze+notify cycle, output appended
#                            # to a dated log file (the cron path).
#   triage_cron.sh --once    # same sequence once, output ALSO to stdout
#                            # (smoke test).
#
# Exit status: non-zero if the collector fails (so the outer flock/cron records
# the failure). If collect succeeds but a later step fails, the exit reflects
# that step.
set -euo pipefail

# --- Fixed location of the checkout (cron has no notion of cwd) ------------- #
REPO="/home/temafey/projects/google-chat-mcp-server"
cd "$REPO"

# --- Reconstruct the environment cron strips ------------------------------- #
# uv lives in ~/.local/bin; keep cargo + the standard system bins too.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin"
# scripts/ is not an installed package — the Python modules add repo root and
# scripts/ to sys.path themselves, but PYTHONPATH makes `uv run python` resolve
# them regardless of how cron invokes us.
export PYTHONPATH="$REPO"

# The analyze step spawns the `claude` CLI, which reads its OAuth credentials
# from CLAUDE_CONFIG_DIR (default: ~/.claude).  cron does NOT source .bashrc, so
# without this the CLI falls back to a STALE ~/.claude/.credentials.json and every
# call fails with HTTP 401 (root cause of the historical failed=N-per-tick).
# analysis_adapters._build_clean_env keeps CLAUDE_* vars, so this propagates to
# the subprocess.  Point it at the actively-refreshed config dir.
export CLAUDE_CONFIG_DIR="/home/temafey/.claude-primary"

# --- Logging --------------------------------------------------------------- #
LOGDIR="$HOME/.claude-orchestrator/gchat-triage/logs"
mkdir -p "$LOGDIR"
# One dated log file per run is acceptable (rotation is out of scope). `date`
# here is shell, not the Python scripts — using it is fine.
LOGFILE="$LOGDIR/triage_cron-$(date +%Y-%m-%d).log"

# --- Parse the single supported flag --------------------------------------- #
ONCE=0
if [[ "${1:-}" == "--once" ]]; then
  ONCE=1
fi

# Run collect → analyze → notify, chained with && so each step is skipped if
# the prior one fails.  We must capture the chain's exit status WITHOUT `set -e`
# aborting the script mid-pipeline, so the body runs in a function whose status
# we inspect.  analyze is passed --cron so it self-skips (exit 0) when
# analyze.run_in_cron is False — Python owns all config gating, no JSON parsing
# in bash.
run_cycle() {
  uv run python scripts/collect_mentions.py \
    && uv run python scripts/analyze_mentions.py --cron \
    && uv run python scripts/notify.py
}

stamp() {
  # date is shell-level — fine here.
  printf '===== triage_cron %s (once=%s) =====\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$ONCE"
}

rc=0
if [[ "$ONCE" -eq 1 ]]; then
  # Smoke test: tee combined stdout+stderr to BOTH the log and the terminal.
  { stamp; run_cycle; } 2>&1 | tee -a "$LOGFILE" || rc="${PIPESTATUS[0]}"
else
  # cron path: append combined stdout+stderr to the dated log only.
  { stamp; run_cycle; } >>"$LOGFILE" 2>&1 || rc=$?
fi

exit "$rc"
