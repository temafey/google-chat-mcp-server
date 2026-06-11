"""analysis_adapters.py — Pluggable AI analysis adapter layer.

Shape mirrors ``scripts/notify.Sender`` (ABC):
  - class attr ``name: str``
  - capability gate  ``available() -> bool``
  - action method    ``run(request) -> AnalysisResult``   (NEVER raises; failures returned)

Usage
-----
>>> adapter = ClaudeAdapter()
>>> if adapter.available():
...     result = adapter.run(AnalysisRequest(mode="classify", prompt="..."))
...     if result.ok:
...         print(result.data)

This module has NO imports from store / config / notify.  It is self-contained.

---

Observed ``claude -p --output-format json`` envelope shape (verified live, 2026-06-11)
---------------------------------------------------------------------------------------
The outer JSON returned on stdout has (at minimum) these fields::

    {
      "type":     "result",
      "subtype":  "success",
      "is_error": false,
      "result":   "<assistant reply text>",   # <-- the text the model produced
      "stop_reason": "end_turn",
      ...
    }

``result`` is a *string* — the raw model output text.  It may itself be a JSON
object string (our goal), or it may have ```json … ``` fences / leading prose.
We strip fences and locate the first balanced ``{...}`` block before parsing.

On failure (is_error=true, non-zero exit, missing "result" key, empty stdout) we
return AnalysisResult(ok=False, error="…").

---

Sanitized subprocess environment
---------------------------------
The child ``claude`` process inherits a COPY of ``os.environ`` with the following
keys *removed* to prevent triage secrets from leaking into the sub-process:

  Hard denylist (exact names):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_APPLICATION_CREDENTIALS

  Pattern denylist (key name contains, case-insensitive):
    SECRET, TOKEN, PASSWORD

  Explicit *keeps* that override the pattern (so the CLI still authenticates):
    ANTHROPIC_API_KEY   — the Claude CLI needs this
    PATH, HOME          — essential for process resolution
    CLAUDE_*            — Claude CLI own config vars (e.g. CLAUDE_CODE_ENTRYPOINT)

Keys in the explicit-keeps set are NEVER stripped even if they match the pattern.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AnalysisRequest:
    """Fully-built analysis job.  ``prompt`` is already rendered by the caller."""
    mode: str                          # "classify" | "summarize"
    prompt: str                        # complete prompt text, ready to pass verbatim
    model: str = "claude-haiku-4-5-20251001"
    timeout_seconds: int = 60


@dataclass
class AnalysisResult:
    """Outcome of an adapter run.  Never raises — failures encoded here."""
    ok: bool
    adapter: str                       # provenance, e.g. "claude/haiku"
    mode: str
    data: Optional[dict] = None        # parsed inner JSON object on success
    error: Optional[str] = None        # concise failure reason
    raw: Optional[str] = None          # bounded raw stdout for debugging (~20k cap)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class AnalysisAdapter(ABC):
    """Abstract adapter.  Mirrors the ``notify.Sender`` capability-gate pattern."""

    name: str = "adapter"

    @abstractmethod
    def available(self) -> bool:  # pragma: no cover — interface
        """Cached capability probe.  Must be fast (no network)."""
        ...

    @abstractmethod
    def run(self, request: AnalysisRequest) -> AnalysisResult:  # pragma: no cover
        """Execute analysis.  NEVER raises — all failures are returned."""
        ...


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Keys that must never be stripped regardless of pattern matching.
_ENV_KEEP = frozenset({
    "ANTHROPIC_API_KEY",
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "XDG_RUNTIME_DIR",
})

# Exact keys to always strip (triage-specific secrets).
_ENV_DENY_EXACT = frozenset({
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
})

# Substrings (upper-cased) whose presence in a key name triggers removal.
_ENV_DENY_PATTERNS = ("SECRET", "TOKEN", "PASSWORD")

# Stdout cap in characters to avoid storing huge blobs.
_RAW_CAP = 20_000

# Regex to strip ```json ... ``` fences (or ``` alone).
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _build_clean_env() -> dict:
    """Return a sanitized copy of os.environ (no triage secrets)."""
    clean: dict = {}
    for key, val in os.environ.items():
        key_upper = key.upper()
        # Explicit keeps override everything.
        if key in _ENV_KEEP or key_upper.startswith("CLAUDE_"):
            clean[key] = val
            continue
        # Exact denylist.
        if key in _ENV_DENY_EXACT:
            continue
        # Pattern denylist.
        if any(pat in key_upper for pat in _ENV_DENY_PATTERNS):
            continue
        clean[key] = val
    return clean


def _short_model_name(model: str) -> str:
    """'claude-haiku-4-5-20251001' → 'haiku'  (best-effort)."""
    # Strip date suffix, then pick the token after the first 'claude-'.
    stripped = re.sub(r"-\d{8}$", "", model)   # remove trailing date
    parts = stripped.lower().split("-")
    # parts like ['claude', 'haiku', '4', '5'] or ['claude', 'opus', '4']
    # The human-readable family name is the token right after 'claude'.
    if len(parts) >= 2 and parts[0] == "claude":
        return parts[1]
    return stripped


def _extract_inner_json(text: str) -> Optional[dict]:
    """Parse the first balanced ``{...}`` from model output text.

    Handles:
      * bare JSON objects
      * ```json ... ``` fences
      * leading prose before the opening ``{``
    Returns None if no valid JSON object found.
    """
    if not text:
        return None
    # Try stripping fences first.
    fence_match = _FENCE_RE.search(text)
    candidate = fence_match.group(1) if fence_match else text
    # Locate first '{'.
    start = candidate.find("{")
    if start == -1:
        return None
    # Walk to find the matching '}'.
    depth = 0
    for i, ch in enumerate(candidate[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(candidate[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ---------------------------------------------------------------------------
# ClaudeAdapter
# ---------------------------------------------------------------------------

class ClaudeAdapter(AnalysisAdapter):
    """Runs analysis via the ``claude`` CLI subprocess.

    Spawns: ``claude -p --model <model> --output-format json <prompt>``

    The child process inherits a sanitized environment (no triage secrets).
    stdout is capped at ``_RAW_CAP`` characters before storage.
    """

    name: str = "claude"

    def __init__(self) -> None:
        self._available_cache: Optional[bool] = None

    # ------------------------------------------------------------------
    # Capability gate (cached)
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """True iff ``claude`` binary is on PATH.  Result cached after first call."""
        if self._available_cache is None:
            self._available_cache = shutil.which("claude") is not None
        return self._available_cache

    # ------------------------------------------------------------------
    # Action method
    # ------------------------------------------------------------------

    def run(self, request: AnalysisRequest) -> AnalysisResult:
        """Execute the analysis.  NEVER raises — all failures returned."""
        short = _short_model_name(request.model)
        provenance = f"claude/{short}"

        # 1. Pre-flight: adapter available?
        if not self.available():
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error="claude CLI not found on PATH",
            )

        # 2. Build argv (NEVER shell=True).
        argv = [
            "claude",
            "-p",
            "--model", request.model,
            "--output-format", "json",
            request.prompt,
        ]

        # 3. Sanitized env.
        clean_env = _build_clean_env()

        # 4. Spawn.
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                shell=False,         # explicit — never shell=True
                env=clean_env,
            )
        except subprocess.TimeoutExpired as exc:
            raw = (exc.stdout or "")[:_RAW_CAP] if exc.stdout else None
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error=f"timeout after {request.timeout_seconds}s",
                raw=raw,
            )
        except Exception as exc:  # noqa: BLE001
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error=f"subprocess error: {exc}",
            )

        # 5. Bound raw output.
        raw_bounded = proc.stdout[:_RAW_CAP] if proc.stdout else None

        # 6. Non-zero exit.
        if proc.returncode != 0:
            stderr_snippet = (proc.stderr or "")[:500]
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error=f"exit {proc.returncode}: {stderr_snippet}",
                raw=raw_bounded,
            )

        # 7. Empty stdout.
        if not proc.stdout or not proc.stdout.strip():
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error="empty stdout",
                raw=None,
            )

        # 8. Parse outer envelope.
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error=f"outer envelope JSON parse error: {exc}",
                raw=raw_bounded,
            )

        # 9. Check for CLI-level error flag.
        if envelope.get("is_error"):
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error=f"CLI reported error: {envelope.get('result', '')[:300]}",
                raw=raw_bounded,
            )

        # 10. Extract assistant text from envelope["result"].
        result_text = envelope.get("result")
        if result_text is None:
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error="envelope missing 'result' field",
                raw=raw_bounded,
            )
        if not isinstance(result_text, str):
            result_text = str(result_text)

        # 11. Parse inner JSON produced by the model.
        inner = _extract_inner_json(result_text)
        if inner is None:
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error=f"inner JSON not found in model output: {result_text[:200]}",
                raw=raw_bounded,
            )

        # 12. Success.
        return AnalysisResult(
            ok=True,
            adapter=provenance,
            mode=request.mode,
            data=inner,
            raw=raw_bounded,
        )


# ---------------------------------------------------------------------------
# Optional: first-available helper (minimal; routing lives in B1/D3)
# ---------------------------------------------------------------------------

def first_available(
    names: list[str],
    *,
    registry: Optional[dict[str, AnalysisAdapter]] = None,
) -> Optional[AnalysisAdapter]:
    """Return the first adapter (by name) whose ``available()`` probe succeeds.

    ``registry`` defaults to the built-in ``_DEFAULT_REGISTRY``.
    Returns None if no adapter is available.

    This helper is intentionally thin — routing policy belongs in the caller.
    """
    reg = registry if registry is not None else _DEFAULT_REGISTRY
    for n in names:
        adapter = reg.get(n)
        if adapter is not None and adapter.available():
            return adapter
    return None


# Module-level default registry (one instance per adapter type).
_DEFAULT_REGISTRY: dict[str, AnalysisAdapter] = {
    "claude": ClaudeAdapter(),
}
