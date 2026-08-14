import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

import codex_cli_usage
from codex_cli_usage import AUTH_REFRESH_ERROR, fetch_usage, main


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


def http_error(request, code, body=None, content_type=None):
    headers = Message()
    if content_type:
        headers["Content-Type"] = content_type
    stream = BytesIO(body.encode()) if body is not None else None
    return urllib.error.HTTPError(request.full_url, code, "error", headers, stream)


class AuthenticationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.auth_file = Path(self.directory.name) / "auth.json"
        self.auth = {
            "auth_mode": "chatgpt",
            "last_refresh": "old",
            "unknown_top_level": {"keep": True},
            "tokens": {
                "access_token": "access-old-secret",
                "refresh_token": "refresh-old-secret",
                "id_token": "id-old-secret",
                "account_id": "account-old",
                "unknown_token_field": "keep-me",
            },
        }
        self.write_auth(self.auth)
        self.auth_patch = patch.object(codex_cli_usage, "AUTH_FILE", self.auth_file)
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.app_server_patch = patch(
            "codex_cli_usage._fetch_usage_via_app_server",
            return_value=None,
        )
        self.app_server_patch.start()
        self.addCleanup(self.app_server_patch.stop)

    def write_auth(self, auth):
        self.auth_file.write_text(json.dumps(auth))

    def test_codex_home_sets_direct_auth_path(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"CODEX_HOME": tmp}, clear=True
        ):
            self.assertEqual(
                codex_cli_usage._find_codex_auth_path(), Path(tmp) / "auth.json"
            )

    def test_transient_403_retries_same_token_without_refresh(self):
        requests = []

        def urlopen(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise http_error(request, 403)
            return Response({"plan_type": "plus"})

        with patch("codex_cli_usage.urllib.request.urlopen", side_effect=urlopen):
            result = fetch_usage()

        self.assertEqual(result, {"plan_type": "plus"})
        self.assertEqual([request.full_url for request in requests], [
            codex_cli_usage.USAGE_URL,
            codex_cli_usage.USAGE_URL,
        ])
        self.assertEqual(
            [request.get_header("Authorization") for request in requests],
            ["Bearer access-old-secret", "Bearer access-old-secret"],
        )

    def test_html_403_never_triggers_token_refresh(self):
        requests = []

        def urlopen(request, timeout):
            requests.append(request)
            raise http_error(
                request,
                403,
                body="<html>temporary rejection</html>",
                content_type="text/html",
            )

        stdout = StringIO()
        stderr = StringIO()
        with patch("codex_cli_usage.urllib.request.urlopen", side_effect=urlopen), patch(
            "codex_cli_usage.time.sleep"
        ), patch("sys.argv", ["codex-cli-usage", "status"]), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            exit_code = main()

        captured = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(requests), 8)
        self.assertTrue(all(request.full_url == codex_cli_usage.USAGE_URL for request in requests))
        self.assertIn("temporarily rejected", stderr.getvalue())
        for secret in ("access-old-secret", "refresh-old-secret"):
            self.assertNotIn(secret, captured)

    def test_401_reloads_concurrently_updated_access_token(self):
        requests = []

        def urlopen(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                updated = json.loads(json.dumps(self.auth))
                updated["tokens"]["access_token"] = "access-concurrent-secret"
                updated["tokens"]["account_id"] = "account-new"
                self.write_auth(updated)
                raise http_error(request, 401)
            return Response({"plan_type": "plus"})

        with patch("codex_cli_usage.urllib.request.urlopen", side_effect=urlopen):
            fetch_usage()

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].full_url, codex_cli_usage.USAGE_URL)
        self.assertEqual(
            requests[1].get_header("Authorization"),
            "Bearer access-concurrent-secret",
        )
        self.assertEqual(requests[1].get_header("Chatgpt-account-id"), "account-new")

    def test_refresh_401_is_concise_and_never_prints_credentials(self):
        def urlopen(request, timeout):
            raise http_error(request, 401)

        stdout = StringIO()
        stderr = StringIO()
        with patch("codex_cli_usage.urllib.request.urlopen", side_effect=urlopen), patch(
            "sys.argv", ["codex-cli-usage", "status"]
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main()

        captured = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue().strip(), f"Error: {AUTH_REFRESH_ERROR}")
        self.assertNotIn("Traceback", captured)
        for secret in ("access-old-secret", "refresh-old-secret", "id-old-secret"):
            self.assertNotIn(secret, captured)


if __name__ == "__main__":
    unittest.main()
