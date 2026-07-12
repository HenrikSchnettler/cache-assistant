#!/usr/bin/env python3
"""
Cache Assistant — keep-cache-alive loop.

Periodically sends a minimal "ping" into a target session so the prompt-cache
window keeps sliding forward instead of expiring while you are away.

Mechanism: each ping is `claude --resume <session> -p "<marker> ..."` run in the
session's directory. Because it replays the same prefix (same model + directory
+ conversation), the request hits the existing cache and refreshes its TTL for
no extra cost. The guard hook recognises the marker and never blocks a ping.

Tier awareness (see RESEARCH.md):
  * The ping interval is derived from the *current* tier every cycle
    (5m -> 240s, 1h -> 3300s), so if the tier changes mid-loop
    (e.g. 1h -> 5m on usage overage) the cadence adapts on the next cycle.

Maximum pings (community-grounded, see RESEARCH.md):
  * Default 12. Roughly a dozen keep-alives cost about the same as letting the
    cache lapse and paying one cold re-write, so past that it is cheaper to just
    let it expire. The cap also prevents a runaway loop if you never come back.
    Coverage at 12 pings: ~48 min on the 5m tier, ~11 h on the 1h tier.

Testing affordances: --interval-override shortens the sleep (the tier-derived
interval is still computed and reported), and --ping-command swaps the real
`claude` call for a stub so the loop logic can be exercised offline.
"""
import argparse
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
import cache_core  # noqa: E402

DEFAULT_MAX_PINGS = 12
PING_PROMPT = (cache_core.KEEPALIVE_MARKER +
               " keepalive — reply with the single word pong and nothing else.")


def log(msg):
    sys.stdout.write("[keep-cache-alive] {}\n".format(msg))
    sys.stdout.flush()


def find_transcript(session_id):
    return cache_core.find_transcript(session_id)


def auto_detect_session(cwd):
    return cache_core.auto_detect_session(cwd)


def real_ping_command(session_id):
    return ["claude", "--resume", session_id, "-p", PING_PROMPT]


def send_ping(ping_command, session_id, cwd, dry):
    if dry:
        log("  (dry-run) would send ping via: {}".format(
            " ".join(shlex.quote(x) for x in (ping_command or real_ping_command(session_id)))))
        return True
    cmd = ping_command or real_ping_command(session_id)
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            log("  ping command exited {} (stderr: {})".format(
                r.returncode, (r.stderr or "").strip()[:200]))
            return False
        return True
    except (OSError, subprocess.TimeoutExpired) as e:
        log("  ping failed: {}".format(e))
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None,
                    help="session id to keep warm (auto-detected from --cwd if omitted)")
    ap.add_argument("--transcript", default=None, help="transcript path (auto-detected if omitted)")
    ap.add_argument("--cwd", default=None, help="directory to run the ping in")
    ap.add_argument("--max-pings", type=int, default=DEFAULT_MAX_PINGS)
    ap.add_argument("--interval-override", type=float, default=None,
                    help="seconds to actually sleep (testing); tier interval still reported")
    ap.add_argument("--ping-command", default=None,
                    help="shell command to run as the ping instead of `claude --resume`")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print-plan", action="store_true",
                    help="print the tier/interval/coverage plan and exit")
    args = ap.parse_args()

    cwd = args.cwd or os.getcwd()
    session_id = args.session
    transcript = args.transcript
    if not session_id:
        session_id, transcript = auto_detect_session(cwd)
        if not session_id:
            log("ERROR: could not auto-detect the current session for cwd {}. "
                "Pass --session <id>.".format(cwd))
            return 2
        log("auto-detected session {}".format(session_id))
    if not transcript:
        transcript = find_transcript(session_id)
    if not transcript or not os.path.exists(transcript):
        log("ERROR: could not locate transcript for session {}.".format(session_id))
        return 2
    ping_command = shlex.split(args.ping_command) if args.ping_command else None

    def detect():
        st = cache_core.get_cache_state(transcript, session_id)
        tier = st.get("tier")
        interval = cache_core.TIER_PING_INTERVAL.get(tier)
        return st, tier, interval

    st, tier, interval = detect()
    if tier is None:
        log("ERROR: no cache tier detected yet in the transcript.")
        return 2

    if args.print_plan:
        for t, iv in cache_core.TIER_PING_INTERVAL.items():
            cov = iv * args.max_pings
            log("tier {}: ping every {}s, max {} pings -> ~{} min coverage".format(
                t, iv, args.max_pings, cov // 60))
        log("current tier: {}".format(tier))
        return 0

    log("target session {} (tier {}), transcript {}".format(session_id, tier, transcript))
    log("plan: ping every {}s, up to {} pings".format(interval, args.max_pings))

    pings = 0
    prev_tier = None
    try:
        while pings < args.max_pings:
            st, tier, interval = detect()
            if tier is None:
                log("tier unknown this cycle; retrying shortly.")
                sleep_for = args.interval_override if args.interval_override is not None else 5
                time.sleep(sleep_for)
                continue
            # Adaptation: the tier is re-read every cycle, so a mid-loop switch
            # (e.g. 1h -> 5m on usage overage) changes the cadence immediately.
            if prev_tier is not None and tier != prev_tier:
                log("  tier changed {} -> {} since last cycle; cadence now {}s.".format(
                    prev_tier, tier, interval))
            remaining = st.get("remaining_seconds")
            rem_txt = "{:.0f}s".format(remaining) if remaining is not None else "?"
            sleep_for = args.interval_override if args.interval_override is not None else \
                max(0.0, min(interval, (remaining if remaining is not None else interval) - 5))
            log("cycle {}/{}: tier={} tier_interval={}s cache_remaining={} -> sleeping {:.0f}s".format(
                pings + 1, args.max_pings, tier, interval, rem_txt, sleep_for))
            time.sleep(sleep_for)

            # Re-detect AFTER sleeping so a switch during the sleep is honoured now.
            st2, tier2, interval2 = detect()
            if tier2 and tier2 != tier:
                log("  tier changed {} -> {} during sleep; cadence adapts to {}s.".format(
                    tier, tier2, cache_core.TIER_PING_INTERVAL.get(tier2)))
                tier = tier2
            prev_tier = tier
            ok = send_ping(ping_command, session_id, cwd, args.dry_run)
            pings += 1
            if ok:
                after = cache_core.get_cache_state(transcript, session_id)
                rs = after.get("remaining_seconds")
                log("  ping #{} sent; cache window now ~{} left ({} tier).".format(
                    pings, cache_core.fmt_mmss(rs) if rs is not None else "?",
                    cache_core.TIER_LABEL.get(after.get("tier"), "?")))
            else:
                log("  ping #{} did not succeed; continuing.".format(pings))
        log("reached max of {} pings; stopping. The cache will now expire "
            "naturally on the {} tier. Re-run to keep warming.".format(args.max_pings, tier))
        return 0
    except KeyboardInterrupt:
        log("stopped by user after {} ping(s). Clean exit.".format(pings))
        return 0


if __name__ == "__main__":
    sys.exit(main())
