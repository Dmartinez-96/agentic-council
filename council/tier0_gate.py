#!/usr/bin/env python3
"""Tier 0: deterministic PreToolUse checks. No model, no network, blocking.

WHY THIS EXISTS. The council catches real defects, but a large share of what it caught on
2026-08-06 needed no judgement at all: a figure updated in one paragraph and not its
neighbour, a pointer that did not resolve, a count written from recall. Twelve frontier
models deliberating over whether "684" should have become "698" is the wrong instrument, and
it is not free -- every such fire spends reviewer attention that should have gone to a defect
nobody could have written a check for.

THE TWO CHECKS.

  STALE SIBLING. When an edit rewrites an atom X -> Y, X often survives elsewhere and the
  file is left contradicting itself. Same-file survivors BLOCK, and the block is deliberately
  hard to satisfy with a small hunk: the only compliant move is one edit spanning every site,
  which is standing rule 1a enforced mechanically rather than remembered.

  POINTER RESOLVER. A `path:NNN` that does not resolve is a claim about the world, and it is
  false. ONLY `path:NNN` IS CHECKED -- see unresolvable_pointers for exactly what that
  covers, which is deliberately less than a reader might assume.

COUNTING, NOT SET MEMBERSHIP, AND THIS IS THE CORE OF THE CHECK. Two earlier versions got
this wrong in different ways and both left the check unable to fire in its central case.
  - Comparing whole-file atom SETS asks "did this atom vanish from the document?" But a
    stale sibling by definition survives, so it stayed in the after-set and subtracted to
    nothing. The check could never fire. Found by the test suite, not by inspection.
  - Comparing old_string's set to new_string's set fixes that only when the atom is fully
    replaced. Rewriting `version 188 build 188` -> `version 191 build 188` leaves 188 in
    new_string, so the difference is again empty -- while other 188s in the file really do
    need updating. Found by the council.
An atom is CHANGED when the edit REDUCES ITS OCCURRENCE COUNT. 2 -> 1 is a change; unchanged
count is not. That is what both failures have in common, and counting is what fixes both.

SURVIVORS ARE SOUGHT OUTSIDE THE HUNK -- FOR `Edit`. Text the author just wrote is text they
just chose; flagging an atom they deliberately kept inside their own new_string would be
noise. Only occurrences beyond the edited span are unreviewed, so only those are reported.
A `Write` HAS NO HUNK: the whole file is new text, so there is no "outside" and every
survivor is reported. That is a real asymmetry rather than an oversight, and it means a Write
that deliberately keeps one occurrence while changing another WILL be flagged; `stale-ok:` is
how that is declared. Stated here because an earlier draft asserted the outside-the-hunk rule
unconditionally while the code passed an empty span list for Write.

CROSS-FILE STALENESS DOES NOT BLOCK THE EDIT IN FRONT OF IT. No single call spans two files,
so blocking would wedge the agent with the work half-done -- strictly worse than the defect.
The atom is REGISTERED against the other file and the next edit to THAT file blocks while it
survives. Every block is therefore one an edit can actually reach.

DECLARING AN INTENTIONAL SURVIVOR. A line carrying `stale-ok: <why>` is exempt. The
declaration lives in the text, so claiming a value is deliberately historical when it is
merely un-updated is a visible lie in the diff rather than an entry in a side-file nobody
reads. Every exemption is logged so the operator can audit the RATE.
BE HONEST ABOUT THE FALSE-POSITIVE SURFACE: historical quotation is NOT the only class. The
same number legitimately recurs as a date, a port, a size limit, an array bound, an example
value, or an unrelated constant. Those are real collisions, not defects, and `stale-ok:` is
how each is declared. If a class proves common enough, narrow the check rather than train the
agent to paper over it.

NEVER BREAKS THE EDIT PATH. Any internal error allows the edit -- a gate that bricks the
editing tool is worse than one that misses.

AND NEVER FAILS OPEN IN SILENCE, BUT BE EXACT ABOUT WHAT THAT COVERS, because an earlier
draft claimed it of everything and the code never did that. Two different things return 0:
  ANNOUNCED (via _degrade, on stderr and in the log) -- an edit this gate MODELS that it
    could not actually check: an unreadable or oversized target, a simulation failure, or a
    tool the matcher routes here whose edit model is unimplemented (NotebookEdit, MultiEdit).
    These are ungated edits, and an ungated edit that looks like a clean pass is precisely
    the absence-reads-as-approval failure this project exists to kill.
  SILENT -- input that is not an edit this gate has any business gating: a payload that is
    not JSON or not an object, a tool outside the matcher, a missing file_path, an Edit whose
    old_string does not occur -- the harness rejects that one itself, observed this session
    as "String to replace not found in file". Announcing these would print
    noise on traffic the gate was never responsible for, which trains the reader to ignore
    the channel that carries the real warnings.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# The tools whose edit model this gate actually implements. NotebookEdit is NOT here because
# a notebook's cell structure is not modelled -- but it IS routed here by the matcher, so
# main() intercepts it and announces the gap rather than passing it over quietly. Look there,
# not at this tuple, for what happens to a routed-but-unmodelled tool.
SUPPORTED_TOOLS = ("Write", "Edit")

STATE_ROOT = Path(os.environ.get("COUNCIL_STATE_ROOT", Path.home() / ".claude" / "state"))
STALE_OK_RE = re.compile(r"stale-ok\s*:", re.IGNORECASE)

# ONE ORDERED ALTERNATION, NOT SEPARATE PATTERNS. As four independent regexes "18,419"
# yielded THREE atoms -- itself plus the components "18" and "419", because a comma is not in
# [\w.] -- and registering "18" would block on nearly every line of any real document.
# Alternation is ordered and finditer resumes past the whole match, so composite shapes must
# precede the bare digit run.
# THRESHOLDS SET BY MEASUREMENT, NOT BY TASTE -- AND THE MEASUREMENT IS REPRODUCIBLE:
#   $ python3 _nogit/gate_replay.py          # GATE_PATH=<variant> to run a counterfactual
# It replays real edits out of this repo's own council logs, reconstructing each pre-edit
# state from the recorded old_string/new_string. A council review raised ground rule 12
# against an earlier form of this comment, which cited tallies with no way to re-derive them;
# the script had only ever lived in an ephemeral /tmp scratchpad, so the pointer was broken by
# construction. It lives in _nogit/ now for exactly that reason.
# HISTORICAL, AND NOT RE-DERIVABLE FROM THE CURRENT TREE: replaying 83 real edits through an
# EARLIER version of this gate blocked 11, of which ~10 were false positives. That version no
# longer exists, so read it as the reason the thresholds were raised, not as a live figure.
# The two causes it exposed:
#   DATES WERE SHREDDED. A hyphen is not in [\w.], so `2026-08-06` yielded THREE atoms --
#   2026, 08 and 06 -- and "06" occurs on a dozen lines of any document mentioning August.
#   A date is consumed WHOLE ONLY WHEN THE NEXT CHARACTER IS NEITHER A WORD CHARACTER NOR A
#   `.`, and the qualifier is load-bearing. An earlier version of this comment claimed its
#   parts "never escape"; the council refused that universal and MEASURING IT CONFIRMED THEM:
#   `2026-08-06.` and `2026-08-06T` each still yield `2026`, because the trailing `(?![\w.])`
#   fails and the token falls through to `\d{3,}`. A SENTENCE-FINAL DATE IS THE ORDINARY FORM
#   IN PROSE, so this is a live false-positive source in .md files rather than a curiosity --
#   which is consistent with .md measuring 12% against .py's 0%.
#   THE TIME PART IS INCLUDED, and omitting it was a hole the council caught the same way:
#   `2026-08-06T19:04:50Z` failed that lookahead (T is a word character) and yielded `2026`
#   -- in a repo whose logs and markers are named with ISO timestamps.
#   THE SUPPORTED GRAMMAR IS NARROWER THAN "ISO 8601" and is named here rather than implied:
#   a date, optionally followed by [T or space] HH:MM, optional :SS, optional .fff, optional
#   Z. NUMERIC OFFSETS ARE NOT CONSUMED -- measured, `2026-08-06T19:04:50+00:00` yields
#   `2026-08-06T19:04:50` -- a PREFIX, not the whole timestamp. An earlier line here called
#   that "harmless" and the atom "still whole"; both overstated it, and the consequence is
#   concrete: two timestamps differing ONLY in offset collapse to the same atom, so an
#   offset-only rewrite is invisible to this gate. It is a narrow blind spot rather than a
#   correctness bug, but it is a blind spot, and the label "ISO datetime" oversold it too.
#   SHORT NUMBERS ARE NOISE. Two-digit atoms (01, 06, 08, 11, 14, 37, 97) and trivial
#   decimals recur structurally everywhere; their survival means nothing. Bare integers now
#   need 3+ digits, and a decimal needs 3+ DIGIT CHARACTERS -- so 596.3, 2.1.223 and 10.5
#   match while 0.0 and 1.5 do not. THE THRESHOLD COUNTS DIGIT CHARACTERS, NOT SIGNIFICANT
#   FIGURES: `0.00` has three digit characters and IS matched (measured). An earlier comment
#   defended leaving that alone by claiming a tighter rule "would also exclude legitimate
#   values like 10.0" -- FALSE, and flagged independently by most of the voting bench: 10.0
#   has three significant figures and a genuine significance rule would KEEP it. The real
#   reason to leave it is different: true significance is regex-hostile, and the cheap
#   approximation that would drop `0.00` (demand a nonzero fraction digit) drops `10.0` too.
# THE CURRENT RATE, re-run 2026-08-08 by the command above over the 2026-08-07..08 log
# cohort (266 fires; 92 edits had a recoverable before-state): 10 blocked, 11% overall --
# **.py 0 of 58 (0%), .md 10 of 30 (33%)**. ALL TEN named a date atom (one also named `192`).
# THAT IS THIS GATE'S REAL WEAK POINT AND IT IS STRUCTURAL: a session-dated document
# legitimately repeats a date across separate entries, and the stale-sibling check reads
# those repeats as unswept siblings.
# DO NOT READ 12% -> 33% AS A TREND. A first draft of this comment did, and decomposing the
# cohort by day refutes it: LOG_GLOB=logs/2026-08-07/* gives .md 2 of 20; logs/2026-08-08/*
# gives .md 5 of 10; but the COMBINED run blocks 10, not 7. The cross-file registry
# accumulates atoms across a run, so the block rate is SUPER-ADDITIVE IN COHORT SIZE -- rate
# depends on how much history the run has seen, which makes two runs over different cohorts
# incomparable as a time series. The earlier 12% also came from a different gate version.
# WHAT SURVIVES THAT OBJECTION, and it is enough to act on:
#   - .py is 0% in EVERY cohort measured -- 0/58 combined, 0/48, 0/11, and 0/59 historically.
#   - .md is materially non-zero in every cohort, and every block names a date.
#   - Longer sessions block MORE, because the registry only grows. The tax rises with
#     session length rather than staying flat, which is the opposite of what a reader would
#     assume from a single percentage.
# DO NOT "fix" this by dropping dates from the atom set: that exact counterfactual was
# measured via GATE_PATH and made things WORSE (.py 0% -> 8%), because `\d{3,}` then matches
# `2026`. Any candidate fix goes through gate_replay.py before it goes in.
# The cost is real and stated: a genuine two-digit stale sibling is now missed. That is the
# right trade, because a check that fires on "06" trains its reader to ignore it -- and the
# registry made it worse, latching onto common atoms and blocking every later edit in those
# files. A check nobody can act on is worse than no check.
ATOM_RE = re.compile(
    # THE ALTERNATION IS EFFECTIVELY ATOMIC, VIA `(?=(...))\1`, AND THAT IS A BUG FIX.
    # Yesterday's fix below cured the TRAILING-PERIOD case of a defect whose root cause it named
    # but did not remove: when a long alternative matches and the trailing lookahead then rejects
    # it, the regex BACKTRACKS INTO A SHORTER ALTERNATIVE at the same start position, and the
    # shorter one passes. A period was one trigger. ANY word character is another, and that half
    # survived.
    # MEASURED 2026-08-11, and the date class was inconsistent with every other class:
    #   `2026-08-11T` -> ['2026']      `2026-08-11_v2` -> ['2026']    `2026-08-119` -> ['119','2026']
    # while the same trailing-word-char condition already yielded [] for versions (`1.2.3x`),
    # hashes (`17c5983x`) and IPs (`192.168.1.1x`). It also still TRUNCATED a grouped number:
    # `1,234,567x` -> ['1,234'], which is the same truncation the comment below records fixing
    # for a trailing period. A truncated atom is worse than none: it is a different, less
    # specific atom that matches more things.
    # HOW IT WAS FOUND, because the route matters more than the bug: a malformed timestamp typed
    # into HANDOFF prose (`2026-08-11T06:5xZ`) yielded a bare `2026`, and THE GATE DENIED THAT
    # EDIT for leaving the file's other bare `2026`s unswept. The false positive reported itself.
    # `(?>` IS AN ATOMIC GROUP: once one of the alternatives inside it matches, the engine will not
    # go back and try a different one. So when the trailing lookahead rejects that match, the
    # attempt at this start position DIES rather than falling through to something shorter.
    # IT NEEDS PYTHON 3.11 -- the CPython `re` documentation marks `(?>...)` "Added in version
    # 3.11" (fetched 2026-08-11) -- AND THIS PROJECT'S FLOOR IS 3.12: install.py's check_python
    # errors below `(3, 12)` and README declares it. IF THAT FLOOR IS EVER LOWERED BELOW 3.11,
    # THIS PATTERN MUST GO BACK to the `(?=(X))\1` form it briefly used (a lookahead captures one
    # alternative, a backreference re-consumes it, needing nothing newer than a backreference) or
    # the gate will not import at all and every PreToolUse will fail.
    # WHAT WAS NEVER VERIFIED BY RUNNING IT: that `(?>` actually raises below 3.11. Only
    # /usr/bin/python3.14 exists on this host, so the docs and the enforced floor are the
    # evidence, not an observed failure.
    # THE ATOMIC FORM IS ALSO SIMPLER THAN WHAT IT REPLACED, and the emulation's cost is worth
    # recording because it was easy to miss: that form needed a CAPTURE GROUP, and `extract_atoms`
    # calls `ATOM_RE.findall`, which returns GROUPS rather than whole matches the moment a pattern
    # has any -- so it worked only while exactly one group spanned the whole match. This form has
    # none, and A1d asserts `ATOM_RE.groups == 0` to keep it that way.
    # EITHER WAY, a date followed by a word character now matches NOTHING -- consistent with
    # `a2026-08-06` and `v1.2026`, which already matched nothing at the leading edge.
    # MEASURED ON THE REAL CORPUS, by the counterfactual this file's comment demands. Three arms of
    # `gate_replay.py` at 2026-08-11T18:07:16Z, 544 reconstructed edits of 1277 Edit fires, with
    # the ATOMIC form as the live arm and each arm's blocks enumerated rather than counted:
    #   pre-yesterday (non-atomic, old lookahead) -> 4: ['398']  grading-contract-design.md,
    #                                                  ['2026'] training-scorer-design.md,
    #                                                  ['2026'] HANDOFF.md,
    #                                                  ['3.10'] install.py
    #   yesterday     (non-atomic, new lookahead) -> 3: drops the training-scorer ['2026']
    #   live          (this atomic group)         -> 2: drops the HANDOFF ['2026'] too
    # So each fix removed exactly one real block, and both removed were BARE-YEAR stale-sibling
    # false positives on dated prose -- the class these fixes exist for. The two that SURVIVE all
    # three arms are not dates: ['398'] and ['3.10']. They are the control, and they are why the
    # remaining count is 2 rather than 0.
    # AND THE CORPUS IS NOT AN INDEPENDENT SAMPLE, which is the caveat worth carrying forward:
    # `gate_replay` reconstructs edits from the council logs, so it contains the edits made WHILE
    # diagnosing this bug -- the HANDOFF ['2026'] block is one of them, and the install.py ['3.10']
    # block is this session's own incomplete version-floor sweep, caught for real. Read a moved
    # figure here as a demonstration that the shape occurs on real prose, never as an unbiased
    # rate. The corpus also MOVES between runs -- it grows as fires land and shrinks as edits are
    # superseded, per the note further down this block -- so these figures are a snapshot rather
    # than constants. Observed here: 524 eight hours before it was 544.
    r"(?<![\w.])(?>"
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?)?"   # date +opt HH:MM[:SS][.f][Z]
    r"|\d{1,3}(?:,\d{3})+"          # 18,419
    r"|\d+\.\d+(?:\.\d+)+"          # 2.1.223 -- dotted versions of three or more parts
    r"|\d{2,}\.\d+|\d+\.\d{2,}"     # 596.3, 12.5 -- but not 0.0 or 1.5
    r"|[0-9a-f]{7,40}"              # git hashes
    r"|\d{3,}"                      # 684, 698 -- but not 06 or 97
    # A TRAILING PERIOD IS SENTENCE PUNCTUATION, NOT PART OF THE NUMBER, and the earlier
    # `(?![\w.])` could not tell the two apart. MEASURED 2026-08-10 by varying ONLY the
    # character after one date: comma, paren, semicolon, colon, newline and true end-of-string
    # all yielded `2026-08-06`, while a PERIOD yielded `2026`. The mechanism was read, not
    # guessed: the date alternative matches, fails the trailing lookahead on the `.`, the regex
    # backtracks, and `\d{3,}` takes the leading year -- which is followed by `-` and so
    # passes. That bare year then collides with every other year in the file, which is a
    # stale-sibling FALSE POSITIVE on any dated prose, and it denied a real edit.
    # THE SAME LOOKAHEAD ALSO LOST ATOMS ENTIRELY at a sentence end -- `count 1226.` -> [],
    # `hash 17c5983.` -> [], `ver 1.2.3.` -> [], `pi 3.14159.` -> [] -- so a number ending a
    # sentence had NO stale-sibling protection, a false NEGATIVE nobody would notice. And it
    # truncated two forms: `2026-08-06T19:04:50Z.` -> `2026-08-06T19:04`, `1,234,567.` ->
    # `1,234`.
    # THE RULE NOW: reject a following word character always, and reject a following `.` ONLY
    # when a word character follows it -- i.e. when the period CONTINUES a number. So
    # `192.168.1.1` still matches whole and `v1.2026` still matches nothing, while a
    # sentence-final atom survives.
    # MEASURE ANY CHANGE TO THIS LOOKAHEAD WITH gate_replay.py, as the comment block above
    # requires, and compare against the same corpus via GATE_PATH rather than against a figure
    # written here. No count is quoted on purpose: a replay only reconstructs edits whose
    # new_string is still in the file, so the cohort shrinks as edits are superseded and grows as
    # fires land, and it has been seen to change between runs minutes apart.
    # PINNED BY `_nogit/test_tier0_gate.py` SECTION A1b, which is where to look before changing
    # this lookahead again: it varies ONLY the trailing character across comma, paren,
    # semicolon, colon, newline and end-of-string, asserts the four formerly-vanishing shapes
    # and the two formerly-truncated ones, and asserts the cases that must NOT change --
    # `192.168.1.1` whole, `a2026-08-06` and `v1.2026` matching nothing.
    r")(?![\w])(?!\.\w)"
)

POINTER_RE = re.compile(r"(?<![\w/])([\w./-]+\.(?:py|md|sh|json|ts|js|txt)):(\d+)\b")

# ---------------------------------------------------------------------------
# BASH WRITES. Standing rule 12 says the shell may READ and MEASURE but may not WRITE, and
# says in the same breath that PROSE DID NOT CHANGE THE BEHAVIOUR -- it records the rule
# being broken again in the session after it was written. This is the structural form: the
# PreToolUse matcher covered Write|Edit|MultiEdit|NotebookEdit only, so `sed -i`, `>` and
# `tee` reached the disk without passing either the gate or the doorman, and nothing
# anywhere said so.
#
# THIS IS A FLOOR, NOT A CEILING, and saying so is not a hedge -- it decides how the checks
# are written. Shell is not reliably parseable by regex: `eval`, variable-expanded commands
# (`$CMD file`), an aliased writer, or a compiled binary that writes will all pass. What is
# covered is the set of constructs this project's own violations used, plus the obvious
# neighbours. A determined bypass is trivial; the target is the ABSENT-MINDED one, which is
# what the rule keeps losing to.
#
# THE FALSE-POSITIVE DIRECTION IS THE EXPENSIVE ONE. A wrong deny blocks legitimate read-only
# work, and this agent runs read-only `python3 - <<'PY'` probes constantly. So a heredoc is
# only a write if its BODY contains a write call; `2>/dev/null` and `>/dev/null` are not
# writes; and anything under a temp root is allowed outright.
# A sed SCRIPT operand, which is NOT a file: `sed -i 's/a/b/' notes.md` has two operands and
# only the second is written, but the script contains slashes, so a "looks like a path" test
# claims it.
# The delimiter set is ENUMERATED, not "any punctuation". A first version used [^\w\s] and
# was measured matching `s.txt` and `y-config.yaml` as if they were scripts, which would skip
# them as operands and silently miss a real in-place write; the enumerated form matches
# neither, while still matching `s/a/b/` and `'s|x|y|'`.
_SED_SCRIPT_RE = re.compile(r"^['\"]?[sy][/|,:;#%!@~]")
# WRAPPER FLAGS WHOSE VALUE IS A SEPARATE TOKEN. Looking through a wrapper means skipping its
# flags, but a flag's VALUE does not start with `-`, so it becomes the command word and hides
# the writer behind it. Only value-taking flags are listed; boolean ones need no entry
# because they are skipped as flags already. `--flag=value` forms need no entry either --
# they are a single token and start with `-`.
_WRAPPER_VALUE_FLAGS = {
    "sudo": {"-u", "-g", "-p", "-C", "-r", "-t", "-U", "--user", "--group", "--prompt"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "xargs": {"-n", "-P", "-I", "-d", "-s", "-a", "-E", "-L", "--max-args", "--replace"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
}
# A shell assignment token, e.g. `S=/tmp/scratch`. Used to resolve expansions that the same
# command sets for itself; see _inline_env.
_ASSIGN_RE = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.S)
_EXPANSION_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _inline_env(tokens: list[str]) -> dict[str, str]:
    """Variables this command assigns TO ITSELF, as {name: value}.

    Not the shell's environment -- this process cannot see that. Only assignments written in
    the command being judged, which is the resolvable subset: `S=/tmp/scratch; ... > $S/out`
    can be decided, while a variable exported in an earlier turn cannot and is left alone.
    """
    env: dict[str, str] = {}
    for tok in tokens:
        m = _ASSIGN_RE.match(tok)
        if m:
            env[m.group(1)] = m.group(2).strip().strip("'\"")
    return env


def _expand(target: str, env: dict[str, str]) -> str:
    """Substitute `$NAME`/`${NAME}` from `env`. Unknown names are LEFT AS THEY ARE, so the
    caller can still recognise an unresolved expansion and decline to judge it."""
    return _EXPANSION_RE.sub(
        lambda m: env.get(m.group(1) or m.group(2), m.group(0)), target or "")
# `[^\n]*` after the tag is load-bearing: a heredoc opener is routinely followed by more of
# the command on the same line (`<<'PY' 2>&1 | tail -20`). Requiring a newline immediately
# after the tag made those bodies invisible, so their contents were tokenised AS SHELL --
# measured, that is where flagged "targets" like ', ' and 'accepted' came from, lifted out of
# `->` arrows inside Python strings.
_HEREDOC_RE = re.compile(r"<<-?\s*'?\"?(\w+)'?\"?[^\n]*\n(.*?)\n\s*\1\b", re.S)
# Write calls inside an interpreter heredoc. Deliberately narrow: these are forms that
# MUTATE a file, not merely open one for reading.
_PY_WRITE_RE = re.compile(
    r"""open\s*\([^)]*['"][rbt]*[wax][rbt+]*['"]"""
    r"""|\.write_text\s*\(|\.write_bytes\s*\("""
    r"""|shutil\.(?:copy|copy2|copyfile|move)\s*\("""
    r"""|os\.(?:remove|unlink|rename|replace|truncate)\s*\("""
    r"""|\.unlink\s*\(|\.rename\s*\(""")
