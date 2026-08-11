#!/usr/bin/env python3
"""Range/offset requests for member file retrieval: parse, read, and the gate bypass.

WHY THE FEATURE EXISTS, from a fire rather than a guess. In
logs/2026-08-02/20260802T054717Z-98058e60.json both kimi and glm requested consult_council.py,
received `truncated to 23992 of 351321 bytes`, and could not reach the function under
discussion. kimi: "I can neither confirm nor refute the code-level claims." Head-only
retrieval made a 351 KB file unreviewable.

THE CHECK THIS FILE EXISTS FOR is the LAST one: a span that skips a brain note's frontmatter
would hand the vault gate a fragment it cannot recognise, and a refuted note's withheld gloss
would be delivered raw. That is a containment hole opened BY the feature, so it is tested as
one -- with a note whose frontmatter says `type: checkable`, asked for from line 5.

    python3 council/tests/test_retrieval_spans.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consult_council as cc  # noqa: E402

P = []


def check(label, cond):
    P.append(bool(cond))
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")


WD = Path(tempfile.mkdtemp(prefix="span_"))
BIG = "".join(f"line{i:05d} " + "x" * 60 + "\n" for i in range(1, 2001))
(WD / "big.py").write_text(BIG)
(WD / "note.md").write_text("---\ntype: checkable\nid: x\n---\n\nGLOSS: the withheld part.\n")
(WD / "plain.md").write_text("# no frontmatter\n" + "y" * (cc.RETRIEVAL_PER_FILE_CAP + 20_000))
# LARGER THAN THE CAP, derived so it stays larger if the cap moves -- this file exists to
# exercise "one line alone exceeds the budget", which a fixed 40,000 stopped doing the
# moment the per-file cap rose past it.
CAP = cc.RETRIEVAL_PER_FILE_CAP - 8
(WD / "onebig.txt").write_text("Z" * (CAP + 10_000) + "\n")

print("=== A. parse_file_request: the grammar, and what is NOT a span ===")
cases = {
    "a.py": ("a.py", None, ""),
    "a.py#L10-20": ("a.py", ("L", 10, 20), ""),
    "a.py#L10-": ("a.py", ("L", 10, None), ""),
    "a.py#B24000-": ("a.py", ("B", 24000, None), ""),
}
for tok, want in cases.items():
    check(f"{tok!r} parses to {want[1]}", cc.parse_file_request(tok) == want)
# A '#' that is not a well-formed span is part of the FILENAME, not a malformed range.
check("'weird#name.py' is a path, not a bad span",
      cc.parse_file_request("weird#name.py") == ("weird#name.py", None, ""))
check("'a.py#X1-2' (unknown unit) is a path, not a bad span",
      cc.parse_file_request("a.py#X1-2") == ("a.py#X1-2", None, ""))
# These DO parse as spans and are invalid -- reported, so the member learns why.
_, span, err = cc.parse_file_request("a.py#L0-5")
check("L0 is rejected (lines are 1-based) with a reason", span is None and "1-based" in err)
_, span, err = cc.parse_file_request("a.py#L20-10")
check("an inverted range is rejected with a reason", span is None and "precedes" in err)


def read(tok):
    path, span, err = cc.parse_file_request(tok)
    if err:
        return None, err
    return cc.read_repo_file(WD, path, span)


print("=== B. reading a span ===")
c, n = read("big.py#L10-12")
check("L10-12 returns exactly 3 lines, starting at line 10",
      c.splitlines() == [f"line{i:05d} " + "x" * 60 for i in (10, 11, 12)])
check(f"...and says so: {n!r}", n == "lines 10-12 of 2000 lines")
c, n = read("big.py#L1999-")
check("an open-ended line range runs to EOF", c.count("\n") == 2 and "lines 1999-2000" in n)
c, n = read("big.py#B70-100")
check("a byte range is INCLUSIVE at both ends (HTTP Range semantics)", len(c) == 31)
check(f"...and says so: {n!r}", n == f"bytes 70-100 of {len(BIG)}")

print("=== C. a range that selects nothing is a DENIAL, not an empty grant ===")
# collect_file_requests' own rule: a denial is always delivered "so a denied file never
# masquerades as an empty one". A granted empty fence IS that masquerade.
c, n = read("big.py#B999999-")
check("a byte offset past EOF is denied, not served empty", c is None and "empty range" in n)
c, n = read("big.py#L9999-")
check("a line past EOF is denied, not served empty", c is None and "empty range" in n)

print("=== D. caps, and notes that match what was delivered ===")
c, n = read("big.py")
check("a plain request still returns the HEAD, capped",
      len(c) == cc.RETRIEVAL_PER_FILE_CAP - 8)
check("a truncated head names the exact continuation request",
      f"#B{CAP}-" in n and f"{CAP} of {len(BIG)}" in n)
c, n = read("big.py#L1-")
# The whole file exceeds the cap, so the delivered range must stop at a LINE boundary and
# the note must count only the lines that SURVIVED -- reporting start..start+len(picked)-1
# would name lines the member never received.
delivered_lines = c.count("\n")
check("a capped line range truncates on a line boundary", c.endswith("\n"))
check(f"...and the note counts only delivered lines ({delivered_lines})",
      n == f"lines 1-{delivered_lines} of 2000 lines; capped at {CAP} bytes")
c, n = read("onebig.txt#L1-1")
check("a single line larger than the cap is truncated, and says that is what happened",
      c is not None and len(c) == CAP and "alone exceeds" in n)

print("=== E. THE GATE BYPASS. A span must not be able to skip a note's frontmatter ===")
c, n = read("note.md#L5-6")
check("a span on a brain note is DENIED", c is None)
check("...and the withheld gloss is nowhere in the result", "withheld part" not in (n or ""))
check(f"...with a reason naming the gate: {n!r}", "vault gate" in n)
c, n = read("note.md")
check("the same note is still served WHOLE (the gate decides, not this guard)",
      c is not None and c.startswith("---"))
# FAIL-CLOSED beyond the note format: any file opening with frontmatter is span-denied,
# because looks_like_brain_note needs the CLOSING delimiter too and a note whose frontmatter
# outran the probe window would otherwise be served.
(WD / "yaml.md").write_text("---\ntitle: ordinary doc\n---\n\nbody\n")
c, n = read("yaml.md#L4-5")
check("an ordinary frontmatter file is span-denied too (fail-closed)", c is None)
check("...and the denial states the ACTUAL rule, not 'this is a brain note'",
      "frontmatter" in n and "brain note" not in n)
c, n = read("plain.md")
check("a truncated file WITHOUT frontmatter still gets the continuation hint",
      f"#B{CAP}-" in n)

print("=== F. ABSOLUTE PATHS INSIDE THE WORKDIR, and the jail that must survive them ===")
# FROM A FIRE, not a hypothetical: on 2026-08-03 deepseek and glm each spent their one
# retrieval on an absolute path taken from the evidence header and were denied outright.
# deepseek's entire round-2 body was that request; retrieval is one-shot, so it could never
# be answered, and its BLOCK carried no reasoning anyone could act on.
c, n = read(str(WD / "big.py"))
check("an absolute path inside the workdir is SERVED", c is not None and c.startswith("line"))
c, n = read(f"{WD}/big.py#L10-12")
check("...and a span still applies to it", c is not None and "line00010" in c)
check("...with the same note as the relative form", n == "lines 10-12 of 2000 lines")
# THE JAIL IS UNCHANGED. Each of these must still be refused, and for its OWN reason -- a
# rewrite that let any of them through would have traded a usability fix for a containment hole.
c, n = read("/etc/passwd")
check("an absolute path OUTSIDE the workdir is still denied", c is None and "outside" in n)
c, n = read(str(WD))
check("the workdir itself is not a file to be served", c is None)
c, n = read(f"{WD}/../{WD.name}/big.py")
check("'..' that lands back inside is normalised, not smuggled", c is not None)
c, n = read(f"{WD}/../../../etc/passwd")
check("'..' that climbs OUT is denied after normalisation", c is None and "outside" in n)
check("a home-relative path is still denied, with a usable reason",
      read("~/.ssh/id_rsa") == (None, "home-relative paths are denied; request the path "
                                     "relative to the workdir root"))
(WD / ".secret").write_text("x")
c, n = read(str(WD / ".secret"))
check("a dotfile is still denied when reached by absolute path", c is None)
# The rewrite must not become a way around the brain-note gate tested in section E.
c, n = read(f"{WD}/note.md#L5-6")
check("the gate still denies a span on a note reached absolutely",
      c is None and "withheld part" not in (n or ""))
out = WD.parent / f"{WD.name}_escape"
out.mkdir(exist_ok=True)
(out / "outside.txt").write_text("SHOULD NOT BE SERVED")
(WD / "link.py").symlink_to(out / "outside.txt")
c, n = read(str(WD / "link.py"))
check("a symlink out of the tree is still refused (absolute form)",
      c is None and "SHOULD NOT BE SERVED" not in (n or ""))
shutil.rmtree(out, ignore_errors=True)

shutil.rmtree(WD, ignore_errors=True)
print(f"\n=== retrieval spans: {sum(P)}/{len(P)} PASS ===")
sys.exit(0 if all(P) else 1)
