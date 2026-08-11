#!/usr/bin/env python3
"""The operator-approval gate in council_leader.review_and_write.

WHY THIS EXISTS. `_atomic_write` fires inside review_and_write immediately after the
verdict check, so before the `approve` seam there was NO point between "the council
allowed it" and "the bytes are on disk" -- an approve-each permission mode (issue #8,
surfaced inside the GUI) could not be built on top of the function, only into it.

THE PROPERTY UNDER TEST is that the gate SUBTRACTS permission and can never add it: it is
consulted only after the council has already permitted a write, so a refusal branch can
never be reached by an approver, let alone overridden. Asserting only that "approve=False
blocks a write" would NOT establish that -- the discriminating cases are the ones where an
approver that would say YES is never consulted at all.

Re-run:  python3 council/tests/test_leader_approve.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import consult_council as cc  # noqa: E402
import council_leader as cl  # noqa: E402

LEADER = cc.Member("tester", cc.LEADER, "openrouter", "x/y", capabilities=cc.LEADER_CAPS)
VOTER = cc.Member("v", cc.VOTING, "openrouter", "x/y", capabilities=cc._DEFAULT_CAPS)

FAILS = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(name)


def review_returning(verdict):
    rc = {"PASS": 0, "WARN": 1, "BLOCK": 2}[verdict]
    def _review(pitch, target, wd, *, session_id="", transcript_path=""):
        return rc, f"VERDICT: {verdict}\nbody", ""
    return _review


def fresh():
    wd = Path(tempfile.mkdtemp())
    # The parent MUST exist: _resolve_write_target resolves strictly and _atomic_write
    # mkstemps into target.parent. Without this every case returns DENIED. Arms asserting
    # a specific verdict (applied / DECLINED / BLOCK) would fail loudly; the ones that
    # would pass for the WRONG reason are the `not path.exists()` and `not consulted`
    # arms, which is precisely where a vacuous green hides.
    (wd / "src").mkdir()
    return wd, wd / "src" / "f.py"


def run(approve, verdict="PASS", rel="src/f.py", leader=LEADER):
    wd, target = fresh()
    res = cl.review_and_write(leader, rel, "CONTENT", wd,
                              review=review_returning(verdict), approve=approve)
    return res, wd / rel


print("== baseline: approve=None reproduces the previous behaviour ==")
res, path = run(None)
check("applied", res["applied"] is True)
check("verdict is the council's", res["verdict"] == "PASS")
check("bytes on disk", path.read_text() == "CONTENT")

print("== approve returns True -> the write proceeds ==")
seen = {}
def yes(target, content, verdict, review_text):
    seen.update(target=str(target), content=content, verdict=verdict, review=review_text)
    return True
res, path = run(yes)
check("applied", res["applied"] is True)
check("bytes on disk", path.read_text() == "CONTENT")
check("approver saw the resolved target", seen.get("target", "").endswith("src/f.py"))
check("approver saw the exact content", seen.get("content") == "CONTENT")
check("approver saw the council verdict", seen.get("verdict") == "PASS")
check("approver saw the review text", "VERDICT: PASS" in (seen.get("review") or ""))

print("== approve returns False -> DECLINED and NOTHING is written ==")
res, path = run(lambda *a: False)
check("not applied", res["applied"] is False)
check("verdict DECLINED, distinct from the council's BLOCK", res["verdict"] == "DECLINED")
check("target does not exist at all", not path.exists())
check("reason names the operator", "declined" in (res.get("reason") or "").lower())

print("== approve RAISES -> fail closed, treated as refusal ==")
def boom(*a):
    raise RuntimeError("approver crashed")
res, path = run(boom)
check("not applied", res["applied"] is False)
check("verdict DECLINED", res["verdict"] == "DECLINED")
check("nothing written", not path.exists())
check("reason records the crash", "approver raised" in (res.get("reason") or ""))

print("== THE DISCRIMINATING CASES: an approver that says YES cannot rescue a refusal ==")
# Falsifier: if the gate sat ABOVE the refusal branches, these would apply the write.
consulted = []
def yes_and_note(*a):
    consulted.append(a)
    return True

res, path = run(yes_and_note, verdict="BLOCK")
check("BLOCK stays BLOCK", res["verdict"] == "BLOCK" and res["applied"] is False)
check("nothing written", not path.exists())
check("approver was NEVER consulted on a BLOCK", not consulted)

consulted.clear()
res, path = run(yes_and_note, rel="../escape.py")
check("jail denial stays DENIED", res["verdict"] == "DENIED" and res["applied"] is False)
check("approver was NEVER consulted on a jail denial", not consulted)

consulted.clear()
res, path = run(yes_and_note, leader=VOTER)
check("non-leader stays DENIED", res["verdict"] == "DENIED" and res["applied"] is False)
check("approver was NEVER consulted for a non-mutating caller", not consulted)

consulted.clear()
wd, _ = fresh()
res = cl.review_and_write(LEADER, "src/f.py", "x" * (cl.LEADER_WRITE_MAX_BYTES + 1), wd,
                          review=review_returning("PASS"), approve=yes_and_note)
check("oversize stays DENIED", res["verdict"] == "DENIED" and res["applied"] is False)
check("approver was NEVER consulted on oversize", not consulted)

print("== a WARN the council permitted is still gateable ==")
res, path = run(lambda *a: False, verdict="WARN")
check("WARN + decline -> DECLINED, no write", res["verdict"] == "DECLINED" and not path.exists())
res, path = run(lambda *a: True, verdict="WARN")
check("WARN + approve -> applied", res["applied"] is True and path.read_text() == "CONTENT")

print()
print(f"FAILURES: {FAILS if FAILS else 'none'}")
sys.exit(1 if FAILS else 0)
