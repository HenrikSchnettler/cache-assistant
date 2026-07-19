# CLAUDE.md — Cache Assistant

Project context for working in this repo. The repo root **is** the plugin
(`.claude-plugin/plugin.json`) — a single-plugin Claude Code repo installed as
`cache-assistant`; hooks resolve scripts via `${CLAUDE_PLUGIN_ROOT}` (the repo
root).

The plugin makes a session's prompt-cache window visible (status line) and
enforceable (guards + keep-alive). Everything derives from the session `.jsonl`
transcript; there are **no runtime dependencies** — stdlib Python 3 only.

## Layout

```
.claude-plugin/plugin.json     # plugin manifest
lib/cache_core.py              # THE engine — shared by everything below
statusline/statusline.py       # status line row (tier + countdown); writes the model/effort sensor
statusline/cache_status.py     # /cache-status backing script
hooks/guard.py                 # UserPromptSubmit guard (expiry + model/effort); can block a send
hooks/session_notice.py        # SessionStart notice: same expiry logic, warns the USER on resume (stderr+exit 2, never blocks)
hooks/hooks.json               # registers both hooks (UserPromptSubmit + SessionStart)
skills/install-statusline/     # install_statusline.py — non-destructive status line merge
skills/keep-cache-alive/       # keepalive.py — tier→`/loop` planner (skill drives the built-in /loop)
commands/cache-status.md
tests/                         # test_core / test_guard / test_installer / test_keepalive / test_session_notice
```

Run the tests with `python3 tests/test_*.py` (93 checks, no network, no deps).

## The cache model this is built on (ground truth)

Established by inspecting real `~/.claude/projects/**/*.jsonl` transcripts and
Anthropic's docs. The code keys off exactly these facts:

- **Tier is per-turn**, readable only from
  `message.usage.cache_creation.ephemeral_1h_input_tokens` vs
  `ephemeral_5m_input_tokens` (whichever is > 0). There is **no `ttl` or
  expiry field** anywhere in the transcript, so the countdown is *derived*.
