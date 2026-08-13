#!/usr/bin/env python3
"""Tests for tier0_gate.py -- the deterministic PreToolUse gate.

MOST OF THESE ARE REGRESSION TESTS. Each corresponds to a defect found in this gate while it
was being written, and nearly all of them were bugs that made it silently WRONG rather than
visibly broken -- a gate that allows every edit while appearing healthy cannot be noticed by
using it, only by testing it. In rough order of how badly each would have hurt:

  B  "changed" computed by set difference, so the check could not fire in its central case.
     Two separate versions failed here: whole-file sets (found by these tests) and
     old/new-string sets (found by the council). Both are covered.
  L  a corrupt registry silently loading as {}, discarding every pending sweep.
  I  a deny that mutated the registry, retiring obligations its rejected fix never met.
  K  producer and consumer keying paths differently, making the registry dead data.
  A  composite atoms leaking their components ("18,419" also yielding "18" and "419").
  M  internal failures allowing the edit in silence.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tier0_gate as t  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"   [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}   [{detail}]")


def run_gate(payload: dict, state: Path, **envkw: str) -> tuple[int, str, str]:
    env = dict(os.environ, COUNCIL_STATE_ROOT=str(state), **envkw)
    p = subprocess.run([sys.executable, str(ROOT / "tier0_gate.py")],
                       input=json.dumps(payload), text=True,
                       capture_output=True, env=env, timeout=120)
    return p.returncode, p.stdout, p.stderr


def decision(stdout: str) -> str | None:
    try:
        return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]
    except (ValueError, KeyError, TypeError):
        return None


def reason(stdout: str) -> str:
    """The deny text, DECODED. Asserting against raw stdout is a void check: JSON escapes
    the inner quotes, so `'"684"' in stdout` is false even when the message says exactly
    that. A test that cannot fail for the right reason is worse than no test."""
    try:
        return json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    except (ValueError, KeyError, TypeError):
        return ""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tier0-"))
    state = tmp / "state"
    t.STATE_ROOT = state

    print("\n-- A. atom extraction is narrow; composites claim their parts --")
    a = t.extract_atoms("n=684 and 18,419 and 596.3 and 8ca1792 and 5 and x9")
    check("multi-digit captured", "684" in a)
    check("thousands-separated captured whole", "18,419" in a)
    check("component NOT leaked (regression)", "18" not in a and "419" not in a,
          f"atoms={sorted(a)}")
    check("decimal captured", "596.3" in a)
    check("git hash captured", "8ca1792" in a)
    check("bare single digit ignored", "5" not in a)

    # A1. THE DATE/DATETIME CLASS, DIRECTLY -- extract_atoms is called on datetimes here rather
    # than reached only through an end-to-end path, so the tokeniser's treatment of them is
    # pinned by an assertion on its own output. Below are the datetime cases, plus the boundary
    # cases separating what is consumed whole from what leaks.
    print("\n-- A1. dates and datetimes: what is consumed whole, and what leaks --")
    check("bare date whole", t.extract_atoms("on 2026-08-06 we ran it") == {"2026-08-06"})
    check("ISO datetime with Z whole",
          t.extract_atoms("at 2026-08-06T19:04:50Z") == {"2026-08-06T19:04:50Z"})
    check("ISO datetime without seconds whole",
          t.extract_atoms("at 2026-08-06T19:04Z") == {"2026-08-06T19:04Z"})
    check("space-separated datetime whole",
          t.extract_atoms("at 2026-08-06 19:04:50") == {"2026-08-06 19:04:50"})
    check("REGRESSION: the year does NOT leak from an ISO datetime",
          "2026" not in t.extract_atoms("2026-08-06T19:04:50Z"),
          "this is the exact bug the ISO fix closed")
    # THE DOCUMENTED LIMITS. These assert the CURRENT behaviour, not desired behaviour --
    # they exist so that changing it is a deliberate act with a failing test, and so the
    # module comment describing them cannot drift away from the code.
    #
    # ONE OF THEM WAS FIXED 2026-08-10 AND THIS IS THE DELIBERATE ACT. The former limit read
    # `extract_atoms("shipped on 2026-08-06.") == {"2026"}` and cited "documented at ATOM_RE"
    # -- a STALE POINTER: the pre-edit ATOM_RE comment mentioned no such thing (checked against
    # the tarball snapshot taken before the change). The limit was real, tested, and undocumented
    # where its own test said to look.
    # WHY IT STOPPED BEING ACCEPTABLE: it was not merely cosmetic. The leaked `2026` collided
    # with every other year in a dated file, and that denied a real edit. Its siblings were
    # never tested at all and are covered below.
    print("\n-- A1b. trailing punctuation: the fix, and what it used to cost --")
    # THE "BEFORE" VALUES ARE COMPUTED HERE, NOT REMEMBERED. An earlier version of this section
    # carried them as prose details ("was set() before"), which is a claim about a tokeniser that
    # no longer exists and cannot be checked by running the suite. The pre-fix pattern differs
    # from the current one in TWO places -- its trailing lookahead and its non-atomic group -- and
    # reconstructing both lets every before/after pair below be PROVEN on each run rather than
    # asserted. It was one place until the group was made atomic; see _SWAPS below.
    import re as _re
    # TWO PROPERTIES HAVE TO BE UNDONE, NOT ONE, and forgetting the second is what broke this
    # block when the group was made atomic: the pre-fix pattern had BOTH the old trailing
    # lookahead AND a non-atomic `(?:` group. Reconstructing only the lookahead leaves the
    # reconstruction atomic, and an atomic group cannot backtrack into the shorter alternative --
    # which is the very decay these checks exist to demonstrate -- so all three "the pre-fix
    # pattern decayed/truncated it" checks started reporting [] instead of the decayed value.
    _SWAPS = ((r")(?![\w])(?!\.\w)", r")(?![\w.])"),
              (r"(?<![\w.])(?>", r"(?<![\w.])(?:"))
    _PRE = t.ATOM_RE.pattern
    for _old_frag, _new_frag in _SWAPS:
        check(f"PRECONDITION: the reconstruction's anchor {_old_frag!r} is present once",
              _PRE.count(_old_frag) == 1,
              f"found {_PRE.count(_old_frag)}; a swap that matches nothing reconstructs "
              f"the LIVE pattern and makes every before/after pair below vacuous")
        _PRE = _PRE.replace(_old_frag, _new_frag)
    _PRE_RE = _re.compile(_PRE)

    def _old(text: str) -> set:
        return {m.group(0) for m in _PRE_RE.finditer(text)}

    # THE BACKSTOP FOR THE PER-SWAP PRECONDITIONS ABOVE: those catch an anchor that matched zero
    # or many times, and this catches the end state they exist to prevent -- a reconstruction
    # equal to the CURRENT pattern, which makes every "and the pre-fix pattern did X" check below
    # pass vacuously by comparing the gate to itself. Kept as a separate assertion because the
    # two failure modes are not the same: a swap could match once and still leave the pattern
    # unchanged if someone made the replacement text identical to what it replaced.
    check("the reconstructed pre-fix pattern differs from the live one",
          _PRE != t.ATOM_RE.pattern,
          "if equal, every before/after pair below is vacuous")

    check("a sentence-final date keeps its whole date",
          t.extract_atoms("shipped on 2026-08-06.") == {"2026-08-06"})
    check("  ...and the pre-fix pattern decayed it to the bare year",
          _old("shipped on 2026-08-06.") == {"2026"}, str(sorted(_old("shipped on 2026-08-06."))))
    # THE TRAILING CHARACTER IS THE ONLY VARIABLE, which is what made the diagnosis a
    # measurement rather than a guess: every other separator already worked.
    for trailer, label in ((",", "comma"), (")", "paren"), (";", "semicolon"),
                           (":", "colon"), ("\n", "newline"), ("", "end of string")):
        check(f"a date followed by {label} is whole",
              t.extract_atoms(f"on 2026-08-06{trailer}") == {"2026-08-06"})
        check(f"  ...and was ALREADY whole before the fix ({label})",
              _old(f"on 2026-08-06{trailer}") == {"2026-08-06"},
              "only the period case regressed, which is what localised the defect")
    # THE FALSE NEGATIVES: a sentence-final number was INVISIBLE, so it got no stale-sibling
    # protection and nothing said so. Each pair asserts the fix AND the loss it repaired.
    for text, atom, label in (("count 1226.", "1226", "integer"),
                              ("hash 17c5983.", "17c5983", "git hash"),
                              ("ver 1.2.3.", "1.2.3", "dotted version"),
                              ("pi 3.14159.", "3.14159", "decimal")):
        check(f"a sentence-final {label} no longer vanishes",
              t.extract_atoms(text) == {atom})
        check(f"  ...and the pre-fix pattern lost it entirely ({label})",
              _old(text) == set(), str(sorted(_old(text))))
    # THE TRUNCATIONS: these returned a SHORTER atom, which is worse than none -- a truncated
    # datetime is a different, less specific atom that matches more things.
    for text, atom, was, label in (
            ("at 2026-08-06T19:04:50Z.", "2026-08-06T19:04:50Z", "2026-08-06T19:04", "datetime"),
            ("n 1,234,567.", "1,234,567", "1,234", "grouped number")):
        check(f"a sentence-final {label} is no longer truncated",
              t.extract_atoms(text) == {atom})
        check(f"  ...and the pre-fix pattern truncated it ({label})",
              _old(text) == {was}, str(sorted(_old(text))))
    # AND THE BEHAVIOUR THAT MUST NOT CHANGE: a period that genuinely CONTINUES a number still
    # binds, so the fix cannot be "ignore periods". These must agree BOTH sides of the change.
    for text, want, label in (("ip 192.168.1.1", {"192.168.1.1"}, "dotted quad matches whole"),
                              ("a2026-08-06", set(), "a leading word char blocks the match"),
                              ("v1.2026", set(), "a preceding period blocks the match"),
                              ("slice text[:400].", {"400"}, "a bracketed slice is unaffected")):
        check(label, t.extract_atoms(text) == want)
        check(f"  ...and the fix did not change it ({label})", _old(text) == want,
              str(sorted(_old(text))))

    print("\n-- A1c. the remaining documented limits --")
    # THE OFFSET IS DROPPED, so this atom is NOT "whole" -- an earlier detail string said it was,
    # contradicting the very assertion it annotated. What survives is second-precision, which is
    # the property that matters: a surviving one really is a specific sibling, not a repeat.
    check("KNOWN LIMIT: a numeric UTC offset is not consumed",
          t.extract_atoms("at 2026-08-06T19:04:50+00:00") == {"2026-08-06T19:04:50"},
          "offset dropped; what remains is still second-precision, so a survivor is a real sibling")
    check("KNOWN LIMIT: 0.00 matches (digit CHARACTERS, not significant figures)",
          t.extract_atoms("v=0.00") == {"0.00"})
    check("10.0 matches, so no significance rule is implied",
          t.extract_atoms("v=10.0") == {"10.0"})
    check("trivial decimals still ignored",
          t.extract_atoms("0.0 and 1.5") == set())

    print("\n-- A1d. effectively atomic: no backtracking into a shorter alternative --")
    # WHAT THIS PINS AND WHY IT IS SEPARATE FROM A1b. A1b covers the TRAILING-PERIOD half of the
    # backtracking defect; this covers the TRAILING-WORD-CHARACTER half, which survived that fix.
    # Mechanism, and it is the same one: a long alternative matches, the trailing lookahead
    # rejects it, and a non-atomic group backtracks into a SHORTER alternative at the same start
    # position, which then passes. The gate removes that backtrack with a real atomic group,
    # `(?>...)`: once an alternative inside it matches, the engine will not re-enter the group to
    # try a shorter one at that start. (The `(?=(X))\1` emulation reaches the same end by fixing
    # one alternative into group 1 so a backreference must re-consume exactly that text; it is
    # what this pattern would revert to if the project's Python floor ever dropped below 3.11,
    # and it is NOT what ships -- see A1d, which asserts groups == 0.) Those fragments are the
    # pointer -- no line numbers, which would stale on the next ATOM_RE edit.
    # `(?>` would say it more plainly, but the CPython `re` documentation gives atomic grouping as
    # "Added in version 3.11" (fetched 2026-08-11), and this project's declared floor is 3.10:
    # README line 424 reads "**Python 3.10+**" and install.py's check_python errors only when
    # `(v.major, v.minor) < (3, 10)`. So the emulation is what keeps a SUPPORTED version working.
    # WHAT WAS NOT VERIFIED: that `(?>` actually raises on 3.10. Only /usr/bin/python3.14 exists
    # on this host, so the version floor is the evidence, not an observed failure.
    # HOW IT WAS FOUND: a malformed timestamp in prose (`2026-08-11T06:5xZ`) yielded a bare
    # `2026`, and the gate denied the edit that contained it for leaving other years unswept.
    # THE BEFORE VALUES ARE COMPUTED, not remembered -- `_old` above reconstructs the pre-atomic
    # pattern, and the preconditions there fail loudly if that reconstruction stops working.
    check("a date followed by a word character matches NOTHING",
          t.extract_atoms("stamped 2026-08-11T06:5xZ here") == set(),
          str(sorted(t.extract_atoms("stamped 2026-08-11T06:5xZ here"))))
    check("  ...and the pre-atomic pattern decayed it to a bare year",
          _old("stamped 2026-08-11T06:5xZ here") == {"2026"},
          str(sorted(_old("stamped 2026-08-11T06:5xZ here"))))
    check("a bare date followed by T matches nothing either",
          t.extract_atoms("at 2026-08-11T") == set())
    check("a date followed by an underscore matches nothing",
          t.extract_atoms("file 2026-08-11_v2.md") == set())
    check("a grouped number followed by a word character is not TRUNCATED",
          t.extract_atoms("n=1,234,567x") == set(),
          "a truncated atom is a DIFFERENT, less specific atom -- worse than none")
    check("  ...and the pre-atomic pattern truncated it",
          _old("n=1,234,567x") == {"1,234"}, str(sorted(_old("n=1,234,567x"))))
    # CONSISTENCY WITH THE CLASSES THAT ALREADY BEHAVED: these needed no fix, and asserting them
    # is what shows the date/grouped classes were the OUTLIERS rather than the new rule being new.
    for s, label in (("1.2.3x", "dotted version"), ("17c5983x", "git hash"),
                     ("192.168.1.1x", "IP")):
        check(f"a {label} followed by a word character still matches nothing",
              t.extract_atoms(s) == set(), f"{s} -> {sorted(t.extract_atoms(s))}")
    # AND THE CASES THAT MUST NOT CHANGE. The emulation commits to the first matching
    # alternative, so the risk is over-committing where a shorter match was correct.
    check("a well-formed datetime is still whole",
          t.extract_atoms("at 2026-08-11T07:32:46Z") == {"2026-08-11T07:32:46Z"})
    check("an IP is still whole", t.extract_atoms("host 192.168.1.1 up") == {"192.168.1.1"})
    check("a two-part decimal still matches", t.extract_atoms("v=12.5") == {"12.5"})
    check("a three-part version still matches", t.extract_atoms("v 2.1.223 here") == {"2.1.223"})
    check("a grouped number is still whole", t.extract_atoms("n=18,419 ok") == {"18,419"})
    check("a sentence-final date is still whole (A1b's case, unregressed)",
          t.extract_atoms("shipped on 2026-08-06.") == {"2026-08-06"})
    # ATOM_RE MUST HAVE NO CAPTURE GROUPS, and the reason is a caller rather than taste:
    # `extract_atoms` calls `ATOM_RE.findall`, which returns GROUPS rather than whole matches the
    # moment a pattern has any. Add one and every atom silently becomes a tuple or a fragment.
    # THIS IS NOT HYPOTHETICAL -- the pattern briefly carried a group on purpose, as part of a
    # `(?=(X))\1` emulation of an atomic group used while the project's Python floor was below
    # 3.11. That worked only because the single group spanned the whole match. The floor is now
    # 3.12, the pattern uses a real `(?>` atomic group, and the group is gone -- so the safe state
    # is zero, and that is what is asserted.
    check("ATOM_RE has no capture groups", t.ATOM_RE.groups == 0,
          f"{t.ATOM_RE.groups} group(s); findall returns groups, so ANY group changes its result")
    for _s in ("on 2026-08-06 we ran it", "n=18,419 and 684 and 17c5983",
               "host 192.168.1.1 v 2.1.223", "1,234,567.", "2026-08-11T06:5xZ"):
        check(f"findall == finditer group(0) for {_s[:24]!r}",
              t.ATOM_RE.findall(_s) == [m.group(0) for m in t.ATOM_RE.finditer(_s)],
              f"findall={t.ATOM_RE.findall(_s)} "
              f"finditer={[m.group(0) for m in t.ATOM_RE.finditer(_s)]}")

    # A2. A DATE CANNOT CARRY A BLOCK ALONE (the user's ruling 2026-08-08). The unit is
    # date_only(); the end-to-end consequence is checked in section A3 below, because a
    # predicate that returns the right answer while the caller ignores it is worth nothing.
    print("\n-- A2. date_only(): which trigger sets can no longer block --")
    check("a lone bare date is date-only", t.date_only({"2026-08-06"}))
    check("several bare dates are still date-only",
          t.date_only({"2026-08-06", "2026-08-07"}))
    check("a date mixed with a non-date is NOT date-only -- it must still block",
          not t.date_only({"192", "2026-08-07"}),
          "the measured mixed case")
    check("a git hash alone is NOT date-only", not t.date_only({"17c5983"}))
    check("an ISO DATETIME is NOT treated as a bare date",
          not t.date_only({"2026-08-06T19:04:50Z"}),
          "seconds make it effectively unique, so a survivor is a real sibling")
    check("the empty set is not date-only",
          not t.date_only(set()), "no atoms means nothing to suppress, not a free pass")

    print("\n-- A3. END TO END: the gate ACTS on it, not just the predicate --")
    f_date = tmp / "dated.md"
    f_date.write_text("entry one 2026-08-06\nentry two 2026-08-06\n")
    rc3, out3, err3 = run_gate(
        {"tool_name": "Edit", "session_id": "a3-date",
         "tool_input": {"file_path": str(f_date), "old_string": "entry one 2026-08-06",
                        "new_string": "entry one 2026-08-07"}}, state)
    check("a date-only stale sibling NO LONGER blocks",
          decision(out3) != "deny", f"rc={rc3} out={out3[:120]}")
    check("and the suppression is ANNOUNCED on stderr, never silent",
          "NOT BLOCKING" in err3, f"stderr={err3[:120]}")

    f_mix = tmp / "mixed.md"
    f_mix.write_text("build 192 on 2026-08-06\nalso build 192 on 2026-08-06\n")
    rc4, out4, err4 = run_gate(
        {"tool_name": "Edit", "session_id": "a3-mixed",
         "tool_input": {"file_path": str(f_mix), "old_string": "build 192 on 2026-08-06",
                        "new_string": "build 195 on 2026-08-07"}}, state)
    check("a MIXED trigger STILL blocks -- the rule did not disarm the gate",
          decision(out4) == "deny", f"rc={rc4} out={out4[:160]}")

    # A4. DELETION IS NOT REWRITING. changed_atoms() works on occurrence counts and cannot
    # tell the two apart; deleted_not_rewritten() is the filter that can. Both call sites are
    # exercised, INCLUDING the Write branch -- a filter wired into a path no test enters is
    # a filter nobody has checked.
    print("\n-- A4. deleted_not_rewritten(): unit, both call sites, and its limit --")
    del_old = 'out.append(f"x {rec.get(\'summary\')[:404]}")'
    del_new = 'out.append("x " + render(rec))'
    check("a deleted line's constant is NOT a rewrite",
          t.deleted_not_rewritten(del_old, del_new, "404"))
    check("an in-place value change IS a rewrite",
          not t.deleted_not_rewritten("alpha 684 beta", "alpha 698 beta", "684"))
    check("a version bump IS a rewrite",
          not t.deleted_not_rewritten("pinned at 2.1.223 now", "pinned at 2.1.224 now",
                                      "2.1.223"))
    check("an atom absent from the old text is not 'deleted'",
          not t.deleted_not_rewritten("nothing here", "nor here", "999"))

    # THE LIMIT, ASSERTED SO IT CANNOT DRIFT UNNOTICED. Below REWRITE_SIMILARITY a genuine
    # rewrite is classified as a deletion and its stale siblings go unreported. That is a
    # FAIL-OPEN: the gate stays silent rather than blocking wrongly. Pinned as current
    # behaviour, not endorsed as correct.
    check("a rewrite that also rewrites its whole line is MISSED (fail-open, documented)",
          t.deleted_not_rewritten("alpha 684 beta",
                                  "completely different wording entirely", "684"),
          "below REWRITE_SIMILARITY -- suppression, never a wrong block")
    # THE FAIL-CLOSED DIRECTION, the one that produces a WRONG BLOCK. A newly added line that
    # merely resembles the deleted one is read as its replacement, so the atom stays in
    # `changed` and can block. The must-be-new rule does NOT prevent this: it excludes lines
    # that already existed, and this is by construction a line that did not.
    # Pinned as CURRENT BEHAVIOUR, not endorsed as correct.
    check("a coincidentally similar NEW line is read as a replacement (fail-closed)",
          not t.deleted_not_rewritten("cap = 400", "cap = xyz", "400"),
          "deletion misread as rewrite -> atom kept -> may block wrongly")
    check("...and the multiplicity rule does not rescue that case",
          not t.deleted_not_rewritten("x = 400\nx = 300\n", "x = 300\nx = 300\n", "400"),
          "an ADDED duplicate is genuinely new, so it counts as a replacement")

    fw = tmp / "written.py"
    fw.write_text("cap = 404\nother = 404\n")
    rcw, outw, _ = run_gate(
        {"tool_name": "Write", "session_id": "a4-write",
         "tool_input": {"file_path": str(fw), "content": "other = 404\n"}}, state)
    check("WRITE branch: deleting a line carrying an atom does NOT block",
          decision(outw) != "deny", f"rc={rcw} out={outw[:160]}")

    fw2 = tmp / "written2.py"
    fw2.write_text("cap = 404\nother = 404\n")
    rcw2, outw2, _ = run_gate(
        {"tool_name": "Write", "session_id": "a4-write2",
         "tool_input": {"file_path": str(fw2), "content": "cap = 407\nother = 404\n"}}, state)
    check("WRITE branch: a genuine in-place rewrite STILL blocks",
          decision(outw2) == "deny", f"rc={rcw2} out={outw2[:160]}")

    # A5. ONE TOKENISER, THREE FUNCTIONS. extract_atoms, _count and survivors_in must agree
    # about what a token IS. They did not: _count and survivors_in used a per-atom regex whose
    # trailing (?![\w.]) treats a HYPHEN as a boundary, so `2026` matched inside `2026-08-07`
    # while extract_atoms consumed the date whole and yielded no bare `2026` at all. Any
    # region holding one real standalone `2026` therefore had its count inflated by every
    # nearby DATE, so deleting dated lines looked like rewriting `2026` -- and date_only()
    # could not suppress it, because the atom reported was not a date.
    # NOT ISOLATED: the replay improvement was measured with date_only(),
    # deleted_not_rewritten() and this tokenisation fix all in place, so no single one of the
    # three is credited here. Attributing a share would need a run per fix.
    print("\n-- A5. REGRESSION: one tokeniser shared by extract_atoms/_count/survivors_in --")
    dated = "on 2026-08-07 and 2026-08-08 we ran it"
    check("extract_atoms yields the dates, never a bare year",
          t.extract_atoms(dated) == {"2026-08-07", "2026-08-08"},
          f"atoms={sorted(t.extract_atoms(dated))}")
    check("_count does NOT find a year inside dates",
          t._count(dated, "2026") == 0, f"got {t._count(dated, '2026')}")
    check("survivors_in does NOT report a year inside dates",
          t.survivors_in(dated, {"2026"}) == {},
          f"got {t.survivors_in(dated, {'2026'})}")
    mixed_year = "year 2026 alone and 2026-08-07 dated"
    check("a genuinely standalone year IS counted, exactly once",
          t._count(mixed_year, "2026") == 1, f"got {t._count(mixed_year, '2026')}")
    check("deleting dated lines is NOT a rewrite of the year",
          t.changed_atoms("kept 2026\ndrop 2026-08-07\n", "kept 2026\n") == {"2026-08-07"},
          f"got {sorted(t.changed_atoms('kept 2026' + chr(10) + 'drop 2026-08-07' + chr(10), 'kept 2026' + chr(10)))}")
    check("ordinary atoms are unaffected by the shared tokeniser",
          t._count("alpha 684 beta 684", "684") == 2
          and t._count("v 2.1.223 here", "2.1.223") == 1
          and t._count("n=18,419 ok", "18,419") == 1)

    # A TRIPWIRE ON A PAST CONSTRUCTION, PLACED HERE RATHER THAN IN A DOCSTRING. Called a
    # tripwire and not an enforced invariant deliberately -- the blind spots enumerated below are
    # what stop it being one. tier0_gate once carried per-atom patterns built with re.escape,
    # which disagreed with ATOM_RE about hyphens. (An earlier version of this comment said FIVE
    # of them. That count is not re-derivable -- Council/ is not a git repository -- so it is
    # gone rather than repeated.) The natural check -- grep the module for that construction --
    # CANNOT live in the module: a comment quoting the search string becomes its own match, and
    # the grep then reports a hit on the comment claiming there are none. That happened. Keeping
    # the pattern in this file, and the searched source in the other, is what makes it real.
    gate_src = (ROOT / "tier0_gate.py").read_text(errors="replace")
    check("PRECONDITION: the gate source was actually read",
          len(gate_src) > 10_000 and "def has_atom" in gate_src,
          f"{len(gate_src)} bytes -- guards against a vacuous pass on an empty read")
    # WHAT THIS BLOCK CHECKS, STATED AS THE GREP IT IS AND NOT AS A GUARANTEE. An earlier
    # version checked only that `re.escape` was absent and reported that as "every membership
    # test must use has_atom()". It does not follow: a hand-written per-atom pattern needs no
    # escaping, so the check could pass while the invariant was broken. It is now four TEXTUAL
    # greps over the gate's source -- no escape call, no compile on an indented line, no direct
    # `re.<fn>(` call among the eight spellings listed, and a NONEMPTY count of unindented
    # `= re.compile` constants, so that the first three being zero is not vacuous. That fourth
    # one asserts only nonemptiness: 9 were measured and the failure message carries that
    # figure, but pinning 9 as a floor would fail the suite for deleting an unrelated regex,
    # which is not what this block is guarding.
    # MEASURED 2026-08-11T03:09:55Z over tier0_gate.py's lines -- escape 0, indented compile 0,
    # eight-spelling `re.<fn>(` 0, unindented `= re.compile` 9. The command is the four
    # comprehensions immediately below: re-running this suite IS the re-measurement, so the
    # figure needs no separate pointer. An earlier version of this comment dated the same
    # figures 2026-08-10, the LOCAL date, while the greps ran after UTC midnight.
    # WHAT THESE GREPS CANNOT SEE, so that nobody reads a proof into them: they never inspect an
    # ARGUMENT. A module-scope `PAT = re.compile(f"...{atom}...")` is unindented and calls no
    # escape; an aliased import (`from re import search`) defeats the spelling list; and the
    # constant count says nothing about whether those patterns are fixed literals. This is a
    # tripwire on the construction that once shipped here, not a proof that none can return.
    # Sections A and B are what test the current behaviour, and they test it by calling the code.
    src_lines = gate_src.splitlines()
    escapes = [ln for ln in src_lines if "re.escape" in ln]
    indented_compiles = [ln for ln in src_lines
                         if ln[:1].isspace() and "re.compile" in ln]
    direct_calls = [ln for ln in src_lines
                    if any(f"re.{fn}(" in ln for fn in
                           ("search", "match", "fullmatch", "findall",
                            "finditer", "sub", "subn", "split"))]
    constants = [ln for ln in src_lines
                 if not ln[:1].isspace() and "= re.compile" in ln]
    check("no re.escape call appears in tier0_gate.py",
          escapes == [], f"found {len(escapes)}: {escapes[:3]}")
    check("no regex is compiled on an indented line there",
          indented_compiles == [],
          f"found {len(indented_compiles)}: {indented_compiles[:3]}")
    check("no direct re.<fn>( call there, in the eight spellings listed",
          direct_calls == [], f"found {len(direct_calls)}: {direct_calls[:3]}")
    check("PRECONDITION: the three zeroes are not vacuous -- the gate DOES use regexes",
          len(constants) > 0,
          f"{len(constants)} module-scope compiled constants; 9 at the time of the comment above")

    # AND THE FIX IT PROTECTS, end to end. Under the old per-atom pattern the second line
    # "carried" the atom 2026 (the pattern treats a hyphen as a boundary), so it was skipped
    # as a replacement candidate and the year read as DELETED. It was rewritten into a date.
    check("a year rewritten into a date is a REWRITE, not a deletion",
          not t.deleted_not_rewritten("year 2026 standalone\n",
                                      "year 2026-08-07 standalone\n", "2026"),
          "the case that actually discriminates the tokeniser fix")

    # A6. BASH WRITE DETECTION. Standing rule 12 forbids shell writes; the PreToolUse matcher
    # never covered Bash, so `sed -i` and `>` reached disk unreviewed. Two directions are
    # pinned here and they pull against each other: a real write must be caught, and ordinary
    # read-only work must NOT be, since a wrong denial blocks the agent's own measurements.
    print("\n-- A6. bash_write_targets(): writes caught, read-only work left alone --")

    def wrote(cmd):
        return [tgt for tgt, _ in t.bash_write_targets(cmd)]

    check("a real redirect is a write", wrote("grep x f.py > out.txt") == ["out.txt"])
    check("a redirect INSIDE a quoted span is not",
          wrote("echo 'a > \"b\"'") == [], "the `>` is text, not an operator")
    check("an fd redirect to the bit bucket is not", wrote("cmd 2>/dev/null") == [])
    check("a temp target is not worth reviewing", wrote("ls >> /tmp/x") == [])
    check("tee is a write", wrote("tee log.txt") == ["log.txt"])
    check("sed -i is a write, and its SCRIPT is not the target",
          wrote("sed -i 's|a|b|' notes.md") == ["notes.md"])
    check("an operand that merely begins with s is still a target",
          wrote("sed -i 1d s.txt") == ["s.txt"],
          "a filename starting s/y must not be mistaken for a sed script")
    check("read-only sed is not a write", wrote("sed -n 5p f.md") == [])
    check("a heredoc that writes is a write",
          wrote('python3 - <<PY\nopen("f.txt","w")\nPY') == ["(heredoc body)"])
    check("a read-only heredoc probe is NOT a write",
          wrote("python3 - <<PY\nprint(1)\nPY") == [],
          "this agent runs these constantly; denying them would block real work")

    # A6a. POSITION. A writer counts only as the COMMAND WORD of its simple command. Every
    # case here was a wrong denial the council found by reading the walk: the name of a
    # writing tool appearing as an ARGUMENT is not a write, and a blocking gate that cannot
    # tell those apart turns ordinary commands back.
    print("\n-- A6a. a writer only counts in command position --")
    check("`tee` as an argument is not a write", wrote("echo tee notes.md") == [])
    # `cp tee dest` IS a write -- of `dest`, by cp. What must not happen is `dest` being
    # attributed to TEE because the word appeared as an argument.
    # BOTH HALVES ARE ASSERTED. Checking only the reason would let a wrong TARGET through --
    # [("tee", "cp (overwrite)")] would satisfy a reason-only test while contradicting the
    # very claim this check exists to make.
    check("the word `tee` as an argument is not a TEE write",
          t.bash_write_targets("cp tee dest") == [("dest", "cp (overwrite)")],
          "the destination is dest, and it is attributed to cp, never to tee")
    check("a read-only command taking `tee` as an argument writes nothing",
          wrote("grep tee notes.md") == [])
    check("`of=` without dd is not a write", wrote("echo of=x") == [])
    check("`of=` WITH dd is a write", wrote("dd if=a of=b.img") == ["b.img"])
    check("a long sed flag merely CONTAINING i is not in-place",
          wrote("sed --file=script notes.md") == [],
          "`--file` contains an i; reading that as -i denies a read-only sed")
    check("a NON-interpreter heredoc carrying write-like text is data, not a program",
          wrote('cat <<EOF\nopen("f","w")\nEOF') == [],
          "only an actual interpreter invocation makes a writing body a write")
    check("an interpreter heredoc that writes is still a write",
          wrote('python3 - <<PY\nopen("f.txt","w")\nPY') == ["(heredoc body)"])
    check("a PREFIX assignment binds only its own command",
          wrote("S=/repo cmd; echo x > $S/out") == [],
          "`S=x cmd` does not export S to the next command")
    check("a STANDALONE assignment does persist",
          wrote("S=/repo; echo x > $S/out") == ["/repo/out"])

    # A6c. WRAPPERS, KEYWORDS, MULTI-TARGET, AND FLAGS THAT EAT THEIR ARGUMENT. Every case
    # here was a defect the council found by reading the walk -- both missed writes and wrong
    # denials. They are grouped because they share one cause: the walk decided what a command
    # was from the token alone, without accounting for what shells actually do with wrappers,
    # keywords, and option arguments.
    print("\n-- A6c. wrappers, keywords, multi-target, argument-eating flags --")
    check("a wrapper does not hide the writer (sudo)", wrote("sudo tee out.md") == ["out.md"])
    check("a wrapper with an assignment does not hide it (env)",
          wrote("env X=1 tee out.md") == ["out.md"])
    check("`command` does not hide it", wrote("command sed -i 1d f.md") == ["f.md"])
    check("tee writes EVERY operand, not the first",
          wrote("tee a.txt b.txt") == ["a.txt", "b.txt"])
    check("a command inside `then` is still in command position",
          wrote("if true; then tee log.md; fi") == ["log.md"])
    check("a shell -c body is judged as SHELL, not by a python-write regex",
          wrote("bash -c 'echo x > out.md'") == ["out.md"])
    check("perl in-place is a write too", wrote("perl -i -pe s/a/b/ f.txt") == ["f.txt"])
    check("`-fi` is -f with the value i, NOT in-place",
          wrote("sed -fi script.sed file.txt") == [],
          "a read-only sed must not be denied")
    check("a separate -f argument is the SCRIPT, not a target",
          wrote("sed -i -f script.txt target.txt") == ["target.txt"])
    check("a heredoc belongs to ITS OWN command, not the command line",
          wrote('python3 x.py; cat <<EOF\nopen("f","w")\nEOF') == [],
          "an interpreter earlier in the line must not make a later cat heredoc a write")

    # A6d. OPTION GRAMMAR AND COMMAND BOUNDARIES. Each case here was a defect found by the
    # council or by probing, in code that had already passed a review round. They share a
    # cause: the walk treated shell syntax as simpler than it is -- a flag as a thing to skip,
    # a newline as whitespace, `>(` as a file, a wrapper as a command.
    print("\n-- A6d. option grammar, wrappers, and command boundaries --")
    check("a newline separates commands",
          wrote("echo ok\nrm victim.md") == ["victim.md"],
          "shlex counts \\n as whitespace; multi-line is this agent's normal shape")
    check("a newline INSIDE quotes is data, not a separator",
          wrote('echo "a\nb" > /tmp/x') == [])
    # The newline rewrite has to respect the rest of shell lexing or it creates NEW blindness.
    # Each of these was a false negative the rewrite itself introduced.
    check("a backslash-newline CONTINUATION joins the lines",
          wrote("rm \\\nvictim.md") == ["victim.md"],
          "splitting it reported the stray backslash and missed the file")
    check("a continuation JOINS the words, so a following # is mid-word",
          wrote("rm safe\\\n#suffix victim.md") == ["safe#suffix", "victim.md"],
          "resetting word adjacency at the join read `#suffix` as a comment "
          "and discarded the real target after it")
    check("a comment ends at its newline, not at end of string",
          wrote("# note\nrm victim.md") == ["victim.md"],
          "shlex's commenter runs to end of STRING once newlines are rewritten")
    check("a trailing comment does not hide the next line",
          wrote("echo ok  # trailing\nrm victim.md") == ["victim.md"])
    check("`#` mid-word is not a comment", wrote("echo a#b > out.md") == ["out.md"])
    check("`#` inside quotes is not a comment",
          wrote('echo "a # b" > out.md') == ["out.md"])
    # PINS THE ESCAPE BRANCH, which nothing else here exercises: an escaped quote inside a
    # double-quoted string must NOT close it. Deleting that branch makes this input desync the
    # quote tracker, so a newline inside the string becomes a command separator and the quoted
    # text is read as `rm victim.md` -- inventing two write targets out of data.
    check("an escaped quote does not close the string",
          wrote('echo "a\\"b\nrm victim.md" > /tmp/x') == [],
          "without escape tracking this reports victim.md and '>' from quoted text")
    # OBSERVED, not inferred: `bash -c 'echo A\ # ; > f'` creates f -- the redirect really
    # executes -- while the detector returned []. Which internal branch misfired was not
    # traced, so it is not written down here.
    check("an escaped space does not make a following # a comment",
          wrote("echo A\\ # ; > victim.md") == ["victim.md"],
          "real bash executes this redirect")
    check("$'...' ANSI-C quoting does not desync the scanner",
          wrote("echo $'it\\'s'\nrm victim.md") == ["victim.md"],
          "backslash escapes inside $'...' but is literal inside plain '...'")
    check("a backslash inside PLAIN single quotes stays literal",
          wrote("echo 'a\\' > out.md") == ["out.md"])
    check("`--` ends the options, so -weirdfile is a filename",
          wrote("rm -- -weirdfile") == ["-weirdfile"])
    check("`cp -t DEST SRC` writes DEST, not the source",
          wrote("cp -t /dest src.md") == ["/dest"],
          "the last-operand rule named a read-only source AND missed the destination")
    check("`--target-directory=` form too", wrote("cp --target-directory=/d a.md") == ["/d"])
    check("truncate's -r reference is READ, not written",
          wrote("truncate -r ref.md f.md") == ["f.md"])
    check("truncate's -s size is not a path", wrote("truncate -s 0 log.md") == ["log.md"])
    check("a wrapper's flag VALUE is not the command word",
          wrote("sudo -u root rm victim.md") == ["victim.md"])
    check("...for wrappers taking a numeric value too",
          wrote("nice -n 10 tee out.md") == ["out.md"])
    check("wrapper state does NOT cross a command boundary",
          wrote("sudo -u; rm victim.md") == ["victim.md"],
          "a pending flag value once swallowed the next command's name")
    check("`;;` terminates a case arm and is not a file",
          wrote("case x in a) rm victim.md ;; esac") == ["victim.md"])
    check("a process substitution is skipped OVER, not stopped at",
          wrote("tee >(logger) real.md") == ["real.md"],
          "treating >( as terminal lost the genuine target after it")
    check("and its inner command is never a target", wrote("tee >(logger)") == [])
    check("a temp root itself is allowed, trailing slash or not",
          t._write_target_ok("/tmp/") and t._write_target_ok("/tmp/ok.txt"))
    check("a traversal out of a temp root is NOT allowed",
          not t._write_target_ok("/tmp/../etc/passwd"),
          "startswith is lexical; the path is normalised first")

    print("\n-- A6b. shell assignments: positional and ordered, or not resolved at all --")
    check("an inline assignment resolving to a temp root is allowed",
          wrote("S=/tmp/scratch; cmd > $S/out.txt") == [])
    check("an inline assignment resolving to a real path is DENIED",
          wrote("S=/home/x/repo; cmd > $S/HANDOFF.md") == ["/home/x/repo/HANDOFF.md"])
    check("`S=` in ARGUMENT position is not an assignment",
          wrote("echo S=/tmp/x; cmd > $S/out.txt") == [],
          "resolving it would judge what the shell would not")
    check("an assignment AFTER the use does not apply to it",
          wrote("cmd > $S/out.txt; S=/tmp/x") == [])
    check("an unresolved expansion is allowed, not guessed at",
          wrote("cmd > $UNKNOWN/f") == [], "documented fail-open")
    # A GAP THAT SURVIVED MANY REVIEW ROUNDS. A test for this branch already existed -- the
    # `cmd > 'unbalanced` case below -- so it was not unexercised. What it asserted was only
    # the RETURN value, and it used an OBVIOUSLY malformed input, which made the branch look
    # like a corner for typos. Both readings were wrong: shlex raises on ORDINARY shapes,
    # `X="$(cat f)"; rm victim.md` among them, where the rm is simply never seen; and
    # RE-MEASURED 2026-08-10 by feeding every Bash command in this install's evidence logs
    # through bash_write_targets, 188 of 3748 UNIQUE commands (5.0%) reach it -- superseding an
    # earlier "150 of 3439 (4.4%)" taken on a smaller corpus. The figure and its bounds are
    # recorded once, at the branch itself in tier0_gate.py; this sentence is a second copy and
    # will stale if that one is re-measured.
    # A test that pins a silent fail-open as acceptable is how a blind spot that size stays
    # invisible. It still fails OPEN -- denying everything unparseable would refuse real
    # work -- but the ANNOUNCEMENT is now asserted too, not just the return.
    import contextlib as _ctx
    import io as _io
    _err = _io.StringIO()
    with _ctx.redirect_stderr(_err):
        _r = t.bash_write_targets('X="$(cat f)"; rm victim.md')
    check("an ordinary command CAN be untokenisable", _r == [],
          "fail-open: pinned as current behaviour, not endorsed")
    check("and an unparsed command ANNOUNCES that it went unchecked",
          "could not parse" in _err.getvalue(),
          "silence here is indistinguishable from a clean pass")
    _err2 = _io.StringIO()
    with _ctx.redirect_stderr(_err2):
        _r2 = t.bash_write_targets("cmd > 'unbalanced")
    check("the same for an unbalanced quote", _r2 == [] and "could not parse" in _err2.getvalue())
    _err3 = _io.StringIO()
    with _ctx.redirect_stderr(_err3):
        t.bash_write_targets("grep x f.py")
    check("a PARSEABLE command announces nothing", _err3.getvalue() == "",
          "the notice must mark the unchecked case, not every command")

    print("\n-- B. REGRESSION: 'changed' is a COUNT drop, not a set difference --")
    check("fully replaced atom is changed",
          t.changed_atoms("foo 188", "foo 191") == {"188"})
    check("COUNT DROP 2->1 is changed even though the atom remains",
          "188" in t.changed_atoms("version 188 build 188", "version 191 build 188"),
          "the case set-difference missed")
    check("unchanged count is NOT flagged",
          t.changed_atoms("keep 188 here", "keep 188 there") == set())
    check("added atom is not 'changed'",
          "191" not in t.changed_atoms("foo", "foo 191"))

    print("\n-- C. hunk spans and line coverage --")
    after, spans = t.apply_with_spans("aaa\nbbb\nccc\n", "bbb", "XXX", False)
    check("single replacement applied", after == "aaa\nXXX\nccc\n")
    check("hunk line identified", t.lines_covered(after, spans) == {2},
          f"lines={t.lines_covered(after, spans)}")
    after2, spans2 = t.apply_with_spans("x 1\nx 2\nx 3\n", "x", "y", True)
    check("replace_all rewrites every occurrence", after2.count("y") == 3)
    check("all hunk lines identified", t.lines_covered(after2, spans2) == {1, 2, 3})

    print("\n-- D. same-file stale sibling BLOCKS --")
    f = tmp / "doc.md"
    f.write_text("alpha 684 beta\ngamma 684 delta\n")
    rc, out, err = run_gate({"tool_name": "Edit", "session_id": "s1",
                             "tool_input": {"file_path": str(f), "old_string": "alpha 684",
                                            "new_string": "alpha 698"}}, state)
    check("exit 0 (deny travels in stdout JSON)", rc == 0, f"rc={rc}")
    check("decision is deny", decision(out) == "deny", out[:100])
    r = reason(out)
    check("names the atom and the surviving line number",
          '"684" still at line(s) 2' in r, r[:120])

    print("\n-- E. a survivor INSIDE the hunk is not flagged --")
    g = tmp / "hunk.md"
    g.write_text("version 188 build 188\nunrelated text\n")
    rc, out, err = run_gate({"tool_name": "Edit", "session_id": "s1b",
                             "tool_input": {"file_path": str(g),
                                            "old_string": "version 188 build 188",
                                            "new_string": "version 191 build 188"}}, state)
    check("deliberate in-hunk survivor allowed", decision(out) is None, out[:140])

    print("\n-- F. stale-ok on the surviving line exempts it --")
    h = tmp / "hand.md"
    h.write_text("alpha 684 beta\nolder run said 684 <!-- stale-ok: superseded -->\n")
    rc, out, err = run_gate({"tool_name": "Edit", "session_id": "s2",
                             "tool_input": {"file_path": str(h), "old_string": "alpha 684",
                                            "new_string": "alpha 698"}}, state)
    check("declared survivor does not block", decision(out) is None, out[:140])

    print("\n-- G. Write is checked too (count drop across the whole file) --")
    w = tmp / "wr.md"
    w.write_text("total 500\nalso 500\nand 500\n")
    rc, out, err = run_gate({"tool_name": "Write", "session_id": "s2b",
                             "tool_input": {"file_path": str(w),
                                            "content": "total 600\nalso 500\nand 500\n"}},
                            state)
    check("Write with a partial rewrite blocks", decision(out) == "deny", out[:140])

    print("\n-- H. pointer resolver, local and decidable only --")
    short = tmp / "short.py"
    short.write_text("one\ntwo\n")
    p = tmp / "ptr.md"
    p.write_text("placeholder\n")

    def ptr(new: str, sess: str):
        return run_gate({"tool_name": "Edit", "session_id": sess,
                         "tool_input": {"file_path": str(p), "old_string": "placeholder",
                                        "new_string": new}}, state)
    check("overshooting line number blocks", decision(ptr("see short.py:99", "s3")[1]) == "deny")
    check("line 0 blocks", decision(ptr("see short.py:0", "s3a")[1]) == "deny")
    check("resolvable pointer allowed", decision(ptr("see short.py:2", "s3b")[1]) is None)
    check("unknown file NOT blocked (undecidable, not broken)",
          decision(ptr("see nowhere_at_all.py:5", "s3c")[1]) is None)

    print("\n-- H2. a self-pointer is judged against the POST-edit text --")
    sp = tmp / "self.md"
    sp.write_text("line1\nplaceholder\n")
    rc, out, err = run_gate({"tool_name": "Edit", "session_id": "s3d",
                             "tool_input": {"file_path": str(sp),
                                            "old_string": "placeholder",
                                            "new_string": "a\nb\nc\nsee self.md:5"}}, state)
    check("pointer valid only after the edit is allowed", decision(out) is None, out[:140])

    print("\n-- I. REGRESSION: a deny must not mutate the registry --")
    proj = tmp / "proj"
    (proj / ".git").mkdir(parents=True)
    p1, p2 = proj / "one.md", proj / "two.md"
    p1.write_text("value 4242 here\nvalue 4242 again\n")
    p2.write_text("elsewhere 4242 lives\n")
    rc, out, err = run_gate({"tool_name": "Edit", "session_id": "s4", "cwd": str(proj),
                             "tool_input": {"file_path": str(p1),
                                            "old_string": "value 4242 here",
                                            "new_string": "value 5353 here"}}, state)
    check("same-file survivor blocks it", decision(out) == "deny", out[:100])
    rp = t._registry_path("s4")
    check("denied edit registered NOTHING",
          not rp.exists() or json.loads(rp.read_text()) == {})

    print("\n-- J. cross-file registers, then blocks at the reachable moment --")
    p1.write_text("value 4242 here\n")
    rc, out, err = run_gate({"tool_name": "Edit", "session_id": "s5", "cwd": str(proj),
                             "tool_input": {"file_path": str(p1),
                                            "old_string": "value 4242 here",
                                            "new_string": "value 5353 here"}}, state)
    check("cross-file staleness does NOT block this edit", decision(out) is None, out[:100])
    reg = json.loads(t._registry_path("s5").read_text())
    check("the other file was registered",
          any(Path(k).name == "two.md" for k in reg), f"keys={list(reg)}")
    check("keyed by RESOLVED absolute path (regression)",
          all(Path(k).is_absolute() for k in reg))
    p1.write_text("value 5353 here\n")
    rc, out, err = run_gate({"tool_name": "Edit", "session_id": "s5", "cwd": str(proj),
                             "tool_input": {"file_path": str(p2), "old_string": "elsewhere",
                                            "new_string": "ELSEWHERE"}}, state)
    check("editing the registered file blocks while the atom survives",
          decision(out) == "deny", out[:140])
    rc, out, err = run_gate({"tool_name": "Edit", "session_id": "s5", "cwd": str(proj),
                             "tool_input": {"file_path": str(p2),
                                            "old_string": "elsewhere 4242 lives",
                                            "new_string": "elsewhere 5353 lives"}}, state)
    check("the sweeping edit is allowed", decision(out) is None, out[:140])
    reg = json.loads(t._registry_path("s5").read_text())
    check("entry cleared once the atom is gone",
          not any(Path(k).name == "two.md" for k in reg), f"registry={reg}")

    print("\n-- K. REGRESSION: a relative file_path still matches the registry --")
    p3, p4 = proj / "three.md", proj / "four.md"
    p3.write_text("token 7171 here\n")
    p4.write_text("token 7171 also\n")
    run_gate({"tool_name": "Edit", "session_id": "s6", "cwd": str(proj),
              "tool_input": {"file_path": str(p3), "old_string": "token 7171 here",
                             "new_string": "token 8282 here"}}, state)
    cwd0 = os.getcwd()
    try:
        os.chdir(proj)
        rc, out, err = run_gate({"tool_name": "Edit", "session_id": "s6", "cwd": str(proj),
                                 "tool_input": {"file_path": "four.md",
                                                "old_string": "token",
                                                "new_string": "TOKEN"}}, state)
    finally:
        os.chdir(cwd0)
    check("relative path resolves to the registered absolute key",
          decision(out) == "deny", out[:140])

    print("\n-- L. REGRESSION: a corrupt registry is LOUD, not a silent empty one --")
    bad_sess = "s7"
    bp = t._registry_path(bad_sess)
    bp.parent.mkdir(parents=True, exist_ok=True)
    bp.write_text("{not json at all")
    d = tmp / "plain.md"
    d.write_text("hello 1234 world\n")
    rc, out, err = run_gate({"tool_name": "Edit", "session_id": bad_sess,
                             "tool_input": {"file_path": str(d), "old_string": "hello",
                                            "new_string": "HELLO"}}, state)
    check("corrupt registry announces itself on stderr",
          "registry unreadable" in err.lower(), err[:160])
    check("and still allows the edit", decision(out) is None)

    print("\n-- M. REGRESSION: internal degradation is never silent --")
    big = tmp / "big.md"
    with open(big, "w") as fh:
        fh.write("x 999\n" * 400_000)          # comfortably over the 2MB scan cap
    check("oversized file really is over the cap", big.stat().st_size > t.MAX_SCAN_BYTES,
          f"{big.stat().st_size} bytes")
    rc, out, err = run_gate({"tool_name": "Edit", "session_id": "s8",
                             "tool_input": {"file_path": str(big), "old_string": "x 999",
                                            "new_string": "x 111"}}, state)
    check("oversized file allows but says NOT GATED", decision(out) is None
          and "NOT GATED" in err, err[:160])

    print("\n-- N. things this gate deliberately does not touch --")
    rc, out, err = run_gate({"tool_name": "Bash", "session_id": "s9",
                             "tool_input": {"command": "ls"}}, state)
    check("Bash untouched", rc == 0 and out.strip() == "")
    # The governing matcher (settings.json PreToolUse) is Write|Edit|MultiEdit|NotebookEdit,
    # so NotebookEdit IS routed here. Being out of scope must therefore be ANNOUNCED, not
    # silent -- a quiet allow for a routed tool is indistinguishable from a clean pass.
    rc, out, err = run_gate({"tool_name": "NotebookEdit", "session_id": "s9b",
                             "tool_input": {"notebook_path": "x.ipynb"}}, state)
    check("NotebookEdit allows but announces it is NOT GATED",
          rc == 0 and decision(out) is None and "NOT GATED" in err, err[:160])
    p = subprocess.run([sys.executable, str(ROOT / "tier0_gate.py")],
                       input='["not", "an", "object"]', text=True, capture_output=True,
                       env=dict(os.environ, COUNCIL_STATE_ROOT=str(state)), timeout=60)
    check("non-object JSON payload does not crash", p.returncode == 0, p.stderr[:120])

    print("\n-- O. registry writes are atomic --")
    t.save_registry("s10", {"/a": ["1"]})
    check("no .tmp left behind",
          not t._registry_path("s10").with_suffix(".json.tmp").exists())
    got, ok = t.load_registry("s10")
    check("round-trips", ok and got == {"/a": ["1"]}, f"{got}")

    print("\n-- P. the kill switch, which the operator relies on to stop a blocking gate --")
    kp = tmp / "kill.md"
    kp.write_text("val 4747 one\nval 4747 two\n")
    payload = {"tool_name": "Edit", "session_id": "ks",
               "tool_input": {"file_path": str(kp), "old_string": "val 4747 one",
                              "new_string": "val 5858 one"}}
    # The fixture must actually be blockable, or every "disabled" assertion below would pass
    # for the wrong reason -- a gate that allows because it found nothing looks identical to
    # one that allows because it was switched off.
    rc, out, err = run_gate(payload, state)
    check("precondition: this edit IS blocked when the gate is on",
          decision(out) == "deny", out[:100])

    rc, out, err = run_gate(payload, state, COUNCIL_TIER0="off")
    check("COUNCIL_TIER0=off is a no-op", rc == 0 and out.strip() == "", f"{rc} {out[:80]}")
    check("and stays quiet on stderr", err.strip() == "", err[:120])
    for spelling in ("OFF", " off ", "0", "disabled"):
        rc, out, err = run_gate(payload, state, COUNCIL_TIER0=spelling)
        # rc AND err are asserted, not just empty stdout: a CRASH also produces empty
        # stdout, so checking output alone would pass for a gate that died rather than one
        # that stood down. Same void-check shape already caught twice in this project.
        check(f"{spelling!r} also disables cleanly",
              rc == 0 and out.strip() == "" and err.strip() == "",
              f"rc={rc} out={out[:40]!r} err={err[:80]!r}")
    for spelling in ("on", "1", "enabled"):
        rc, out, err = run_gate(payload, state, COUNCIL_TIER0=spelling)
        check(f"{spelling!r} keeps the gate on", decision(out) == "deny", out[:80])
    # An unrecognised value must NOT quietly disable. "false" is the obvious way to get this
    # wrong, and a gate that silently stops blocking is worse than one that never started.
    rc, out, err = run_gate(payload, state, COUNCIL_TIER0="false")
    check("an unrecognised value still gates", decision(out) == "deny", out[:80])
    check("and says so on stderr", "not recognised" in err, err[:120])
    audit = (state / "ks" / "tier0-audit.jsonl").read_text()
    check("disabling is recorded in the audit log", '"event": "disabled"' in audit)
    # The switch itself must not raise on junk -- it runs before the normal guards.
    rc, out, err = run_gate({"tool_name": "Edit", "session_id": "ks",
                             "tool_input": "not a dict"}, state, COUNCIL_TIER0="off")
    check("the disabled path survives a malformed payload", rc == 0, err[:160])

    total = PASS + FAIL
    print(f"\n{PASS}/{total} checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
