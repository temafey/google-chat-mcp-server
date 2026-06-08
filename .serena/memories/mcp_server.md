# MCP Server — Tools, Scopes, Auth, Env

## Registered MCP tools (in `server.py`)
- **Chat:** `get_chat_spaces`, `get_space_messages`, `send_chat_message`, `upload_chat_attachment`, `search_chat_messages`, `list_space_members`, `find_users_by_name`, `whoami`, `list_messages_for_me`.
- **Calendar:** `get_calendars`, `get_calendar_events`, `get_calendar_event`, `create_calendar_event`, `update_calendar_event`, `delete_calendar_event`, `quick_add_calendar_event`, `get_calendar_freebusy`.
- `list_messages_for_me` = thin wrapper over `mentions_core.list_messages_for_me` (the "addressed to me" detection core). `whoami` resolves identity.
- Demo stubs (`add`, `fetch_weather`, `get_ip_my_address`) already removed.

## Search-by-sender architecture
No server-side sender filter under user OAuth → N-space client-side scan is state of the art (verified; don't re-research Cloud Search/Vault unless admin Workspace Enterprise). User's own sender id: `users/117216798078927891621`.

## Name resolution
`directory.readonly` + People `listDirectoryPeople` resolves Chat sender IDs → real names ~100%. Already SHIPPED + prod token re-authed (2026-06-05). People API alone canNOT resolve coworker display names.

## Scopes (single OAuth flow, both APIs)
- Chat: `chat.spaces.readonly`, `chat.messages`, `chat.memberships.readonly`, `userinfo.profile`, `directory.readonly`.
- Calendar: `calendar.readonly`, `calendar.events` (deliberately narrowed, NOT full calendar).
- Owned by `google_chat.SCOPES`. Expansion footguns: see `mem:conventions`.

## Auth flows
- `--auth cli` → `auth_cli.run_cli_auth()` (paste redirect URL). Manual CLI auth needs `.env` exported first.
- `--auth web` → `server_auth.run_auth_server()` FastAPI: `/auth`, `/auth/callback`, `/auth/refresh`, `/oauth2callback`, `/status`; `oauth_flows` dict tracks in-progress exchanges.

## Env-var config (credentials.json now OPTIONAL)
`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`, `WORKSPACE_MCP_PORT`, `USER_GOOGLE_EMAIL`.

## Token-payload trimming
`SAVE_TOKEN_MODE` (default on, owned by `google_chat.py`; calendar reads it live). Strips Chat messages to `{sender,createTime,text,thread}`; calendar events to a fixed whitelist. Toggle off via `--disable-token-saving`.

## Reference docs (vetted, read before re-querying Context7)
- `docs/google-chat-api-guide.md` — API reference (scopes/quotas/threading/auth).
- `docs/google-cloud-setup.md` — operational GCP/OAuth/Chat-App setup walkthrough + pitfalls.

## Live test sandbox
`spaces/AAQA3S7I39E` ("artem-test-chat", Artem-only) for write/search smoke tests.