# Writing HERE is always fine: scratch space and the bit bucket.
_WRITE_OK_PREFIXES = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/fd/", "/tmp/",
                      "/var/tmp/", "/proc/self/fd/")


def _write_target_ok(target: str) -> bool:
    """True when a write to `target` will NOT be reported. Four cases, in order:
      - the target is empty after stripping: a trailing `>` with no operand, or `> ""`.
        (`>&2` never reaches here at all: `shlex.shlex('cmd >&2', punctuation_chars=True)`
        yields `['cmd', '>&', '2']`, so the token is `>&` and the redirect branch does not
        fire. Checked because the first draft of this line used it as the example, wrongly.)
      - it is under a temp root or is the bit bucket (_WRITE_OK_PREFIXES);
      - it contains a shell expansion this process cannot resolve -- a DELIBERATE FAIL-OPEN,
        not a judgement that the write is safe, and a broad one: `> $ANYTHING` is never
        reported, because guessing at variables this process cannot see would deny real work;
      - it is under CLAUDE_SCRATCHPAD, when that is set.
    """
    t = (target or "").strip().strip("'\"")
    if not t:
        return True
    # NORMALISED BEFORE THE PREFIX TEST. `startswith` is lexical, so `/tmp/../etc/passwd`
    # satisfied it and was allowed -- measured True before this. normpath collapses the `..`
    # so the test asks about the path that would actually be written.
    # WHAT THIS STILL DOES NOT DO: resolve SYMLINKS. A symlink under /tmp pointing outside it
    # is allowed, and establishing otherwise needs the filesystem, which this check does not
    # touch. Named as a gap rather than implied away.
    t = os.path.normpath(t)
    # THE ROOT ITSELF COUNTS, NOT ONLY WHAT IS UNDER IT. normpath strips the trailing slash,
    # so `/tmp/` becomes `/tmp` and no longer matches the `/tmp/` prefix -- measured, that
    # flipped a bare temp root from allowed to reported. Comparing against the stripped root
    # as well keeps the prefixes readable while making the test say what it means.
    # THE SAME BOUNDARY RULE AS THE SCRATCHPAD BRANCH BELOW, and it was an INCOMPLETE SWEEP
    # to fix one and not the other: entries here are a mix of slash-terminated directories
    # (`/tmp/`) and bare files (`/dev/null`), so a plain `startswith` allowed the SIBLING
    # `/dev/null_evil` -- measured True -- by exactly the mechanism just removed four lines
    # down. A match now requires the entry itself or a path separator after it.
    for _p in _WRITE_OK_PREFIXES:
        _root = _p.rstrip("/")
        if t == _root or t.startswith(_root + "/"):
            return True
    # AN UNRESOLVED EXPANSION CANNOT BE JUDGED. `> $S/out.txt` is usually a scratchpad path
    # here, but this process does not have the shell's variables and will not guess. Allowed
    # rather than denied: a wrong deny blocks real work, and this check is a floor whose gaps
    # are named rather than papered over. Real flagged examples before this: `$S/ruff_new.txt`
    # and `$SP/inspect_probe.py`.
    if "$" in t or "`" in t:
        return True
    # A PREFIX MATCH IS NOT A CONTAINMENT TEST. With a scratchpad of `/tmp/x`, a bare
    # `startswith` also allowed `/tmp/x_evil` -- a SIBLING, not a child. The boundary has to
    # be a path separator, or the directory itself.
    scratch = os.path.normpath(os.environ.get("CLAUDE_SCRATCHPAD") or "").rstrip(os.sep)
    if not scratch or scratch == ".":
        return False
    return t == scratch or t.startswith(scratch + os.sep)


