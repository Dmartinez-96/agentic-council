#!/usr/bin/env python3
"""Watch a council fire live in the terminal -- the terminal half of issue #9.

The engine reports nothing until it exits, and a fire is slow enough that the silence
matters. Re-derive the distribution rather than trusting a number here:

    python3 -c "
    import json,pathlib,statistics
    w=[]
    for d in sorted(pathlib.Path('logs').iterdir())[-3:]:
        if not d.is_dir(): continue
        for f in d.glob('*.json'):
            try: e=json.load(open(f))
            except Exception: continue
            ds=[m.get('duration_s',0) for m in e.get('members',[])]+[m.get('duration_s',0) for m in (e.get('shadow') or [])]
            if ds: w.append(max(ds))
    print(len(w), statistics.median(w), sorted(w)[int(.9*len(w))])"

This wrapper spawns the engine with `--events-fd`, renders each seat AS IT LANDS, and
leaves the engine's own stdout untouched so anything already parsing it keeps working.

    council_watch.py --layer reasoning < pitch.txt
    council_watch.py --layer posttool --members codex --tool-name Write --target-path f.py

Argument handling, stated exactly: `--no-colour` is CONSUMED by this wrapper, `-h`/`--help`
print THIS script's help and exit without starting the engine, `--events-fd` is REJECTED
(the wrapper owns both ends of that pipe and will not silently override a caller's
choice), and everything else is forwarded to consult_council.py unchanged. To read the
engine's own help, run consult_council.py directly.

WHY A WRAPPER RATHER THAN A FLAG ON THE ENGINE. The events fd must be opened by whoever
consumes it. A wrapper owns both ends of the pipe, so the engine keeps a single
responsibility and the same NDJSON stream feeds this renderer and the GUI identically --
one producer, two consumers, no second format to drift.

COLOUR IS OFF UNLESS STDERR IS A TTY. The renderer draws on STDERR, keeping stdout a clean
passthrough for the engine's verdict; and the engine's own output is captured by the
PostToolUse hook, where escape codes would land inside the recorded verdict text.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# THE MARKER LAYOUT AND THE LIVENESS RULE BOTH BELONG TO council_advisor, which writes them.
# BOTH MODES USE THE ADVISOR WHEN IT IS PRESENT; only --follow REQUIRES it, per the tolerant
# import below. The dependency map, by enclosing function, because a file-wide grep of `ca.`
# lists the names a file mentions but cannot attribute any of them to a calling mode:
#   main(), the default mode, which WRITES a fire's marker:
#       write_pending_marker, _events_path, clear_pending_marker
#   follow(), which READS fires: marker_is_live, read_events, summarise_partial, _events_path
#   and the three helpers follow() itself calls -- _session_markers (_pending_dir),
#       _host_markers (EVIDENCE_STATE_ROOT, PENDING_DIRNAME) and _fire_block (read_events,
#       summarise_partial, _events_path)
# Importing them is what keeps this file from growing a second, drifting definition of where a
# marker lives and when a fire counts as running.
# NAMED, NOT NUMBERED, deliberately: a line number cited across files is a pointer that goes
# stale silently the next time the other file grows, and grep finds a def just as fast.
# TOLERANT IMPORT, and here is what its absence now costs, which is no longer nothing: with no
# council_advisor the default mode still runs the fire and still renders it in THIS terminal,
# but writes no marker, so no other process can see it. --follow refuses outright, guarding on
# `ca is None` at entry. A package shipping without the advisor loses visibility, not review.
try:
    import council_advisor as ca
except Exception:  # noqa: BLE001
    ca = None

ENGINE = Path(__file__).resolve().parent / "consult_council.py"

# 8-colour ANSI only: 256-colour and truecolour are not universal across terminals, and a
# verdict must never be legible only on some of them. Colour is an ACCENT here -- every
# line also carries the verdict word, so a colour-blind reader or a piped log loses
# nothing.
_C = {"PASS": "\033[32m", "WARN": "\033[33m", "BLOCK": "\033[31m",
      "DELIBERATING": "\033[36m", "UNPARSEABLE": "\033[35m", "ERROR": "\033[31m"}
_DIM, _BOLD, _OFF = "\033[2m", "\033[1m", "\033[0m"

# Distinct accents per seat so a reader tracks a member across rounds without reading the
# name every time. Assigned by first appearance, not hardcoded, because the roster is
# user-configurable in size and composition -- a fixed table would mislabel a custom bench.
_SEAT_COLOURS = ("\033[36m", "\033[35m", "\033[34m", "\033[32m", "\033[33m", "\033[36;1m",
                 "\033[35;1m", "\033[34;1m", "\033[32;1m", "\033[33;1m", "\033[37m",
                 "\033[37;1m")


class Renderer:
    def __init__(self, colour: bool) -> None:
        self.colour = colour
        self.seat_colour: dict[str, str] = {}
        self.started: dict[str, float] = {}

    def _c(self, text: str, code: str) -> str:
        return f"{code}{text}{_OFF}" if self.colour and code else text

    def _seat(self, name: str) -> str:
        if name not in self.seat_colour:
            self.seat_colour[name] = _SEAT_COLOURS[len(self.seat_colour) % len(_SEAT_COLOURS)]
        return self._c(f"{name:<9}", self.seat_colour[name])

    def line(self, rec: dict) -> str | None:
        ev = rec.get("ev")
        if ev == "run_started":
            v, i = rec.get("voting") or [], rec.get("inspectors") or []
            fast = "  [FAST MODE -- reduced depth]" if rec.get("fast_mode") else ""
            return (self._c(f"council: {len(v)} voting, {len(i)} inspecting", _BOLD)
                    + self._c(f"  ({', '.join(v)}{' | ' + ', '.join(i) if i else ''}){fast}", _DIM))
        if ev == "round_started":
            return self._c(f"-- round {rec.get('round')} --", _DIM)
        if ev == "member_started":
            return f"  {self._seat(str(rec.get('member')))} {self._c('...', _DIM)}"
        if ev == "member_finished":
            verdict = str(rec.get("verdict") or "?")
            dur = rec.get("duration_s")
            cost = rec.get("cost")
            extra = f" {dur}s" if dur is not None else ""
            # MEASURED, not inferred: in a sampled fire the five OpenRouter seats carry a
            # `cost` key and codex carries none
            #   python3 -c "import json,glob; d=json.load(open(sorted(glob.glob('logs/2026-07-30/*.json'))[-1])); print({m['role']: ('cost' in m) for m in d['members']})"
            # A missing cost is UNKNOWN, not zero, so it renders as a dash. Why it is
            # missing (transport, billing arrangement) is not something this renderer
            # knows or should assert.
            extra += f" ${cost:.4f}" if isinstance(cost, (int, float)) else " $-"
            return (f"  {self._seat(str(rec.get('member')))} "
                    f"{self._c(verdict, _C.get(verdict, ''))}{self._c(extra, _DIM)}")
        if ev == "member_corrected":
            return (f"  {self._seat(str(rec.get('member')))} "
                    f"{self._c(str(rec.get('was')), _DIM)} -> "
                    f"{self._c(str(rec.get('verdict')), _C.get(str(rec.get('verdict')), ''))}"
                    f"{self._c('  (' + str(rec.get('why') or '') + ')', _DIM)}")
        if ev == "tool_request":
            mark = "granted" if rec.get("granted") else "DENIED"
            return (f"  {self._seat(str(rec.get('member')))} "
                    f"{self._c(str(rec.get('kind')) + ': ' + mark, _DIM)}")
        if ev == "dropped":
            return self._c(f"  [{rec.get('n')} progress record(s) dropped -- "
                           f"renderer fell behind; the council was unaffected]", _DIM)
        if ev == "final_verdict":
            verdict = str(rec.get("verdict") or "?")
            lost = rec.get("events_dropped") or 0
            tail = f"   ({lost} event(s) dropped)" if lost else ""
            return (self._c("VERDICT: ", _BOLD) + self._c(verdict, _C.get(verdict, ""))
                    + self._c(f"   log: {rec.get('log_path')}{tail}", _DIM))
        if ev == "note":
            return self._c(f"  {rec.get('text')}", _DIM)
        return None


# How long a finished fire's last frame stays on screen before it clears.
LINGER_S = 20.0
# Shorter, because a doorman turn is itself short: long enough to catch a DENY that ended the
# edit, not long enough to clutter the table with turns that simply passed.
DOORMAN_LINGER_S = 10.0

# Verdict -> one character, so a whole seat fits in a fixed-width cell. The WORD is kept in
# the tally on the phase line above, so a reader never has to decode a colour or a letter to
# learn what the council concluded -- this compression is for the grid only.
_V1 = {"PASS": "P", "WARN": "W", "BLOCK": "B", "UNPARSEABLE": "U", "ERROR": "E",
       "DELIBERATING": "~"}


def _label_for(marker: Path, fallback_session: str) -> str:
    """A name for this fire that means something to a human.

    THE SESSION ID IDENTIFIES NOTHING TO A READER: it is a 36-character hash, and with several
    sessions running the whole point of a label is telling them apart. The marker records the
    session's working directory, so its basename is used -- that is what an operator calls the
    thing they are working on. A marker written before that field existed has no cwd, so the
    short session id is the fallback: less useful, but never wrong.
    """
    try:
        cwd = json.loads(marker.read_text()).get("cwd") or ""
    except (OSError, ValueError):
        cwd = ""
    return os.path.basename(cwd.rstrip("/")) or (fallback_session[:8] or "?")


def _elapsed_s(marker: Path) -> int:
    """Seconds since this fire started, from the marker's own `started` field.

    mtime is the fallback and means the same thing: the marker is written once at the start and
    the heartbeat beats into a SIDECAR precisely so nothing refreshes it.
    """
    try:
        started = json.loads(marker.read_text()).get("started")
        if isinstance(started, str):
            began = datetime.fromisoformat(started)
            if began.tzinfo is None:
                began = began.replace(tzinfo=timezone.utc)
            return max(0, int((datetime.now(timezone.utc) - began).total_seconds()))
    except (OSError, ValueError, TypeError):
        pass
    try:
        return max(0, int(datetime.now(timezone.utc).timestamp() - marker.stat().st_mtime))
    except OSError:
        return 0


def _seat_cell(render: Renderer, name: str, seat: dict | None) -> str:
    """One fixed-width cell: who, what they said, and how long they took.

    A SEAT THAT HAS NOT REPORTED SHOWS A DOT AND NO TIME, never a zero -- a zero would read as
    "answered instantly" when it means "has not answered". The name is truncated to four
    characters, which is enough to keep every seat on both tiers distinct.
    """
    if not seat or not seat.get("verdict"):
        return f"{name[:4]:<4} {render._c('.', _DIM)} {render._c('    -', _DIM)}"
    verdict = str(seat.get("verdict"))
    ch = _V1.get(verdict, "?")
    dur = seat.get("duration_s")
    dur_s = f"{dur:>5.1f}" if isinstance(dur, (int, float)) else "    -"
    return (f"{name[:4]:<4} {render._c(ch, _C.get(verdict, ''))} "
            f"{render._c(dur_s, _DIM)}")


def _seat_lines(render: Renderer, summary: dict) -> list[str]:
    """The phase-count line with its tally, then ONE GRID ROW PER ROUND.

    PER ROUND, NOT PER TIER, and that is the point. A voting seat reports twice -- round 1
    before it has seen its peers, round 2 after -- and those two verdicts are different data:
    the whole anchoring question is whether a seat MOVED. A single row keyed by seat name can
    only hold one of them, so round 2 overwrote round 1 in place and the r1 verdicts were
    destroyed as they were replaced. Both rounds now get their own row.

    ROUND -> ROW, from summary["seat_rounds"]: 1 and 2 are the voting rounds, 3 the inspector
    pass, 4 the second inspector pass (which only exists when a member requested it, so its row
    appears only when populated).

    ROUND 0 IS A ROW TOO, whenever it has anything in it. It is not a phase: summarise_partial's
    `_int` maps an absent or unparseable `round` to 0, so 0 collects the malformed records. Since
    these rows are keyed by round rather than by tier, a seat landing there would otherwise be
    rendered nowhere at all -- present in the tally, absent from every grid, which reads as a
    seat that never reported. Verified reachable: a member_finished record with no `round` key
    lands under `seat_rounds[0]`.

    NO FALLBACK PATH, deliberately, and this was measured rather than assumed. Every
    member_finished populates `seats` and `seat_rounds` together, and a bad round becomes 0
    instead of being dropped, so "seats reported but no per-round detail" cannot occur. A branch
    for it would be unreachable code justifying itself with an unobserved scenario.

    SPLIT OUT FROM THE HEADER because the two have different lifetimes: the header is built from
    the MARKER and these rows from the EVENTS SIDECAR, so the rows can be refreshed in a window
    where the header can no longer be rebuilt.
    """
    exp, fin, seats = summary["expected"], summary["finished"], summary["seats"]
    rounds = summary.get("seat_rounds") or {}
    n_v, n_i = len(exp["voting"]), len(exp["inspector"])
    phases = "  ".join([f"r1 {len(fin.get(1, set()))}/{n_v}",
                        f"r2 {len(fin.get(2, set()))}/{n_v}",
                        f"insp {len(fin.get(3, set()))}/{n_i}"]
                       + ([f"p2 {len(fin.get(4, set()))}"] if fin.get(4) else []))
    # THE TALLY STAYS ON THE COLLAPSED MAP: it answers "where does the council stand", which is
    # the aggregated position (round 2 for a voter that has finished both), not a sum over rows.
    # Summing every round would double-count each voting seat and report 12 verdicts from 6.
    tally = collections.Counter(str(s.get("verdict")) for s in seats.values()
                                if s.get("verdict"))
    tally_s = " ".join(f"{n}{_V1.get(v, '?')}" for v, n in sorted(tally.items()))
    lines = [f"  {phases}" + (f"      {tally_s}" if tally_s else "")]

    for label, rnd, names in (("vote r1", 1, exp["voting"]),
                              ("vote r2", 2, exp["voting"]),
                              ("insp   ", 3, exp["inspector"]),
                              ("insp p2", 4, exp["inspector"])):
        if not names:
            continue
        # Round 4 is CONDITIONAL -- it runs only when a member asks for it. An always-present
        # row of dots would report a phase as outstanding that this fire will never run.
        if rnd == 4 and not rounds.get(4):
            continue
        got = rounds.get(rnd) or {}
        cells = "  ".join(_seat_cell(render, n, got.get(n)) for n in names)
        lines.append(f"  {render._c(label, _DIM)}  {cells}")

    # ROUND 0: the malformed-record bucket, shown only when non-empty. Listed by the names
    # actually present rather than against an expected roster, because 0 has no roster -- it is
    # whatever `_int` could not parse. Without this row such a seat appears in the tally and in
    # no grid, which reads as a seat that never reported.
    stray = rounds.get(0) or {}
    if stray:
        cells = "  ".join(_seat_cell(render, n, stray[n]) for n in sorted(stray))
        lines.append(f"  {render._c('rnd ?  ', _DIM)}  {cells}")
    return lines


def _fire_block(render: Renderer, marker: Path) -> list[str]:
    """The lines for one running fire: header, phase counts and tally, then the seat grids."""
    summary = ca.summarise_partial(ca.read_events(ca._events_path(marker)))
    try:
        rec = json.loads(marker.read_text())
    except (OSError, ValueError):
        rec = {}
    label = _label_for(marker, str(rec.get("session_id") or ""))
    target = os.path.basename(str(rec.get("target_path") or "?"))
    elapsed = _elapsed_s(marker)
    cap = getattr(ca, "FIRE_TIMEOUT_S", 0)

    head = (f"{render._c(label, _BOLD)}  {rec.get('tool_name') or '?'} {target}"
            f"   {elapsed}s")
    if cap and elapsed >= 0.8 * cap:
        head += render._c(f" / {cap}s CAP", _C.get("BLOCK", ""))

    exp = summary["expected"]
    if not exp["voting"] and not exp["inspector"]:
        # No roster yet. Expected briefly at the start of every fire, since the marker is
        # written before the engine is spawned; sustained, it means events never arrived.
        state = "starting" if elapsed < 60 else "running, no roster reported"
        return [head, f"  {render._c(state, _DIM)}"]
    return [head] + _seat_lines(render, summary)


def _find_log_for(rec: dict) -> Path | None:
    """The completed log this marker's fire wrote, or None.

    THE SIDECAR IS NOT AVAILABLE WHEN THIS IS WANTED, which is the whole reason for reading a
    log. clear_pending_marker unlinks the marker, the beats and the events in one loop; that loop
    measured a median 5.5 us and a max 11.2 us over 200 trials. The poll that would have to catch
    that window is 1.0 s by default (--interval, floored at 0.2 s), i.e. at least four orders of
    magnitude longer even at the tightest setting allowed. So the sidecar outlives the marker by
    microseconds, a poll landing inside that window is a coincidence rather than something to
    design around, and a re-read attempted after the marker disappears finds a deleted file. The
    log is durable and is written before the cleanup runs.

    NEWER-THAN-THE-MARKER IS REQUIRED, not merely nice. A killed fire writes NO log, and the
    same session editing the same file earlier would otherwise match and be presented as this
    fire's final result -- stale data wearing a completed fire's label, which is worse than the
    dash it replaced. Both stamps are ISO-8601 UTC, so the comparison is direct.

    TWO MATCHERS, AND THE EXACT ONE WINS. `tool_use_id` is unique per edit call, so when both the
    marker and the log carry it the pairing is an identification rather than a guess, and no
    timestamp reasoning is needed at all.

    THE FALLBACK IS session_id + tool_name + target_path, EARLIEST log at or after the marker's
    stamp. It is a heuristic: those three fields are identical across every fire a session aimed
    at one file, so several logs match and the earliest is merely the most likely. Two fires
    overlapping on the same session, tool and target cannot be told apart, and a finished fire's
    final frame can then show its SIBLING's verdicts. This path exists for logs written BEFORE
    tool_use_id was recorded, and for direct CLI runs which have no originating tool call.

    Both matchers are bounded to two date directories (the started date and the day after, since a
    fire can cross midnight), and this runs once per finished fire rather than once per poll.
    """
    started = str(rec.get("started") or "")
    if not started:
        return None
    try:
        t0 = datetime.fromisoformat(started)
    except ValueError:
        return None
    root = getattr(ca, "COUNCIL_ROOT", None)
    if root is None:
        return None
    stamp = t0.strftime("%Y%m%dT%H%M%SZ")
    days = {t0.strftime("%Y-%m-%d"),
            (t0 + timedelta(days=1)).strftime("%Y-%m-%d")}
    want_id = str(rec.get("tool_use_id") or "")
    best: tuple[str, Path] | None = None
    for day in sorted(days):
        for p in sorted((Path(root) / "logs" / day).glob("*.json")):
            if p.name.split("-")[0] < stamp:
                continue
            try:
                d = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            # EXACT MATCH FIRST, and it RETURNS rather than competing for `best`: a tool_use_id
            # identifies one edit call, so there is nothing to rank and no timestamp to weigh.
            log_id = str(d.get("tool_use_id") or "")
            if want_id and log_id:
                if log_id == want_id:
                    return p
                # Both sides carry an id and they differ, so this log belongs to a DIFFERENT
                # edit. Skipping it outright is the point of recording the id -- letting it fall
                # through to the heuristic would let a sibling win on timestamp alone.
                continue
            if (str(d.get("session_id") or "") != str(rec.get("session_id") or "")
                    or str(d.get("tool_name") or "") != str(rec.get("tool_name") or "")
                    or str(d.get("target_path") or "") != str(rec.get("target_path") or "")):
                continue
            try:
                if datetime.fromisoformat(str(d.get("timestamp"))) < t0:
                    continue
            except ValueError:
                continue
            if best is None or p.name < best[0]:
                best = (p.name, p)
    return best[1] if best else None


def _summary_from_log(path: Path) -> dict | None:
    """A finished fire's log reshaped into what _seat_lines consumes, or None.

    SAME SHAPE AS summarise_partial so one renderer serves a live fire and a finished one.
    The mapping, from the log's own fields: `round1` -> round 1, `members` -> round 2 (members
    is the FINAL per-seat state, which for a two-round voter is its round-2 record), `shadow`
    -> round 3.

    WHAT THIS CANNOT SHOW, and it is a real limit rather than a caveat for form's sake: the log
    stores `shadow` as ONE FLAT LIST with no pass number, so a second inspector pass cannot be
    separated from the first and every inspector lands on round 3.
    EVERY FIELD IS READ DEFENSIVELY, and no tally is quoted here: the corpus gains a log per
    COMPLETED fire (a fire killed at the cap writes none), so any count would be stale almost
    immediately. To check the shape yourself, glob `logs/*/*.json` and count the logs whose
    `round1`, and whose `shadow`, is non-empty. `shadow` is the one that comes back absent on a
    minority of logs; the reason is NOT established, so do not infer a roster setting from it.

    EXPECTED COMES FROM THE ROSTER, not from who answered. A seat that never reported must still
    occupy a cell -- deriving the roster from the results would silently shrink the grid to the
    seats that happened to answer, which is exactly the information a reader needs to not lose.

    `quorum_state` IS PASSED THROUGH, NEVER RECOMPUTED HERE, and that is deliberate. It records
    whether the fire could have reached a BLOCK at all, and the threshold behind it is the
    auto-revert rule, which consult_council documents as the user's call rather than the agent's.
    Deriving it here would put ceil(n/2) in a second place, and a copy that drifts would misreport
    enforcement rather than merely look untidy. This module imports council_advisor, not
    consult_council, so it has no access to block_quorum() and should not grow one. The field is
    absent from any log written before it existed, so it passes through as None, and a reader must
    treat None as UNKNOWN -- never as "reachable".
    """
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    voting, inspector = [], []
    for m in ((d.get("roster") or {}).get("members") or []):
        if not isinstance(m, dict):
            continue
        (voting if m.get("tier") == "voting" else inspector).append(str(m.get("name")))
    rows = {1: d.get("round1"), 2: d.get("members"), 3: d.get("shadow")}
    seat_rounds: dict[int, dict[str, dict]] = {}
    finished: dict[int, set[str]] = {}
    for rnd, items in rows.items():
        if not isinstance(items, list):
            continue
        for m in items:
            if not isinstance(m, dict) or not m.get("role"):
                continue
            role = str(m["role"])
            seat_rounds.setdefault(rnd, {})[role] = {
                "round": rnd, "verdict": m.get("verdict"),
                "duration_s": m.get("duration_s"), "text": m.get("text"),
            }
            finished.setdefault(rnd, set()).add(role)
    if not voting and not inspector:
        voting = sorted(seat_rounds.get(2, {}))
        inspector = sorted(seat_rounds.get(3, {}))
    # The aggregate the tally reads: each voter's FINAL record plus each inspector's, which is
    # rounds 2 and 3. Including round 1 here would count every voter twice.
    seats = {**seat_rounds.get(2, {}), **seat_rounds.get(3, {})}
    return {"expected": {"voting": voting, "inspector": inspector},
            "started": {}, "finished": finished, "seats": seats,
            "seat_rounds": seat_rounds, "corrected": [],
            "quorum_state": d.get("quorum_state"),
            "final_verdict": d.get("final_verdict"), "log_name": path.name}


def _doorman_block(render: Renderer, marker: Path) -> list[str]:
    """The doorman's turn, which precedes any review marker for that edit."""
    try:
        rec = json.loads(marker.read_text())
    except (OSError, ValueError):
        rec = {}
    label = _label_for(marker, "")
    target = os.path.basename(str(rec.get("target_path") or "?"))
    return [f"{render._c(label, _BOLD)}  {rec.get('tool_name') or '?'} {target}"
            f"   {_elapsed_s(marker)}s",
            f"  {render._c('doorman reviewing (gate, before the council)', _DIM)}"]


