#!/usr/bin/env python3
"""Run the council's test suites.

WHY A RUNNER AT ALL. The package ships no automated suite over the ENGINE. What a cloner
can run today is the executable guards (the brain vault validator, the installer's
prerequisite checks, the scrub gate in the sync path) -- real and load-bearing, but none
of them exercise the council itself. The 33 suites that do exist have lived in a directory
that is not part of the package.

THIS FILE DOES NOT BY ITSELF MAKE THEM CLONEABLE, and saying otherwise would be the same
false claim the suites are meant to catch: it currently sits in `council/tests/`, which does not
ship. Packaging is a separate, still-open step. What this file supplies is the part that
was actually missing -- discovery, prerequisite handling, and a single exit code.

TWO RULES THIS RUNNER FOLLOWS, both of them lessons this project already paid for.

1. A MISSING PREREQUISITE IS A SKIP, NEVER A FAILURE. Two suites need a working `bwrap`
   (test_exec_interrupt and test_leader_resources -- MEASURED by running every candidate
   on a PATH with bwrap removed, not grepped; an earlier draft of this docstring said
   FIVE, repeating a count the project's own record had already corrected). A suite that
   FAILS for want of a sandbox teaches a newcomer that the project is broken, which is
   worse than saying nothing. Skips carry their reason and never affect the exit code.

2. REQUIREMENTS ARE DECLARED BY THE SUITE, NOT LISTED HERE. Each suite states its own
   needs in a `# requires: <name>` line. A central table of which-suite-needs-what would
   be a second copy of a fact that lives in the suite, and two such copies in this
   codebase have drifted from what they copied (see engine_rules() in council_gui.py).
   Adding a suite therefore requires no edit here.

EXIT CODE: 0 when nothing FAILED. Skips do not fail the run; a suite that could not be
launched does, and so does a run that discovered nothing or hit an unknown requirement --
see main() for why those two are failures rather than quiet successes.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

def _bwrap_works() -> bool:
    """Can bubblewrap ACTUALLY create a namespace here, not merely exist on PATH?

    `shutil.which("bwrap") is not None` was the first version and it is a void check for
    the property that matters: the common failure is not a missing binary but a kernel or
    container that forbids unprivileged user namespaces, where the binary is present and
    every sandboxed suite fails anyway -- as a FAILURE, contradicting this runner's one
    promise that a missing prerequisite is a skip. So run the smallest real sandbox
    instead and ask whether it exited 0.

    THE PAYLOAD IS RESOLVED, NOT HARDCODED. An earlier version ran `/usr/bin/true`, which
    was measured on ONE host; where `true` lives elsewhere that probe fails for a reason
    having nothing to do with bwrap, and the suite would be falsely SKIPPED -- a skip
    invented by the checker is as bad as the failure it was avoiding. If `true` cannot be
    found at all, report bwrap as unusable rather than guessing a path: this function must
    never claim a sandbox works on evidence it does not have.
    """
    true_bin = shutil.which("true")
    if not true_bin:
        return False
    try:
        return subprocess.run(["bwrap", "--ro-bind", "/", "/", true_bin],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# Each entry: name -> (probe, human reason). The PROBE decides, never a host assumption.
# `network` USED TO BE HERE and was removed: no suite declared it, and its probe only read
# an opt-in flag rather than testing connectivity -- an unused requirement whose name
# promised a check it did not perform. Add it back with a real probe if a suite needs one.
REQUIREMENTS = {
    "bwrap": (_bwrap_works,
              "bubblewrap cannot create a namespace here (missing, or unprivileged user "
              "namespaces are disabled); the exec sandbox cannot run"),
    "openrouter": (lambda: bool(os.environ.get("OPENROUTER_API_KEY")),
                   "OPENROUTER_API_KEY is not set"),
}
_REQ_RE = re.compile(r"^#\s*requires:\s*(.+)$", re.MULTILINE)


def declared_requirements(path: Path) -> list[str]:
    """Requirement names a suite declares. Unknown names are returned as-is so the runner
    reports them rather than silently ignoring a requirement it does not understand --
    an unrecognised prerequisite must not read as 'no prerequisite'."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for m in _REQ_RE.finditer(text):
        out.extend(p.strip() for p in m.group(1).split(",") if p.strip())
    return out