def bash_write_targets(command: str) -> list[tuple[str, str]]:
    """Paths this shell command appears to WRITE, as (target, construct) pairs.

    Returns [] for a read-only command. Targets under a temp root are dropped -- they are
    writes, but not ones worth reviewing. See the block comment above for the coverage
    boundary; it is a floor, and the gaps are named there rather than implied.
    """
    import shlex
    if not command or not isinstance(command, str):
        return []
    found: list[tuple[str, str]] = []
    rest = command

    # HEREDOC BODIES ARE HANDLED FIRST AND THEN REMOVED. A heredoc body is data, not shell:
    # a `>` inside it is text, and shlex would tokenise its contents as if they were code.
    # Whether a WRITING body counts is decided further down, once the command words are
    # known: `cat <<EOF` carrying Python-looking text is data, not a program, and calling
    # that "an interpreter heredoc that writes" was an overclaim.
    heredoc_writes = False
    for _tag, body in _HEREDOC_RE.findall(command):
        if _PY_WRITE_RE.search(body):
            heredoc_writes = True
        rest = rest.replace(body, " ")

    # A NEWLINE SEPARATES COMMANDS; shlex counts it as whitespace. Without this, everything
    # after the first line of a multi-line command shares that line's command word, so
    # `echo ok\nrm victim` never sees `rm` in command position and the delete goes unreported.
    # Nearly every command this agent runs is multi-line, so the detector was blind on its
    # own dominant shape. Newlines INSIDE quotes are left alone -- they are data, and the
    # scan below tracks quote state precisely so a quoted body is not chopped into commands.
    # THREE THINGS THIS SCAN MUST GET RIGHT, and a first version got only the first:
    #   QUOTES -- a newline inside them is data. Escapes are tracked too, because a `\"`
    #   inside a double-quoted string would otherwise close the quote early and desync
    #   everything after it.
    #   LINE CONTINUATION -- `\` immediately before a newline JOINS the lines. Replacing it
    #   with ` ; ` split one command in two: `rm \<NL>victim.md` reported the stray `\` as
    #   the file to delete and missed victim.md entirely.
    #   COMMENTS -- an unquoted `#` runs to end of LINE. shlex's own comment handling runs to
    #   end of STRING, so once newlines became ` ; ` a single leading `# note` swallowed the
    #   whole command and tokenising returned NOTHING. Comments are therefore stripped here,
    #   per line, and shlex's handling is switched off below so it cannot re-swallow.
    out_chars: list[str] = []
    quote, esc, in_comment, prev = "", False, False, " "
    ansi_c = False
    for ch in rest:
        if in_comment:
            if ch == "\n":
                in_comment = False
                out_chars.append(" ; ")
            continue
        if esc:
            esc = False
            if ch == "\n":
                # LINE CONTINUATION: both characters vanish and the two lines become ONE
                # WORD where they met. `prev` therefore keeps the character BEFORE the
                # backslash -- resetting it to a space made the join look like a word
                # boundary, so `rm safe\<NL>#suffix victim.md` (which the shell reads as the
                # single word `safe#suffix`) had its `#` misread as a comment and everything
                # after it, including the real target, was discarded.
                continue
            out_chars.append("\\")
            out_chars.append(ch)
            # A SENTINEL, NOT THE CHARACTER ITSELF. An ESCAPED space is word-INTERNAL, so a
            # `#` after it is literal text, not a comment. Recording the space verbatim made
            # the word-start test fire and swallowed the rest of the line -- verified against
            # real bash, where `echo A\ # ; > victim.md` DOES create the file while the
            # detector reported nothing.
            prev = "\x00"
            continue
        if ch == "\\" and (quote != "'" or ansi_c):
            esc = True
            continue
        if quote:
            if ch == quote:
                quote = ""
                ansi_c = False
            out_chars.append(ch)
            prev = ch
            continue
        if ch in "'\"":
            quote = ch
            # `$'...'` IS ANSI-C QUOTING, where a backslash escapes -- unlike a plain
            # single-quoted string, where it is literal. Without this the `\'` in `$'it\'s'`
            # reads as the closing quote, opens a phantom quote on the real one, and swallows
            # whatever follows, including a later command.
            ansi_c = (ch == "'" and prev == "$")
            out_chars.append(ch)
            prev = ch
            continue
        # `#` opens a comment only at the start of a word -- `a#b` is an ordinary token, and
        # treating it as a comment would hide whatever followed.
        if ch == "#" and (prev.isspace() or prev in ";&|()"):
            in_comment = True
            continue
        if ch == "\n":
            out_chars.append(" ; ")
            prev = " "
            continue
        out_chars.append(ch)
        prev = ch
    rest = "".join(out_chars)

    try:
        lexer = shlex.shlex(rest, punctuation_chars=True)
        lexer.whitespace_split = True
        # Comments were already stripped per line above. shlex's own commenter runs to end of
        # STRING, and with newlines now rewritten as ` ; ` that would swallow everything after
        # the first `#`.
        lexer.commenters = ""
        toks = list(lexer)
    except ValueError as exc:
        # THE COMMAND CANNOT BE TOKENISED. This still FAILS OPEN -- denying everything this
        # module cannot parse would refuse real work for no security gain, since a deliberate
        # bypass has easier routes -- but it no longer fails SILENTLY, and that distinction is
        # the whole finding here.
        # WHY IT MATTERS MORE THAN IT LOOKS: `shlex` raises on ordinary shapes, not just
        # malformed ones. `X="$(cat f)"; rm victim.md` raises "No closing quotation", so the
        # `rm` was never seen. RE-MEASURED 2026-08-10 by feeding every Bash command in this
        # install's evidence logs through THIS function and counting the ones that print the
        # message below: 188 of 3748 UNIQUE commands, 5.0%, drawn from 4268 executions in 18 of
        # 18 log files read, with 0 unparseable log lines. WHAT THAT DOES NOT SHOW: anything
        # about the rate on another install, and nothing about whether any of those 188 was
        # trying to write at all -- the count is of commands this module cannot READ, not of
        # writes it missed. THE BRANCH IS EXERCISED AND ITS ANNOUNCEMENT IS ASSERTED -- probed
        # 2026-08-10, `bash_write_targets("cmd > 'unbalanced")` returns [] and prints the
        # message below, and _nogit/test_tier0_gate.py checks both halves for that input. Which
        # is the point: a return value alone cannot tell a fail-open apart from a clean pass,
        # and at the rate measured above that is the failure this project names as its own
        # worst -- a check indistinguishable from a passing one.
        # An UNPARSED command is now announced. It is not a deny; it is a refusal to pretend
        # the command was checked.
        # STDERR ONLY, NOT _log: this function takes a command and nothing else -- it has no
        # session_id, and reaching for one raised NameError, which would have turned a loud
        # fail-open into a crash on the first untokenisable command. The audit record belongs
        # to the caller, which owns the session.
        print(f"tier0_gate: could not parse this command, so NO write check ran for it "
              f"({type(exc).__name__}: {exc}). The gate is not saying this command is safe; "
              f"it is saying it did not look.", file=sys.stderr)
        return []

    # ASSIGNMENTS ARE COLLECTED POSITIONALLY AND IN ORDER, and both halves matter. A shell
    # only treats `NAME=value` as an assignment at a COMMAND POSITION -- in `echo S=/tmp` it
    # is an argument -- and an assignment written after a use does not apply to that use.
    # Collecting them blindly would let this module resolve an expansion the shell would NOT,
    # turning a deliberate fail-open into a confidently WRONG judgement, which is worse than
    # the gap it closes. So the walk carries the environment it has seen SO FAR, and only
    # records an assignment when a command could actually start here.
    # POSITION IS WHAT MAKES A TOKEN A COMMAND, and it governs WRITERS as well as
    # assignments. An earlier version anchored only the assignments, so `echo tee notes.md`,
    # `cp tee dest` and `echo of=x` were each read as writes -- wrong denials on a blocking
    # path, which this module names as the expensive direction. A writer counts only when it
    # IS the command word of its simple command.
    # ASSIGNMENT SCOPE follows the shell too: `S=x cmd` binds S for THAT COMMAND ONLY, while
    # `S=x; cmd` persists. `prefix` holds the former and is dropped at the separator -- unless
    # no command word ever appeared, in which case it graduates into `env`.
    # THE ALLOWLIST IS A FAIL-OPEN AND A JUDGEMENT, not a measurement: a heredoc fed to a
    # command NOT named here is treated as data and allowed, so a wrapper script is a bypass.
    # Kept that way deliberately -- `cat <<EOF` carrying example code or a test fixture is
    # ordinary, and denying it was a wrong-denial the council already caught once. Widened to
    # cover the plausible runners rather than inverted. Operator's call, 2026-08-09.
    interpreters = {"python", "python2", "python3", "pypy", "pypy3", "perl", "ruby", "node",
                    "deno", "bun", "sh", "bash", "zsh", "tclsh", "awk", "php", "uv"}
    shells = {"sh", "bash", "zsh"}
    # A WRAPPER HIDES THE REAL WRITER. `sudo tee out`, `env X=1 tee out`, `command sed -i ...`
    # all put a harmless word in command position while the writer sits behind it. The walk
    # looks THROUGH these rather than stopping at them.
    # `xargs` belongs here for the same reason as `sudo`: it puts a harmless word in command
    # position and runs the real writer behind it. `xargs sed -i 1d < list` was missed until
    # it was added, found by probing rather than by review.
    wrappers = {"sudo", "env", "nice", "command", "time", "nohup", "stdbuf", "doas", "xargs"}
    # `find -exec CMD ... ;` is the same shape with the command introduced by an OPTION
    # rather than by position, so these reopen a command start where one is not otherwise
    # expected. COMMAND POSITION IS ONLY HALF OF WHY `find . -exec sed -i 1d {} +` was
    # missed: even once `sed` is recognised, its target here is `{}` -- a placeholder, not a
    # path -- so nothing is reported unless the in-place branch also names an UNNAMED target,
    # which it now does.
    exec_flags = {"-exec", "-execdir", "-ok", "-okdir"}
    # Shell keywords are not commands and not separators, but a command DOES start after
    # them. Without this, `if true; then tee log.md; fi` never sees tee in command position.
    keywords = {"then", "else", "elif", "do", "done", "fi", "esac", "in", "{", "}", "!"}
    # `;;` terminates a case arm. Without it, `case x in a) rm f.md ;; esac` reported `;;`
    # itself as a file to be deleted. `>(` and `<(` open PROCESS SUBSTITUTIONS, which are not
    # files -- `tee >(logger)` reported both `>(` and `logger` as write targets.
    seps = (";", ";;", "&&", "||", "|", "&", "(", ")", ">(", "<(")
    env: dict[str, str] = {}
    prefix: dict[str, str] = {}
    cmd_word = ""
    at_cmd_start = True
    heredoc_interp = False
    wrapper_ctx = ""            # whose flags we are currently skipping, "" when none
    skip_wrapper_value = False
    for i, tok in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if tok in seps:
            if not cmd_word:
                env.update(prefix)      # `S=x;` standalone: persists to later commands
            prefix.clear()
            cmd_word = ""
            at_cmd_start = True
            # WRAPPER STATE IS PER-COMMAND AND MUST NOT CROSS A BOUNDARY. Measured before
            # this reset: `sudo -u; rm victim.md` reported nothing, because the pending
            # `-u` value survived the `;` and swallowed `rm` -- so the delete went
            # unreported. A missed write is the direction this detector exists to close.
            wrapper_ctx = ""
            skip_wrapper_value = False
            continue
        if tok in keywords or tok in exec_flags:
            cmd_word = ""
            at_cmd_start = True
            wrapper_ctx = ""
            skip_wrapper_value = False
            continue
        # A HEREDOC BELONGS TO THE COMMAND IT IS ATTACHED TO, not to the command line. A
        # global "an interpreter appeared somewhere" flag made `python3 x.py; cat <<EOF`
        # deny the cat heredoc -- the exact wrong-denial the surrounding comment promises
        # will not happen. The `<<` operator survives tokenisation, so the opener can be
        # bound to the command word in force when it appears.
        if tok == "<<":
            if cmd_word in interpreters:
                heredoc_interp = True
            continue
        # EVERY WRITING REDIRECT OPERATOR, not just the bare ones. `&>` and `&>>` send both
        # streams to a file and `>|` forces a clobber -- all three write, and all three
        # tokenise as their own operator, so matching only ">"/">>" let them through. Found
        # by probing the detector against write forms no test covered rather than by review.
        # `>&` is excluded on purpose: it duplicates a descriptor, it does not open a file.
        if tok in (">", ">>", "&>", "&>>", ">|"):
            found.append((_expand(nxt, {**env, **prefix}), "shell redirect"))
            continue                    # an operator is never a command word
        if at_cmd_start and _ASSIGN_RE.match(tok):
            prefix.update(_inline_env([tok]))
            continue                    # still at a command start: `A=1 B=2 cmd` is legal
        if skip_wrapper_value:
            # The VALUE of a wrapper flag. It does not start with `-`, so without this it
            # became the command word and hid the writer behind it. Measured before the fix:
            # `sudo -u root rm victim.md` and `nice -n 10 tee out.md` both reported nothing,
            # while the same commands without the flag reported correctly.
            skip_wrapper_value = False
            continue
        if at_cmd_start and tok.startswith("-"):
            if tok in _WRAPPER_VALUE_FLAGS.get(wrapper_ctx, ()):
                skip_wrapper_value = True
            continue                    # a flag of a wrapper we are looking through
        is_cmd_word = at_cmd_start
        if at_cmd_start:
            cmd_word = os.path.basename(tok.strip("'\""))
            at_cmd_start = False
            if cmd_word in wrappers:
                wrapper_ctx = cmd_word  # whose flags the next tokens belong to
                cmd_word = ""
                at_cmd_start = True     # the real command is still ahead
                continue
        scope = {**env, **prefix}
        if is_cmd_word and cmd_word == "tee":
            # EVERY operand, not the first. `tee a.txt b.txt` writes both, and stopping at
            # the first left the rest unreviewed.
            # A PROCESS SUBSTITUTION IS SKIPPED OVER, NOT STOPPED AT. `>(cmd)` is not a file,
            # but operands can follow it: treating it as a terminator made
            # `tee >(logger) real.md` report NOTHING, losing a genuine target. Measured on
            # the version before this. Nesting is tracked so the inner command's own words
            # are never mistaken for files.
            depth = 0
            for cand in toks[i + 1:]:
                if cand in (">(", "<("):
                    depth += 1
                    continue
                if cand == ")" and depth:
                    depth -= 1
                    continue
                if depth:
                    continue          # inside the substitution: a command, not a file
                if cand in seps or cand in (">", ">>", "&>", "&>>", ">|"):
                    break
                if cand.startswith("-"):
                    continue
                found.append((_expand(cand, scope), "tee"))
        elif cmd_word == "dd" and tok.startswith("of="):
            found.append((_expand(tok[3:], scope), "dd of="))
        elif is_cmd_word and cmd_word in ("cp", "mv", "install", "rm", "truncate"):
            # DESTRUCTIVE VERBS ONLY -- those that OVERWRITE or DELETE existing content.
            # Operator's scope call, 2026-08-09: `touch`, `ln`, `tar` and `curl -o` create or
            # fetch and are deliberately NOT covered, because each needs its own operand rule
            # and every added rule is another way to deny wrongly.
            # WHICH OPERAND IS WRITTEN DIFFERS BY VERB: cp/mv/install write only their LAST
            # operand (the destination) while the earlier ones are sources merely read; rm and
            # truncate act on EVERY operand. Treating them alike would report a source file as
            # though the command overwrote it.
            # THREE THINGS A NAIVE "SKIP ANYTHING STARTING WITH -" LOOP GETS WRONG, each
            # measured against the previous version before this replaced it:
            #   `--` ENDS THE OPTIONS. After it, `-weirdfile` is a FILENAME.
            #   `rm -- -weirdfile` reported [] -- the target lost entirely.
            #   `-t DEST` INVERTS THE OPERAND ORDER for cp/mv/install: the destination is the
            #   flag's value and the trailing operands are SOURCES. `cp -t /dest src.md`
            #   reported src.md, so it named a read-only source as overwritten AND missed the
            #   real destination -- wrong in both directions at once.
            #   `-r REF` for truncate names a file read only for its size. Only `-s` was
            #   skipped, so `truncate -r ref.md f.md` reported ref.md as overwritten.
            tail = []
            for a in toks[i + 1:]:
                if a in seps:
                    break
                tail.append(a)
            operands: list[str] = []
            dest = None
            end_of_opts = False
            j = 0
            while j < len(tail):
                a = tail[j]
                if not end_of_opts and a == "--":
                    end_of_opts = True
                elif not end_of_opts and a.startswith("-"):
                    if cmd_word in ("cp", "mv", "install") and a in ("-t",
                                                                     "--target-directory"):
                        if j + 1 < len(tail):
                            dest = tail[j + 1]
                        j += 2
                        continue
                    if a.startswith("--target-directory="):
                        dest = a.split("=", 1)[1]
                    elif cmd_word == "truncate" and a in ("-s", "--size",
                                                          "-r", "--reference"):
                        j += 2            # a size, or a reference read for its size
                        continue
                else:
                    operands.append(a)
                j += 1
            if dest is not None:
                targets = [dest]
            elif cmd_word in ("rm", "truncate"):
                targets = operands
            else:
                targets = operands[-1:]
            why = "rm (delete)" if cmd_word == "rm" else f"{cmd_word} (overwrite)"
            for tgt in targets:
                found.append((_expand(tgt, scope), why))
        elif tok == "-c" and nxt and cmd_word in interpreters:
            body = nxt.strip("'\"")
            if cmd_word in shells:
                # A SHELL `-c` BODY IS SHELL, so it is judged by the same walk rather than by
                # a Python-write regex that cannot match `echo x > out`. One level down is
                # enough for the absent-minded case; the body shrinks, so this terminates.
                found.extend(bash_write_targets(body))
            elif _PY_WRITE_RE.search(body):
                found.append(("(-c program)", "an inline interpreter program that writes"))
        elif is_cmd_word and cmd_word in ("sed", "perl"):
            # FLAGS AND OPERANDS ARE SEPARATED IN ONE LEFT-TO-RIGHT PASS, because a short
            # cluster can CONSUME THE NEXT TOKEN and a naive scan then reads that token as a
            # file. Two wrong denials came from getting this wrong: `sed -fi script.sed f.txt`
            # is `-f` with the attached value `i` (read-only, no in-place at all), and
            # `sed -i -f script.txt target.txt` must not report the SCRIPT as written.
            # Within a cluster the scan stops at the first argument-taking letter (e/f), so
            # the `i` in `-fi` is that flag's value rather than the in-place switch.
            inplace = False
            operands: list[str] = []
            tail = []
            for a in toks[i + 1:]:
                if a in seps:
                    break
                tail.append(a)
            j, skip = 0, False
            while j < len(tail):
                a = tail[j]
                if skip:
                    skip = False
                elif a.startswith("--"):
                    if a == "--in-place" or a.startswith("--in-place="):
                        inplace = True
                elif a.startswith("-"):
                    cluster = a[1:]
                    for ch in cluster:
                        if ch in "ef":
                            if cluster.endswith(ch):
                                skip = True     # its value is the NEXT token
                            break
                        if ch == "i":
                            inplace = True
                            break
                else:
                    operands.append(a)
                j += 1
            if not inplace:
                continue
            # AN IN-PLACE EDIT WITH NO NAMEABLE TARGET IS STILL A WRITE. `find . -exec sed -i
            # 1d {} +` names `{}`, a placeholder, and `xargs sed -i 1d < list` takes its files
            # from stdin -- in neither case does a path appear in the command at all. Fixing
            # command position alone left both reporting nothing, because the operand filter
            # found nothing that looked like a path. Reporting an unnamed target is the honest
            # answer: the flag says a file will be rewritten, and this module cannot say which.
            if not any(_SED_SCRIPT_RE.search(c) is None
                       and ("/" in c or c.endswith((".md", ".py", ".json", ".txt", ".sh",
                                                    ".toml", ".yaml", ".yml")))
                       for c in operands):
                found.append(("(target not named in the command)",
                              f"{cmd_word} -i (in-place edit)"))
                continue
            for cand in operands:
                # A SCRIPT IS NOT A FILE. `sed -i 's/a/b/' x.md` has two operands and only
                # the second is written; the script contains slashes, so a naive
                # "looks like a path" test claims it.
                if _SED_SCRIPT_RE.search(cand):
                    continue
                if "/" in cand or cand.endswith((".md", ".py", ".json", ".txt", ".sh",
                                                 ".toml", ".yaml", ".yml")):
                    found.append((_expand(cand, scope),
                                  f"{cmd_word} -i (in-place edit)"))

    # THE HEREDOC VERDICT, now that the command words are known. A writing body counts only
    # when an interpreter was actually invoked: `cat <<EOF` carrying Python-looking text is
    # data, and denying it would be a wrong deny of the most common kind.
    if heredoc_writes and heredoc_interp:
        found.append(("(heredoc body)", "an interpreter heredoc that writes"))

    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for target, why in found:
        clean = target.strip().strip("'\"")
        if (clean, why) in seen or _write_target_ok(clean):
            continue
        seen.add((clean, why))
        out.append((clean, why))
    return out

