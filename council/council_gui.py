#!/usr/bin/env python3
"""The standalone council GUI -- the operator cockpit (issue #3).

    python3 council_gui.py

FIVE TABS, one per thing the council does: Config, Run, Leader, Brain, Metrics.

THE DISCIPLINE, inherited from the VS Code extension and not weakened here:
  - The GUI NEVER validates a roster. `--print-roster` is the read path, so the engine
    stays the single authority; a second validator would disagree with the first
    eventually and the user would be configuring against a fiction.
  - The GUI NEVER handles API keys. The engine reads them from its own process
    environment; nothing here reads, stores, or displays them.
  - The GUI NEVER imports the engine to RUN it. Fires are subprocesses (see
    council_gui_engine.py), so a hung or crashing fire cannot take the UI with it.
    The one import is `brain_note_banner`, and that is the opposite of duplication: the
    Brain tab must apply the AUTHORITATIVE retrieval gate, and reimplementing it is
    exactly what would defeat it.

WHY QT AND NOT A LOCAL WEB UI. The user's call, 2026-07-31: a Qt app, targeting his Linux
workstation. It also removes an entire vulnerability class -- a desktop app has no
listening socket, so the DNS-rebinding/CSRF exposure documented against loopback-bound
local AI tools (CVE-2024-28224, CVE-2025-66416) does not apply. What it does NOT remove
is untrusted MODEL OUTPUT rendered in the UI, which is this app's whole job; every widget
showing member text below therefore uses setPlainText, never rich text.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

import council_gui_engine as ge

COUNCIL_ROOT = Path(__file__).resolve().parent
ROSTER_PATH = COUNCIL_ROOT / "roster.json"
BRAIN_DIR = COUNCIL_ROOT / "_brain"

# Only these marker files may be toggled -- a whitelist, so no code path here can create
# or delete an arbitrary file. Each is read by the engine per fire, so a change takes
# effect on the NEXT fire, and each is GLOBAL to the install rather than per-session.
MARKERS = {
    "FAST": "all members run at their lowest reasoning effort (faster, not better)",
    "DISABLED": "silence the council's automatic review hook entirely",
    "NO_SHADOW": "disable layer 2, the inspector tier (half the bench)",
    "NO_AUTO_REVERT": "a BLOCK stops warning-and-reverting the file",
}

VERDICT_COLOURS = {"PASS": "#2e7d32", "WARN": "#ef6c00", "BLOCK": "#c62828",
                   "DECLINED": "#6a1b9a", "UNPARSEABLE": "#6d4c41", "ERROR": "#c62828"}


def mono() -> QFont:
    f = QFont("monospace")
    f.setStyleHint(QFont.StyleHint.TypeWriter)
    return f


class FireWorker(QObject):
    """Runs one fire on a worker thread, relaying progress records to the UI thread.

    Qt widgets may only be touched from the UI thread, so this object emits signals and
    never renders anything itself.
    """

    event = Signal(dict)
    finished = Signal(object)

    def __init__(self, args: list[str], stdin_text: str = "",
                 engine: Path | None = None, control: bool = False) -> None:
        super().__init__()
        self.run = ge.EngineRun(args, stdin_text=stdin_text, engine=engine,
                                control=control)

    def start(self) -> None:
        for rec in self.run.stream():
            self.event.emit(rec)
        self.finished.emit(self.run)

    def cancel(self) -> None:
        self.run.cancel()


class ConfigTab(QWidget):
    """Roster, leader and switches. Every read comes from the engine, never from a
    second parse of roster.json."""

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["member", "tier", "transport", "model", "fallback"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 1)

        leader_box = QGroupBox("Leader (the only role that can write files)")
        lb = QHBoxLayout(leader_box)
        self.leader_transport = QComboBox()
        self.leader_transport.addItem("Claude Code harness (no council-native leader)", "")
        for t in ("openrouter", "codex_subprocess", "gemini_rest", "deepseek_https"):
            self.leader_transport.addItem(t, t)
        self.leader_name = QLineEdit(); self.leader_name.setPlaceholderText("name")
        self.leader_model = QLineEdit(); self.leader_model.setPlaceholderText("model slug")
        for w in (QLabel("transport"), self.leader_transport, QLabel("name"),
                  self.leader_name, QLabel("model"), self.leader_model):
            lb.addWidget(w)
        lay.addWidget(leader_box)

        sw = QGroupBox("Switches (take effect on the next fire; GLOBAL to this install)")
        sl = QVBoxLayout(sw)
        self.marker_boxes: dict[str, QCheckBox] = {}
        for name, why in MARKERS.items():
            cb = QCheckBox(f"{name} -- {why}")
            cb.stateChanged.connect(lambda _s, n=name: self.set_marker(n))
            self.marker_boxes[name] = cb
            sl.addWidget(cb)
        lay.addWidget(sw)

        row = QHBoxLayout()
        for label, slot in (("Reload from engine", self.reload),
                            ("Save roster.json", self.save),
                            ("Reset to built-in default", self.reset)):
            b = QPushButton(label); b.clicked.connect(slot); row.addWidget(b)
        lay.addLayout(row)
        self.reload()

    def reload(self) -> None:
        data = ge.print_roster()
        if "error" in data:
            self.status.setText(f"could not read the roster: {data['error']}")
            return
        members = data.get("members", [])
        self.table.setRowCount(len(members))
        for r, m in enumerate(members):
            for c, key in enumerate(("name", "tier", "transport", "model", "fallback_model")):
                self.table.setItem(r, c, QTableWidgetItem(str(m.get(key) or "")))
        leader = data.get("leader") or {}
        idx = self.leader_transport.findData(leader.get("transport") or "")
        self.leader_transport.setCurrentIndex(max(idx, 0))
        self.leader_name.setText(str(leader.get("name") or ""))
        self.leader_model.setText(str(leader.get("model") or ""))
        errs, warns = data.get("errors") or [], data.get("warnings") or []
        bits = [f"active source: {data.get('source')}", f"{len(members)} seats"]
        if errs:
            bits.append(f"ROSTER REJECTED -- running on the built-in default: {'; '.join(errs[:3])}")
        if warns:
            bits.append("warnings: " + "; ".join(warns[:3]))
        self.status.setText("   |   ".join(bits))
        for name, cb in self.marker_boxes.items():
            cb.blockSignals(True)
            cb.setChecked((COUNCIL_ROOT / name).exists())
            cb.blockSignals(False)

    def set_marker(self, name: str) -> None:
        if name not in MARKERS:          # whitelist, enforced at the write, not just the UI
            return
        path = COUNCIL_ROOT / name
        try:
            if self.marker_boxes[name].isChecked():
                path.touch()
            elif path.exists():
                path.unlink()
        except OSError as e:
            QMessageBox.warning(self, "Switch", f"could not update {name}: {e}")
        self.reload()

    def save(self) -> None:
        members = []
        for r in range(self.table.rowCount()):
            def cell(c):
                it = self.table.item(r, c)
                return it.text().strip() if it else ""
            if not cell(0):
                continue
            rec = {"name": cell(0), "tier": cell(1), "transport": cell(2), "model": cell(3)}
            if cell(4):
                rec["fallback_model"] = cell(4)
            members.append(rec)
        roster: dict = {"members": members}
        transport = self.leader_transport.currentData()
        if transport:
            roster["leader"] = {"name": self.leader_name.text().strip() or "leader",
                                "transport": transport,
                                "model": self.leader_model.text().strip()}
        try:
            ROSTER_PATH.write_text(json.dumps(roster, indent=2) + "\n")
        except OSError as e:
            QMessageBox.warning(self, "Roster", f"could not write roster.json: {e}")
            return
        # Re-read through the engine: it may REJECT what was just written, and the user
        # must see the engine's verdict rather than an optimistic "saved".
        self.reload()

    def reset(self) -> None:
        try:
            if ROSTER_PATH.exists():
                ROSTER_PATH.unlink()
        except OSError as e:
            QMessageBox.warning(self, "Roster", f"could not delete roster.json: {e}")
            return
        self.reload()


class RunTab(QWidget):
    """Launch a consult and watch every seat land as it finishes."""

    def __init__(self) -> None:
        super().__init__()
        self.thread: QThread | None = None
        self.worker: FireWorker | None = None
        lay = QVBoxLayout(self)

        self.pitch = QPlainTextEdit()
        self.pitch.setPlaceholderText("The proposal, design decision, or diff to put before the council...")
        lay.addWidget(self.pitch, 1)

        row = QHBoxLayout()
        self.layer = QComboBox(); self.layer.addItems(["reasoning", "posttool", "stop_prose"])
        self.go = QPushButton("Run council"); self.go.clicked.connect(self.start)
        self.stop = QPushButton("Stop"); self.stop.clicked.connect(self.cancel)
        self.stop.setEnabled(False)
        self.state = QLabel("idle")
        for w in (QLabel("layer"), self.layer, self.go, self.stop, self.state):
            row.addWidget(w)
        row.addStretch(1)
        lay.addLayout(row)

        self.seats = QTableWidget(0, 5)
        self.seats.setHorizontalHeaderLabels(["seat", "tier", "round", "verdict", "took"])
        self.seats.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.seats, 1)
        self.rows: dict[str, int] = {}

        self.out = QPlainTextEdit(); self.out.setReadOnly(True); self.out.setFont(mono())
        lay.addWidget(self.out, 2)

    def start(self) -> None:
        text = self.pitch.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "Run", "The pitch is empty.")
            return
        self.seats.setRowCount(0); self.rows.clear(); self.out.clear()
        self.go.setEnabled(False); self.stop.setEnabled(True)
        self.state.setText("running")
        self.worker = FireWorker(["--layer", self.layer.currentText()], stdin_text=text)
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.worker.event.connect(self.on_event)
        self.worker.finished.connect(self.on_finished)
        self.thread.start()

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.state.setText("stopping")

    def on_event(self, rec: dict) -> None:
        ev = rec.get("ev")
        if ev == "member_started":
            name = str(rec.get("member"))
            r = self.rows.get(name)
            if r is None:
                r = self.seats.rowCount(); self.seats.insertRow(r); self.rows[name] = r
            for c, v in enumerate((name, rec.get("tier"), rec.get("round"), "running", "")):
                self.seats.setItem(r, c, QTableWidgetItem(str(v)))
        elif ev in ("member_finished", "member_corrected"):
            name = str(rec.get("member"))
            r = self.rows.get(name)
            if r is None:
                r = self.seats.rowCount(); self.seats.insertRow(r); self.rows[name] = r
                self.seats.setItem(r, 0, QTableWidgetItem(name))
            verdict = str(rec.get("verdict") or "")
            item = QTableWidgetItem(verdict)
            if verdict in VERDICT_COLOURS:
                item.setForeground(Qt.GlobalColor.black)
                item.setData(Qt.ItemDataRole.ToolTipRole, VERDICT_COLOURS[verdict])
            self.seats.setItem(r, 3, item)
            took = rec.get("duration_s")
            self.seats.setItem(r, 4, QTableWidgetItem(f"{took}s" if took is not None else ""))
            if ev == "member_corrected":
                self.append(f"[{name}] verdict corrected {rec.get('was')} -> {verdict} "
                            f"({rec.get('why')})")
        elif ev == "round_started":
            self.append(f"-- round {rec.get('round')} --")
        elif ev == "tool_request":
            self.append(f"[{rec.get('member')}] {rec.get('kind')}: "
                        f"{'granted' if rec.get('granted') else 'DENIED'}")
        elif ev == "dropped":
            self.append(f"[{rec.get('n')} progress record(s) dropped -- the UI fell "
                        f"behind; the council itself was unaffected]")
        elif ev == "final_verdict":
            self.append(f"VERDICT: {rec.get('verdict')}    log: {rec.get('log_path')}")

    def append(self, line: str) -> None:
        # setPlainText/appendPlainText only: this text originates from models and must
        # never be interpreted as rich text.
        self.out.appendPlainText(line)

    def on_finished(self, run) -> None:
        self.go.setEnabled(True); self.stop.setEnabled(False)
        if run.start_error:
            self.state.setText("failed")
            self.append(f"could not start: {run.start_error}")
        elif run.cancelled:
            self.state.setText("stopped")
            self.append("[stopped by operator; the engine and everything it spawned were killed]")
        else:
            self.state.setText(f"done (exit {run.returncode})")
        if run.stdout:
            self.append("\n" + run.stdout)
        if self.thread is not None:
            self.thread.quit(); self.thread.wait(5000)


class BrainTab(QWidget):
    """The vault as a traversable wiki -- the user's framing, 2026-07-31.

    Notes are rendered THROUGH the engine's retrieval gate. That is the whole point: the
    gate withholds a note's gloss on five statuses (INVALID, SUPERSEDED, FAILING,
    GATE_UNAVAILABLE, UNREADABLE), and a viewer that read the files directly would happily
    display the gloss of a note the gate refuses to serve -- defeating the one mechanism
    that stops a refuted note being read as fact.
    """

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        self.status = QLabel(""); lay.addWidget(self.status)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.index = QListWidget(); self.index.currentItemChanged.connect(self.show_note)
        split.addWidget(self.index)
        right = QWidget(); rl = QVBoxLayout(right)
        self.banner = QLabel(""); self.banner.setWordWrap(True)
        self.body = QPlainTextEdit(); self.body.setReadOnly(True); self.body.setFont(mono())
        self.links = QListWidget(); self.links.itemActivated.connect(self.follow)
        rl.addWidget(self.banner)
        rl.addWidget(self.body, 3)
        rl.addWidget(QLabel("links out / backlinks (double-click to follow)"))
        rl.addWidget(self.links, 1)
        split.addWidget(right); split.setSizes([260, 700])
        lay.addWidget(split, 1)
        self.notes: dict[str, Path] = {}
        self.backlinks: dict[str, set[str]] = {}
        self.reload()

    def reload(self) -> None:
        import re
        self.notes.clear(); self.backlinks.clear(); self.index.clear()
        if not BRAIN_DIR.is_dir():
            self.status.setText(f"no vault at {BRAIN_DIR}")
            return
        outlinks: dict[str, set[str]] = {}
        for p in sorted(BRAIN_DIR.glob("*.md")):
            text = p.read_text(errors="replace")
            m = re.search(r"^id:\s*(\S+)", text, re.M)
            nid = m.group(1) if m else p.stem
            self.notes[nid] = p
            outlinks[nid] = set(re.findall(r"\[\[([^\]]+)\]\]", text))
        for src, dests in outlinks.items():
            for d in dests:
                self.backlinks.setdefault(d, set()).add(src)
        self.outlinks = outlinks
        for nid in sorted(self.notes):
            self.index.addItem(QListWidgetItem(nid))
        dangling = {d for ds in outlinks.values() for d in ds} - set(self.notes)
        orphans = [n for n in self.notes if not self.backlinks.get(n)]
        self.status.setText(
            f"{len(self.notes)} notes   |   "
            f"{sum(len(v) for v in outlinks.values())} links   |   "
            f"{len(dangling)} dangling   |   {len(orphans)} with no backlink")

    def show_note(self, cur, _prev=None) -> None:
        if cur is None:
            return
        nid = cur.text()
        path = self.notes.get(nid)
        if path is None:
            return
        raw = path.read_text(errors="replace")
        status, banner, body = self._gated(path, raw)
        self.banner.setText(f"[{status}] {banner or ''}")
        # body is None when the gate WITHHOLDS the gloss; show the reason, not the text.
        self.body.setPlainText(
            body if body is not None else
            f"-- gloss withheld by the retrieval gate (status {status}) --\n\n"
            f"The gate refuses to serve this note's gloss. Read the file directly only if "
            f"you intend to audit it:\n{path}")
        self.links.clear()
        for out in sorted(self.outlinks.get(nid, ())):
            mark = "" if out in self.notes else "   (DANGLING)"
            self.links.addItem(f"-> {out}{mark}")
        for back in sorted(self.backlinks.get(nid, ())):
            self.links.addItem(f"<- {back}")

    def _gated(self, path: Path, raw: str):
        """Ask the ENGINE's gate, never a local reimplementation."""
        try:
            import consult_council as cc
            # THREE arguments: (disp, content, workdir). A two-argument call raises
            # TypeError, which this method's own except would swallow into
            # GATE_UNAVAILABLE -- so every note would render as "withheld" and the tab
            # would look cautious while being simply broken. Fail-closed hides arity bugs.
            return cc.brain_note_banner(str(path), raw, COUNCIL_ROOT)
        except Exception as e:                       # noqa: BLE001
            # Fail CLOSED: if the gate cannot be consulted, withhold rather than display.
            return ("GATE_UNAVAILABLE", f"could not consult the retrieval gate: {e}", None)

    def follow(self, item: QListWidgetItem) -> None:
        target = item.text().split(maxsplit=1)[-1].replace("   (DANGLING)", "").strip()
        for i in range(self.index.count()):
            if self.index.item(i).text() == target:
                self.index.setCurrentRow(i)
                return


