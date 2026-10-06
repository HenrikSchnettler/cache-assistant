#!/usr/bin/env python3
"""Correctness + efficiency tests for cache_core, using synthetic transcripts
that mirror the real Claude Code .jsonl schema (verified against on-disk data)."""
import json, os, sys, time, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import cache_core

STATE = tempfile.mkdtemp(prefix="ca-test-")
os.environ["CACHE_ASSISTANT_STATE_DIR"] = STATE
WORK = tempfile.mkdtemp(prefix="ca-tx-")
TX = os.path.join(WORK, "t.jsonl")

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <<< " + str(extra)))
    if not cond:
        fails.append(name)

def iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def assistant(epoch, tier, read=20000, create=8000, inp=3, rid=None):
    cc = {"ephemeral_5m_input_tokens": create if tier == "5m" else 0,
          "ephemeral_1h_input_tokens": create if tier == "1h" else 0}
    return {"type": "assistant", "requestId": rid or ("req_%d" % int(epoch*1000)),
            "timestamp": iso(epoch),
            "message": {"model": "claude-opus-4-8",
                        "usage": {"input_tokens": inp, "output_tokens": 500,
                                  "cache_read_input_tokens": read,
                                  "cache_creation_input_tokens": create,
                                  "cache_creation": cc}}}

def append(obj):
    with open(TX, "a") as fh:
        fh.write(json.dumps(obj) + "\n")

# --- Build a 1h session, anchor at t0 ---------------------------------------
t0 = 1_780_000_000.0
append({"type": "user", "timestamp": iso(t0 - 5)})
append(assistant(t0 - 2, "1h"))
append(assistant(t0, "1h"))          # newest write

s = cache_core.get_cache_state(TX, "sess", now=t0 + 10)
check("tier is 1h", s["tier"] == "1h", s["tier"])
check("ttl 3600", s["ttl_seconds"] == 3600)
check("anchor == t0", abs(s["anchor_epoch"] - t0) < 0.01, s["anchor_epoch"])
check("remaining ~3590", abs(s["remaining_seconds"] - 3590) < 1.5, s["remaining_seconds"])
check("not expired", s["expired"] is False)
check("first parse is full", s["path"] == "full", s["path"])
check("rewrite tokens = read+create+input", s["rewrite_tokens"] == 20000 + 8000 + 3, s["rewrite_tokens"])

# --- FAST PATH: no new bytes -> reuse anchor, countdown advances -------------
s2 = cache_core.get_cache_state(TX, "sess", now=t0 + 20)
check("second call is fast (no reparse)", s2["path"] == "fast", s2["path"])
check("fast path advances countdown", abs(s2["remaining_seconds"] - 3580) < 1.5, s2["remaining_seconds"])
check("fast path keeps tier", s2["tier"] == "1h")
check("fast path keeps anchor", abs(s2["anchor_epoch"] - t0) < 0.01)

# --- MID-SESSION TIER CHANGE 1h -> 5m (usage overage) -----------------------
t1 = t0 + 120
append(assistant(t1, "5m"))          # tier flips to 5m
s3 = cache_core.get_cache_state(TX, "sess", now=t1 + 5)
check("tier flips to 5m", s3["tier"] == "5m", s3["tier"])
check("tier-change parse is incremental (NOT full)", s3["path"] == "incremental", s3["path"])
check("ttl re-based to 300", s3["ttl_seconds"] == 300)
check("anchor re-based to t1", abs(s3["anchor_epoch"] - t1) < 0.01, s3["anchor_epoch"])
check("remaining re-based ~295", abs(s3["remaining_seconds"] - 295) < 1.5, s3["remaining_seconds"])

# On the 5m tier, after 300s the same anchor is expired (fast path, no new data)
s3b = cache_core.get_cache_state(TX, "sess", now=t1 + 305)
check("5m expired after 305s via fast path", s3b["expired"] is True and s3b["path"] == "fast",
      (s3b["expired"], s3b["path"]))

# --- MID-SESSION TIER CHANGE back 5m -> 1h ----------------------------------
t2 = t1 + 200
append(assistant(t2, "1h"))
s4 = cache_core.get_cache_state(TX, "sess", now=t2 + 5)
check("tier flips back to 1h", s4["tier"] == "1h", s4["tier"])
check("ttl back to 3600", s4["ttl_seconds"] == 3600)
check("anchor re-based to t2", abs(s4["anchor_epoch"] - t2) < 0.01)

# --- A pure cache-READ turn (no creation) keeps tier, updates anchor --------
t3 = t2 + 60
append(assistant(t3, "1h", read=30000, create=0))   # read-only refresh
s5 = cache_core.get_cache_state(TX, "sess", now=t3 + 5)
check("read-only turn advances anchor", abs(s5["anchor_epoch"] - t3) < 0.01, s5["anchor_epoch"])
check("read-only turn keeps last-write tier 1h", s5["tier"] == "1h", s5["tier"])

# --- EFFICIENCY: 2000 idle ticks must never trigger a full/incremental reparse
mtime_before = os.path.getmtime(TX)
t_start = time.perf_counter()
paths = set()
N = 2000
for i in range(N):
    r = cache_core.get_cache_state(TX, "sess", now=t3 + 10 + i)
    paths.add(r["path"])
elapsed = time.perf_counter() - t_start
check("2000 idle ticks all fast path", paths == {"fast"}, paths)
check("transcript untouched by reads", os.path.getmtime(TX) == mtime_before)
per_call_us = elapsed / N * 1e6
print("  in-process per-call (fast path): %.1f us  (%.4fs for %d calls)" % (per_call_us, elapsed, N))
check("fast path < 200us/call in-process", per_call_us < 200, per_call_us)

