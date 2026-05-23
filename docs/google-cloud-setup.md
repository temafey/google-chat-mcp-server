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
3. Enable these APIs (search by name, or use direct links):
   - **Google Chat API** — required for all Chat tools
     (https://console.cloud.google.com/apis/library/chat.googleapis.com)
   - **Google Calendar API** — required for all Calendar tools
     (https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)

> **Display-name resolution** uses `chat.memberships.readonly` + space-memberships
> listings (not the People API anymore), so you do **not** need to enable
> People API. The first time `list_space_members` / `find_users_by_name` /
> `search_chat_messages` touches a space, the in-process cache fills with real
> names for everyone in that space.

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

## 3. OAuth consent screen — scopes (Data Access tab)

Google reorganised this UI in 2025 — what used to be called *OAuth consent
screen* is now **Google Auth Platform** in the GCP console, and the scope list
lives in a separate tab called **Data Access** (no longer behind *Edit App*).

If you skip this step or miss a scope, Google's consent page **silently issues
a token without the missing scope** instead of failing — every tool that needs
that scope then returns `403 Request had insufficient authentication scopes`.

### Add the required scopes

Direct link: https://console.cloud.google.com/auth/scopes (switch to the
correct project in the top selector first).

Alternate path via menu: **APIs & Services** → **Google Auth Platform** →
**Data Access** tab.

1. Click **ADD OR REMOVE SCOPES**
2. In the right-hand filter, paste each scope below one at a time, tick the
   checkbox on the matched row, then move to the next:

   | Scope | Used by |
   |---|---|
   | `https://www.googleapis.com/auth/chat.spaces.readonly` | `get_chat_spaces` |
   | `https://www.googleapis.com/auth/chat.messages` | read messages, send messages, upload attachments |
   | `https://www.googleapis.com/auth/chat.memberships.readonly` | `list_space_members`, `find_users_by_name`, display-name resolution |
   | `https://www.googleapis.com/auth/userinfo.profile` | resolve `"me"` to a `users/<id>` |
   | `https://www.googleapis.com/auth/calendar.readonly` | `get_calendars`, `get_calendar_freebusy` |
   | `https://www.googleapis.com/auth/calendar.events` | all `*_calendar_event` tools |

   If a Calendar scope is missing from the filter, the Calendar API is not
   enabled — go back to §1 and enable it.
3. Click **UPDATE** at the bottom of the side panel, then **SAVE** on the
   Data Access page.

### Testing-mode gotcha

If the app is in **Testing** (default for new projects), only accounts in the
Test users list can complete the OAuth flow.

**Audience** tab → **Test users** → **+ Add users** → add the Google account
you'll authenticate as.

### Verify the token actually got the scopes

After the one-time auth flow in §4, run:

```bash
python3 -c "import json; t=json.load(open('token.json')); print('\n'.join(sorted(t['scopes'])))"
```

You must see all six scopes. If any are missing, **the Data Access list did
not include them** — re-do this section, `rm token.json`, and re-run §4.

---

## 4. Chat App configuration (mandatory even for read-only user-OAuth)

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

## 5. Authorization (one-time)

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

## 6. Environment variables (alternative to credentials.json)

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

## 7. MCP client wiring

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

## 8. Verifying it works

In your MCP client, ask the assistant:

> List my Google Chat spaces

If you get a JSON list of spaces, the setup is complete. If you get an error,
check §9.

---

## 9. Common pitfalls

### `404 Google Chat app not found`

The Chat API is enabled but the Chat App is not configured. See §4. Even with
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
the wrapper script (not `source .env && uv run ...` — see §6).

### `403 Request had insufficient authentication scopes`

Returned by any tool whose scope was not granted on the consent screen. Two
common causes:

1. The scope is not in the **Data Access** list (§3). Google then silently
   issued a token without it. Fix: add the scope to Data Access, `rm
   token.json`, re-run `--auth cli`.
2. The token was issued before the scope was added to `SCOPES` in
   `google_chat.py`. Fix: same — `rm token.json` and re-auth.

Verify which scopes are actually in the current token:

```bash
python3 -c "import json; t=json.load(open('token.json')); print('\n'.join(sorted(t['scopes'])))"
```

### Sender shows as `users/<numeric-id>` instead of a real name

The in-process display-name cache is empty for that user. The cache fills as
you call `list_space_members` / `find_users_by_name` for spaces the user is
in. Cold-start a session does not have it. Workaround: call
`list_space_members` on the relevant space once, or use `find_users_by_name`
to pre-warm.

The cache resets when the MCP server restarts (it's process-memory only).

### `Scope has changed from X to Y` warning on refresh

Only happens if code passes `SCOPES` to
`Credentials.from_authorized_user_file(path, SCOPES)` after expanding
`SCOPES`. This codebase deliberately omits the second arg — see the comment
near `get_credentials` in `google_chat.py`. If you ever re-add it, every tool
breaks on token refresh after the next scope expansion, not just the
new-scope tools.

---

## 10. Resetting / re-authenticating

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
