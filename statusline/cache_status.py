#!/usr/bin/env python3
"""On-demand, human-readable cache status for the current (or a given) session.
Backs the /cache-status command."""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import cache_core  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None)
    ap.add_argument("--transcript", default=None)
    ap.add_argument("--cwd", default=None)
    args = ap.parse_args()

    cwd = args.cwd or os.getcwd()
    session_id, transcript = args.session, args.transcript
    if not session_id:
        session_id, transcript = cache_core.auto_detect_session(cwd)
    if session_id and not transcript:
        transcript = cache_core.find_transcript(session_id)
    if not transcript:
        print("Cache Assistant: could not locate a session transcript for {}.".format(cwd))
        return 2

    st = cache_core.get_cache_state(transcript, session_id)
    print("⚡ Cache Assistant — status")
    print("  session:    {}".format(session_id))
    print("  transcript: {}".format(transcript))
    if not st["have_data"]:
        print("  state:      warming up (no cache-bearing turn yet)")
        return 0
    tier = st["tier"]
    print("  tier:       {}  (TTL {}s)".format(
        cache_core.TIER_LABEL.get(tier, "?"), st.get("ttl_seconds")))
    anchor = datetime.datetime.fromtimestamp(st["anchor_epoch"]).strftime("%H:%M:%S")
    print("  last touch: {}  (cache last refreshed)".format(anchor))
    if st["expired"]:
        print("  window:     EXPIRED — next send re-caches ~{} tokens from cold".format(
            cache_core.fmt_tokens(st["rewrite_tokens"])))
    else:
        print("  window:     {} remaining".format(cache_core.fmt_mmss(st["remaining_seconds"])))
        print("  if it lapses: ~{} tokens re-cached from cold".format(
            cache_core.fmt_tokens(st["rewrite_tokens"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
