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
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

import council_gui_engine as ge

COUNCIL_ROOT = Path(__file__).resolve().parent


def _roster_path() -> Path:
    """The roster file THIS PROCESS will read and write.

    The override is read HERE FIRST rather than only via the engine. A first version
    asked the engine and fell back to a bare `roster.json`, and the council caught the
    hole: `except Exception` is much broader than "cannot be imported" -- it also swallows
    an import-time error or an AttributeError -- so with COUNCIL_ROSTER_PATH SET and the
    import failing for any reason, the fallback silently ignored the override and
    disagreed with the engine. Reading the variable directly makes the override survive
    every failure mode; the engine is still asked for the no-override answer so the
    default lives in one place.
    """
    override = os.environ.get("COUNCIL_ROSTER_PATH")
    if override:
        p = Path(override).expanduser()
        return p if p.is_absolute() else COUNCIL_ROOT / p
    try:
        import consult_council as cc
        return cc.ROSTER_PATH
    except Exception:
        return COUNCIL_ROOT / "roster.json"


def hook_roster_path(harness: str | None = None) -> Path:
    """The roster a HOOK-DRIVEN fire will read, which is NOT necessarily this GUI's.

    THE BUG THIS EXISTS FOR IS STILL OPEN, and the honest statement is that this function
    REPORTS it rather than fixing it. `hook_env.sh` exports
    COUNCIL_ROSTER_PATH=roster.<harness>-led.json and every hook runs through that
    wrapper, but THE GUI DOES NOT: `vscode-extension/src/extension.ts` `launchGui()`
    spawns `cp.spawn(python, [script])` on council_gui.py directly, with no wrapper.
    A GUI LAUNCHED THAT WAY RESOLVES `roster.json`, AND BOTH ROUTES THROUGH _roster_path()
    AGREE ON IT -- which is the part a reviewer rightly asked to be sourced rather than
    asserted. With COUNCIL_ROSTER_PATH unset, consult_council.py's else-branch sets
    `ROSTER_PATH = COUNCIL_ROOT / "roster.json"`, so the engine-import route returns
    roster.json; and the `except` fallback returns the same literal. The claim therefore
    holds in the NORMAL case, not merely the degraded one. Meanwhile the hooks read
    `roster.<harness>-led.json`, so a bench edited and saved here is not the bench the
    next fire uses.
    A CORRECTION KEPT ON PURPOSE: an earlier draft of this docstring said "no launcher
    script and no .desktop entry references council_gui.py" and called it measured. That
    was FALSE -- the extension above is exactly such a launcher, and the council caught
    it. The conclusion did not change (that launcher spawns bare, so the mismatch is real
    and is now sourced rather than assumed), but the evidence behind it was wrong, which
    is the more dangerous half.

    WHY THIS ONLY WARNS AND DOES NOT REDIRECT THE SAVE: choosing which file the GUI writes
    is a configuration decision with real consequences either way, not a defect with one
    correct answer. Silently switching the destination would surprise anyone who has been
    editing roster.json on purpose. So the mismatch is SURFACED, matching the ruling
    already made for the family-overlap banner: announce loudly, block nothing.

    This DOES duplicate hook_env.sh's naming rule, which is the drift engine_rules()
    warns about, and the duplication is deliberate and bounded: the GUI cannot ask a shell
    wrapper it never invokes. If that naming changes in hook_env.sh, change it here too.
    """
    # RESOLVED FROM THE ENVIRONMENT, not hardcoded. hook_env.sh exports COUNCIL_HARNESS
    # alongside COUNCIL_ROSTER_PATH, so when this GUI was launched through the wrapper the
    # harness is KNOWN. A hardcoded "claude" compared a codex-led install against
    # roster.claude-led.json and would have raised a FALSE mismatch -- caught by a layer-2
    # inspector, and it matters here because codex-led is an active configuration in this
    # project, not a hypothetical one.
    # ON A BARE LAUNCH THE HARNESS IS NOT KNOWABLE: no hook has run and nothing records
    # which one will. "claude" is hook_env.sh's own default, so the two agree wherever an
    # answer exists at all -- but a bare-launched GUI on a codex-only install can still
    # name the wrong file, and that is a real limit of warning from outside the wrapper.
    # PRESERVE-IF-SET COMES FIRST, mirroring the wrapper: hook_env.sh only assigns the
    # harness-led default when COUNCIL_ROSTER_PATH is EMPTY, so an explicitly exported
    # value is what the hooks actually read. Without this the helper cried wolf in the one
    # case where the user had already made the GUI and the hooks agree -- a false alarm in
    # the function whose entire job is to prevent one. Caught by the council.
    override = os.environ.get("COUNCIL_ROSTER_PATH")
    if override:
        p = Path(override).expanduser()
        return p if p.is_absolute() else COUNCIL_ROOT / p
    harness = harness or os.environ.get("COUNCIL_HARNESS") or "claude"
    return COUNCIL_ROOT / f"roster.{harness}-led.json"


ROSTER_PATH = _roster_path()
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


def leader_transports() -> list[str]:
    """The transports a LEADER may use, read from the engine's LEADER_TRANSPORTS.

    NOT VALID_TRANSPORTS: that is the MEMBER set and is strictly larger, and offering it
    would put a transport in the dropdown that _call_leader's chain has no branch for --
    a leader that fails at dispatch with ok=False. I made exactly that mistake and the
    bench caught it. (The example it caught me with was `claude_subprocess`, which has
    SINCE been given a branch and is now a legitimate leader; the rule is unchanged, only
    that instance of it is gone.)

    Falls back to an EMPTY list if the import fails, so the GUI still opens. Empty rather
    than a transcribed copy on purpose: a hardcoded list here is the very drift this
    function exists to prevent, and it went stale within a day when the engine gained a
    transport. An empty dropdown is visibly broken; a stale one is silently wrong.
    """
    try:
        import consult_council as cc
        return list(cc.LEADER_TRANSPORTS)
    except Exception:
        return []


# The two shipped bench layouts, plus the escape hatch. DERIVED, NOT TRANSCRIBED: each
# member's transport/model/fallback comes from the engine's own DEFAULT_REGISTRY by name,
# and the one seat that registry does not carry -- claude -- is built from the engine's
# CLAUDE_MODEL / CLAUDE_OPENROUTER_FALLBACK constants. Nothing here re-types a model slug,
# because a transcribed slug is exactly what goes stale when the engine moves.
PRESET_CUSTOM = "Custom (edit the table yourself)"
PRESET_LAYOUTS = {
    "Claude leads": ("claude", ["codex", "gemini", "deepseek", "kimi", "grok", "glm"]),
    "Codex leads": ("codex", ["claude", "gemini", "deepseek", "kimi", "grok", "glm"]),
}
PRESET_INSPECTORS = ["hunyuan", "qwen", "minimax", "mimo", "nemotron", "mistral"]


