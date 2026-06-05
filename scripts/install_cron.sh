#!/usr/bin/env bash
#
# install_cron.sh — idempotent crontab installer for the Chat Triage Assistant
# (T2.3). Reads the poll cadence from config.json, composes a single crontab
# line that runs triage_cron.sh under flock(1) overlap protection, and installs
# it WITHOUT touching any of the user's other cron entries.
#
# This script does NOT enable cron-on-boot (that needs sudo / /etc/wsl.conf) and
# does NOT run any sudo command. See docs/chat-triage-assistant/CRON-SETUP.md for
# the one-time host setup the user performs manually.
#
# Re-running this script is safe: it strips any prior triage_cron.sh line first,
# then appends the freshly-composed one.
set -euo pipefail

REPO="/home/temafey/projects/google-chat-mcp-server"
WRAPPER="$REPO/scripts/triage_cron.sh"
TRIAGE_DIR="$HOME/.claude-orchestrator/gchat-triage"
CRON_LOCK="$TRIAGE_DIR/cron.lock"
CRON_LOG="$TRIAGE_DIR/logs/cron.log"

cd "$REPO"

# --- Make the wrapper + this installer executable -------------------------- #
chmod +x "$REPO/scripts/triage_cron.sh" "$REPO/scripts/install_cron.sh"
echo "Made scripts/triage_cron.sh and scripts/install_cron.sh executable."

# --- Read poll_cadence_minutes from config.json (default 10) ---------------- #
# Use the project's own config loader so schema defaults / merges apply.
CADENCE="$(
  uv run python -c "import sys;sys.path.insert(0,'scripts');import config;print(config.load_config()['poll_cadence_minutes'])" \
    2>/dev/null || true
)"
# Validate it is a positive integer; otherwise fall back to 10.
if ! [[ "$CADENCE" =~ ^[1-9][0-9]*$ ]]; then
  echo "WARNING: could not read a valid poll_cadence_minutes; defaulting to 10." >&2
  CADENCE=10
fi
echo "Poll cadence: every ${CADENCE} minute(s)."

# --- Compose the crontab line ---------------------------------------------- #
# flock -n acquires cron.lock non-blocking: if a prior run still holds it, this
# tick is skipped (overlap protection at the schedule level — the Python scripts
# additionally hold their own run/token locks). All wrapper output is appended
# to cron.log.
CRON_LINE="*/${CADENCE} * * * * /usr/bin/flock -n ${CRON_LOCK} ${WRAPPER} >> ${CRON_LOG} 2>&1"

# --- Ensure runtime dirs exist so the first tick can write immediately ------ #
mkdir -p "$TRIAGE_DIR/logs"

# --- Install idempotently --------------------------------------------------- #
# Read the existing crontab (empty if none), drop any prior triage_cron.sh line,
# append the new line, and load it back.
EXISTING="$(crontab -l 2>/dev/null || true)"
FILTERED="$(printf '%s\n' "$EXISTING" | grep -v 'triage_cron.sh' || true)"

# Build the new crontab content. Trim leading/trailing blank lines for tidiness.
NEW_CRONTAB="$(
  {
    printf '%s\n' "$FILTERED" | sed '/^[[:space:]]*$/d'
    printf '%s\n' "$CRON_LINE"
  }
)"

printf '%s\n' "$NEW_CRONTAB" | crontab -
echo "Installed crontab. Current entries:"
echo "----------------------------------------"
crontab -l
echo "----------------------------------------"

# --- Reminder: cron-on-boot is a manual, sudo step -------------------------- #
cat <<'NOTE'

NOTE: The schedule is installed, but cron itself must be RUNNING and set to
start on WSL boot. That is a host/sudo action this script does NOT perform.

  1. Start cron now:        sudo service cron start
  2. Enable on WSL boot:    add a [boot] command to /etc/wsl.conf, then run
                            `wsl --shutdown` once from Windows.

Full steps: docs/chat-triage-assistant/CRON-SETUP.md
NOTE
