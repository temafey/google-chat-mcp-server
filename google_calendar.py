"""Google Calendar API layer for the MCP server.

Mirrors the patterns in `google_chat.py`:
- credentials reuse via `get_credentials()` imported from google_chat (single token.json)
- thread-pool offload for blocking googleapiclient calls
- HttpError → {"error", "status"} dict mapping
- SAVE_TOKEN_MODE field filtering on read paths to keep response payloads small

Calendar OAuth scopes required:
- `https://www.googleapis.com/auth/calendar.readonly` — calendarList.list, freebusy
- `https://www.googleapis.com/auth/calendar.events`  — events.list/get/insert/patch/delete/quickAdd
"""

import asyncio
import datetime
import json
from typing import Any, Dict, List, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import google_chat  # imported as a module so SAVE_TOKEN_MODE is read live, not snapshotted
from google_chat import get_credentials

# Calendar-specific scopes. Merged into the global SCOPES list in google_chat.py
# so a single OAuth flow covers both Chat and Calendar.
CALENDAR_SCOPES = [
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/calendar.events',
]


# Fields kept when SAVE_TOKEN_MODE is enabled (default). Mirrors the philosophy
# in list_space_messages — strip Calendar API responses down to what an LLM
# actually needs so a "list this week's events" call doesn't blow the context
# window on unused metadata (creator, organizer email, reminders, conferenceData…).
_EVENT_KEEP_FIELDS = (
    'id',
    'summary',
    'description',
    'location',
    'start',
    'end',
    'status',
    'htmlLink',
    'recurrence',
    'recurringEventId',
    'attendees',
    'hangoutLink',
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


def _build_service(creds: Credentials):
    """Build a fresh Calendar v3 service. googleapiclient services aren't
    documented as thread-safe, so build per-call when offloading to a worker."""
    return build('calendar', 'v3', credentials=creds)


def _filter_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Strip event to the LLM-friendly subset. Attendees are reduced to
    email + responseStatus so a 30-person meeting doesn't dump 30 full records."""
    out: Dict[str, Any] = {}
    for k in _EVENT_KEEP_FIELDS:
        if k in event:
            out[k] = event[k]
    if 'attendees' in out and isinstance(out['attendees'], list):
        out['attendees'] = [
            {
                'email': a.get('email'),
                'displayName': a.get('displayName'),
                'responseStatus': a.get('responseStatus'),
                'optional': a.get('optional', False),
            }
            for a in out['attendees']
        ]
    return out


def _parse_date_or_datetime(value: str, end_of_day: bool = False) -> str:
    """Accept YYYY-MM-DD or full RFC3339 and return an RFC3339 timestamp.

    Calendar API requires a timezone offset; if the caller passes a bare
    YYYY-MM-DD we default to UTC. end_of_day=True turns "2026-05-22" into
    2026-05-22T23:59:59.999999Z so date-only ranges include the whole day.
    """
    try:
        dt = datetime.datetime.strptime(value, '%Y-%m-%d')
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return dt.replace(tzinfo=datetime.timezone.utc).isoformat()
    except ValueError:
        pass

    try:
        # fromisoformat handles "2026-05-22T10:00:00+02:00" and "...Z" (3.11+)
        normalized = value.replace('Z', '+00:00') if value.endswith('Z') else value
        dt = datetime.datetime.fromisoformat(normalized)
    except ValueError as e:
        raise ValueError(
            f"Date must be YYYY-MM-DD or RFC3339 (got {value!r})"
        ) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------

def _list_calendars_sync(creds: Credentials) -> List[Dict[str, Any]]:
    service = _build_service(creds)
    out: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"maxResults": 250}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.calendarList().list(**kwargs).execute()
        out.extend(resp.get('items', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return out


async def list_calendars() -> List[Dict[str, Any]]:
    """List calendars in the user's calendarList (paginated).

    Returns the full CalendarListEntry items — id, summary, timeZone,
    accessRole, primary flag are the load-bearing fields for downstream calls.
    """
    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    try:
        items = await asyncio.to_thread(_list_calendars_sync, creds)
    except HttpError as err:
        return [{"_error": _http_error_to_dict(err)}]

    if not google_chat.SAVE_TOKEN_MODE:
        return items

    return [
        {
            'id': c.get('id'),
            'summary': c.get('summary'),
            'description': c.get('description'),
            'timeZone': c.get('timeZone'),
            'accessRole': c.get('accessRole'),
            'primary': c.get('primary', False),
            'selected': c.get('selected', False),
        }
        for c in items
    ]


# ---------------------------------------------------------------------------
# Events — read
# ---------------------------------------------------------------------------

def _list_events_sync(
    creds: Credentials,
    calendar_id: str,
    time_min: Optional[str],
    time_max: Optional[str],
    query: Optional[str],
    single_events: bool,
    order_by: Optional[str],
    max_results_per_page: int,
) -> List[Dict[str, Any]]:
    service = _build_service(creds)
    events: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {
            "calendarId": calendar_id,
            "maxResults": max_results_per_page,
            "singleEvents": single_events,
        }
        if time_min:
            kwargs["timeMin"] = time_min
        if time_max:
            kwargs["timeMax"] = time_max
        if query:
            kwargs["q"] = query
        if order_by:
            kwargs["orderBy"] = order_by
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.events().list(**kwargs).execute()
        events.extend(resp.get('items', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return events


async def list_calendar_events(
    calendar_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    query: Optional[str] = None,
    single_events: bool = True,
    order_by: str = 'startTime',
) -> List[Dict[str, Any]]:
    """List events from a calendar within an optional date range.

    `single_events=True` expands recurring events into individual instances
    (typically what callers want for a "show me this week" view). When
    expansion is on, orderBy='startTime' is allowed; otherwise the API
    rejects it.
    """
    if not calendar_id:
        raise ValueError("calendar_id is required ('primary' for the user's main calendar)")

    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    time_min = _parse_date_or_datetime(start_date) if start_date else None
    time_max = _parse_date_or_datetime(end_date, end_of_day=True) if end_date else None

    # orderBy='startTime' requires singleEvents=True per API contract.
    effective_order_by = order_by if single_events else None

    try:
        events = await asyncio.to_thread(
            _list_events_sync,
            creds,
            calendar_id,
            time_min,
            time_max,
            query,
            single_events,
            effective_order_by,
            250,
        )
    except HttpError as err:
        raise Exception(f"Failed to list events: {_http_error_to_dict(err)}")

    if not google_chat.SAVE_TOKEN_MODE:
        return events
    return [_filter_event(e) for e in events]


async def get_calendar_event(calendar_id: str, event_id: str) -> Dict[str, Any]:
    """Get a single event by ID. Returns the filtered event (or raw if
    SAVE_TOKEN_MODE is disabled). Errors come back as {"error", "status"}."""
    if not calendar_id or not event_id:
        raise ValueError("calendar_id and event_id are required")

    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    def _get() -> Dict[str, Any]:
        service = _build_service(creds)
        return service.events().get(calendarId=calendar_id, eventId=event_id).execute()

    try:
        event = await asyncio.to_thread(_get)
    except HttpError as err:
        return _http_error_to_dict(err)

    return event if not google_chat.SAVE_TOKEN_MODE else _filter_event(event)


# ---------------------------------------------------------------------------
# Events — write
# ---------------------------------------------------------------------------

def _build_event_body(
    summary: str,
    start: str,
    end: str,
    description: Optional[str],
    location: Optional[str],
    attendees: Optional[List[str]],
    time_zone: Optional[str],
) -> Dict[str, Any]:
    """Build an Event resource body from caller-friendly inputs.

    `start` and `end` accept either YYYY-MM-DD (all-day event, uses `date`)
    or RFC3339 dateTime (uses `dateTime` + timeZone).
    """
    def _slot(value: str) -> Dict[str, Any]:
        # All-day: "YYYY-MM-DD"
        try:
            datetime.datetime.strptime(value, '%Y-%m-%d')
            return {'date': value}
        except ValueError:
            pass
        normalized = _parse_date_or_datetime(value)
        slot: Dict[str, Any] = {'dateTime': normalized}
        if time_zone:
            slot['timeZone'] = time_zone
        return slot

    body: Dict[str, Any] = {
        'summary': summary,
        'start': _slot(start),
        'end': _slot(end),
    }
    if description is not None:
        body['description'] = description
    if location is not None:
        body['location'] = location
    if attendees:
        body['attendees'] = [{'email': e} for e in attendees]
    return body


async def create_calendar_event(
    calendar_id: str,
    summary: str,
    start: str,
    end: str,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    time_zone: Optional[str] = None,
    send_updates: str = 'none',
) -> Dict[str, Any]:
    """Insert a new event. `send_updates` controls invitation emails:
    'all', 'externalOnly', or 'none' (default — silent create)."""
    if not calendar_id or not summary or not start or not end:
        raise ValueError("calendar_id, summary, start, end are required")

    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    body = _build_event_body(summary, start, end, description, location, attendees, time_zone)

    def _insert() -> Dict[str, Any]:
        service = _build_service(creds)
        return service.events().insert(
            calendarId=calendar_id,
            body=body,
            sendUpdates=send_updates,
        ).execute()

    try:
        event = await asyncio.to_thread(_insert)
    except HttpError as err:
        return _http_error_to_dict(err)
    return event if not google_chat.SAVE_TOKEN_MODE else _filter_event(event)


async def update_calendar_event(
    calendar_id: str,
    event_id: str,
    summary: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[str]] = None,
    time_zone: Optional[str] = None,
    send_updates: str = 'none',
) -> Dict[str, Any]:
    """Patch-update an event. Only the fields you pass are changed; everything
    else stays untouched (events.patch semantics).

    Pass `attendees=[]` to clear the list (omit to leave unchanged). Same for
    `description`/`location` — empty string clears, None leaves alone.
    """
    if not calendar_id or not event_id:
        raise ValueError("calendar_id and event_id are required")

    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    body: Dict[str, Any] = {}
    if summary is not None:
        body['summary'] = summary
    if description is not None:
        body['description'] = description
    if location is not None:
        body['location'] = location
    if attendees is not None:
        body['attendees'] = [{'email': e} for e in attendees]

    if start is not None or end is not None:
        # Need both start and end to be coherent on update. Fetch missing side.
        def _get_existing() -> Dict[str, Any]:
            service = _build_service(creds)
            return service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        existing: Optional[Dict[str, Any]] = None
        if start is None or end is None:
            try:
                existing = await asyncio.to_thread(_get_existing)
            except HttpError as err:
                return _http_error_to_dict(err)

        def _slot(value: str) -> Dict[str, Any]:
            try:
                datetime.datetime.strptime(value, '%Y-%m-%d')
                return {'date': value}
            except ValueError:
                pass
            slot: Dict[str, Any] = {'dateTime': _parse_date_or_datetime(value)}
            if time_zone:
                slot['timeZone'] = time_zone
            return slot

        if start is not None:
            body['start'] = _slot(start)
        elif existing is not None:
            body['start'] = existing.get('start', {})
        if end is not None:
            body['end'] = _slot(end)
        elif existing is not None:
            body['end'] = existing.get('end', {})

    if not body:
        raise ValueError("No fields supplied to update")

    def _patch() -> Dict[str, Any]:
        service = _build_service(creds)
        return service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=body,
            sendUpdates=send_updates,
        ).execute()

    try:
        event = await asyncio.to_thread(_patch)
    except HttpError as err:
        return _http_error_to_dict(err)
    return event if not google_chat.SAVE_TOKEN_MODE else _filter_event(event)


async def delete_calendar_event(
    calendar_id: str,
    event_id: str,
    send_updates: str = 'none',
) -> Dict[str, Any]:
    """Delete an event. Returns {"deleted": True, "event_id": ...} on success
    or {"error", "status"} on API failure."""
    if not calendar_id or not event_id:
        raise ValueError("calendar_id and event_id are required")

    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    def _delete() -> None:
        service = _build_service(creds)
        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates=send_updates,
        ).execute()

    try:
        await asyncio.to_thread(_delete)
    except HttpError as err:
        return _http_error_to_dict(err)
    return {"deleted": True, "calendar_id": calendar_id, "event_id": event_id}


async def quick_add_event(
    calendar_id: str,
    text: str,
    send_updates: str = 'none',
) -> Dict[str, Any]:
    """Create an event from a natural-language string via events.quickAdd
    (e.g. 'Lunch with John tomorrow at 12:30'). Calendar parses time/title."""
    if not calendar_id or not text:
        raise ValueError("calendar_id and text are required")

    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    def _quick_add() -> Dict[str, Any]:
        service = _build_service(creds)
        return service.events().quickAdd(
            calendarId=calendar_id,
            text=text,
            sendUpdates=send_updates,
        ).execute()

    try:
        event = await asyncio.to_thread(_quick_add)
    except HttpError as err:
        return _http_error_to_dict(err)
    return event if not google_chat.SAVE_TOKEN_MODE else _filter_event(event)


# ---------------------------------------------------------------------------
# Free/busy
# ---------------------------------------------------------------------------

async def query_freebusy(
    calendar_ids: List[str],
    start_date: str,
    end_date: str,
    time_zone: Optional[str] = None,
) -> Dict[str, Any]:
    """Query busy blocks across one or more calendars for a date range.

    Returns the raw `calendars` map from the API response keyed by calendar_id
    (busy intervals + any per-calendar errors). Up to 50 calendars per call
    per Google's API limit.
    """
    if not calendar_ids:
        raise ValueError("calendar_ids must be a non-empty list")
    if len(calendar_ids) > 50:
        raise ValueError("Calendar API allows at most 50 calendars per freebusy query")

    creds = get_credentials()
    if not creds:
        raise Exception("No valid credentials found. Please authenticate first.")

    body: Dict[str, Any] = {
        'timeMin': _parse_date_or_datetime(start_date),
        'timeMax': _parse_date_or_datetime(end_date, end_of_day=True),
        'items': [{'id': cid} for cid in calendar_ids],
    }
    if time_zone:
        body['timeZone'] = time_zone

    def _query() -> Dict[str, Any]:
        service = _build_service(creds)
        return service.freebusy().query(body=body).execute()

    try:
        resp = await asyncio.to_thread(_query)
    except HttpError as err:
        return _http_error_to_dict(err)

    return {
        'timeMin': resp.get('timeMin'),
        'timeMax': resp.get('timeMax'),
        'calendars': resp.get('calendars', {}),
    }
