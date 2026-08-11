#!/usr/bin/env python3
"""council_session.py -- conversation persistence. ASSERTS; run it, do not read it.

ISOLATION: every check runs against a TEMPORARY conversations root, monkeypatched onto the
module. Without that this suite would write into the operator's real ~/.council/sessions and
its own passes would depend on what was already there.
"""
import sys, tempfile, shutil, json
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import council_leader as cl
import council_session as cs

n = 0
def ck(cond, label):
    global n
    assert cond, f"FAILED: {label}"
    n += 1
    print(f"  [ok] {label}")

TMP = Path(tempfile.mkdtemp(prefix="convroot-"))
cs.CONVERSATIONS_ROOT = TMP

def rec(rounds=(), stop="final answer (no actions)", writes=None, claims=(), intent=None):
    return cl.TurnRecord("lead", tuple(rounds), "final text", stop, (), "", False,
                         writes or {"requested": (), "applied": (), "unapplied": (),
                                    "altered": ()},
                         claims, (), intent)

print("=== A. conversation creation ===")
cid = cs.new_conversation(Path("/tmp/wd"), "codex", cid="conv1")
d = cs.conversation_dir(cid)
ck(d.is_dir() and (d / "turns").is_dir(), "creates the conversation and its turns/ directory")
ck(cs.scratch_dir(cid).is_dir(), "creates ONE scratch for the whole conversation")
ck(cs.scratch_dir(cid) == cs.scratch_dir(cid), "and returns the SAME scratch every call")
ck(json.loads((d / "meta.json").read_text())["leader"] == "codex", "meta records the leader")
ck(cs.turn_numbers(cid) == [], "a fresh conversation has no completed turns")
for bad in ("", "../escape", "a/b", ".hidden"):
    try:
        cs.conversation_dir(bad); ok = False
    except ValueError:
        ok = True
    ck(ok, f"refuses an unusable conversation id {bad!r}")

print("=== B. turns are visible only when COMPLETE ===")
p1 = cs.persist_turn(cid, rec(rounds=[{"round": 0, "notes": ("READ a.py",)}]), "task one", "H1")
ck(p1.name == "0001" and p1.is_dir(), "the first turn lands as 0001/")
ck(not list((d / "turns").glob("*.partial")), "no .partial directory survives a completed write")
ck(cs.turn_numbers(cid) == [1], "and it is counted as completed")
(d / "turns" / "0002.partial").mkdir()
ck(cs.turn_numbers(cid) == [1], "a leftover .partial is NOT counted as a completed turn")
shutil.rmtree(d / "turns" / "0002.partial")
cs.persist_turn(cid, rec(), "task two", "H2")
ck(cs.turn_numbers(cid) == [1, 2], "turn numbers increment")
ck(cs.prior_handoff(cid) == "H2", "prior_handoff returns the MOST RECENT turn's handoff")

print("=== C. the spine is DERIVED, and reads notes only ===")
cid2 = cs.new_conversation(Path("/tmp/wd"), "codex", cid="conv2")
cs.persist_turn(cid2, rec(writes={"requested": ("a.py", "b.py"),
                                  "applied": ("/tmp/wd/a.py",),
                                  "unapplied": ({"path": "b.py", "note": "verdict=BLOCK"},),
                                  "altered": ()}), "first task line\nsecond line", "h")
spine = cs.conversation_spine(cid2)
ck("turn 1: first task line" in spine, "the spine names the turn and its task's FIRST line")
ck("applied=['a.py']" in spine, "it reports what was applied")
ck("UNAPPLIED=['b.py']" in spine, "and what was NOT -- the discrepancy is carried forward")
ck("second line" not in spine, "only the first task line is carried (bounded by construction)")
ck(cs.conversation_spine("conv1") and cs.conversation_spine(cid2) != cs.conversation_spine("conv1"),
   "spines are per-conversation")

print("=== D. the DECIDED ledger is verbatim, and supersession MARKS rather than deletes ===")
cid3 = cs.new_conversation(Path("/tmp/wd"), "codex", cid="conv3")
cs.persist_turn(cid3, rec(intent={"decided": "use approach X", "next": "", "open": "",
                                  "supersedes": []}), "t1", "h1")
cs.persist_turn(cid3, rec(intent={"decided": "use approach Y instead", "next": "", "open": "",
                                  "supersedes": ["turn 1 -- X needs a GPU we do not have"]}),
                "t2", "h2")
led = cs.decided_ledger(cid3)
ck([e["decided"] for e in led] == ["use approach X", "use approach Y instead"],
   "every DECIDED line is carried VERBATIM, oldest first")
ck(led[0]["superseded_by"] and led[0]["superseded_by"][0]["turn"] == 2,
   "a superseded decision is MARKED with the turn that reversed it")
ck(led[0]["decided"] == "use approach X",
   "and RETAINED, not deleted -- that it was reversed is itself information")
ck(not led[1]["superseded_by"], "the live decision carries no supersession")

print("=== E. bounds are announced, never silent ===")
cid4 = cs.new_conversation(Path("/tmp/wd"), "codex", cid="conv4")
for i in range(12):
    cs.persist_turn(cid4, rec(), f"task number {i} " + "x" * 200, "h")
small = cs.conversation_spine(cid4, cap=400)
ck("omitted for size" in small, "a truncated spine SAYS it was truncated")
ck(len(small.encode()) <= 400 + 60, "and stays near its cap (the notice is short and bounded)")
ck("turn 12" in small, "the NEWEST turns are the ones kept")

print("=== F. corrupt state does not take the conversation down ===")
cid5 = cs.new_conversation(Path("/tmp/wd"), "codex", cid="conv5")
cs.persist_turn(cid5, rec(intent={"decided": "d", "next": "", "open": "", "supersedes": []}),
                "t", "h")
(cs.conversation_dir(cid5) / "turns" / "0001" / "state.json").write_text("{ not json")
ck(cs.decided_ledger(cid5) == [], "an unreadable state.json is skipped, not raised")
ck(cs.conversation_spine(cid5) == "", "and the spine degrades to empty rather than crashing")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nALL {n} CHECKS PASSED")