def preset_roster(label: str) -> dict | None:
    """Build one preset roster dict, or None if `label` is not a preset (e.g. Custom).

    Returns the same shape `save()` writes, so applying a preset and saving by hand produce
    the same file. Raises nothing: if the engine cannot be imported the caller gets None and
    the UI says so, rather than writing a roster built from guesses.
    """
    if label not in PRESET_LAYOUTS:
        return None
    try:
        import consult_council as cc
    except Exception:
        return None
    by_name = {m.name: m for m in cc.DEFAULT_REGISTRY}

    def seat(name: str, tier: str) -> dict:
        m = by_name.get(name)
        if m is not None:
            rec = {"name": name, "tier": tier, "transport": m.transport, "model": m.model}
            if m.fallback_model:
                rec["fallback_model"] = m.fallback_model
            return rec
        # Only `claude` reaches here today: the built-in registry seats codex as the
        # subscription voter and has no claude row, so its values come from the constants
        # the claude transport itself runs on.
        if name == "claude":
            return {"name": "claude", "tier": tier, "transport": "claude_subprocess",
                    "model": cc.CLAUDE_MODEL,
                    "fallback_model": cc.CLAUDE_OPENROUTER_FALLBACK}
        raise KeyError(f"preset names {name!r}, which is in neither the registry nor the "
                       "claude special case")

    leader_name, voting = PRESET_LAYOUTS[label]
    try:
        members = ([seat(n, "voting") for n in voting]
                   + [seat(n, "inspector") for n in PRESET_INSPECTORS])
        lead = seat(leader_name, "voting")   # borrow its transport/model, not its tier
    except KeyError:
        # A preset naming a seat the engine no longer knows. Same posture as a failed
        # import: return None and let the caller say so, rather than writing a roster with
        # a guessed transport and model.
        return None
    return {"members": members,
            "leader": {"name": leader_name, "transport": lead["transport"],
                       "model": lead["model"]}}


def engine_rules() -> dict:
    """Every constraint the roster editor needs, READ FROM THE ENGINE.

    Zero literals is the whole point. The same rule expressed in two places has drifted
    TWICE in this project: the leader dropdown (a GUI copy that a later "fix" made wrong by
    deriving it from the MEMBER transport set) and the fallback gate (a dispatch leg added
    without updating the validator that permits it). A third incident is often miscounted
    as drift and is NOT: the canonical-name rejection came from the validator working
    exactly as intended against a wrong instruction. A cascading editor multiplies the
    places a rule can be expressed, so it takes all of them from here.

    The fallback question is the subtle one and the bench caught me getting it backwards:
    FALLBACK_CAPABLE_TRANSPORTS is the set of DIRECT-VENDOR transports that have an
    OpenRouter retry leg. It is NOT the set of transports allowed to carry a fallback.
    `openrouter` may always carry one -- the validator's openrouter branch never checks
    it -- so gating on the tuple alone would have disabled fallback for the transport that
    uses it most. See fallback_allowed().
    """
    try:
        import consult_council as cc
        return {
            "tiers": sorted(cc.VALID_TIERS),
            "transports": sorted(cc.VALID_TRANSPORTS),
            "canonical": dict(cc.CANONICAL_TRANSPORT_NAMES),
            "direct_models": dict(cc.DIRECT_TRANSPORT_MODELS),
            "fallback_direct": tuple(cc.FALLBACK_CAPABLE_TRANSPORTS),
        }
    except Exception:
        # The GUI still opens without the engine; the editor just cannot cascade.
        return {"tiers": [], "transports": [], "canonical": {},
                "direct_models": {}, "fallback_direct": ()}


def fallback_allowed(transport: str, rules: dict) -> bool:
    """Whether a fallback_model is legal on `transport`, matching the validator's shape:
    unconditional for openrouter, and otherwise only for direct-vendor transports that
    actually have a retry leg."""
    return transport == "openrouter" or transport in rules["fallback_direct"]


