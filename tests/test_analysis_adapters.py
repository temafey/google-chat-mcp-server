"""Unit tests for scripts/analysis_adapters.py.

All tests use unittest.mock — NO real network / CLI calls.

Coverage:
  - available() → True when shutil.which finds claude, False when not, cached.
  - run() happy path: parses faked envelope's inner JSON → data, ok=True.
  - run() failures (all return ok=False, never raise):
      * CLI not found on PATH
      * non-zero exit code
      * subprocess.TimeoutExpired
      * stdout is non-JSON
      * envelope missing 'result' key
      * inner text is prose, not JSON
  - argv uses a list (no shell=True).
  - sanitized env: TELEGRAM_BOT_TOKEN absent; PATH retained.
  - _extract_inner_json handles fences, leading prose, bare JSON.
  - _short_model_name extracts family token correctly.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest.mock import MagicMock, patch

# Adjust import path: project root is in PYTHONPATH via pytest invocation.
from scripts.analysis_adapters import (
    AnalysisRequest,
    AnalysisResult,
    ClaudeAdapter,
    _build_clean_env,
    _extract_inner_json,
    _short_model_name,
    first_available,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope(result_text: str, is_error: bool = False) -> str:
    """Build a minimal outer envelope JSON string as the CLI would emit."""
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": result_text,
        "stop_reason": "end_turn",
    })


def _make_completed_proc(
    stdout: str,
    returncode: int = 0,
    stderr: str = "",
) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# available() — capability gate
# ---------------------------------------------------------------------------

class TestAvailable(unittest.TestCase):

    def test_true_when_which_finds_claude(self):
        adapter = ClaudeAdapter()
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            self.assertTrue(adapter.available())

    def test_false_when_which_returns_none(self):
        adapter = ClaudeAdapter()
        with patch("shutil.which", return_value=None):
            self.assertFalse(adapter.available())

    def test_cached_after_first_call(self):
        """which() must be called exactly once regardless of how many times
        available() is invoked."""
        adapter = ClaudeAdapter()
        with patch("shutil.which", return_value="/usr/bin/claude") as mock_which:
            result1 = adapter.available()
            result2 = adapter.available()
            result3 = adapter.available()
        self.assertTrue(result1)
        self.assertTrue(result2)
        self.assertTrue(result3)
        mock_which.assert_called_once()

    def test_cached_false_also_not_rechecked(self):
        adapter = ClaudeAdapter()
        with patch("shutil.which", return_value=None) as mock_which:
            adapter.available()
            adapter.available()
        mock_which.assert_called_once()


# ---------------------------------------------------------------------------
# run() — happy path
# ---------------------------------------------------------------------------

class TestRunSuccess(unittest.TestCase):

    def _run_with_inner(self, inner_dict: dict, mode: str = "classify") -> AnalysisResult:
        """Helper: fake a successful claude invocation returning inner_dict."""
        adapter = ClaudeAdapter()
        req = AnalysisRequest(mode=mode, prompt="test prompt")
        envelope_text = _make_envelope(json.dumps(inner_dict))
        proc = _make_completed_proc(stdout=envelope_text)

        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc):
            return adapter.run(req)

    def test_ok_true_on_valid_inner_json(self):
        result = self._run_with_inner({"category": "incident", "confidence": 0.9})
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)

    def test_data_equals_parsed_inner_dict(self):
        inner = {"category": "question", "confidence": 0.7, "summary": "hello"}
        result = self._run_with_inner(inner)
        self.assertEqual(result.data, inner)

    def test_adapter_provenance_contains_family_name(self):
        result = self._run_with_inner({"x": 1})
        # "claude/haiku" for default model
        self.assertTrue(result.adapter.startswith("claude/"))

    def test_mode_propagated(self):
        result = self._run_with_inner({"x": 1}, mode="summarize")
        self.assertEqual(result.mode, "summarize")

    def test_inner_json_with_fences(self):
        """Model wraps its output in ```json ... ``` fences."""
        adapter = ClaudeAdapter()
        req = AnalysisRequest(mode="classify", prompt="p")
        inner_text = '```json\n{"category": "bug", "confidence": 0.8}\n```'
        envelope_text = _make_envelope(inner_text)
        proc = _make_completed_proc(stdout=envelope_text)

        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc):
            result = adapter.run(req)

        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"category": "bug", "confidence": 0.8})

    def test_inner_json_with_leading_prose(self):
        """Model prepends prose before the JSON object."""
        adapter = ClaudeAdapter()
        req = AnalysisRequest(mode="classify", prompt="p")
        inner_text = 'Here is the classification:\n{"category": "info", "confidence": 0.5}'
        envelope_text = _make_envelope(inner_text)
        proc = _make_completed_proc(stdout=envelope_text)

        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("subprocess.run", return_value=proc):
            result = adapter.run(req)

        self.assertTrue(result.ok)
        self.assertEqual(result.data["category"], "info")


# ---------------------------------------------------------------------------
# run() — failure modes (NEVER raise; always return ok=False)
# ---------------------------------------------------------------------------

class TestRunFailures(unittest.TestCase):

    def _adapter_with_claude(self) -> ClaudeAdapter:
        """Return a ClaudeAdapter whose available() will return True."""
        a = ClaudeAdapter()
        a._available_cache = True   # bypass the which() probe
        return a

    def test_cli_not_found_returns_ok_false(self):
        adapter = ClaudeAdapter()
        req = AnalysisRequest(mode="classify", prompt="p")
        with patch("shutil.which", return_value=None):
            result = adapter.run(req)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        self.assertIn("not found", result.error.lower())

    def test_nonzero_exit_returns_ok_false(self):
        adapter = self._adapter_with_claude()
        req = AnalysisRequest(mode="classify", prompt="p")
        proc = _make_completed_proc(stdout="", returncode=1, stderr="some error")
        with patch("subprocess.run", return_value=proc):
            result = adapter.run(req)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_nonzero_exit_does_not_raise(self):
        adapter = self._adapter_with_claude()
        req = AnalysisRequest(mode="classify", prompt="p")
        proc = _make_completed_proc(stdout="", returncode=2, stderr="")
        with patch("subprocess.run", return_value=proc):
            # Must not raise
            result = adapter.run(req)
        self.assertIsInstance(result, AnalysisResult)

    def test_timeout_expired_returns_ok_false(self):
        adapter = self._adapter_with_claude()
        req = AnalysisRequest(mode="classify", prompt="p", timeout_seconds=5)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5)):
            result = adapter.run(req)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        self.assertIn("timeout", result.error.lower())

    def test_timeout_does_not_raise(self):
        adapter = self._adapter_with_claude()
        req = AnalysisRequest(mode="classify", prompt="p", timeout_seconds=5)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5)):
            result = adapter.run(req)  # must not raise
        self.assertIsInstance(result, AnalysisResult)

    def test_empty_stdout_returns_ok_false(self):
        adapter = self._adapter_with_claude()
        req = AnalysisRequest(mode="classify", prompt="p")
        proc = _make_completed_proc(stdout="", returncode=0)
        with patch("subprocess.run", return_value=proc):
            result = adapter.run(req)
        self.assertFalse(result.ok)
        self.assertIn("empty", result.error.lower())

    def test_non_json_stdout_returns_ok_false(self):
        adapter = self._adapter_with_claude()
        req = AnalysisRequest(mode="classify", prompt="p")
        proc = _make_completed_proc(stdout="not json at all", returncode=0)
        with patch("subprocess.run", return_value=proc):
            result = adapter.run(req)
        self.assertFalse(result.ok)

    def test_envelope_missing_result_key_returns_ok_false(self):
        adapter = self._adapter_with_claude()
        req = AnalysisRequest(mode="classify", prompt="p")
        envelope_without_result = json.dumps({"type": "result", "is_error": False})
        proc = _make_completed_proc(stdout=envelope_without_result, returncode=0)
        with patch("subprocess.run", return_value=proc):
            result = adapter.run(req)
        self.assertFalse(result.ok)
        self.assertIn("result", result.error.lower())

    def test_inner_text_is_prose_not_json_returns_ok_false(self):
        adapter = self._adapter_with_claude()
        req = AnalysisRequest(mode="classify", prompt="p")
        envelope_text = _make_envelope("I cannot provide a JSON response.")
        proc = _make_completed_proc(stdout=envelope_text, returncode=0)
        with patch("subprocess.run", return_value=proc):
            result = adapter.run(req)
        self.assertFalse(result.ok)
        self.assertIsNone(result.data)

    def test_is_error_true_in_envelope_returns_ok_false(self):
        adapter = self._adapter_with_claude()
        req = AnalysisRequest(mode="classify", prompt="p")
        envelope_text = _make_envelope("error occurred", is_error=True)
        proc = _make_completed_proc(stdout=envelope_text, returncode=0)
        with patch("subprocess.run", return_value=proc):
            result = adapter.run(req)
        self.assertFalse(result.ok)

    def test_generic_subprocess_exception_returns_ok_false(self):
        """Unexpected subprocess error must be caught, not raised."""
        adapter = self._adapter_with_claude()
        req = AnalysisRequest(mode="classify", prompt="p")
        with patch("subprocess.run", side_effect=OSError("no such file")):
            result = adapter.run(req)
        self.assertFalse(result.ok)
        self.assertIsInstance(result, AnalysisResult)


# ---------------------------------------------------------------------------
# argv shape (no shell=True) and sanitized env
# ---------------------------------------------------------------------------

class TestArgvAndEnv(unittest.TestCase):

    def test_argv_is_list_no_shell(self):
        """subprocess.run must be called with a list (not a string) and shell=False."""
        adapter = ClaudeAdapter()
        adapter._available_cache = True
        req = AnalysisRequest(mode="classify", prompt="my test prompt", model="claude-haiku-4-5-20251001")
        inner = {"x": 1}
        proc = _make_completed_proc(stdout=_make_envelope(json.dumps(inner)))

        with patch("subprocess.run", return_value=proc) as mock_run:
            adapter.run(req)

        call_args, call_kwargs = mock_run.call_args
        argv = call_args[0]
        self.assertIsInstance(argv, list, "argv must be a list, not a string")
        self.assertEqual(argv[0], "claude")
        self.assertIn("-p", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--model", argv)
        self.assertIn("claude-haiku-4-5-20251001", argv)
        self.assertIn("my test prompt", argv)
        # shell must be False (or absent, defaulting to False)
        self.assertFalse(call_kwargs.get("shell", False))

    def test_sanitized_env_no_telegram_token(self):
        """TELEGRAM_BOT_TOKEN must not appear in the env passed to subprocess."""
        adapter = ClaudeAdapter()
        adapter._available_cache = True
        req = AnalysisRequest(mode="classify", prompt="p")
        proc = _make_completed_proc(stdout=_make_envelope(json.dumps({"ok": True})))

        tainted_env = dict(os.environ)
        tainted_env["TELEGRAM_BOT_TOKEN"] = "secret-bot-token"
        tainted_env["TELEGRAM_CHAT_ID"] = "12345"
        tainted_env["SOME_PASSWORD"] = "hunter2"

        with patch("os.environ", new=tainted_env), \
             patch("subprocess.run", return_value=proc) as mock_run:
            adapter.run(req)

        _, call_kwargs = mock_run.call_args
        passed_env = call_kwargs.get("env", {})
        self.assertNotIn("TELEGRAM_BOT_TOKEN", passed_env)
        self.assertNotIn("TELEGRAM_CHAT_ID", passed_env)
        self.assertNotIn("SOME_PASSWORD", passed_env)

    def test_sanitized_env_path_retained(self):
        """PATH must survive the env sanitization so the CLI can resolve binaries."""
        adapter = ClaudeAdapter()
        adapter._available_cache = True
        req = AnalysisRequest(mode="classify", prompt="p")
        proc = _make_completed_proc(stdout=_make_envelope(json.dumps({"ok": True})))

        with patch("subprocess.run", return_value=proc) as mock_run:
            adapter.run(req)

        _, call_kwargs = mock_run.call_args
        passed_env = call_kwargs.get("env", {})
        self.assertIn("PATH", passed_env)

    def test_sanitized_env_anthropic_api_key_retained(self):
        """ANTHROPIC_API_KEY must NOT be stripped (the CLI needs it)."""
        adapter = ClaudeAdapter()
        adapter._available_cache = True
        req = AnalysisRequest(mode="classify", prompt="p")
        proc = _make_completed_proc(stdout=_make_envelope(json.dumps({"ok": True})))

        env_with_key = dict(os.environ)
        env_with_key["ANTHROPIC_API_KEY"] = "sk-ant-test-key"

        with patch("os.environ", new=env_with_key), \
             patch("subprocess.run", return_value=proc) as mock_run:
            adapter.run(req)

        _, call_kwargs = mock_run.call_args
        passed_env = call_kwargs.get("env", {})
        self.assertIn("ANTHROPIC_API_KEY", passed_env)
        self.assertEqual(passed_env["ANTHROPIC_API_KEY"], "sk-ant-test-key")

    def test_sanitized_env_google_secret_stripped(self):
        """GOOGLE_OAUTH_CLIENT_SECRET must be absent from the child env."""
        adapter = ClaudeAdapter()
        adapter._available_cache = True
        req = AnalysisRequest(mode="classify", prompt="p")
        proc = _make_completed_proc(stdout=_make_envelope(json.dumps({"ok": True})))

        env_with_secret = dict(os.environ)
        env_with_secret["GOOGLE_OAUTH_CLIENT_SECRET"] = "g-secret"

        with patch("os.environ", new=env_with_secret), \
             patch("subprocess.run", return_value=proc) as mock_run:
            adapter.run(req)

        _, call_kwargs = mock_run.call_args
        passed_env = call_kwargs.get("env", {})
        self.assertNotIn("GOOGLE_OAUTH_CLIENT_SECRET", passed_env)


# ---------------------------------------------------------------------------
# _extract_inner_json — unit
# ---------------------------------------------------------------------------

class TestExtractInnerJson(unittest.TestCase):

    def test_bare_json_object(self):
        self.assertEqual(_extract_inner_json('{"a": 1}'), {"a": 1})

    def test_json_with_fences(self):
        text = '```json\n{"b": 2}\n```'
        self.assertEqual(_extract_inner_json(text), {"b": 2})

    def test_json_with_leading_prose(self):
        text = "Here you go:\n\n{\"c\": 3}"
        self.assertEqual(_extract_inner_json(text), {"c": 3})

    def test_prose_only_returns_none(self):
        self.assertIsNone(_extract_inner_json("No JSON here at all."))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_extract_inner_json(""))

    def test_none_input_returns_none(self):
        self.assertIsNone(_extract_inner_json(None))  # type: ignore[arg-type]

    def test_invalid_json_returns_none(self):
        self.assertIsNone(_extract_inner_json("{not valid json"))

    def test_nested_object(self):
        obj = {"outer": {"inner": [1, 2, 3]}}
        self.assertEqual(_extract_inner_json(json.dumps(obj)), obj)


# ---------------------------------------------------------------------------
# _short_model_name — unit
# ---------------------------------------------------------------------------

class TestShortModelName(unittest.TestCase):

    def test_haiku_extracted(self):
        self.assertEqual(_short_model_name("claude-haiku-4-5-20251001"), "haiku")

    def test_opus_extracted(self):
        self.assertEqual(_short_model_name("claude-opus-4-8"), "opus")

    def test_sonnet_extracted(self):
        self.assertEqual(_short_model_name("claude-sonnet-4-6-20251001"), "sonnet")

    def test_unknown_model_does_not_raise(self):
        result = _short_model_name("some-random-model")
        self.assertIsInstance(result, str)


# ---------------------------------------------------------------------------
# first_available helper
# ---------------------------------------------------------------------------

class TestFirstAvailable(unittest.TestCase):

    def test_returns_none_when_no_adapter_available(self):
        a = ClaudeAdapter()
        a._available_cache = False
        result = first_available(["claude"], registry={"claude": a})
        self.assertIsNone(result)

    def test_returns_first_available(self):
        a = ClaudeAdapter()
        a._available_cache = True
        result = first_available(["claude"], registry={"claude": a})
        self.assertIs(result, a)

    def test_skips_unknown_names(self):
        a = ClaudeAdapter()
        a._available_cache = True
        result = first_available(["openai", "claude"], registry={"claude": a})
        self.assertIs(result, a)


# ---------------------------------------------------------------------------
# _build_clean_env — unit
# ---------------------------------------------------------------------------

class TestBuildCleanEnv(unittest.TestCase):

    def test_path_present(self):
        env = _build_clean_env()
        self.assertIn("PATH", env)

    def test_telegram_token_absent(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret", "TELEGRAM_CHAT_ID": "123"}):
            env = _build_clean_env()
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("TELEGRAM_CHAT_ID", env)

    def test_anthropic_key_present_despite_matching_pattern(self):
        """ANTHROPIC_API_KEY contains no TOKEN/SECRET/PASSWORD, but even if a
        variant did, it must survive because it is in _ENV_KEEP."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant"}):
            env = _build_clean_env()
        self.assertIn("ANTHROPIC_API_KEY", env)

    def test_pattern_key_stripped(self):
        """A key whose name contains SECRET/TOKEN/PASSWORD is removed."""
        with patch.dict(os.environ, {"MY_CUSTOM_SECRET": "x", "DB_PASSWORD": "y"}):
            env = _build_clean_env()
        self.assertNotIn("MY_CUSTOM_SECRET", env)
        self.assertNotIn("DB_PASSWORD", env)


if __name__ == "__main__":
    unittest.main()
