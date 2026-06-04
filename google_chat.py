import asyncio
import os
import json
import mimetypes
import tempfile
import datetime
from typing import Any, List, Dict, Optional, Tuple
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from pathlib import Path

# If modifying these scopes, delete the file token.json and re-authenticate.
# `chat.memberships.readonly` is needed by list_space_members / find_users_by_name
# so the server can resolve user IDs ↔ display names (Chat-native, instead of
# People API which doesn't work under user OAuth here).
# Calendar scopes power google_calendar.py — `calendar.readonly` covers
# calendarList.list + freebusy.query; `calendar.events` covers events.* CRUD.
SCOPES = [
    'https://www.googleapis.com/auth/chat.spaces.readonly',
    'https://www.googleapis.com/auth/chat.messages',
    'https://www.googleapis.com/auth/chat.memberships.readonly',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/calendar.events',
]

# Cache for user display names: {user_id: display_name}
_user_display_name_cache: Dict[str, str] = {}
DEFAULT_CALLBACK_URL = os.environ.get(
    'GOOGLE_OAUTH_REDIRECT_URI', 'http://localhost:8000/auth/callback'
)


def get_client_config() -> dict:
    """Return OAuth client config from credentials.json or environment variables.

    credentials.json takes precedence. Falls back to GOOGLE_OAUTH_CLIENT_ID +
    GOOGLE_OAUTH_CLIENT_SECRET env vars when the file is absent.
    """
    creds_file = Path('credentials.json')
    if creds_file.exists():
        with open(creds_file) as f:
            return json.load(f)

    client_id = os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
    client_secret = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET')
    if not client_id or not client_secret:
        raise FileNotFoundError(
            "credentials.json not found and GOOGLE_OAUTH_CLIENT_ID / "
            "GOOGLE_OAUTH_CLIENT_SECRET environment variables are not set."
        )

    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [DEFAULT_CALLBACK_URL],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
DEFAULT_TOKEN_PATH = 'token.json'

# Store credentials info
token_info = {
    'credentials': None,
    'last_refresh': None,
    'token_path': DEFAULT_TOKEN_PATH
}

def set_token_path(path: str) -> None:
    """Set the global token path for OAuth storage.
    
    Args:
        path: Path where the token should be stored
    """
    token_info['token_path'] = path

# Global flag for message filtering
SAVE_TOKEN_MODE = True

def set_save_token_mode(enabled: bool) -> None:
    """Set whether to filter message fields to save tokens.

    Args:
        enabled: True to enable filtering, False to disable
    """
    global SAVE_TOKEN_MODE
    SAVE_TOKEN_MODE = enabled

# Maximum bytes for the `text` field of a Google Chat message.
# Source: docs/google-chat-api-guide.md §4 ("Size").
MAX_MESSAGE_TEXT_BYTES = 32_000

# Root directory the AI is allowed to upload files from. Restricting this
# blocks `..` traversal and prevents the AI from exfiltrating arbitrary files
# such as /etc/passwd by passing them to upload_attachment.
_upload_dir: Path = Path.cwd().resolve()

def set_upload_dir(path: str) -> None:
    """Set the root directory permitted for attachment uploads."""
    global _upload_dir
    _upload_dir = Path(path).expanduser().resolve()

def get_upload_dir() -> Path:
    return _upload_dir

def save_credentials(creds: Credentials, token_path: Optional[str] = None) -> None:
    """Save credentials to file and update in-memory cache.
    
    Args:
        creds: The credentials to save
        token_path: Path to save the token file
    """
    # Use configured token path if none provided
    if token_path is None:
        token_path = token_info['token_path']
    
    # Save to file
    token_path = Path(token_path)
    with open(token_path, 'w') as token:
        token.write(creds.to_json())
    
    # Update in-memory cache
    token_info['credentials'] = creds
    token_info['last_refresh'] = datetime.datetime.utcnow()

def get_credentials(token_path: Optional[str] = None) -> Optional[Credentials]:
    """Gets valid user credentials from storage or memory.
    
    Args:
        token_path: Optional path to token file. If None, uses the configured path.
    
    Returns:
        Credentials object or None if no valid credentials exist
    """
    if token_path is None:
        token_path = token_info['token_path']
    
    creds = token_info['credentials']
    
    # If no credentials in memory, try to load from file.
    # IMPORTANT: do not pass SCOPES here — it forces the library to expect
    # those exact scopes on refresh, which raises "Scope has changed" the
    # moment we expand SCOPES (e.g. adding chat.memberships.readonly).
    # Old tokens keep working for endpoints whose scope they were granted;
    # endpoints needing the new scope return 403 until the user re-auths.
    if not creds:
        token_path = Path(token_path)
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path))
            token_info['credentials'] = creds

    # If we have credentials that need refresh
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds, token_path)
        except Exception:
            return None
    
    return creds if (creds and creds.valid) else None