class LeaderTab(QWidget):
    """Drive a leader turn, with the permission mode in front of you.

    The leader is the only role that can write files, so this tab is where issue #8's
    permission modes live rather than being a separate gate somewhere else:

      auto          apply every write the COUNCIL permits (the wall still blocks a BLOCK)
      approve-each  be asked first, seeing target, verdict and review text
      plan-only     never apply anything; the council still reviews so you see what
                    WOULD have happened

    approve-each answers travel on a dedicated control pipe, not stdin -- stdin carries
    the task and is closed straight after, so a decision read from there would EOF and
    silently decline every write.
    """

    def __init__(self) -> None:
        super().__init__()
        self.thread: QThread | None = None
        self.worker: FireWorker | None = None
        lay = QVBoxLayout(self)

        row = QHBoxLayout()
        self.mode = QComboBox(); self.mode.addItems(["approve-each", "plan-only", "auto"])
        self.workdir = QLineEdit(str(COUNCIL_ROOT))
        self.go = QPushButton("Run turn"); self.go.clicked.connect(self.start)
        self.stop = QPushButton("Stop"); self.stop.clicked.connect(self.cancel)
        self.stop.setEnabled(False)
        self.state = QLabel("idle")
        for w in (QLabel("mode"), self.mode, QLabel("workdir"), self.workdir,
                  self.go, self.stop, self.state):
            row.addWidget(w)
        lay.addLayout(row)

        self.leader_label = QLabel("")
        self.leader_label.setWordWrap(True)
        lay.addWidget(self.leader_label)

        self.task = QPlainTextEdit()
        self.task.setPlaceholderText("The task for the leader to carry out...")
        lay.addWidget(self.task, 1)

        self.out = QPlainTextEdit(); self.out.setReadOnly(True); self.out.setFont(mono())
        lay.addWidget(self.out, 2)
        self.refresh_leader()

    def refresh_leader(self) -> None:
        data = ge.print_roster()
        leader = (data or {}).get("leader") or {}
        if leader.get("transport"):
            self.leader_label.setText(
                f"leader: {leader.get('name')} ({leader.get('transport')} "
                f"{leader.get('model') or ''}) -- writes go through the council wall")
        else:
            self.leader_label.setText(
                "No council-native leader is configured. The Claude Code harness leads by "
                "default and this tab cannot drive it -- set a leader in the Config tab "
                "(roster.json's top-level `leader` key) to run turns from here.")

    def start(self) -> None:
        task = self.task.toPlainText().strip()
        if not task:
            QMessageBox.information(self, "Leader", "The task is empty.")
            return
        self.out.clear()
        self.go.setEnabled(False); self.stop.setEnabled(True)
        self.state.setText("running")
        args = ["--task", task, "--workdir", self.workdir.text().strip() or str(COUNCIL_ROOT),
                "--mode", self.mode.currentText()]
        need_control = self.mode.currentText() == "approve-each"
        self.worker = FireWorker(args, engine=COUNCIL_ROOT / "council_leader_run.py",
                                 control=need_control)
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.worker.event.connect(self.on_event)
        self.worker.finished.connect(self.on_finished)
        self.thread.start()

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.state.setText("stopping")

    def on_event(self, rec: dict) -> None:
        ev = rec.get("ev")
        if ev == "approval_request":
            self.ask(rec)
        elif ev == "leader_action":
            applied = "APPLIED" if rec.get("applied") else "not applied"
            self.out.appendPlainText(
                f"{rec.get('action')} {rec.get('target')}: {rec.get('verdict')} -- {applied}"
                + (f"  ({rec.get('reason')})" if rec.get("reason") else ""))
        elif ev == "note":
            self.out.appendPlainText(str(rec.get("text")))
        elif ev == "final_verdict":
            self.out.appendPlainText(str(rec.get("verdict")))

    def ask(self, rec: dict) -> None:
        """Show the proposed write and relay the decision. Anything but Yes declines."""
        req_id = str(rec.get("id"))
        box = QMessageBox(self)
        box.setWindowTitle("Approve this write?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"{rec.get('target')}\n\ncouncil verdict: {rec.get('verdict')}   "
                    f"({rec.get('bytes')} bytes)")
        # setDetailedText, never rich text: this carries model-authored content.
        box.setDetailedText(f"--- proposed content ---\n{rec.get('preview')}\n\n"
                            f"--- council review ---\n{rec.get('review')}")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        approved = box.exec() == QMessageBox.StandardButton.Yes
        sent = False
        if self.worker is not None and approved:
            sent = self.worker.run.send_control(f"APPROVE {req_id}")
        elif self.worker is not None:
            sent = self.worker.run.send_control(f"DECLINE {req_id}")
        self.out.appendPlainText(
            f"{req_id}: {'approved' if approved else 'declined'}"
            + ("" if sent else "  (could not reach the turn; it will decline on its own)"))

    def on_finished(self, run) -> None:
        self.go.setEnabled(True); self.stop.setEnabled(False)
        if run.start_error:
            self.state.setText("failed"); self.out.appendPlainText(run.start_error)
        elif run.cancelled:
            self.state.setText("stopped")
        else:
            self.state.setText(f"done (exit {run.returncode})")
        for blob in (run.stdout, run.stderr):
            if blob:
                self.out.appendPlainText("\n" + blob)
        if self.thread is not None:
            self.thread.quit(); self.thread.wait(5000)


