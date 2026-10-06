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

def assistant(epoch, tier, read=85000, create=9000, inp=4):
    cc = {"ephemeral_5m_input_tokens": create if tier=="5m" else 0,
          "ephemeral_1h_input_tokens": create if tier=="1h" else 0}
    return {"type":"assistant","requestId":"req_%d"%int(epoch*1000),"timestamp":iso(epoch),
            "message":{"model":"claude-opus-4-8","usage":{"input_tokens":inp,"output_tokens":9,
            "cache_read_input_tokens":read,"cache_creation_input_tokens":create,"cache_creation":cc}}}

def compact_boundary(epoch):
    # Shape of the line Claude Code appends on /compact or auto-compact.
    return {"type":"system","subtype":"compact_boundary","content":"Conversation compacted",
            "timestamp":iso(epoch),"compactMetadata":{"trigger":"manual","preTokens":34004}}

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

# ============ Scenario F: compact after expiry =============================
print("\n-- Scenario F: compact after the window expired --")
sess="F"; tx=new_tx("F")
append(tx, assistant(time.time()-4000, "1h"))     # expired...
cache_core.write_settings_state(sess, "opus", "high")
check("F0 expired before compact (blocks)", is_block(run(sess, tx, "before compact")))
append(tx, compact_boundary(time.time()-10))      # ...then the user compacts
o1 = run(sess, tx, "first message after compact")
check("F1 first send after compact NOT blocked", not is_block(o1), o1)
append(tx, assistant(time.time(), "1h"))          # that turn caches the new prefix
check("F2 next send allowed", not is_block(run(sess, tx, "second message")))
# Expiry still works after a compact once a new window has run out.
sess="F3"; tx=new_tx("F3")
append(tx, compact_boundary(time.time()-9000))
append(tx, assistant(time.time()-4000, "1h"))
check("F3 post-compact window that expired still blocks", is_block(run(sess, tx, "late")))

# ============ Scenario G: small re-write is never blocked ====================
print("\n-- Scenario G: block threshold --")
sess="G"; tx=new_tx("G")
append(tx, assistant(time.time()-4000, "1h", read=9000, create=3000))   # expired, ~12k
check("G1 expired but small: NOT blocked", not is_block(run(sess, tx, "carry on")))
env["CLAUDE_PLUGIN_OPTION_BLOCK_THRESHOLD_TOKENS"] = "10000"
check("G2 same session blocks once the threshold is lowered",
      is_block(run(sess, tx, "carry on")))
env["CLAUDE_PLUGIN_OPTION_BLOCK_THRESHOLD_TOKENS"] = "0"
sess="G3"; tx=new_tx("G3")
append(tx, assistant(time.time()-4000, "1h", read=10, create=5))
check("G3 threshold 0 always blocks", is_block(run(sess, tx, "tiny")))
del env["CLAUDE_PLUGIN_OPTION_BLOCK_THRESHOLD_TOKENS"]
sess="G4"; tx=new_tx("G4")
append(tx, assistant(time.time()-10, "1h", read=9000, create=3000))     # warm, small
cache_core.write_settings_state(sess, "opus", "high")
run(sess, tx, "baseline")
cache_core.write_settings_state(sess, "sonnet", "high")
check("G4 model change on a small warm cache NOT blocked",
      not is_block(run(sess, tx, "after switch")))

# ============ Scenario H: PreModelSwitch / PostModelSwitch hook ==============
print("\n-- Scenario H: model-switch hook --")
SWITCH = os.path.join(PLUGIN, "hooks", "model_switch.py")
def switch(session, event, src, dst, source="command", warm=True, tokens=120000):
    payload = json.dumps({"session_id":session,"hook_event_name":event,
        "from_model":src,"to_model":dst,"requested_model":dst,"source":source,
        "context_tokens":tokens,"prompt_cache_warm":warm,"cache_ttl":"1h",
        "estimated_cache_write_usd":1.2,"pricing":"catalog"})
    r = subprocess.run([sys.executable, SWITCH], input=payload, capture_output=True,
                       text=True, env=env)
    return json.loads(r.stdout) if r.stdout.strip() else {}
def decision(o): return (o.get("hookSpecificOutput") or {}).get("permissionDecision")

o = switch("H", "PreModelSwitch", "opus", "sonnet")
check("H1 warm + large switch asks for confirmation", decision(o) == "ask", o)
check("H1 reason names the re-cache size",
      "120.0k" in o["hookSpecificOutput"]["permissionDecisionReason"], o)
check("H2 cold cache switch passes silently",
      switch("H", "PreModelSwitch", "opus", "sonnet", warm=False) == {})
check("H3 small switch passes silently",
      switch("H", "PreModelSwitch", "opus", "sonnet", tokens=8000) == {})
check("H4 SDK switch is not asked (nobody to ask)",
      switch("H", "PreModelSwitch", "opus", "sonnet", source="sdk") == {})

# confirmed at the switch prompt -> the send guard does not ask again
sess="H5"; tx=new_tx("H5")
append(tx, assistant(time.time()-10, "1h"))
cache_core.write_settings_state(sess, "opus", "high")
run(sess, tx, "baseline")
switch(sess, "PreModelSwitch", "opus", "sonnet")
switch(sess, "PostModelSwitch", "opus", "sonnet")
cache_core.write_settings_state(sess, "sonnet", "high")
check("H5 confirmed switch: first send NOT blocked again",
      not is_block(run(sess, tx, "go on")))

# a switch nobody could confirm (SDK / desktop picker) blocks the send once
sess="H6"; tx=new_tx("H6")
append(tx, assistant(time.time()-10, "1h"))
run(sess, tx, "baseline")
switch(sess, "PostModelSwitch", "opus", "sonnet", source="sdk")
o1 = run(sess, tx, "go on")
check("H6 unconfirmed switch: first send BLOCKED", is_block(o1), o1)
check("H6 reason names both models", "opus" in reason(o1) and "sonnet" in reason(o1), reason(o1))
check("H6 re-send allowed", not is_block(run(sess, tx, "go on")))

sess="H7"; tx=new_tx("H7")
append(tx, assistant(time.time()-10, "1h"))
run(sess, tx, "baseline")
switch(sess, "PostModelSwitch", "opus", "sonnet", source="sdk")
switch(sess, "PostModelSwitch", "sonnet", "opus", source="sdk")
check("H7 switching back clears the block", not is_block(run(sess, tx, "go on")))

sess="H8"; tx=new_tx("H8")
append(tx, assistant(time.time()-10, "1h", read=9000, create=3000))
run(sess, tx, "baseline")
switch(sess, "PostModelSwitch", "opus", "sonnet", source="sdk", tokens=12000)
check("H8 unconfirmed but small switch NOT blocked", not is_block(run(sess, tx, "go on")))

print("\n%d failures" % len(fails))
shutil.rmtree(STATE, ignore_errors=True); shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if fails else 0)
