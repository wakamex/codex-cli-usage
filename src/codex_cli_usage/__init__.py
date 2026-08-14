#!/usr/bin/env python3
"""codex-cli-usage - Codex CLI usage monitor.

Fetches rate limit data from OpenAI's ChatGPT backend API
through Codex's app-server, with a direct HTTP compatibility fallback.
Zero external Python dependencies.

Usage:
    codex-cli-usage              Show current usage (colored)
    codex-cli-usage status       Same as above
    codex-cli-usage json         Print raw JSON
    codex-cli-usage daemon       Run in foreground, refresh every 5 min, write to ~/.codex/usage-limits.json
    codex-cli-usage statusline   Codex statusline command (reads cache)
    codex-cli-usage install      Print setup instructions
"""

import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_TTY = sys.stdout.isatty()


def _find_codex_path(filename: str) -> Path:
    """Return the first existing path for a .codex file.

    Checks the native path first (~/.codex/<filename>).  On Windows, if the
    native path doesn't exist, also checks WSL distros via //wsl$/.
    """
    native = Path.home() / ".codex" / filename
    if native.exists():
        return native

    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["wsl", "-l", "-q"],
                capture_output=True, timeout=5,
            )
            decoded = result.stdout.decode("utf-16-le", errors="ignore")
            distros = [d.strip() for d in decoded.splitlines() if d.strip()]
        except Exception:
            distros = []

        for distro in distros:
            wsl_home = Path(f"//wsl$/{distro}/home")
            try:
                users = [u for u in wsl_home.iterdir() if u.is_dir()]
            except OSError:
                continue
            for user_dir in users:
                candidate = user_dir / ".codex" / filename
                if candidate.exists():
                    return candidate

    # Fall back to native path even if it doesn't exist yet
    return native


def _find_codex_auth_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "auth.json"
    return _find_codex_path("auth.json")


CODEX_DIR = Path.home() / ".codex"
AUTH_FILE = _find_codex_auth_path()
USAGE_FILE = _find_codex_path("usage-limits.json")
DAEMON_INTERVAL = 300  # 5 minutes
USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
AUTH_REFRESH_ERROR = "Authentication refresh failed. Run codex login again."
USAGE_403_ERROR = "Usage service temporarily rejected the request (HTTP 403). Try again."
USAGE_403_RETRY_DELAYS = (0, 0.25, 0.5, 0.75, 1, 1.5, 2)
APP_SERVER_TIMEOUT = 20
CLIENT_VERSION = "0.1.8"
_RPC_EOF = object()


class AuthenticationError(RuntimeError):
    """Authentication could not be recovered automatically."""


class UsageServiceError(RuntimeError):
    """The usage service rejected an otherwise authenticated request."""


