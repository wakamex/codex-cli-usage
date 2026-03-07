# codexusage

Codex CLI usage monitor. Fetches your rate limit data from OpenAI's ChatGPT backend API and displays it in the terminal.

## Example output

`codexusage` command:

```
Plan: plus
  Session (5h)         39%  resets 1h26m
  Week (7d)            15%  resets 143h26m
```

Codex statusline (self-caching — refreshes from API when stale, no daemon needed):

```
5h:39% 7d:15% plus reset:1h26m
```

## Install

```bash
uv tool install codex-cli-usage
```

Then run:

```bash
# Check usage once
codexusage

# Run the daemon (keeps usage-limits.json updated)
codexusage daemon
```

## Commands

| Command | Description |
|---------|-------------|
| `codexusage` | Show current usage (colored terminal output) |
| `codexusage json` | Print raw JSON |
| `codexusage daemon [-i SECS]` | Run in foreground, refresh every 5 min (customizable) |
| `codexusage statusline` | Compact statusline (self-caching, no daemon needed) |
| `codexusage install` | Print setup instructions |

## How Codex CLI rate limiting works

Discovered by inspecting the Codex CLI and its authentication flow.

### Data sources

Codex CLI gets rate limit data from:

1. **`/backend-api/codex/usage` endpoint** — Returns utilization percentages and reset times for each rate limit window (primary 5h, secondary 7d, plus per-model and code review limits).

### Rate limit types

| Type | Description |
|------|-------------|
| `primary_window` | Rolling 5-hour session window |
| `secondary_window` | Rolling 7-day all-models window |
| `additional_rate_limits` | Per-model limits (e.g. specific model caps) |
| `code_review_rate_limit` | Code review usage limit |

### Authentication

The OAuth tokens live at `~/.codex/auth.json`, written by the Codex CLI on login.

The access token expires roughly hourly. codexusage refreshes it automatically using the stored refresh token.

### Local files

| File | Written by | Contains |
|------|-----------|----------|
| `~/.codex/auth.json` | Codex CLI | OAuth tokens (access, refresh, id_token) |
| `~/.codex/usage-limits.json` | codexusage daemon | Cached API usage data (this tool) |