def _quorum_rows(render: Renderer, summary: dict) -> list[str]:
    """The enforcement line for a finished fire: could it have reached a BLOCK at all?

    THREE STATES, AND SILENCE MEANS TWO DIFFERENT THINGS, so neither may be rendered as an
    assurance. `block_reachable` False is the only one that prints.
      - FALSE: too few readable verdicts came back for the threshold, so no combination of
        them could have reverted the file. This is the row worth interrupting a reader for.
      - TRUE: prints nothing. The threshold was met, and a line reading "enforcement was
        possible" on every ordinary fire trains the eye to skip the row that matters. TRUE
        does NOT mean the panel was whole -- the threshold is ceil(n/2), half the configured
        bench rounded up rather than all of it, so seats can be missing while the quorum is
        still met. Measured on this install: 6 configured voting seats, block_quorum() 3, and
        3 is exactly half rather than a majority -- do not read "majority" into it, since that
        would put the bar at 4. Missing seats appear as empty cells in the grid above, never
        on this line.
      - NONE: the log predates the field, or the fire was salvaged from its sidecar and never
        wrote one. That is UNKNOWN, not healthy. It currently prints nothing, which means
        UNKNOWN and TRUE look IDENTICAL on this surface -- a known limitation of this row, not
        a claim that the fire was fine. Anything that later renders UNKNOWN must distinguish
        it from TRUE, never from FALSE.
    The `is not False` test is deliberate: `not qs.get(...)` would collapse None into the same
    branch as False and report an unmeasured fire as unenforceable.
    """
    qs = summary.get("quorum_state")
    if not isinstance(qs, dict) or qs.get("block_reachable") is not False:
        return []
    reported = len(qs.get("reported") or [])
    configured = len(qs.get("configured") or [])
    rows = [f"  {render._c('QUORUM UNREACHABLE', _C.get('BLOCK', ''))}  "
            f"{reported} readable verdict(s) of {configured} configured seat(s); "
            f"{qs.get('quorum')} BLOCK(s) needed to auto-revert"]
    absent = [str(a) for a in (qs.get("absent") or [])]
    if absent:
        rows.append(f"  {render._c('never ran', _DIM)}         {', '.join(absent)}")
    return rows


