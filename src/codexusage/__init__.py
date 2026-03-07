#!/usr/bin/env python3
"""codexusage - Codex CLI usage monitor.

Fetches rate limit data from OpenAI's ChatGPT backend API
using your Codex CLI OAuth token. Zero external dependencies.

Usage:
    codexusage              Show current usage (colored)
    codexusage status       Same as above
    codexusage json         Print raw JSON
    codexusage daemon       Run in foreground, refresh every 5 min, write to ~/.codex/usage-limits.json
    codexusage statusline   Codex statusline command (reads cache)
    codexusage install      Print setup instructions
"""

import argparse
import json
import signal
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CODEX_DIR = Path.home() / ".codex"
AUTH_FILE = CODEX_DIR / "auth.json"
USAGE_FILE = CODEX_DIR / "usage-limits.json"
DAEMON_INTERVAL = 300  # 5 minutes
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"


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


def refresh_access_token(auth: dict) -> str:
    """Refresh the access token using the refresh token."""
    tokens = auth.get("tokens", {})
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No refresh token in auth.json")

    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }).encode()

    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())

    return result["access_token"]


def fetch_usage() -> dict:
    """Fetch usage from ChatGPT's backend API.

    Returns the raw API response with rate limit windows, plan info, etc.
    """
    auth = get_auth()
    if not auth:
        raise RuntimeError("No auth at ~/.codex/auth.json — run `codex` first")

    tokens = auth.get("tokens", {})
    access_token = tokens.get("access_token")
    if not access_token:
        raise RuntimeError("No access token in auth.json")

    account_id = tokens.get("account_id", "")

    # Try with current token, refresh if needed
    for attempt in range(2):
        req = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "chatgpt-account-id": account_id,
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                access_token = refresh_access_token(auth)
            else:
                raise

    raise RuntimeError("Failed to fetch usage after token refresh")


def build_usage_json(api_data: dict) -> dict:
    """Transform API response into our cached format."""
    plan = api_data.get("plan_type", "unknown")
    rl = api_data.get("rate_limit", {})

    result = {
        "plan": plan,
        "source": "api",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    primary = rl.get("primary_window")
    if primary:
        result["5h"] = {
            "pct": primary["used_percent"],
            "resets_at": datetime.fromtimestamp(primary["reset_at"], tz=timezone.utc).isoformat() if primary.get("reset_at") else None,
            "window_secs": primary.get("limit_window_seconds"),
        }

    secondary = rl.get("secondary_window")
    if secondary:
        result["7d"] = {
            "pct": secondary["used_percent"],
            "resets_at": datetime.fromtimestamp(secondary["reset_at"], tz=timezone.utc).isoformat() if secondary.get("reset_at") else None,
            "window_secs": secondary.get("limit_window_seconds"),
        }

    # Additional rate limits (e.g. per-model limits)
    additional = api_data.get("additional_rate_limits", [])
    if additional:
        result["additional"] = []
        for item in additional:
            name = item.get("limit_name", "")
            sub_rl = item.get("rate_limit", {})
            entry = {"name": name}
            p = sub_rl.get("primary_window")
            if p:
                entry["primary"] = {
                    "pct": p["used_percent"],
                    "resets_at": datetime.fromtimestamp(p["reset_at"], tz=timezone.utc).isoformat() if p.get("reset_at") else None,
                }
            s = sub_rl.get("secondary_window")
            if s:
                entry["secondary"] = {
                    "pct": s["used_percent"],
                    "resets_at": datetime.fromtimestamp(s["reset_at"], tz=timezone.utc).isoformat() if s.get("reset_at") else None,
                }
            result["additional"].append(entry)

    # Code review limits
    cr = api_data.get("code_review_rate_limit", {})
    cr_primary = cr.get("primary_window")
    if cr_primary and cr_primary.get("used_percent", 0) > 0:
        result["code_review"] = {
            "pct": cr_primary["used_percent"],
            "resets_at": datetime.fromtimestamp(cr_primary["reset_at"], tz=timezone.utc).isoformat() if cr_primary.get("reset_at") else None,
        }

    # Credits
    credits = api_data.get("credits", {})
    if credits and credits.get("has_credits"):
        result["credits"] = credits

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

    R = "\033[0;31m"
    Y = "\033[0;33m"
    G = "\033[0;32m"
    D = "\033[0;90m"
    RST = "\033[0m"

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
    for label, key in [
        ("Session (5h)", "5h"),
        ("Week (7d)", "7d"),
    ]:
        bucket = data.get(key)
        if bucket:
            pct = bucket["pct"]
            reset = fmt_reset(bucket.get("resets_at"))
            print(f"  {label:20s} {color_pct(pct)}{D}{reset}{RST}")

    # Additional per-model limits
    for item in data.get("additional", []):
        name = item["name"]
        for wlabel, wkey in [("5h", "primary"), ("7d", "secondary")]:
            w = item.get(wkey)
            if w and w.get("pct", 0) > 0:
                label = f"{name} ({wlabel})"
                print(f"  {label:20s} {color_pct(w['pct'])}{D}{fmt_reset(w.get('resets_at'))}{RST}")

    cr = data.get("code_review")
    if cr:
        print(f"  {'Code Review':20s} {color_pct(cr['pct'])}{D}{fmt_reset(cr.get('resets_at'))}{RST}")


def cmd_daemon(interval: int = DAEMON_INTERVAL):
    """Run in foreground, refresh every `interval` seconds."""
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    print(f"codexusage daemon started (refreshing every {interval}s)")
    print(f"Writing to {USAGE_FILE}")

    while True:
        try:
            api_data = fetch_usage()
            data = build_usage_json(api_data)
            write_usage_file(data)
            pcts = []
            for key in ("5h", "7d"):
                b = data.get(key)
                if b:
                    pcts.append(f"{key}:{int(b['pct'])}%")
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
    R = "\033[0;31m"
    Y = "\033[0;33m"
    G = "\033[0;32m"
    D = "\033[0;90m"
    RST = "\033[0m"

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
    five_h = usage.get("5h", {})
    seven_d = usage.get("7d", {})

    parts = []

    if five_h:
        parts.append(f"5h:{color_pct(int(five_h.get('pct', 0)))}")
    if seven_d:
        parts.append(f"7d:{color_pct(int(seven_d.get('pct', 0)))}")

    parts.append(f"{D}{plan}{RST}")

    reset = fmt_reset(five_h.get("resets_at"))
    if reset:
        parts.append(f"{D}reset:{reset}{RST}")

    print(" ".join(parts))


def cmd_install():
    """Print setup instructions."""
    print("""codexusage setup
================

1. Run the daemon (in a terminal, tmux, or systemd):
   codexusage daemon

2. Quick check:
   codexusage         # show current usage
   codexusage json    # raw JSON output

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


if __name__ == "__main__":
    main()
