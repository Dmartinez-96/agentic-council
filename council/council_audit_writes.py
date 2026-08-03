#!/usr/bin/env python3
"""Find files that changed during a session but were never reviewed by the council.

THE HOLE THIS EXISTS FOR.

The council fires on PostToolUse for Write / Edit / NotebookEdit. It does NOT fire
on Bash, and neither does the PreToolUse gate. So all of these modify a source file
with no review, no gate, and no possibility of a BLOCK:

    sed -i ...            cat > file <<EOF        tee file
    printf ... > file     patch -p1 < x.diff      python3 -c "open(f,'w')..."

A `Write` gets three critics and an auto-revert on BLOCK. A `cat >` gets silence.
That is not a late review, like the PostToolUse gap. It is NO review, and it is the
widest hole in the design. Claude has walked through it: the package sync writes
eleven files that way, and so does every `cp`.

WHAT THIS IS: a DETECTOR, not a control. It cannot prevent a Bash write -- shell is
not reliably parseable and a regex gate would both leak and misfire. It makes the
bypass VISIBLE, by comparing what actually changed on disk against what the council
actually saw.

WHAT IT MISSES. Say it plainly or the tool lies by omission:
  - Anything outside the scanned roots (see --root).
  - A file whose mtime was preserved (`touch -r`, `cp -p`, some archive extractions).
  - Attribution before session_id existed. MEASURED: 6,813 council logs predate it
    and carry none, and 341 recent logs lack it too. A file reviewed under one of
    those cannot be matched to a session and will be reported as unreviewed.
  - Anything under an unreadable directory. The scan now REPORTS those rather than
    skipping them silently (see scan_errors), because "I could not look" and "I
    looked and found nothing" are the same output otherwise, and that is the exact
    void check this project keeps tripping over.

It also produces FALSE POSITIVES by design. An mtime moves for reasons that have
nothing to do with Claude: another process, an editor, a build, a git operation. A
file here is a QUESTION -- "who wrote that, and did anyone review it?" -- never an
accusation.

EXIT CODES (a caller reads these, so they are part of the contract):
    0  scan completed cleanly; no unreviewed changes in the scanned roots
    1  unreviewed changes FOUND (listed on stdout). The scan may ALSO have been
       incomplete; if so it says so loudly on stdout, and there may be more.
    2  could not run: no session, no evidence, or a --root that does not exist
    3  scan was INCOMPLETE (unreadable directories) and found nothing. NOT a pass.
       Split from 0 on purpose: "I found nothing" and "I could not look" must never
       share an exit status.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

COUNCIL_ROOT = Path(__file__).resolve().parent
STATE_ROOT = Path.home() / ".claude" / "state"

# Directory names skipped everywhere: caches, virtualenvs, vendored deps, VCS
# internals. This is a JUDGEMENT about where hand-edited source usually is not, and
# every entry is therefore a place this tool can be blind. If you keep source in one
# of these, remove it from the set.
#
# Deliberately NOT skipped: "logs", "state", "projects", "build", "dist". An earlier
# version skipped all five on the assumption they are never source, which is wrong:
# projects do keep hand-written code in build/ or dist/, and "logs"/"state" are
# ordinary package names. Skipping them would have HIDDEN REAL EDITS, the one thing
# this tool must not do. Council-specific churn is excluded by PATH instead (see
# _noise_paths), which is narrower.
NOISE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              ".pytest_cache", ".mypy_cache", ".ruff_cache", "site-packages",
              ".tox", ".eggs"}
NOISE_SUFFIX = (".pyc", ".pyo", ".swp", ".tmp", ".lock")

# Paths that churn as a CONSEQUENCE of a council running, so reporting them is noise
# about ourselves. Matched as path PREFIXES, which is why this is precise where a
# bare directory-name filter would not be: it excludes THIS council's logs/, not
# every directory in the world called "logs".
#
# The sibling Claude_Council is included because it writes its own logs on the same
# hooks. A first run without it buried the real finding under hundreds of its log
# files -- the signal was there, and unreadable.
def _noise_paths() -> tuple[Path, ...]:
    siblings = [COUNCIL_ROOT, COUNCIL_ROOT.parent / "Claude_Council"]
    out: list[Path] = []
    for s in siblings:
        out += [s / "logs", s / "threads", s / "reverted", s / "_nogit"]
    out += [Path.home() / ".claude" / "state", Path.home() / ".claude" / "projects"]
    return tuple(out)


ABS_PATH_RE = re.compile(r"(/(?:home|opt|usr|srv|var|tmp)/[^\s'\"`;|&$()<>]{2,})")


def newest_session() -> str:
    if not STATE_ROOT.is_dir():
        return ""
    dirs = [d for d in STATE_ROOT.iterdir() if d.is_dir()]
    return max(dirs, key=lambda d: d.stat().st_mtime).name if dirs else ""


def sessions_with_activity(within_hours: float) -> list[str]:
    """Session ids whose evidence file was MODIFIED inside the window, newest first.

    Say what this is, because the name it nearly had ("active_sessions") would have
    lied: it is an mtime test on evidence.jsonl, so it reports sessions that were
    WRITTEN TO in the window. That is a SUPERSET of concurrency -- two sessions run
    back-to-back inside the window both appear here, though they never overlapped.
    The caller only uses it to decide whether auto-picking a session is safe, and for
    that a superset is the correct side to err on: a needless demand for
    --session-id costs one flag, while auditing the wrong session costs a false
    accusation that a reviewed file bypassed the council.
    """
    if not STATE_ROOT.is_dir():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    live = []
    for d in STATE_ROOT.iterdir():
        ev = d / "evidence.jsonl"
        if not ev.is_file():
            continue
        mt = datetime.fromtimestamp(ev.stat().st_mtime, tz=timezone.utc)
        if mt >= cutoff:
            live.append((mt, d.name))
    return [name for _, name in sorted(live, reverse=True)]


def _parse(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def evidence_events(session_id: str) -> list[dict]:
    ev = STATE_ROOT / session_id / "evidence.jsonl"
    if not ev.exists():
        return []
    out = []
    for line in ev.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def reviewed_at(session_id: str) -> dict[str, datetime]:
    """path -> the LAST time the council reviewed it in this session.

    A timestamp, not a membership test. Membership was the first version's bug, and
    it defeated the entire tool: a file reviewed once and then rewritten by Bash
    afterwards stayed in the "reviewed" set, so the detector HID the exact bypass it
    was built to expose. Comparing mtime against the last review time catches it.
    """
    seen: dict[str, datetime] = {}
    for f in COUNCIL_ROOT.glob("logs/*/*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        if d.get("session_id") != session_id:
            continue
        t, ts = d.get("target_path"), _parse(d.get("timestamp", ""))
        if not t or ts is None:
            continue
        try:
            key = str(Path(t).resolve())
        except OSError:
            continue
        if key not in seen or ts > seen[key]:
            seen[key] = ts
    return seen


def infer_roots(events: list[dict]) -> list[Path]:
    """Where to look, derived from the session's EVIDENCE.

    MEASURED: a Bash evidence event carries only
        ['at','command','description','exit_code','interrupted','stderr_tail',
         'stdout_tail','tool']
    -- there is NO cwd and NO file_path. An earlier version read `e["cwd"]`, which
    is always absent, so Bash contributed nothing and a project touched ONLY through
    Bash was never scanned: invisible in precisely the case this tool exists for.
    So absolute paths are pulled out of the command STRING instead. Crude, but it is
    the only signal Bash leaves behind.

    THE WORKSPACE DIRECTORY IS DERIVED FROM WHERE THE COUNCIL ITSELF SITS -- its parent.
    A path under a sibling of the council tree is treated as a project, and its root is
    that sibling. This was previously the literal component "Professional", which stopped
    matching anything when the tree moved to ~/Documents on 2026-08-02 and would have made
    this tool report NO project roots beyond the cwd -- an integrity tool going quiet
    without saying so, which reads exactly like a clean audit.
    THE LIMIT IS UNCHANGED AND WORTH STATING: a project living somewhere other than beside
    the council (say ~/code/thing) is still only reached via the cwd. That was true of the
    "Professional" version too; deriving the directory did not widen the reach, it only
    stopped the reach depending on one machine's folder name.
    """
    workspace = COUNCIL_ROOT.parent
    roots: set[Path] = {Path.cwd().resolve()}
    for e in events:
        cands: list[str] = []
        # `file_path` is the evidence schema (Read/Edit/Write events). `target_path`
        # is the COUNCIL LOG schema and never appears here -- reading it was a silent
        # no-op in an earlier draft.
        v = e.get("file_path")
        if isinstance(v, str):
            cands.append(v)
        cmd = e.get("command")
        if isinstance(cmd, str):
            cands.extend(ABS_PATH_RE.findall(cmd))
        for val in cands:
            p = Path(val)
            if not p.is_absolute():
                continue
            try:
                rel = p.relative_to(workspace)
            except ValueError:
                continue
            if rel.parts:
                roots.add(workspace / rel.parts[0])
    out: list[Path] = []
    for r in sorted(roots):
        if not any(r != o and o in r.parents for o in roots):
            out.append(r)
    return out


def changed_since(roots: list[Path], since: datetime
                  ) -> tuple[list[tuple[Path, datetime]], list[str]]:
    """Walk in Python, and SURFACE traversal errors.

    Two deliberate choices, both learned the hard way:

    1. Not `find -newermt`. MEASURED on this host: a malformed timestamp
       (`-newermt "9999-99-99 99:99:99"`) exits 0, prints nothing to stderr, and
       matches nothing. So a broken probe is INDISTINGUISHABLE from "no files
       changed", and the obvious hardening -- check find's return code -- does not
       help, because the return code is 0.

    2. `os.walk(onerror=...)`. MEASURED: bare os.walk SILENTLY SKIPS a directory it
       cannot read, yielding nothing and raising nothing. A detector that reports a
       clean bill of health because it could not open the directory is worse than no
       detector, so the errors come back with the results and are printed.
    """
    cutoff = since.timestamp()
    out: list[tuple[Path, datetime]] = []
    errors: list[str] = []
    noise = _noise_paths()
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(
                root, onerror=lambda e: errors.append(f"{e.filename}: {e.strerror}")):
            here = Path(dirpath)
            if any(here == n or n in here.parents for n in noise):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in NOISE_DIRS]
            for name in filenames:
                if name.endswith(NOISE_SUFFIX):
                    continue
                p = here / name
                try:
                    m = p.stat().st_mtime
                except OSError as e:
                    errors.append(f"{p}: {e.strerror}")
                    continue
                if m > cutoff:
                    out.append((p, datetime.fromtimestamp(m, timezone.utc)))
    return out, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session-id", default="")
    ap.add_argument("--root", action="append", default=[],
                    help="Directory to scan. Repeatable. Default: inferred from "
                         "the session's evidence file.")
    ap.add_argument("--hours", type=float, default=0.0,
                    help="Only consider files changed in the last N hours. Use this "
                         "when the session's evidence file is long-lived: the "
                         "default window is the FIRST event in that file, which on a "
                         "resumed session can be weeks back and will bury the "
                         "finding under thousands of unrelated mtimes.")
    args = ap.parse_args()

    # REFUSE TO GUESS WHEN SESSIONS ARE CONCURRENT.
    #
    # Auto-picking the newest session is safe only when there IS one session. With
    # several live at once it silently audits the WRONG one: reviewed_at() then holds
    # some other session's reviews, none of them match the files that changed here,
    # and the tool reports NEVER REVIEWED for files the council reviewed a dozen
    # times. That is not a degraded answer, it is a confident false accusation from
    # the one tool whose entire job is integrity -- and it is indistinguishable from
    # a real finding.
    #
    # Observed exactly that on 2026-07-14: three sessions live, auto-pick landed on
    # the wrong one, and three thoroughly-reviewed files were reported as bypassed.
    # So when it is ambiguous, do not choose. Say so and exit 2 (cannot run), which
    # is a REFUSAL, not a pass.
    sid = args.session_id
    if not sid:
        window_h = args.hours if args.hours > 0 else 24.0
        live = sessions_with_activity(window_h)
        if len(live) > 1:
            print(f"AMBIGUOUS: {len(live)} sessions wrote evidence in the last "
                  f"{window_h:g}h (they need not have overlapped). Auto-picking one "
                  f"COULD land on the wrong session's reviews, which could cause "
                  f"reviewed files to be reported as bypassed.", file=sys.stderr)
            for s in live:
                print(f"  --session-id {s}", file=sys.stderr)
            print("Re-run with --session-id. Refusing to guess.", file=sys.stderr)
            return 2
        sid = live[0] if live else newest_session()
    if not sid:
        print("no session found under ~/.claude/state/", file=sys.stderr)
        return 2

    events = evidence_events(sid)
    if not events:
        print(f"no evidence file for session {sid}; cannot bound the time window.",
              file=sys.stderr)
        return 2

    starts = [t for t in (_parse(e.get("at") or e.get("timestamp") or "")
                          for e in events) if t]
    if not starts:
        print(f"evidence for {sid} has no usable timestamps.", file=sys.stderr)
        return 2
    start = min(starts)

    # --hours overrides the inferred window. It exists because the inferred one can
    # be useless: an evidence file survives compaction and resumption, so its FIRST
    # event may be weeks old. Measured on this machine: one session's window opened
    # 2026-05-16 and swept 25,220 changed files, burying the sixteen that mattered.
    if args.hours > 0:
        start = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    else:
        span_h = (datetime.now(timezone.utc) - start).total_seconds() / 3600
        if span_h > 24:
            print(f"NOTE: the inferred window is {span_h:.0f} hours wide (the first "
                  f"event in this session's evidence file). On a resumed session that "
                  f"is usually far too wide, and the report will be mostly unrelated "
                  f"mtimes. Narrow it with --hours N.", file=sys.stderr)
            print(file=sys.stderr)

    reviewed = reviewed_at(sid)
    roots = [Path(r).expanduser().resolve() for r in args.root] or infer_roots(events)

    # A --root that does not exist is a TYPO, not an empty directory. Silently
    # skipping it would scan nothing and report a clean pass, which is the same
    # void check this tool exists to expose.
    missing = [r for r in roots if not r.is_dir()]
    if missing:
        print("ERROR: these roots do not exist (typo?). Refusing to report a clean "
              "scan over directories that are not there:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        return 2

    changed, errors = changed_since(roots, start)

    unreviewed = []
    for p, mtime in changed:
        key = str(p.resolve())
        last = reviewed.get(key)
        if last is None or mtime > last:
            unreviewed.append((p, mtime, last))

    print(f"session  : {sid}")
    print(f"since    : {start.isoformat()}")
    print(f"roots    : {', '.join(str(r) for r in roots)}")
    print(f"reviewed : {len(reviewed)} file(s) went through the council")
    print(f"changed  : {len(changed)} file(s) changed on disk in that window")
    if errors:
        print()
        print(f"!! SCAN WAS INCOMPLETE: {len(errors)} path(s) could not be read.")
        print("!! Anything under them is invisible to this report. Do not read a")
        print("!! clean result as proof; the scan did not finish.")
        for e in errors[:5]:
            print(f"     {e}")
    print()

    if not unreviewed:
        print("No unreviewed changes IN THE SCANNED ROOTS. Note the qualifier: this")
        print("is not proof that nothing was written unseen, only that nothing was")
        print("found where it looked. The module docstring lists what it misses.")
        # A scan that could not read part of the tree has NOT come back clean, and
        # must not exit 0. "I found nothing" and "I could not look" are the same
        # words with opposite meanings, and exit code is what a caller reads.
        return 3 if errors else 0

    print("=" * 74)
    print(f"{len(unreviewed)} FILE(S) CHANGED WITHOUT COUNCIL REVIEW")
    print("=" * 74)
    print("Modified during the session but never reviewed -- or modified AGAIN")
    print("after its last review. The usual cause is a Bash write (sed -i, cat >,")
    print("tee, cp, patch), which the council cannot see. Innocent causes exist:")
    print("another process, an editor, a build. This is a QUESTION, not a verdict.")
    print()
    for p, mtime, last in unreviewed:
        if last is None:
            print(f"  NEVER REVIEWED   {p}")
        else:
            print(f"  CHANGED AFTER    {p}")
            print(f"                   reviewed {last.isoformat()}, "
                  f"modified {mtime.isoformat()}")
    print()
    print("If Claude wrote any of these from Bash, that work skipped three critics,")
    print("the rule-11 gate, and auto-revert. Use Write/Edit so it does not.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
