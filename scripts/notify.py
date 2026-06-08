"""scripts/notify.py — headless notification DISPATCHER for the Chat Triage
Assistant (T2.1).

ZERO Claude/LLM calls. Every decision here is deterministic. The module:

1. Decides which ledger items deserve a notification *right now*
   (:func:`_is_new_candidate` baseline rule + overdue-promise escalations).
2. Honours the R5 kill switch (``config.is_active``) and quiet hours
   (``config.quiet_hours``, wrap-around aware via :mod:`zoneinfo`).
3. Dispatches a single digest to every enabled :class:`Sender`
   (ConsoleSender always; GCInboxSender behind its ``enabled()`` gate).
4. Persists ``last_notified`` / ``promise_escalated_at`` *without* a status
   transition or a spurious history entry.

The ONLY outward/network write anywhere in this module is
``google_chat.send_message`` inside :meth:`GCInboxSender.send`, and it is gated
by :meth:`GCInboxSender.enabled`.

SEAM (T2.1 step 4) — see :func:`_set_item_fields` and the module docstring note
below. ``scripts/store.py`` exposes no dedicated *no-history* field setter:
``set_status`` always forces a ``status:*`` history entry and a status change.
Rather than hack ``set_status`` (forbidden) or edit ``store.py`` (forbidden), we
set fields on the public item dict (items ARE plain dicts under
``store["items"][id]`` — documented contract) and persist via ``store.save``.
If/when ``store.set_fields(store, id, now=None, **fields)`` is added, this module
prefers it automatically — no change needed here. See the SEAM report.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import sys
from abc import ABC, abstractmethod
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

# ``scripts/`` is not an installed package; make sibling modules importable and
# put the repo root on the path so ``google_chat`` resolves regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT))

import config  # noqa: E402  (scripts/config.py)
import store  # noqa: E402  (scripts/store.py)
import templates  # noqa: E402  (scripts/templates.py — config-driven render engine)
import google_chat  # noqa: E402  (repo-root google_chat.py)


# --------------------------------------------------------------------------- #
# Time helpers.
# --------------------------------------------------------------------------- #
def _iso(dt: datetime) -> str:
    """ISO-8601 UTC string (``...Z``, second precision) — matches store.py."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso(value) -> datetime | None:
    """Parse an ISO-8601 string (``...Z`` or offset) to aware UTC; None on junk."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# English month abbreviations — the deterministic default for human_due when no locale
# table is supplied (avoids the C-locale-dependent strftime("%b")).
_MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _plural_index(n: int, rule: str) -> int:
    """CLDR-ish plural category index for ``n`` under ``rule``.

    'slavic' (ru/uk): 0=one, 1=few, 2=many. Anything else: 0=one, 1=other.
    """
    if rule == "slavic":
        if n % 10 == 1 and n % 100 != 11:
            return 0  # one
        if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
            return 1  # few
        return 2  # many
    return 0 if n == 1 else 1


def _pick_form(value, n: int, rule: str) -> str:
    """Choose a plural form. ``value`` is a string (no plural) or a list of forms.

    The chosen index is clamped to the list length, so a 1- or 2-form list still works
    under a 3-category rule.
    """
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        return value[min(_plural_index(n, rule), len(value) - 1)]
    return value


def relative_time(iso_str, now: datetime | None, loc: dict | None = None) -> str:
    """Coarse human relative age: 'just now' / '5m ago' / '2h ago' / '3d ago'.

    ``now`` is INJECTED (no wall-clock read here) so callers stay testable.
    Future/garbage timestamps collapse to the 'just now' label. When ``loc`` is given
    its ``rel_just_now`` / ``rel_min`` / ``rel_hour`` / ``rel_day`` templates (each a
    string or a list of plural forms, with a ``$count`` placeholder) and ``plural`` rule
    drive the wording; without ``loc`` the default is abbreviated English.
    """
    dt = _parse_iso(iso_str)
    if dt is None or now is None:
        return ""
    loc = loc or {}
    rule = loc.get("plural", "en")

    def fmt(key: str, n: int, default: str) -> str:
        tmpl = _pick_form(loc.get(key, default), n, rule)
        return Template(tmpl).safe_substitute(count=n)

    secs = (now.astimezone(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return loc.get("rel_just_now", "just now")
    mins = int(secs // 60)
    if mins < 60:
        return fmt("rel_min", mins, "${count}m ago")
    hours = int(secs // 3600)
    if hours < 24:
        return fmt("rel_hour", hours, "${count}h ago")
    days = int(secs // 86400)
    return fmt("rel_day", days, "${count}d ago")


def absolute_time(iso_str, now: datetime | None, loc: dict | None = None) -> str:
    """Absolute send time of the message, e.g. '04 Jun 10:00'.

    Unlike :func:`relative_time` this never goes stale once the (static) digest
    lands in Telegram / Google Chat — it shows WHEN the message was actually sent,
    not its age at send-time. The timestamp is rendered in ``now``'s timezone
    (production: the config tz injected via :func:`_resolve_now`), falling back to
    UTC when ``now`` is naive / None. Month abbreviations come from ``loc['months']``
    (12-entry list) when supplied, else English. Empty string on a junk/absent date.
    """
    dt = _parse_iso(iso_str)
    if dt is None:
        return ""
    loc = loc or {}
    tz = now.tzinfo if (now is not None and now.tzinfo is not None) else timezone.utc
    local = dt.astimezone(tz)
    months = loc.get("months") or _MONTHS_EN
    mon = months[local.month - 1] if len(months) == 12 else _MONTHS_EN[local.month - 1]
    return f"{local.day:02d} {mon} {local.hour:02d}:{local.minute:02d}"


def human_due(iso_str, loc: dict | None = None, now: datetime | None = None) -> str:
    """Human due date, e.g. '04 Jun 18:00'.

    Rendered in ``now``'s timezone (production: the config tz via :func:`_resolve_now`)
    so it matches the wall clock the user reads it on; falls back to UTC when ``now``
    is naive / None. Month abbreviations come from ``loc['months']`` (12-entry list)
    when supplied, else English. The localized ``loc['no_due']`` (default
    '(no due date)') is returned when the date is absent; an unparseable value falls
    back to its raw string.
    """
    loc = loc or {}
    if not iso_str:
        return loc.get("no_due", "(no due date)")
    dt = _parse_iso(iso_str)
    if dt is None:
        return str(iso_str)
    tz = now.tzinfo if (now is not None and now.tzinfo is not None) else timezone.utc
    dt = dt.astimezone(tz)
    months = loc.get("months") or _MONTHS_EN
    mon = months[dt.month - 1] if len(months) == 12 else _MONTHS_EN[dt.month - 1]
    return f"{dt.day:02d} {mon} {dt.hour:02d}:{dt.minute:02d}"


def _parse_hhmm(value: str) -> dtime:
    """Parse ``"HH:MM"`` into a :class:`datetime.time`."""
    hh, _, mm = value.partition(":")
    return dtime(hour=int(hh), minute=int(mm))


def _tz_for(cfg: dict) -> ZoneInfo:
    return ZoneInfo((cfg.get("quiet_hours") or {}).get("tz") or "UTC")


def _resolve_now(cfg: dict, now=None) -> datetime:
    """Return an aware ``now``; default to wall clock in the config tz."""
    tz = _tz_for(cfg)
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now


def in_quiet_hours(now: datetime, cfg: dict) -> bool:
    """Is ``now`` inside ``[quiet_hours.start, quiet_hours.end)`` (config tz)?

    Handles the wrap-around window (e.g. ``22:00`` → ``08:00`` spans midnight).
    """
    qh = cfg.get("quiet_hours") or {}
    start_s, end_s = qh.get("start"), qh.get("end")
    if not start_s or not end_s:
        return False
    local = now.astimezone(_tz_for(cfg)).time()
    start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
    if start <= end:
        return start <= local < end
    # Wrap-around: inside if at/after start OR before end.
    return local >= start or local < end


# --------------------------------------------------------------------------- #
# Notifiability (deterministic — no LLM).
# --------------------------------------------------------------------------- #
def _matches_baseline(item: dict, cfg: dict) -> bool:
    """BASELINE: direct_dm OR vip sender OR urgency keyword in text."""
    if item.get("trigger") == "direct_dm":
        return True
    if item.get("sender_id") in set(cfg.get("vip_senders") or []):
        return True
    text = (item.get("text") or "").lower()
    for kw in cfg.get("urgency_keywords") or []:
        if kw and kw.lower() in text:
            return True
    return False


def _is_new_candidate(item: dict, cfg: dict) -> bool:
    """NEW-notify candidate: status==new AND never notified AND baseline."""
    return (
        item.get("status") == "new"
        and item.get("last_notified") is None
        and _matches_baseline(item, cfg)
    )


def new_candidates(store_data: dict, cfg: dict) -> list:
    return [
        it
        for it in store_data.get("items", {}).values()
        if _is_new_candidate(it, cfg)
    ]


def escalation_candidates(store_data: dict, now: datetime) -> list:
    """Overdue promises not yet escalated (``promise_escalated_at`` unset).

    Fires even when ``last_notified`` is already set — the safety net.
    """
    return [
        it
        for it in store.overdue_promises(store_data, now=now)
        if it.get("promise_escalated_at") is None
    ]


def pinned_candidates(store_data: dict) -> list:
    """OPEN items the user has pinned (``pinned`` truthy).

    These RIDE ALONG on any digest that is already firing (see :func:`run_once`):
    they never force a lone send, are never marked notified, and reappear every
    cycle until unpinned. ``closed`` / ``ignored`` items are not open, so a pin on
    a finished item is silently inert.
    """
    return [it for it in store.open_items(store_data) if it.get("pinned")]


# --------------------------------------------------------------------------- #
# Sender plugin architecture.
# --------------------------------------------------------------------------- #
class Sender(ABC):
    """A notification channel. Keep the registry (``build_senders``) trivially
    extensible — Telegram / Windows-toast senders drop in here in Wave E."""

    name: str = "sender"

    @abstractmethod
    def enabled(self, cfg: dict) -> bool:  # pragma: no cover - interface
        ...

    @abstractmethod
    def send(self, new_items: list, esc_items: list, now) -> bool:  # pragma: no cover
        """Render this channel's OWN text from the items and deliver it.

        Each sender owns its formatting dialect (plain / gchat / tg_html) so a
        single store snapshot renders differently per channel. ``now`` is the
        injected dispatch clock used for relative timestamps.
        """
        ...


class ConsoleSender(Sender):
    """Always available; prints the PLAIN Card digest to stdout (cron logs)."""

    name = "console"

    def __init__(self, cfg: dict | None = None):
        self._templates = (cfg or {}).get("templates")

    def enabled(self, cfg: dict) -> bool:
        return True

    def send(self, new_items: list, esc_items: list, now) -> bool:
        print(build_digest(new_items, esc_items, now=now, templates_cfg=self._templates))
        return True


class GCInboxSender(Sender):
    """Posts the digest to a Google Chat space via ``google_chat.send_message``.

    Enabled iff ``channels.gc_inbox.enabled`` AND a ``space_name`` is set. This
    is the ONLY outward/network write call in the module.
    """

    name = "gc_inbox"

    def __init__(self, cfg: dict | None = None):
        gc = ((cfg or {}).get("channels") or {}).get("gc_inbox") or {}
        self._space_name = gc.get("space_name")
        self._templates = (cfg or {}).get("templates")

    def enabled(self, cfg: dict) -> bool:
        gc = (cfg.get("channels") or {}).get("gc_inbox") or {}
        return bool(gc.get("enabled")) and bool(gc.get("space_name"))

    def send(self, new_items: list, esc_items: list, now) -> bool:
        # Render the Google-Chat dialect (``*bold*`` + ``<url|label>`` links).
        text = render_card(
            new_items, esc_items, now=now, mode="gchat", link_fn=chat_room_link,
            templates_cfg=self._templates,
        )
        # The sole network/write call. ``send_message`` is async in
        # google_chat.py; bridge it for this sync dispatcher. Tests may patch
        # google_chat.send_message with either a coroutine or a plain function.
        result = google_chat.send_message(self._space_name, text)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        if isinstance(result, dict) and result.get("error"):
            print(
                f"[notify] gc_inbox send failed: {result.get('error')}",
                file=sys.stderr,
            )
            return False
        return True


class TelegramSender(Sender):
    """Posts the digest to a Telegram chat via the Bot API ``sendMessage``.

    Enabled iff ``channels.telegram.enabled`` AND both a bot token and a chat id
    are available. The token comes from ``secrets.env`` (``TELEGRAM_BOT_TOKEN``);
    the chat id from ``secrets.env`` (``TELEGRAM_CHAT_ID``) or, as a fallback,
    ``channels.telegram.chat_id`` — secrets win. Secrets are read fresh each
    cycle (``config.load_secrets``) so adding a token does not require a restart.

    Stdlib only (``urllib.request`` + ``json``). The bot URL embeds the token, so
    it is NEVER printed or logged — failure messages carry only the Telegram
    ``description`` or a generic exception class name.
    """

    name = "telegram"

    # Telegram messages cap at 4096 chars; stay safely under with a round 4000.
    _TEXT_LIMIT = 4000
    _TIMEOUT = 10  # seconds

    def __init__(self, cfg: dict | None = None, secrets: dict | None = None):
        secrets = config.load_secrets() if secrets is None else secrets
        tg = ((cfg or {}).get("channels") or {}).get("telegram") or {}
        self._token = secrets.get("TELEGRAM_BOT_TOKEN")
        # Secrets win; fall back to a chat_id pinned in config if present.
        self._chat_id = secrets.get("TELEGRAM_CHAT_ID") or tg.get("chat_id")
        self._templates = (cfg or {}).get("templates")

    def enabled(self, cfg: dict) -> bool:
        tg = (cfg.get("channels") or {}).get("telegram") or {}
        return bool(tg.get("enabled")) and bool(self._token) and bool(self._chat_id)

    def _http_post(self, url: str, payload: dict) -> dict:
        """POST ``payload`` as JSON to ``url`` and return the parsed JSON body.

        SEAM: the sole network call. Tests patch THIS method so no test ever
        touches the wire. Raises on transport/HTTP errors (caller catches).
        """
        import json as _json
        import urllib.request

        data = _json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._TIMEOUT) as resp:
            return _json.loads(resp.read().decode("utf-8"))

    def send(self, new_items: list, esc_items: list, now) -> bool:
        # Render the Telegram HTML dialect. ALL dynamic text is html-escaped
        # inside render_card (SECURITY: fetched Chat text is untrusted data).
        text = render_card(
            new_items, esc_items, now=now, mode="tg_html", link_fn=chat_room_link,
            templates_cfg=self._templates,
        )
        # Build the token-bearing URL locally; it must never escape this scope.
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "parse_mode": "HTML",
            "text": text[: self._TEXT_LIMIT],
            "disable_web_page_preview": True,
        }
        try:
            body = self._http_post(url, payload)
        except Exception as exc:  # noqa: BLE001 - isolate + sanitize
            # NEVER include the URL/token; only a generic exception class name.
            print(
                f"[notify] telegram send failed: {type(exc).__name__}",
                file=sys.stderr,
            )
            return False
        if isinstance(body, dict) and body.get("ok") is True:
            return True
        # ok:false — surface only Telegram's own ``description`` (no token).
        reason = (body or {}).get("description") if isinstance(body, dict) else "bad response"
        print(f"[notify] telegram send failed: {reason}", file=sys.stderr)
        return False


def build_senders(cfg: dict) -> list:
    """Return the enabled senders. A trivial list — extend here for Wave E."""
    candidates = [ConsoleSender(cfg), GCInboxSender(cfg), TelegramSender(cfg)]
    return [s for s in candidates if s.enabled(cfg)]


def _dispatch(senders: list, new_items: list, esc_items: list, now) -> list:
    """Send to every sender; never let one failure block the others.

    Each sender renders its OWN per-channel text from ``(new_items, esc_items,
    now)``. Returns the names of senders that succeeded. A sender that returns
    False or raises is logged and skipped.
    """
    succeeded = []
    for sender in senders:
        try:
            ok = sender.send(new_items, esc_items, now)
        except Exception as exc:  # noqa: BLE001 - isolate sender failures
            print(f"[notify] sender {sender.name!r} raised: {exc}", file=sys.stderr)
            ok = False
        if ok:
            succeeded.append(sender.name)
        else:
            print(f"[notify] sender {sender.name!r} reported failure", file=sys.stderr)
    return succeeded


# --------------------------------------------------------------------------- #
# Digest.
# --------------------------------------------------------------------------- #
# Display cap on the NEW section only. The header always reports TRUE totals and
# every dispatched item is still marked notified — this is purely a cap on how
# many NEW lines the digest TEXT shows, so a large backlog can't produce a
# wall-of-text message. OVERDUE/escalation lines are never capped (safety net).
DIGEST_NEW_CAP = 10

# Block indent for the secondary lines of a Card block (lines 2..4).
_INDENT = "   "
# Static DEFAULT label for the permalink line — never user data, never escaped. The
# template engine passes a localized label (config ``templates.locales.*.open_link``);
# this default keeps direct ``_link_line`` callers (and tests) unaffected.
_LINK_LABEL = "🔗 Open in Chat"

# Priority → icon. urgent/high → 🔴, medium → 🟡, normal → 🟢, else ⚪.
_PRIORITY_ICON = {
    "urgent": "🔴",
    "high": "🔴",
    "medium": "🟡",
    "normal": "🟢",
}


def _snippet(text, limit: int = 140) -> str:
    """Collapse newlines, strip, truncate to ``limit`` with an ellipsis."""
    text = (text or "").replace("\r", " ").replace("\n", " ").strip()
    # Collapse runs of whitespace introduced by the newline flattening.
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def chat_room_link(item: dict):
    """Build a room-level Google Chat link from an item — PURE STRING, no network.

    [EXPLICIT] Returns https://chat.google.com/room/{SPACE_ID}; resolves to the
      SPACE (room), opening at the latest message. The API resource name
      spaces/{space}/messages/{message} is an API id, NOT a web address — Google
      web ignores a hand-appended message segment.
    [EXPLICIT] Reliable per-message landing requires the UI "Copy link" permalink
      (Google Workspace, Sep 2023), which is NOT reproducible from the API
      resource name — no public mapping exists.
    [INFERRED] The chat.google.com/room/{space} form opens the correct room
      (empirically confirmed by the maintainer); Google's documented canonical
      form is the Gmail-embedded mail.google.com/chat/u/0/#chat/space/{id} — we
      keep the verified chat.google.com form.
    [ASSUMED] 'opens at the latest message' reflects the maintainer's
      observation, not a documented guarantee.
    Fallback: no space_id → None (link line omitted).
    """
    space = item.get("space_name")
    space_id = None
    if isinstance(space, str) and space.startswith("spaces/"):
        space_id = space[len("spaces/"):] or None
    if space_id:
        return f"https://chat.google.com/room/{space_id}"
    return None


def _priority_icon(item: dict) -> str:
    return _PRIORITY_ICON.get((item.get("priority") or "").lower(), "⚪")


def _is_dm(item: dict) -> bool:
    if item.get("trigger") == "direct_dm":
        return True
    st = (item.get("space_type") or "").upper()
    return "DM" in st or st == "DIRECT_MESSAGE"


def _bold(text: str, mode: str) -> str:
    if mode == "gchat":
        return f"*{text}*"
    if mode == "tg_html":
        return f"<b>{text}</b>"
    return text


# Google-Chat structural characters → look-alike neutralizers. Chat renders its
# OWN formatting, so raw ``<url|label>`` (link), ``<users/id>`` (mention) and
# ``*_~`` (bold/italic/strike) smuggled in fetched message text would render as
# LIVE markup. We map each to a Unicode look-alike that is visually ~identical
# but inert to the Chat parser. Applied ONLY to dynamic text via the gchat
# branch below — never to the ``<url|label>`` / ``*sender*`` we build ourselves.
_GCHAT_DEFANG = str.maketrans({
    "<": "‹",  # ‹  SINGLE LEFT-POINTING ANGLE QUOTATION MARK
    ">": "›",  # ›  SINGLE RIGHT-POINTING ANGLE QUOTATION MARK
    "|": "∣",  # ∣  DIVIDES
    "*": "∗",  # ∗  ASTERISK OPERATOR
    "_": "ˍ",  # ˍ  MODIFIER LETTER LOW MACRON
    "~": "⁓",  # ⁓  SWUNG DASH
})


def _esc(text, mode: str) -> str:
    """Neutralize dynamic (untrusted) text per channel; plain passes through.

    SECURITY: fetched Chat text is untrusted DATA and MUST NOT smuggle live
    markup into a rendered digest.
    - ``tg_html``: HTML-escape so ``<b>`` / ``<a>`` / ``<script>`` render as
      literal characters, never as live markup.
    - ``gchat``: Google Chat renders its own formatting, so defang the structural
      characters (``< > | * _ ~``) to inert look-alikes — a body containing
      ``<https://phish|label>`` (spoofed link) or ``<users/all>`` (mention/ping)
      or ``*x*`` (bold) renders as inert text, not live markup.
    - ``plain``: no markup engine downstream — pass through verbatim.

    Static emoji / dividers / the link label and the ``<url|label>`` / ``<a>`` we
    build OURSELVES are never routed through here, so our own permalink and the
    bold-sender wrapper still render live.
    """
    s = "" if text is None else str(text)
    if mode == "tg_html":
        return html.escape(s)
    if mode == "gchat":
        return s.translate(_GCHAT_DEFANG)
    return s


def _link_line(url, mode: str, label: str = _LINK_LABEL):
    """Render the permalink line for ``mode``; None when there is no url.

    ``label`` is the (already-localized) static link text — never user data, never
    escaped. Defaults to the English ``_LINK_LABEL`` so direct callers are unaffected.
    """
    if not url:
        return None
    if mode == "gchat":
        return f"<{url}|{label}>"
    if mode == "tg_html":
        return f'<a href="{html.escape(url, quote=True)}">{label}</a>'
    return f"{label}: {url}"


def render_card(
    new_items: list,
    esc_items: list,
    *,
    now: datetime | None,
    mode: str,
    link_fn=chat_room_link,
    cap=None,
    templates_cfg=None,
) -> str:
    """Render the per-channel 'Card' digest via the config-driven template engine.

    Thin delegation to :func:`templates.render`. ``mode`` ∈ {'plain', 'gchat',
    'tg_html'} selects the markup dialect; ``templates_cfg`` is ``config['templates']``
    (None → :data:`templates.DEFAULT_TEMPLATES`). ``cap`` overrides the active profile's
    ``new_cap`` (None → profile value; ``<= 0`` → no cap). The escaping primitives
    (``_esc`` / ``_bold`` / ``_link_line`` / ``_snippet`` …) defined above remain the
    single security layer the engine reuses.
    """
    return templates.render(
        new_items,
        esc_items,
        now=now,
        mode=mode,
        templates_cfg=templates_cfg,
        link_fn=link_fn,
        cap=cap,
    )


def build_digest(
    new_items: list, esc_items: list, *, now=None, cap=None, templates_cfg=None
) -> str:
    """PLAIN/console Card render — the no-markup fallback used by ConsoleSender
    and the ``--dry-run`` preview. ``now`` is injected for relative times."""
    return render_card(
        new_items,
        esc_items,
        now=now,
        mode="plain",
        link_fn=chat_room_link,
        cap=cap,
        templates_cfg=templates_cfg,
    )


# --------------------------------------------------------------------------- #
# Persistence seam (T2.1 step 4).
# --------------------------------------------------------------------------- #
def _set_item_fields(store_data: dict, item_id: str, fields: dict) -> None:
    """Set ``fields`` on an item WITHOUT a status transition or history entry.

    SEAM: prefer a real ``store.set_fields`` if it ever lands; until then mutate
    the public item dict directly (NOT a ``set_status`` hack, NOT a store.py
    edit). Caller persists via ``store.save`` afterwards.
    """
    setter = getattr(store, "set_fields", None)
    if setter is not None:
        setter(store_data, item_id, **fields)
    else:
        store_data["items"][item_id].update(fields)


# --------------------------------------------------------------------------- #
# Dispatch cycle.
# --------------------------------------------------------------------------- #
def run_once(
    store_data: dict,
    cfg: dict,
    *,
    now=None,
    dry_run: bool = False,
    store_path=None,
    senders: list | None = None,
) -> dict:
    """Run one dispatch cycle. Returns a result dict describing what happened.

    Never raises on a sender failure. Persists notification state only when at
    least one sender succeeded, and never under ``dry_run``.
    """
    now = _resolve_now(cfg, now)
    result = {
        "muted": False,
        "quiet": False,
        "dry_run": dry_run,
        "new_notified": [],
        "escalated": [],
        "suppressed_quiet": [],
        "pinned_ridealong": [],
        "senders_succeeded": [],
    }

    # R5 kill switch — emit nothing, exit 0.
    if not config.is_active(cfg, now=now):
        result["muted"] = True
        print("[notify] muted (kill switch / mute_until active) — nothing sent")
        return result

    quiet = in_quiet_hours(now, cfg)
    result["quiet"] = quiet

    new_cands = new_candidates(store_data, cfg)
    esc_cands = escalation_candidates(store_data, now)
    esc_ids = {it["id"] for it in esc_cands}

    # Quiet hours suppress NEW baseline notifications (hold them, do NOT mark
    # notified). Overdue escalations still fire — the safety net.
    if quiet:
        result["suppressed_quiet"] = [it["id"] for it in new_cands]
        notify_new = []
    else:
        notify_new = [it for it in new_cands if it["id"] not in esc_ids]

    to_dispatch = notify_new + esc_cands
    if not to_dispatch:
        # Pins NEVER force a lone send — with nothing else firing, a pinned item
        # stays silent and simply waits for the next digest that has its own
        # reason to fire. (Piggyback, not a trigger.)
        print("[notify] no candidates to notify")
        return result

    # Pin piggyback: pinned OPEN items ride along on a digest that is ALREADY
    # firing. They are NOT marked notified (so they reappear every cycle until
    # unpinned), are de-duped against this cycle's NEW/escalation candidates, and
    # are suppressed in quiet hours exactly like NEW items (only the escalation
    # safety net speaks during quiet hours).
    present_ids = {it["id"] for it in to_dispatch}
    ride_pins = (
        []
        if quiet
        else [it for it in pinned_candidates(store_data) if it["id"] not in present_ids]
    )
    render_new = notify_new + ride_pins
    result["pinned_ridealong"] = [it["id"] for it in ride_pins]

    if dry_run:
        print("[notify] DRY-RUN — would dispatch (no send, no persist):")
        print(build_digest(render_new, esc_cands, now=now, templates_cfg=cfg.get("templates")))
        result["new_notified"] = [it["id"] for it in notify_new]
        result["escalated"] = [it["id"] for it in esc_cands]
        return result

    active = build_senders(cfg) if senders is None else senders
    succeeded = _dispatch(active, render_new, esc_cands, now)
    result["senders_succeeded"] = succeeded

    # Mark notified iff at least one sender succeeded.
    if succeeded:
        now_iso = _iso(now)
        updates: dict = {}
        for it in notify_new:
            updates[it["id"]] = {"last_notified": now_iso}
        for it in esc_cands:
            updates[it["id"]] = {
                "last_notified": now_iso,
                "promise_escalated_at": now_iso,
            }
        for iid, fields in updates.items():
            _set_item_fields(store_data, iid, fields)
        store.save(store_data, store_path)
        result["new_notified"] = [it["id"] for it in notify_new]
        result["escalated"] = [it["id"] for it in esc_cands]
    else:
        print("[notify] no sender succeeded — items NOT marked notified")

    return result


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
def _format_result(result: dict) -> str:
    if result["muted"]:
        return "muted — nothing sent"
    parts = [
        f"notified={len(result['new_notified'])}",
        f"escalated={len(result['escalated'])}",
        f"suppressed_quiet={len(result['suppressed_quiet'])}",
        f"senders={','.join(result['senders_succeeded']) or '-'}",
        f"quiet={result['quiet']}",
        f"dry_run={result['dry_run']}",
    ]
    return "dispatch: " + " ".join(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Chat triage notification dispatcher")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute + print candidates, send NOTHING, persist NOTHING",
    )
    parser.add_argument("--store-path", default=None, help="override store.json path")
    parser.add_argument("--config-path", default=None, help="override config.json path")
    args = parser.parse_args(argv)

    cfg = config.load_config(args.config_path)
    store_data = store.load(args.store_path)
    result = run_once(
        store_data,
        cfg,
        dry_run=args.dry_run,
        store_path=args.store_path,
    )
    print(_format_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
