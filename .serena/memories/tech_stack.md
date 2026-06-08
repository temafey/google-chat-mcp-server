# Tech Stack

- **Python `>=3.13,<3.14`** (hard pin in pyproject). Uses 3.12+ syntax freely.
- **Package manager: `uv`** (uv.lock present). `uv` reads `pyproject.toml` — `requirements.txt` is STALE/incompatible (pins `fastmcp>=2.13.1` vs pyproject `fastmcp>=0.4.1,<0.5.0`). Ignore requirements.txt.
- Build backend: `hatchling`.
- **MCP framework: `fastmcp>=0.4.1,<0.5.0`** (the 0.4.x line, NOT the 2.x rewrite).
- Google: `google-auth`, `google-auth-oauthlib`, `google-api-python-client`.
- Web auth server: `fastapi` + `uvicorn`.
- HTTP: `httpx`. Timezones: `tzdata` (zoneinfo on WSL).
- **Tests: `pytest>=8` + `pytest-asyncio`** in `[dependency-groups].dev`; `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed — async test fns auto-run).
- Notify channels reach external services: Telegram Bot API (httpx), Google Chat REST (incoming card to a space).
- Platform: Linux/WSL2. Cron via host crontab + `flock`; WSL persistence via `/etc/wsl.conf`.