MAX_SCAN_BYTES = 2_000_000
MAX_CROSS_FILES = 400
MAX_REGISTRY_FILES = 200          # bound the registry; excess is dropped LOUDLY
MAX_REGISTRY_ATOMS = 50


def _log(session_id: str, record: dict) -> bool:
    """Append one audit record. Returns True if it was written.

    THE RETURN VALUE EXISTS FOR THE KILL SWITCH. When the gate is disabled it stays quiet by
    design, so this record is what makes "was the gate on?" answerable afterwards -- and a
    swallowed OSError would leave that state both silent and unrecorded here. Whether any
    other channel would show it has not been audited; the claim is about THIS log, not about
    uniqueness. The kill-switch caller checks the result and speaks up when it is False.
    Other call sites ignore it, which is a deliberate asymmetry rather than a rule: the
    degrade paths carry their own stderr notice, while `allow` is silent on both channels
    because a per-edit notice for the normal case is noise."""
    try:
        d = STATE_ROOT / (session_id or "_no_session")
        d.mkdir(parents=True, exist_ok=True)
        record["t"] = datetime.now(timezone.utc).isoformat()
        with open(d / "tier0-audit.jsonl", "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return True
    except OSError:
        return False


def _degrade(session_id: str, why: str, detail: str = "") -> int:
    """Allow the edit, but never quietly. Returns 0 so callers can `return _degrade(...)`.

    Every ungated edit announces itself. The alternative -- an early `return 0` -- is
    byte-identical to a clean pass from the agent's side, which is the failure this whole
    project exists to make impossible."""
    _log(session_id, {"event": "ungated", "why": why, "detail": detail})
    print(f"tier0_gate: NOT GATED -- {why}" + (f" ({detail})" if detail else ""),
          file=sys.stderr)
    return 0


def extract_atoms(text: str) -> set[str]:
    return set(ATOM_RE.findall(text or ""))


def _count(text: str, atom: str) -> int:
    """How many times `atom` occurs AS AN ATOM -- tokenised exactly as extract_atoms does.

    THE BUG THIS FIXES, and it was the engine behind most of the gate's false positives.
    The old body was an independent regex, `(?<![\\w.])2026(?![\\w.])`. A HYPHEN is in
    neither lookaround, so that pattern matched the year INSIDE every date: extract_atoms
    reads `2026-08-07` as one atom and yields no bare `2026`, while the old _count found one
    there anyway. The two functions disagreed about what a token was.
    The consequence was not cosmetic. Any region holding a genuine standalone `2026` had its
    count inflated by every nearby DATE, so an edit that merely removed some dated lines
    looked like it had rewritten `2026`, and the gate demanded a sweep of every date in the
    file. date_only() cannot suppress that, because the atom reported is `2026`, not a date.
    Matching on the alternation itself makes the two agree by construction rather than by
    two patterns being kept in step by hand.
    """
    return sum(1 for m in ATOM_RE.finditer(text or "") if m.group(0) == atom)


def has_atom(text: str, atom: str) -> bool:
    """Does `atom` occur in `text` AS AN ATOM? The membership test callers should use.

    It exists because fixing _count and survivors_in to share ATOM_RE was an INCOMPLETE
    SWEEP -- three further functions kept building the old `(?<![\\w.])ATOM(?![\\w.])` regex
    of their own, so the file still held two definitions of what a token was. The council
    caught that, and it was not theoretical: `deleted_not_rewritten` searching for the atom
    `2026` matched inside `2026-08-07`, so a dated line counted as "still carries the atom"
    and was skipped as a replacement candidate.
    THE INVARIANT IS ABOUT ATOM_RE, NOT ABOUT THIS FUNCTION, and an earlier version of this
    docstring got that wrong by claiming every membership question routes through here. It
    does not, and the split was checked by resolving each call site to its enclosing def
    rather than by reading line numbers: `deleted_not_rewritten` and `declared_exempt` call
    this, while `_count`, `survivors_in` and `cross_file_survivors` run ATOM_RE.finditer
    directly -- the last two because they tokenise a line ONCE and test many atoms against
    the result, which this single-atom signature cannot express.
    What must hold is narrower and checkable: NO PER-ATOM PATTERN IS BUILT ANYWHERE IN THIS
    MODULE, so every one of those paths tokenises identically.
    THAT CHECK LIVES IN THE SUITE (_nogit/test_tier0_gate.py, section A5, "no per-atom regex
    is constructed anywhere in tier0_gate.py") RATHER THAN HERE. A grep for the offending
    construction cannot be asserted from inside the file it searches: the comment quoting the
    search string becomes its own match, so the check reports a hit on the comment claiming
    there are none. Keeping the pattern and the searched source in different files is what
    makes the check mean anything.
    """
    return any(m.group(0) == atom for m in ATOM_RE.finditer(text or ""))


def changed_atoms(old_text: str, new_text: str) -> set[str]:
    """Atoms whose OCCURRENCE COUNT the edit reduced.

    Counting rather than set membership is the whole correctness argument -- see the module
    docstring. An atom kept the same number of times was not changed by this edit; an atom
    kept fewer times had at least one occurrence rewritten, and any that remain elsewhere in
    the file are the ones nobody has looked at."""
    out = set()
    for atom in extract_atoms(old_text):
        if _count(new_text, atom) < _count(old_text, atom):
            out.add(atom)
    return out


def apply_with_spans(before: str, old: str, new: str,
                     replace_all: bool) -> tuple[str, list[tuple[int, int]]]:
    """Apply the edit AND report where the new text landed, as (start, end) offsets.

    The spans are what let survivors be sought outside the hunk. Built here rather than by a
    separate search afterwards because locating new_string post-hoc is ambiguous when the
    same text already occurred elsewhere."""
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    cur = 0
    while True:
        i = before.find(old, pos)
        if i < 0:
            break
        parts.append(before[pos:i])
        cur += i - pos
        spans.append((cur, cur + len(new)))
        parts.append(new)
        cur += len(new)
        pos = i + len(old)
        if not replace_all:
            break
    parts.append(before[pos:])
    return "".join(parts), spans


def lines_covered(text: str, spans: list[tuple[int, int]]) -> set[int]:
    """1-based line numbers touched by any span."""
    if not spans:
        return set()
    starts = [0]
    for ln in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(ln))
    covered: set[int] = set()
    for s, e in spans:
        for idx in range(len(starts) - 1):
            if starts[idx] < e and starts[idx + 1] > s:
                covered.add(idx + 1)
    return covered


