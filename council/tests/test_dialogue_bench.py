#!/usr/bin/env python3
"""Local, API-free regression for the full-bench dialogue round-table.

Validates the 2026-07-21 upgrade (the user: "dialogs should be all council
members ... default to the entire bench, not just voters", and "the delay is
fine and worthwhile for the quality") with the HYBRID convergence/consensus
semantics the council converged on:
  - the round-table convenes the ENTIRE bench (voting + inspectors), derived
    from the registry so future members join automatically;
  - CONVERGENCE gates on the FULL live roster: an open question from ANY member
    (voter OR inspector) keeps the round-table going, so full-bench
    participation is substantive;
  - CONSENSUS, once converged, is computed over the VOTING members ONLY -- an
    inspector never changes the PASS/WARN/BLOCK label;
  - legacy threads (no `voting` key) treat every member as voting; an explicit
    empty `voting` list is honored (not mistaken for legacy).

No network / API calls: assess() and the roster helpers are pure functions over
synthetic thread dicts.

FIXTURES ARE REGISTRY-COMPLETE, and the reason is a defect this file used to
have. The verdict fixtures indexed one tier POSITIONALLY -- V[0..2] or INS[0..2],
depending on the scenario -- while the registry yields SIX of each (measured
2026-07-30: voting_members() and inspector_members() both return 6). Whichever
tier a scenario indexed that way left ITS members past index 2 with no record at
all, and assess() reads a member with no record as `pending`, so nothing could
converge. Precisely, and NOT as a blanket over all members: T1 and T4 covered
every voter via voters() and missed INS[3..5]; T2, T3 and T5 covered every
inspector and missed V[3..5]. That broke T1/T2/T5 loudly (5 red checks, measured)
and, far worse, made T3 and T4 VOID: both assert "NOT converged", and they would
have reported not-converged even with their deliberating member removed, because
the uncovered members held convergence open by themselves.
WHY the positional indexing was there is NOT established, and nothing here needs
it to be.
So every scenario now starts from `full()` -- every member terminal -- and
overrides ONLY the members the scenario is about. T3 and T4 additionally assert
a CONTROL: the same roster WITHOUT the override DOES converge, which is what
makes them tests of the question-gating rule rather than of a gap in the
fixture. Run:
    python3 council/tests/test_dialogue_bench.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import council_dialogue as cdl  # noqa: E402
import consult_council as cc  # noqa: E402

FAILS = []

# Registry-derived expectations (not hardcoded names -> tracks future members).
V = [m.name for m in cc.voting_members()]
INS = [m.name for m in cc.inspector_members()]
ALL_ = list(cc.BENCH_MEMBERS)
KEYS = ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY")


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def rec(role, verdict, questions=None, errored=False):
    return {"role": role, "text": f"VERDICT: {verdict}", "stderr": "",
            "duration_s": 1.0, "errored": errored, "verdict": verdict,
            "questions": list(questions or [])}


def mkthread(members, voting, round_members):
    t = {"id": "synthetic", "members": list(members), "dropped": [],
         "proposal": "p", "rounds": [{"n": 1, "claude": "p", "members": round_members}]}
    if voting is not None:
        t["voting"] = list(voting)
    return t


def voters(v):
    return {r: rec(r, v) for r in V}


def full(v="PASS", i="PASS", over=None):
    """Every member of the CURRENT registry with a terminal record.

    This is the fixture baseline: because convergence gates on the full live
    roster, any member left out reads as `pending` and holds the round-table
    open on its own. Starting from full() means a test's own override is the
    only thing that can change the outcome.
    """
    rm = {r: rec(r, v) for r in V}
    rm.update({r: rec(r, i) for r in INS})
    rm.update(over or {})
    return rm


print(f"== registry: voting={V} inspectors={INS} ==")
check("bench is voting-first, all tiers", ALL_ == V + INS)
check("fixture baseline covers every bench member", set(full()) == set(ALL_))

print("== build_bench_roster (keys present -> full bench) ==")
_saved = {k: os.environ.get(k) for k in KEYS}
for k in KEYS:
    os.environ[k] = _saved[k] or "present-dummy"
try:
    members, voting = cdl.build_bench_roster()
finally:
    for k, val in _saved.items():
        if val is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = val
check("members == full bench", members == ALL_)
check("voting == registry voting members", voting == V)

print("== build_bench_roster (keys absent -> only key-less transports survive) ==")
_popped = {k: os.environ.pop(k, None) for k in KEYS}
try:
    m2, v2 = cdl.build_bench_roster()
finally:
    for k, val in _popped.items():
        if val is not None:
            os.environ[k] = val
# DERIVE the expected survivors; do not name them. This pair used to assert
# m2 == ["codex"], which encoded one roster rather than the behaviour, and went red the
# moment the operator's roster.json changed -- which the GUI now makes routine. What
# build_bench_roster actually promises is that a member whose transport needs an env key
# is dropped when that key is absent, so that is what gets asserted. The check stays
# discriminating: if key-gating broke and nothing were dropped, m2 would equal the full
# bench and this would fail -- except on a roster where every seat is key-less, in which
# case nothing SHOULD drop and the checks pass correctly. Which case this run exercised is
# reported (not asserted) below.
def _needs_key(n):
    # None-tolerant, mirroring build_bench_roster, which skips a name it cannot resolve
    # rather than raising. A name in the fixture that is absent from the active registry
    # must not turn this check into a crash.
    m = cc.member_by_name(n)
    return bool(m and cc.TRANSPORT_KEY_ENV.get(m.transport))


keyless = [n for n in ALL_ if not _needs_key(n)]
keyless_voting = [n for n in V if not _needs_key(n)]
check("survivors are exactly the key-less transports", m2 == keyless)
check("voting survivors are exactly the key-less voting transports", v2 == keyless_voting)
# REPORTED, not asserted. On a roster where every seat is key-less (codex + claude, say)
# nothing SHOULD be dropped and the checks above pass by correctly asserting that. Making
# "someone was dropped" a requirement would fail a legitimate roster -- the very coupling
# this rewrite removes. The line prints so a reader can see whether the run exercised a
# drop or only the trivial case.
print(f"   (this roster: {len(ALL_) - len(keyless)} of {len(ALL_)} seats key-gated)")

print("== T1: voters all PASS; inspectors TERMINAL (one BLOCK) -> PASS, BLOCK ignored ==")
rm = full(over={INS[0]: rec(INS[0], "BLOCK"), INS[-1]: rec(INS[-1], "WARN")})
a = cdl.assess(mkthread(ALL_, V, rm))
check("converged (all live terminal)", a["converged"] is True)
check("consensus PASS (inspector BLOCK does NOT flip it)", a["consensus"] == "PASS")
check("voting_live == the voters", set(a["voting_live"]) == set(V))

print("== T2: one voter WARNs; everyone else terminal PASS -> consensus WARN ==")
a = cdl.assess(mkthread(ALL_, V, full(over={V[-1]: rec(V[-1], "WARN")})))
check("converged", a["converged"] is True)
check("consensus WARN", a["consensus"] == "WARN")

print("== T3: a VOTER is DELIBERATING with a question -> NOT converged ==")
# CONTROL FIRST: the identical roster without the question DOES converge, so the
# assertion below is about the question and not about a hole in the fixture.
check("control: same roster, no question -> converged",
      cdl.assess(mkthread(ALL_, V, full()))["converged"] is True)
rm = full(over={V[-1]: rec(V[-1], "DELIBERATING", ["please clarify"])})
a = cdl.assess(mkthread(ALL_, V, rm))
check("NOT converged (voter pending)", a["converged"] is False)
check("consensus PENDING", a["consensus"] == "PENDING")

print("== T4 (HYBRID): an INSPECTOR question BLOCKS convergence; voters terminal ==")
rm = full(over={INS[-1]: rec(INS[-1], "DELIBERATING",
                             ["a question that MUST gate"])})
a = cdl.assess(mkthread(ALL_, V, rm))
check("NOT converged (inspector question gates)", a["converged"] is False)
check("consensus PENDING", a["consensus"] == "PENDING")
# The control for T4 is T3's control: same full() roster, converges.

print("== T5: a voter BLOCKs -> consensus BLOCK ==")
rm = full(over={V[1]: rec(V[1], "WARN"), V[-1]: rec(V[-1], "BLOCK")})
a = cdl.assess(mkthread(ALL_, V, rm))
check("converged", a["converged"] is True)
check("consensus BLOCK (from a voter)", a["consensus"] == "BLOCK")

print("== T6: LEGACY thread (no 'voting' key, voters-only members) ==")
a = cdl.assess(mkthread(V, None, voters("PASS")))
check("legacy converged", a["converged"] is True)
check("legacy consensus PASS", a["consensus"] == "PASS")
check("legacy voting_live == all members", set(a["voting_live"]) == set(V))

print("== T7: explicit EMPTY voting list is honored (the is-not-None fix) ==")
check("voting_set(voting=[]) is empty, NOT a legacy fallback",
      cdl.voting_set(mkthread(ALL_, [], {})) == set())
a = cdl.assess(mkthread(ALL_, [], full()))
check("no voters -> NOT converged", a["converged"] is False)

print("== per-member 'voting' flag + voting_set helper ==")
a = cdl.assess(mkthread(ALL_, V, full()))
check("a voter is flagged voting", a["per"][V[0]]["voting"] is True)
check("an inspector is flagged non-voting", a["per"][INS[0]]["voting"] is False)
check("voting_set uses thread['voting']", cdl.voting_set(mkthread(ALL_, V, {})) == set(V))
check("voting_set legacy fallback = all members",
      cdl.voting_set(mkthread(V, None, {})) == set(V))

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("ALL GREEN")
sys.exit(0)
