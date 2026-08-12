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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# THE MARKER LAYOUT AND THE LIVENESS RULE BOTH BELONG TO council_advisor, which writes them.
# --follow needs three of its names -- _pending_dir (656), _events_path (817) and
# marker_is_live (833) -- and importing them is what keeps this file from growing a second,
# drifting definition of "a fire is running".
# TOLERANT IMPORT: the default mode spawns its own engine and needs none of this, so a
# package without the advisor must still be able to run that mode rather than failing at
# import time over a feature it is not using.
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
    """The phase-count line with its tally, then one grid row per tier.

    SPLIT OUT FROM THE HEADER because the two have different lifetimes: the header is built
    from the MARKER, and these rows are built from the EVENTS SIDECAR. Cleanup removes the
    marker first and the sidecar last, so there is a window in which the rows can still be
    refreshed and the header cannot -- and refreshing them is worth it, because the seat that
    reports last is otherwise the one most likely to be missing from a fire's final frame.
    """
    exp, fin, seats = summary["expected"], summary["finished"], summary["seats"]
    n_v, n_i = len(exp["voting"]), len(exp["inspector"])
    phases = "  ".join([f"r1 {len(fin.get(1, set()))}/{n_v}",
                        f"r2 {len(fin.get(2, set()))}/{n_v}",
                        f"insp {len(fin.get(3, set()))}/{n_i}"]
                       + ([f"p2 {len(fin.get(4, set()))}"] if fin.get(4) else []))
    tally = collections.Counter(str(s.get("verdict")) for s in seats.values()
                                if s.get("verdict"))
    tally_s = " ".join(f"{n}{_V1.get(v, '?')}" for v, n in sorted(tally.items()))
    lines = [f"  {phases}" + (f"      {tally_s}" if tally_s else "")]
    for tier_label, names in (("vote", exp["voting"]), ("insp", exp["inspector"])):
        if not names:
            continue
        cells = "  ".join(_seat_cell(render, n, seats.get(n)) for n in names)
        lines.append(f"  {render._c(tier_label, _DIM)}  {cells}")
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
    # [deadline, block, header, events_path, finalised] -- five elements, matching the unpack.
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
                # header and sidecar path are kept so the block can be FINALISED below.
                lingering[str(m)] = [now + LINGER_S, block, block[0],
                                     ca._events_path(m), False]
                blocks.append(block)
            for m in sorted(door, key=str):
                seen_keys.add(str(m))
                block = _doorman_block(render, m)
                door_lingering[str(m)] = [now + DOORMAN_LINGER_S, block]
                blocks.append(block)

            # A FIRE THAT HAS GONE gets ONE last read before it is frozen. Cleanup unlinks the
            # marker BEFORE the events sidecar, so at the moment this loop notices a fire has
            # ended its final records are usually still on disk -- and the seat that reports
            # last is exactly the one a poll-interval-stale frame is missing. The header cannot
            # be rebuilt (it came from the marker, which is gone), so it is reused and only the
            # rows are refreshed. Done once, flagged, and never retried: after the sidecar goes
            # the read returns nothing and would blank rows that were correct.
            for key, entry in list(lingering.items()):
                deadline, block, header, events, finalised = entry
                if key in seen_keys:
                    continue
                if now >= deadline:
                    del lingering[key]
                    continue
                if not finalised:
                    fresh = ca.summarise_partial(ca.read_events(events))
                    if fresh["seats"]:
                        block = [header] + _seat_lines(render, fresh)
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

    r, w = os.pipe()
    try:
        proc = subprocess.Popen(
            [sys.executable, str(ENGINE), *forwarded, "--events-fd", str(w)],
            pass_fds=(w,))
    except OSError as e:
        os.close(r); os.close(w)
        print(f"council_watch: could not start the engine: {e}", file=sys.stderr)
        return 2
    # The PARENT must close its copy of the write end, or the read below never sees EOF
    # when the engine exits and this wrapper hangs forever.
    os.close(w)

    buf = ""
    with os.fdopen(r, "r", buffering=1) as stream:
        for chunk in stream:
            buf += chunk
            *complete, buf = buf.split("\n")
            for raw in complete:
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue        # a malformed record must not kill the view
                out = render.line(rec)
                if out is not None:
                    print(out, file=sys.stderr, flush=True)

    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