# --- COMPACTION: boundary / summary reset the anchor; idle after it stays fast
def compact_case(name, line):
    path = os.path.join(WORK, name + ".jsonl")
    with open(path, "w") as fh:
        fh.write(json.dumps(assistant(t0 - 4000, "1h", rid="r_" + name)) + "\n")
        fh.write(json.dumps(line) + "\n")
    r = cache_core.get_cache_state(path, name, now=t0)
    check(name + ": no stale anchor after compact", r["have_data"] is False and r["expired"] is None, r)
    r2 = cache_core.get_cache_state(path, name, now=t0 + 1)
    check(name + ": idle tick after compact is fast", r2["path"] == "fast", r2["path"])
    with open(path, "a") as fh:
        fh.write(json.dumps(assistant(t0, "1h", rid="r2_" + name)) + "\n")
    r3 = cache_core.get_cache_state(path, name, now=t0 + 2)
    check(name + ": next turn re-anchors (warm)", r3["have_data"] and r3["expired"] is False, r3)

compact_case("boundary", {"type": "system", "subtype": "compact_boundary",
                          "content": "Conversation compacted", "timestamp": iso(t0 - 5)})
compact_case("summary", {"type": "user", "isCompactSummary": True, "timestamp": iso(t0 - 5),
                         "message": {"role": "user", "content": "summary"}})

# --- Unexpected cache miss + keep-alive touch + threshold --------------------
def fresh(name):
    path = os.path.join(WORK, name + ".jsonl")
    open(path, "w").close()
    return path
def add(path, obj):
    with open(path, "a") as fh:
        fh.write(json.dumps(obj) + "\n")

p_miss = fresh("miss")
add(p_miss, assistant(t0, "1h", read=60000, create=2000, rid="m1"))
add(p_miss, assistant(t0 + 60, "1h", read=61000, create=900, rid="m2"))
r = cache_core.get_cache_state(p_miss, "miss", now=t0 + 61)
check("miss: a normal warm hit is no miss", r["miss"] is None, r["miss"])
busted = assistant(t0 + 120, "1h", read=0, create=63000, rid="m3")
busted["message"]["model"] = "claude-sonnet-5-5"
add(p_miss, busted)
r = cache_core.get_cache_state(p_miss, "miss", now=t0 + 121)
check("miss: read 0 inside the window is flagged", bool(r["miss"]), r)
check("miss: carries expected vs read", r["miss"]["expected_tokens"] == 61903
      and r["miss"]["read_tokens"] == 0, r["miss"])
check("miss: attributes a model change", r["miss"]["model_changed"] is True, r["miss"])
check("miss: model reported", r["model"] == "claude-sonnet-5-5", r["model"])

p_exp = fresh("expd")
add(p_exp, assistant(t0, "5m", read=60000, create=2000, rid="e1"))
add(p_exp, assistant(t0 + 900, "5m", read=0, create=62000, rid="e2"))
r = cache_core.get_cache_state(p_exp, "expd", now=t0 + 901)
check("miss: a re-write after the window lapsed is NOT a miss", r["miss"] is None, r["miss"])

p_same = fresh("same")
add(p_same, assistant(t0, "1h", read=60000, create=2000, rid="s1"))
add(p_same, assistant(t0 + 1, "1h", read=60000, create=2000, rid="s1"))
r = cache_core.get_cache_state(p_same, "same", now=t0 + 2)
check("miss: blocks of one request never flag", r["miss"] is None, r["miss"])

p_touch = fresh("touch")
add(p_touch, assistant(t0, "5m", rid="t1"))
r = cache_core.get_cache_state(p_touch, "touch", now=t0 + 400)
check("touch: expired without a ping", r["expired"] is True, r)
cache_core.record_touch("touch", now=t0 + 240)
r = cache_core.get_cache_state(p_touch, "touch", now=t0 + 400)
check("touch: a keep-alive hit slides the window", r["expired"] is False
      and abs(r["remaining_seconds"] - 140) < 0.01, r)
check("touch: fast path still used", r["path"] == "fast", r["path"])
add(p_touch, assistant(t0 + 500, "5m", rid="t2"))
r = cache_core.get_cache_state(p_touch, "touch", now=t0 + 501)
check("touch: a newer turn wins over an older touch", r["anchor_epoch"] == t0 + 500, r)

os.environ.pop("CLAUDE_PLUGIN_OPTION_BLOCK_THRESHOLD_TOKENS", None)
check("threshold: default", cache_core.block_threshold() == cache_core.DEFAULT_BLOCK_THRESHOLD)
check("threshold: below is small", not cache_core.is_large(cache_core.DEFAULT_BLOCK_THRESHOLD - 1))
check("threshold: at is large", cache_core.is_large(cache_core.DEFAULT_BLOCK_THRESHOLD))
check("threshold: unknown size is large", cache_core.is_large(None))
os.environ["CLAUDE_PLUGIN_OPTION_BLOCK_THRESHOLD_TOKENS"] = "1234"
check("threshold: plugin option wins", cache_core.block_threshold() == 1234)
os.environ["CLAUDE_PLUGIN_OPTION_BLOCK_THRESHOLD_TOKENS"] = "nonsense"
check("threshold: garbage falls back", cache_core.block_threshold() == cache_core.DEFAULT_BLOCK_THRESHOLD)
os.environ.pop("CLAUDE_PLUGIN_OPTION_BLOCK_THRESHOLD_TOKENS", None)

print("\n%d checks, %d failures" % (0, len(fails)))
shutil.rmtree(STATE, ignore_errors=True)
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if fails else 0)
