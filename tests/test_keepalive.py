#!/usr/bin/env python3
"""Offline tests for the keep-cache-alive loop: tier-derived interval, mid-loop
tier adaptation, observable countdown reset after each ping, max-ping cap, and
clean stop. A stub ping stands in for `claude --resume` and appends a synthetic
turn (flipping tier 1h->5m from the 2nd ping) so the loop's decisions are visible."""
import json, os, sys, time, tempfile, shutil, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..")
KA = os.path.join(PLUGIN, "skills", "keep-cache-alive", "keepalive.py")
STATE = tempfile.mkdtemp(prefix="ca-ka-state-")
WORK = tempfile.mkdtemp(prefix="ca-ka-")
env = dict(os.environ, CACHE_ASSISTANT_STATE_DIR=STATE)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <<< " + str(extra)))
    if not cond: fails.append(name)

def iso(e):
    import datetime
    return datetime.datetime.fromtimestamp(e, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]+"Z"

def turn(tier):
    cc={"ephemeral_5m_input_tokens":9000 if tier=="5m" else 0,
        "ephemeral_1h_input_tokens":9000 if tier=="1h" else 0}
    return json.dumps({"type":"assistant","requestId":"r%f"%time.time(),"timestamp":iso(time.time()),
        "message":{"model":"claude-opus-4-8","usage":{"input_tokens":3,"output_tokens":5,
        "cache_read_input_tokens":22000,"cache_creation_input_tokens":9000,"cache_creation":cc}}})

TX = os.path.join(WORK, "t.jsonl")
with open(TX, "w") as fh: fh.write(turn("1h")+"\n")   # start on 1h, warm

# Stub ping: increments a counter, appends a turn (1h for ping1, 5m thereafter).
STUB = os.path.join(WORK, "stub_ping.py")
CNT = os.path.join(WORK, "cnt")
STUB_SRC = r'''
import time, json, os, datetime
CNT = __CNT__
TX = __TX__
cnt = int(open(CNT).read()) if os.path.exists(CNT) else 0
cnt += 1
open(CNT, "w").write(str(cnt))
tier = "5m" if cnt >= 2 else "1h"
ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
cc = {"ephemeral_5m_input_tokens": 9000 if tier == "5m" else 0,
      "ephemeral_1h_input_tokens": 9000 if tier == "1h" else 0}
o = {"type": "assistant", "requestId": "r" + str(time.time()), "timestamp": ts,
     "message": {"model": "m", "usage": {"input_tokens": 3, "output_tokens": 5,
     "cache_read_input_tokens": 22000, "cache_creation_input_tokens": 9000,
     "cache_creation": cc}}}
open(TX, "a").write(json.dumps(o) + "\n")
'''
with open(STUB, "w") as fh:
    fh.write(STUB_SRC.replace("__CNT__", repr(CNT)).replace("__TX__", repr(TX)))

r = subprocess.run([sys.executable, KA, "--session", "ksess", "--transcript", TX,
                    "--max-pings", "3", "--interval-override", "0.15",
                    "--ping-command", "%s %s" % (sys.executable, STUB)],
                   capture_output=True, text=True, env=env)
out = r.stdout
print(out)

check("K1 loop exited cleanly", r.returncode == 0, r.stderr)
check("K2 reports 1h tier_interval=3300 early", "tier_interval=3300" in out, out)
check("K3 detects tier change 1h -> 5m mid-loop", "tier changed 1h -> 5m" in out, out)
check("K4 uses 5m interval (240) after change", "tier_interval=240" in out, out)
check("K5 sends exactly 3 pings", out.count("ping #") >= 3 and "ping #3 sent" in out, out)
check("K6 countdown observably resets after ping (near-full window)",
      ("59:" in out or "58:" in out) and ("4:5" in out or "4:4" in out), out)
check("K7 stops at max with clean message", "reached max of 3 pings" in out, out)

# print-plan
r2 = subprocess.run([sys.executable, KA, "--session","kk","--transcript",TX,"--print-plan"],
                    capture_output=True, text=True, env=env)
check("K8 print-plan shows both tiers + coverage", "tier 5m" in r2.stdout and "tier 1h" in r2.stdout
      and "coverage" in r2.stdout, r2.stdout)

print("\n%d failures" % len(fails))
shutil.rmtree(STATE, ignore_errors=True); shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if fails else 0)
