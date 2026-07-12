# Cache Assistant

A Claude Code plugin to **understand and control your prompt-cache window**.

Claude Code re-caches your whole conversation prefix on every turn, and that
cache expires after a period of inactivity — 5 minutes or 1 hour depending on how
you're billed. Let it lapse and your next message pays a slow, expensive cold
re-write. Cache Assistant makes that window visible and gives you guardrails.

## What you get

- **Status line row** — the current cache **tier** (`5m` / `1h`) and a live
  `mm:ss` **countdown** to expiry, ticking every second. It reads the *current*
  tier from the transcript each tick, so a mid-session tier switch (e.g. `1h → 5m`
  on usage overage) re-bases the countdown immediately.
- **Cache-expiry guard** — before a send, if the window has already expired, the
  first attempt is **blocked** with an explanation and a token estimate for the
  cold re-write. Send again to proceed.
- **Model / effort-change guard** — switching model or reasoning effort busts the
  whole cache. The first message under the new setting is **blocked** so you can
  revert without losing your warm cache. Send again to proceed.
- **`install-statusline` skill** — adds the row to your status line
  **non-destructively**, wrapping any status line you already have.
- **`keep-cache-alive` skill** — a tier-aware loop that pings the session to keep
  the window warm while you're away, with a sensible ping cap.
- **`/cache-status` command** — an on-demand readout of tier, countdown, and cold
  re-cache cost.

See [`CLAUDE.md`](CLAUDE.md) for the cache-window model this is built on and how
the code is laid out.

## Install

Add Henrik's shared marketplace, then install Cache Assistant:

```
/plugin marketplace add HenrikSchnettler/claude-plugins
/plugin install cache-assistant@henriks-claude-plugins
```

For plugin development from a local clone:

```
claude --plugin-dir /path/to/cache-assistant
```

Then add the status line:

```
/install-statusline
```

…and restart Claude Code. The row appears as `⚡ cache 1h · 57:12 left`
(green healthy · yellow expiring · red expired).

## How it works

A single Python engine (`lib/cache_core.py`) derives tier + countdown from the
session `.jsonl` transcript. It's built for a 1-second cadence: it memoises
per-session state on disk and, when nothing has changed, recomputes the countdown
with **zero file parsing**; when the transcript grows it reads only the appended
bytes. A tier change is an appended line, so it's always caught by the
incremental read and never served stale.

## Layout

```
.claude-plugin/plugin.json
lib/cache_core.py                       # shared engine (tier, countdown, state)
statusline/statusline.py                # the status line row
statusline/cache_status.py              # /cache-status backing script
hooks/hooks.json, hooks/guard.py        # UserPromptSubmit guards
commands/cache-status.md
skills/install-statusline/              # SKILL.md + install_statusline.py
skills/keep-cache-alive/                # SKILL.md + keepalive.py
tests/                                  # correctness + efficiency tests
```

## Requirements

- Claude Code with plugin support; Python 3 (stdlib only — no dependencies).
- macOS / Linux (the status line and hooks are POSIX shell + Python 3).

## Tests

```
python3 tests/test_core.py
python3 tests/test_guard.py
python3 tests/test_installer.py
python3 tests/test_keepalive.py
```
