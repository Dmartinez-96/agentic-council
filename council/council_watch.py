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
import json
import os
import subprocess
import sys
from pathlib import Path

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


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog="All other arguments are forwarded to consult_council.py unchanged.")
    ap.add_argument("--no-colour", action="store_true",
                    help="Never colourise, even on a TTY.")
    args, forwarded = ap.parse_known_args()

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
