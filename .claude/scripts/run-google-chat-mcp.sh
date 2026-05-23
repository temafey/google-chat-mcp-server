#!/usr/bin/env bash
# Wrapper for google-chat-mcp-server that loads credentials from .env.google
# Used by .mcp.json to keep OAuth secrets out of the config file.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${SCRIPT_DIR}/../../.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: Missing ${ENV_FILE}" >&2
    echo "Create it with your Google OAuth credentials:" >&2
    echo "  GOOGLE_OAUTH_CLIENT_ID=....apps.googleusercontent.com" >&2
    echo "  GOOGLE_OAUTH_CLIENT_SECRET=..." >&2
    echo "  GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/oauth2callback" >&2
    echo "  WORKSPACE_MCP_PORT=8000" >&2
    echo "  USER_GOOGLE_EMAIL=your@email.com" >&2
    exit 1
fi

set -a
source "$ENV_FILE"
set +a

_reset_mouse() { printf '\e[?9l\e[?1000l\e[?1001l\e[?1002l\e[?1003l\e[?1004l\e[?1005l\e[?1006l\e[?1015l\e[?1016l'; }
trap '_reset_mouse' EXIT INT TERM

# For --auth cli: run auth_cli.py directly — avoids importing fastmcp/typer/rich
# which permanently re-enable mouse modes and cannot be suppressed otherwise
if [[ " $* " == *" --auth cli "* ]] || [[ "$*" == "--auth cli" ]]; then
    uv --directory "$REPO_DIR" run --quiet auth_cli.py
else
    uv --directory "$REPO_DIR" run --quiet server.py "$@"
fi
