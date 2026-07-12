#!/usr/bin/env python3
"""
Cache Assistant status line.

Reads Claude Code's status-line JSON on stdin and prints ONE row showing the
current prompt-cache tier (5m / 1h) and a live mm:ss countdown until the cache
window expires. Designed to be driven at a 1-second cadence via
`statusLine.refreshInterval = 1`.

The tier and countdown are derived from the *current* newest turn of the
transcript on every call (see lib/cache_core.py), so a mid-session tier switch
is picked up on the very next render with the countdown re-based to the new
tier — no restart, no stale state.

It also records the live model + effort into the shared session state file so
the guard hook can detect model/effort changes.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import cache_core  # noqa: E402


def _color(code, text, enabled):
    if not enabled:
        return text
    return "\033[{}m{}\033[0m".format(code, text)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}

    session_id = data.get("session_id") or "unknown"
    transcript_path = data.get("transcript_path")

    model = data.get("model") or {}
    effort = data.get("effort") or {}
    # Record the live selection for the guard hook (cheap, atomic).
    try:
        cache_core.write_settings_state(
            session_id, model.get("id"), effort.get("level"))
    except Exception:
        pass

    colors = os.environ.get("NO_COLOR") is None

    try:
        state = cache_core.get_cache_state(transcript_path, session_id)
    except Exception:
        # Never let the status line crash the row; degrade gracefully.
        print("⚡ cache …")
        return

    if not state["have_data"]:
        print(_color("2", "⚡ cache · warming…", colors))
        return

    tier = state["tier"]
    tier_txt = cache_core.TIER_LABEL.get(tier, "?")

    if tier not in cache_core.TIER_TTL:
        print(_color("2", "⚡ cache {} · ?".format(tier_txt), colors))
        return

    remaining = state["remaining_seconds"]
    if state["expired"]:
        toks = cache_core.fmt_tokens(state["rewrite_tokens"])
        body = "⚡ cache {} · EXPIRED · ~{} to re-cache".format(tier_txt, toks)
        print(_color("31", body, colors))  # red
        return

    mmss = cache_core.fmt_mmss(remaining)
    body = "⚡ cache {} · {} left".format(tier_txt, mmss)
    if remaining <= 30:
        print(_color("33", body, colors))   # yellow: expiring soon
    else:
        print(_color("32", body, colors))   # green: healthy


if __name__ == "__main__":
    main()