def survivors_in(text: str, atoms: set[str],
                 skip_lines: set[int] | None = None) -> dict[str, list[int]]:
    """Line numbers where each atom survives, skipping the hunk and declared exemptions."""
    found: dict[str, list[int]] = {}
    if not atoms:
        return found
    skip = skip_lines or set()
    # TOKENISED THE SAME WAY AS extract_atoms AND _count, for the reason spelled out at
    # _count: a per-atom regex with `(?![\w.])` treats a hyphen as a boundary, so searching
    # for `2026` used to "find" it inside `2026-08-07` and report a survivor on every dated
    # line in the file. Deriving the line's atoms from the shared alternation removes the
    # possibility of the two disagreeing.
    for i, line in enumerate(text.splitlines(), 1):
        if i in skip or STALE_OK_RE.search(line):
            continue
        present = {m.group(0) for m in ATOM_RE.finditer(line)}
        for atom in atoms:
            if atom in present:
                found.setdefault(atom, []).append(i)
    return found


# A BARE CALENDAR DATE, and ONLY that: `2026-08-06` but not `2026-08-06T19:04:50Z`.
# A datetime carries seconds and is effectively unique, so a surviving one really is a
# sibling worth reporting; a bare date repeated across a dated document is not.
BARE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def date_only(atoms) -> bool:
    """True when EVERY atom in `atoms` is a bare YYYY-MM-DD.

    RE-RUN `python3 _nogit/gate_replay.py` FOR CURRENT FIGURES. The numbers below are a
    snapshot of a MOVING cohort, not properties of the gate: the cohort is this repo's own
    council logs, so it grows while the work proceeds, and the same measurement taken twice
    in one session gave 92 then 103 recoverable edits with the rates moving too.
    AS MEASURED 2026-08-08 over the 2026-08-07..08 cohort (103 recoverable edits): 14 blocked
    -- .md 13 of 33, .py 1 of 66. Of the 14 trigger sets, seven named nothing but dates and
    seven also named `192`.
    The cause is structural rather than a mis-set threshold: a session-dated document
    legitimately repeats `2026-08-07` across separate entries, so the stale-sibling check
    reads ordinary repetition as an unswept sweep.
    DISTINGUISH THIS FROM THE COUNTERFACTUAL THAT FAILED: dropping dates from the ATOM SET
    was measured via GATE_PATH and made things WORSE, because `\\d{3,}` then matches `2026`.
    Dates remain atoms here and remain available to every other check; the only change is
    that a date CANNOT CARRY A BLOCK ALONE. A mixed trigger still blocks -- `['192',
    '2026-08-07']` contains a non-date -- as does a git-hash sibling.
    WHAT THIS DOES NOT FIX, measured the same day: a NON-date false positive. Deleting a line
    that merely contains an atom drops its occurrence count, which `changed_atoms` cannot
    distinguish from a rewrite -- removing one `[:400]` slice from doorman.py was blocked
    against two unrelated `text[:400]` constants. That class is untouched by this rule.
    The user's ruling, 2026-08-08, chosen over scoping the gate by file extension.
    """
    return bool(atoms) and all(BARE_DATE_RE.match(a) for a in atoms)


