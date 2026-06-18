# Open a Google Chat link from Telegram in Chrome (PWA) — quick & dirty

## TL;DR
A Telegram message can carry only an `http(s)` URL — you **cannot** force a specific
browser from inside the message. So keep the Telegram link as a normal
`https://chat.google.com/...` URL and make the browser choice happen **on the machine**:
a tiny URL router (set as the default browser) sends `chat.google.com` to Chrome and
everything else to Edge. Chrome then captures the URL into the installed Google Chat PWA.

## What goes in the Telegram message
Plain link:
```
https://chat.google.com/room/AAQAugHrEgY
```
Or as clickable text (Telegram Desktop: select text → `Ctrl+K` → paste the same https URL).

> Custom schemes like `gchat://...` will NOT work — Telegram only makes
> `http://` / `https://` links clickable. The message stays a plain https link.

## One-time machine setup (~5 min)
1. **Install the Chat PWA in Chrome** — open `https://chat.google.com` in Chrome →
   install icon in the address bar → **Install**.
   (Required so Chrome "controls" `chat.google.com` and opens it in the app window, not a tab.)
2. **Install a URL router and set it as the default browser:**
   - **BrowserPicker** (open-source) or **Browser Tamer** — both rule-based and lightweight.
3. **Add a rule:** `chat.google.com` → **Chrome**; default / fallback → **Edge**.

## Result
Click the link in Telegram → the router intercepts it → Chrome opens the room directly
in the Google Chat PWA window. Edge stays default for everything else.

## No-install fallback (1 extra click, keeps Edge default)
Don't want to change the default browser? Install the **"Open in Chrome"** extension in Edge.
The Telegram link opens in Edge as usual; one click on the extension re-opens the current
page in Chrome (→ PWA).

## Notes
- Needs **Chrome 139+**, which introduced the current *navigation capturing* behavior
  for installed PWAs on **Windows, Mac, and Linux** (ChromeOS support lands later).
  In this version an in-scope `chat.google.com` link is captured into the PWA
  **automatically** — there is no per-link prompt. If you'd rather it open in a tab,
  toggle the opt-out in the PWA's own settings (⋮ menu → app settings).
- ⚠ Don't confuse this with the *old* "Link Capturing" feature (ChromeOS-only,
  ChromeOS 98). The mechanism here is the newer **navigation capturing (Chrome 139)**,
  which is desktop-first — cite that name to avoid landing on outdated 2022 docs.
- The setup lives on **each machine where you click**. The Telegram message itself is
  identical for everyone — just a normal https link.

## References
- [Navigation management into installed PWAs — Chrome for Developers](https://developer.chrome.com/docs/capabilities/pwa-navigation-management)
- [mortenn/BrowserPicker — GitHub](https://github.com/mortenn/BrowserPicker)
- [Browser Tamer — aloneguid.uk](https://www.aloneguid.uk/projects/bt/)
