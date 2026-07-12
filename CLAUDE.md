# CLAUDE.md — Cache Assistant

Project context for working in this repo. The repo root is itself a **local
Claude Code marketplace** (`.claude-plugin/marketplace.json`) hosting one plugin,
`plugins/cache-assistant/`.

The plugin makes a session's prompt-cache window visible (status line) and
enforceable (guards + keep-alive). Everything derives from the session `.jsonl`
transcript; there are **no runtime dependencies** — stdlib Python 3 only.

## Layout

```
plugins/cache-assistant/
  lib/cache_core.py            # THE engine — shared by everything below
  statusline/statusline.py     # status line row (tier + countdown); writes the model/effort sensor
  statusline/cache_status.py   # /cache-status backing script
  hooks/guard.py + hooks.json  # UserPromptSubmit guard (expiry + model/effort)
  skills/install-statusline/   # install_statusline.py — non-destructive status line merge
  skills/keep-cache-alive/     # keepalive.py — tier-aware warm-keeping loop
  commands/cache-status.md
tests/                         # test_core / test_guard / test_installer / test_keepalive
```

Run the tests with `python3 tests/test_*.py` (66 checks, no network, no deps).

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
  installed to be fully effective.
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
- **Keep-alive.** Interval is re-derived from the *current* tier every cycle
  (5m→240s, 1h→3300s, each a margin below the TTL), so it adapts to a mid-loop
  tier switch. Pings via `claude --resume <session> -p "<marker> …"` replay the
  prefix → cache hit → TTL slides forward. Default **max 12 pings** (community
  break-even: ~a dozen keep-alives cost about one cold re-write; also runaway
  protection). Ctrl-C stops cleanly.
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
