import io
import json
import subprocess
import sys
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

import codex_cli_usage
from codex_cli_usage import _fetch_usage_via_app_server, build_usage_json, fetch_usage, main


def rpc_line(message):
    return json.dumps(message) + "\n"


class FakeStdin(io.StringIO):
    def close(self):
        self.was_closed = True


class FakeProcess:
    def __init__(self, lines=(), returncode=0):
        self.stdin = FakeStdin()
        self.stdout = iter(lines)
        self.returncode = returncode
        self.waited = False
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode

    def poll(self):
        return self.returncode if self.waited or self.terminated else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class BlockingStdout:
    def __init__(self):
        self.stopped = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.stopped.wait()
        raise StopIteration


class BlockingProcess(FakeProcess):
    def __init__(self):
        super().__init__()
        self.stdout = BlockingStdout()

    def terminate(self):
        super().terminate()
        self.stdout.stopped.set()


def app_server_process(result, notifications=()):
    return FakeProcess([
        rpc_line({"id": 1, "result": {"userAgent": "codex"}}),
        *(rpc_line(notification) for notification in notifications),
        rpc_line({"id": 2, "result": result}),
    ])


class AppServerTests(unittest.TestCase):
    def run_adapter(self, process):
        with patch("codex_cli_usage.shutil.which", return_value="/usr/bin/codex"), patch(
            "codex_cli_usage.subprocess.Popen", return_value=process
        ) as popen:
            result = _fetch_usage_via_app_server()
        return result, popen

    def test_successful_initialize_and_rate_limit_response(self):
        process = app_server_process({
            "planType": "pro",
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 12,
                    "windowDurationMins": 300,
                    "resetsAt": 2000000000,
                },
                "secondary": {
                    "usedPercent": 34,
                    "windowDurationMins": 10080,
                    "resetsAt": 2000000100,
                },
            },
        })

        result, popen = self.run_adapter(process)

        self.assertEqual(result["plan_type"], "pro")
        self.assertEqual(result["rate_limit"]["primary_window"]["limit_window_seconds"], 18000)
        self.assertEqual(result["rate_limit"]["secondary_window"]["limit_window_seconds"], 604800)
        self.assertTrue(process.stdin.was_closed)
        self.assertTrue(process.waited)
        command = popen.call_args.args[0]
        self.assertEqual(command, ["/usr/bin/codex", "app-server", "--stdio"])
        self.assertNotIn("shell", popen.call_args.kwargs)
        requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(requests[0]["method"], "initialize")
        self.assertEqual(requests[0]["params"]["clientInfo"]["name"], "codex_cli_usage")
        self.assertEqual(requests[1:], [
            {"method": "initialized"},
            {"method": "account/rateLimits/read", "id": 2},
        ])

    def test_weekly_only_aggregate_response(self):
        process = app_server_process({
            "rateLimits": {
                "primary": {
                    "usedPercent": 3,
                    "windowDurationMins": 10080,
                    "resetsAt": None,
                },
                "secondary": None,
            },
        })

        result, _ = self.run_adapter(process)
        usage = build_usage_json(result)

        self.assertIn("primary", usage)
        self.assertNotIn("secondary", usage)
        self.assertNotIn("5h", usage)
        self.assertEqual(usage["primary"]["window_secs"], 604800)

    def test_additional_bucket_conversion_excludes_aggregate(self):
        aggregate = {
            "limitId": "codex",
            "primary": {"usedPercent": 3, "windowDurationMins": 10080, "resetsAt": None},
        }
        process = app_server_process({
            "rateLimits": aggregate,
            "rateLimitsByLimitId": {
                "codex": aggregate,
                "spark": {
                    "limitId": "spark",
                    "limitName": "Spark",
                    "primary": {"usedPercent": 50, "windowDurationMins": 300, "resetsAt": None},
                },
            },
        })

        result, _ = self.run_adapter(process)
        usage = build_usage_json(result)

        self.assertEqual(len(usage["additional"]), 1)
        self.assertEqual(usage["additional"][0]["name"], "Spark")
        self.assertEqual(usage["additional"][0]["primary"]["window_secs"], 18000)

    def test_notifications_before_matching_response_are_ignored(self):
        process = app_server_process(
            {"rateLimits": {"primary": None, "secondary": None}},
            notifications=(
                {"method": "account/updated", "params": {"authMode": "chatgpt"}},
                {"id": 99, "result": {}},
            ),
        )

        result, _ = self.run_adapter(process)

        self.assertEqual(result["rate_limit"], {})

    def test_eof_malformed_json_and_rpc_errors_fall_back(self):
        cases = (
            FakeProcess([]),
            FakeProcess(["not-json\n"]),
            FakeProcess([rpc_line({"id": 1, "error": {"message": "unsupported"}})]),
            FakeProcess([
                rpc_line({"id": 1, "result": {}}),
                rpc_line({"id": 2, "error": {"message": "method unavailable"}}),
            ]),
        )
        for process in cases:
            with self.subTest(lines=process.stdout):
                result, _ = self.run_adapter(process)
                self.assertIsNone(result)

    def test_timeout_terminates_child(self):
        process = BlockingProcess()
        with patch("codex_cli_usage.APP_SERVER_TIMEOUT", 0.01):
            result, _ = self.run_adapter(process)

        self.assertIsNone(result)
        self.assertTrue(process.terminated)

    def test_missing_or_older_codex_falls_back_to_urllib(self):
        payload = json.dumps({"plan_type": "plus", "rate_limit": {}}).encode()
        http_response = MagicMock()
        http_response.__enter__.return_value.read.return_value = payload
        auth = {"tokens": {"access_token": "access", "account_id": "account"}}
        with patch("codex_cli_usage.shutil.which", return_value=None), patch(
            "codex_cli_usage.get_auth", return_value=auth
        ), patch(
            "codex_cli_usage.urllib.request.urlopen", return_value=http_response
        ) as urlopen:
            result = fetch_usage()

        self.assertEqual(result["plan_type"], "plus")
        urlopen.assert_called_once()

    def test_older_codex_rpc_error_falls_back_to_urllib(self):
        process = FakeProcess([
            rpc_line({"id": 1, "result": {}}),
            rpc_line({"id": 2, "error": {"message": "method unavailable"}}),
        ])
        payload = json.dumps({"plan_type": "plus", "rate_limit": {}}).encode()
        http_response = MagicMock()
        http_response.__enter__.return_value.read.return_value = payload
        auth = {"tokens": {"access_token": "access", "account_id": "account"}}
        with patch("codex_cli_usage.shutil.which", return_value="/usr/bin/codex"), patch(
            "codex_cli_usage.subprocess.Popen", return_value=process
        ), patch("codex_cli_usage.get_auth", return_value=auth), patch(
            "codex_cli_usage.urllib.request.urlopen", return_value=http_response
        ) as urlopen:
            result = fetch_usage()

        self.assertEqual(result["plan_type"], "plus")
        urlopen.assert_called_once()

    def test_rpc_errors_never_expose_credentials_or_tracebacks(self):
        process = FakeProcess([
            rpc_line({"id": 1, "result": {}}),
            rpc_line({
                "id": 2,
                "error": {"message": "access-secret refresh-secret"},
            }),
        ])
        auth = {
            "tokens": {
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "account_id": "account",
            },
        }

        def unauthorized(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("codex_cli_usage.shutil.which", return_value="/usr/bin/codex"), patch(
            "codex_cli_usage.subprocess.Popen", return_value=process
        ), patch("codex_cli_usage.get_auth", return_value=auth), patch(
            "codex_cli_usage.urllib.request.urlopen", side_effect=unauthorized
        ), patch.object(sys, "argv", ["codex-cli-usage", "status"]), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            exit_code = main()

        captured = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertNotIn("Traceback", captured)
        self.assertNotIn("access-secret", captured)
        self.assertNotIn("refresh-secret", captured)


if __name__ == "__main__":
    unittest.main()