async def refresh_token(token_path: Optional[str] = None) -> Tuple[bool, str]:
    """Attempt to refresh the current token.
    
    Args:
        token_path: Path to the token file. If None, uses the configured path.
    
    Returns:
        Tuple of (success: bool, message: str)
    """
    if token_path is None:
        token_path = token_info['token_path']
        
    try:
        creds = token_info['credentials']
        if not creds:
            token_path = Path(token_path)
            if not token_path.exists():
                return False, "No token file found"
            creds = Credentials.from_authorized_user_file(str(token_path))

        if not creds.refresh_token:
            return False, "No refresh token available"
        
        creds.refresh(Request())
        save_credentials(creds, token_path)
        return True, "Token refreshed successfully"
    except Exception as e:
        return False, f"Failed to refresh token: {str(e)}"

def get_user_display_name(sender: Dict, creds: Optional[Credentials] = None) -> str:
    """Return a human-readable display name for a Chat sender — cache only, no network.

    Resolution order:
        1. Module-level cache (`_user_display_name_cache`). Populated by
           `_resolve_me_sync` (for self) and `list_space_members` /
           `find_users_by_name` (for any user the caller has met via a
           space membership listing).
        2. Inline `displayName` on the sender object (sometimes present for
           bots; rarely for humans in `messages.list` responses).
        3. Synthesized `"Bot (xxxxxxxx...)"` for BOT senders.
        4. Fallback: the raw `users/<id>` string.

    The earlier People-API code path was removed because the current OAuth
    scopes (`userinfo.profile` only) do not authorize People-API user
    lookups — every call 403'd and was cached as the user_id, which made
    the result confusing.
    """
    user_id = sender.get('name', '') if sender else ''
    if not user_id:
        return 'Unknown'

    if user_id in _user_display_name_cache:
        return _user_display_name_cache[user_id]

    if sender.get('displayName'):
        _user_display_name_cache[user_id] = sender['displayName']
        return sender['displayName']

    if sender.get('type') == 'BOT':
        short_id = user_id.replace('users/', '') or 'unknown'
        display_name = f"Bot ({short_id[:8]}...)"
        _user_display_name_cache[user_id] = display_name
        return display_name

    return user_id


