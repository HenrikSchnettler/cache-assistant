#!/usr/bin/env python3
"""Offline tests for the keep-cache-alive planner. The daemon/`claude --resume`
loop is gone: the skill now drives the built-in `/loop` command, and this script
only detects the current tier and emits the matching `/loop` command. Tests cover
the seconds->/loop-duration mapping, the per-tier command (interval + marker),
the human-readable plan, `--command-only`, and the no-tier error path."""
import json, os, sys, time, tempfile, shutil, subprocess, importlib.util

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

def write_tx(name, tier):
    p = os.path.join(WORK, name)
    with open(p, "w") as fh:
        fh.write(turn(tier) + "\n")
    return p

def run(tx, *extra):
    return subprocess.run([sys.executable, KA, "--session", "ksess", "--transcript", tx, *extra],
                          capture_output=True, text=True, env=env)

# --- Import the module directly to unit-test the pure mapping helpers ----------
spec = importlib.util.spec_from_file_location("keepalive", KA)
ka = importlib.util.module_from_spec(spec); spec.loader.exec_module(ka)

check("K1 loop_interval maps 240s -> 4m", ka.loop_interval(240) == "4m", ka.loop_interval(240))
check("K2 loop_interval maps 1800s -> 30m", ka.loop_interval(1800) == "30m", ka.loop_interval(1800))
check("K3 loop_interval rounds sub-minute up to minutes (matches /loop)",
      ka.loop_interval(90) == "2m", ka.loop_interval(90))
check("K4 ping prompt carries the keepalive marker",
      ka.PING_PROMPT.startswith("[cache-assistant:keepalive]"), ka.PING_PROMPT)
check("K5 1h command uses /loop 30m + marker",
      ka.loop_command("1h").startswith("/loop 30m ") and "[cache-assistant:keepalive]" in ka.loop_command("1h"),
      ka.loop_command("1h"))
check("K6 5m command uses /loop 4m", ka.loop_command("5m").startswith("/loop 4m "), ka.loop_command("5m"))

# --- Invariant: every emitted interval must be cron-safe, because /loop schedules
# it as a cron job. Whole minutes, dividing 60 evenly (even */N gaps, no wrap),
# and short enough that cron's <=10% "fire late" jitter still lands before the
# TTL. This is the guard that would have caught the old 3300s/55m value. (Both
# tiers are sub-hour, which the jitter bound below already forces.)
for _t, _iv in ka.cache_core.TIER_PING_INTERVAL.items():
    _ttl = ka.cache_core.TIER_TTL[_t]
    check("INV-%s interval is whole minutes" % _t, _iv % 60 == 0, _iv)
    check("INV-%s minutes divide 60 evenly (even cron gaps)" % _t,
          0 < _iv // 60 <= 60 and 60 % (_iv // 60) == 0, _iv)
    check("INV-%s survives <=10%% jitter under the TTL" % _t, _iv * 1.1 < _ttl, (_iv, _ttl))

# --- Subprocess: human-readable plan on the 1h tier ---------------------------
tx1h = write_tx("t1h.jsonl", "1h")
r = run(tx1h)
check("K7 plan exits cleanly on a detected tier", r.returncode == 0, r.stderr)
check("K8 plan reports current tier 1h", "current tier: 1h" in r.stdout, r.stdout)
check("K9 plan prints the /loop 30m command", "/loop 30m" in r.stdout, r.stdout)
check("K10 plan shows the 1800s margin below the 3600s TTL",
      "1800s margin below the 3600s TTL" in r.stdout, r.stdout)

# --- Subprocess: --command-only is a single ready-to-run /loop line ------------
r2 = run(tx1h, "--command-only")
lines = [ln for ln in r2.stdout.splitlines() if ln.strip()]
check("K11 --command-only prints exactly one line", len(lines) == 1, r2.stdout)
check("K12 --command-only line is the /loop command with marker",
      lines and lines[0].startswith("/loop 30m ") and "[cache-assistant:keepalive]" in lines[0], r2.stdout)

# --- Subprocess: 5m tier picks /loop 4m ---------------------------------------
tx5m = write_tx("t5m.jsonl", "5m")
r3 = run(tx5m, "--command-only")
check("K13 5m tier picks /loop 4m", r3.stdout.strip().startswith("/loop 4m "), r3.stdout)

# --- Subprocess: empty transcript -> no tier -> error (exit 2) -----------------
empty = os.path.join(WORK, "empty.jsonl"); open(empty, "w").close()
r4 = run(empty)
check("K14 empty transcript errors with exit 2", r4.returncode == 2, (r4.returncode, r4.stdout))
check("K15 error explains no tier detected", "no cache tier detected" in r4.stdout, r4.stdout)

print("\n%d failures" % len(fails))
shutil.rmtree(STATE, ignore_errors=True); shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if fails else 0)