def get_auth() -> dict | None:
    """Read OAuth credentials from Codex's auth file."""
    try:
        return json.loads(AUTH_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_plan(auth: dict | None = None) -> str:
    """Return plan type from auth tokens (decoded from id_token JWT)."""
    if auth is None:
        auth = get_auth()
    if not auth:
        return "unknown"
    # Plan type comes from the usage API, fall back to checking JWT claims
    tokens = auth.get("tokens", {})
    # Try to decode the id_token payload (JWT middle segment)
    id_token = tokens.get("id_token", "")
    try:
        import base64
        payload = id_token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type", "unknown")
    except Exception:
        return "unknown"


def _request_usage(access_token: str, account_id: str) -> dict:
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _request_usage_with_403_retry(access_token: str, account_id: str) -> dict:
    """Retry transient 403 responses without refreshing credentials."""
    last_error = None
    transient_html_403 = False
    for delay in (None, *USAGE_403_RETRY_DELAYS):
        if delay is not None:
            time.sleep(delay)
        try:
            return _request_usage(access_token, account_id)
        except urllib.error.HTTPError as error:
            if error.code != 403:
                raise
            last_error = error
            content_type = error.headers.get_content_type() if error.headers else ""
            transient_html_403 = content_type == "text/html"

    if transient_html_403:
        raise UsageServiceError(USAGE_403_ERROR) from None
    raise last_error


def _read_rpc_lines(stream, messages: queue.Queue) -> None:
    try:
        for line in stream:
            messages.put(line)
    finally:
        messages.put(_RPC_EOF)


def _wait_for_rpc_response(messages: queue.Queue, request_id: int, deadline: float) -> dict | None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = messages.get(timeout=remaining)
        except queue.Empty:
            return None
        if line is _RPC_EOF:
            return None
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(message, dict):
            return None
        if message.get("id") == request_id:
            return message


def _stop_app_server(process: subprocess.Popen, deadline: float) -> bool:
    if process.stdin and not process.stdin.closed:
        process.stdin.close()
    try:
        return process.wait(timeout=max(0, deadline - time.monotonic())) == 0
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return False


def _rate_limit_snapshot(snapshot: dict) -> dict:
    result = {}
    for kind in ("primary", "secondary"):
        window = snapshot.get(kind)
        if not isinstance(window, dict):
            continue
        duration_mins = window.get("windowDurationMins")
        result[f"{kind}_window"] = {
            "used_percent": window.get("usedPercent"),
            "reset_at": window.get("resetsAt"),
            "limit_window_seconds": duration_mins * 60
            if isinstance(duration_mins, int | float) and not isinstance(duration_mins, bool)
            else None,
        }
    return result


def _adapt_app_server_rate_limits(result: dict) -> dict | None:
    aggregate = result.get("rateLimits")
    if not isinstance(aggregate, dict):
        return None

    api_data = {
        "plan_type": result.get("planType") or get_plan(),
        "rate_limit": _rate_limit_snapshot(aggregate),
    }
    by_limit_id = result.get("rateLimitsByLimitId")
    if not isinstance(by_limit_id, dict):
        return api_data

    aggregate_id = aggregate.get("limitId") or "codex"
    additional = []
    for limit_id, snapshot in by_limit_id.items():
        if (
            limit_id in {aggregate_id, "codex"}
            or not isinstance(snapshot, dict)
            or snapshot == aggregate
        ):
            continue
        additional.append({
            "limit_name": snapshot.get("limitName") or limit_id,
            "rate_limit": _rate_limit_snapshot(snapshot),
        })
    if additional:
        api_data["additional_rate_limits"] = additional
    return api_data


def _fetch_usage_via_app_server() -> dict | None:
    """Fetch usage through Codex's authenticated JSON-RPC app-server."""
    codex = shutil.which("codex")
    if not codex:
        return None

    deadline = time.monotonic() + APP_SERVER_TIMEOUT
    try:
        process = subprocess.Popen(
            [codex, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except OSError:
        return None

    stopped = False
    try:
        if process.stdin is None or process.stdout is None:
            return None
        messages = queue.Queue()
        threading.Thread(
            target=_read_rpc_lines,
            args=(process.stdout, messages),
            daemon=True,
        ).start()

        initialize = {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "codex_cli_usage",
                    "title": "Codex CLI Usage",
                    "version": CLIENT_VERSION,
                },
            },
        }
        process.stdin.write(json.dumps(initialize) + "\n")
        process.stdin.flush()
        initialized = _wait_for_rpc_response(messages, 1, deadline)
        if not initialized or initialized.get("error") or "result" not in initialized:
            return None

        process.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        process.stdin.write(json.dumps({
            "method": "account/rateLimits/read",
            "id": 2,
        }) + "\n")
        process.stdin.flush()
        response = _wait_for_rpc_response(messages, 2, deadline)
        if not response or response.get("error") or not isinstance(response.get("result"), dict):
            return None
        usage = _adapt_app_server_rate_limits(response["result"])
        if usage is None:
            return None
        stopped = _stop_app_server(process, deadline)
        return usage if stopped else None
    except (BrokenPipeError, OSError, ValueError):
        return None
    finally:
        if not stopped and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _fetch_usage_direct() -> dict:
    """Compatibility fallback using auth.json and urllib directly.

    Returns the raw API response with rate limit windows, plan info, etc.
    """
    auth = get_auth()
    if not auth:
        raise RuntimeError(f"No auth at {AUTH_FILE}. Run `codex` first")

    tokens = auth.get("tokens", {})
    access_token = tokens.get("access_token")
    if not access_token:
        raise RuntimeError("No access token in auth.json")

    account_id = tokens.get("account_id", "")

    try:
        return _request_usage_with_403_retry(access_token, account_id)
    except urllib.error.HTTPError as error:
        if error.code not in (401, 403):
            raise

    # Codex may have rotated credentials while this process was requesting.
    latest = get_auth()
    latest_tokens = latest.get("tokens", {}) if latest else {}
    latest_access_token = latest_tokens.get("access_token")
    if latest_access_token and latest_access_token != access_token:
        auth = latest
        access_token = latest_access_token
        account_id = latest_tokens.get("account_id", account_id)
        try:
            return _request_usage_with_403_retry(access_token, account_id)
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403):
                raise

    raise AuthenticationError(AUTH_REFRESH_ERROR) from None


