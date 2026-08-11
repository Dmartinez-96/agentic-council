#!/usr/bin/env python3
"""Durable regression for build_exfil_context (the web-fetch exfil-brake corpus).

Tests the PRODUCTION symbol cc.build_exfil_context against cc._exfil_span (not a
re-implementation -- rule 3). Proves, with discriminating cases (rule 6 falsifiers
stated inline), that:
  - the two originally-omitted blocks (assistant transcript, standing rules/CLAUDE.md)
    are now in the corpus, so a >=WEB_EXFIL_SPAN verbatim span from either is CAUGHT;
  - the OLD 3-block corpus (the pre-fix behaviour, reproduced by the default args) did
    NOT catch those spans -- the gap was real;
  - conclusion_block is in the INSPECTOR corpus (6-block) but NOT the VOTING corpus
    (5-block): voting members request in round 1 before the conclusion exists, inspectors
    request after seeing it, so the split is timing-correct;
  - a benign URL (no verbatim prompt span) is never falsely flagged.

SCOPE: this exercises the HELPER for the argument sets the production call sites pass;
it does NOT execute main()'s call sites, so it would still pass if a call site were
reverted to an inline join. That wiring is verified separately (pyflakes; the
test_tooling_e2e.py / test_retrieval.py regressions; and the call-site grep showing
consult_council.py builds exfil_context / insp_exfil_context from these blocks).

Self-contained, no API, gitignored (survives compaction). Run:
  python3 council/tests/test_exfil_context.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import consult_council as cc  # noqa: E402

SPAN = cc.WEB_EXFIL_SPAN
assert SPAN == 64, f"test assumes WEB_EXFIL_SPAN==64, found {SPAN}"

# Distinct >=SPAN secrets, each living in exactly ONE block.
S_EV   = "EVIDENCE-SECRET-"   + "a" * (SPAN + 5)
S_DIR  = "DIRECTIVE-SECRET-"  + "b" * (SPAN + 5)
S_PIT  = "PITCH-SECRET-"      + "c" * (SPAN + 5)
S_ASST = "ASSISTANT-SECRET-"  + "d" * (SPAN + 5)
S_STD  = "STANDING-SECRET-"   + "e" * (SPAN + 5)
S_CONCL = "CONCLUSION-SECRET-" + "f" * (SPAN + 5)

evidence   = "env probe " + S_EV
directives = "user says " + S_DIR
pitch      = "diff under review " + S_PIT
assistant  = "earlier Claude said " + S_ASST
standing   = "# CLAUDE.md rule 1b " + S_STD
conclusion = "VERDICT: WARN reasons " + S_CONCL

# Voting arg-set: 5 blocks, no conclusion -- what the production voting call site
# passes (consult_council.py `exfil_context`).
voting = cc.build_exfil_context(evidence, directives, pitch, assistant, standing)
# Inspector arg-set: 6 blocks incl. conclusion -- what the production inspector call
# site passes (`insp_exfil_context`).
inspector = cc.build_exfil_context(evidence, directives, pitch, assistant, standing,
                                   conclusion)
# OLD pre-fix corpus: default args drop assistant+standing+conclusion (3-block).
old3 = cc.build_exfil_context(evidence, directives, pitch)


def pq(secret: str) -> str:
    return "/search?q=" + secret


benign = "/wiki/Python_(programming_language)"

CASES = [
    # (label, pathquery, corpus, want)
    ("voting catches evidence span",     pq(S_EV),   voting,    True),
    ("voting catches directive span",    pq(S_DIR),  voting,    True),
    ("voting catches pitch span",        pq(S_PIT),  voting,    True),
    ("voting catches assistant span (fix)",  pq(S_ASST), voting, True),
    ("voting catches standing span (fix)",   pq(S_STD),  voting, True),
    # the production voting arg-set omits conclusion (voting requests land in round 1,
    # before it exists), so the helper given that arg-set does not carry it.
    ("voting arg-set does NOT carry conclusion",  pq(S_CONCL), voting,   False),
    # the production inspector arg-set includes conclusion (seen before requesting).
    ("inspector arg-set catches conclusion span", pq(S_CONCL), inspector, True),
    ("inspector still catches assistant span",  pq(S_ASST), inspector, True),
    # regression guard: pre-fix 3-block corpus missed the two gaps.
    ("OLD 3-block MISSED assistant span", pq(S_ASST), old3,     False),
    ("OLD 3-block MISSED standing span",  pq(S_STD),  old3,     False),
    ("OLD 3-block still caught pitch span", pq(S_PIT), old3,    True),
    # benign URL: no verbatim prompt span -> never flagged.
    ("benign URL not flagged (voting)",    benign,    voting,    False),
    ("benign URL not flagged (inspector)", benign,    inspector, False),
]

ok = True
for label, pathquery, corpus, want in CASES:
    got = cc._exfil_span(pathquery, corpus)
    good = (got == want)
    ok = ok and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got {got} want {want}")

print(f"\n{'ALL PASS' if ok else 'SOME FAILED'} ({len(CASES)} cases)")
sys.exit(0 if ok else 1)