REWRITE_SIMILARITY = 0.6


def deleted_not_rewritten(old_text: str, new_text: str, atom: str) -> bool:
    """True when `atom` vanished because its CONTEXT WAS REMOVED, not because it was
    replaced by a new value.

    THE BUG THIS CLOSES. `changed_atoms` works on occurrence counts, which is right for its
    own job but cannot tell a REWRITE from a DELETION: both drop the count. So deleting a
    line that merely happened to contain a number made the gate announce "this edit rewrites
    ['400']" and demand a sweep of every unrelated `400` in the file. Observed on incidental
    slice constants; the mechanism by which that reached the operator is not recorded here.
    HOW IT DISCRIMINATES. A rewrite leaves a NEAR-VARIANT of the original line behind: the
    surrounding text survives and only the value moves. A deletion leaves nothing resembling
    it. So: take the old lines holding the atom, and look for a new line that resembles one
    of them but does NOT contain the atom. Found -> TREATED AS a rewrite; absent -> TREATED
    AS a deletion. Those are classifications by a heuristic, not facts about the edit, and
    each is wrong sometimes -- see the failure directions below.
    A CANDIDATE REPLACEMENT MUST ITSELF BE NEW, and omitting that check reintroduced the
    exact false positive this function exists to remove -- found by the council, then
    reproduced: delete `timeout = 400` while an unrelated `timeout = 300` survives, and the
    survivor scores well above REWRITE_SIMILARITY against the deleted line, so it was read as
    the replacement and the atom stayed in `changed`.
    "NEW" IS COUNTED, NOT SET MEMBERSHIP. A set said "already present" even when the edit
    added ANOTHER copy, so rewriting `x = 400` into a second `x = 300` beside an existing one
    was read as a deletion and a genuine surviving `400` went unreported -- also the
    council's find. A line is evidence of a rewrite only if this edit left MORE of it than
    there were before.
    TWO FAILURE DIRECTIONS:
      - Below REWRITE_SIMILARITY a real rewrite reads as a deletion -> its siblings go
        unreported. FAIL-OPEN: silence, never a wrong block. PINNED by a test in
        _nogit/test_tier0_gate.py section A4.
      - A newly-added line that coincidentally resembles the deleted one reads as a
        replacement -> the atom is kept and may block. FAIL-CLOSED, and the worse of the two.
        The must-be-new rule does NOT prevent this one: it only excludes lines that already
        existed, and this case is by definition a line that did not. Also PINNED in A4, along
        with a check that the multiplicity rule does not rescue it.
    WHAT IT DOES NOT FIX, same family: an incidental constant genuinely rewritten in place
    (120 -> 140) still reports every unrelated 120. That needs semantics this gate lacks.
    The 0.6 threshold is a judgement, not a measurement; it is named so it can be tuned.
    Run `_nogit/gate_replay.py` after touching it.
    """
    from difflib import SequenceMatcher
    olds = [ln for ln in old_text.splitlines() if has_atom(ln, atom)]
    if not olds:
        return False

    def _line_counts(text: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for line in text.splitlines():
            stripped = line.strip()
            counts[stripped] = counts.get(stripped, 0) + 1
        return counts

    old_counts, new_counts = _line_counts(old_text), _line_counts(new_text)
    for old_line in olds:
        for new_line in new_text.splitlines():
            if has_atom(new_line, atom):
                continue          # still carries the atom: not the replacement we want
            stripped = new_line.strip()
            if new_counts.get(stripped, 0) <= old_counts.get(stripped, 0):
                continue          # no more of this line than before: this edit did not add it
            if SequenceMatcher(None, old_line.strip(),
                               stripped).ratio() >= REWRITE_SIMILARITY:
                return False
    return True


def declared_exempt(text: str, atoms: set[str]) -> set[str]:
    """Atoms appearing on AT LEAST ONE `stale-ok:` line.

    Not "only on exempt lines" -- an earlier docstring claimed that and the code never
    implemented it. This is used for logging the exemption rate, so at-least-one is the
    quantity that matters anyway."""
    out = set()
    for line in text.splitlines():
        if not STALE_OK_RE.search(line):
            continue
        for atom in atoms:
            if has_atom(line, atom):
                out.add(atom)
    return out


def unresolvable_pointers(new_text: str, base: Path,
                          self_path: Path | None = None,
                          self_text: str | None = None) -> list[str]:
    """`path:NNN` references that provably do not resolve.

    WHAT THIS COVERS, stated exactly because the previous docstring implied more: only
    `path:NNN` against a file that EXISTS. It does not check section anchors, PR numbers or
    ticket IDs -- without an authoritative store a block cannot tell "pointer broken" from
    "store unreachable", and blocking an edit for the second is a defect of its own. A
    pointer whose file cannot be found is likewise left alone: that is undecidable here, and
    more often prose than a reference.

    A POINTER INTO THE FILE BEING EDITED is measured against the POST-EDIT text. Checking it
    against what is still on disk would judge the reference by a version the edit is about to
    replace, which is wrong in both directions."""
    bad = []
    for ref, num in POINTER_RE.findall(new_text or ""):
        try:
            line_no = int(num)
        except ValueError:
            continue
        if line_no <= 0:
            bad.append(f"{ref}:{line_no} -- there is no line {line_no}")
            continue
        cands = [base / ref, Path.cwd() / ref, Path(ref)]
        target = next((c for c in cands if c.exists() and c.is_file()), None)
        if target is None:
            continue
        try:
            if self_path is not None and self_text is not None and \
                    target.resolve() == self_path.resolve():
                n = len(self_text.splitlines())
            else:
                if target.stat().st_size > MAX_SCAN_BYTES:
                    continue
                n = len(target.read_text(errors="replace").splitlines())
        except OSError:
            continue
        if line_no > n:
            bad.append(f"{ref}:{line_no} -- file has only {n} lines")
    return bad


SCAN_SUFFIXES = (".py", ".md", ".sh", ".json", ".ts", ".js", ".txt", ".toml", ".yaml", ".yml")
SKIP_DIRS = {".git", "node_modules", "__pycache__", "logs", ".venv", "venv", "_brain"}
# Basenames of append-only historical records. Consumed by cross_file_survivors(), whose walk
# explains what the exemption is and is not; keep the rationale there rather than duplicating it.
ARCHIVAL_BASENAMES = {"HANDOFF.md", "Noumad-harness-todo.md"}


def scan_root_for(path: Path, cwd: str) -> Path | None:
    """Where a cross-file sweep should look: the edited file's repo, else its directory.

    `logs/` is skipped for a specific reason -- this repo's council logs QUOTE the agent's own
    prose verbatim, so finding a figure there is the agent's own text echoing back, not a
    stale sibling. Registering that would manufacture work out of an echo."""
    try:
        p = path.resolve().parent
        for cand in (p, *p.parents):
            if (cand / ".git").exists():
                return cand
        return Path(cwd).resolve() if cwd else p
    except OSError:
        return None


def cross_file_survivors(root: Path | None, edited: Path,
                         atoms: set[str]) -> tuple[dict, dict]:
    """Files other than the edited one where a changed atom still appears.

    Returns (mapping, stats). STATS ARE NOT DECORATION: a sweep that skipped files it could
    not read, or stopped at a cap, has not checked what it appears to have checked, and
    reporting completeness it does not have is the failure this project keeps finding.
    `truncated` is tracked as a FLAG rather than inferred from `scanned == cap`, because
    finishing with exactly cap files and stopping at cap are different events that a count
    alone cannot distinguish."""
    out: dict[str, list[str]] = {}
    stats = {"scanned": 0, "skipped_unreadable": 0, "skipped_large": 0, "truncated": False,
             "skipped_archival": 0}
    if not atoms or root is None:
        return out, stats
    try:
        edited_res = edited.resolve()
    except OSError:
        edited_res = edited
    # has_atom, not a per-atom regex: same tokenisation as everywhere else in this module.
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(SCAN_SUFFIXES):
                continue
            # APPEND-ONLY HISTORICAL RECORDS ARE SKIPPED HERE AND NOWHERE ELSE. The distinction
            # is between a document whose old values should PROPAGATE when they change and one
            # whose old values ARE the content. In code, an atom that moved in one file and
            # survived in another is the staleness this gate exists to catch; in a dated log of
            # past sessions, every superseded figure is deliberately kept, and "correcting" them
            # would destroy the record.
            # THE CASE THAT PRODUCED THIS, 2026-08-11: appending a section to HANDOFF.md became
            # impossible. 20 of its lines carried atoms -- past dates, a quoted replay result --
            # rewritten elsewhere in the same session, so every APPEND was denied over history
            # that was correct. The per-line `stale-ok:` escape does not fit: it marks
            # exceptions, and here the whole file is the exception.
            # THIS IS THE OTHER-FILES WALK ONLY. The same-file stale-sibling check runs elsewhere
            # and is untouched, so rewriting an atom in HANDOFF.md while leaving its sibling two
            # lines down is still caught. And the skip is COUNTED, because a sweep that quietly
            # declined to read a file would report coverage it does not have.
            if fn in ARCHIVAL_BASENAMES:
                stats["skipped_archival"] += 1
                continue
            if stats["scanned"] >= MAX_CROSS_FILES:
                stats["truncated"] = True
                return out, stats
            fp = Path(dirpath) / fn
            try:
                if fp.resolve() == edited_res:
                    continue
                if fp.stat().st_size > MAX_SCAN_BYTES:
                    stats["skipped_large"] += 1
                    continue
                text = fp.read_text(errors="replace")
            except OSError:
                stats["skipped_unreadable"] += 1
                continue
            stats["scanned"] += 1
            # TOKENISE EACH LINE ONCE, not once per atom -- the same shape survivors_in
            # uses. A first version of this loop called has_atom(line, atom) for every atom,
            # which re-ran ATOM_RE over the line N times. MEASURED over this repo's 158
            # scannable files with 6 atoms, I/O excluded: old per-atom regex 252 ms,
            # per-atom has_atom 717 ms, this form 152 ms. So the correctness fix had made
            # this walk ~3x slower than the buggy version it replaced, and tokenising once
            # makes it faster than either. The hit counts also differ -- 27 for the old
            # regex against 24 here -- and that gap IS the hyphen bug: the old pattern
            # matched `2026` inside `2026-08-07`.
            hit: set[str] = set()
            for ln in text.splitlines():
                if STALE_OK_RE.search(ln):
                    continue
                hit |= atoms & {m.group(0) for m in ATOM_RE.finditer(ln)}
                if len(hit) == len(atoms):
                    break            # nothing further can be learned from this file
            hits = sorted(hit)
            if hits:
                out[str(fp.resolve())] = sorted(hits)
    return out, stats


def _registry_path(session_id: str) -> Path:
    return STATE_ROOT / (session_id or "_no_session") / "tier0-registry.json"


def load_registry(session_id: str) -> tuple[dict, bool]:
    """Returns (registry, ok). ok=False means it existed but could not be used as-is.

    A CORRUPT REGISTRY IS NOT AN EMPTY ONE. Silently returning {} would discard every pending
    cross-file sweep and report a clean slate -- absence reading as approval, again. The
    caller announces it.

    ENTRY VALUES ARE VALIDATED, NOT JUST THE ROOT. Checking only `isinstance(data, dict)`
    left a malformed value (a string where a list belongs) to raise later inside `len(v)` or
    iteration -- and since nothing repaired it, that store would wedge every subsequent fire
    in this session. Bad entries are dropped here, once, and reported."""
    p = _registry_path(session_id)
    if not p.exists():
        return {}, True
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    clean: dict[str, list[str]] = {}
    dropped = 0
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v):
            clean[k] = v
        else:
            dropped += 1
    if dropped:
        print(f"tier0_gate: dropped {dropped} malformed registry entr"
              f"{'y' if dropped == 1 else 'ies'}; their sweeps will NOT be enforced.",
              file=sys.stderr)
    return clean, dropped == 0