def follow(render: Renderer, session: str, interval: float) -> int:
    """Watch fires this wrapper did NOT start, by tailing their events sidecars.

    WHY THIS MODE EXISTS. The default mode spawns the engine and reads a pipe it owns, which
    covers a fire launched by hand. It cannot show the fires that matter most -- the ones the
    PostToolUse hook starts for every edit, launched by the hook and watched by nobody. Those
    stream into a sidecar beside their pending-review marker (council_advisor opens it before
    spawning the engine and removes it with the marker afterwards), so they can be tailed.

    WHY NOT THE STATUSLINE. A status line is drawn by the harness on ITS schedule, and a
    stamped line was observed sitting unchanged for minutes across an edit completing -- so
    whatever the cause, that surface cannot be relied on to advance while a fire runs. This
    loop owns its own clock and re-reads every `interval`, which is the property a live view
    needs and the reason the same information is worth rendering in two places.

    READ-ONLY, AND IT NEVER TOUCHES THE FIRE. The sidecar belongs to the advisor; this loop
    only reads, notices its disappearance, and waits for the next one.
    """
    if ca is None:
        print("council_watch: --follow needs council_advisor.py beside this script "
              "(it owns the marker layout); it could not be imported.", file=sys.stderr)
        return 2
    tty = sys.stderr.isatty()
    # FINISHED FIRES LINGER, keyed by marker path -> (deadline, last rendered block). Without
    # this, a fire vanishes the instant its marker does, so the last thing on screen is a
    # half-finished round and there is no way to tell "it completed" from "I looked away".
    # Entries are MUTABLE lists, not tuples, because the finalise step below rewrites the block
    # and sets the done flag in place. The assignment site builds
    # [deadline, block, header, events_path, finalised, marker_rec] -- six elements, matching the
    # unpack. marker_rec is carried because finalising reads the fire's LOG, and locating that
    # log needs the marker's session_id/tool_name/target_path/started -- by which point the
    # marker itself has been deleted. All three sites move together or not at all.
    lingering: dict[str, list] = {}
    # Doorman turns linger separately and more briefly. They carry no sidecar to re-read, so
    # the entry is just [deadline, block].
    door_lingering: dict[str, list] = {}
    try:
        while True:
            now = time.monotonic()
            if session:
                live = [p for p in _session_markers(session) if ca.marker_is_live(p)]
                door = [p for p in _session_markers(session, "*.doorman")
                        if ca.marker_is_live(p)]
            else:
                live = [p for p in _host_markers("*.json") if ca.marker_is_live(p)]
                door = [p for p in _host_markers("*.doorman") if ca.marker_is_live(p)]

            blocks: list[list[str]] = []
            seen_keys = set()
            for m in sorted(live, key=str):
                seen_keys.add(str(m))
                block = _fire_block(render, m)
                try:
                    mrec = json.loads(m.read_text())
                except (OSError, ValueError):
                    mrec = {}
                # header, sidecar path and marker record are kept so the block can be FINALISED
                # below, after the marker and its sidecar are gone.
                lingering[str(m)] = [now + LINGER_S, block, block[0],
                                     ca._events_path(m), False, mrec]
                blocks.append(block)
            for m in sorted(door, key=str):
                seen_keys.add(str(m))
                block = _doorman_block(render, m)
                door_lingering[str(m)] = [now + DOORMAN_LINGER_S, block]
                blocks.append(block)

            # A FIRE THAT HAS GONE IS FINALISED FROM ITS LOG, not from its sidecar. The sidecar is
            # unlinked in the same loop as the marker (that loop measured a median 5.5 us) while
            # this polls at 1.0 s, so by the time a fire is noticed gone its sidecar is gone too
            # and a re-read returns nothing -- which is why the last seat to report used to stay
            # "-" in the frozen frame while the clearing countdown ran. The log is durable, is
            # written before cleanup, and carries every seat plus the final verdict.
            # THE SIDECAR IS STILL THE FALLBACK: a fire KILLED at the cap writes no log at all, and
            # there the sidecar is the only record of the rounds that did finish.
            # The header is reused rather than rebuilt, since it came from the now-deleted marker.
            # Done once and flagged, because an unflagged retry would re-read the log on every
            # poll of the linger window -- LINGER_S / interval = 20.0 / 1.0 = 20 reads per fire at
            # the defaults.
            for key, entry in list(lingering.items()):
                deadline, block, header, events, finalised, mrec = entry
                if key in seen_keys:
                    continue
                if now >= deadline:
                    del lingering[key]
                    continue
                if not finalised:
                    fresh = None
                    logp = _find_log_for(mrec)
                    if logp is not None:
                        fresh = _summary_from_log(logp)
                    if fresh is None:
                        cand = ca.summarise_partial(ca.read_events(events))
                        fresh = cand if cand["seats"] else None
                    if fresh is not None:
                        rows = _seat_lines(render, fresh)
                        verdict = str(fresh.get("final_verdict") or "")
                        if verdict:
                            rows.append(f"  final: "
                                        f"{render._c(verdict, _C.get(verdict, ''))}")
                        # AFTER the verdict, deliberately: the reachability line qualifies the
                        # verdict above it, and a reader who stops at "final: WARN" has read the
                        # less important half.
                        # THE SIDECAR FALLBACK TAKEN JUST ABOVE CANNOT SUPPLY IT. Measured:
                        # summarise_partial([]) returns exactly corrected, expected, finished,
                        # seat_rounds, seats, started -- no quorum_state (and no final_verdict,
                        # which is why the verdict line above is also skipped for a salvaged
                        # fire). So a killed fire draws NO line here, which on this surface is
                        # indistinguishable from a reachable one.
                        # THAT GAP IS NARROWER THAN IT LOOKS, and the reason is in the CONTROL
                        # PATH rather than in any report's wording: council_advisor's timeout
                        # handler builds the partial and then `return emit_warning(salvaged)`,
                        # returning down the WARNING route, while the revert lives further on in
                        # the `rc == 2` branch that a killed wrapper never reaches. So a salvaged
                        # fire cannot revert, and reachability is MOOT there rather than unknown.
                        # format_partial says as much in its own text, but that text is a report,
                        # not the mechanism -- the two agree here, and it is the branch that
                        # settles it.
                        rows += _quorum_rows(render, fresh)
                        block = [header] + rows
                        entry[1] = block
                    entry[4] = True
                blocks.append(block + [render._c(
                    f"  finished; clearing in {int(deadline - now)}s", _DIM)])

            # THE DOORMAN'S TURN LINGERS TOO, and briefly: a DENY ends the turn by rejecting the
            # edit, so without this the row vanishes the instant the decision lands and the one
            # phase a reader most wants to catch is the one they cannot see. Shorter than a
            # fire's linger because a doorman turn is itself short.
            for key, entry in list(door_lingering.items()):
                deadline, block = entry
                if key in seen_keys:
                    continue
                if now >= deadline:
                    del door_lingering[key]
                    continue
                blocks.append(block + [render._c(
                    f"  doorman turn over; clearing in {int(deadline - now)}s", _DIM)])

            out = [render._c("COUNCIL", _BOLD)
                   + f"  {len(live)} live, {len(door)} at the doorman"
                   + f"   {datetime.now().strftime('%H:%M:%S')}"
                   + render._c(f"   (poll {interval:g}s, Ctrl-C to exit)", _DIM), ""]
            if not blocks:
                out.append(render._c("  no council fires running", _DIM))
            for b in blocks:
                out.extend(b)
                out.append("")

            if tty:
                # Cursor home, then erase forward, then paint. Chosen over a full-screen
                # clear so the previous frame is overwritten rather than removed first.
                sys.stderr.write("\033[H\033[J" + "\n".join(out) + "\n")
            else:
                # NOT A TTY: cursor control would land as literal escape bytes in a pipe or a
                # log file, so frames are appended instead. Readable, just not in place.
                sys.stderr.write("\n".join(out) + "\n")
            sys.stderr.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        if tty:
            sys.stderr.write("\n")
        return 0


