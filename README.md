# codex-cli-usage

`codex-cli-usage` shows your Codex rate limits from the terminal: how much of the 5-hour and weekly limits you have used, and when they reset.

```sh
uv tool install codex-cli-usage
codex-cli-usage
```

`codex-cli-usage` is developed in [clanker-usage](https://github.com/wakamex/clanker-usage), which also has the documentation, the issue tracker, and matching monitors for Claude Code, Antigravity, Gemini CLI, Cursor, Devin, DeepSeek, OpenCode Go and Z.ai. This repository holds its history up to version 0.1.10 and is read-only.

Every version stays on PyPI. 0.4.0 changed the options, JSON output and cache location ([release notes](https://github.com/wakamex/clanker-usage/releases/tag/v0.4.0)); to keep the earlier behavior, install `codex-cli-usage==0.1.10`.