def save_registry(session_id: str, reg: dict) -> bool:
    """Atomic write, bounded. Returns True on success. A half-written registry read by the
    next fire is worse than none, so tmp+os.replace makes a reader see either the old file
    or the new one.

    EVERY LOSS HERE IS ANNOUNCED, and the earlier version got this wrong twice: it claimed to
    drop the OLDEST entries while `list(reg)[CAP:]` actually drops the NEWEST (dicts preserve
    insertion order), and it truncated each file's atom list in silence. Both are sweeps that
    will not be enforced, which is exactly the class of thing that must never be quiet."""
    try:
        if len(reg) > MAX_REGISTRY_FILES:
            for k in list(reg)[MAX_REGISTRY_FILES:]:
                reg.pop(k, None)
            print(f"tier0_gate: registry exceeded {MAX_REGISTRY_FILES} files; the most "
                  f"RECENTLY added entries were dropped and their sweeps will NOT be "
                  f"enforced.", file=sys.stderr)
        for k, v in list(reg.items()):
            if len(v) > MAX_REGISTRY_ATOMS:
                print(f"tier0_gate: {k} had {len(v)} pending atoms; kept the first "
                      f"{MAX_REGISTRY_ATOMS}, the rest will NOT be enforced.",
                      file=sys.stderr)
                reg[k] = v[:MAX_REGISTRY_ATOMS]
        p = _registry_path(session_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(reg, indent=2))
        os.replace(tmp, p)
        return True
    except OSError as e:
        print(f"tier0_gate: could not persist the registry ({e}); cross-file sweeps "
              f"recorded by this edit will NOT be enforced.", file=sys.stderr)
        return False


def emit_deny(reason: str) -> int:
    """PreToolUse denies via structured stdout at exit 0, NOT a non-zero exit -- the contract
    laziness_gate.py uses. Getting this wrong fails open silently."""
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, sys.stdout)
    sys.stdout.write("\n")
    return 0


BASH_GATE_MODES = ("warn", "deny", "off")


def bash_gate(session_id: str, tool_input: dict) -> int:
    """Standing rule 12's structural half: the shell may READ and MEASURE, it may not WRITE.

    THREE MODES, AND THE DEFAULT IS THE WEAK ONE ON PURPOSE. `COUNCIL_BASH_GATE=warn` (the
    default) reports and lets the command run; `deny` refuses it; `off` skips the check.
    Warn-first is the OPERATOR'S DECISION -- not a council recommendation and not this
    module's own preference, and the distinction matters because the standing rules forbid
    recording the former as the latter. The reasoning he was given: successive review rounds
    each found real defects in this detector, several of them in the repairs for earlier
    ones, so it should earn `deny` from its own audit log rather than from anyone's
    confidence in it.
    AN UNRECOGNISED VALUE IS LOUD, because a misread switch that silently behaves as `warn`
    is indistinguishable from a working one -- the same reasoning as COUNCIL_TIER0.
    IT NEVER BREAKS THE SHELL, and the whole body sits inside the guard for that reason. An
    earlier version wrapped only the detector call, leaving the logging, the message
    formatting and emit_deny able to raise straight through main() and into the operator's
    terminal. A guard that can wedge a shell gets uninstalled within the hour, so the promise
    has to cover everything the guard does, not merely the part most likely to fail.
    """
    try:
        return _bash_gate_inner(session_id, tool_input)
    except Exception as e:  # noqa: BLE001
        # Last resort. Even the announcement is guarded: if stderr itself is broken there is
        # nowhere left to complain to, and the command must still run.
        try:
            print(f"tier0_gate: the shell-write guard FAILED ({type(e).__name__}: {e}); this "
                  f"command was NOT checked for writes.", file=sys.stderr)
            _log(session_id, {"event": "bash_check_error", "err": repr(e)})
        except Exception:  # noqa: BLE001
            pass
        return 0


