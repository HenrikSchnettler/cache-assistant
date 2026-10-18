#!/usr/bin/env python3
"""
Cache Assistant — UserPromptSubmit guard hook.

Fires before every user prompt is sent. It implements two guards, both against
the *currently active* cache tier, with "send again to confirm" semantics:

  1. Cache-expiry guard: if the prompt-cache window has expired, block the
     first send and explain roughly how many tokens will be re-written to the
     cache from cold if it goes through anyway.

  2. Model-switch guard: if this session switched model since its last message
     without confirming it at the switch prompt (recorded by
     hooks/model_switch.py) — and the cache is still warm — block the first
     send, because that switch busts the whole cache. This lets the user switch
     back without having destroyed the cache.

There is no other source for the model or effort: `~/.claude/settings.json` is
shared by every session on the machine (a `/model` in one session persists
there), so it cannot say what *this* session runs, and reading it made a switch
in one session block a send in another. An effort change is reported after the
fact, as a cache miss in the band.

The acknowledgement is the band's "Allow send anyway" button (hooks/register.tsx),
which records `ack` in the guard state: the next message goes through
unblocked and normal logic resumes (the ack is not sticky). Where the band is
not drawn (hidden, or a Claude Code without mods), re-sending the same prompt
is the acknowledgement instead.

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


def _load_guard(session_id):
    return cache_core.load_guard(session_id)


def _save_guard(session_id, data):
    cache_core.save_guard(session_id, data)


def _prompt_hash(prompt):
    return hashlib.sha1((prompt or "").encode("utf-8", "replace")).hexdigest()


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


def _proceed(has_band):
    if has_band:
        return "To proceed anyway, press Allow send anyway in the band above the prompt, then send."
    return "To proceed anyway, send the same message again."


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
    # The settings.json baseline of 0.3.x/0.4.0: nothing compares it any more.
    guard.pop("committed", None)

    # --- Acknowledgement: the band's Allow send anyway, or (without the band) the
    # same prompt re-sent after a block -> let it through.
    pending = guard.get("pending")
    now = time.time()
    has_band = bool(guard.get("band"))
    is_acked = guard.pop("ack", None) is not None
    is_resent = not has_band and pending and (now - pending.get("at", 0)) <= PENDING_TTL \
        and pending.get("prompt_hash") == _prompt_hash(prompt)
    if is_acked or is_resent:
        # This send is the confirmation. Clear the block (and the switch it was
        # about) and return to normal logic on subsequent sends.
        guard["pending"] = None
        guard.pop("switch", None)
        _save_guard(session_id, guard)
        _allow()

    # Any other prompt supersedes a stale/mismatched pending block.
    if pending:
        guard["pending"] = None

    try:
        state = cache_core.get_cache_state(transcript_path, session_id)
    except Exception:
        state = {"have_data": False}

    cache_warm = state.get("have_data") and state.get("expired") is False
    rewrite = cache_core.fmt_tokens(state.get("rewrite_tokens"))
    # Below the threshold a cold re-write is cheap: never block, just send.
    large = cache_core.is_large(state.get("rewrite_tokens"))

    # --- Model-switch guard: a switch reported by the PreModelSwitch/PostModelSwitch
    # hooks (hooks/model_switch.py). This is the live source where it exists; a
    # switch the user already confirmed at the switch prompt is not asked again.
    switch = guard.pop("switch", None)
    if switch and cache_warm and large and not switch.get("confirmed") \
            and switch.get("warm"):
        guard["pending"] = {"reason": "model_switch",
                            "prompt_hash": _prompt_hash(prompt), "at": now}
        # Kept until a send goes through: switching back clears it untouched.
        guard["switch"] = switch
        _save_guard(session_id, guard)
        _block(
            "⚡ Cache Assistant: the model changed {} → {}. That busts the "
            "whole prompt cache and forces ~{} tokens to be re-cached on this "
            "first message under the new model.\n\n"
            "→ If this was a mistake, switch back now and your warm cache is "
            "untouched.\n"
            "→ {}"
            .format(switch.get("from"), switch.get("to"), rewrite, _proceed(has_band)))

    # --- Expiry guard: expired cache window -----------------------------------
    if state.get("have_data") and state.get("expired") and large:
        tier = cache_core.TIER_LABEL.get(state.get("tier"), "?")
        guard["pending"] = {"reason": "expiry",
                            "prompt_hash": _prompt_hash(prompt), "at": now}
        _save_guard(session_id, guard)
        _block(
            "⚡ Cache Assistant: your {} prompt-cache window has EXPIRED. "
            "Sending now re-writes ~{} tokens to the cache from cold (a slower, "
            "more expensive turn).\n\n"
            "→ {}"
            .format(tier, rewrite, _proceed(has_band)))

    # --- Normal path: allow ---------------------------------------------------
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