def fetch_usage() -> dict:
    """Fetch usage through Codex, falling back to direct HTTP for compatibility."""
    usage = _fetch_usage_via_app_server()
    return usage if usage is not None else _fetch_usage_direct()


_WINDOW_CLASSES = (
    ("5h", "5-hour", 5 * 3600),
    ("daily", "Daily", 24 * 3600),
    ("weekly", "Weekly", 7 * 24 * 3600),
    ("monthly", "Monthly", 30 * 24 * 3600),
    ("annual", "Annual", 365 * 24 * 3600),
)


def classify_window(seconds: int | float | None, kind: str) -> tuple[str, str]:
    """Derive a stable key and display label from a window duration."""
    if not isinstance(seconds, int | float) or isinstance(seconds, bool) or seconds <= 0:
        return kind, kind.title()
    for key, label, expected in _WINDOW_CLASSES:
        if abs(seconds - expected) / expected <= 0.1:
            return key, label
    return kind, kind.title()


def _build_window(kind: str, window: dict) -> dict:
    seconds = window.get("limit_window_seconds")
    key, label = classify_window(seconds, kind)
    reset_at = window.get("reset_at")
    return {
        "kind": kind,
        "pct": window.get("used_percent"),
        "resets_at": datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat() if reset_at else None,
        "window_secs": seconds,
        "key": key,
        "label": label,
    }


