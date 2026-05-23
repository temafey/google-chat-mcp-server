# Google Chat API: Engineering Reference

Practical reference distilled from official Google Workspace Chat documentation (REST v1, verified via Context7 against `developers.google.com/workspace/chat`). Last verified 2026-05-13. This is not a tutorial — it's the set of facts a senior engineer needs to make correct decisions when building on Chat API.

The Chat API has a single current version: **v1**. Both REST and gRPC bindings exist; Python apps usually use REST via `googleapiclient.discovery` (`build("chat", "v1", credentials=creds)`).

---

## 1. The auth decision drives everything

There are exactly **two** authentication models, and they are not interchangeable. The choice determines which features you can use, what users see, and what credentials you need to operate.

### App authentication (service account)

- Uses a **service account key** (`credentials.json` downloaded from Google Cloud Console).
- The Chat app acts **as itself**. The author of any created message is the bot, not a user.
- The only scope is `https://www.googleapis.com/auth/chat.bot` (plus narrower `chat.app.*` scopes for memberships, spaces, etc.). `chat.bot` **does not appear** on OAuth consent screens — it's app-level only and cannot be used with user OAuth or domain-wide delegation.
- Required for: posting `cardsV2`, sending widgets, creating memberships as the app, app-driven space creation.

### User authentication (OAuth user flow)

- Uses an **OAuth client ID** (downloadable JSON), with the standard authorization-code flow (`google-auth-oauthlib`).
- The Chat app acts **on behalf of a user**. Created messages are attributed to that user.
- Scopes: `chat.messages`, `chat.messages.create`, `chat.messages.readonly`, `chat.spaces`, `chat.spaces.readonly`, `chat.memberships`, `chat.memberships.readonly`, `userinfo.profile`. Pick the **narrowest** scope that covers the operation.
- Required for: anything attributed to a real user account; reading message history a user is in; integrating with personal MCP/agent tooling.

### Capability matrix (verified against `create` API docs)

| Capability | App auth (`chat.bot`) | User auth (`chat.messages*`) |
|---|---|---|
| Send `text` message | yes | yes |
| Send `cardsV2` / widgets | yes | **no** |
| Attach uploaded file (`attachment[]`) | yes | yes |
| Create new thread | yes | yes |
| Reply in existing thread | yes | yes |
| Read messages from spaces user belongs to | n/a (bot model) | yes |
| Get attachment metadata | yes (`chat.bot`) | limited |
| Manage memberships | yes (with `chat.app.memberships`) | yes (with `chat.memberships`) |

This is the rule that most often surprises teams: **`cardsV2` cannot be sent with user OAuth.** If you need rich card UI, you must register a Chat app with a service account.

### Scope hygiene

- "If modifying these scopes, delete the file token.json." — Google's own guidance. Scopes are baked into the issued token; adding a scope later requires re-consent.
- Prefer `chat.messages.create` (write-only) over `chat.messages` (read+write) when you only need to post.
- Prefer `chat.spaces.readonly` over `chat.spaces` when you don't manage spaces.
- Never request `chat.bot` and user scopes in the same OAuth flow — `chat.bot` won't be granted via consent and the flow fails.

---

## 2. Architecture patterns

Google documents four supported app architectures. Pick based on **where your code runs** and **what events you need to receive**.

| Pattern | When it fits | When it doesn't |
|---|---|---|
| **Web / HTTP service** | Public app, marketplace listing, full DevOps control, any language. The standard for production. | Behind a firewall with no inbound HTTPS. |
| **Pub/Sub** | App is inside a private network and cannot accept inbound HTTPS. Events are pulled from a subscription. | Latency-sensitive synchronous responses (Pub/Sub adds queuing). |
| **Webhook (incoming only)** | One-way posting *into* a space from external systems (CI, monitoring, cron). Simplest possible integration. | Needs to receive Chat events, handle commands, or do anything bidirectional. |
| **Apps Script / AppSheet** | Low-code or no-code prototypes inside Google Workspace. | Anything production-grade with version control or CI. |

### Conversational patterns inside an architecture

- **Synchronous (call-response):** Chat sends an HTTPS event → your service replies in the HTTP response body. Easiest, but you have **30 seconds** before Chat times out the request.
- **Asynchronous (multiple responses):** ACK the event quickly, then post follow-up messages via `spaces.messages.create` over a separate connection. Required for long-running work.
- **Event-driven (subscribed):** Subscribe to Google Workspace Events API for events you care about (membership changes, reactions). Decouples your code from Chat's request lifecycle.
- **One-way (webhook):** External system → Chat. No interaction with users beyond posting.

### HTTP retry handling