- **The TTL is a sliding window**: every request that hits the cache resets the
  timer ("refreshed for no additional cost each time the cached content is
  used"). So the window expires at `anchor + TTL`, where the **anchor** is the
  timestamp of the newest assistant turn with cache activity (read or write), and
  `TTL ∈ {300s (5m), 3600s (1h)}`.
- **Tier can change mid-session.** On a Claude subscription Claude Code requests
  **1h** automatically; if you tip into usage overage it drops to **5m** (also
  `FORCE_PROMPT_CACHING_5M=1` / `ENABLE_PROMPT_CACHING_1H=1`). A change shows up
  as the next appended turn carrying the *other* `ephemeral_*` bucket.
- **Subagents always use 5m**, even on a subscription, and live in a **separate**
  `.../<session>/subagents/agent-*.jsonl`. The main transcript keeps its own tier.
- **Model or effort changes bust the whole cache** (each is part of the cache
  key) → the next turn is a full re-read. Critically, the `UserPromptSubmit` hook
  stdin does **not** include the pending model/effort, and the transcript's last
  turn still shows the *previous* model. The only live source is the **status
  line** stdin (`model.id`, `effort.level`) — which is why `statusline.py` writes
  those to a per-session sensor file that `guard.py` reads (fallback:
  `settings.json`). The model/effort guard therefore needs the status line
  installed to be fully effective. Docs confirm this is irreducible: there is
  **no `$CLAUDE_MODEL` env var**, only `SessionStart` stdin carries `model` (not
  guaranteed), and `effort`/`$CLAUDE_EFFORT` appear only on tool-context events
  (`PreToolUse`/`PostToolUse`/`Stop`/`SubagentStop`) — never on `UserPromptSubmit`.
- **Surface support.** Hooks run in the terminal CLI and in **desktop local
  sessions**, so the expiry guard and the SessionStart notice (both
  transcript-only) work on both. But the **desktop app does not execute custom
  status lines** (the `statusLine` setting is silently ignored — Claude Code issue
  [#41456](https://github.com/anthropics/claude-code/issues/41456)), so on desktop
  the sensor is never written and the model/effort guard **degrades to its
  `settings.json` fallback**: it still catches a *persisted* change (`/model`
  saved as default) but not a session-only picker switch. Cloud/remote/WSL
  sessions don't load plugins at all.
- Multiple assistant `.jsonl` lines can share one `requestId` with **identical**
  usage (content blocks of one API response) — take the latest, never sum.

Cold-rewrite token estimate (shown by the status line and guards) =
`cache_read + cache_creation + input_tokens` of the newest request.

## Key design decisions / conventions

- **Efficiency (`cache_core.get_cache_state`).** Per-session state is memoised on
  disk. Each call stats the transcript; if size+inode are unchanged it reuses the
  memoised anchor/tier with **zero parsing** (the "fast" path) and just recomputes
  the countdown from `now`. If the file grew it seeks to the last consumed byte
  offset and parses **only the appended lines** ("incremental"). A tier change is
  always an appended line, so it's caught by the incremental path and can never be
  stale — never a full re-parse in steady state. Measured ~36 ms/tick (dominated
  by Python interpreter startup), size-independent, ~11 MB RSS.
- **State dir:** `CACHE_ASSISTANT_STATE_DIR` env override, else
  `$TMPDIR/cache-assistant`. Holds `state-<session>.json` (cache memo + sensor)
  and `guard-<session>.json` (guard baseline + pending-ack). Writes are atomic
  (`os.replace`) and self-correcting, so a lost write just means a re-parse.
- **Guard semantics.** "Send again to confirm": a block records
  `{reason, prompt_hash}`; the identical re-send is the acknowledgement (allowed,
  then normal logic resumes — not sticky). The model/effort guard blocks only
  while the current selection differs from the setting the last message was sent
  under, so reverting before sending clears it with the cache intact. The guard
  **never** blocks slash-commands, empty prompts, or keep-alive pings (marked with
  `cache_core.KEEPALIVE_MARKER`), and has a top-level safety net that **allows on
  any internal error** — it must never wrongly block a send.
- **SessionStart companion (`hooks/session_notice.py`).** Same expiry detection
  as the guard, but for the moment you *enter* a session (startup/resume/clear/
  compact) rather than send. A SessionStart hook has no send to block; to warn the
  **user** (not just Claude) it uses the one documented user-visible channel for
  this event — **stderr + exit 2**. Per the hooks contract SessionStart "cannot
  block", so exit 2 does *not* stop the session from starting, but its stderr is
  shown to the user (rendered in the transcript since Claude Code v2.1.199). So
  when the window has already **expired** it prints the cold-re-write warning and
  exits 2; otherwise it exits 0 silently. This means the warning goes to the user,
  *not* into Claude's context (the `additionalContext`/exit-0 path would be the
  reverse). It's read-only w.r.t. the guard's `guard-<session>.json` state machine
  and shares the guard's allow-on-error safety net (a SessionStart hook must never
  break startup).
- **Keep-alive.** The skill drives the **built-in `/loop`** command — it stays
  *in-session*, so a ping is just a normal user turn that replays the cached
  prefix → cache hit → TTL slides forward. No daemon, no `claude --resume`, no
  self-referential resume of the current session (the earlier out-of-session
  daemon design was the layer mismatch this replaced). `keepalive.py` is now a
  small **planner**: it detects the current tier and prints the matching `/loop`
  command. `/loop` schedules on a **cron** backend, so the intervals are chosen
  cron-safe — a whole number of minutes dividing 60, short enough that cron's
  ≤10% fire-late jitter still lands before the TTL: 5m→`/loop 4m` = 240s,
  1h→`/loop 30m` = 1800s. (55m/3300s, the old daemon value, is *not* cron-safe —
  it rounds to 60m = the TTL, or fires unevenly — so the 1h tier uses 30m;
  `tests/test_keepalive.py` enforces this invariant over every tier.) The ping
  carries `cache_core.KEEPALIVE_MARKER` so `guard.py` never blocks it. `/loop`
  runs until the user stops it — and only while this session and machine are
  alive, and it can't adapt if the tier drops mid-session (both caveated in the
  SKILL). The planner surfaces the community break-even (~a dozen pings ≈ one
  cold re-write) as guidance rather than a hard cap.
- **Paths.** Hooks reference scripts via `${CLAUDE_PLUGIN_ROOT}`. The installer
  bakes an absolute path to `statusline.py` into settings and sets
  `refreshInterval: 1` (needed for the 1-second countdown); when a status line
  already exists it generates a wrapper that runs the original first, then our
  row, and saves the original for `--restore`. Re-running is idempotent.

## Sources

- https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- https://code.claude.com/docs/en/prompt-caching
- https://code.claude.com/docs/en/statusline · https://code.claude.com/docs/en/hooks
- Keep-alive prior art: github.com/yujiachen-y/claude-code-cache-keepalive
