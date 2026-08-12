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
import subprocess
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

# ---------------------------------------------------------------- H. per-round rows + log frame
# Covers the two features added for the watcher: seat_rounds (round 1 and round 2 kept apart
# rather than one overwriting the other) and the log-backed final frame that replaced a re-read
# of a sidecar which is already deleted by the time it is wanted.
try:
    import council_watch as cw  # noqa: E402

    s = ca.summarise_partial([
        {"ev": "run_started", "voting": ["a", "b"], "inspectors": ["z"]},
        {"ev": "member_finished", "member": "a", "round": 1, "verdict": "PASS",
         "duration_s": 1.0},
        {"ev": "member_finished", "member": "b", "round": 1, "verdict": "BLOCK",
         "duration_s": 2.0},
        {"ev": "member_finished", "member": "a", "round": 2, "verdict": "WARN",
         "duration_s": 3.0},
        {"ev": "member_finished", "member": "stray", "verdict": "PASS"},
    ])
    # THE BUG THIS PINS: with a single name-keyed map, a's round-2 WARN destroyed its round-1
    # PASS, so the r1 verdicts could not be shown at all once r2 landed.
    check(s["seat_rounds"][1]["a"]["verdict"] == "PASS", "H1 round 1 verdict survives round 2")
    check(s["seat_rounds"][2]["a"]["verdict"] == "WARN", "H2 round 2 recorded separately")
    check(s["seats"]["a"]["verdict"] == "WARN",
          "H3 the collapsed map still aggregates to the latest round")
    check("b" not in s["seat_rounds"].get(2, {}),
          "H4 a seat that has not reported in r2 is absent from r2, not carried over")
    check(s["seat_rounds"][0]["stray"]["verdict"] == "PASS",
          "H5 a record with no round lands in the round-0 bucket rather than vanishing")

    # A correction must reach the per-round view too, or the tally and the grid disagree.
    s2 = ca.summarise_partial([
        {"ev": "member_finished", "member": "a", "round": 2, "verdict": "PASS"},
        {"ev": "member_corrected", "member": "a", "round": 2, "verdict": "BLOCK", "was": "PASS"},
    ])
    check(s2["seat_rounds"][2]["a"]["verdict"] == "BLOCK", "H6 correction lands on its round")
    check(s2["seats"]["a"]["verdict"] == "BLOCK", "H7 correction lands on the aggregate")

    # ALIASING. `seats` is latest-wins, so for a two-round seat seats[m] would BE round 2's
    # record unless copied. A correction naming round 1 must then leave round 2 alone; sharing
    # the object makes the aggregate write bleed into round 2 and this check fails.
    s3 = ca.summarise_partial([
        {"ev": "member_finished", "member": "a", "round": 1, "verdict": "PASS"},
        {"ev": "member_finished", "member": "a", "round": 2, "verdict": "WARN"},
        {"ev": "member_corrected", "member": "a", "round": 1, "verdict": "BLOCK", "was": "PASS"},
    ])
    check(s3["seat_rounds"][1]["a"]["verdict"] == "BLOCK",
          "H7a a round-1 correction reaches round 1")
    check(s3["seat_rounds"][2]["a"]["verdict"] == "WARN",
          "H7b a round-1 correction does NOT bleed into round 2 through a shared object")

    rend = cw.Renderer(colour=False)
    lines = cw._seat_lines(rend, s)
    check(sum(1 for ln in lines if "vote r1" in ln) == 1, "H8 exactly one r1 row")
    check(sum(1 for ln in lines if "vote r2" in ln) == 1, "H9 exactly one r2 row")
    check(any("rnd ?" in ln for ln in lines), "H10 the round-0 bucket is rendered when non-empty")

    # The log-backed final frame, against a log written in the shape the engine writes.
    d = Path(tempfile.mkdtemp())
    (d / "logs" / "2026-08-12").mkdir(parents=True)
    log = d / "logs" / "2026-08-12" / "20260812T120000Z-aaaaaaaa.json"
    log.write_text(json.dumps({
        "timestamp": "2026-08-12T12:00:00+00:00", "session_id": "S", "tool_name": "Edit",
        "target_path": "/f.py", "final_verdict": "WARN",
        "roster": {"members": [{"name": "a", "tier": "voting"},
                               {"name": "gone", "tier": "voting"},
                               {"name": "z", "tier": "inspector"}]},
        "round1": [{"role": "a", "verdict": "PASS", "duration_s": 1.0}],
        "members": [{"role": "a", "verdict": "WARN", "duration_s": 2.0}],
        "shadow": [{"role": "z", "verdict": "PASS", "duration_s": 3.0}],
    }))
    # A SIBLING LOG, later, same session/tool/target -- the NORMAL case, since a session edits
    # the same file repeatedly. Picking the newest match returns this one instead of the fire's
    # own, so without this fixture the earliest-wins rule is untested.
    sib = d / "logs" / "2026-08-12" / "20260812T121500Z-bbbbbbbb.json"
    sib.write_text(json.dumps({
        "timestamp": "2026-08-12T12:15:00+00:00", "session_id": "S", "tool_name": "Edit",
        "target_path": "/f.py", "final_verdict": "BLOCK",
        "roster": {"members": [{"name": "a", "tier": "voting"}]},
        "round1": [{"role": "a", "verdict": "BLOCK"}],
        "members": [{"role": "a", "verdict": "BLOCK"}], "shadow": [],
    }))
    # FILENAME SAYS LATER, TIMESTAMP SAYS EARLIER. Only the timestamp guard can reject this; the
    # filename prefix filter cannot, which is what makes that guard's test discriminating.
    skew = d / "logs" / "2026-08-12" / "20260812T125959Z-cccccccc.json"
    skew.write_text(json.dumps({
        "timestamp": "2026-08-12T11:00:00+00:00", "session_id": "T", "tool_name": "Edit",
        "target_path": "/g.py", "final_verdict": "PASS",
        "roster": {"members": [{"name": "a", "tier": "voting"}]},
        "round1": [{"role": "a", "verdict": "PASS"}],
        "members": [{"role": "a", "verdict": "PASS"}], "shadow": [],
    }))
    saved_root = getattr(ca, "COUNCIL_ROOT")
    try:
        ca.COUNCIL_ROOT = d
        marker = {"started": "2026-08-12T11:59:00+00:00", "session_id": "S",
                  "tool_name": "Edit", "target_path": "/f.py"}
        found = cw._find_log_for(marker)
        check(found is not None and found.name == log.name,
              "H11 the EARLIEST matching log is the fire's own, not a later sibling")
        # A KILLED FIRE WRITES NO LOG. Without the newer-than-the-marker guard, an earlier log
        # from the same session and file would be presented as this fire's final result.
        late = dict(marker, started="2026-08-12T12:20:00+00:00")
        check(cw._find_log_for(late) is None,
              "H12 no log at or after the marker means None, never an older one")
        skewed = {"started": "2026-08-12T12:30:00+00:00", "session_id": "T",
                  "tool_name": "Edit", "target_path": "/g.py"}
        check(cw._find_log_for(skewed) is None,
              "H12a a log whose FILENAME is later but whose timestamp is earlier is rejected")

        # EXACT MATCHING BY tool_use_id. The discriminating shape: the id'd log is the LATER of
        # the two, so the earliest-wins heuristic would return the sibling. Only an exact match
        # can return it, and only a real skip of the mismatched id can keep the sibling from
        # winning -- so these two checks fail if either arm is removed.
        idlog = d / "logs" / "2026-08-12" / "20260812T123000Z-dddddddd.json"
        idlog.write_text(json.dumps({
            "timestamp": "2026-08-12T12:30:00+00:00", "session_id": "S", "tool_name": "Edit",
            "target_path": "/f.py", "tool_use_id": "toolu_WANTED", "final_verdict": "PASS",
            "roster": {"members": [{"name": "a", "tier": "voting"}]},
            "round1": [{"role": "a", "verdict": "PASS"}],
            "members": [{"role": "a", "verdict": "PASS"}], "shadow": [],
        }))
        # The sibling gets an id too, so the mismatch arm is what must exclude it. Earlier
        # filename than the wanted log, so it wins on the heuristic if it is not skipped.
        sibid = d / "logs" / "2026-08-12" / "20260812T122000Z-eeeeeeee.json"
        sibid.write_text(json.dumps({
            "timestamp": "2026-08-12T12:20:00+00:00", "session_id": "S", "tool_name": "Edit",
            "target_path": "/f.py", "tool_use_id": "toolu_OTHER", "final_verdict": "BLOCK",
            "roster": {"members": [{"name": "a", "tier": "voting"}]},
            "round1": [{"role": "a", "verdict": "BLOCK"}],
            "members": [{"role": "a", "verdict": "BLOCK"}], "shadow": [],
        }))
        idmarker = {"started": "2026-08-12T12:19:00+00:00", "session_id": "S",
                    "tool_name": "Edit", "target_path": "/f.py",
                    "tool_use_id": "toolu_WANTED"}
        got = cw._find_log_for(idmarker)
        check(got is not None and got.name == idlog.name,
              "H18 tool_use_id matches its own log even when an earlier sibling exists")
        # A marker whose id matches NO log must not fall back onto an id'd sibling: the fire it
        # names has not written a log, and the honest answer is None rather than a stranger's.
        missing = dict(idmarker, tool_use_id="toolu_ABSENT")
        got2 = cw._find_log_for(missing)
        check(got2 is None or str(json.loads(got2.read_text()).get("tool_use_id") or "") == "",
              "H19 an unmatched tool_use_id never returns a log carrying a DIFFERENT id")
        summary = cw._summary_from_log(log)
        check(summary["final_verdict"] == "WARN", "H13 the final verdict is carried")
        check(summary["seat_rounds"][1]["a"]["verdict"] == "PASS"
              and summary["seat_rounds"][2]["a"]["verdict"] == "WARN",
              "H14 round1 and members map to rounds 1 and 2")
        check(set(summary["seats"]) == {"a", "z"},
              "H15 the tally map is r2 + inspectors, so no voter is counted twice")
        check("gone" in summary["expected"]["voting"],
              "H16 a seat that never reported still occupies a cell (roster, not results)")
        rows = cw._seat_lines(rend, summary)
        check(any("gone" in ln and "-" in ln for ln in rows),
              "H17 the absent seat renders as no-report rather than being dropped")
    finally:
        ca.COUNCIL_ROOT = saved_root