Google Chat retries your service if it does not receive a timely success. **Idempotency is your problem to solve.** Use the `eventTime` plus event identifiers to de-duplicate. The `requestId` and `messageId` query parameters on `messages.create` give you idempotency on the write side (same `requestId` returns the previously created message instead of creating a duplicate).

---

## 3. Quotas — they shape design more than you'd expect

There are **two layers** of quotas. You hit whichever ceiling comes first.

### Per-project quotas (per minute, per Cloud project)

| Operation | Methods | Limit |
|---|---|---|
| Message writes | `messages.create`, `messages.patch`, `messages.delete` | **3000 / min** |
| Message reads | `messages.get`, `messages.list` | 3000 / min |
| Membership writes | `members.create`, `members.delete` | 300 / min |
| Membership reads | `members.get`, `members.list` | 3000 / min |
| Space writes | `spaces.setup`, `spaces.create`, `spaces.patch`, `spaces.delete` | **60 / min** |
| Space reads | `spaces.get`, `spaces.list`, `spaces.findDirectMessage` | 3000 / min |
| Attachment writes | `media.upload` | 600 / min |
| Attachment reads | `messages.attachments.get`, `media.download` | 3000 / min |
| Reactions | create/delete: 600 / min; list: 3000 / min |  |
| Custom emojis | writes: 600 / min; reads: 3000 / min |  |

### Per-space quotas (per second, shared across **all** apps in the space)

| Operation | Limit |
|---|---|
| Reads (`media.download`, `spaces.get`, `members.*`, `messages.get/list/attachments.get`, `reactions.list`) | **15 / sec** |
| Writes (`media.upload`, `spaces.delete/patch`, `messages.create/delete/patch`, `reactions.delete`) | **1 / sec** |
| Reaction create | 5 / sec |
| Message writes in import mode | 10 / sec |

The per-space write quota of **1 message/sec** is the practical bottleneck. If your app posts updates to a single space (e.g. a status channel), you cannot exceed one per second sustained, regardless of project quotas.

### Quota error handling

- Quota exceeded returns **HTTP 429 (Too many requests)**.
- Backend rate-limit checks can also return 429 even if you're under the published quota.
- Retry with **exponential backoff** (start ~1s, double up to ~32s, with jitter). Do not retry tighter than this.
- Stay within per-minute quotas and there's no per-day cap.

---

## 4. Message model

### Size

The maximum message size (including all fields — text, cards, widgets, metadata) is **32,000 bytes**. Validate locally before calling the API; the server rejects oversized payloads with 400 and you waste the round trip.

### Threading

A `Thread` is identified by **two** values that behave differently:

- `name` — the resource name `spaces/{space}/threads/{thread}`. Globally addressable. Use this to reply to threads created by **users** or **other apps**.
- `threadKey` — a client-supplied string up to 4000 chars, scoped to **your Chat app only**. Two different apps using the same `threadKey` value still post into separate threads. Use this when your app owns the threading scheme (e.g., one thread per incident ID).

When creating a message, the `messageReplyOption` query parameter controls behavior if the requested thread is missing:

| Value | Behavior |
|---|---|
| `MESSAGE_REPLY_OPTION_UNSPECIFIED` (default) | Starts a new thread; `thread` field is ignored if absent. |
| `REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` | Reply if thread exists; otherwise start a new thread. Forgiving. |
| `REPLY_MESSAGE_OR_FAIL` | Reply if thread exists; otherwise return 404. Strict — preferred when correctness matters more than always-deliver. |

Only valid in **named** spaces. Direct-message spaces don't support multi-threading.

### Idempotency

- `requestId` (query param): a UUID you generate. Re-sending with the same `requestId` returns the original message, not a duplicate. Use this on every write.
- `messageId` (query param): your custom ID, must start with `client-`, ≤ 63 chars `[a-z0-9-]`. Unique per space. Useful when *you* need to address the message later by your own ID.

---

## 5. Listing messages

`spaces.messages.list` is the only path for reading history.

```
GET /v1/{parent=spaces/*}/messages
  ?pageSize=…          (default 25, max 1000)
  &pageToken=…
  &filter=…
  &orderBy="create_time ASC" | "create_time DESC"
  &showDeleted=true|false
```

### Filter syntax (small DSL, easy to get wrong)

Supported fields: `create_time` and `thread.name`. Both can be combined with `AND`.

```
create_time > "2026-05-01T00:00:00Z" AND create_time < "2026-05-02T00:00:00Z"
thread.name = "spaces/AAAA/threads/BBBB"
```

`create_time` uses **RFC-3339** with offsets (UTC `Z` or `+00:00`). String values are double-quoted. Operators: `>`, `<`, `=`, `AND`. There is no `OR`, no `LIKE`, no text search.

