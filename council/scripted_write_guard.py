#!/usr/bin/env python3
"""PreToolUse guard: WARN when a Bash command writes to a file the council would review.

WHY THIS EXISTS. The council's review hook matches the harness's EDITING tools
(`Write|Edit|NotebookEdit`); Bash carries the evidence LOGGER, which logs and does not review.
So a file changed with `python3 - <<'PY'`, `sed -i`, or a redirect lands with NO review, and
NOTHING ANYWHERE SAYS SO -- the absence of a verdict is indistinguishable from a clean one.

THAT GAP IS EASY TO FALL INTO RATHER THAN HARD. A script can assert that each anchor matched
exactly once, which is genuinely the safer way to bound a multi-anchor edit -- so the tool
that better protects the FILE is the one that hides the change from the REVIEWER. An agent
following good practice on one axis can defeat review on the other without noticing, and it
has happened here more than once.

IT WARNS AND DOES NOT BLOCK, deliberately. A hard refusal would break legitimate shell work
(writing probes and scratch files, or recovering when the editing tools are failing), and an
escape hatch that has to be argued with is its own hazard.

WHAT IT DOES NOT DO, stated plainly because a guard that oversells itself is worse than none:
it does not stop anything, it cannot see writes performed by a program it merely launched
(`./build.sh` may write anything), and its pattern list is a HEURISTIC -- a novel way to write
a file passes unseen, exactly as the repo's own scrub gate warns about its FORBIDDEN list.
It catches the shapes that actually caused the incident, not all possible shapes.

EXIT STATUS IS ALWAYS 0. A PreToolUse hook that fails must never block the tool it is
advising on, and a guard that crashed the shell would be removed within the hour.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

COUNCIL_ROOT = Path(__file__).resolve().parent

# Shapes that WRITE. Each was chosen because it appeared in real use, not from imagination:
# the heredoc is what the agent actually used, and the others are the obvious neighbours.
WRITE_PATTERNS = (
    (re.compile(r"(?<![>\d])>(?!>)\s*\S"), "a `>` redirect"),
    (re.compile(r">>\s*\S"), "a `>>` append"),
    (re.compile(r"\bsed\s+-[a-z]*i\b"), "`sed -i` in-place editing"),
    (re.compile(r"\btee\b"), "`tee`"),
    (re.compile(r"\bdd\b[^|]*\bof="), "`dd of=`"),
    (re.compile(r"\.write_text\s*\(|\bopen\s*\([^)]*['\"][wa]"), "a Python file write"),
    (re.compile(r"\bcp\b|\bmv\b|\binstall\b\s+-"), "a copy/move"),
    (re.compile(r"\btruncate\b|\bshred\b"), "a truncate/shred"),
)

# Writing HERE is the thing worth warning about: these are the trees the council reviews.
# A write to /tmp, a scratchpad, or anywhere else is ordinary work and is left alone.
REVIEWED_ROOTS = (COUNCIL_ROOT, Path.home() / "Documents" / "agentic-council")


def looks_like_write(cmd: str) -> list:
    return [why for rx, why in WRITE_PATTERNS if rx.search(cmd)]


def mentions_reviewed_tree(cmd: str) -> bool:
    """True if the command names a path inside a tree the council reviews.

    DELIBERATELY INCLUDES A BARE RELATIVE MENTION of a tracked-looking file, because the
    incident's commands were run with cwd inside the repo and never spelled an absolute path.
    """
    for root in REVIEWED_ROOTS:
        if str(root) in cmd:
            return True
    # `foo.py`, `_brain/x.md`, `roster.json` -- relative names of the kinds of file that live
    # in these trees. A path under /tmp is excluded first so a scratch probe does not trip it.
    stripped = re.sub(r"/tmp/\S+|/home/\S+/\.claude/\S+", " ", cmd)
    return bool(re.search(r"\b[\w./-]+\.(py|md|json|sh|txt)\b", stripped))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0                      # a guard that cannot parse must not block the tool
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd:
        return 0
    why = looks_like_write(cmd)
    if not why or not mentions_reviewed_tree(cmd):
        return 0
    # STDERR, NOT STDOUT: stdout of a PreToolUse hook can be interpreted as a decision, and
    # this hook decides nothing. It only says something out loud.
    print(
        "COUNCIL GUARD (advisory, nothing was blocked): this Bash command looks like it "
        f"WRITES to a reviewed tree via {', '.join(why)}.\n"
        "The council's PostToolUse reviewer matches Write|Edit|NotebookEdit ONLY -- Bash gets "
        "the evidence logger, which LOGS and does not REVIEW. A file changed this way lands "
        "with no verdict, and no verdict looks exactly like a clean one.\n"
        "USE Write/Edit for anything the council should see. If a scripted edit is genuinely "
        "necessary, fire the council on the result explicitly before treating it as reviewed.",
        file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