def unmet(reqs: list[str]) -> tuple[list[str], list[str]]:
    """Returns (unmet_reasons, unknown_names) -- and they are SEPARATE on purpose.

    An UNMET requirement is a fact about this host and is a legitimate skip. An UNKNOWN
    requirement is a fact about this FILE: a name no probe can evaluate, which is almost
    always a typo in a `# requires:` line. The first version lumped them together and
    skipped both, so `# requires: bwarp` silently disabled a suite and the run still
    exited 0 -- a test that never ran, reported as a clean pass. That is the same
    absence-reads-as-approval failure the council's own pending-review markers exist to
    stop, reproduced inside the test runner. main() now FAILS on unknown names.
    """
    missing: list[str] = []
    unknown: list[str] = []
    for r in reqs:
        probe = REQUIREMENTS.get(r)
        if probe is None:
            unknown.append(r)
        elif not probe[0]():
            missing.append(f"{r}: {probe[1]}")
    return missing, unknown


def discover(names: list[str]) -> list[Path]:
    if names:
        out = []
        for n in names:
            p = HERE / (n if n.endswith(".py") else f"test_{n}.py")
            if not p.exists():
                print(f"no such suite: {p.name}", file=sys.stderr)
                raise SystemExit(2)
            out.append(p)
        return out
    return sorted(HERE.glob("test_*.py"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the council test suites.")
    ap.add_argument("suites", nargs="*", help="suite names (default: all)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="per-suite timeout in seconds (default 600)")
    ap.add_argument("--list", action="store_true",
                    help="list suites and their declared requirements, run nothing")
    args = ap.parse_args()

    suites = discover(args.suites)
    if not suites:
        # A RUN THAT FOUND NOTHING IS A FAILURE, not an empty success. Exiting 0 here
        # would print "0/0 passed" and hand a green result to anyone whose glob, working
        # directory or packaging was wrong -- a clean bill of health over zero tests.
        print(f"NO SUITES DISCOVERED under {HERE}. Refusing to report success over an "
              f"empty run.", file=sys.stderr)
        return 1
    if args.list:
        for s in suites:
            reqs = declared_requirements(s)
            print(f"  {s.name:<40} requires: {', '.join(reqs) if reqs else '-'}")
        return 0

    passed: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    t0 = time.time()

    for s in suites:
        miss, unknown = unmet(declared_requirements(s))
        if unknown:
            # UNKNOWN != UNMET. This is a name no probe can evaluate -- almost always a
            # typo in a `# requires:` line. Failing is the whole point: skipping would
            # disable the suite silently and still exit 0.
            why = ("unknown requirement(s): " + ", ".join(unknown)
                   + f"; known requirements are {', '.join(sorted(REQUIREMENTS))}")
            failed.append((s.name, why))
            print(f"FAIL  {s.name}  ({why})")
            continue
        if miss:
            skipped.append((s.name, "; ".join(miss)))
            print(f"SKIP  {s.name}  ({'; '.join(miss)})")
            continue
        start = time.time()
        try:
            proc = subprocess.run([sys.executable, str(s)], cwd=str(ROOT),
                                  capture_output=True, text=True, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            failed.append((s.name, f"timed out after {args.timeout}s"))
            print(f"FAIL  {s.name}  (timed out after {args.timeout}s)")
            continue
        except OSError as e:
            failed.append((s.name, f"could not launch: {e}"))
            print(f"FAIL  {s.name}  (could not launch: {e})")
            continue
        dt = time.time() - start
        # 77 is the autotools SKIP convention: a suite that decides at RUNTIME it cannot
        # run (something no static `# requires:` line could know) says so this way.
        if proc.returncode == 77:
            reason = (proc.stdout.strip().splitlines() or ["no reason given"])[-1]
            skipped.append((s.name, reason))
            print(f"SKIP  {s.name}  ({reason})")
        elif proc.returncode == 0:
            passed.append(s.name)
            print(f"PASS  {s.name}  ({dt:.1f}s)")
        else:
            tail = (proc.stdout + proc.stderr).strip().splitlines()
            detail = " | ".join(tail[-3:]) if tail else f"exit {proc.returncode}"
            failed.append((s.name, detail))
            print(f"FAIL  {s.name}  (exit {proc.returncode}, {dt:.1f}s)")
            for line in tail[-12:]:
                print(f"        {line}")

    total = len(passed) + len(failed) + len(skipped)
    print(f"\n{'=' * 66}")
    print(f"{len(passed)}/{total} passed, {len(failed)} failed, {len(skipped)} skipped "
          f"in {time.time() - t0:.1f}s")
    if skipped:
        print("\nSKIPPED (a missing prerequisite is not a failure):")
        for n, why in skipped:
            print(f"  {n}: {why}")
    if failed:
        print("\nFAILED:")
        for n, why in failed:
            print(f"  {n}: {why}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