# MCP functions
async def list_chat_spaces() -> List[Dict]:
    """Lists all Google Chat spaces the authenticated user is a member of.

    Paginates through `spaces.list` so the result is complete even when the
    user is in more than one page of spaces (pageSize max is 100).
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")

        service = build('chat', 'v1', credentials=creds)
        all_spaces: List[Dict] = []
        page_token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"pageSize": 100}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.spaces().list(**kwargs).execute()
            all_spaces.extend(resp.get('spaces', []))
            page_token = resp.get('nextPageToken')
            if not page_token:
                break
        return all_spaces
    except Exception as e:
        raise Exception(f"Failed to list chat spaces: {str(e)}")

async def list_space_messages(space_name: str, 
                            start_date: Optional[datetime.datetime] = None,
                            end_date: Optional[datetime.datetime] = None) -> List[Dict]:
    """Lists messages from a specific Google Chat space with optional time filtering.
    
    Args:
        space_name: The name/identifier of the space to fetch messages from
        start_date: Optional start datetime for filtering messages. If provided without end_date,
                   will query messages for the entire day of start_date
        end_date: Optional end datetime for filtering messages. Only used if start_date is also provided
    
    Returns:
        List of message objects from the space matching the time criteria
        
    Raises:
        Exception: If authentication fails or API request fails
    """
    try:
        creds = get_credentials()
        if not creds:
            raise Exception("No valid credentials found. Please authenticate first.")
            
        service = build('chat', 'v1', credentials=creds)
        
        # Prepare filter string based on provided dates
        filter_str = None
        if start_date:
            if end_date:
                # Format for date range query
                filter_str = f"createTime > \"{start_date.isoformat()}\" AND createTime < \"{end_date.isoformat()}\""
            else:
                # For single day query, set range from start of day to end of day
                day_start = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = day_start + datetime.timedelta(days=1)
                filter_str = f"createTime > \"{day_start.isoformat()}\" AND createTime < \"{day_end.isoformat()}\""
        
        # Make API request with pagination
        messages = []
        page_token = None
        
        while True:
            list_args = {
                'parent': space_name,
                'pageSize': 100
            }
            if filter_str:
                list_args['filter'] = filter_str
            if page_token:
                list_args['pageToken'] = page_token
                
            response = service.spaces().messages().list(**list_args).execute()
            
            # Extend messages list with current page results
            current_page_messages = response.get('messages', [])
            if current_page_messages:
                messages.extend(current_page_messages)
            
            page_token = response.get('nextPageToken')
            if not page_token:
                break

        if not SAVE_TOKEN_MODE:
            return messages

        filtered_messages = []
        for msg in messages:
            sender = msg.get('sender', {})
            display_name = get_user_display_name(sender, creds) if sender else 'Unknown'

            filtered_msg = {
                'sender': display_name,
                'createTime': msg.get('createTime'),
                'text': msg.get('text'),
                'thread': msg.get('thread')
            }
            filtered_messages.append(filtered_msg)

        return filtered_messages

    except Exception as e:
        raise Exception(f"Failed to list messages in space: {str(e)}")


def _validate_space_name(space_name: str) -> None:
    if not isinstance(space_name, str) or not space_name.startswith("spaces/") or space_name == "spaces/":
        raise ValueError(
            f"space_name must look like 'spaces/<id>' (got {space_name!r})"
        )


def _validate_thread_name(space_name: str, thread_name: str) -> None:
    expected_prefix = f"{space_name}/threads/"
    if not thread_name.startswith(expected_prefix) or thread_name == expected_prefix:
        raise ValueError(
            f"thread_name must look like '{expected_prefix}<id>' (got {thread_name!r})"
        )


def _http_error_to_dict(err: HttpError) -> Dict[str, Any]:
    status = getattr(err.resp, "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    try:
        payload = json.loads(err.content.decode("utf-8")) if err.content else {}
        message = (payload.get("error") or {}).get("message") or str(err)
    except (ValueError, AttributeError):
        message = str(err)
    return {"error": message, "status": status}


async def send_message(
    space_name: str,
    text: str,
    thread_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Post a text message to a Google Chat space, optionally as a thread reply.

    When thread_name is supplied, REPLY_MESSAGE_OR_FAIL is used so a stale or
    wrong thread name returns a 404 instead of silently starting a new thread.
    """
    _validate_space_name(space_name)
    if thread_name is not None:
        _validate_thread_name(space_name, thread_name)
    if text is None:
        text = ""
    if len(text.encode("utf-8")) > MAX_MESSAGE_TEXT_BYTES:
        raise ValueError(
            f"text exceeds Google Chat's {MAX_MESSAGE_TEXT_BYTES}-byte limit"
        )

    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    service = build('chat', 'v1', credentials=creds)
    body: Dict[str, Any] = {"text": text}
    create_kwargs: Dict[str, Any] = {"parent": space_name, "body": body}
    if thread_name:
        body["thread"] = {"name": thread_name}
        create_kwargs["messageReplyOption"] = "REPLY_MESSAGE_OR_FAIL"

    try:
        return service.spaces().messages().create(**create_kwargs).execute()
    except HttpError as err:
        return _http_error_to_dict(err)


