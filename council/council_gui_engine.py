#!/usr/bin/env python3
"""The GUI's seam onto the council engine: spawn, stream events, never import.

DELIBERATELY QT-FREE. Nothing here imports PySide6, for two reasons. The risky parts --
process spawning, pipe lifetime, partial-line parsing, cancellation -- are exactly the
parts a GUI test cannot easily reach, so they live where the ordinary test suite can
drive them. And the same seam serves any front end: the Qt app, council_watch.py, or a
future one.

WHY SUBPROCESS AND NOT `import consult_council`. The VS Code extension established the
discipline and it holds here: the engine validates its own roster (`--print-roster` is the
read path), so a front end that imports it would duplicate validation and drift from the
authority. A subprocess also means a crashing or hanging fire cannot take the GUI with it,
and that API keys stay in the engine's process environment, never in the UI's.

CANCELLATION IS A PROCESS-TREE PROBLEM. The engine spawns members (codex is a subprocess;
exec requests run bubblewrap), so terminating only the direct child can leave the real
work running -- a lesson already paid for elsewhere in this project. `cancel()` signals the
whole process GROUP and escalates to SIGKILL, then `stream()` still drains to EOF so the
caller always sees a terminal state rather than a silent stop.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator

COUNCIL_ROOT = Path(__file__).resolve().parent
ENGINE = COUNCIL_ROOT / "consult_council.py"

CANCEL_GRACE_S = 5.0        # after SIGTERM to the group, before SIGKILL


class EngineRun:
    """One council fire. Iterate `stream()` for events; read the result fields after.

    After the stream is exhausted: `returncode` is the engine's exit status (0/1/2 map to
    PASS/WARN/BLOCK), `stdout` is its full human-readable output, `stderr` likewise, and
    `cancelled` records whether the caller stopped it.
    """

    def __init__(self, args: list[str], stdin_text: str = "",
                 python: str | None = None, engine: Path | None = None,
                 control: bool = False, interrupt: bool = False) -> None:
        self.args = list(args)
        self.stdin_text = stdin_text
        self.python = python or sys.executable
        self.engine = Path(engine) if engine else ENGINE
        # A CONTROL CHANNEL is a second pipe running the OTHER way, for answers the child
        # must block on -- approve-each decisions for a leader turn. It cannot be stdin:
        # stdin carries the task and is closed immediately after, so a child reading
        # decisions there would see EOF and silently decline every write.
        self.control = control
        # AN INTERRUPT CHANNEL is a THIRD pipe, and it is separate from `control` on purpose:
        # control is a BLOCKING request/response the child reads expecting the answer to a
        # specific approval, so an operator's ABORT or STEER arriving there would be consumed
        # as that answer and silently decline a write the council had permitted.
        self.interrupt = interrupt
        self._interrupt_w = None
        self._control_w: int | None = None
        self.proc: subprocess.Popen | None = None
        self.returncode: int | None = None
        self.stdout: str = ""
        self.stderr: str = ""
        self.cancelled = False
        self.start_error: str | None = None

    def stream(self) -> Iterator[dict]:
        """Spawn the engine and yield each progress record as it arrives.

        Yields nothing and sets `start_error` if the engine cannot be launched -- a caller
        must check that rather than read an empty stream as "a fire with no events".
        """
        if not self.engine.is_file():
            self.start_error = f"engine not found at {self.engine}"
            return
        r, w = os.pipe()
        # STDOUT AND STDERR GO TO TEMP FILES, NOT PIPES, AND THIS IS LOAD-BEARING.
        # MEASURED: with stdout on a pipe that nobody drains, a child writing 200 KB
        # filled the 64 KB buffer and blocked -- and because this loop is busy reading the
        # EVENTS fd, nothing ever drained it. The events stream never reached EOF in 12s.
        # A real multi-member fire prints far more than 64 KB, so the GUI would have hung
        # on essentially every run. Draining three pipes concurrently would need threads;
        # a temp file has no capacity limit and needs none. (council_watch.py sidesteps
        # this differently, by letting stdout be inherited -- not an option here, because
        # the GUI needs the text.)
        out_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        err_f = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        self._out_f, self._err_f = out_f, err_f
        argv = [self.python, str(self.engine), *self.args, "--events-fd", str(w)]
        passed = [w]
        cr = cw = None
        if self.control:
            cr, cw = os.pipe()          # child READS cr; this process WRITES cw
            argv += ["--control-fd", str(cr)]
            passed.append(cr)
            self._control_w = cw
        # A SECOND, SEPARATE PIPE for operator interrupts. NOT the control pipe: that one is a
        # BLOCKING request/response for write approvals, and the child reads it expecting an
        # answer to a specific question -- a steering message arriving there would be consumed
        # as that answer and silently decline a write the council had permitted.
        ir = iw = None
        if self.interrupt:
            ir, iw = os.pipe()
            argv += ["--interrupt-fd", str(ir)]
            passed.append(ir)
            self._interrupt_w = iw
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=out_f, stderr=err_f,
                pass_fds=tuple(passed), text=True, cwd=str(COUNCIL_ROOT),
                # Own process GROUP so cancel() can reach the members the engine spawned,
                # not just the engine itself.
                start_new_session=True)
        except OSError as e:
            os.close(r); os.close(w)
            # ir/iw belong here too: a spawn failure that closed only the control pair would
            # leak the interrupt pair for the life of the GUI, and a leaked write end also
            # keeps a reader from ever seeing EOF.
            for fd in (cr, cw, ir, iw):
                if fd is not None:
                    os.close(fd)
            self._control_w = None
            self._interrupt_w = None
            out_f.close(); err_f.close()
            self.start_error = f"could not start the engine: {e}"
            return
        if cr is not None:
            os.close(cr)                # the CHILD owns the read end now
        if ir is not None:
            os.close(ir)                # likewise: the CHILD owns the interrupt read end
        # The PARENT's copy of the write end must go, or the read below never sees EOF
        # when the engine exits, and the caller hangs forever.
        os.close(w)
        try:
            if self.proc.stdin is not None:
                try:
                    self.proc.stdin.write(self.stdin_text)
                except BrokenPipeError:
                    pass            # the engine may exit before reading its pitch
                finally:
                    self.proc.stdin.close()
            buf = ""
            with os.fdopen(r, "r", buffering=1) as events:
                for chunk in events:
                    buf += chunk
                    *complete, buf = buf.split("\n")
                    for raw in complete:
                        if not raw.strip():
                            continue
                        try:
                            yield json.loads(raw)
                        except json.JSONDecodeError:
                            continue    # a malformed record must not end the stream
        finally:
            if self.proc is not None:
                try:
                    self.proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self._kill_group()
                    self.proc.wait()
                self.returncode = self.proc.returncode
            # communicate() would return (None, None) here: stdout/stderr are FILES, not
            # pipes, so the child's output is on disk and must be read back explicitly.
            # Reading it is what makes the redirect a fix rather than a data loss.
            for attr, fh in (("stdout", self._out_f), ("stderr", self._err_f)):
                if fh is None:
                    continue
                try:
                    fh.seek(0)
                    setattr(self, attr, fh.read())
                except (OSError, ValueError):
                    pass                # keep the default "" rather than raise at teardown
                finally:
                    try:
                        fh.close()      # TemporaryFile unlinks on close; nothing leaks
                    except OSError:
                        pass
            self._out_f = self._err_f = None
            self._close_control()

    def send_interrupt(self, line: str) -> bool:
        """Send ABORT or 'STEER <text>'. Returns False if it could not be sent.

        UNLIKE send_control THERE IS NO SAFE DEFAULT to fall back on: a control answer that
        does not arrive means the write is declined, which errs toward doing nothing, whereas
        an interrupt that does not arrive means the turn KEEPS RUNNING. So False here must be
        surfaced to the operator rather than swallowed -- otherwise a stop button silently
        does nothing.
        """
        if self._interrupt_w is None:
            return False
        try:
            os.write(self._interrupt_w, (line.rstrip("\n") + "\n").encode())
            return True
        except OSError:
            return False

    def send_control(self, line: str) -> bool:
        """Answer a question the child is blocking on. Returns False if it cannot be sent.

        The caller MUST treat False as "the child did not receive this" -- for an approval
        that means the write will be declined, which is the safe direction.
        """
        if self._control_w is None:
            return False
        try:
            os.write(self._control_w, (line.rstrip("\n") + "\n").encode())
            return True
        except OSError:
            self._close_control()
            return False

    def _close_control(self) -> None:
        """Drop the write end. A child blocked on the control channel then sees EOF and
        declines, rather than waiting forever for an answer that is never coming."""
        if self._control_w is not None:
            try:
                os.close(self._control_w)
            except OSError:
                pass
            self._control_w = None

    def _kill_group(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(self.proc.pid)
        except OSError:
            return
        for sig, wait in ((signal.SIGTERM, CANCEL_GRACE_S), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(pgid, sig)
            except OSError:
                return
            try:
                self.proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue

    def cancel(self) -> None:
        """Stop the fire and everything it spawned. Safe to call more than once."""
        self.cancelled = True
        self._kill_group()


def print_roster(python: str | None = None, engine: Path | None = None) -> dict:
    """Read the ACTIVE roster from the engine -- the GUI's single source of truth.

    The engine owns roster validation, so the GUI must never parse roster.json itself: a
    second validator would disagree with the first eventually, and the user would be
    configuring against a fiction. Returns the engine's own JSON, or an `error` key.
    """
    eng = Path(engine) if engine else ENGINE
    try:
        res = subprocess.run(
            [python or sys.executable, str(eng), "--print-roster"],
            capture_output=True, text=True, timeout=120, cwd=str(COUNCIL_ROOT))
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": f"could not read the roster: {e}"}
    if res.returncode != 0:
        return {"error": f"engine exited {res.returncode}", "stderr": res.stderr}
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        return {"error": f"roster output was not JSON: {e}", "stdout": res.stdout[:2000]}
