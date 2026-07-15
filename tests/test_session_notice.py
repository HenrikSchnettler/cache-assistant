#!/usr/bin/env python3
"""End-to-end tests for the SessionStart notice hook, invoked as a real
subprocess with the documented SessionStart stdin contract."""
import json, os, sys, time, tempfile, shutil, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(PLUGIN, "lib"))
import cache_core

STATE = tempfile.mkdtemp(prefix="ca-notice-state-")
WORK = tempfile.mkdtemp(prefix="ca-notice-tx-")
NOTICE = os.path.join(PLUGIN, "hooks", "session_notice.py")
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

def run(session, tx, source="resume"):
    payload = json.dumps({"session_id":session,"transcript_path":tx,"cwd":WORK,
                          "hook_event_name":"SessionStart","source":source})
    r = subprocess.run([sys.executable, NOTICE], input=payload, capture_output=True,
                       text=True, env=env)
    return r.returncode, r.stderr, r.stdout

# A user-visible warning == exit code 2 with the message on stderr.
def warned(rc, err):
    return rc == 2 and bool(err.strip())

# ============ Scenario A: expired window on resume -> warn =================
print("\n-- Scenario A: resume with expired cache --")
sess="A"; tx=new_tx("A")
append(tx, assistant(time.time()-4000, "1h"))   # anchor 4000s ago -> expired (ttl 3600)
rc, err, out = run(sess, tx)
check("A1 warns the user (exit 2 + stderr)", warned(rc, err), (rc, err))
check("A1 message mentions EXPIRED", "EXPIRED" in err, err)
check("A1 message has token estimate", "tokens" in err, err)
check("A1 nothing on stdout (stderr is the user channel)", out.strip() == "", out)

# ============ Scenario B: still-warm window -> silent ======================
print("\n-- Scenario B: resume with warm cache --")
sess="B"; tx=new_tx("B")
append(tx, assistant(time.time(), "1h"))          # fresh anchor -> warm
rc, err, out = run(sess, tx)
check("B1 exits 0 while warm", rc == 0, rc)
check("B1 stays silent while warm", not err.strip(), err)

# ============ Scenario C: brand-new session -> silent ======================
print("\n-- Scenario C: startup, no cache data --")
sess="C"; tx=new_tx("C")                           # empty transcript, no turns
rc, err, out = run(sess, tx, source="startup")
check("C1 exits 0", rc == 0, rc)
check("C1 silent with no cache data", not err.strip(), err)

# ============ Scenario D: 5m tier expired names the tier ===================
print("\n-- Scenario D: expired 5m window --")
sess="D"; tx=new_tx("D")
append(tx, assistant(time.time()-600, "5m"))       # 600s ago -> expired (ttl 300)
rc, err, out = run(sess, tx)
check("D1 warns the user", warned(rc, err), (rc, err))
check("D1 names the 5m tier", "5m" in err, err)

# ============ Scenario E: missing transcript -> silent, never crashes ======
print("\n-- Scenario E: nonexistent transcript --")
sess="E"
rc, err, out = run(sess, os.path.join(WORK, "does-not-exist.jsonl"))
check("E1 exits 0 (no crash, not a scary exit 2)", rc == 0, (rc, err))
check("E1 silent with no transcript", not err.strip(), err)

print("\n%d failures" % len(fails))
shutil.rmtree(STATE, ignore_errors=True); shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if fails else 0)