class MetricsTab(QWidget):
    """What the logs support, and explicitly what they do not.

    TWO MEASURED FACTS SHAPE THIS TAB, and both are about refusing to overstate:

    1. COST COVERAGE IS PARTIAL. Seats on a subprocess transport record no cost field at
       all, so summing what IS recorded produces a number that silently omits them. This
       tab therefore names the unpriced seats and labels the figure a PARTIAL subtotal
       rather than a total.
    2. A MISSING fast_mode KEY MEANS UNKNOWN, NOT FALSE. Logs written before that field
       existed lack it, and `entry.get("fast_mode")` is falsey for those -- which reads
       "unknown provenance" as "full depth" and pools reduced-depth fires into the same
       numbers. This tab tests for key PRESENCE and reports unknown-depth fires
       separately.
    """

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.days = QComboBox(); self.days.addItems(["last 1 day", "last 3 days", "last 7 days"])
        self.days.setCurrentIndex(1)
        b = QPushButton("Recompute"); b.clicked.connect(self.reload)
        row.addWidget(QLabel("window")); row.addWidget(self.days); row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.out = QPlainTextEdit(); self.out.setReadOnly(True); self.out.setFont(mono())
        lay.addWidget(self.out, 1)
        self.reload()

    def reload(self) -> None:
        n = {0: 1, 1: 3, 2: 7}[self.days.currentIndex()]
        logs = COUNCIL_ROOT / "logs"
        if not logs.is_dir():
            self.out.setPlainText(f"no logs directory at {logs}")
            return
        days = [d for d in sorted(logs.iterdir()) if d.is_dir()][-n:]
        fires = 0
        verdicts: dict[str, int] = {}
        depth_known = depth_fast = depth_unknown = 0
        priced = 0.0
        priced_seats: set[str] = set()
        unpriced_seats: set[str] = set()
        slowest: list[float] = []
        for d in days:
            for f in d.glob("*.json"):
                try:
                    e = json.loads(f.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                fires += 1
                v = str(e.get("final_verdict") or "?")
                verdicts[v] = verdicts.get(v, 0) + 1
                if "fast_mode" in e:
                    depth_known += 1
                    depth_fast += 1 if e["fast_mode"] else 0
                else:
                    depth_unknown += 1
                durs = []
                for m in list(e.get("members") or []) + list(e.get("shadow") or []):
                    durs.append(m.get("duration_s") or 0)
                    role = str(m.get("role") or "?")
                    if isinstance(m.get("cost"), (int, float)):
                        priced += float(m["cost"]); priced_seats.add(role)
                    else:
                        unpriced_seats.add(role)
                if durs:
                    slowest.append(max(durs))
        if not fires:
            self.out.setPlainText(f"no fires in the last {n} log day(s)")
            return
        slowest.sort()
        med = slowest[len(slowest) // 2] if slowest else 0
        p90 = slowest[int(0.9 * len(slowest))] if slowest else 0
        lines = [
            f"window: {len(days)} log day(s), {fires} fires",
            "",
            "verdicts:  " + "   ".join(f"{k}={v}" for k, v in sorted(verdicts.items())),
            "",
            f"slowest seat per fire:  median {med}s   p90 {p90}s   max {slowest[-1] if slowest else 0}s",
            "   SCOPE: this is the max PER-SEAT duration. It excludes prompt assembly, the",
            "   round barriers and the tool legs, so it is a LOWER BOUND on wall-clock.",
            "",
            f"PARTIAL cost subtotal: ${priced:.4f}",
            f"   priced seats ({len(priced_seats)}): {', '.join(sorted(priced_seats)) or 'none'}",
            f"   UNPRICED seats ({len(unpriced_seats)}): {', '.join(sorted(unpriced_seats)) or 'none'}",
            "   This is NOT a total. Seats above that record no cost are missing from it,",
            "   and no figure here estimates them.",
            "",
            f"review depth:  {depth_known} fires recorded it ({depth_fast} at reduced depth)",
            f"               {depth_unknown} fires have NO fast_mode key -- UNKNOWN, not full",
            "               depth. Those are excluded from the reduced-depth count rather",
            "               than assumed to be full-depth fires.",
        ]
        self.out.setPlainText("\n".join(lines))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Workers' Council")
        self.resize(1100, 800)
        tabs = QTabWidget()
        tabs.addTab(ConfigTab(), "Config")
        tabs.addTab(RunTab(), "Run")
        tabs.addTab(LeaderTab(), "Leader")
        tabs.addTab(BrainTab(), "Brain")
        tabs.addTab(MetricsTab(), "Metrics")
        self.setCentralWidget(tabs)


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
