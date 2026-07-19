---
name: keep-cache-alive
description: Keep the current session's Claude Code prompt cache warm while you are away by driving the built-in /loop command to send a tiny, tier-aware "ping" message on a fixed interval, so you don't pay a cold cache re-write when you come back.
---

# Keep the prompt cache alive

Use this skill when the user wants to keep their prompt cache warm during an idle
stretch (stepping away, a long meeting) so the next real message is a cache hit
instead of an expensive cold re-write.

## How it works

The prompt cache stays warm as long as the session keeps *using* its cached
prefix (system prompt + tools + conversation history) before the TTL runs out —
every hit slides the window forward for free. So "keeping it alive" just means
re-touching the session on a schedule that stays a margin under the TTL.

This skill does that with the built-in **`/loop`** command. `/loop` runs a fixed
interval **inside this session**, and each tick re-sends one tiny **ping**
message as a normal in-session turn. That turn replays the cached prefix → cache
hit → the TTL slides forward. There is no background daemon, no `claude
--resume`, and nothing runs outside the session that launched it.

The ping carries the marker `[cache-assistant:keepalive]`, which the guard hook
recognises so it never blocks a ping.

## Tier-aware interval

The interval is derived from the current cache tier, each a safety margin below
the TTL. `/loop` schedules on a **cron** backend, so the intervals are chosen to
be cron-safe (whole minutes that divide 60 evenly, short enough that cron's
up-to-10% "fire late" jitter still lands before the TTL):

| tier | TTL   | ping interval | `/loop` interval |
|------|-------|---------------|------------------|
| 5m   | 300s  | 240s          | `4m`             |
| 1h   | 3600s | 1800s         | `30m`            |

(The 1h tier uses 30m rather than the intuitive ~55m because cron cannot express
55m safely — it would round to 60m, exactly the TTL, or fire unevenly.)

## Steps

1. **Detect the tier and get the exact `/loop` command.** This also confirms the
   cache is readable and reports how much window is left right now:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/keep-cache-alive/keepalive.py"
   ```

   (Use `--command-only` to print just the `/loop …` line, ready to run.)

2. **Start the loop** by running the command it printed. For example on the 1h
   tier:

   ```
   /loop 30m [cache-assistant:keepalive] cache keepalive ping — reply with the single word "pong" and do nothing else.
   ```

   Each tick the model just replies `pong`; you can watch the status-line
   countdown jump back to full after every ping.

## Stopping

The loop runs until you stop it — tell Claude to stop looping (or interrupt the
session). Rule of thumb: about a dozen pings cost roughly one cold re-write, so
if you'll be away much longer than the coverage that buys (~48 min on the 5m
tier, ~6 h on the 1h tier) it's cheaper to just let the cache expire and pay one
re-write when you return.

## Caveats

- **It only runs while this session and machine are alive.** The loop is
  session-only (`/loop`'s cron job dies when the session ends) and fires only
  while the process is running and the machine awake — closing the terminal or
  letting the laptop sleep stops it. (Firing only while the REPL is idle is fine:
  if you come back and start typing, your own turns keep the cache warm.)
- **A mid-session tier drop makes the fixed interval wrong.** The interval is
  chosen once, at start. If your plan tips into usage overage the tier drops
  1h → 5m (the window shrinks 3600s → 300s), and a 30-minute ping would then land
  long after each 5-minute window has already closed — paying a cold re-write
  every time, worse than not looping at all. If the status line shows the tier
  has dropped to **5m**, stop the loop and re-run this skill to re-pick the
  interval (or just let the cache expire — 5m-tier looping is near break-even).
- **Pings append short turns to the transcript.** This is intended for a session
  you have **stepped away from** (idle). Avoid looping a session you are actively
  typing into at the same moment.
