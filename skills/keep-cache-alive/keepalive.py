#!/usr/bin/env python3
"""
Cache Assistant — keep-cache-alive planner.

The keep-cache-alive skill keeps a session's prompt cache warm by driving the
built-in ``/loop`` command:

    /loop <interval> <ping>

``/loop`` re-sends the ping on a fixed interval, and because each ping is a
normal in-session turn it replays the whole cached prefix (system prompt +
tools + conversation history) -> cache hit -> the sliding TTL slides forward for
free. Nothing runs outside the session: no background daemon, no
``claude --resume`` subprocess, no self-referential resume of the current
session.

This script does the one part that needs the transcript: detect the *current*
cache tier and translate it into the right ``/loop`` interval. ``/loop``
schedules on a cron backend, so each interval is chosen cron-safe (a whole
number of minutes dividing 60, short enough that cron's <=10% fire-late jitter
still lands before the TTL):

    5m tier  (300s TTL)  -> ping every 240s  == /loop 4m
    1h tier (3600s TTL)  -> ping every 1800s == /loop 30m

It prints a ready-to-run ``/loop`` command whose ping carries
``cache_core.KEEPALIVE_MARKER`` so the guard hook never blocks it. The skill
runs this script, then invokes that ``/loop`` command.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
import cache_core  # noqa: E402

PING_PROMPT = (cache_core.KEEPALIVE_MARKER +
               ' cache keepalive ping — reply with the single word "pong" and do nothing else.')


def log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def loop_interval(seconds):
    """Render an interval in whole seconds as a ``/loop`` duration (240 -> '4m').

    ``/loop`` schedules via cron (1-minute granularity) and rounds any sub-minute
    remainder *up* to a whole minute (``ceil(N/60)m``). We render minutes to match
    what will actually run, never a bare ``Ns`` string that ``/loop`` would
    silently change. Our tier values are exact minute multiples, so no rounding
    happens in practice.
    """
    if seconds % 3600 == 0:
        return "{}h".format(seconds // 3600)
    minutes = (seconds + 59) // 60  # round up, mirroring /loop's ceil(N/60)m
    return "{}m".format(minutes)


def loop_command(tier):
    """The exact ``/loop`` command that keeps a session on ``tier`` warm."""
    interval = cache_core.TIER_PING_INTERVAL[tier]
    return "/loop {} {}".format(loop_interval(interval), PING_PROMPT)


def resolve(session_id, transcript, cwd):
    """Fill in (session_id, transcript) from cwd if not given. Returns (sid, tx, err)."""
    if not session_id:
        detected_sid, detected_tx = cache_core.auto_detect_session(cwd)
        if not detected_sid:
            return None, None, ("could not auto-detect the current session for cwd {}. "
                                "Pass --session <id>.".format(cwd))
        session_id = detected_sid
        # Honour an explicitly supplied --transcript; only fall back to detection.
        if not transcript:
            transcript = detected_tx
    if not transcript:
        transcript = cache_core.find_transcript(session_id)
    if not transcript or not os.path.exists(transcript):
        return None, None, "could not locate transcript for session {}.".format(session_id)
    return session_id, transcript, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None,
                    help="session id (auto-detected from --cwd if omitted)")
    ap.add_argument("--transcript", default=None,
                    help="transcript path (auto-detected if omitted)")
    ap.add_argument("--cwd", default=None, help="directory to resolve the session from")
    ap.add_argument("--command-only", action="store_true",
                    help="print just the /loop command line and exit")
    args = ap.parse_args()

    session_id, transcript, err = resolve(args.session, args.transcript,
                                          args.cwd or os.getcwd())
    if err:
        log("ERROR: " + err)
        return 2

    state = cache_core.get_cache_state(transcript, session_id)
    tier = state.get("tier")
    if tier not in cache_core.TIER_PING_INTERVAL:
        log("ERROR: no cache tier detected yet in the transcript "
            "(send at least one message in this session first).")
        return 2

    cmd = loop_command(tier)
    if args.command_only:
        log(cmd)
        return 0

    interval = cache_core.TIER_PING_INTERVAL[tier]
    ttl = cache_core.TIER_TTL[tier]
    remaining = state.get("remaining_seconds")

    log("current tier: {} (TTL {}s)".format(cache_core.TIER_LABEL.get(tier, tier), ttl))
    if remaining is not None:
        log("cache window remaining now: {}".format(cache_core.fmt_mmss(remaining)))
    log("keepalive interval: every {}s — a {}s margin below the {}s TTL".format(
        interval, ttl - interval, ttl))
    log("")
    log("Run this to keep it warm on the /loop schedule:")
    log("  " + cmd)
    log("")
    log("The loop pings on that interval until you stop it. Rule of thumb: ~a "
        "dozen pings cost about one cold re-write, so if you'll be away much "
        "longer than that buys (~{} min), just let the cache expire instead."
        .format((interval * 12) // 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
