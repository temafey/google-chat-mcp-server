#!/usr/bin/env bash
#
# triage_cron.sh — cron wrapper for the Google Chat Triage Assistant (T2.3).
#
# Runs the read-only collector THEN the notifier, chained so notify is skipped
# if collect fails. cron hands us a MINIMAL environment (no PATH, no venv, no
# PYTHONPATH), so this wrapper reconstructs everything the Python scripts need.
#
# It adds ZERO new Chat-write calls of its own — the only outward write is
# notify.py's GCInboxSender, gated by config. The collector is strictly
# read-only. Both scripts hold their own run/token locks; overlap protection at
# the cron level is provided by the flock(1) in the installed crontab line.
#
# Usage:
#   triage_cron.sh           # one collect+notify cycle, output appended to a
#                            # dated log file (the cron path).
#   triage_cron.sh --once    # same sequence once, output ALSO to stdout
#                            # (smoke test).
#
# Exit status: non-zero if the collector fails (so the outer flock/cron records
# the failure). If collect succeeds but notify fails, the exit reflects notify.
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

# Run collect THEN notify, chained with && so notify is skipped on collect
# failure. We must capture the chain's exit status WITHOUT `set -e` aborting the
# script mid-pipeline, so the body runs in a function whose status we inspect.
run_cycle() {
  uv run python scripts/collect_mentions.py \
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