def build_usage_json(api_data: dict) -> dict:
    """Transform API response into our cached format."""
    plan = api_data.get("plan_type", "unknown")
    rl = api_data.get("rate_limit") or {}

    result = {
        "schema_version": 2,
        "plan": plan,
        "source": "api",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    for kind in ("primary", "secondary"):
        raw_window = rl.get(f"{kind}_window")
        if not isinstance(raw_window, dict):
            continue
        window = _build_window(kind, raw_window)
        result[kind] = {
            "pct": window["pct"],
            "resets_at": window["resets_at"],
            "window_secs": window["window_secs"],
        }
        legacy_key = {"5h": "5h", "weekly": "7d"}.get(window["key"])
        if legacy_key and legacy_key not in result:
            result[legacy_key] = dict(result[kind])

    # Preserve additional limits in JSON for compatibility, but normal output
    # intentionally shows aggregate Codex limits only.
    additional = api_data.get("additional_rate_limits") or []
    if additional:
        result["additional"] = []
        for item in additional:
            entry = {"name": item.get("limit_name", "")}
            sub_rl = item.get("rate_limit") or {}
            for kind in ("primary", "secondary"):
                raw_window = sub_rl.get(f"{kind}_window")
                if isinstance(raw_window, dict):
                    window = _build_window(kind, raw_window)
                    entry[kind] = {
                        "pct": window["pct"],
                        "resets_at": window["resets_at"],
                        "window_secs": window["window_secs"],
                    }
            result["additional"].append(entry)

    # Code review limits
    cr = api_data.get("code_review_rate_limit") or {}
    cr_primary = cr.get("primary_window")
    if cr_primary and cr_primary.get("used_percent", 0) > 0:
        result["code_review"] = {
            "pct": cr_primary["used_percent"],
            "resets_at": datetime.fromtimestamp(cr_primary["reset_at"], tz=timezone.utc).isoformat() if cr_primary.get("reset_at") else None,
        }

    # Credits
    credits = api_data.get("credits") or {}
    if credits and credits.get("has_credits"):
        result["credits"] = credits

    return result


def usage_windows(usage: dict) -> list[dict]:
    """Enumerate schema-v2 windows, falling back to duration-keyed caches."""
    result = []
    if usage.get("schema_version", 1) >= 2 or any(key in usage for key in ("primary", "secondary")):
        for kind in ("primary", "secondary"):
            bucket = usage.get(kind)
            if not isinstance(bucket, dict):
                continue
            key, label = classify_window(bucket.get("window_secs"), kind)
            result.append({**bucket, "kind": kind, "key": key, "label": label})
        return result

    for legacy_key, kind, fallback_seconds in (
        ("5h", "primary", 5 * 3600),
        ("7d", "secondary", 7 * 24 * 3600),
    ):
        bucket = usage.get(legacy_key)
        if not isinstance(bucket, dict):
            continue
        seconds = bucket.get("window_secs")
        if not isinstance(seconds, int | float) or isinstance(seconds, bool) or seconds <= 0:
            seconds = fallback_seconds
        key, label = classify_window(seconds, kind)
        result.append({
            "kind": kind,
            "pct": bucket.get("pct"),
            "resets_at": bucket.get("resets_at"),
            "window_secs": seconds,
            "key": key,
            "label": label,
        })
    return result


def write_usage_file(data: dict):
    """Write usage data to ~/.codex/usage-limits.json."""
    USAGE_FILE.write_text(json.dumps(data, indent=2) + "\n")


# -- CLI commands --

def cmd_status(raw_json=False):
    """Fetch and display current usage."""
    api_data = fetch_usage()
    data = build_usage_json(api_data)

    if raw_json:
        print(json.dumps(data, indent=2))
        return

    R = "\033[0;31m" if _TTY else ""
    Y = "\033[0;33m" if _TTY else ""
    G = "\033[0;32m" if _TTY else ""
    D = "\033[0;90m" if _TTY else ""
    RST = "\033[0m" if _TTY else ""

    def color_pct(pct):
        p = int(pct)
        c = R if p >= 70 else Y if p >= 50 else G
        return f"{c}{p}%{RST}"

    def fmt_reset(iso):
        if not iso:
            return ""
        try:
            reset = datetime.fromisoformat(iso)
            now = datetime.now(timezone.utc)
            secs = int((reset - now).total_seconds())
            if secs <= 0:
                return ""
            m = secs // 60
            if m >= 60:
                return f" resets {m // 60}h{m % 60}m"
            return f" resets {m}m"
        except Exception:
            return ""

    print(f"Plan: {data.get('plan', '?')}")
    for window in usage_windows(data):
        if window.get("pct") is None:
            continue
        reset = fmt_reset(window.get("resets_at"))
        print(f"  {window['label']:20s} {color_pct(window['pct'])}{D}{reset}{RST}")

    cr = data.get("code_review")
    if cr:
        print(f"  {'Code Review':20s} {color_pct(cr['pct'])}{D}{fmt_reset(cr.get('resets_at'))}{RST}")


def cmd_daemon(interval: int = DAEMON_INTERVAL):
    """Run in foreground, refresh every `interval` seconds."""
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    print(f"codex-cli-usage daemon started (refreshing every {interval}s)")
    print(f"Writing to {USAGE_FILE}")

    while True:
        try:
            api_data = fetch_usage()
            data = build_usage_json(api_data)
            write_usage_file(data)
            pcts = []
            for window in usage_windows(data):
                if window.get("pct") is not None:
                    pcts.append(f"{window['key']}:{int(window['pct'])}%")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {' '.join(pcts)}")
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e}", file=sys.stderr)

        time.sleep(interval)