### Pagination

Always loop on `nextPageToken` until empty. The first response with no `nextPageToken` is the end — do not assume `pageSize` items means more pages.

---

## 6. Attachments

### Upload mechanics

Two-step, both required:

1. `media.upload` (`POST https://chat.googleapis.com/upload/v1/{parent=spaces/*}/attachments:upload`) — uploads the binary, returns an `attachmentDataRef`.
2. `messages.create` — pass the entire upload response as a single element of the `attachment` array:

```python
uploaded = service.media().upload(
    parent="spaces/...",
    body={"filename": "report.pdf"},
    media_body=MediaFileUpload("report.pdf", mimetype="application/pdf"),
).execute()

service.spaces().messages().create(
    parent="spaces/...",
    body={"text": "Caption", "attachment": [uploaded]},
).execute()
```

The `attachment[]` element is `{"attachmentDataRef": {"attachmentUploadRef": {...}}}`. Do **not** unwrap to inner fields.

### Constraints

- **Max file size: 200 MB.**
- Scopes accepted: `chat.import`, `chat.messages.create`, `chat.messages`.
- The `parent` of `media.upload` and `messages.create` **must match** — uploading to one space and posting to another fails.
- `media.upload` is rate-limited at **600/min per project** and **1/sec per space** (shared write quota).

### Fetching attachment data

`spaces.messages.attachments.get` returns metadata only. The actual bytes are fetched via the **Media API** with the `media.download` endpoint. Metadata reads require the `chat.bot` scope (app auth).

---

## 7. Error handling

Common HTTP responses and what they mean:

| Status | Cause | Action |
|---|---|---|
| 400 | Malformed body, bad `space_name` format, oversized message, invalid filter syntax, illegal `messageId` characters. | Fix request locally. Do not retry. |
| 401 | Token expired or revoked. | Refresh credentials (`creds.refresh(Request())`). |
| 403 | Caller lacks permission: not a space member, missing scope, app not installed. | Surface clearly; retrying won't help. |
| 404 | Space/thread/message not found, **or** strict `REPLY_MESSAGE_OR_FAIL` thread didn't exist. | Distinguish from "permission" — UI should treat differently. |
| 429 | Quota or rate limit exceeded. | Exponential backoff with jitter. |
| 5xx | Transient Google-side issue. | Backoff + retry, capped attempts. |

Catch `googleapiclient.errors.HttpError` at the boundary, inspect `e.resp.status`, return a structured error to your caller instead of leaking the traceback.

---

## 8. Application of this to the current project

This repository (`google-chat-mcp-server`) is a **user-auth** Chat client wrapping the API as MCP tools. Therefore:

- The cards/widgets path is **off the table** for now. To enable, register a separate Chat app with a service account in Google Cloud Console and add a second auth flow.
- The current scope set (`chat.spaces.readonly` + `chat.messages` + `userinfo.profile`) is already sufficient for: listing spaces, reading messages, sending text, uploading attachments, threading. **No re-auth is needed** to extend the project with `send_message` / `upload_attachment` MCP tools.
- The per-space **1 write/sec** ceiling is unlikely to bite a personal MCP integration, but consider it if the AI fan-outs to many spaces in a loop.
- Per Google's guidance, validate the 32 KB message-size cap locally before calling the API.
- Pagination is already implemented correctly in `list_space_messages` (loops on `nextPageToken`).
- The filter string built in `list_space_messages` uses ISO 8601 / RFC-3339 timestamps with `AND` — this matches the documented filter DSL.

---

## 9. Verified sources

All facts in this document come from the following pages on `developers.google.com/workspace/chat` (retrieved via Context7, 2026-05-13):

- `/api/guides/auth` — scope tables and auth-type prerequisites.
- `/authenticate-authorize` — definitions of app vs user authentication.
- `/structure` and `/concepts/structure` — supported architectures and conversational patterns.
- `/limits` — per-project and per-space quotas, retry guidance.
- `/api/reference/rest/v1/spaces.messages/create` — message-create endpoint, scopes, response shape.
- `/api/reference/rest/v1/media/upload` — attachment upload endpoint and constraints.
- `/api/reference/rest/v1/spaces.messages/list` — list endpoint, filter DSL, pagination.
- `/reference/rest/v1/spaces.messages` (Thread resource) — `name` vs `threadKey` semantics.
- `/upload-media-attachments` — Python upload + attach example using `googleapiclient`.
- `/receive-respond-interactions` — retry-handling guidance for inbound HTTP events.
- `/private-messages` — threading reply mechanics.

When in doubt, re-query Context7 against `/websites/developers_google_workspace_chat` rather than relying on this document — the upstream specification is the only source of truth and can change without notice.