async def upload_attachment(
    space_name: str,
    file_path: str,
    text: Optional[str] = None,
    thread_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload a local file and post it as an attachment in one message.

    file_path must resolve inside the configured upload directory (see
    set_upload_dir). This is the boundary that prevents an AI client from
    asking the server to upload arbitrary host files.
    """
    _validate_space_name(space_name)
    if thread_name is not None:
        _validate_thread_name(space_name, thread_name)

    resolved = Path(file_path).expanduser().resolve()
    upload_root = _upload_dir
    try:
        resolved.relative_to(upload_root)
    except ValueError:
        raise ValueError(
            f"file_path {str(resolved)!r} is outside the allowed upload "
            f"directory {str(upload_root)!r}"
        )
    if not resolved.is_file():
        raise FileNotFoundError(f"No such file: {str(resolved)!r}")

    caption = text if text is not None else ""
    if len(caption.encode("utf-8")) > MAX_MESSAGE_TEXT_BYTES:
        raise ValueError(
            f"text exceeds Google Chat's {MAX_MESSAGE_TEXT_BYTES}-byte limit"
        )

    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    mimetype = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
    media = MediaFileUpload(str(resolved), mimetype=mimetype)
    service = build('chat', 'v1', credentials=creds)

    try:
        uploaded = service.media().upload(
            parent=space_name,
            body={"filename": resolved.name},
            media_body=media,
        ).execute()
    except HttpError as err:
        return _http_error_to_dict(err)

    body: Dict[str, Any] = {"text": caption, "attachment": [uploaded]}
    create_kwargs: Dict[str, Any] = {"parent": space_name, "body": body}
    if thread_name:
        body["thread"] = {"name": thread_name}
        create_kwargs["messageReplyOption"] = "REPLY_MESSAGE_OR_FAIL"

    try:
        return service.spaces().messages().create(**create_kwargs).execute()
    except HttpError as err:
        return _http_error_to_dict(err)


def _list_messages_sync(
    creds: Credentials,
    space_name: str,
    start_iso: Optional[str],
    end_iso: Optional[str],
) -> List[Dict]:
    """Synchronous paginated `messages.list` — runs in a worker thread.

    Builds a fresh `chat` service per call so it is safe to invoke from
    multiple threads concurrently (googleapiclient services are not
    documented as thread-safe).
    """
    service = build('chat', 'v1', credentials=creds)
    filter_parts: List[str] = []
    if start_iso:
        filter_parts.append(f'createTime > "{start_iso}"')
    if end_iso:
        filter_parts.append(f'createTime < "{end_iso}"')
    filter_str = " AND ".join(filter_parts) if filter_parts else None

    messages: List[Dict] = []
    page_token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"parent": space_name, "pageSize": 100}
        if filter_str:
            kwargs["filter"] = filter_str
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.spaces().messages().list(**kwargs).execute()
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return messages


def _resolve_me_sync(creds: Credentials) -> str:
    """Return the authenticated user's Chat sender ID, i.e. 'users/<id>'.

    Uses the OAuth2 userinfo endpoint (covered by `userinfo.profile`) — the
    numeric `id` it returns is the same Google Account ID that Chat uses in
    `sender.name`. As a side effect, seeds the display-name cache with the
    user's own name so search results show "Artem Onyshchenko" rather than
    the raw `users/<id>`.
    """
    oauth2 = build('oauth2', 'v2', credentials=creds)
    info = oauth2.userinfo().get().execute()
    user_id_raw = info.get("id")
    if not user_id_raw:
        raise Exception("Could not resolve current user ID from userinfo endpoint")
    user_id = f"users/{user_id_raw}"
    display = info.get("name") or info.get("given_name")
    if display:
        _user_display_name_cache[user_id] = display
    return user_id


async def search_chat_messages(
    sender: str,
    start_date: datetime.datetime,
    end_date: Optional[datetime.datetime] = None,
    space_names: Optional[List[str]] = None,
    max_concurrency: int = 10,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Search Chat messages across spaces by sender + date range.

    `sender` MUST be either `"me"` (resolved via userinfo) or `"users/<id>"`.
    To find someone by display name, call `find_users_by_name` first — that
    tool resolves a name substring against space memberships and returns
    `user_id` values you can pass here.

    Google Chat's REST API does NOT support a server-side `sender` filter
    (only `createTime` and `thread.name`). This function fans out per-space
    `messages.list` calls with a `createTime` filter in parallel (bounded by
    `max_concurrency`) and filters by sender client-side.
    """
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    if sender == "me":
        sender = await asyncio.to_thread(_resolve_me_sync, creds)
    if not sender.startswith("users/"):
        raise ValueError(
            f"sender must be 'me' or 'users/<id>' (got {sender!r}). "
            "To search by display name, call find_users_by_name first."
        )

    if end_date is None:
        day_start = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + datetime.timedelta(days=1)
        start_iso, end_iso = day_start.isoformat(), day_end.isoformat()
    else:
        start_iso, end_iso = start_date.isoformat(), end_date.isoformat()

    if space_names:
        for sp in space_names:
            _validate_space_name(sp)
        spaces_to_scan: List[Dict] = [{"name": sp, "displayName": None} for sp in space_names]
    else:
        spaces_to_scan = await list_chat_spaces()

    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def _scan(sp: Dict) -> Tuple[Dict, List[Dict], Optional[Dict[str, Any]]]:
        async with sem:
            try:
                msgs = await asyncio.to_thread(
                    _list_messages_sync, creds, sp["name"], start_iso, end_iso
                )
                return sp, msgs, None
            except HttpError as err:
                return sp, [], _http_error_to_dict(err)
            except Exception as err:
                return sp, [], {"error": str(err), "status": None}

    scan_results = await asyncio.gather(*[_scan(sp) for sp in spaces_to_scan])

    hits: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for sp, msgs, err in scan_results:
        if err:
            errors.append({"space_name": sp["name"], **err})
            continue
        for m in msgs:
            sender_obj = m.get("sender") or {}
            sender_id = sender_obj.get("name", "")
            if sender_id != sender:
                continue
            display = get_user_display_name(sender_obj, creds) if sender_obj else "Unknown"
            hits.append({
                "space_name": sp["name"],
                "space_display_name": sp.get("displayName"),
                "message_name": m.get("name"),
                "create_time": m.get("createTime"),
                "sender_id": sender_id,
                "sender_display_name": display,
                "text": m.get("text"),
                "thread_name": (m.get("thread") or {}).get("name"),
            })

    hits.sort(key=lambda h: h.get("create_time") or "", reverse=True)
    if limit is not None and limit > 0:
        hits = hits[:limit]

    return {
        "sender": sender,
        "start_date": start_iso,
        "end_date": end_iso,
        "spaces_scanned": len(spaces_to_scan),
        "spaces_failed": len(errors),
        "errors": errors,
        "total_matches": len(hits),
        "results": hits,
    }


def _list_space_members_sync(
    creds: Credentials,
    space_name: str,
) -> List[Dict]:
    """Synchronous paginated `spaces.members.list` — runs in a worker thread.

    Builds a fresh `chat` service per call (googleapiclient services aren't
    documented as thread-safe). Returns the raw `memberships` list — the
    caller can pull `member.name` / `member.displayName` from each.
    """
    service = build('chat', 'v1', credentials=creds)
    memberships: List[Dict] = []
    page_token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"parent": space_name, "pageSize": 1000}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.spaces().members().list(**kwargs).execute()
        memberships.extend(resp.get("memberships", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return memberships


def _membership_to_user_record(m: Dict) -> Optional[Dict[str, Any]]:
    """Extract a uniform user record from a Membership. Returns None for
    membership entries that aren't an individual user (e.g. group members).
    Side effect: seeds `_user_display_name_cache` when displayName is present.
    """
    member = m.get('member') or {}
    user_id = member.get('name')
    if not user_id:
        return None
    display_name = member.get('displayName') or user_id
    if display_name and display_name != user_id:
        _user_display_name_cache[user_id] = display_name
    return {
        "user_id": user_id,
        "display_name": display_name,
        "type": member.get('type', 'HUMAN'),
        "is_anonymous": member.get('isAnonymous', False),
        "state": m.get('state'),
        "role": m.get('role'),
    }


async def list_space_members(space_name: str) -> Dict[str, Any]:
    """List members of a Google Chat space (humans + bots, skips group rows).

    Requires the `chat.memberships.readonly` scope — if your `token.json`
    was issued before this scope was added, you'll see a 403 error dict
    and need to re-authenticate.
    """
    _validate_space_name(space_name)
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    try:
        memberships = await asyncio.to_thread(_list_space_members_sync, creds, space_name)
    except HttpError as err:
        return {"space_name": space_name, **_http_error_to_dict(err), "members": []}

    members: List[Dict[str, Any]] = []
    for m in memberships:
        rec = _membership_to_user_record(m)
        if rec is not None:
            members.append(rec)
    return {
        "space_name": space_name,
        "member_count": len(members),
        "members": members,
    }


async def find_users_by_name(
    name_query: str,
    space_names: Optional[List[str]] = None,
    max_concurrency: int = 10,
) -> Dict[str, Any]:
    """Resolve a display-name substring to one or more `users/<id>` values.

    Scans space memberships in parallel (every space the user is in, or just
    the supplied subset). Matching is **case-insensitive substring** on the
    member's `displayName`. Dedups by user_id and reports which spaces each
    match was found in — handy when there are several "John"s.

    Typical flow:
        result = find_users_by_name("artem")
        # → pick result["matches"][0]["user_id"], then:
        search_chat_messages(sender=<id>, start_date=...)
    """
    if not name_query or not name_query.strip():
        raise ValueError("name_query must be a non-empty string")

    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    if space_names:
        for sp in space_names:
            _validate_space_name(sp)
        spaces_to_scan: List[Dict] = [{"name": sp, "displayName": None} for sp in space_names]
    else:
        spaces_to_scan = await list_chat_spaces()

    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def _scan(sp: Dict) -> Tuple[Dict, List[Dict], Optional[Dict[str, Any]]]:
        async with sem:
            try:
                ms = await asyncio.to_thread(_list_space_members_sync, creds, sp["name"])
                return sp, ms, None
            except HttpError as err:
                return sp, [], _http_error_to_dict(err)
            except Exception as err:
                return sp, [], {"error": str(err), "status": None}

    scan_results = await asyncio.gather(*[_scan(sp) for sp in spaces_to_scan])

    query_lower = name_query.lower()
    matches: Dict[str, Dict[str, Any]] = {}
    errors: List[Dict[str, Any]] = []
    for sp, memberships, err in scan_results:
        if err:
            errors.append({"space_name": sp["name"], **err})
            continue
        for m in memberships:
            rec = _membership_to_user_record(m)
            if rec is None:
                continue
            display = rec["display_name"] or ""
            # Skip records that don't have a real display name (just the user_id).
            if display == rec["user_id"]:
                continue
            if query_lower not in display.lower():
                continue
            entry = matches.get(rec["user_id"])
            if entry is None:
                entry = {
                    "user_id": rec["user_id"],
                    "display_name": display,
                    "type": rec["type"],
                    "spaces": [],
                }
                matches[rec["user_id"]] = entry
            entry["spaces"].append({
                "name": sp["name"],
                "display_name": sp.get("displayName"),
            })

    return {
        "query": name_query,
        "spaces_scanned": len(spaces_to_scan),
        "spaces_failed": len(errors),
        "errors": errors,
        "match_count": len(matches),
        "matches": sorted(matches.values(), key=lambda r: r["display_name"].lower()),
    }


# Triage config — the chat-triage assistant persists its identity cache here.
# T1.4 owns the full schema; whoami() only writes me_user_id + me_display_name
# and MERGES into whatever is already on disk (never overwrites other keys).
TRIAGE_CONFIG_PATH = Path('~/.claude-orchestrator/gchat-triage/config.json').expanduser()


def _write_triage_identity(me_user_id: str, me_display_name: Optional[str]) -> None:
    """Atomically merge the two identity keys into the triage config.

    Reads any existing config, updates ONLY `me_user_id` + `me_display_name`,
    and writes via a temp file + `os.replace` so a crash or concurrent writer
    can never leave a partial/corrupt config.json. If the file or directory is
    missing it is created with just these two keys — the rest of the schema is
    deliberately left to T1.4.
    """
    config_path = TRIAGE_CONFIG_PATH
    config_dir = config_path.parent
    config_dir.mkdir(parents=True, exist_ok=True)

    data: Dict[str, Any] = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            # Corrupt/unreadable existing file: fall back to a fresh object
            # rather than crashing. We still only own the two identity keys.
            data = {}

    data['me_user_id'] = me_user_id
    data['me_display_name'] = me_display_name

    fd, tmp_path = tempfile.mkstemp(dir=str(config_dir), prefix='.config-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as tmp:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, config_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def whoami() -> Dict[str, Any]:
    """Resolve the authenticated user's Chat identity and cache it for triage.

    Resolves `users/<id>` via the OAuth2 userinfo endpoint (reusing
    `_resolve_me_sync`, the same logic `search_chat_messages` uses for
    `sender="me"`), looks up a best-effort display name from the cache that
    `_resolve_me_sync` seeds, and persists both into the triage config via an
    atomic read-modify-write of only those two keys.

    Returns:
        {"me_user_id": "users/<id>", "me_display_name": "<name or null>"}
    """
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    me_user_id = await asyncio.to_thread(_resolve_me_sync, creds)
    # _resolve_me_sync seeds _user_display_name_cache with the user's own name
    # when userinfo returns one; None is an acceptable fallback per contract.
    me_display_name = _user_display_name_cache.get(me_user_id)

    await asyncio.to_thread(_write_triage_identity, me_user_id, me_display_name)

    return {"me_user_id": me_user_id, "me_display_name": me_display_name}