def _session_markers(session: str, pattern: str = "*.json") -> list[Path]:
    """Markers of one kind for one session. Empty on any filesystem trouble -- a watcher
    that raised on a missing directory would die the moment it out-lived a session."""
    try:
        d = ca._pending_dir(session)
        return list(d.glob(pattern)) if d.is_dir() else []
    except OSError:
        return []


def _host_markers(pattern: str) -> list[Path]:
    """The same, across every session on the host, for `--session ''`."""
    try:
        return list(ca.EVIDENCE_STATE_ROOT.glob(f"*/{ca.PENDING_DIRNAME}/{pattern}"))
    except OSError:
        return []


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="All other arguments are forwarded to consult_council.py unchanged.")
    ap.add_argument("--no-colour", action="store_true",
                    help="Never colourise, even on a TTY.")
    ap.add_argument("--follow", action="store_true",
                    help="Do NOT start a fire. Tail the hook-driven fires already running "
                         "for this session and render them live. This is the mode for "
                         "watching the council review your edits as they happen.")
    ap.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
                    help="Which session's fires to follow in --follow mode. Defaults to "
                         "$CLAUDE_CODE_SESSION_ID; pass an empty string for every session "
                         "on this host.")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="Seconds between polls in --follow mode (default 1.0).")
    args, forwarded = ap.parse_known_args()

    if args.follow:
        # --follow OWNS NO ENGINE, so the forwarding contract does not apply. Leftover engine
        # arguments would be silently ignored, and silently ignoring an argument someone
        # typed is how a watcher ends up watching something other than what was asked for.
        if forwarded:
            print(f"council_watch: --follow starts no fire, so it cannot forward "
                  f"{forwarded} to the engine; remove them.", file=sys.stderr)
            return 2
        return follow(Renderer((not args.no_colour) and sys.stderr.isatty()),
                      args.session, max(0.2, args.interval))

    if any(a == "--events-fd" or a.startswith("--events-fd=") for a in forwarded):
        print("council_watch: --events-fd is owned by this wrapper; remove it.",
              file=sys.stderr)
        return 2
    if not ENGINE.is_file():
        print(f"council_watch: engine not found at {ENGINE}", file=sys.stderr)
        return 2

    colour = (not args.no_colour) and sys.stderr.isatty()
    render = Renderer(colour)

    # A DIRECT PITCH IS A FIRE TOO, and --follow can only see it if it leaves the same two
    # artifacts a hook-driven fire does: a pending-review MARKER to be enumerated, and an
    # events SIDECAR to be rendered. Before this, only the PostToolUse hook wrote them, so a
    # pitch streamed to its own terminal and was invisible everywhere else.
    # THE MARKER NAME IS NOT tool_use_id-SHAPED HERE, and nothing should assume it is: a direct
    # run has no originating tool call, so write_pending_marker falls back to a uuid. Any reader
    # keying off the filename is reading an implementation detail of the hook path.
    # THE LAYOUT RULES STAY IN council_advisor, which owns them: the marker path, the liveness
    # test and the sidecar name are all its functions, called here rather than reimplemented,
    # so there is no second definition free to drift from the writers.
    marker = None
    if ca is not None:
        target = ""
        for i, a in enumerate(forwarded):
            if a == "--target-path" and i + 1 < len(forwarded):
                target = forwarded[i + 1]
            elif a.startswith("--target-path="):
                target = a.split("=", 1)[1]
        # tool_name "pitch" so the row is distinguishable from a hook-driven Edit/Write at a
        # glance; an empty tool_use_id makes write_pending_marker name the file from a uuid,
        # which is correct here -- a direct run has no originating tool call to key it from.
        marker = ca.write_pending_marker(args.session, "pitch", target or "(direct pitch)",
                                         "", os.getcwd())
    events = ca._events_path(marker) if (ca is not None and marker is not None) else None

    r, w = os.pipe()
    try:
        proc = subprocess.Popen(
            [sys.executable, str(ENGINE), *forwarded, "--events-fd", str(w)],
            pass_fds=(w,))
    except OSError as e:
        os.close(r); os.close(w)
        if ca is not None:
            ca.clear_pending_marker(marker)
        print(f"council_watch: could not start the engine: {e}", file=sys.stderr)
        return 2
    # The PARENT must close its copy of the write end, or the read below never sees EOF
    # when the engine exits and this wrapper hangs forever.
    os.close(w)

    # THE SIDECAR IS WHAT ANOTHER TERMINAL READS. This process owns the only pipe the engine
    # writes to, so without teeing, --follow would find the marker and have nothing to render.
    # Instrumentation must never cost the review: every failure here degrades to sink = None.
    sink = None
    if events is not None:
        try:
            sink = open(events, "a", buffering=1)
        except OSError:
            sink = None

    buf = ""
    try:
        with os.fdopen(r, "r", buffering=1) as stream:
            for chunk in stream:
                buf += chunk
                *complete, buf = buf.split("\n")
                for raw in complete:
                    if not raw.strip():
                        continue
                    # TEE BEFORE PARSING, so a record this renderer cannot read still reaches
                    # the sidecar -- summarise_partial parses defensively and a dropped line
                    # would be a seat missing from someone else's view, not a cosmetic loss.
                    if sink is not None:
                        try:
                            sink.write(raw + "\n")
                        except OSError:
                            # CLOSE BEFORE DROPPING THE REFERENCE. Setting it to None first
                            # makes the finally below skip its close() and leaks the handle --
                            # the disabling of a broken sink must not also strand it.
                            try:
                                sink.close()
                            except OSError:
                                pass
                            sink = None
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue        # a malformed record must not kill the view
                    out = render.line(rec)
                    if out is not None:
                        print(out, file=sys.stderr, flush=True)
        return proc.wait()
    finally:
        # THE CHILD GOES FIRST, and the ORDER is load-bearing: clearing the marker while the
        # engine is still alive advertises a FINISHED fire that is still burning tokens, which
        # inverts what a marker means to every reader of it.
        # An interrupt can land anywhere above -- inside the stream loop as well as in
        # proc.wait() -- so this reaps unconditionally on `poll() is None` rather than assuming
        # where it struck. SIGTERM first, SIGKILL only after a bounded wait; whether the engine
        # runs cleanup of its own on SIGTERM is NOT established here, only that it is given the
        # chance to. The wait() after kill() is what actually reaps: without it the marker
        # clears over a zombie, which is the same lie in a shorter-lived form.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if sink is not None:
            sink.close()
        if ca is not None:
            ca.clear_pending_marker(marker)


if __name__ == "__main__":
    sys.exit(main())
