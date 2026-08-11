#!/usr/bin/env python3
"""Headless drive of the Config tab's preset action -- the first test to CONSTRUCT the GUI.

WHY IT EXISTS, and the reasoning is a quality-bar lesson rather than a feature note. The
preset feature was "verified" by importing council_gui and calling `preset_roster` at module
level. A reviewer pointed out what that could not catch: `_apply_preset` calls
`self._warn_duplicate_names()`, and neither an import nor pyflakes will notice a missing
attribute inside a method nobody runs. The method does exist -- but "it exists" was not
established by the check that was run, and a check that cannot fail is not a check. This file
runs the actual widget method, so the whole call graph of the action is executed.

Qt runs under QT_QPA_PLATFORM=offscreen, so there is no window and no display requirement.
The two modal dialogs `_apply_preset` opens are replaced by stubs that RECORD what they were
asked -- a real QMessageBox would block a headless run forever, and stubbing them also lets
the declined path be tested, which is the one where nothing may change.

    python3 council/tests/test_gui_presets.py
(the file sets QT_QPA_PLATFORM itself, so no environment variable is needed. It only needs an
interpreter with PySide6: on THIS host the system `python3` has it -- verified 2026-08-04,
`from PySide6.QtWidgets import QSpinBox` resolves. An earlier version of this line named
`.venv-gui/bin/python3`, which existed on the old WSL2 host and does NOT exist here; running
it gives rc=127, which reads like a broken test rather than a missing interpreter.)
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import council_gui as g  # noqa: E402

P = []
ASKED = []


def check(label, cond):
    P.append(bool(cond))
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")


def stub_question(_parent, title, text, *a, **k):
    ASKED.append(("question", title, text))
    return QMessageBox.StandardButton.Yes


def stub_decline(_parent, title, text, *a, **k):
    ASKED.append(("declined", title, text))
    return QMessageBox.StandardButton.No


def stub_warning(_parent, title, text, *a, **k):
    ASKED.append(("warning", title, text))
    return QMessageBox.StandardButton.Ok


app = QApplication.instance() or QApplication([])
QMessageBox.warning = staticmethod(stub_warning)

# Captured BEFORE anything can run _apply_preset, because it is the baseline the
# "roster.json was never written" check compares against at the end.
ROSTER_AT_START = g.ROSTER_PATH.read_bytes() if g.ROSTER_PATH.exists() else None
print(f"roster.json at start: "
      f"{'absent' if ROSTER_AT_START is None else str(len(ROSTER_AT_START)) + ' bytes'}")

print("== constructing the Config tab (this alone exercises reload/_build_row/cascade) ==")
QMessageBox.question = staticmethod(stub_question)
tab = g.ConfigTab()
check("ConfigTab constructs without raising", tab is not None)
check("the preset combo offers Custom plus every layout",
      tab.preset.count() == len(g.PRESET_LAYOUTS) + 1)
before_rows = tab.table.rowCount()

print("== APPLYING each preset, which runs _apply_preset end to end ==")
for label, (leader_name, voting) in g.PRESET_LAYOUTS.items():
    ASKED.clear()
    idx = tab.preset.findText(label)
    tab.preset.setCurrentIndex(idx)          # fires currentIndexChanged -> _apply_preset
    names = [g.cell_text(tab.table, r, 0) for r in range(tab.table.rowCount())]
    tiers = [g.cell_text(tab.table, r, 1) for r in range(tab.table.rowCount())]
    seats = dict(zip(names, tiers))
    print(f"   {label}: {tab.table.rowCount()} rows, leader box = "
          f"{tab.leader_name.text()!r}/{tab.leader_transport.currentData()!r}")
    check(f"{label}: the operator was ASKED before anything changed",
          any(a[0] == "question" for a in ASKED))
    check(f"{label}: every voting seat is present and tiered voting",
          all(seats.get(n) == "voting" for n in voting))
    check(f"{label}: every inspector seat is present and tiered inspector",
          all(seats.get(n) == "inspector" for n in g.PRESET_INSPECTORS))
    check(f"{label}: no extra seats beyond the preset",
          len(names) == len(voting) + len(g.PRESET_INSPECTORS))
    check(f"{label}: the leader box carries the preset's leader",
          tab.leader_name.text() == leader_name)
    check(f"{label}: the leader's transport is one the engine offers",
          tab.leader_transport.currentData() in g.leader_transports())
    check(f"{label}: no warning dialog was raised", not any(a[0] == "warning" for a in ASKED))
    check(f"{label}: the combo snapped back to Custom (it is an action, not a state)",
          tab.preset.currentText() == g.PRESET_CUSTOM)
    check(f"{label}: the status line says it is NOT saved",
          "NOT SAVED" in tab.status.text())

print("== DECLINING leaves the table untouched ==")
QMessageBox.question = staticmethod(stub_decline)
snapshot = [[g.cell_text(tab.table, r, c) for c in range(5)]
            for r in range(tab.table.rowCount())]
leader_before = tab.leader_name.text()
ASKED.clear()
tab.preset.setCurrentIndex(tab.preset.findText(list(g.PRESET_LAYOUTS)[0]))
after = [[g.cell_text(tab.table, r, c) for c in range(5)]
         for r in range(tab.table.rowCount())]
check("declining asked, then changed NOTHING in the table", ASKED and after == snapshot)
check("declining left the leader box alone", tab.leader_name.text() == leader_before)
check("declining still snaps the combo back to Custom",
      tab.preset.currentText() == g.PRESET_CUSTOM)

print("== a preset the engine cannot build is refused, not half-applied ==")
QMessageBox.question = staticmethod(stub_question)
_real = g.preset_roster
g.preset_roster = lambda _label: None          # the engine-unavailable path
snapshot = [[g.cell_text(tab.table, r, c) for c in range(5)]
            for r in range(tab.table.rowCount())]
ASKED.clear()
tab.preset.setCurrentIndex(tab.preset.findText(list(g.PRESET_LAYOUTS)[0]))
g.preset_roster = _real
after = [[g.cell_text(tab.table, r, c) for c in range(5)]
         for r in range(tab.table.rowCount())]
check("an unbuildable preset WARNS the operator", any(a[0] == "warning" for a in ASKED))
check("and leaves the table exactly as it was", after == snapshot)

print("== ROSTER.JSON WAS NEVER TOUCHED: applying a preset only stages it ==")
# The whole design claim of the feature, checked against the FILE ITSELF.
# The first version of this check read `getattr(tab, "_wrote_roster", ...)` -- an attribute
# council_gui never sets, so hasattr was always False and the check reported ok whatever the
# code did. A void check, inside the file whose docstring is about void checks. The bench
# caught it. What discriminates is the bytes on disk: if _apply_preset ever wrote roster.json,
# the comparison below fails.
check("roster.json is byte-identical to what it was before any preset was applied",
      (g.ROSTER_PATH.read_bytes() if g.ROSTER_PATH.exists() else None) == ROSTER_AT_START)
# ...and the check above can only mean something if a write WOULD have been visible to it.
# So prove the instrument: write a byte, confirm the same comparison goes False, restore.
_probe = (ROSTER_AT_START or b"") + b"\n"
_saved = g.ROSTER_PATH.read_bytes() if g.ROSTER_PATH.exists() else None
try:
    g.ROSTER_PATH.write_bytes(_probe)
    would_catch = ((g.ROSTER_PATH.read_bytes() if g.ROSTER_PATH.exists() else None)
                   != ROSTER_AT_START)
finally:
    if _saved is None:
        g.ROSTER_PATH.unlink(missing_ok=True)
    else:
        g.ROSTER_PATH.write_bytes(_saved)
check("INSTRUMENT PROOF: that same comparison DOES go False when the file is written",
      would_catch)
check("and the file was restored after the instrument proof",
      (g.ROSTER_PATH.read_bytes() if g.ROSTER_PATH.exists() else None) == ROSTER_AT_START)

print("== RETRIEVAL CAP: the spin box round-trips through roster.json and the ENGINE ==")
# THE FAILURE THIS EXISTS FOR is a control that displays a value and drops it. Reading the
# widget back would not catch it; only the engine's own answer does. So this saves, then asks
# the engine what cap it is ACTUALLY using -- a second process, parsing the file the GUI wrote.
import council_gui_engine as ge  # noqa: E402

_saved = g.ROSTER_PATH.read_bytes() if g.ROSTER_PATH.exists() else None
try:
    seeded = tab.retrieval_cap.value()
    lo, hi = tab.retrieval_cap.minimum(), tab.retrieval_cap.maximum()
    check(f"the spin box is seeded from the engine, not from Qt's stock 0..99 ({seeded:,})",
          seeded == tab._cap_default and seeded > 99)
    check(f"...and its range came from the engine too ({lo:,}-{hi:,})", lo > 0 and hi > lo)
    # A value the engine did not suggest, so a stale read cannot produce it by accident.
    raised = seeded + 40_000
    tab.retrieval_cap.setValue(raised)
    check("the widget accepted a raised value (it is inside the engine's range)",
          tab.retrieval_cap.value() == raised)
    tab.save()
    import json as _json
    on_disk = _json.loads(g.ROSTER_PATH.read_text())
    check("save() wrote retrieval_per_file_cap into roster.json",
          on_disk.get("retrieval_per_file_cap") == raised)
    live = ge.print_roster()
    check("THE ENGINE, re-reading that file in its own process, now uses the raised cap",
          live.get("retrieval_per_file_cap") == raised)
    check("...and the engine warns about the raised budget rather than silently taking it",
          any("retrieval_per_file_cap" in w for w in (live.get("warnings") or [])))
    # PERTURB FIRST, or this proves nothing: the widget already holds `raised`, so a reload()
    # that seeded nothing at all would leave it there and the check would pass regardless.
    tab.retrieval_cap.setValue(seeded + 8_000)     # not saved, so the file still says `raised`
    tab.reload()
    check("reload() re-seeds the widget FROM THE ENGINE, discarding an unsaved edit",
          tab.retrieval_cap.value() == raised)
    # BACK TO DEFAULT: the key must be REMOVED, not written as the default value, or a later
    # change to the engine's default would never reach this install.
    tab.retrieval_cap.setValue(seeded)
    tab.save()
    on_disk = _json.loads(g.ROSTER_PATH.read_text())
    check("setting it back to the default REMOVES the key rather than pinning today's number",
          "retrieval_per_file_cap" not in on_disk)
    check("and the engine is back on its own default",
          ge.print_roster().get("retrieval_per_file_cap") == seeded)
finally:
    if _saved is None:
        g.ROSTER_PATH.unlink(missing_ok=True)
    else:
        g.ROSTER_PATH.write_bytes(_saved)
check("roster.json restored to exactly its pre-test bytes",
      (g.ROSTER_PATH.read_bytes() if g.ROSTER_PATH.exists() else None) == ROSTER_AT_START)

print()
print(f"=== GUI presets: {sum(P)}/{len(P)} PASS ===")
sys.exit(0 if all(P) else 1)