def cell_text(table, row: int, col: int) -> str:
    """Read a cell whether it holds a widget or a plain item.

    Tolerant on purpose: the roster editor uses cell WIDGETS (combo boxes, line edits) so
    the columns can constrain each other, but a row can still hold plain items, and a
    reader that understood only one shape would silently return "" for the other -- which
    for the name column means the seat is dropped from the saved roster without a word.
    """
    w = table.cellWidget(row, col)
    if w is not None:
        if isinstance(w, QComboBox):
            return w.currentText().strip()
        if isinstance(w, QLineEdit):
            return w.text().strip()
    it = table.item(row, col)
    return it.text().strip() if it else ""


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
                 engine: Path | None = None, control: bool = False,
                 interrupt: bool = False) -> None:
        super().__init__()
        self.run = ge.EngineRun(args, stdin_text=stdin_text, engine=engine,
                                control=control, interrupt=interrupt)

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
        # Read ONCE, before any widget is built: _build_row and _apply_cascade both need
        # them, and reload() runs at the end of this constructor.
        self.rules = engine_rules()
        self.known_slugs: list[str] = []
        lay = QVBoxLayout(self)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        # PRESETS. An ACTION, not a state display: the combo does not track what the table
        # currently holds, it applies a layout to it. Selecting one only STAGES the change
        # (the table and leader box are rewritten); roster.json is untouched until Save, the
        # same rule every other edit in this tab follows. Confirmed first, because a
        # mis-click would otherwise silently discard a hand-built bench.
        prow = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.addItem(PRESET_CUSTOM, "")
        for label in PRESET_LAYOUTS:
            self.preset.addItem(label, label)
        self.preset.currentIndexChanged.connect(lambda _i: self._apply_preset())
        prow.addWidget(QLabel("preset"))
        prow.addWidget(self.preset, 1)
        prow.addWidget(QLabel("(stages the table -- Save roster.json to persist)"))
        lay.addLayout(prow)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["member", "tier", "transport", "model", "fallback"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 1)

        leader_box = QGroupBox("Leader (the only role that can write files)")
        lb = QHBoxLayout(leader_box)
        self.leader_transport = QComboBox()
        self.leader_transport.addItem("Claude Code harness (no council-native leader)", "")
        for t in leader_transports():
            self.leader_transport.addItem(t, t)
        self.leader_name = QLineEdit(); self.leader_name.setPlaceholderText("name")
        self.leader_model = QLineEdit(); self.leader_model.setPlaceholderText("model slug")
        for w in (QLabel("transport"), self.leader_transport, QLabel("name"),
                  self.leader_name, QLabel("model"), self.leader_model):
            lb.addWidget(w)
        self.leader_transport.currentIndexChanged.connect(
            lambda _i: self._leader_cascade())
        lay.addWidget(leader_box)

        # FAMILY-OVERLAP BANNER. The user's ruling of 2026-08-05: a single-family bench is
        # a LEGITIMATE configuration (someone may genuinely want a council of claudes), so
        # this warns LOUDLY and blocks NOTHING. Hidden entirely when there is nothing to
        # say, because a banner that is always present is a banner nobody reads.
        # The text is built from the ENGINE's structured overlap field, never from a
        # second family rule computed here -- a duplicated rule is the drift this file's
        # own engine_rules() docstring records being bitten by twice.
        self.overlap_banner = QLabel("")
        self.overlap_banner.setWordWrap(True)
        self.overlap_banner.setStyleSheet(
            "background:#8a4b00; color:#ffffff; padding:6px; border-radius:4px;")
        self.overlap_banner.hide()
        lay.addWidget(self.overlap_banner)

        # RETRIEVAL BUDGET. The user's ruling of 2026-08-03: 128k is the right default, but the
        # user must be able to raise it per install. The value, its default and its bounds all
        # come from the ENGINE via print_roster -- the GUI hardcodes none of them, for the same
        # reason it never parses roster.json itself: a second source of truth eventually
        # disagrees with the first, and the user would be configuring against a fiction.
        # Seeded in reload(); range and suffix are set there too.
        ret_box = QGroupBox("Member file retrieval")
        rl = QHBoxLayout(ret_box)
        self.retrieval_cap = QSpinBox()
        self.retrieval_cap.setGroupSeparatorShown(True)
        self.retrieval_cap.setSingleStep(8_000)
        self.retrieval_cap_note = QLabel()
        self.retrieval_cap_note.setWordWrap(True)
        rl.addWidget(QLabel("bytes per granted file"))
        rl.addWidget(self.retrieval_cap)
        rl.addWidget(self.retrieval_cap_note, 1)
        lay.addWidget(ret_box)

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
        # Add/Remove exist because R6 is "the exact SIZE and constitution of the council",
        # and size cannot change if the table can only be re-typed. Add appends a row
        # PRE-SEEDED with tier=voting / transport=openrouter. Save writes roster.json
        # immediately without judging it; the engine's verdict appears on the NEXT read
        # (reload / print_roster), which is where a bad seat surfaces as "ROSTER REJECTED"
        # with the engine's own error list -- so the UI never re-implements validation.
        for label, slot in (("Add member", self.add_member),
                            ("Remove selected", self.remove_member),
                            ("Reload from engine", self.reload),
                            ("Save roster.json", self.save),
                            ("Reset to built-in default", self.reset)):
            b = QPushButton(label); b.clicked.connect(slot); row.addWidget(b)
        lay.addLayout(row)
        self.reload()

    def _apply_preset(self) -> None:
        """Stage a preset layout into the table and the leader box. Never writes a file.

        The two failure modes are handled the same way -- do nothing and say why -- because
        the alternative is a half-applied bench: `preset_roster` returns None if the engine
        cannot be imported or names a seat it no longer knows, and a declined confirmation
        leaves everything untouched. In both cases the combo snaps back to Custom so it never
        claims a layout the table does not hold.
        """
        label = self.preset.currentData() or ""
        if not label:
            return                      # Custom: the table is whatever the user made it
        roster = preset_roster(label)
        if roster is None:
            QMessageBox.warning(self, "Preset",
                                f"could not build the {label!r} preset from the engine -- "
                                "the roster is unchanged.")
            self._select_preset(PRESET_CUSTOM)
            return
        seats = ", ".join(f"{m['name']}({m['tier'][:3]})" for m in roster["members"])
        if QMessageBox.question(
                self, "Apply preset",
                f"Replace the roster table with '{label}'?\n\n"
                f"leader: {roster['leader']['name']} ({roster['leader']['transport']})\n"
                f"seats: {seats}\n\n"
                "Nothing is written until you press Save roster.json."
        ) != QMessageBox.StandardButton.Yes:
            self._select_preset(PRESET_CUSTOM)
            return
        self.table.setRowCount(0)
        self.table.setRowCount(len(roster["members"]))
        for r, m in enumerate(roster["members"]):
            self._build_row(r, m)
        lead = roster["leader"]
        leader_note = f"leader {lead['name']}"
        idx = self.leader_transport.findData(lead["transport"])
        if idx < 0:
            leader_note = "LEADER UNCHANGED (its transport is not offered)"
            # The engine no longer offers this leader transport. The leader box is LEFT AS
            # IT WAS -- the table now holds the preset's seats while the leader is whatever
            # was selected before, so both the dialog and the status line say so explicitly
            # rather than letting that mismatch pass as a clean apply.
            QMessageBox.warning(self, "Preset",
                                f"the table was applied, but transport "
                                f"{lead['transport']!r} is not in LEADER_TRANSPORTS, so the "
                                "leader was left alone -- set it by hand before saving.")
        else:
            self.leader_transport.setCurrentIndex(idx)
            self.leader_name.setText(lead["name"])
            self.leader_model.setText(lead["model"])
            # After the setText calls, exactly as reload() does: setting the index above
            # fires the cascade, and the two setText calls then overwrite what it forced.
            self._leader_cascade()
        self._warn_duplicate_names()
        self.status.setText(f"preset '{label}' staged -- {len(roster['members'])} seats, "
                            f"{leader_note}. NOT SAVED: press Save roster.json to "
                            "write it, or Reload from engine to discard.")
        self._select_preset(PRESET_CUSTOM)

    def _select_preset(self, label: str) -> None:
        """Move the combo without re-entering _apply_preset."""
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(max(self.preset.findText(label), 0))
        self.preset.blockSignals(False)

    def _row_of(self, widget) -> int:
        """Which row a cell widget sits in. Found by scanning rather than captured in the
        signal connection, because Remove renumbers every row after it -- a captured index
        would quietly edit the wrong seat."""
        for r in range(self.table.rowCount()):
            for c in range(self.table.columnCount()):
                if self.table.cellWidget(r, c) is widget:
                    return r
        return -1

    def _build_row(self, r: int, m: dict) -> None:
        """Populate one row with the constrained editors. Column 0 name and columns 3/4
        model+fallback are EDITABLE, so a slug newer than this UI is always typable; the
        cascade only locks them where the engine forces one legal value."""
        rules = self.rules
        name = QLineEdit(str(m.get("name") or ""))
        tier = QComboBox(); tier.addItems(rules["tiers"])
        ti = tier.findText(str(m.get("tier") or ""))
        tier.setCurrentIndex(ti if ti >= 0 else 0)
        transport = QComboBox(); transport.addItems(rules["transports"])
        pi = transport.findText(str(m.get("transport") or ""))
        transport.setCurrentIndex(pi if pi >= 0 else 0)
        model = QComboBox(); model.setEditable(True)
        model.addItems(self.known_slugs)
        model.setCurrentText(str(m.get("model") or ""))
        fb = QComboBox(); fb.setEditable(True)
        fb.addItems(self.known_slugs)
        fb.setCurrentText(str(m.get("fallback_model") or ""))
        for c, w in enumerate((name, tier, transport, model, fb)):
            self.table.setCellWidget(r, c, w)
        transport.currentTextChanged.connect(
            lambda _t, w=transport: self._apply_cascade(self._row_of(w)))
        self._apply_cascade(r)

    def _apply_cascade(self, r: int) -> None:
        """Make the row express what the engine will actually accept.

        For the four direct-vendor transports the validator forces BOTH the record name
        (CANONICAL_TRANSPORT_NAMES) and the model (DIRECT_TRANSPORT_MODELS) to one value
        each, so those cells are filled in and locked rather than offered as a choice --
        a one-option dropdown would imply a decision that does not exist. openrouter
        leaves both free. Fallback follows fallback_allowed(), which is NOT the bare
        FALLBACK_CAPABLE_TRANSPORTS tuple; see engine_rules().
        """
        if r < 0:
            return
        rules = self.rules
        name = self.table.cellWidget(r, 0)
        transport = self.table.cellWidget(r, 2)
        model = self.table.cellWidget(r, 3)
        fb = self.table.cellWidget(r, 4)
        if not all((name, transport, model, fb)):
            return
        t = transport.currentText()
        canonical = rules["canonical"].get(t)
        forced_model = rules["direct_models"].get(t)
        if canonical is not None:
            name.setText(canonical)
            name.setReadOnly(True)
            name.setToolTip(f"forced by transport {t}")
        else:
            name.setReadOnly(False)
            name.setToolTip("")
        if forced_model is not None:
            model.setCurrentText(forced_model)
            model.setEnabled(False)
            model.setToolTip(f"{t} reads its model from a module constant")
        else:
            model.setEnabled(True)
            model.setToolTip("")
        allowed = fallback_allowed(t, rules)
        if not allowed:
            fb.setCurrentText("")
        fb.setEnabled(allowed)
        fb.setToolTip("" if allowed else f"fallback_model is not read on {t}")
        self._warn_duplicate_names()

    def _leader_cascade(self) -> None:
        """The same derive-and-lock rule for the Leader box.

        This exists because the leader box is where the rule actually bit: a leader was
        named `codex-leader` for transport codex_subprocess, and _validate_leader runs the
        SAME _validate_transport_model path as a member, so the canonical name is forced
        there too. Constraining only the members table would have left the exact path that
        failed still able to fail.
        """
        rules = self.rules
        t = self.leader_transport.currentData() or ""
        canonical = rules["canonical"].get(t)
        forced_model = rules["direct_models"].get(t)
        if canonical is not None:
            self.leader_name.setText(canonical)
            self.leader_name.setReadOnly(True)
            self.leader_name.setToolTip(f"forced by transport {t}")
        else:
            self.leader_name.setReadOnly(False)
            self.leader_name.setToolTip("")
        if forced_model is not None:
            self.leader_model.setText(forced_model)
            self.leader_model.setReadOnly(True)
            self.leader_model.setToolTip(f"{t} reads its model from a module constant")
        else:
            self.leader_model.setReadOnly(False)
            self.leader_model.setToolTip("")
        # No transport selected means the Claude Code harness leads and neither field is
        # written to roster.json at all, so leaving them editable would invite typing
        # into fields that go nowhere.
        harness = not t
        self.leader_name.setEnabled(not harness)
        self.leader_model.setEnabled(not harness)

    def _warn_duplicate_names(self) -> None:
        """Surface a canonical-name clash BEFORE Save. Auto-fill can produce two seats
        both named `codex`, which the engine rejects as a duplicate -- better to see it
        while editing than as a rejection afterwards. Advisory only: the engine remains
        the authority and nothing here blocks a save."""
        seen: dict[str, int] = {}
        dupes = []
        for r in range(self.table.rowCount()):
            n = cell_text(self.table, r, 0)
            if not n:
                continue
            if n in seen:
                dupes.append(n)
            seen[n] = r
        if dupes:
            self.status.setText(
                f"duplicate member name(s): {', '.join(sorted(set(dupes)))} -- the engine "
                f"will reject this roster. Two seats cannot share a name, and the direct-"
                f"vendor transports each force one.")

    def add_member(self) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        # Seeded openrouter/voting: that is the shape 11 of 12 default seats take, and it
        # is the only transport leaving both name and model free to fill in.
        self._build_row(r, {"tier": "voting", "transport": "openrouter"})
        self.table.setCurrentCell(r, 0)
        self.status.setText("row added -- name and model, then Save roster.json")

    def remove_member(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            self.status.setText("select a row first (click any cell in it)")
            return
        for r in rows:
            self.table.removeRow(r)
        self.status.setText(f"removed {len(rows)} row(s) -- not saved until you "
                            f"press Save roster.json")

    def reload(self) -> None:
        data = ge.print_roster()
        if "error" in data:
            self.status.setText(f"could not read the roster: {data['error']}")
            return
        members = data.get("members", [])
        # Suggestions, never a whitelist: every slug already in the roster plus the
        # constants. The combo stays editable so a model newer than this UI is typable --
        # a frozen list would be a third source of truth and would block new models.
        self.known_slugs = sorted({str(m.get("model")) for m in members if m.get("model")}
                                  | {str(m.get("fallback_model")) for m in members
                                     if m.get("fallback_model")}
                                  | set(self.rules["direct_models"].values()))
        self.table.setRowCount(0)
        self.table.setRowCount(len(members))
        for r, m in enumerate(members):
            self._build_row(r, m)
        leader = data.get("leader") or {}
        idx = self.leader_transport.findData(leader.get("transport") or "")
        self.leader_transport.setCurrentIndex(max(idx, 0))
        self.leader_name.setText(str(leader.get("name") or ""))
        self.leader_model.setText(str(leader.get("model") or ""))
        # LAST, and unconditionally. Setting the index above fires the cascade, but the
        # two setText calls then overwrite whatever it forced -- so the forced values have
        # to be re-applied after them. Unconditional because selecting index 0 emits no
        # change signal when it was already 0, and the harness case still has to disable
        # the two fields.
        self._leader_cascade()
        # FAMILY OVERLAP, rendered from the engine's structured field. Nothing here
        # recomputes what a family is; if the field is absent (an older engine) the banner
        # simply stays hidden rather than guessing.
        ov = data.get("leader_family_overlap") or {}
        parts = []
        if ov.get("voting"):
            parts.append(f"VOTING seats share the leader's family ({ov.get('family')}): "
                         f"{', '.join(ov['voting'])} -- these vote on the leader's own "
                         f"writes")
        if ov.get("inspector"):
            parts.append(f"inspector seats in the same family: "
                         f"{', '.join(ov['inspector'])} (advisory, changes no verdict)")
        if ov.get("undetermined"):
            parts.append(f"family UNDETERMINED for {', '.join(ov['undetermined'])} -- not "
                         f"a clean bill of health, these could not be compared at all")
        notices = []
        if parts:
            notices.append(
                "LEADER/MEMBER FAMILY OVERLAP -- allowed, nothing is blocked; shown so "
                "the result is not mistaken for independent review. " + "; ".join(parts)
                + ".")
        # ROSTER DESTINATION MISMATCH. Not a family question at all, but the same class of
        # hazard and so it shares the banner: a configuration that does not mean what the
        # user thinks. Raised whenever the two paths DIFFER -- see the note below for why
        # existence is deliberately not part of the test.
        hookp = hook_roster_path()
        if hookp != ROSTER_PATH:
            # NO exists() GATE, and its removal is a fix rather than a tightening. An
            # earlier version warned only when hookp ALREADY existed, reasoning that a
            # fresh install has "no divergence to warn about". That is backwards: on a
            # fresh install the hooks route to hookp on their FIRST fire, so edits saved
            # here are lost from the very beginning -- the gate hid the case where the
            # warning was most useful. The honest test is whether the two paths DIFFER,
            # not whether one of them happens to exist yet.
            # AND THE TEXT HEDGES WHAT IT CANNOT KNOW. Without COUNCIL_HARNESS this window
            # was not launched through the wrapper, so which harness will run is genuinely
            # unknowable from here and "claude" is only hook_env.sh's default. Stating the
            # filename flatly would name the wrong file on a codex-led install -- the code
            # knew it might be wrong while the user saw a definitive claim.
            assumed = "" if os.environ.get("COUNCIL_HARNESS") else (
                " -- assuming the 'claude' harness, since this window was not launched "
                "through hook_env.sh and which harness will run cannot be known from here")
            notices.append(
                f"ROSTER MISMATCH: this window edits and saves {ROSTER_PATH.name}, but "
                f"hooks run through hook_env.sh and would read {hookp.name}{assumed}. A "
                f"bench saved here is NOT the bench your next PostToolUse review uses. To "
                f"edit that one, launch this GUI through hook_env.sh (or export "
                f"COUNCIL_ROSTER_PATH).")
        if notices:
            self.overlap_banner.setText("  ||  ".join(notices))
            self.overlap_banner.show()
        else:
            self.overlap_banner.hide()
        # RETRIEVAL BUDGET, seeded from the engine. Bounds first, then the value: setValue
        # CLAMPS to the current range, so seeding a 128,000 default into a spin box still
        # holding Qt's stock 0..99 maximum would silently store 99. Signals are blocked
        # because reload() runs on Save, and a valueChanged handler firing mid-reload would
        # be reacting to the engine's own answer rather than to the user.
        cap = int(data.get("retrieval_per_file_cap") or 0)
        cap_default = int(data.get("retrieval_per_file_cap_default") or 0)
        lo = int(data.get("retrieval_per_file_cap_min") or 0)
        hi = int(data.get("retrieval_per_file_cap_max") or 0)
        self.retrieval_cap.blockSignals(True)
        if hi >= lo > 0:
            self.retrieval_cap.setRange(lo, hi)
        if cap:
            self.retrieval_cap.setValue(cap)
        self.retrieval_cap.blockSignals(False)
        self._cap_default = cap_default
        note = (f"default {cap_default:,}" if cap_default else "")
        if cap and cap_default and cap > cap_default:
            note += (f" -- RAISED to {cap:,}: a grant may now carry up to that many bytes, "
                     f"and a member that requests a file of at least that size pays it")
        elif cap and cap_default and cap < cap_default:
            note += (f" -- LOWERED to {cap:,}: a file larger than that is truncated, and a "
                     f"member may not be able to reach what it is reviewing")
        if lo and hi:
            note += f"   (allowed {lo:,}-{hi:,})"
        self.retrieval_cap_note.setText(note)
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
            def cell(c, _r=r):
                return cell_text(self.table, _r, c)
            if not cell(0):
                continue
            rec = {"name": cell(0), "tier": cell(1), "transport": cell(2), "model": cell(3)}
            if cell(4):
                rec["fallback_model"] = cell(4)
            members.append(rec)
        roster: dict = {"members": members}
        # WRITTEN ONLY WHEN IT DIFFERS FROM THE DEFAULT. Persisting the default would bake
        # today's 128,000 into every roster.json, so a later change to the engine's default
        # would silently not reach any install that had ever pressed Save. Absent means
        # "whatever the engine's default is", which is what the user chose by not changing it.
        cap = int(self.retrieval_cap.value())
        if cap and cap != getattr(self, "_cap_default", 0):
            roster["retrieval_per_file_cap"] = cap
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
        if ev == "run_started":
            # THE SEAT LIST AND THE DEPTH, BEFORE ANY VERDICT ARRIVES. Both are things the
            # operator otherwise cannot see and both have burned this project:
            #   - a missing API key drops seats SILENTLY: the dropped seat is announced on
            #     stderr and leaves no record in the log, and the fire still returns a
            #     confident-looking verdict from whoever remained. This list is the bench that
            #     actually ran. THE ORDER IS THE WHOLE POINT AND IS EASY TO GET BACKWARDS --
            #     a review of this comment argued the opposite -- so check it rather than
            #     assume: in consult_council.main(), the key-drop loop rebinds `members` and
            #     the run_started emit passes `list(members)` AFTER it. Were the emit first,
            #     this would print the configured roster and show nothing.
            #   - fast_mode is `FAST_PATH.exists()`, and FAST_PATH is a file in the COUNCIL
            #     DIRECTORY (consult_council.py:248), so it is one marker for the whole
            #     install rather than per session and another session can set it for this
            #     one. What it changes, traced rather than assumed: FAST_PATH -> the
            #     _FAST_SNAPSHOT at :279 -> fast_mode() -> effort_for(), which returns
            #     FAST_EFFORT instead of _FULL_EFFORT, with openrouter seats getting a
            #     FAST-aware effort of their own. It moves EFFORT ONLY -- models and round
            #     count are untouched. Say "lower effort", not "lower quality" -- but do not
            #     read that as reassurance either. The measured position is that low effort is
            #     FASTER and has never been measured as GOOD; nobody has shown it is as good,
            #     which is not the same as showing it is no worse. The banner states what
            #     changed and leaves the quality question open, because that is where it is.
            voting = rec.get("voting") or []
            inspectors = rec.get("inspectors") or []
            self.append(f"== {rec.get('layer')} / {rec.get('tool_name')} -> "
                        f"{rec.get('target_path')}")
            self.append(f"   voting({len(voting)}): {', '.join(map(str, voting)) or 'NONE'}"
                        f"   inspectors({len(inspectors)}): "
                        f"{', '.join(map(str, inspectors)) or 'none'}")
            if rec.get("fast_mode"):
                self.append("   FAST is armed (install-wide): members will run at LOW "
                            "reasoning effort, same models and rounds")
        elif ev == "member_started":
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
        elif ev == "round_finished":
            # THE PER-ROUND SNAPSHOT, which the seats table cannot keep. That table holds one
            # cell per member and round 2 OVERWRITES round 1 in it, so by the time a fire ends
            # the independent round-1 verdicts are gone from the display even though they are
            # in the log. Printing them here keeps both rounds visible in one place, which is
            # the comparison that matters: round 2 is where members see each other, so a
            # verdict that moves between the two is the only visible sign of peer exposure.
            # It does NOT distinguish herding from being genuinely persuaded -- nothing in
            # this stream can -- it just stops the earlier position from vanishing.
            verdicts = rec.get("verdicts") or {}
            if verdicts:
                shown = ", ".join(f"{k}={v}" for k, v in sorted(verdicts.items()))
                self.append(f"-- round {rec.get('round')} verdicts: {shown}")
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

    The GPU checkbox is the tab's other control, and it is a SANDBOX decision rather than a
    permission one -- it varies what the turn's EXEC can REACH, not what it may write.
    council_leader_run builds the profile from it (:167-172): ticked gives
    cc.elevated_exec_profile(gpu=True), unticked leaves profile=None so run_exec_sandbox
    falls back to its default. Read those two functions for the actual bounds rather than
    trusting a summary here; this docstring deliberately does not restate them, because a
    copied list of limits is the thing that goes stale when one of them changes.

    The turn streams as it runs -- rounds, the leader's prose, actions and rejected
    envelopes -- through on_event below.
    """

    def __init__(self) -> None:
        super().__init__()
        self.thread: QThread | None = None
        self.worker: FireWorker | None = None
        lay = QVBoxLayout(self)

        row = QHBoxLayout()
        self.mode = QComboBox(); self.mode.addItems(["approve-each", "plan-only", "auto"])
        self.workdir = QLineEdit(str(COUNCIL_ROOT))
        # ELEVATION IS A PER-TURN OPERATOR DECISION, which is why it is a checkbox here and
        # not a roster key: members fire unattended, a leader turn has a person watching it.
        # UNTICKED IS NOT THE MEMBER SANDBOX, and this comment used to say it was
        # ("byte-for-byte"). Line references, so this is checkable rather than asserted:
        #   the PROFILE does match. Unticked leaves profile=None (council_leader_run.py:167)
        #   and run_exec_sandbox takes profile=None as its default (consult_council.py:4735),
        #   resolving it at :4790 via `profile = profile or default_exec_profile()` -- the
        #   same default profile VALUES a member fire gets. Not the same OBJECT:
        #   default_exec_profile() is a function that builds a fresh ExecProfile per call,
        #   deliberately, so the module's constants stay live knobs (see its docstring).
        #   THE SCRATCH IS WHAT DIFFERS. council_leader_run.py:208 creates a per-turn scratch
        #   for EVERY leader turn, ticked or not, and run_exec_sandbox binds it read-write at
        #   /scratch (consult_council.py:4840, `--bind <scratch> EXEC_SCRATCH_MOUNT`). The
        #   member path at :5005 calls run_exec_sandbox with no scratch argument at all.
        # So an unticked leader turn still has one writable location that survives the turn;
        # a member never does. That is a difference in what EXEC can WRITE, not only reach.
        self.gpu = QCheckBox("GPU")
        self.gpu.setToolTip(
            "Let this turn's EXEC reach the host GPU, with memory bounded by a cgroup and "
            "the host's own CPU/file-size limits inherited. Network stays OFF. Refused "
            "before the turn starts if this host has no GPU device.")
        self.go = QPushButton("Run turn"); self.go.clicked.connect(self.start)
        self.stop = QPushButton("Stop"); self.stop.clicked.connect(self.cancel)
        self.stop.setEnabled(False)
        # ABORT AND STOP ARE DIFFERENT, and both are offered because they fail differently.
        # STOP kills the process group: nothing is written, no handoff is authored, and a
        # conversation loses the turn entirely. ABORT asks the turn to end: the running
        # command is killed, remaining actions are skipped, and the record and handoff are
        # still produced -- so a conversation keeps what the turn actually did. Stop is the
        # hammer for a wedged process; Abort is the one to reach for first.
        self.abort = QPushButton("Abort turn"); self.abort.clicked.connect(self.send_abort)
        self.abort.setEnabled(False)
        self.steer = QLineEdit(); self.steer.setPlaceholderText(
            "steer the turn (delivered at the next prompt) -- press Enter")
        self.steer.returnPressed.connect(self.send_steer)
        self.steer.setEnabled(False)
        self.state = QLabel("idle")
        for w in (QLabel("mode"), self.mode, QLabel("workdir"), self.workdir, self.gpu,
                  self.go, self.stop, self.abort, self.steer, self.state):
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
            # THREE DIFFERENT CAUSES, and the old single message covered all of them
            # badly. Since DEFAULT_LEADER shipped, an ABSENT roster.json yields a leader,
            # so arriving here means one of: the ENGINE could not be read at all, the
            # roster was REJECTED, or the roster deliberately omits `leader`. Each needs a
            # different action, and all three are distinguishable from the payload:
            # `error` (engine unreadable), then `errors` (roster rejected), then the
            # residual case where neither is present and no leader transport was returned.
            if (data or {}).get("error"):
                # THE ENGINE FAILED: ge.print_roster returns {"error": ...} on a launch
                # failure, timeout or non-zero exit, and refresh_leader (unlike
                # ConfigTab.reload, which guards at its top) has no such guard. Nothing
                # about roster.json is KNOWN on this path, so say that.
                # PROVENANCE, checkable rather than asserted: a draft of this branch
                # claimed "no top-level `leader`" on the engine-failure path -- a false
                # claim about a file that had never been read. Both layers flagged it in
                # logs/2026-08-06/20260806T124309Z-6fe7c77c.json.
                self.leader_label.setText(
                    f"Could not read the roster from the engine: {data['error']}. "
                    "Whether a leader is configured is UNKNOWN -- this is not a statement "
                    "about roster.json's contents.")
                return
            errs = (data or {}).get("errors") or []
            if errs:
                self.leader_label.setText(
                    "roster.json was REJECTED, so no leader is active and this tab cannot "
                    f"run turns. First error: {errs[0]} -- fix it in the Config tab. The "
                    "council falls back to the built-in default bench, so fires still "
                    "run on the panel the engine chose rather than the one you configured.")
            else:
                self.leader_label.setText(
                    "This roster.json has no top-level `leader`, so the Claude Code "
                    "harness leads and this tab cannot drive it. To run turns from here, "
                    "set one in the Config tab -- transport `claude_subprocess` with name "
                    "`claude` is the leader shipped by default when there is no "
                    "roster.json at all. This says nothing about whether the hooks are "
                    "active; a leader and the PostToolUse review are separate paths.")

    def start(self) -> None:
        task = self.task.toPlainText().strip()
        if not task:
            QMessageBox.information(self, "Leader", "The task is empty.")
            return
        self.out.clear()
        # THE MODE IS LOCKED FOR THE DURATION, not just the Run button. Every read of it
        # happens in this method, before the worker thread starts -- once for --mode and once
        # for the approve-each control-pipe decision below -- so leaving the combo live let it
        # drift while a turn ran, and the control could read "plan-only" while the turn it
        # launched went on applying writes under "auto". Nothing consults the widget after
        # launch, so disabling it changes no behaviour; it stops the display from
        # contradicting the running turn. on_finished restores it on every exit path.
        self.go.setEnabled(False); self.stop.setEnabled(True); self.mode.setEnabled(False)
        self.abort.setEnabled(True); self.steer.setEnabled(True)
        self.state.setText("running")
        args = ["--task", task, "--workdir", self.workdir.text().strip() or str(COUNCIL_ROOT),
                "--mode", self.mode.currentText()]
        if self.gpu.isChecked():
            args.append("--gpu")
        need_control = self.mode.currentText() == "approve-each"
        # ALWAYS an interrupt channel for a leader turn, in every mode. Stopping is not a
        # mode-specific privilege, and a plan-only turn can still spend an hour in an EXEC.
        self.worker = FireWorker(args, engine=COUNCIL_ROOT / "council_leader_run.py",
                                 control=need_control, interrupt=True)
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.worker.event.connect(self.on_event)
        self.worker.finished.connect(self.on_finished)
        self.thread.start()

    def send_abort(self) -> None:
        """Ask the turn to end gracefully. Unlike Stop, the record and handoff still land."""
        if self.worker is None:
            return
        # A FAILED SEND IS REPORTED, NEVER SWALLOWED, and that is the difference from an
        # approval: a control answer that goes missing DECLINES the write, erring toward doing
        # nothing, whereas an interrupt that goes missing leaves the turn RUNNING. An operator
        # who pressed Abort and saw nothing would reasonably conclude it had stopped.
        if self.worker.run.send_interrupt("ABORT"):
            self.state.setText("aborting")
            self.out.appendPlainText("[operator] ABORT sent -- the turn will end and still "
                                     "write its record and handoff")
        else:
            self.out.appendPlainText("[operator] ABORT COULD NOT BE SENT -- the turn is "
                                     "STILL RUNNING. Use Stop to kill the process group.")

    def send_steer(self) -> None:
        """Send a message to the leader without stopping the turn."""
        if self.worker is None:
            return
        msg = self.steer.text().strip()
        if not msg:
            return
        if self.worker.run.send_interrupt(f"STEER {msg}"):
            self.steer.clear()
            self.out.appendPlainText(f"[operator] STEER queued: {msg}")
        else:
            self.out.appendPlainText("[operator] STEER COULD NOT BE SENT -- the leader will "
                                     "not see it.")

    def cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.state.setText("stopping")

    def on_event(self, rec: dict) -> None:
        """Render one streamed event. Anything unhandled is DROPPED SILENTLY, which is why
        the set below has to track what the engine actually emits.

        THAT DROP IS NOT HYPOTHETICAL: this handler covered only approval_request,
        leader_action, note and final_verdict, while run_leader_turn had been emitting
        leader_round / leader_text / leader_problem (council_leader.py:853, 865, 878) and
        council_leader_run a leader_action_final recap. Four event kinds arrived and
        vanished. BE EXACT ABOUT WHAT THAT COST, because the original complaint that opened
        this item said the tab "shows NOTHING until the whole task finishes" and that is not
        true of the code this replaced: writes, notes, approval prompts and the final
        verdict already streamed live. What was missing is the leader's REASONING -- round
        boundaries, the prose of each reply, and envelopes rejected whole -- so a turn
        showed its actions with nothing explaining them.

        FIELD NAMES ARE READ FROM THE EMITTERS, not guessed: leader_round carries `round`;
        leader_text `round` and `text`; leader_problem `round` and `problems` (emitted at
        council_leader.py:853, 865, 878). Check a new field against its emitter rather than
        against this list, which goes stale the moment the engine adds one.
        """
        ev = rec.get("ev")
        if ev == "run_started":
            # The leader path emits this with layer="leader_turn" and tool_name set to the
            # PERMISSION MODE, which is the one field here that changes what the turn may do
            # to the tree: auto applies every write the council permits, approve-each asks
            # first, plan-only never applies anything. The tab's combo box shows what was
            # SELECTED; this shows what the running turn was actually launched with, and the
            # two can differ if the box moved after Run was pressed.
            self.out.appendPlainText(
                f"== leader turn: mode={rec.get('tool_name')} "
                f"leader={', '.join(map(str, rec.get('voting') or [])) or 'unknown'} "
                f"workdir={rec.get('target_path')}")
        elif ev == "approval_request":
            self.ask(rec)
        elif ev == "leader_round":
            self.out.appendPlainText(f"\n--- round {rec.get('round')} ---")
        elif ev == "leader_text":
            # The round's COMPLETE reply, delivered once the model call returns -- not a
            # token stream. It arrives BEFORE that round's actions, which is the ordering
            # that makes the tab readable: reasoning, then what it did about it.
            text = str(rec.get("text") or "").rstrip()
            if text:
                self.out.appendPlainText(text)
        elif ev == "leader_family_overlap":
            # Announced ONCE, at seat time, before any round. Never blocking: a
            # single-family bench is a legitimate configuration. Rendered here so an
            # operator watching a turn scroll past sees WHY the verdicts that follow may
            # not be independent -- the Config tab's banner is only seen by someone who
            # went looking at the roster.
            voting = rec.get("voting") or []
            inspector = rec.get("inspector") or []
            undet = rec.get("undetermined") or []
            bits = []
            if voting:
                bits.append(f"VOTING: {', '.join(voting)}")
            if inspector:
                bits.append(f"inspectors: {', '.join(inspector)}")
            if undet:
                bits.append(f"UNDETERMINED: {', '.join(undet)}")
            self.out.appendPlainText(
                f"[leader family overlap] {rec.get('leader')} is family "
                f"{rec.get('family') or '?'}; same family on its own review panel -- "
                + "; ".join(bits)
                + ". Allowed and not blocked; shown so the result is not mistaken for "
                  "independent review.")
        elif ev == "leader_reprompt":
            # The turn tried to END here with no WRITE ever emitted, and the harness sent it
            # back once. Shown because the alternative is a round that reads, in the live
            # stream, as the leader inexplicably speaking twice in a row -- and because an
            # operator watching a plan-only turn needs to know the extra model call was the
            # harness's doing, not the leader's.
            self.out.appendPlainText(
                f"[round {rec.get('round')}] ended with no WRITE emitted -- re-prompted once")
        elif ev == "leader_steer":
            # THE HUMAN redirecting the turn, as distinct from leader_reprompt, which is the
            # HARNESS nudging it. Rendered as QUEUED, never as delivered, and the reason is
            # that the two arrival points differ: a steer read at a ROUND BOUNDARY goes into
            # that round's prompt, assembled moments later, so it is all but certain to land;
            # one buffered during an IN-FLIGHT CALL waits for the following round, which a
            # turn ending first never reaches. The event cannot tell them apart, so the
            # weaker of the two claims is the honest one to show.
            # "THE NEXT PROMPT ASSEMBLED" is exact and covers both arrival points: a steer
            # read at a ROUND BOUNDARY goes into THAT round's prompt, assembled moments later,
            # while one buffered during an in-flight call waits for the following round. An
            # earlier version said "the next prompt", which was false for the boundary case.
            self.out.appendPlainText(
                f"[round {rec.get('round')}] operator steer QUEUED for the next prompt "
                f"assembled (none is assembled if the turn ends first)")
        elif ev == "leader_problem":
            # An actions envelope rejected WHOLE -- none of it ran. Surfaced loudly because
            # the turn otherwise looks like a round that simply chose to do nothing.
            problems = rec.get("problems") or []
            joined = "; ".join(str(p) for p in problems) if problems else "unspecified"
            self.out.appendPlainText(
                f"[round {rec.get('round')}] ACTIONS REJECTED, none ran: {joined}")
        elif ev in ("leader_action", "leader_action_final"):
            # THREE EMITTERS, TWO SCHEMAS, ONE EVENT NAME -- and this branch used to know only
            # one of them, so every non-write action rendered as "read a.py: None -- not
            # applied": the verdict field does not exist on that schema, and `ok` (True) was
            # never read, so a successful read displayed as a failure and its note -- the byte
            # count, the exit status, the denial reason -- was dropped entirely.
            #   council_leader.py:983      leader_action        round, action, target, ok, note
            #   council_leader_run.py:158  leader_action        action, target, verdict,
            #                                                   applied, reason   (WRITES only)
            #   council_leader_run.py:224  leader_action_final  action, target, applied,
            #                                                   verdict="", reason
            # Only the middle one carries a council verdict, because only a write is reviewed.
            # In the recap, `applied` is populated from `res.ok`, so it means "the action
            # succeeded" and is worded that way rather than as an application decision.
            if ev == "leader_action_final" or "ok" in rec:
                outcome = "ok" if (rec.get("ok") if "ok" in rec
                                   else rec.get("applied")) else "FAILED"
                detail = str(rec.get("note") or rec.get("reason") or "")
            else:
                verdict = str(rec.get("verdict") or "")
                outcome = "APPLIED" if rec.get("applied") else "not applied"
                outcome = f"{verdict} -- {outcome}" if verdict else outcome
                detail = str(rec.get("reason") or "")
            prefix = "recap: " if ev == "leader_action_final" else ""
            self.out.appendPlainText(
                f"{prefix}{rec.get('action')} {rec.get('target')}: {outcome}"
                + (f"  ({detail})" if detail else ""))
        elif ev == "note":
            self.out.appendPlainText(str(rec.get("text")))
        elif ev == "dropped":
            # The SAME emitter serves both tabs and emits this on either stream, but only
            # RunTab was listening -- so a leader turn that outran its UI lost records AND the
            # notice that it had. That is the worst shape for this tab in particular: its
            # content is the leader's REASONING, so a silent gap reads as the leader having
            # said less than it did, and nothing distinguishes that from a quiet turn.
            self.out.appendPlainText(
                f"[{rec.get('n')} progress record(s) dropped -- the UI fell behind; the "
                f"turn itself was unaffected]")
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
        # EVERY control start() disabled is restored HERE, and mode is now in that set. A
        # disable without a matching re-enable froze the combo permanently after the first
        # turn -- the tab kept working and silently stopped being reconfigurable, which is the
        # shape of bug that survives testing because nothing raises. This runs on the failed,
        # cancelled and completed paths alike, since all three land here.
        self.go.setEnabled(True); self.stop.setEnabled(False); self.mode.setEnabled(True)
        # DISABLED WITH THE REST. An Abort button live after the turn ended would write into a
        # closed pipe and report a failure the operator cannot act on.
        self.abort.setEnabled(False); self.steer.setEnabled(False)
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