def _bash_gate_inner(session_id: str, tool_input: dict) -> int:
    """The body of bash_gate, separated so ONE try in the caller covers all of it."""
    mode = os.environ.get("COUNCIL_BASH_GATE", "").strip().lower() or "warn"
    if mode not in BASH_GATE_MODES:
        print(f"tier0_gate: COUNCIL_BASH_GATE={mode!r} is not recognised; treating it as "
              f"'warn'. Valid: {', '.join(BASH_GATE_MODES)}.", file=sys.stderr)
        mode = "warn"
    if mode == "off":
        return 0
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return 0
    writes = bash_write_targets(command)
    if not writes:
        return 0
    detail = "; ".join(f"{tgt} [{why}]" for tgt, why in writes)
    _log(session_id, {"event": "bash_deny" if mode == "deny" else "bash_would_deny",
                      "mode": mode, "targets": [t for t, _ in writes],
                      "constructs": sorted({w for _, w in writes}),
                      "command": command[:400]})
    # THE MESSAGE MUST NOT CONTRADICT THE FACT OF ITS OWN EXISTENCE. An earlier wording said
    # the write "reaches disk without passing this gate ... and NOTHING RECORDS that it went
    # unreviewed" -- untrue on the only path that can emit it: the `_log` call directly above
    # has just ATTEMPTED a bash_deny/bash_would_deny record, and `emit_deny` below stops the
    # command outright in deny mode.
    # TWO PRECISIONS THE COUNCIL EXTRACTED, both the same class as the defect being fixed:
    # `_log` swallows OSError, so it attempts rather than guarantees persistence; and this is
    # NOT the only shell-write check on the machine -- `scripted_write_guard.py` holds the
    # Bash matcher today and warns on the same shapes. Claiming exclusivity would be true
    # only after that guard is retired, which has not happened.
    reason = (
        f"SHELL WRITE: this command appears to write {detail}. Standing rule 12 -- the shell "
        f"may READ and MEASURE but may not WRITE. The gap is specific: a file changed from "
        f"the shell never reaches the DOORMAN or the COUNCIL, because both run on the "
        f"Write/Edit tools. It lands with no verdict, and no verdict looks exactly like a "
        f"clean one. Use Write/Edit for anything they should see. If this is a temp-file "
        f"write the check misread, set COUNCIL_BASH_GATE=off for the run and say so, rather "
        f"than working around it silently."
    )
    if mode == "deny":
        return emit_deny(reason)
    print(f"tier0_gate: WOULD DENY (COUNCIL_BASH_GATE=warn) -- {detail}\n"
          f"  The command is running anyway. {reason}", file=sys.stderr)
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0                     # valid JSON of the wrong shape: not ours to gate
    tool_name = payload.get("tool_name", "")
    # TYPE-CHECKED BEFORE THE KILL SWITCH USES IT. `or {}` catches None and {} but passes a
    # TRUTHY non-dict straight through, and the disabled path below then calls .get on it --
    # so a malformed payload would make the kill switch itself raise instead of being the
    # reliable no-op it is meant to be. This exact defect was fixed in doorman.py and not
    # carried here; the sweep is the point, not the individual fix.
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    session_id = payload.get("session_id", "")

    # KILL SWITCH. This gate BLOCKS, and its false-positive rate on real prose is not yet
    # measured, so there has to be a way to stop it that does not involve editing a live
    # settings.json shared with running sessions. `COUNCIL_TIER0=off` makes it a no-op.
    # DISABLING IS RECORDED WHERE POSSIBLE, AND SAID ALOUD WHEN IT IS NOT. _log swallows
    # OSError, so "it lands in the audit log" is not a guarantee -- an unwritable state
    # directory would make the gate both silent and unrecorded, which is the one combination
    # this whole project treats as unacceptable. When the record fails, it prints instead.
    # ROUTINE DISABLING IS OTHERWISE QUIET, because the operator set this deliberately and a
    # notice on every edit trains a reader to ignore the channel carrying real warnings.
    # AN UNRECOGNISED VALUE IS ALWAYS LOUD: `COUNCIL_TIER0=false` silently behaving as ON is
    # exactly the gap between what someone intended and what they got.
    # NOTE THE COUPLING: this returns before the doorman is consulted AND before the Bash
    # routing below, so COUNCIL_TIER0=off disables ALL THREE -- the deterministic file checks,
    # the doorman, and the shell-write guard. It is a single control for THIS PROCESS, not an
    # oversight -- but it does NOT silence the PreToolUse tier, and calling it that would
    # mislead an operator into thinking a quiet terminal meant nothing was checking. The
    # matcher (read 2026-08-09) also registers `laziness_gate.py` on the edit tools and
    # `scripted_write_guard.py` on Bash; both are separate processes with their own logic and
    # neither reads this variable. What is lost here is only the ability to keep the free
    # checks while silencing the model call.
    # THIS ENUMERATION WENT STALE ONCE ALREADY: the Bash routing was added below this point
    # without updating the list, so the comment named two of the three things the switch
    # silences. Anything else routed below here belongs in this sentence too.
    switch = os.environ.get("COUNCIL_TIER0", "").strip().lower()
    if switch in ("off", "0", "disabled"):
        if not _log(session_id, {"event": "disabled", "via": "COUNCIL_TIER0",
                                 "file": tool_input.get("file_path", "")}):
            print("tier0_gate: DISABLED via COUNCIL_TIER0, and the audit record could not "
                  "be written -- this edit is ungated with no durable trace.",
                  file=sys.stderr)
        return 0
    if switch not in ("", "on", "1", "enabled"):
        print(f"tier0_gate: COUNCIL_TIER0={switch!r} is not recognised; the gate is "
              f"treating it as ON. Use 'off' to disable.", file=sys.stderr)
    if not isinstance(tool_input, dict):
        return 0
    # ROUTED HERE BUT NOT MODELLED. The matcher registering THIS gate is
    # `Write|Edit|MultiEdit|NotebookEdit` (re-read 2026-08-09), so registering it there
    # routes NotebookEdit to it -- and this gate does not model a notebook's cell structure.
    # THE SETTINGS FILE HAS MORE PreToolUse ENTRIES THAN THIS ONE, and an earlier version of
    # this comment implied otherwise by naming only this matcher: the same read also shows a
    # `Bash` matcher running `scripted_write_guard.py`, and `laziness_gate.py` alongside this
    # gate on the edit tools. Re-read the file rather than trusting this sentence -- it is a
    # pointer at an operator-editable file, which is the shape that goes stale.
    # Returning 0 quietly would make every notebook edit ungated and INDISTINGUISHABLE from
    # a clean pass, which is the exact failure this gate exists to prevent. So it announces
    # the gap instead. MultiEdit is handled the same way because the matcher NAMES it (that
    # much is checkable in settings.json); whether this harness actually offers the tool is
    # not something this module can establish, and an earlier version of this comment
    # asserted its absence as fact. If it does not exist the branch is simply unreachable,
    # which costs nothing; if it appears later, it degrades loudly instead of silently.
    # BASH IS A DIFFERENT CHECK ENTIRELY -- no file_path, no edit to simulate, no atoms. It
    # asks one question: does this command WRITE? Handled before the file-edit path because
    # Bash is not in SUPPORTED_TOOLS and would otherwise fall straight through to `return 0`.
    if tool_name == "Bash":
        return bash_gate(session_id, tool_input)
    if tool_name in ("NotebookEdit", "MultiEdit"):
        return _degrade(session_id, f"{tool_name} is routed to this gate but its edit "
                                    f"model is not implemented", str(tool_input)[:120])
    if tool_name not in SUPPORTED_TOOLS:
        return 0
    raw_path = tool_input.get("file_path") or ""
    if not raw_path:
        return 0
    path = Path(raw_path)

    # ---- read the file and simulate the edit -------------------------------------------
    try:
        if tool_name == "Write":
            before = ""
            if path.exists():
                if path.stat().st_size > MAX_SCAN_BYTES:
                    return _degrade(session_id, "file exceeds scan cap", str(path))
                before = path.read_text(errors="replace")
            after = tool_input.get("content") or ""
            spans: list[tuple[int, int]] = []
            # A DELETED ATOM IS NOT A REWRITTEN ONE -- see deleted_not_rewritten().
            changed = {a for a in changed_atoms(before, after)
                       if not deleted_not_rewritten(before, after, a)}
        else:
            old = tool_input.get("old_string") or ""
            new = tool_input.get("new_string") or ""
            if not path.exists():
                return 0             # the tool will fail on its own; nothing to gate
            if path.stat().st_size > MAX_SCAN_BYTES:
                return _degrade(session_id, "file exceeds scan cap", str(path))
            before = path.read_text(errors="replace")
            if not old or old not in before:
                return 0             # the tool itself will reject this
            after, spans = apply_with_spans(before, old, new,
                                            bool(tool_input.get("replace_all")))
            # Same filter as the Write branch. Here it sees the HUNK (old vs new) rather
            # than the whole file -- a narrower scope, NOT an exclusive one: the Write branch
            # above applies the same two functions to before/after.
            changed = {a for a in changed_atoms(old, new)
                       if not deleted_not_rewritten(old, new, a)}
    except OSError as e:
        return _degrade(session_id, "could not read the target file", f"{path}: {e}")
    except Exception as e:  # noqa: BLE001
        return _degrade(session_id, "edit simulation failed",
                        f"{path}: {type(e).__name__}: {e}")

    reasons: list[str] = []
    pending_clear: str | None = None
    pending_register: dict[str, list[str]] = {}
    stats: dict = {}
    reg: dict = {}
    try:
        hunk = lines_covered(after, spans)
        same_file = survivors_in(after, changed, skip_lines=hunk)
        exempt = declared_exempt(after, changed)
        if exempt:
            _log(session_id, {"event": "exempt", "file": str(path),
                              "atoms": sorted(exempt)})
        if same_file and date_only(same_file):
            # A DATE CANNOT CARRY A BLOCK BY ITSELF -- see date_only() for the measurement
            # and for the failed counterfactual this is NOT.
            # ANNOUNCED, NEVER SILENT: an edit that skips a check and says nothing is
            # indistinguishable from a gate that never ran, which is the exact failure this
            # file exists to prevent.
            print(f"tier0_gate: NOT BLOCKING {path.name} -- every surviving atom is a bare "
                  f"date ({sorted(same_file)}). Dates repeat legitimately across entries of "
                  f"a dated document, so they no longer block alone. Check them yourself if "
                  f"this sweep actually mattered.", file=sys.stderr)
            _log(session_id, {"event": "date_only_suppressed", "check": "same_file",
                              "file": str(path), "atoms": sorted(same_file)})
        elif same_file:
            detail = "; ".join(
                f'"{a}" still at line(s) {", ".join(map(str, ls))}'
                for a, ls in sorted(same_file.items())
            )
            reasons.append(
                f"STALE SIBLING in {path.name}: this edit rewrites {sorted(same_file)} "
                f"but {detail}. Widen old_string/new_string so ONE edit covers every site "
                f"(standing rule 1a). If a survivor is intentional -- a date, a port, an "
                f"unrelated constant, a deliberate historical quotation -- mark that line "
                f"`stale-ok: <why>`."
            )

        bad = unresolvable_pointers(
            tool_input.get("new_string") or tool_input.get("content") or "",
            path.parent, self_path=path, self_text=after)
        if bad:
            reasons.append("POINTER DOES NOT RESOLVE: " + "; ".join(bad))

        reg, reg_ok = load_registry(session_id)
        if not reg_ok:
            print("tier0_gate: registry unreadable; pending cross-file sweeps are LOST and "
                  "will NOT be enforced this session.", file=sys.stderr)
            _log(session_id, {"event": "registry_corrupt"})
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        entry = reg.get(key) or []
        # THE ARCHIVAL EXEMPTION APPLIES IN BOTH DIRECTIONS, and this is the one that matters for
        # an append-only record. The walk in cross_file_survivors() skips these files when looking
        # for survivors ELSEWHERE; this check is the reverse -- atoms rewritten elsewhere earlier
        # that survive HERE -- and it is the direction that made HANDOFF.md unappendable: a dated
        # log accumulates other files' superseded values BY DESIGN, so every APPEND was denied
        # over history that was correct.
        # STILL FULLY CHECKED: the same-file stale-sibling rule, which is what catches an edit
        # that rewrites an atom in this file while leaving its sibling two lines down.
        if path.name in ARCHIVAL_BASENAMES:
            entry = []
            _log(session_id, {"event": "archival_cross_file_exempt", "file": str(path)})
        still = [a for a in entry if survivors_in(after, {a})]
        if still and date_only(set(still)):
            # Same rule as the same-file check above, and it matters MORE here for a reason
            # that is structural rather than a tally: the registry only ACCUMULATES, so a
            # date registered once goes on matching every later edit to that file, and the
            # block rate therefore grows with session length.
            # No per-branch count is quoted here on purpose -- such a figure goes stale the
            # moment the cohort moves. `date_only()` carries the snapshot; `gate_replay.py`
            # is the authority over both.
            # `still` is deliberately NOT cleared from the registry. A date that later
            # co-occurs with a non-date atom must still be able to contribute to a block, and
            # dropping it here would silently forget that.
            print(f"tier0_gate: NOT BLOCKING {path.name} -- the surviving cross-file atoms "
                  f"are all bare dates ({sorted(still)}).", file=sys.stderr)
            _log(session_id, {"event": "date_only_suppressed", "check": "cross_file",
                              "file": str(path), "atoms": sorted(still)})
        elif still:
            reasons.append(
                f"INCOMPLETE CROSS-FILE SWEEP: {still} were rewritten elsewhere earlier in "
                f"this session and still survive here. Update them in this edit, or mark "
                f"the line `stale-ok: <why>` if the old value is deliberately kept."
            )
        if entry and not still:
            pending_clear = key
        if changed:
            root = scan_root_for(path, payload.get("cwd", "") or "")
            elsewhere, stats = cross_file_survivors(root, path, changed)
            for other, atoms in elsewhere.items():
                pending_register[other] = sorted(set(reg.get(other) or []) | set(atoms))
    except Exception as e:  # noqa: BLE001
        _log(session_id, {"event": "error", "where": "checks", "err": repr(e)})
        print(f"tier0_gate: CHECKS DID NOT RUN for {path} "
              f"({type(e).__name__}: {e}). This edit was NOT gated.", file=sys.stderr)
        return 0

    if reasons:
        # DENY CHANGES NO STATE. The edit is not landing, so the file on disk is unchanged
        # and the registry must keep describing it as it already is. Clearing a sweep here
        # would retire an obligation the rejected fix never met; registering atoms from a
        # rejected edit would invent obligations for a change that never happened.
        _log(session_id, {"event": "deny", "file": str(path), "reasons": reasons})
        return emit_deny("\n\n".join(reasons))

    # THE DOORMAN RUNS ONLY IF THE FREE CHECKS PASSED. It costs a model call, so spending
    # one on an edit already being turned back would be waste; and its objections are
    # advisory while the gate's are not, so the binding decision should be reached first.
    # A DOORMAN DENY MUTATES NO STATE either, for exactly the reason a gate deny does not:
    # the edit is not landing, so the registry must keep describing the file as it is.
    # A PROGRESS MARKER FOR THE DOORMAN'S TURN, because this phase was invisible. The
    # statusline reads the PostToolUse advisor's pending-review markers, and the doorman runs
    # HERE, in the PreToolUse gate, before any of those exist -- so while a model call is
    # being waited on, the operator sees an idle or blank line and reads it as "nothing is
    # happening". That is the same absence-reads-as-approval shape the advisor's markers were
    # built to close, one phase earlier.
    #
    # `.doorman`, NOT `.json`, AND THE SUFFIX IS LOAD-BEARING. Every reader of that directory
    # globs `*.json` -- council_advisor's count_inflight and orphan_markers, and the
    # statusline's three -- so a name ending `.doorman` matches none of them, while a real
    # `<id>.json` marker still matches. Checked with fnmatch against that pattern plus a
    # positive control, because a file that DID match would inflate the in-flight count and
    # be reported as a lost review.
    #
    # IT NEVER AFFECTS THE EDIT. Writing it is wrapped so a failure leaves `_dm` None, and
    # removal happens in a `finally` so every route out of the review -- deny, exception,
    # clean pass -- takes it with it. An instrument that could block the gate it measures
    # would be worse than no instrument, and this gate sits in front of every edit.
    _dm = None
    try:
        _dm = (STATE_ROOT / (session_id or "_no_session") / "pending-review"
               / (re.sub(r"[^A-Za-z0-9_.-]", "_", payload.get("tool_use_id") or "")
                  + ".doorman"))
        _dm.parent.mkdir(parents=True, exist_ok=True)
        _dm.write_text(json.dumps({
            "started": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "tool_name": payload.get("tool_name"),
            "target_path": str(path),
            # SAME FIELD AS THE REVIEW MARKER, so a watcher labels a doorman row the way it
            # labels a fire row: by the directory an operator recognises rather than by a
            # session hash. Written from the payload, which is the session's own cwd -- not
            # from this process's, which is wherever the harness happened to launch the hook.
            "cwd": payload.get("cwd") or "",
        }))
    except Exception:  # noqa: BLE001
        _dm = None
    try:
        import doorman
        deny, _meta = doorman.review(payload)
    except Exception as e:  # noqa: BLE001
        deny = None
        _log(session_id, {"event": "doorman_error", "err": repr(e)})
        print(f"doorman: not consulted ({type(e).__name__}: {e}); edit proceeds and the "
              f"council reviews it as usual.", file=sys.stderr)
    finally:
        if _dm is not None:
            try:
                _dm.unlink()
            except OSError:
                pass
    if deny:
        _log(session_id, {"event": "doorman_deny", "file": str(path)})
        return emit_deny(deny)

    try:
        if pending_clear is not None:
            reg.pop(pending_clear, None)
            _log(session_id, {"event": "sweep_cleared", "file": pending_clear})
        if pending_register:
            reg.update(pending_register)
            _log(session_id, {"event": "registered", "from": str(path),
                              "files": pending_register, "stats": stats})
        # `skipped_archival` belongs in this condition for the same reason the other three do:
        # a sweep that deliberately skipped a file has not checked it, and a deliberate skip is
        # still a gap in coverage. Announcing it is what keeps "no survivors found" from meaning
        # "no survivors found in the files I felt like reading".
        if stats.get("truncated") or stats.get("skipped_unreadable") \
                or stats.get("skipped_large") or stats.get("skipped_archival"):
            _log(session_id, {"event": "sweep_incomplete", "stats": stats})
            print(f"tier0_gate: cross-file sweep incomplete ({stats}); staleness outside "
                  f"what was scanned has NOT been checked.", file=sys.stderr)
        if pending_clear is not None or pending_register:
            save_registry(session_id, reg)
    except Exception as e:  # noqa: BLE001
        _log(session_id, {"event": "error", "where": "apply", "err": repr(e)})
        print(f"tier0_gate: registry update failed ({type(e).__name__}: {e}); "
              f"cross-file sweeps may be incomplete.", file=sys.stderr)

    _log(session_id, {"event": "allow", "file": str(path)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
