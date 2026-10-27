"""Workspace header is sent only when ANTHROPIC_WORKSPACE_ID is set."""

import importlib
import logging
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402


FAKE_KEY = "sk-ant-test"
FAKE_WORKSPACE = "wrkspc_test"


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _header_map(headers):
    return {key.lower(): value for key, value in headers.items()}


def _capture_request_headers(client):
    """Replace the HTTP transport and return headers from the first attempt."""
    import httpx2

    captured = []

    def fake_send(request, **kwargs):
        captured.append(_header_map(request.headers))
        return httpx2.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "stopped"},
            },
            request=request,
        )

    client._client.send = fake_send
    try:
        client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8,
            messages=[{"role": "user", "content": "hi"}],
        )
    except Exception:
        pass
    if not captured:
        raise AssertionError("client did not attempt an HTTP request")
    return captured[0]


class AnthropicClientTests(unittest.TestCase):
    def setUp(self):
        self._log_handler = _ListHandler()
        self._log_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self._log_handler)

    def tearDown(self):
        logging.getLogger().removeHandler(self._log_handler)
        importlib.reload(config)

    def _assert_secrets_not_logged(self):
        logged = "\n".join(self._log_handler.messages)
        self.assertNotIn(FAKE_KEY, logged)
        self.assertNotIn(FAKE_WORKSPACE, logged)

    def test_config_reads_workspace_id_from_env(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_WORKSPACE_ID": FAKE_WORKSPACE}):
            importlib.reload(config)
            self.assertEqual(config.ANTHROPIC_WORKSPACE_ID, FAKE_WORKSPACE)
        with mock.patch.dict(os.environ, {"ANTHROPIC_WORKSPACE_ID": "  "}):
            importlib.reload(config)
            self.assertEqual(config.ANTHROPIC_WORKSPACE_ID, "")

    def test_header_sent_when_workspace_id_set(self):
        with mock.patch.object(config, "ANTHROPIC_API_KEY", FAKE_KEY), mock.patch.object(
            config, "ANTHROPIC_WORKSPACE_ID", FAKE_WORKSPACE
        ):
            client = config.create_anthropic_client()
            headers = _capture_request_headers(client)
        self.assertEqual(headers.get("anthropic-workspace-id"), FAKE_WORKSPACE)
        self.assertEqual(
            _header_map(client.default_headers).get("anthropic-workspace-id"),
            FAKE_WORKSPACE,
        )
        self._assert_secrets_not_logged()

    def test_header_absent_when_workspace_id_blank(self):
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                with mock.patch.object(config, "ANTHROPIC_API_KEY", FAKE_KEY), mock.patch.object(
                    config, "ANTHROPIC_WORKSPACE_ID", blank
                ):
                    client = config.create_anthropic_client()
                    headers = _capture_request_headers(client)
                self.assertIsNone(headers.get("anthropic-workspace-id"))
                self.assertNotIn("anthropic-workspace-id", _header_map(client.default_headers))
                self.assertEqual(dict(client._custom_headers), {})
        self._assert_secrets_not_logged()

    def test_only_helper_constructs_clients(self):
        allowed = {os.path.join(ROOT, "config.py")}
        offenders = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in {".git", "__pycache__", "venv", ".venv", "tests"}
            ]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                if path in allowed:
                    continue
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
                if "anthropic.Anthropic(" in text or "anthropic.AsyncAnthropic(" in text:
                    offenders.append(os.path.relpath(path, ROOT))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
