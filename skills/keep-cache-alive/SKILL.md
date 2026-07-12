---
name: keep-cache-alive
description: Keep the current session's Claude Code prompt cache warm while you are away by sending minimal tier-aware ping messages on a schedule, so you don't pay a cold cache re-write when you come back.
---

# Keep the prompt cache alive

Use this skill when the user wants to keep their prompt cache warm during an idle
stretch (stepping away, a long meeting) so the next real message is a cache hit
instead of an expensive cold re-write.

It runs `keepalive.py`, which:

- **Detects the current cache tier** from the session transcript and derives the
  ping interval from it — `240s` on the 5-minute tier, `3300s` on the 1-hour
  tier (each a safety margin under the TTL).
- **Re-checks the tier every cycle**, so if it changes mid-loop (for example
  `1h → 5m` when the plan tips into usage overage) the cadence adapts
  immediately.
- **Pings** by running `claude --resume <session> -p "<marker> …"` in the
  session's directory. Replaying the same prefix hits the existing cache and
  slides its TTL forward. The guard hook recognises the marker and never blocks
  a ping. You can watch the status line countdown jump back to full after each
  ping.
- **Stops after a maximum of 12 pings by default** (community-grounded: about a
  dozen keep-alives cost roughly one cold re-write, and the cap prevents a
  runaway loop if you never return — see `RESEARCH.md`). Coverage at 12 pings is
  ~48 min on the 5m tier and ~11 h on the 1h tier. Ctrl-C stops cleanly anytime.

## Steps

1. **Show the plan** (tier + cadence + coverage), which also confirms detection:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/keep-cache-alive/keepalive.py" --print-plan
   ```

2. **Run the loop.** With no `--session` it auto-detects the current session from
   the working directory:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/keep-cache-alive/keepalive.py"
   ```

   Run it in the background (or a separate terminal) so it can ping while you are
   away, and stop it with Ctrl-C when you return.

## Options

- `--max-pings N` — change the cap (default 12).
- `--session <id>` / `--cwd <dir>` — target a specific session/directory.
- `--print-plan` — print the schedule and exit.

## Caveat

Pings append turns to the target session's transcript. This is intended for a
session you have **stepped away from** (idle). It keeps the expensive shared
prefix — system prompt, project context, long history — warm, which is the part
that costs the most to rebuild. Avoid running it against a session you are
actively typing into at the same moment.