def _get_cached_usage(max_age: int = DAEMON_INTERVAL) -> dict:
    """Read cached usage, refreshing from API if stale or missing."""
    try:
        usage = json.loads(USAGE_FILE.read_text())
        updated = datetime.fromisoformat(usage["updated_at"])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        if age < max_age:
            return usage
    except Exception:
        pass
    try:
        api_data = fetch_usage()
        usage = build_usage_json(api_data)
        write_usage_file(usage)
        return usage
    except Exception:
        try:
            return json.loads(USAGE_FILE.read_text())
        except Exception:
            return {}


def cmd_statusline():
    """Statusline command. Reads cached usage and prints compact summary."""
    R = "\033[0;31m" if _TTY else ""
    Y = "\033[0;33m" if _TTY else ""
    G = "\033[0;32m" if _TTY else ""
    D = "\033[0;90m" if _TTY else ""
    RST = "\033[0m" if _TTY else ""

    def color_pct(pct: int) -> str:
        c = R if pct >= 70 else Y if pct >= 50 else G
        return f"{c}{pct}%{RST}"

    def fmt_reset(iso: str | None) -> str:
        if not iso:
            return ""
        try:
            reset = datetime.fromisoformat(iso)
            secs = int((reset - datetime.now(timezone.utc)).total_seconds())
            if secs <= 0:
                return ""
            m = secs // 60
            if m >= 60:
                return f"{m // 60}h{m % 60}m"
            return f"{m}m"
        except Exception:
            return ""

    usage = _get_cached_usage()

    plan = usage.get("plan", "?")
    windows = usage_windows(usage)

    parts = []

    for window in windows:
        if window.get("pct") is not None:
            parts.append(f"{window['key']}:{color_pct(int(window['pct']))}")

    parts.append(f"{D}{plan}{RST}")

    reset = fmt_reset(windows[0].get("resets_at")) if windows else ""
    if reset:
        parts.append(f"{D}reset:{reset}{RST}")

    print(" ".join(parts))


def cmd_install():
    """Print setup instructions."""
    print("""codex-cli-usage setup
================

1. Run the daemon (in a terminal, tmux, or systemd):
   codex-cli-usage daemon

2. Quick check:
   codex-cli-usage         # show current usage
   codex-cli-usage json    # raw JSON output

3. The daemon writes to ~/.codex/usage-limits.json every 5 minutes.
""")


def main():
    parser = argparse.ArgumentParser(description="Codex CLI usage monitor")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="Show current usage (default)")
    sub.add_parser("json", help="Print raw JSON")
    daemon_parser = sub.add_parser("daemon", help="Run refresh daemon")
    daemon_parser.add_argument("-i", "--interval", type=int, default=DAEMON_INTERVAL,
                               help=f"Refresh interval in seconds (default: {DAEMON_INTERVAL})")
    sub.add_parser("statusline", help="Compact statusline (reads cache)")
    sub.add_parser("install", help="Print setup instructions")
    args = parser.parse_args()

    cmd = args.command or "status"
    try:
        if cmd == "status":
            cmd_status()
        elif cmd == "json":
            cmd_status(raw_json=True)
        elif cmd == "daemon":
            cmd_daemon(interval=args.interval)
        elif cmd == "statusline":
            cmd_statusline()
        elif cmd == "install":
            cmd_install()
    except (AuthenticationError, UsageServiceError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
