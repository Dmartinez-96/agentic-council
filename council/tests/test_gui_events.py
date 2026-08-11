#!/usr/bin/env python3
"""Drive RunTab.on_event and LeaderTab.on_event with EVERY event their producers emit.

WHY IT EXISTS. Both handlers end in a chain of `elif ev == ...` with no else, and both
docstrings state that an unhandled event is DROPPED SILENTLY. The project has already paid
for that once -- four leader events reached a GUI with no branches for them and vanished --
and the repair was verified BY READING THE CODE. Reading is how the gap got there. This file
makes the property executable: feed each tab one record per event name its own producer
emits, and require the output pane to grow. A missing branch fails a check instead of
producing a quiet UI.

IT ALREADY EARNED ITS KEEP: written to cover the new `leader_reprompt` branch, the first run
of the LeaderTab sweep failed on `dropped` -- the same EventEmitter emits it on either
stream, RunTab announced it, and LeaderTab had no branch, so a leader turn that outran its UI
lost records and the notice that it had.

THE PRODUCER LISTS ARE DERIVED, NOT TYPED FROM MEMORY: each is the set of names its emitter
actually passes to `events.emit(...)`, plus what run_leader_turn forwards through
council_leader_run's bridge, plus `dropped`, which the emitter raises on any stream when the
consumer falls behind. Re-derive with:
    grep -o 'emit("[a-z_]*"' consult_council.py council_leader_run.py | sort -u
    grep -o 'event("[a-z_]*"' council_leader.py | sort -u

WHAT THE SWEEP DOES NOT ESTABLISH: that any line is CORRECT or well-worded -- only that the
event reached a branch and changed something visible. A branch printing the wrong field
passes the sweep, which is not hypothetical: the leader_action two-schema bug did exactly
that. Correctness is asserted only where a check below reads the rendered TEXT, and those
are named individually rather than implied by the sweep's coverage.

    QT_QPA_PLATFORM=offscreen python3 council/tests/test_gui_events.py
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import council_gui as g  # noqa: E402

P = []


def check(label, cond):
    P.append(bool(cond))
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")


app = QApplication.instance() or QApplication([])

# One representative record per event. Fields are those the EMITTERS pass -- a handler
# reading a field absent here still renders (str(None)), which is why the assertion is
# "produced output", not "produced exact text".
RUN_EVENTS = {
    "run_started": {"layer": "reasoning", "tool_name": "Edit", "target_path": "/tmp/x.py",
                    "voting": ["codex", "gemini"], "inspectors": ["muse"], "fast_mode": True},
    "round_started": {"round": 1},
    "member_started": {"member": "codex", "tier": "voting", "round": 1},
    "member_finished": {"member": "codex", "tier": "voting", "round": 1, "verdict": "PASS",
                        "duration_s": 1.2, "model_used": "m"},
    "member_corrected": {"member": "codex", "tier": "voting", "was": "UNPARSEABLE",
                         "verdict": "PASS", "why": "retry parsed"},
    "round_finished": {"round": 1, "verdicts": {"codex": "PASS", "gemini": "WARN"}},
    "tool_request": {"member": "gemini", "kind": "file", "granted": True},
    "final_verdict": {"verdict": "WARN", "log_path": "/tmp/log.json"},
    "dropped": {"n": 3},
}

LEADER_EVENTS = {
    "run_started": {"layer": "leader_turn", "tool_name": "plan-only",
                    "target_path": "/tmp/wd", "voting": ["codex"], "inspectors": []},
    "leader_round": {"round": 0},
    "leader_text": {"round": 0, "text": "thinking about it"},
    "leader_problem": {"round": 0, "problems": ["actions past the cap"]},
    "leader_reprompt": {"round": 1},
    "leader_steer": {"round": 1},
    # Emitted ONCE at seat time by council_leader_run.main(), before any round. Carries a
    # VOTING overlap and an UNDETERMINED seat together, because the renderer has to keep
    # them apart: a voting overlap means the leader's family can veto its own write, while
    # an undetermined seat means only that nothing could be concluded about it.
    "leader_family_overlap": {"leader": "codex", "family": "openai",
                              "voting": ["codex"], "inspector": [],
                              "undetermined": ["mystery"]},
    # leader_action arrives under TWO schemas and both must be exercised, because the branch
    # that renders them has to tell them apart. This one is the NON-WRITE schema, forwarded
    # from council_leader.py:983: round/action/target/ok/note, and no verdict at all.
    "leader_action": {"round": 0, "action": "read", "target": "a.py", "ok": True,
                      "note": "READ a.py (12 bytes)"},
    # The WRITE schema, council_leader_run.py:158 -- the only one carrying a council verdict,
    # because a write is the only action that is reviewed. Keyed separately so the sweep sends
    # both; `_ev` carries the name actually emitted.
    "leader_action(write)": {"_ev": "leader_action", "action": "write", "target": "x.py",
                             "verdict": "PASS", "applied": True, "reason": "12 bytes"},
    "leader_action_final": {"action": "exec", "target": "ls", "verdict": "", "applied": True,
                            "reason": "exit 0"},
    "note": {"text": "per-turn scratch: /tmp/s"},
    "dropped": {"n": 2},
    "final_verdict": {"verdict": "turn ended: final answer (no actions)",
                      "log_path": "/tmp/s"},
}
# approval_request needs the modal neutralised before it can be swept. MEASURED, not assumed:
# delivered to a LeaderTab with QMessageBox untouched under QT_QPA_PLATFORM=offscreen, the
# call does NOT return -- `timeout 15` killed it with rc 124, having printed the line before
# the delivery and never the one after. `ask` reaches `box.exec()`, which runs a nested event
# loop with no one to close it. Stubbing exec covers the branch instead of excusing it.
QMessageBox.exec = lambda self: QMessageBox.StandardButton.No
LEADER_EVENTS["approval_request"] = {"id": "req-1", "target": "/tmp/x.py", "verdict": "PASS",
                                     "bytes": 12, "preview": "print(1)", "review": "ok"}


def event_name(key, fields):
    """The `ev` a record actually carries. A dict key is unique but an event name is not --
    leader_action arrives under two schemas -- so a fixture may set `_ev` to the real name
    and use the key purely as a label."""
    return fields.get("_ev", key)


def surface(tab):
    """Everything an operator could SEE change, as one comparable value.

    Watching only the text pane was wrong and this test caught it on its first run:
    member_started and member_finished write into RunTab's seats TABLE and never touch the
    pane, so a pane-only assertion failed two events that render perfectly well. The property
    under test is 'the event had a visible effect', not 'the event printed a line'.
    """
    seats = getattr(tab, "seats", None)
    cells = ()
    if seats is not None:
        cells = tuple(
            (seats.item(r, c).text() if seats.item(r, c) is not None else None)
            for r in range(seats.rowCount()) for c in range(seats.columnCount()))
    return (tab.out.toPlainText(), seats.rowCount() if seats is not None else 0, cells)


def sweep(name, tab, events):
    print(f"== {name}: one record per event its producer emits ==")
    covered = []
    for key, fields in events.items():
        rec = {k: v for k, v in fields.items() if k != "_ev"}
        rec["ev"] = event_name(key, fields)
        before = surface(tab)
        tab.on_event(rec)
        changed = surface(tab) != before
        check(f"{name}.on_event({key!r}) had a visible effect", changed)
        if changed:
            covered.append(rec["ev"])
    return covered


run_tab = g.RunTab()
check("RunTab constructs", run_tab is not None)
sweep("RunTab", run_tab, RUN_EVENTS)

leader_tab = g.LeaderTab()
check("LeaderTab constructs", leader_tab is not None)
sweep("LeaderTab", leader_tab, LEADER_EVENTS)

# The sweep above only proves the events LISTED here are handled. This one proves the lists
# are not quietly shorter than the vocabulary the events module documents -- so a name added
# to council_events without a branch, or without a line here, is caught.
import council_events as ce  # noqa: E402

declared = set(ce.EVENT_NAMES)
exercised = {event_name(k, v) for k, v in RUN_EVENTS.items()} | \
            {event_name(k, v) for k, v in LEADER_EVENTS.items()}
missing = sorted(declared - exercised)
check(f"every name in council_events.EVENT_NAMES is accounted for (unaccounted: {missing})",
      not missing)

# EVENT_NAMES IS NOT THE AUTHORITY, and checking against it alone is the weaker check it
# looks like: it proves EVENT_NAMES is a subset of the fixtures, while EVENT_NAMES itself was
# stale by six names when this file was written. The PRODUCERS are the authority. This scans
# them for the names they actually pass to emit()/event() -- a static scan, so it sees a
# literal and would miss a name built at runtime, which nothing currently does.
import re  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
produced = set()
for src, pat in ((ROOT / "consult_council.py", r'emit\(\s*"([a-z_]+)"'),
                 (ROOT / "council_leader_run.py", r'emit\(\s*"([a-z_]+)"'),
                 (ROOT / "council_leader.py", r'event\(\s*"([a-z_]+)"')):
    produced |= set(re.findall(pat, src.read_text(encoding="utf-8")))
# `dropped` is raised by the emitter itself rather than by any producer, so it is not in the
# scan and is not a gap.
unswept = sorted(produced - exercised)
# `not unswept` ALONE IS VACUOUS: a regex that matched nothing gives produced=set(), unswept
# =[], and the check passes while having scanned for nothing. The count is part of the
# ASSERTION, not just the label, so a scan that stops finding names fails instead of passing
# silently. The floor is deliberately loose -- it guards against zero, not against drift.
check(f"every name the PRODUCERS emit has a fixture (produced={len(produced)}, "
      f"unswept: {unswept})", not unswept and len(produced) >= 8)
undeclared = sorted(produced - declared)
check(f"every name the PRODUCERS emit is declared in EVENT_NAMES (undeclared: {undeclared})",
      not undeclared)

# TEXT CHECKS. The sweep above cannot tell a right line from a wrong one, and the bug that
# prompted this file proves it matters: the non-write schema carries `ok`/`note` and no
# verdict, and the renderer used to read `applied`/`verdict`, so a successful READ printed as
# "read a.py: None -- not applied" with its note dropped -- and it changed the pane, so a
# visibility sweep called it handled. These read the rendered text instead.
fresh = g.LeaderTab()
fresh.on_event({"ev": "leader_action", "round": 0, "action": "read", "target": "a.py",
                "ok": True, "note": "READ a.py (12 bytes)"})
line = fresh.out.toPlainText()
check("non-write leader_action renders its NOTE and does not invent a verdict",
      "READ a.py (12 bytes)" in line and "None" not in line and "not applied" not in line)

fresh2 = g.LeaderTab()
fresh2.on_event({"ev": "leader_action", "action": "write", "target": "x.py",
                 "verdict": "PASS", "applied": True, "reason": "12 bytes"})
wline = fresh2.out.toPlainText()
check("write leader_action still renders the council verdict and the applied decision",
      "PASS" in wline and "APPLIED" in wline)

fresh3 = g.LeaderTab()
fresh3.on_event({"ev": "leader_action", "round": 0, "action": "exec", "target": "false",
                 "ok": False, "note": "EXEC false (exit 1)"})
fline = fresh3.out.toPlainText()
check("a FAILED non-write action is not rendered as a success",
      "FAILED" in fline and "exit 1" in fline)

fresh4 = g.LeaderTab()
fresh4.on_event({"ev": "leader_reprompt", "round": 1})
check("leader_reprompt names the round and says what happened",
      "round 1" in fresh4.out.toPlainText()
      and "no WRITE" in fresh4.out.toPlainText())

print(f"\n=== gui events: {sum(P)}/{len(P)} PASS ===")
sys.exit(0 if all(P) else 1)
