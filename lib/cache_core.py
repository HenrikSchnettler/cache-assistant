#!/usr/bin/env python3
"""
Cache Assistant — core engine.

Derives the *current* prompt-cache state of a Claude Code session from its
`.jsonl` transcript, efficiently enough to be called once per second by a
status line without ever re-parsing the whole file.

Ground truth (see RESEARCH.md):
  * Cache tier is recorded per assistant turn in
        message.usage.cache_creation.ephemeral_5m_input_tokens
        message.usage.cache_creation.ephemeral_1h_input_tokens
    Whichever is > 0 is the tier that turn wrote to. There is NO explicit
    ttl field anywhere in the transcript.
  * The cache TTL is a *sliding window*: every request that hits the cache
    resets the timer. The last cache-touching request is therefore the
    anchor, and the window expires at anchor + TTL.
  * The tier can change mid-session (subscription 1h -> usage-overage 5m,
    or via env overrides), so the tier must always be read from the newest
    turn, never captured once.

Efficiency design:
  * Per-session state is memoised on disk. Each call stats the transcript;
    if size+inode are unchanged since last time it reuses the cached anchor
    and tier with zero file reads (the "fast" path). If the file grew it
    seeks to the last byte offset it consumed and parses ONLY the appended
    lines (the "incremental" path). A tier change is always a newly appended
    line, so it is caught by the incremental path and can never be stale.
"""

import json
import os
import time

# Tier -> time-to-live in seconds.
TIER_TTL = {"5m": 300, "1h": 3600}
TIER_LABEL = {"5m": "5m", "1h": "1h"}

# Keep-alive ping recommendations per tier (seconds). Each sits a safety margin
# below the tier TTL so a ping always lands before the window closes:
#   5m TTL (300s) -> ping every 240s (60s margin)
#   1h TTL (3600s) -> ping every 3300s (300s margin)
TIER_PING_INTERVAL = {"5m": 240, "1h": 3300}

# Prompts carrying this marker are keep-alive pings; the guard hook never blocks
# them. Single source of truth, imported by the hook and the keep-alive loop.
KEEPALIVE_MARKER = "[cache-assistant:keepalive]"

_UNSET = object()


def state_dir():
    """Directory holding per-session memo/state files (overridable for tests)."""
    base = os.environ.get("CACHE_ASSISTANT_STATE_DIR")
    if not base:
        base = os.path.join(os.environ.get("TMPDIR") or "/tmp", "cache-assistant")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    return base


def state_path(session_id):
    safe = "".join(c for c in (session_id or "unknown") if c.isalnum() or c in "-_")
    return os.path.join(state_dir(), "state-{}.json".format(safe or "unknown"))


def _load_state(session_id):
    try:
        with open(state_path(session_id), "r") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _atomic_write(path, data):
    tmp = "{}.{}.tmp".format(path, os.getpid())
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _save_state(session_id, full):
    _atomic_write(state_path(session_id), full)


