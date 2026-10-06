#!/usr/bin/env python3
"""
Cache Assistant — PreModelSwitch / PostModelSwitch hook.

Claude Code tells these two events what a model switch costs: whether the
current model's prompt cache is still warm (`prompt_cache_warm`), how many
tokens the next request re-sends (`context_tokens`) and the tier (`cache_ttl`).
That is the live signal the UserPromptSubmit guard never had.

  PreModelSwitch   A switch the user makes by hand (`/model`, the picker) while
                   the cache is warm and the re-write is large is turned into a
                   confirmation ("ask"): declining leaves the cache untouched.
                   Small re-writes and cold caches pass silently.

  PostModelSwitch  Records the switch for the send guard (hooks/guard.py): a
                   switch that was confirmed here is not asked about again, one
                   that could not be asked about (SDK / desktop picker,
                   automatic fallback) blocks the first send once instead.
                   Switching back to the original model clears the record.

Like the other hooks it allows on any internal error.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import cache_core  # noqa: E402
import guard as guard_state  # noqa: E402

# A confirmation older than this (seconds) no longer vouches for a switch.
ASK_TTL = 900


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}

    event = data.get("hook_event_name")
    session_id = data.get("session_id") or "unknown"
    src, dst = data.get("from_model"), data.get("to_model")
    tokens = data.get("context_tokens")
    warm = bool(data.get("prompt_cache_warm"))
    now = time.time()
    state = guard_state._load_guard(session_id)

    if event == "PreModelSwitch":
        if not (warm and cache_core.is_large(tokens)
                and data.get("source") in ("command", "picker")):
            sys.exit(0)
        state["switch_asked"] = {"to": dst, "at": now}
        guard_state._save_guard(session_id, state)
        cost = data.get("estimated_cache_write_usd")
        cost_txt = " (about ${:.2f})".format(cost) if isinstance(cost, (int, float)) else ""
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreModelSwitch",
            "permissionDecision": "ask",
            "permissionDecisionReason": (
                "⚡ Cache Assistant: the {} prompt cache for {} is still warm. "
                "Switching to {} busts it: the next message re-caches ~{} "
                "tokens{} from cold."
                .format(data.get("cache_ttl") or "?", src, dst,
                        cache_core.fmt_tokens(tokens), cost_txt))}}))
        sys.exit(0)

    if event == "PostModelSwitch":
        asked = state.pop("switch_asked", None)
        confirmed = bool(asked and asked.get("to") == dst
                         and now - asked.get("at", 0) <= ASK_TTL)
        prev = state.get("switch")
        origin = prev.get("from") if prev else src
        if data.get("source") == "resume" or origin == dst:
            # Restored on resume, or back on the model the cache was built on.
            state.pop("switch", None)
        else:
            state["switch"] = {
                "from": origin, "to": dst, "at": now,
                "warm": bool(prev.get("warm")) if prev else warm,
                "tokens": tokens,
                "confirmed": confirmed and (prev.get("confirmed", True) if prev else True),
            }
        guard_state._save_guard(session_id, state)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
