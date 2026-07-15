#!/usr/bin/env python3
"""
Cache Assistant — SessionStart notice hook.

Companion to the UserPromptSubmit guard (hooks/guard.py). That guard fires on
every *send* and can block the thread, showing its reason to the user; this one
fires when a session *starts* (startup / resume / clear / compact). A
SessionStart hook cannot block — there is no send to hold back — so to surface
the very same "your prompt cache is cold" warning *to the user* the moment they
return to a session whose window lapsed, it uses the one documented user-visible
channel for this event: it writes the message to **stderr and exits 2**.

Per the hooks contract, a SessionStart hook "cannot block" (so exit 2 does NOT
prevent the session from starting) but its stderr is "shown to the user"; since
Claude Code v2.1.199 that stderr is rendered in the transcript. Note this means
the warning goes to the user only, not into Claude's model context.

Detection is the same logic as the guard's expiry branch, run against the
current tier: if the sliding prompt-cache window has EXPIRED, print the warning
and exit 2. When the cache is still warm — or there is no cache-bearing turn yet
(a brand-new session) — it stays silent and exits 0.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import cache_core  # noqa: E402


def _silent():
    """No warning to show: let the session start untouched (exit 0, no output)."""
    sys.exit(0)


def _notify(message):
    """Show `message` to the user. SessionStart can't block, so exit 2 just
    surfaces stderr in the transcript (Claude Code >= v2.1.199)."""
    sys.stderr.write(message + "\n")
    sys.exit(2)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}

    session_id = data.get("session_id") or "unknown"
    transcript_path = data.get("transcript_path")

    try:
        state = cache_core.get_cache_state(transcript_path, session_id)
    except Exception:
        _silent()

    # Silent unless there is real cache data AND its window has already expired.
    if not (state.get("have_data") and state.get("expired")):
        _silent()

    tier = cache_core.TIER_LABEL.get(state.get("tier"), "?")
    rewrite = cache_core.fmt_tokens(state.get("rewrite_tokens"))
    _notify(
        "⚡ Cache Assistant: this session's {} prompt-cache window has EXPIRED "
        "while it was idle. Your first message will re-write ~{} tokens to the "
        "cache from cold (a slower, more expensive turn); every message after "
        "that reuses the warm cache as normal."
        .format(tier, rewrite))


if __name__ == "__main__":
    # Safety net: a SessionStart hook must never break session startup, so any
    # unexpected failure falls through to a silent no-op (exit 0, no output).
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