except Exception as e:  # noqa: BLE001
    FAILS.append(f"H per-round/log frame raised: {type(e).__name__}: {e}")
    CHECKS += 1

# ------------------------------------------------------- I. the effort-table invariant
# _check_effort_tables runs at IMPORT and is what makes MODES load-bearing. Each of its four
# raises is driven here by swapping a table and calling it directly -- the tables are module
# globals the function reads, so patching them exercises the same code path the import does
# without needing a mutated copy of the module on disk.
try:
    import consult_council as cc  # noqa: E402

    def raises(label: str) -> None:
        try:
            cc._check_effort_tables()
        except RuntimeError:
            check(True, label)
        except Exception as exc:  # noqa: BLE001
            check(False, f"{label} (raised {type(exc).__name__}, wanted RuntimeError)")
        else:
            check(False, f"{label} (no raise)")

    # DOES tool_use_id ACTUALLY REACH THE LOG? A parameter with a default lets the whole chain
    # compile and run while writing nothing, so this calls the real write_log and reads the file
    # back rather than trusting the signature.
    saved_logs = cc.LOGS_ROOT
    try:
        cc.LOGS_ROOT = Path(tempfile.mkdtemp())
        written = cc.write_log("layer1", "Edit", "/f.py", "pitch",
                               [{"role": "codex", "verdict": "PASS"}], "PASS",
                               session_id="S", tool_use_id="toolu_ARRIVED")
        got = json.loads(written.read_text())
        check(got.get("tool_use_id") == "toolu_ARRIVED",
              "I0 tool_use_id reaches the log entry, not just the signature")
        check(got.get("session_id") == "S", "I0a session_id still recorded alongside it")
        # THE FLAG IS A CONTRACT BETWEEN TWO FILES: the advisor sends a spelling and the engine
        # declares one. If they diverge the plumbing is broken, and nothing else in the suite
        # would notice, since each file is individually valid.
        # CHECKED BY SOURCE, NOT BY RUNNING THE ENGINE. Invoking consult_council.py with valid
        # arguments STARTS A REAL COUNCIL FIRE: it does not exit, it fans out to the bench and
        # spends real API calls, and a --help run cannot substitute because argparse fires the
        # help action before it reports unrecognised arguments, returning 0 for a bogus flag too.
        # WHAT THIS DOES NOT ESTABLISH: that argparse is wired to args.tool_use_id or that the
        # advisor's branch is reached. It establishes only that the two spellings agree, which
        # is the half that fails silently; I0 covers the value reaching the log.
        root = Path(__file__).resolve().parent.parent
        engine_src = (root / "consult_council.py").read_text()
        advisor_src = (root / "council_advisor.py").read_text()
        check('add_argument("--tool-use-id"' in engine_src,
              "I0b the engine declares the --tool-use-id option")
        check('"--tool-use-id"' in advisor_src,
              "I0c the advisor sends that same spelling")
    finally:
        cc.LOGS_ROOT = saved_logs

    saved = (cc.MODES, cc.CORE_EFFORT, cc.OPENROUTER_EFFORT, cc._FULL_EFFORT)
    try:
        check(cc._check_effort_tables() is None, "I1 the shipped tables satisfy the invariant")

        cc.OPENROUTER_EFFORT = {"fast": "minimal", "default": "low"}
        raises("I2 a mode with no OPENROUTER_EFFORT value is refused")

        cc.OPENROUTER_EFFORT = saved[2]
        cc.CORE_EFFORT = {"fast": dict(saved[1]["fast"])}
        raises("I3 CORE_EFFORT missing a non-deep mode is refused")

        cc.CORE_EFFORT = {"fast": {"codex": "none", "gemini": "low"},
                          "default": {"codex": "low"}}
        raises("I4 CORE_EFFORT modes covering different members are refused")

        cc.CORE_EFFORT = saved[1]
        cc._FULL_EFFORT = {"codex": lambda: "high"}
        raises("I5 _FULL_EFFORT missing a member DEEP would ask for is refused")
    finally:
        (cc.MODES, cc.CORE_EFFORT, cc.OPENROUTER_EFFORT, cc._FULL_EFFORT) = saved
    check(cc._check_effort_tables() is None, "I6 the tables are restored after the mutations")
    # MODES IS ACTUALLY READ, which is the whole point -- adding a name to it with no effort
    # value anywhere must fail rather than wait for a fire to KeyError.
    try:
        cc.MODES = tuple(saved[0]) + ("ludicrous",)
        raises("I7 a mode added to MODES with no effort value is refused")
    finally:
        cc.MODES = saved[0]
except Exception as e:  # noqa: BLE001
    FAILS.append(f"I effort-table invariant raised: {type(e).__name__}: {e}")
    CHECKS += 1

# ---------------------------------------------------------------- report
print(f"{CHECKS - len(FAILS)}/{CHECKS} checks passed")
for f in FAILS:
    print(f"  FAIL: {f}")
sys.exit(1 if FAILS else 0)
