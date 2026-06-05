# Chat Triage Assistant — Cron Setup (WSL)

This runbook wires the **Chat Triage Assistant** to run unattended every
`poll_cadence_minutes` (default **10**). Each tick runs the **read-only
collector** (`scripts/collect_mentions.py`) and then the **notifier**
(`scripts/notify.py`), with overlap protection, logging, and survival across a
WSL restart.

The schedule is driven by three pieces:

| Piece | What it does | Who installs it |
|-------|--------------|-----------------|
| `scripts/triage_cron.sh` | Wrapper: fixes cron's minimal env, runs collect → notify, logs | you (`bash`) |
| `scripts/install_cron.sh` | Writes the crontab line idempotently | you (`bash`) |
| `cron` service + `/etc/wsl.conf` | Runs the schedule, survives reboot | **you, with `sudo`** |

> The collector is **strictly read-only** against Chat. The **only** outward
> Chat write is the notifier's `gc_inbox` sender, which is gated by config.
> The notifier also self-suppresses during quiet hours and when the kill switch
> is set, so cron can safely run 24/7.

---

## 1. One-time host setup (you run this, with `sudo`)

WSL does not start `cron` automatically. Do both steps once.

### 1a. Start cron now

```bash
sudo service cron start
```

Verify:

```bash
service cron status     # should report cron is running
```

### 1b. Enable cron on every WSL boot

Add a `[boot]` command to **`/etc/wsl.conf`** (create the file if absent):

```ini
[boot]
command="service cron start"
```

This only takes effect after WSL is fully restarted. From a **Windows**
PowerShell / CMD prompt, run **once**:

```powershell
wsl --shutdown
```

Then reopen your WSL terminal. From now on `cron` starts automatically whenever
WSL boots. (Without the `wsl --shutdown`, the `[boot]` directive is not picked
up until the next full WSL shutdown.)

---

## 2. Install / refresh the schedule (no sudo)

From the repo root:

```bash
bash scripts/install_cron.sh
```

This script:

- makes both `.sh` files executable,
- reads `poll_cadence_minutes` from `config.json` (default 10),
- removes any prior `triage_cron.sh` crontab line, then appends the new one,
- prints the resulting crontab.

Re-run it any time you change `poll_cadence_minutes` — it is idempotent and
never disturbs your other cron entries.

The installed crontab line looks like this (N = your cadence):

```cron
*/N * * * * /usr/bin/flock -n $HOME/.claude-orchestrator/gchat-triage/cron.lock /home/temafey/projects/google-chat-mcp-server/scripts/triage_cron.sh >> $HOME/.claude-orchestrator/gchat-triage/logs/cron.log 2>&1
```

`flock -n` provides **overlap protection**: if a previous run is still going,
the next tick is skipped rather than stacking. (The Python scripts additionally
hold their own run-lock and token-lock.)

---

## 3. Smoke test

Run one collect → notify cycle by hand, printed to your terminal:

```bash
bash scripts/triage_cron.sh --once
```

Expect a collector digest line (`Chat triage — N new / M open total`) followed
by a notifier result line (`dispatch: …` or `muted — nothing sent`). During
quiet hours (22:00–08:00 Europe/Kyiv by default) the notifier self-suppresses —
that is correct behaviour, not an error.

---

## 4. Where logs go & how to verify

| File | Contents |
|------|----------|
| `~/.claude-orchestrator/gchat-triage/logs/cron.log` | stdout/stderr captured by the crontab redirect |
| `~/.claude-orchestrator/gchat-triage/logs/triage_cron-YYYY-MM-DD.log` | per-day wrapper log (collect + notify output) |
| `~/.claude-orchestrator/gchat-triage/logs/collect-YYYY-MM-DD.log` | the collector's structured JSON events |

Verify the schedule and tail the log:

```bash
crontab -l                                                  # confirm the line is present
tail -f ~/.claude-orchestrator/gchat-triage/logs/cron.log   # watch live ticks
```

---

## 5. How to pause

Use the **kill switch** in `config.json` — no need to touch cron:

```jsonc
// ~/.claude-orchestrator/gchat-triage/config.json
{ "enabled": false }
```

With `enabled: false` (or a future `mute_until`), both the collector and the
notifier short-circuit and do nothing. cron can keep ticking harmlessly. Set it
back to `true` to resume.

---

## 6. Health note after a WSL restart

WSL shutdowns stop the `cron` daemon. If you configured `/etc/wsl.conf` (step
1b) it restarts automatically — but it is worth confirming after any unexpected
restart:

```bash
service cron status
```

If it is not running, start it (`sudo service cron start`) and double-check that
`/etc/wsl.conf` still contains the `[boot]` command.
