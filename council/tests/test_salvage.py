#!/usr/bin/env python3
"""Falsifier for council_advisor's timeout-salvage path.

# requires: none

WHAT THIS IS FOR. When the wrapper's cap kills a fire, the engine never reaches its single
write_log() call, so the review leaves no logs/ entry. The salvage path reads the events
sidecar the engine streamed into and rebuilds a PARTIAL report from the seat-rounds that
did finish. Everything below exercises that path against inputs it will really see -- a
truncated tail, a malformed record, a BLOCK, seats that never reported -- and section F
drives the PRODUCTION path itself: a real subprocess, a real cap, a real kill.

SCOPE. Section F substitutes a stub for the engine, so what it exercises is this module's
timeout handling, not the council's. The fd-inheritance leg is covered there too, since a
stub that could not write to the inherited descriptor would yield no partial at all.

EVERY TEST PATCHES PARTIAL_STORE. A suite that appended synthetic fires to the real store
would put invented records in an evidence file, which is the contamination this project
exists to prevent.

Run:  python3 council/tests/test_salvage.py      (exit 0 pass, 1 fail)
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import council_advisor as ca  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(cond: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(label)


def write_events(lines: list[str]) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "toolu_x.json.events"
    p.write_text("\n".join(lines) + "\n")
    return p


def rec(**kw) -> str:
    return json.dumps(kw)


# ---------------------------------------------------------------- A. read_events
# A torn final line is the CASE THIS EXISTS FOR: the process dies mid-write, so the last
# record is half a line of JSON. Everything before it must still be recovered.
p = write_events([
    rec(ev="run_started", voting=["codex", "kimi"], inspectors=["qwen"]),
    rec(ev="member_finished", member="codex", tier="voting", round=1, verdict="PASS"),
    '{"ev":"member_finished","member":"kim',        # torn tail
])
recs = ca.read_events(p)
check(len(recs) == 2, f"A1 torn tail: kept {len(recs)} of 2 intact records")
check(recs[0]["ev"] == "run_started", "A2 first record survives a torn tail")
check(ca.read_events(Path("/nonexistent/nope.events")) == [], "A3 missing file -> []")

p = write_events([rec(ev="run_started", voting=["a"], inspectors=[]), "", "[1,2,3]",
                  rec(ev="member_finished", member="a", tier="voting", round=1,
                      verdict="WARN")])
check(len(ca.read_events(p)) == 2, "A4 blank line and non-dict JSON are skipped")

# ---------------------------------------------------------------- B. summarise_partial
EV = [
    rec(ev="run_started", voting=["codex", "gemini", "kimi", "deepseek"],
        inspectors=["qwen", "mimo"]),
    rec(ev="member_finished", member="codex", tier="voting", round=1, verdict="WARN",
        member_text="round one text", duration_s=7.4),
    rec(ev="member_finished", member="gemini", tier="voting", round=1, verdict="PASS",
        member_text="", duration_s=2.1),
    # codex reports AGAIN in round 2 -- the later record must win, because round 2 is what
    # the council would have aggregated.
    rec(ev="member_finished", member="codex", tier="voting", round=2, verdict="BLOCK",
        member_text="round two text", duration_s=9.9),
]
s = ca.summarise_partial(ca.read_events(write_events(EV)))
check(s["expected"]["voting"] == ["codex", "gemini", "kimi", "deepseek"],
      "B1 expected voting roster recovered from run_started")
check(s["expected"]["inspector"] == ["qwen", "mimo"], "B2 expected inspectors recovered")
check(s["seats"]["codex"]["round"] == 2, "B3 later round supersedes earlier for a seat")
check(s["seats"]["codex"]["verdict"] == "BLOCK", "B4 latest verdict wins")
check(s["seats"]["codex"]["text"] == "round two text", "B5 latest text wins")
check(sorted(s["finished"][1]) == ["codex", "gemini"], "B6 round-1 finishers counted")
check("kimi" not in s["seats"] and "deepseek" not in s["seats"],
      "B7 seats that never reported are absent")

s2 = ca.summarise_partial(ca.read_events(write_events(EV + [
    rec(ev="member_corrected", member="gemini", tier="voting", was="UNPARSEABLE",
        verdict="PASS")])))
check(s2["seats"]["gemini"]["verdict"] == "PASS", "B8 member_corrected applied")
check(len(s2["corrected"]) == 1, "B9 correction recorded for the report")

# ONE DAMAGED RECORD MUST NOT COST THE WHOLE SALVAGE. A `round` that is a string or a dict
# is fatal to a bare int() conversion; a missing one is not. All three must leave the other
# seats' verdicts intact.
for bad in ('{"ev":"member_finished","member":"z","round":"two","verdict":"PASS"}',
            '{"ev":"member_finished","member":"z","round":{"a":1},"verdict":"PASS"}',
            '{"ev":"member_finished","member":"z","verdict":"PASS"}'):
    try:
        got = ca.summarise_partial(ca.read_events(write_events(EV + [bad])))
        ok = "codex" in got["seats"]
    except Exception as e:  # noqa: BLE001
        ok = False
        FAILS.append(f"B10 malformed round raised {type(e).__name__}: {e}")
    check(ok, f"B10 malformed round survives, other seats kept: {bad[:44]}")

# ---------------------------------------------------------------- C. format_partial
out = ca.format_partial(s, "Edit", "/tmp/x.py", 1500)
check("NOT A COMPLETE REVIEW" in out and "NOT A PASS" in out,
      "C1 report refuses to read as a pass")
check("kimi" in out and "deepseek" in out, "C2 seats that never reported are NAMED")
check("qwen" in out and "mimo" in out, "C3 missing inspectors are NAMED")
# The CLAIM, not the digits: asserting the percentages would fail on any rewording, which
# trains a reader to ignore the suite. The property is that a bias is stated and the slow
# seats are named.
check("slowest" in out and "kimi and deepseek" in out,
      "C4 selection bias is stated and names the slow seats")
check("round two text" in out, "C5 the surviving seat's own text is carried")
check("BLOCK" in out and "does NOT trigger the revert protocol" in out,
      "C6 a BLOCK is shown and explicitly downgraded")
# This BLOCK was cast in round 2, so the report must NOT claim the fire never had one.
check("never reached the round 2" not in out,
      "C7 a round-2 BLOCK is not described as untested-by-peers")
check("blocked in a round its peers DID see" in out,
      "C8 a round-2 BLOCK gets the incomplete-panel explanation")

s_r1 = ca.summarise_partial(ca.read_events(write_events([
    rec(ev="run_started", voting=["codex", "kimi"], inspectors=[]),
    rec(ev="member_finished", member="codex", tier="voting", round=1, verdict="BLOCK",
        member_text="early block")])))
out_r1 = ca.format_partial(s_r1, "Write", "/tmp/y.py", 1500)
check("never reached the round 2" in out_r1, "C9 a round-1 BLOCK is called untested")
check("blocked in a round its peers DID see" not in out_r1,
      "C10 a round-1 BLOCK does not get the round-2 wording")

# ---------------------------------------------------------------- D. record_partial
d = Path(tempfile.mkdtemp())
orig_store = ca.PARTIAL_STORE
try:
    ca.PARTIAL_STORE = d / "partials.jsonl"
    ca.record_partial(s, "Edit", "/tmp/x.py", "sess-1", 1500)
    ca.record_partial(s, "Edit", "/tmp/x.py", "sess-1", 1500)
    written = [json.loads(l) for l in ca.PARTIAL_STORE.read_text().splitlines() if l.strip()]
    check(len(written) == 2, "D1 appends one line per partial")
    check(written[0]["partial"] is True, "D2 entries are flagged partial")
    check(written[0]["verdicts"]["codex"] == "BLOCK", "D3 verdicts are recorded")
    check(written[0]["session_id"] == "sess-1", "D4 session is attributable")
    ca.PARTIAL_STORE = Path("/nonexistent-dir-xyz/partials.jsonl")
    ca.record_partial(s, "Edit", "/tmp/x.py", "sess-1", 1500)
    check(True, "D5 an unwritable store is swallowed rather than raising")
except Exception as e:  # noqa: BLE001
    FAILS.append(f"D5 record_partial raised: {type(e).__name__}: {e}")
    CHECKS += 1
finally:
    ca.PARTIAL_STORE = orig_store

# ---------------------------------------------------------------- E. sidecar naming
m = Path("/tmp/sess/pending-review/toolu_01.with.dots.json")
check(str(ca._events_path(m)).endswith(".json.events"), "E1 events path appends")
check(not ca._events_path(m).match("*.json"), "E2 .events is outside the *.json glob")
check(ca._beats_path(m) != ca._events_path(m), "E3 beats and events do not collide")
check(ca.ORPHAN_MIN_AGE_S == ca.FIRE_TIMEOUT_S,
      "E4 orphan age is derived from the fire cap, so they cannot drift")

# ---------------------------------------------------------------- F. the PRODUCTION path
# Sections A-E test the pieces. This one drives main() itself: a real subprocess that
# streams two events into the inherited descriptor and then hangs, a cap short enough to
# fire, and a real kill. It is the only check here that would catch the plumbing being
# wrong -- a descriptor that did not survive the fork, a salvage that ran after the sidecar
# was deleted, or a timeout branch that returned the bare notice instead of the partial.
d = Path(tempfile.mkdtemp())
stub = d / "stub_engine.py"
stub.write_text(
    "import sys, os, time\n"
    "fd = int(sys.argv[sys.argv.index('--events-fd') + 1])\n"
    "sys.stdin.read()\n"
    "def w(o): os.write(fd, (o + '\\n').encode())\n"
    "w('{\"ev\":\"run_started\",\"voting\":[\"codex\",\"kimi\"],\"inspectors\":[\"qwen\"]}')\n"
    "w('{\"ev\":\"member_finished\",\"member\":\"codex\",\"tier\":\"voting\",\"round\":1,"
    "\"verdict\":\"WARN\",\"member_text\":\"codex found a real defect\",\"duration_s\":1.0}')\n"
    "time.sleep(300)\n")

saved = (ca.WRAPPER, ca.FIRE_TIMEOUT_S, ca.EVIDENCE_STATE_ROOT, ca.PARTIAL_STORE,
         sys.stdin)
try:
    ca.WRAPPER = stub
    ca.FIRE_TIMEOUT_S = 3
    ca.EVIDENCE_STATE_ROOT = d / "state"
    ca.PARTIAL_STORE = d / "partials.jsonl"
    (d / "target.py").write_text("b\n")
    payload = {"session_id": "probe-sess", "cwd": str(d), "tool_name": "Edit",
               "tool_use_id": "toolu_probe1",
               "tool_input": {"file_path": str(d / "target.py"), "old_string": "a",
                              "new_string": "b"},
               "tool_response": {}}
    sys.stdin = io.StringIO(json.dumps(payload))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ca.main()
    ctx = json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]
    check("COUNCIL PARTIAL REVIEW" in ctx, "F1 timeout delivers a PARTIAL, not the bare notice")
    check("codex found a real defect" in ctx, "F2 the surviving seat's text crossed the fork")
    check("kimi" in ctx and "qwen" in ctx, "F3 seats that never reported are named")
    check("NOT A PASS" in ctx, "F4 the partial refuses to read as approval")
    check(ca.PARTIAL_STORE.exists(), "F5 the partial is recorded to its own store")
    # The sidecar must be gone: salvage reads it, then normal cleanup removes it.
    marker = ca.EVIDENCE_STATE_ROOT / "probe-sess" / "pending-review" / "toolu_probe1.json"
    check(not ca._events_path(marker).exists(), "F6 events sidecar cleaned up after salvage")
    check(not marker.exists(), "F7 marker cleared -- a timeout is a completed attempt")
except Exception as e:  # noqa: BLE001
    FAILS.append(f"F production path raised: {type(e).__name__}: {e}")
    CHECKS += 1
finally:
    (ca.WRAPPER, ca.FIRE_TIMEOUT_S, ca.EVIDENCE_STATE_ROOT, ca.PARTIAL_STORE,
     sys.stdin) = saved

# ---------------------------------------------------------------- G. stop_audit's salvage
# The Stop hook runs the same engine against the same caps, so it inherits the same loss when
# a fire is killed -- and it matters more there, because that hook's whole job is to check
# outward prose before a turn ends. This drives its production path with a stub engine.
import stop_audit as sa  # noqa: E402

d = Path(tempfile.mkdtemp())
stub = d / "stub_engine.py"
stub.write_text(
    "import sys, os, time\n"
    "fd = int(sys.argv[sys.argv.index('--events-fd') + 1])\n"
    "sys.stdin.read()\n"
    "def w(o): os.write(fd, (o + '\\n').encode())\n"
    "w('{\"ev\":\"run_started\",\"voting\":[\"codex\",\"kimi\"],\"inspectors\":[\"qwen\"]}')\n"
    "w('{\"ev\":\"member_finished\",\"member\":\"codex\",\"tier\":\"voting\",\"round\":1,"
    "\"verdict\":\"WARN\",\"member_text\":\"the prose overstates the result\","
    "\"duration_s\":1.0}')\n"
    "time.sleep(300)\n")

saved_sa = (sa.WRAPPER, ca.EVIDENCE_STATE_ROOT, ca.FIRE_TIMEOUT_S, ca.PARTIAL_STORE)
try:
    sa.WRAPPER = stub
    ca.EVIDENCE_STATE_ROOT = d / "state"
    ca.FIRE_TIMEOUT_S = 3          # stop_audit reads the cap off council_advisor
    ca.PARTIAL_STORE = d / "partials.jsonl"
    rc, out, err = sa.audit_one_block("Some outward prose to audit.", "sess-stop", str(d))
    check(rc == 2, f"G1 a timed-out audit returns 2 (got {rc})")
    check("COUNCIL PARTIAL REVIEW" in err, "G2 the partial review reaches the caller")
    check("the prose overstates the result" in err,
          "G3 the finished seat's own text is carried")
    check("timed out (>3s)" in err, "G4 the message quotes the shared cap, not a literal")
    check(ca.PARTIAL_STORE.exists(), "G5 the partial is recorded to its own store")
    # THE REGRESSION THIS SECTION EXISTS FOR. The cleanup used to ask
    # `isinstance(sys.exc_info()[1], TimeoutExpired)` inside `finally`, which is None once the
    # `except` has returned -- so it deleted the sidecar on exactly the path where that
    # sidecar is the only surviving record. It must still be on disk after a timeout.
    kept = list((d / "state").rglob("*.stopprobe.events"))
    check(len(kept) == 1, f"G6 the events sidecar SURVIVES a timeout (found {len(kept)})")
except Exception as e:  # noqa: BLE001
    FAILS.append(f"G stop_audit salvage raised: {type(e).__name__}: {e}")
    CHECKS += 1
finally:
    (sa.WRAPPER, ca.EVIDENCE_STATE_ROOT, ca.FIRE_TIMEOUT_S, ca.PARTIAL_STORE) = saved_sa

# ---------------------------------------------------------------- report
print(f"{CHECKS - len(FAILS)}/{CHECKS} checks passed")
for f in FAILS:
    print(f"  FAIL: {f}")
sys.exit(1 if FAILS else 0)
