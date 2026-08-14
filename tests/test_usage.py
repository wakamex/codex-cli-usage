import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from codex_cli_usage import (
    UsageServiceError,
    build_usage_json,
    classify_window,
    cmd_status,
    cmd_statusline,
    usage_windows,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class UsageWindowTests(unittest.TestCase):
    def test_primary_5h_and_secondary_weekly(self):
        usage = build_usage_json(fixture("primary_5h_secondary_weekly.json"))

        self.assertEqual(usage["schema_version"], 2)
        self.assertEqual(usage["primary"]["window_secs"], 18000)
        self.assertEqual(usage["secondary"]["window_secs"], 604800)
        self.assertEqual(usage["5h"]["window_secs"], 18000)
        self.assertEqual(usage["7d"]["window_secs"], 604800)
        self.assertIn("additional", usage)

    def test_weekly_primary_only_does_not_fabricate_5h(self):
        usage = build_usage_json(fixture("primary_weekly_only.json"))

        self.assertIn("primary", usage)
        self.assertNotIn("secondary", usage)
        self.assertEqual(usage["primary"]["window_secs"], 604800)
        self.assertNotIn("5h", usage)
        self.assertIn("7d", usage)

    def test_daily_monthly_and_unknown_windows(self):
        cases = fixture("duration_variants.json")

        for case in cases:
            with self.subTest(case=case["name"]):
                usage = build_usage_json(case["response"])
                window = usage_windows(usage)[0]
                self.assertEqual(window["key"], case["key"])
                self.assertEqual(window["label"], case["label"])
                self.assertNotIn("5h", usage)
                self.assertNotIn("7d", usage)

    def test_approximately_classifies_supported_durations(self):
        self.assertEqual(classify_window(5.25 * 3600, "primary")[0], "5h")
        self.assertEqual(classify_window(23 * 3600, "primary")[0], "daily")
        self.assertEqual(classify_window(604799, "primary"), ("weekly", "Weekly"))
        self.assertEqual(classify_window(31 * 86400, "primary")[0], "monthly")
        self.assertEqual(classify_window(366 * 86400, "primary")[0], "annual")

    def test_nullable_and_missing_rate_limits(self):
        for case in fixture("nullable_missing.json"):
            with self.subTest(case=case["name"]):
                usage = build_usage_json(case["response"])
                self.assertEqual(usage_windows(usage), case["windows"])

    def test_unknown_window_with_nullable_fields_is_retained(self):
        usage = build_usage_json({
            "rate_limit": {
                "primary_window": {
                    "used_percent": None,
                    "reset_at": None,
                    "limit_window_seconds": None,
                },
            },
        })

        self.assertEqual(usage_windows(usage)[0], {
            "kind": "primary",
            "pct": None,
            "resets_at": None,
            "key": "primary",
            "label": "Primary",
            "window_secs": None,
        })

    def test_malformed_window_values_are_unavailable(self):
        invalid_windows = (
            {"used_percent": float("nan")},
            {"used_percent": 1, "limit_window_seconds": float("inf")},
            {"used_percent": 1, "reset_at": "not-a-timestamp"},
            {"used_percent": 1, "reset_at": 10**30},
        )

        for window in invalid_windows:
            with self.subTest(window=window), self.assertRaisesRegex(
                UsageServiceError, "Invalid rate limit response"
            ):
                build_usage_json({"rate_limit": {"primary_window": window}})

    def test_status_shows_one_weekly_row_and_no_spark_bucket(self):
        output = StringIO()
        with patch(
            "codex_cli_usage.fetch_usage",
            return_value=fixture("primary_weekly_only.json"),
        ), redirect_stdout(output):
            cmd_status()

        self.assertEqual(output.getvalue().count("Weekly"), 1)
        self.assertNotIn("5-hour", output.getvalue())
        self.assertNotIn("Spark", output.getvalue())

    def test_statusline_enumerates_structured_windows(self):
        usage = build_usage_json(fixture("primary_5h_secondary_weekly.json"))
        output = StringIO()
        with patch("codex_cli_usage._get_cached_usage", return_value=usage), redirect_stdout(output):
            cmd_statusline()

        self.assertIn("5h:12%", output.getvalue())
        self.assertIn("weekly:34%", output.getvalue())
        self.assertNotIn("Spark", output.getvalue())

    def test_old_cache_fallback_honors_stored_duration(self):
        windows = usage_windows({"5h": {"pct": 3, "window_secs": 604800}})

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["key"], "weekly")
        self.assertEqual(windows[0]["label"], "Weekly")
if __name__ == "__main__":
    unittest.main()
