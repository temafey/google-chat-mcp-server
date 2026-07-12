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

Observed ``gemini -p <prompt> --model <model> --output-format json`` envelope shape
------------------------------------------------------------------------------------
The outer JSON returned on stdout is a single object::

    {
      "response": "<assistant reply text>",
      "stats": { "models": {...}, "tools": {...}, "files": {...} }
    }

On error an ``"error"`` key may be present::

    { "error": { "type": "...", "message": "...", "code": <int> } }

``response`` is a *string* — the raw model output text, same format as Claude.
We extract inner JSON from it using the same ``_extract_inner_json`` helper.

Source: https://google-gemini.github.io/gemini-cli/docs/cli/headless.html
(verified 2026-06-12, gemini CLI v0.30.0)

---

Observed ``codex exec --json --model <model> <prompt>`` JSONL stream shape
---------------------------------------------------------------------------
With ``--json``, stdout is newline-delimited JSON (one JSON object per line)::

    {"type":"thread.started","thread_id":"<uuid>"}
    {"type":"turn.started"}
    {"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"<text>"}}
    {"type":"turn.completed","usage":{"input_tokens":...,"output_tokens":...}}

The assistant's final reply is the ``text`` field of the *last*
``item.completed`` event where ``item.type == "agent_message"``.
Authentication is via ``OPENAI_API_KEY`` env var (retained by ``_ENV_KEEP``).

Source: https://codex.danielvaughan.com/2026/04/08/codex-exec-jsonl-reference/
(verified 2026-06-12, openai/codex)

---

Sanitized subprocess environment
---------------------------------
The child process inherits a COPY of ``os.environ`` with the following
keys *removed* to prevent triage secrets from leaking into the sub-process:

  Hard denylist (exact names):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_APPLICATION_CREDENTIALS

  Pattern denylist (key name contains, case-insensitive):
    SECRET, TOKEN, PASSWORD

  Explicit *keeps* that override the pattern (so the CLI still authenticates):
    ANTHROPIC_API_KEY   — the Claude CLI needs this
    OPENAI_API_KEY      — the Codex CLI needs this (kept despite "KEY" not matching
                          TOKEN/SECRET/PASSWORD, but listed explicitly for clarity)
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
    "ANTHROPIC_API_KEY",   # Claude CLI auth
    "OPENAI_API_KEY",      # Codex CLI auth
    "GEMINI_API_KEY",      # Gemini CLI auth (alternative to OAuth)
    "GOOGLE_API_KEY",      # Gemini CLI auth (alternative name)
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


def _short_model_name_gemini(model: str) -> str:
    """'gemini-2.5-flash' → 'flash', 'gemini-2.5-pro' → 'pro'  (best-effort).

    Takes the last non-numeric token as the human-readable family name.
    Falls back to the full model string if no better token found.
    """
    parts = model.lower().split("-")
    # parts like ['gemini', '2', '5', 'flash'] or ['gemini', '2', '5', 'pro']
    # Take the last token that is not purely numeric.
    for part in reversed(parts):
        if part and not part.isdigit():
            return part
    return model


def _short_model_name_codex(model: str) -> str:
    """'gpt-5.2-codex' → 'codex', 'gpt-4o' → '4o'  (best-effort).

    Takes the last hyphen-separated token.  Falls back to the full model string.
    """
    parts = model.lower().split("-")
    if parts:
        return parts[-1]
    return model


def _extract_codex_agent_message(stdout: str) -> Optional[str]:
    """Parse JSONL from ``codex exec --json`` and return the last agent message text.

    Scans all lines for ``item.completed`` events where ``item.type`` is
    ``"agent_message"`` (current schema) or ``"assistant_message"`` (pre-v0.44
    schema).  Returns the ``text`` field of the *last* such event, or None if
    none found.

    Defensive: skips lines that are not valid JSON or lack the expected fields.
    """
    last_text: Optional[str] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        # Accept both current ("agent_message") and legacy ("assistant_message") schemas.
        item_type = item.get("type", "")
        if item_type in ("agent_message", "assistant_message"):
            text = item.get("text")
            if isinstance(text, str) and text:
                last_text = text
    return last_text


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

    Spawns: ``claude -p --model <model> --output-format json --strict-mcp-config <prompt>``

    ``--strict-mcp-config`` (with no ``--mcp-config``) disables ALL MCP servers,
    so the repo's ``.mcp.json`` (google_chat + serena) is never loaded — the
    classification task uses no tools, so MCP boot is pure overhead/failure risk.

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
        # --strict-mcp-config with no --mcp-config loads ZERO MCP servers,
        # ignoring the repo's .mcp.json (google_chat + serena).  Classification
        # returns pure JSON and uses no tools, so booting those servers is pure
        # overhead: startup latency (risking the 60s timeout), extra context
        # tokens, and — in cron's credential-less env — a guaranteed google_chat
        # MCP boot failure.  Disabling MCP keeps this call fast and hermetic.
        argv = [
            "claude",
            "-p",
            "--model", request.model,
            "--output-format", "json",
            "--strict-mcp-config",
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
# GeminiAdapter
# ---------------------------------------------------------------------------

class GeminiAdapter(AnalysisAdapter):
    """Runs analysis via the Google ``gemini`` CLI subprocess.

    Spawns: ``gemini -p <prompt> --model <model> --output-format json``

    The outer JSON envelope returned on stdout::

        {
          "response": "<assistant reply text>",
          "stats": { ... }           # usage stats; not parsed here
        }

    On error (non-zero exit, missing "response" key, ``"error"`` key present) we
    return AnalysisResult(ok=False, error=...).

    ``response`` may contain ```json ... ``` fences or leading prose — parsed via
    ``_extract_inner_json`` (same logic as ClaudeAdapter).

    Authentication: the Gemini CLI uses OAuth or a ``GEMINI_API_KEY`` /
    ``GOOGLE_API_KEY`` env var.  Both are retained by ``_ENV_KEEP``.

    Source: https://google-gemini.github.io/gemini-cli/docs/cli/headless.html
    Verified: 2026-06-12, gemini CLI v0.30.0
    """

    name: str = "gemini"

    def __init__(self) -> None:
        self._available_cache: Optional[bool] = None

    # ------------------------------------------------------------------
    # Capability gate (cached)
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """True iff ``gemini`` binary is on PATH.  Result cached after first call."""
        if self._available_cache is None:
            self._available_cache = shutil.which("gemini") is not None
        return self._available_cache

    # ------------------------------------------------------------------
    # Action method
    # ------------------------------------------------------------------

    def run(self, request: AnalysisRequest) -> AnalysisResult:
        """Execute the analysis.  NEVER raises — all failures returned."""
        # Best-effort short model name: strip vendor prefix if present.
        short = _short_model_name_gemini(request.model)
        provenance = f"gemini/{short}"

        # 1. Pre-flight: adapter available?
        if not self.available():
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error="gemini CLI not found on PATH",
            )

        # 2. Build argv (NEVER shell=True).
        #    --output-format json  → single JSON object on stdout (not JSONL)
        argv = [
            "gemini",
            "-p", request.prompt,
            "--model", request.model,
            "--output-format", "json",
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

        # 9. Check for CLI-level error field.
        if "error" in envelope:
            err_obj = envelope["error"]
            err_msg = (
                err_obj.get("message", str(err_obj))
                if isinstance(err_obj, dict)
                else str(err_obj)
            )
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error=f"CLI reported error: {err_msg[:300]}",
                raw=raw_bounded,
            )

        # 10. Extract assistant text from envelope["response"].
        result_text = envelope.get("response")
        if result_text is None:
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error="envelope missing 'response' field",
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
# CodexAdapter
# ---------------------------------------------------------------------------

class CodexAdapter(AnalysisAdapter):
    """Runs analysis via the OpenAI ``codex`` CLI subprocess.

    Spawns: ``codex exec --json --model <model> <prompt>``

    With ``--json``, stdout is JSONL (one JSON object per line).  We read ALL
    lines and look for the *last* ``item.completed`` event where
    ``item.type == "agent_message"`` — its ``item.text`` field is the model's
    final reply text.  We then extract the inner JSON object from that text via
    ``_extract_inner_json`` (same logic as the other adapters).

    Relevant JSONL events::

        {"type":"thread.started","thread_id":"<uuid>"}
        {"type":"turn.started"}
        {"type":"item.completed","item":{"id":"...","type":"agent_message","text":"..."}}
        {"type":"turn.completed","usage":{...}}

    Authentication: ``OPENAI_API_KEY`` env var (retained by ``_ENV_KEEP``).

    Note: codex exec requires a workspace root and may try to run shell tools
    under its sandbox.  For our read-only classify/summarize prompts this is
    unlikely to trigger, but if needed the caller can append ``--sandbox
    read-only`` via a future config option.

    Source: https://codex.danielvaughan.com/2026/04/08/codex-exec-jsonl-reference/
            https://developers.openai.com/codex/noninteractive
    Verified: 2026-06-12
    """

    name: str = "codex"

    def __init__(self) -> None:
        self._available_cache: Optional[bool] = None

    # ------------------------------------------------------------------
    # Capability gate (cached)
    # ------------------------------------------------------------------

    def available(self) -> bool:
        """True iff ``codex`` binary is on PATH.  Result cached after first call."""
        if self._available_cache is None:
            self._available_cache = shutil.which("codex") is not None
        return self._available_cache

    # ------------------------------------------------------------------
    # Action method
    # ------------------------------------------------------------------

    def run(self, request: AnalysisRequest) -> AnalysisResult:
        """Execute the analysis.  NEVER raises — all failures returned."""
        short = _short_model_name_codex(request.model)
        provenance = f"codex/{short}"

        # 1. Pre-flight: adapter available?
        if not self.available():
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error="codex CLI not found on PATH",
            )

        # 2. Build argv (NEVER shell=True).
        #    codex exec --json emits JSONL event stream to stdout.
        #    --ephemeral avoids writing session rollout files to disk.
        argv = [
            "codex", "exec",
            "--json",
            "--ephemeral",
            "--model", request.model,
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

        # 8. Parse JSONL: find last agent_message item.completed event.
        result_text = _extract_codex_agent_message(proc.stdout)
        if result_text is None:
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error="no agent_message found in JSONL output",
                raw=raw_bounded,
            )

        # 9. Parse inner JSON produced by the model.
        inner = _extract_inner_json(result_text)
        if inner is None:
            return AnalysisResult(
                ok=False,
                adapter=provenance,
                mode=request.mode,
                error=f"inner JSON not found in model output: {result_text[:200]}",
                raw=raw_bounded,
            )

        # 10. Success.
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
    "gemini": GeminiAdapter(),
    "codex": CodexAdapter(),
}
