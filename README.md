# codex-cli-usage

Codex CLI usage monitor. Fetches your rate limit data through Codex and displays it in the terminal.

## Example output

`codex-cli-usage` command:

```
Plan: plus
  Weekly               39%  resets 143h26m
```

Codex statusline (self-caching — refreshes from API when stale, no daemon needed):

```
weekly:39% plus reset:143h26m
```

## Install

```bash
uv tool install codex-cli-usage
```

Then run:

```bash
# Check usage once
codex-cli-usage

# Run the daemon (keeps usage-limits.json updated)
codex-cli-usage daemon
```

## Commands

| Command | Description |
|---------|-------------|
| `codex-cli-usage` | Show current usage (colored terminal output) |
| `codex-cli-usage json` | Print raw JSON |
| `codex-cli-usage daemon [-i SECS]` | Run in foreground, refresh every 5 min (customizable) |
| `codex-cli-usage statusline` | Compact statusline (self-caching, no daemon needed) |
| `codex-cli-usage install` | Print setup instructions |

## How Codex CLI rate limiting works

Discovered by inspecting the Codex CLI and its authentication flow.

### Data sources

codex-cli-usage gets rate limit data from:

1. **`codex app-server --stdio`** — Preferred. Codex owns authentication and returns rate limits through `account/rateLimits/read`.
2. **`/backend-api/codex/usage` endpoint** — Compatibility fallback when Codex is missing or its app-server does not support the rate-limit method.

### Rate limit types

| Type | Description |
|------|-------------|
| `primary_window` | Primary window; duration comes from `limit_window_seconds` |
| `secondary_window` | Secondary window; duration comes from `limit_window_seconds` |
| `additional_rate_limits` | Per-model limits (e.g. specific model caps) |
| `code_review_rate_limit` | Code review usage limit |

Window names are derived from `limit_window_seconds`, not from whether the
backend calls a window primary or secondary. Approximately 5-hour, daily,
weekly, monthly, and annual windows receive duration labels. Other or missing
durations are shown as `Primary` or `Secondary`. Additional per-model limits
remain available in JSON for compatibility but are not shown in normal output.

### Cache format

Schema version 2 stores aggregate windows by their backend role while retaining
their actual duration:

```json
{
  "schema_version": 2,
  "primary": {
    "pct": 39,
    "resets_at": "2026-07-18T20:00:00+00:00",
    "window_secs": 604800
  }
}
```

Legacy `5h` and `7d` aliases are written only when the corresponding aggregate
window actually classifies as 5-hour or weekly. Readers should prefer
`primary` and `secondary`.

### Authentication

The OAuth tokens live at `~/.codex/auth.json`, written by the Codex CLI on login.

Codex owns automatic token refresh and credential persistence. The compatibility
fallback reads `auth.json` but never modifies credentials.

### Local files

| File | Written by | Contains |
|------|-----------|----------|
| `~/.codex/auth.json` | Codex CLI | OAuth tokens (access, refresh, id_token) |
| `~/.codex/usage-limits.json` | codex-cli-usage daemon | Cached API usage data (this tool) |
