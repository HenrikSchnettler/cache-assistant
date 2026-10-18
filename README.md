# Cache Assistant

A Claude Code plugin to **understand and control your prompt-cache window**.

Claude Code re-caches your whole conversation prefix on every turn, and that
cache expires after a period of inactivity — 5 minutes or 1 hour depending on how
you're billed. Let it lapse and your next message pays a slow, expensive cold
re-write. Cache Assistant makes that window visible and gives you guardrails.

## What you get

- **Band above the prompt** (Claude Code mod, v2.1.288+) — the current cache
  **tier** (`5m` / `1h`), a live **countdown** to expiry, the cold re-write size
  and the model, plus **advice**: when
  the window has expired it says whether to just keep going (small re-write) or
  to compact or clear first (large one), with **Compact** and **Clear** buttons
  right there; it warns before a large cache goes
  cold, tells you how long switching back stays free after a **model switch**,
  and reports an **unexpected cache miss** (a request that did not read the warm
  prefix) with the setting that changed. A **keep-alive button** holds the cache
  warm with pings that add nothing to the conversation and stops by itself after
  a ping limit. `/cache-keepalive` toggles it, `/cache-band` shows or hides the
  band.
- **One threshold for "small vs large"** — the plugin option
  `block_threshold_tokens` (default **50,000**). Below it nothing blocks and the
  band says to keep going; at or above it the guards interrupt. `0` always
  blocks. `keepalive_max_pings` (default 12) caps the keep-alive.
- **Model-switch confirmation** — `/model` or the picker on a warm, large cache
  asks first (Claude Code's `PreModelSwitch` hook), naming the re-cache size and
  cost. A switch that cannot be asked about (desktop/SDK, automatic fallback)
  blocks the first send once instead; switching back clears it.
- **Cache-expiry guard** — before a send, if the window has already expired, the
  first attempt is **blocked** with an explanation and a token estimate for the
  cold re-write. Press **Allow send anyway** in the band to let the next message
  through (without the band: send the same message again). Only for re-writes at
  or above the threshold.
- **Model / effort-change guard** — switching model or reasoning effort busts the
  whole cache. The first message under the new setting is **blocked** so you can
  revert without losing your warm cache. **Allow send anyway** in the band proceeds.
  A model switch is reported by the model-switch hooks; an effort change is seen
  only once it is saved to `~/.claude/settings.json` (a session-only `/effort`
  change is not caught here, but the band reports the cache miss it causes).
- **Session-start notice** — when you **resume** a session whose window lapsed
  while you were away, a heads-up is **shown to you on entry** (with the same cold
  re-write estimate), so you know the first turn is cold before you type. A
  session-start hook can't block a send, so it just warns (and never stops the
  session from starting); it stays quiet when the cache is still warm. Requires
  Claude Code v2.1.199+ for the message to render.
- **`keep-cache-alive` skill** — drives the built-in `/loop` command to send a
  tiny, tier-aware ping into the session on a fixed interval, keeping the window
  warm while you're away without any out-of-session daemon.
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

## How it works

A single Python engine (`lib/cache_core.py`) derives tier + countdown from the
session `.jsonl` transcript. It runs on every send and turn, so it memoises
per-session state on disk and, when nothing has changed, recomputes the countdown
with **zero file parsing**; when the transcript grows it reads only the appended
bytes. A tier change is an appended line, so it's always caught by the
incremental read and never served stale.

## Layout

```
.claude-plugin/plugin.json
lib/cache_core.py                       # shared engine (tier, countdown, state)
scripts/cache_status.py                 # /cache-status and the band's backing script
hooks/hooks.json                        # registers the hooks and the mod's module
hooks/register.tsx                      # the band above the prompt (hooks module)
hooks/advice.ts                         # what the band says, as pure functions
hooks/model_switch.py                   # PreModelSwitch / PostModelSwitch hook
types/index.d.ts                        # the mod's state contract
hooks/guard.py                          # UserPromptSubmit guards (block on send)
hooks/session_notice.py                 # SessionStart notice (warn on resume)
commands/cache-status.md
skills/keep-cache-alive/                # SKILL.md + keepalive.py
tests/                                  # correctness + efficiency tests
```

## Surface support

Everything runs in the **terminal CLI**. Hooks also run in **desktop local
sessions**, so the guards and the session-start notice work there too, and the
band renders in the desktop app's Code tab. Cloud / remote / WSL sessions don't
load plugins at all.

## Requirements

- Claude Code with plugin support; Python 3 (stdlib only — no dependencies).
- macOS / Linux (the hooks are Python 3).

## Tests

```
python3 tests/test_core.py
python3 tests/test_guard.py
python3 tests/test_keepalive.py
claude plugin test .          # the band (tests/band.test.ts)
python3 tests/test_session_notice.py
```
