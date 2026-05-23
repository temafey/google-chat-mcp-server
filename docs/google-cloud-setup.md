# Google Cloud setup guide

End-to-end setup for running this MCP server against your own Google Workspace
account. Covers GCP project creation, OAuth client, Chat App configuration,
environment variables, wrapper script, and the authorization flow.

For API-level reference (scopes, quotas, threading semantics), see
[`google-chat-api-guide.md`](google-chat-api-guide.md). This document is
operational — *how to get from zero to a working server*.

---

## 1. Google Cloud project

You need a GCP project with the right APIs enabled and an OAuth client.

### Enable the APIs

1. Open https://console.cloud.google.com
2. Create a new project or pick an existing one
3. Enable these APIs (search by name):
   - **Google Chat API** — required for `get_chat_spaces` / `get_space_messages`
   - **People API** — required for resolving sender display names

Direct link: https://console.cloud.google.com/marketplace/product/google/chat.googleapis.com

> **Note:** People API can only resolve names of users in your contacts. For
> Workspace coworkers outside your contacts, `sender` will stay as the raw
> `users/<numeric-id>`. This is a permission limit of user-OAuth, not a bug.

---

## 2. OAuth client

Pick one of two client types. Both work — the difference is mainly the redirect
URI registration model.

### Option A — Desktop application (recommended for personal use)

1. Go to https://console.cloud.google.com/auth/clients
2. **Create client → Desktop application**
3. Save — you get `client_id` and `client_secret`

Desktop clients accept **any** `http://localhost:<port>` redirect URI without
explicit registration. Simplest to set up.

### Option B — Web application (use when you need a stable callback)

1. **Create client → Web application**
2. Under **Authorized redirect URIs** add the exact URL your server will use,
   e.g. `http://localhost:8077/oauth2callback`
3. Under **Authorized JavaScript origins** add `http://localhost:8077`

If you change the port later, you must add the new URI here — Google rejects
unregistered redirect URIs with `redirect_uri_mismatch`.

### Save the credentials

Two ways — pick one:

- **File**: download the JSON as `credentials.json` and put it in the repo root
- **Environment variables**: skip the download and use env vars (see §5)

The code prefers `credentials.json` if present. Otherwise it falls back to env
vars.

---

## 3. Chat App configuration (mandatory even for read-only user-OAuth)

Even when you authenticate as a user (not a bot), Google Chat API requires a
Chat App to be configured in the same GCP project. Without it, every API call
returns `404 Google Chat app not found`.

1. Open https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat
2. Fill in the **Configuration** tab:
   - **App name** — anything, e.g. `Personal Chat MCP`
   - **Avatar URL** — required field. Any HTTPS image URL works. Example:
     `https://www.gstatic.com/images/branding/product/2x/chat_2020q4_48dp.png`
   - **Description** — anything
   - **Functionality** — leave defaults. Not relevant for user-OAuth reads.
   - **Connection settings** — required, but never invoked for user-OAuth.
     Pick **App URL** and enter a placeholder like `https://example.com/chat`
   - **Visibility** — add your own email so the app is visible to you
3. Save and wait ~1 minute for propagation

---

## 4. Authorization (one-time)

You need a `token.json` that the server will use to call Google. Generate it
once via CLI flow:

```bash
.claude/scripts/run-google-chat-mcp.sh --auth cli
```

The flow:

1. The script prints an **Authorization URL**
2. Open it in any browser, log in as the Google account you want to authorize
3. Approve the requested scopes
4. The browser redirects to the configured callback URL. **The browser will
   show "ERR_CONNECTION_REFUSED"** — this is expected; the CLI flow doesn't
   run a server.
5. **Copy the full URL from the browser address bar** (it has `?code=...&state=...`)
6. Paste it back into the terminal where the script is waiting
7. `token.json` is saved in the repo root

The token includes a refresh token, so it works indefinitely until you revoke
access — no re-authentication needed.

If you prefer the web flow with a real callback page:

```bash
.claude/scripts/run-google-chat-mcp.sh --auth web --port 8077
```

Then open `http://localhost:8077/auth`.

---

## 5. Environment variables (alternative to credentials.json)

The server reads these env vars when `credentials.json` is absent:

| Variable | Purpose | Required |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | OAuth client ID | yes |
| `GOOGLE_OAUTH_CLIENT_SECRET` | OAuth client secret | yes |
| `GOOGLE_OAUTH_REDIRECT_URI` | Full redirect URI sent to Google | optional |
| `WORKSPACE_MCP_PORT` | Default port for `--port` flag | optional |
| `USER_GOOGLE_EMAIL` | Pre-selects the account via `login_hint` | optional |
| `MCP_SINGLE_USER_MODE` | Reserved, informational | optional |