def _parse_iso_epoch(ts):
    """Parse '2026-07-12T11:07:23.870Z' -> epoch seconds (UTC). Cheap, no imports."""
    if not ts:
        return None
    try:
        # Strip trailing Z / timezone; transcript timestamps are always UTC.
        core = ts.strip()
        if core.endswith("Z"):
            core = core[:-1]
        date_part, _, time_part = core.partition("T")
        y, mo, d = (int(x) for x in date_part.split("-"))
        hh, mm, rest = time_part.split(":")
        sec = float(rest)
        # days since epoch via a fixed formula (UTC, proleptic Gregorian).
        # Use time.timegm-equivalent through calendar without importing calendar:
        import datetime  # stdlib, ~1ms, only hit when a NEW line is parsed
        dt = datetime.datetime(y, mo, d, int(hh), int(mm), int(sec),
                               int(round((sec - int(sec)) * 1_000_000)),
                               tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _scan_assistant_line(obj, acc):
    """Fold one transcript object into the running accumulator `acc`.

    Only `assistant` entries that actually touched the cache matter. Multiple
    lines can share one requestId (content blocks of a single API response and
    thus a single cache access) with identical usage; taking the latest such
    line is correct and never double counts.
    """
    if obj.get("type") != "assistant":
        return
    msg = obj.get("message") or {}
    usage = msg.get("usage") or {}
    read = usage.get("cache_read_input_tokens") or 0
    create = usage.get("cache_creation_input_tokens") or 0
    inp = usage.get("input_tokens") or 0
    if read == 0 and create == 0:
        return  # no cache interaction on this request
    ts = _parse_iso_epoch(obj.get("timestamp"))
    if ts is None:
        return
    # This request refreshed the cache -> it becomes the anchor if newest.
    if acc["anchor_epoch"] is None or ts >= acc["anchor_epoch"]:
        acc["anchor_epoch"] = ts
        # On a cold cache the whole current prefix is re-written: read+create+input.
        acc["rewrite_tokens"] = int(read + create + inp)
        acc["last_request_id"] = obj.get("requestId")
    # Tier is only defined by a WRITE. Track the newest turn that wrote.
    cc = usage.get("cache_creation") or {}
    t1 = cc.get("ephemeral_1h_input_tokens") or 0
    t5 = cc.get("ephemeral_5m_input_tokens") or 0
    tier = "1h" if t1 > 0 else ("5m" if t5 > 0 else None)
    if tier is not None and (acc["tier_epoch"] is None or ts >= acc["tier_epoch"]):
        acc["tier_epoch"] = ts
        acc["tier"] = tier


def _blank_acc():
    return {
        "anchor_epoch": None,
        "tier": None,
        "tier_epoch": None,
        "rewrite_tokens": None,
        "last_request_id": None,
    }


def _acc_from_cache(cache):
    acc = _blank_acc()
    for k in acc:
        if k in cache:
            acc[k] = cache[k]
    return acc


def get_cache_state(transcript_path, session_id, now=None):
    """Return the current cache state. See module docstring for the contract."""
    if now is None:
        now = time.time()

    result = {
        "tier": None,
        "ttl_seconds": None,
        "anchor_epoch": None,
        "remaining_seconds": None,
        "expired": None,
        "rewrite_tokens": None,
        "have_data": False,
        "path": "none",  # which code path ran: none|fast|incremental|full
    }

    full = _load_state(session_id)
    cache = full.get("cache") or {}

    try:
        st = os.stat(transcript_path) if transcript_path else None
    except OSError:
        st = None

    if st is None:
        # No transcript yet (brand new session). Return "no data".
        return result

    size = st.st_size
    inode = st.st_ino
    same_file = (cache.get("path_key") == transcript_path
                 and cache.get("inode") == inode)

    if same_file and cache.get("size") == size and cache.get("anchor_epoch"):
        # FAST PATH: nothing appended since last call. Reuse memoised anchor.
        acc = _acc_from_cache(cache)
        result["path"] = "fast"
    else:
        # Need to read. Incremental if we can trust the prior offset, else full.
        acc = _acc_from_cache(cache) if same_file and size >= cache.get("size", 0) else _blank_acc()
        start = cache.get("consumed", 0) if (same_file and size >= cache.get("consumed", 0)) else 0
        result["path"] = "incremental" if start > 0 else "full"
        consumed = start
        try:
            with open(transcript_path, "rb") as fh:
                fh.seek(start)
                chunk = fh.read()
            last_nl = chunk.rfind(b"\n")
            if last_nl != -1:
                complete = chunk[: last_nl + 1]
                consumed = start + last_nl + 1
                for raw in complete.split(b"\n"):
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    _scan_assistant_line(obj, acc)
            # else: only a partial line so far; keep prior acc + offset.
        except OSError:
            return result
        # Persist the refreshed memo (preserving the settings sensor block).
        new_cache = dict(acc)
        new_cache.update({
            "path_key": transcript_path,
            "inode": inode,
            "size": size,
            "consumed": consumed,
        })
        full["cache"] = new_cache
        _save_state(session_id, full)

    if acc["anchor_epoch"] is None:
        return result  # transcript exists but no cache-bearing turn yet

    tier = acc["tier"]
    result["have_data"] = True
    result["tier"] = tier
    result["anchor_epoch"] = acc["anchor_epoch"]
    result["rewrite_tokens"] = acc["rewrite_tokens"]
    if tier in TIER_TTL:
        ttl = TIER_TTL[tier]
        result["ttl_seconds"] = ttl
        remaining = acc["anchor_epoch"] + ttl - now
        result["remaining_seconds"] = remaining
        result["expired"] = remaining <= 0
    return result


# --- settings sensor (model / effort), written by the status line ----------

def write_settings_state(session_id, model_id, effort_level, now=None):
    """Record the live model + effort so the guard hook can read them.

    The status line receives model.id and effort.level on stdin (the live
    session values, including mid-session /model and /effort changes) and calls
    this every render, so the file trails the true selection by <= refreshInterval.
    """
    if now is None:
        now = time.time()
    full = _load_state(session_id)
    full["settings"] = {
        "model_id": model_id,
        "effort_level": effort_level,
        "updated_at": now,
    }
    _save_state(session_id, full)


def read_settings_state(session_id):
    return (_load_state(session_id).get("settings")) or {}


def _projects_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def find_transcript(session_id):
    """Locate a session's transcript by id (prefers the top-level over subagent)."""
    import glob
    hits = glob.glob(os.path.join(_projects_dir(), "**", session_id + ".jsonl"),
                     recursive=True)
    hits.sort(key=lambda p: ("subagents" in p, len(p)))
    return hits[0] if hits else None


def entry_cwd(path):
    """Best-effort cwd recorded in a transcript's first useful line."""
    try:
        with open(path) as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("cwd"):
                    return obj["cwd"]
    except OSError:
        pass
    return None


def auto_detect_session(cwd):
    """The current session = newest top-level transcript whose cwd matches `cwd`.
    Returns (session_id, transcript_path) or (None, None)."""
    import glob
    cands = [p for p in glob.glob(os.path.join(_projects_dir(), "**", "*.jsonl"),
                                  recursive=True) if "subagents" not in p]
    best = None
    for p in cands:
        if entry_cwd(p) != cwd:
            continue
        m = os.path.getmtime(p)
        if best is None or m > best[0]:
            best = (m, p)
    if not best:
        return None, None
    return os.path.splitext(os.path.basename(best[1]))[0], best[1]


def fmt_mmss(seconds):
    """Format a (possibly negative) seconds value as mm:ss, clamped at 0."""
    s = int(seconds)
    if s < 0:
        s = 0
    return "{:d}:{:02d}".format(s // 60, s % 60)


def fmt_tokens(n):
    if n is None:
        return "?"
    n = int(n)
    if n >= 1000:
        return "{:.1f}k".format(n / 1000.0)
    return str(n)
