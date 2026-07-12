#!/usr/bin/env python3
"""End-to-end tests for the UserPromptSubmit guard hook, invoked as a real
subprocess with the documented stdin contract."""
import json, os, sys, time, tempfile, shutil, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(PLUGIN, "lib"))
import cache_core

STATE = tempfile.mkdtemp(prefix="ca-guard-state-")
WORK = tempfile.mkdtemp(prefix="ca-guard-tx-")
GUARD = os.path.join(PLUGIN, "hooks", "guard.py")
# Both the in-process cache_core calls AND the guard subprocess must share the
# same state dir (in real use they inherit Claude Code's identical environment).
os.environ["CACHE_ASSISTANT_STATE_DIR"] = STATE
env = dict(os.environ, CACHE_ASSISTANT_STATE_DIR=STATE)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <<< " + str(extra)))
    if not cond: fails.append(name)

def iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def assistant(epoch, tier, read=25000, create=9000, inp=4):
    cc = {"ephemeral_5m_input_tokens": create if tier=="5m" else 0,
          "ephemeral_1h_input_tokens": create if tier=="1h" else 0}
    return {"type":"assistant","requestId":"req_%d"%int(epoch*1000),"timestamp":iso(epoch),
            "message":{"model":"claude-opus-4-8","usage":{"input_tokens":inp,"output_tokens":9,
            "cache_read_input_tokens":read,"cache_creation_input_tokens":create,"cache_creation":cc}}}

def new_tx(name):
    p = os.path.join(WORK, name+".jsonl")
    open(p, "w").close()
    return p

def append(p, obj):
    with open(p, "a") as fh: fh.write(json.dumps(obj)+"\n")

def run(session, tx, prompt):
    payload = json.dumps({"session_id":session,"transcript_path":tx,"prompt":prompt,
                          "hook_event_name":"UserPromptSubmit"})
    r = subprocess.run([sys.executable, GUARD], input=payload, capture_output=True,
                       text=True, env=env)
    out = {}
    if r.stdout.strip():
        try: out = json.loads(r.stdout)
        except ValueError: out = {"_raw": r.stdout}
    return out
def is_block(o): return o.get("decision") == "block"
def reason(o): return o.get("reason","")

# ============ Scenario A: expiry guard + acknowledgement ====================
print("\n-- Scenario A: expired cache --")
sess="A"; tx=new_tx("A")
append(tx, assistant(time.time()-4000, "1h"))   # anchor 4000s ago -> expired (ttl 3600)
cache_core.write_settings_state(sess, "opus", "high")
o1 = run(sess, tx, "please refactor this")
check("A1 first send BLOCKED on expiry", is_block(o1))
check("A1 reason mentions EXPIRED", "EXPIRED" in reason(o1), reason(o1))
check("A1 reason has token estimate", "tokens" in reason(o1))
o2 = run(sess, tx, "please refactor this")        # identical re-send
check("A2 re-send ALLOWED (acknowledged)", not is_block(o2), o2)
# model the real world: ack send produces a fresh warm turn
append(tx, assistant(time.time(), "1h"))
o3 = run(sess, tx, "next unrelated message")
check("A3 after warm turn, normal send allowed", not is_block(o3), o3)

# ============ Scenario B: model change guard ================================
print("\n-- Scenario B: model switch on warm cache --")
sess="B"; tx=new_tx("B")
append(tx, assistant(time.time(), "1h"))          # warm
cache_core.write_settings_state(sess, "claude-opus-4-8", "high")
o1 = run(sess, tx, "msg one")
check("B1 first message allowed (no baseline yet)", not is_block(o1))
cache_core.write_settings_state(sess, "claude-sonnet-5", "high")   # user switches model
o2 = run(sess, tx, "msg two")
check("B2 first msg under new model BLOCKED", is_block(o2))
check("B2 reason names the model switch", "model" in reason(o2) and "→" in reason(o2), reason(o2))
o3 = run(sess, tx, "msg two")                     # confirm
check("B3 re-send ALLOWED (acknowledged)", not is_block(o3))
o4 = run(sess, tx, "msg three still sonnet")
check("B4 subsequent send under sonnet NOT re-blocked", not is_block(o4), o4)

# ============ Scenario C: revert without cache damage ======================
print("\n-- Scenario C: revert model before sending --")
sess="C"; tx=new_tx("C")
append(tx, assistant(time.time(), "1h"))
cache_core.write_settings_state(sess, "claude-opus-4-8", "high")
run(sess, tx, "establish baseline")               # commit opus/high
cache_core.write_settings_state(sess, "claude-sonnet-5", "high")  # switch...
oblock = run(sess, tx, "risky send")
check("C1 switch blocks", is_block(oblock))
cache_core.write_settings_state(sess, "claude-opus-4-8", "high")  # ...revert before confirming
orevert = run(sess, tx, "a different message after reverting")
check("C2 after revert, send ALLOWED (cache intact)", not is_block(orevert), orevert)

# ============ Scenario D: effort change guard ==============================
print("\n-- Scenario D: effort switch on warm cache --")
sess="D"; tx=new_tx("D")
append(tx, assistant(time.time(), "1h"))
cache_core.write_settings_state(sess, "claude-opus-4-8", "high")
run(sess, tx, "baseline")
cache_core.write_settings_state(sess, "claude-opus-4-8", "low")   # effort high->low
o = run(sess, tx, "after effort change")
check("D1 effort change BLOCKED", is_block(o))
check("D1 reason names effort", "effort" in reason(o), reason(o))

# ============ Scenario E: bypasses =========================================
print("\n-- Scenario E: never-block cases --")
sess="E"; tx=new_tx("E")
append(tx, assistant(time.time()-4000, "1h"))     # expired, but...
cache_core.write_settings_state(sess, "opus", "high")
check("E1 slash command allowed", not is_block(run(sess, tx, "/model sonnet")))
sys.path.insert(0, os.path.join(PLUGIN, "hooks"))
import guard as guardmod
check("E2 keepalive ping allowed", not is_block(run(sess, tx, "hi "+guardmod.KEEPALIVE_MARKER)))
check("E3 empty prompt allowed", not is_block(run(sess, tx, "   ")))

print("\n%d failures" % len(fails))
shutil.rmtree(STATE, ignore_errors=True); shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if fails else 0)
