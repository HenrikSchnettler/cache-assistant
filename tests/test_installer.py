#!/usr/bin/env python3
"""Tests for the non-destructive status line installer. Actually executes the
resulting statusLine.command to prove both the user's row and ours render."""
import json, os, sys, time, tempfile, shutil, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..")
INSTALLER = os.path.join(PLUGIN, "skills", "install-statusline", "install_statusline.py")
OUR_SL = os.path.join(PLUGIN, "statusline", "statusline.py")

STATE = tempfile.mkdtemp(prefix="ca-inst-state-")
WORK = tempfile.mkdtemp(prefix="ca-inst-")
env = dict(os.environ, CACHE_ASSISTANT_STATE_DIR=STATE, NO_COLOR="1")

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <<< " + str(extra)))
    if not cond: fails.append(name)

def iso(e):
    import datetime
    return datetime.datetime.fromtimestamp(e, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]+"Z"

# synthetic warm 1h transcript so our row shows a countdown
TX = os.path.join(WORK, "tx.jsonl")
with open(TX, "w") as fh:
    cc = {"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":9000}
    fh.write(json.dumps({"type":"assistant","requestId":"r1","timestamp":iso(time.time()),
        "message":{"model":"claude-opus-4-8","usage":{"input_tokens":3,"output_tokens":5,
        "cache_read_input_tokens":20000,"cache_creation_input_tokens":9000,"cache_creation":cc}}})+"\n")

STDIN = json.dumps({"session_id":"inst","transcript_path":TX,
                    "model":{"id":"claude-opus-4-8","display_name":"Opus"},"effort":{"level":"high"}})

def install(settings_path, *extra):
    return subprocess.run([sys.executable, INSTALLER, "--settings", settings_path,
                           "--statusline", OUR_SL, *extra],
                          capture_output=True, text=True, env=env)

def run_statusline_command(settings_path):
    sl = json.load(open(settings_path))["statusLine"]
    r = subprocess.run(["sh", "-c", sl["command"]], input=STDIN, capture_output=True, text=True, env=env)
    return sl, r.stdout

# ===== Case 1: no existing status line =====================================
print("\n-- Case 1: fresh install (no prior status line) --")
s1 = os.path.join(WORK, "s1"); os.makedirs(s1); p1 = os.path.join(s1, "settings.json")
json.dump({"model":"opus"}, open(p1,"w"))
r = install(p1)
check("1 installer ok", r.returncode == 0, r.stderr)
cfg = json.load(open(p1))
check("1 model key preserved", cfg.get("model") == "opus")
check("1 refreshInterval=1", cfg["statusLine"].get("refreshInterval") == 1, cfg["statusLine"])
sl, out = run_statusline_command(p1)
check("1 our cache row renders", "⚡ cache 1h" in out and "left" in out, out)
# re-running after a fresh (direct) install must refresh in place, NOT wrap our own command
install(p1)
cfg1b = json.load(open(p1))
check("1 re-run stays direct (not self-wrapped)",
      "cache-assistant-statusline.sh" not in cfg1b["statusLine"]["command"]
      and "statusline.py" in cfg1b["statusLine"]["command"], cfg1b["statusLine"]["command"])
_, out1b = run_statusline_command(p1)
check("1 re-run renders our row exactly once", out1b.count("⚡ cache 1h") == 1, repr(out1b))

# ===== Case 2: merge into an EXISTING custom status line ====================
print("\n-- Case 2: merge (preserve existing line) --")
s2 = os.path.join(WORK, "s2"); os.makedirs(s2); p2 = os.path.join(s2, "settings.json")
# original command contains a single quote to stress escaping
json.dump({"statusLine":{"type":"command","command":"echo \"it's user line\"","padding":2}},
          open(p2,"w"))
r = install(p2)
check("2 installer ok", r.returncode == 0, r.stderr)
cfg = json.load(open(p2))
check("2 command now points to wrapper", "cache-assistant-statusline.sh" in cfg["statusLine"]["command"])
check("2 padding preserved", cfg["statusLine"].get("padding") == 2, cfg["statusLine"])
check("2 refreshInterval=1", cfg["statusLine"].get("refreshInterval") == 1)
sidecar = json.load(open(os.path.join(s2, "cache-assistant", "original-statusline.json")))
check("2 original preserved in sidecar", sidecar.get("command") == "echo \"it's user line\"", sidecar)
sl, out = run_statusline_command(p2)
check("2 BOTH rows render (user + ours)", ("it's user line" in out) and ("⚡ cache 1h" in out), repr(out))
check("2 a settings backup was created", any(f.startswith("settings.json.cache-assistant.bak") for f in os.listdir(s2)))

# ===== Case 3: idempotent re-run ===========================================
print("\n-- Case 3: idempotent re-run --")
r = install(p2)
cfg2 = json.load(open(p2))
check("3 still wrapped once", "cache-assistant-statusline.sh" in cfg2["statusLine"]["command"])
sl, out2 = run_statusline_command(p2)
check("3 user line appears exactly once", out2.count("it's user line") == 1, repr(out2))
check("3 our row appears exactly once", out2.count("⚡ cache 1h") == 1, repr(out2))
sidecar2 = json.load(open(os.path.join(s2, "cache-assistant", "original-statusline.json")))
check("3 sidecar original unchanged", sidecar2.get("command") == "echo \"it's user line\"")

# ===== Case 4: restore =====================================================
print("\n-- Case 4: restore original --")
r = install(p2, "--restore")
cfg3 = json.load(open(p2))
check("4 restored to original command", cfg3["statusLine"].get("command") == "echo \"it's user line\"", cfg3["statusLine"])

print("\n%d failures" % len(fails))
shutil.rmtree(STATE, ignore_errors=True); shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if fails else 0)