### `.env` file format

Put a `.env` file in the repo root:

```
GOOGLE_OAUTH_CLIENT_ID=380420187125-xxxxxxxxxxxxxxx.apps.googleusercontent.com
GOOGLE_OAUTH_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxx
USER_GOOGLE_EMAIL=you@example.com
MCP_SINGLE_USER_MODE=true
WORKSPACE_MCP_PORT=8077
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8077/oauth2callback
```

`.env` is already gitignored.

### Loading the .env file

**Use the wrapper script** — it handles `set -a` / `source` / `set +a` so vars
get exported to the subprocess:

```bash
.claude/scripts/run-google-chat-mcp.sh --auth cli
```

> A plain `source .env && uv run server.py` does **not** work: `source` sets
> shell variables, but without `export` they don't propagate to the child
> process. The wrapper script (or `set -a` before sourcing) is the fix.

Alternative: use `uv run --env-file .env server.py ...` which loads the file
directly.

---

## 6. MCP client wiring

Add this to your MCP client config (e.g. `~/.config/claude-desktop/claude_desktop_config.json`
or `.mcp.json`):

```json
{
  "mcpServers": {
    "google_chat": {
      "command": "/absolute/path/to/google-chat-mcp-server/.claude/scripts/run-google-chat-mcp.sh",
      "args": [
        "--token-path", "/absolute/path/to/google-chat-mcp-server/token.json"
      ]
    }
  }
}
```

Restart the client. The tools `get_chat_spaces` and `get_space_messages` should
appear in its tool list.

---

## 7. Verifying it works

In your MCP client, ask the assistant:

> List my Google Chat spaces

If you get a JSON list of spaces, the setup is complete. If you get an error,
check §8.

---

## 8. Common pitfalls

### `404 Google Chat app not found`

The Chat API is enabled but the Chat App is not configured. See §3. Even with
user-OAuth (no bot), this configuration is mandatory.

### `Authentication Processing Error: Invalid or expired OAuth state parameter`

The error page came from a **different server** running on the same port (e.g.
a parallel `workspace-mcp` instance). The state parameter is per-server; if
another OAuth-aware server intercepts the callback, you'll see this.

Fixes:

- Stop the conflicting server (`kill <pid>`, find with `ss -tlnp | grep <port>`)
- Or change `WORKSPACE_MCP_PORT` + `GOOGLE_OAUTH_REDIRECT_URI` to a free port
  (and re-register the new URI in GCP for Web clients)

The CLI flow doesn't care about the response page — even with an error page
showing, the URL in the browser's address bar still has the `?code=...`
parameter, which is all you need.

### `redirect_uri_mismatch`

For **Web** OAuth clients only — the redirect URI you sent doesn't match any
registered URI in GCP. Add the exact URI under Authorized redirect URIs.
Desktop clients don't have this restriction.

### `ERR_CONNECTION_REFUSED` after authorization

Expected during `--auth cli`. The CLI flow doesn't start a server. Copy the URL
from the address bar (it has the `code` param) and paste it back into the
terminal.

### `credentials.json not found and GOOGLE_OAUTH_CLIENT_ID / ... not set`

Either drop `credentials.json` into the repo root, OR set the env vars and use
the wrapper script (not `source .env && uv run ...` — see §5).

### Sender shows as `users/<numeric-id>` instead of a real name

People API can only resolve names of users in your contacts. For Workspace
coworkers outside your contacts, the raw ID stays. This is by design — not a
bug in the server.

To map your *own* ID, post a test message in a fresh space and read it back;
the `sender` of your message is your ID.

---

## 9. Resetting / re-authenticating

If you change scopes, switch Google accounts, or the refresh token gets
revoked:

```bash
rm token.json
.claude/scripts/run-google-chat-mcp.sh --auth cli
```

To revoke access entirely: https://myaccount.google.com/permissions

---

## Related docs

- [`google-chat-api-guide.md`](google-chat-api-guide.md) — API-level reference
  (scopes, message size limits, threading, attachment upload, error codes)
- [`../tasks/env-vars-support.md`](../tasks/env-vars-support.md) — task notes
  for the env-vars feature
- [`../tasks/add-write-capabilities.md`](../tasks/add-write-capabilities.md) —
  planned write-side tools (send message, upload attachment)
