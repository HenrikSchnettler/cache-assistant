#!/usr/bin/env python3
"""
Cache Assistant — UserPromptSubmit guard hook.

Fires before every user prompt is sent. It implements two guards, both against
the *currently active* cache tier, with "send again to confirm" semantics:

  1. Cache-expiry guard: if the prompt-cache window has expired, block the
     first send and explain roughly how many tokens will be re-written to the
     cache from cold if it goes through anyway.

  2. Model / effort-change guard: if the live model or reasoning-effort level
     differs from the setting the last message was sent under — and the cache
     is still warm — block the first send, because that switch busts the whole
     cache. This lets the user revert to the previous model/effort without
     having destroyed the cache.

Re-sending the same prompt is the acknowledgement: it goes through unblocked,
and control returns to the normal cache-state logic (the ack is not sticky).

Blocking uses `{"decision":"block","reason":...}` on exit 0, per the
UserPromptSubmit hook contract.
"""

import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import cache_core  # noqa: E402

# Prompts containing this marker are keep-cache-alive pings and are never blocked.
KEEPALIVE_MARKER = cache_core.KEEPALIVE_MARKER
# A pending block older than this (seconds) is ignored (stale).
PENDING_TTL = 900
# Sensor (model/effort) older than this falls back to settings.json.
SENSOR_MAX_AGE = 30


def _guard_path(session_id):
    return os.path.join(cache_core.state_dir(), "guard-{}.json".format(
        "".join(c for c in (session_id or "x") if c.isalnum() or c in "-_") or "x"))


def _load_guard(session_id):
    try:
        with open(_guard_path(session_id)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_guard(session_id, data):
    cache_core._atomic_write(_guard_path(session_id), data)


def _prompt_hash(prompt):
    return hashlib.sha1((prompt or "").encode("utf-8", "replace")).hexdigest()


def _settings_json_selection():
    """Fallback current model/effort from user settings.json."""
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        with open(path) as fh:
            s = json.load(fh)
        return s.get("model"), s.get("effortLevel")
    except (OSError, ValueError):
        return None, None


def _current_selection(session_id):
    """Live (model, effort). Prefer the status-line sensor; fall back to settings."""
    sensor = cache_core.read_settings_state(session_id)
    if sensor and (time.time() - (sensor.get("updated_at") or 0)) <= SENSOR_MAX_AGE:
        m = sensor.get("model_id")
        e = sensor.get("effort_level")
        if m:
            return m, e, "sensor"
    m, e = _settings_json_selection()
    return m, e, "settings"


def _allow(extra_context=None):
    out = {}
    if extra_context:
        out = {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": extra_context}}
    if out:
        sys.stdout.write(json.dumps(out))
    sys.exit(0)


def _block(reason):
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}

    session_id = data.get("session_id") or "unknown"
    transcript_path = data.get("transcript_path")
    prompt = data.get("prompt") or ""

    # Never guard slash-commands, empty prompts, or keepalive pings.
    if not prompt.strip() or prompt.lstrip().startswith("/") or KEEPALIVE_MARKER in prompt:
        _allow()

    guard = _load_guard(session_id)
    model, effort, _src = _current_selection(session_id)
    cur_setting = {"model_id": model, "effort_level": effort}

    # --- Acknowledgement: same prompt re-sent after a block -> let it through.
    pending = guard.get("pending")
    now = time.time()
    if pending and (now - pending.get("at", 0)) <= PENDING_TTL \
            and pending.get("prompt_hash") == _prompt_hash(prompt):
        # This send is the confirmation. Clear the block, commit current setting,
        # and return to normal logic on subsequent sends.
        guard["pending"] = None
        guard["committed"] = cur_setting
        _save_guard(session_id, guard)
        _allow()

    # Any other prompt supersedes a stale/mismatched pending block.
    if pending:
        guard["pending"] = None

    try:
        state = cache_core.get_cache_state(transcript_path, session_id)
    except Exception:
        state = {"have_data": False}

    committed = guard.get("committed")
    cache_warm = state.get("have_data") and state.get("expired") is False
    rewrite = cache_core.fmt_tokens(state.get("rewrite_tokens"))

    # --- Guard 1: model / effort change while the cache is still warm ---------
    if committed and model and cache_warm:
        changed_model = committed.get("model_id") != model
        changed_effort = (committed.get("effort_level") or None) != (effort or None)
        if changed_model or changed_effort:
            what = []
            if changed_model:
                what.append("model {} → {}".format(
                    committed.get("model_id"), model))
            if changed_effort:
                what.append("effort {} → {}".format(
                    committed.get("effort_level") or "?", effort or "?"))
            guard["pending"] = {"reason": "model_effort",
                                "prompt_hash": _prompt_hash(prompt), "at": now}
            _save_guard(session_id, guard)
            _block(
                "⚡ Cache Assistant: you changed {}. That busts the whole "
                "prompt cache and forces ~{} tokens to be re-cached on this first "
                "message under the new setting.\n\n"
                "→ If this was a mistake, switch back to the previous "
                "setting now and your warm cache is untouched.\n"
                "→ To proceed anyway, send the same message again."
                .format(" and ".join(what), rewrite))

    # --- Guard 2: expired cache window ---------------------------------------
    if state.get("have_data") and state.get("expired"):
        tier = cache_core.TIER_LABEL.get(state.get("tier"), "?")
        guard["pending"] = {"reason": "expiry",
                            "prompt_hash": _prompt_hash(prompt), "at": now}
        _save_guard(session_id, guard)
        _block(
            "⚡ Cache Assistant: your {} prompt-cache window has EXPIRED. "
            "Sending now re-writes ~{} tokens to the cache from cold (a slower, "
            "more expensive turn).\n\n"
            "→ To proceed, send the same message again."
            .format(tier, rewrite))

    # --- Normal path: allow and commit the current setting as the baseline ---
    guard["committed"] = cur_setting
    if guard.get("pending") is None and "pending" in guard:
        del guard["pending"]
    _save_guard(session_id, guard)
    _allow()


if __name__ == "__main__":
    # Safety net: this hook runs on every send, so a bug must never block the
    # user. Any unexpected failure falls through to "allow" (exit 0, no output).
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
