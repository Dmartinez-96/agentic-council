#!/usr/bin/env python3
"""Run one leader turn as a subprocess, with a permission mode -- the Leader tab's engine.

    council_leader_run.py --task "..." --workdir DIR [--mode auto|approve-each|plan-only]
                          [--events-fd N] [--max-rounds 8]

WHY THIS EXISTS AS A SEPARATE PROGRAM. council_leader.py is a library with no CLI, so a
GUI could only drive a leader by importing the engine into its own process -- where a hung
model call would freeze the window and a crash would take the UI with it. A subprocess
keeps the same isolation the council fires already have, and lets the GUI stream a turn
the same way it streams a fire: one NDJSON event channel, one renderer.

THE PERMISSION MODES, which are issue #8 landing inside the leader surface rather than
gating it:

  auto          every write the COUNCIL permits is applied. The pre-emptive wall still
                runs -- a BLOCK never reaches disk -- this mode simply adds no second gate.
  approve-each  the operator is asked before each permitted write, and sees the target,
                the council's verdict and its review text first. Implemented through
                review_and_write's `approve` seam, which is consulted ONLY on the PASS/WARN
                path: it can refuse a write the council allowed, and can never allow one
                the council refused.
  plan-only     no write is ever applied. The council still reviews each proposal so the
                operator sees what WOULD have happened, but the applier is never reached.

THE APPROVAL PROTOCOL, when the mode is approve-each. Decisions arrive on a DEDICATED
`--control-fd`, never on stdin: the GUI's engine seam writes the pitch to stdin and closes
it before draining events, so an approval read from stdin would hit EOF and auto-decline
every single time -- the runner would appear to work and silently refuse everything.

This process emits an `approval_request` event carrying an id, the target, the verdict and
a bounded preview, then blocks reading one line from the control fd. The controller answers
`APPROVE <id>`; anything else, a mismatched id, or EOF DECLINES, because an operator who
went away did not approve.

AND IF THE REQUEST COULD NOT BE SENT, THE ANSWER IS NO. The event channel is deliberately
lossy -- it drops records rather than stall a review -- so an `approval_request` can fail
to reach the operator. Blocking for a reply to a question nobody was asked would hang the
turn forever, so a failed emit declines immediately. The gate can only ever subtract
permission, and that has to hold when the gate itself is degraded.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import consult_council as cc
import council_events
import council_leader as cl

MODES = ("auto", "approve-each", "plan-only")
PREVIEW_MAX = 4000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--task", required=True)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--mode", choices=MODES, default="approve-each")
    ap.add_argument("--max-rounds", type=int, default=cl.LEADER_MAX_ROUNDS_PER_TURN)
    ap.add_argument("--events-fd", type=int, default=None)
    ap.add_argument("--control-fd", type=int, default=None, metavar="N",
                    help="Read approve-each decisions from already-open fd N, one line "
                         "per decision ('APPROVE <id>'). NOT stdin: the GUI seam closes "
                         "stdin after sending the task, so decisions read from there "
                         "would EOF and silently decline every write.")
    ap.add_argument("--session-id", default="")
    ap.add_argument("--transcript-path", default="")
    args = ap.parse_args()

    events = council_events.emitter_from_fd(args.events_fd, cc._redact_request_lines)

    # ARGUMENT validation first, before anything about the roster: a caller who asked for
    # approve-each without a control channel has made a mistake that is true regardless of
    # which leader is configured, and reporting the roster problem instead would hide it.
    if args.mode == "approve-each" and args.control_fd is None:
        print("--mode approve-each requires --control-fd: there would be no channel to "
              "ask on, and every write would be declined without the operator ever "
              "seeing it.", file=sys.stderr)
        return 2

    leader = cc.active_leader()
    if leader is None:
        print("no council-native leader is configured (roster.json's top-level `leader` "
              "key). The Claude Code harness leads by default, which this runner cannot "
              "drive.", file=sys.stderr)
        events.emit("note", text="no council-native leader configured")
        return 2
    if not args.workdir.is_dir():
        print(f"workdir is not a directory: {args.workdir}", file=sys.stderr)
        return 2

    events.emit("run_started", layer="leader_turn", tool_name=args.mode,
                target_path=str(args.workdir), voting=[leader.name], inspectors=[],
                fast_mode=False)

    pending = {"n": 0}
    control = None
    if args.control_fd is not None:
        try:
            control = os.fdopen(args.control_fd, "r", buffering=1)
        except OSError as e:
            print(f"--control-fd {args.control_fd} is unusable: {e}", file=sys.stderr)
            return 2
    def ask_operator(target, content, verdict, review_text) -> bool:
        """Ask the operator over the control fd. Anything short of an explicit yes is no."""
        pending["n"] += 1
        req_id = f"w{pending['n']}"
        sent = events.emit("approval_request", id=req_id, target=str(target),
                           verdict=verdict,
                           bytes=len(content.encode("utf-8", "surrogatepass")),
                           preview=content[:PREVIEW_MAX], review=review_text)
        if not sent:
            # The question never reached anyone. Waiting for its answer would hang the
            # turn forever, so decline: an unasked operator has not approved.
            return False
        line = control.readline()
        if not line:
            events.emit("note", text=f"{req_id}: control channel closed; declining")
            return False
        ok = line.strip() == f"APPROVE {req_id}"
        events.emit("note", text=f"{req_id}: {'approved' if ok else 'declined'} by operator")
        return ok

    def apply_write(ldr, rel_path, content, workdir, *, session_id="",
                    transcript_path="", review=cl._council_review):
        if args.mode == "plan-only":
            # The council still reviews, so the operator sees the verdict a real write
            # would have drawn; only the application is withheld.
            res = cl.review_and_write(ldr, rel_path, content, workdir,
                                      session_id=session_id,
                                      transcript_path=transcript_path, review=review,
                                      approve=lambda *_a: False)
            if res.get("verdict") == "DECLINED":
                res = {**res, "reason": "plan-only mode: no write is ever applied"}
        else:
            approve = ask_operator if args.mode == "approve-each" else None
            res = cl.review_and_write(ldr, rel_path, content, workdir,
                                      session_id=session_id,
                                      transcript_path=transcript_path, review=review,
                                      approve=approve)
        events.emit("leader_action", action="write", target=str(rel_path),
                    verdict=res.get("verdict"), applied=bool(res.get("applied")),
                    reason=res.get("reason") or "")
        return res

    record = asyncio.run(cl.run_leader_turn(
        leader, args.task, args.workdir, session_id=args.session_id,
        transcript_path=args.transcript_path, max_rounds=args.max_rounds,
        apply_write=apply_write))

    for res in getattr(record, "results", []) or []:
        if getattr(res, "kind", "") != "write":     # writes already reported above
            events.emit("leader_action", action=getattr(res, "kind", "?"),
                        target=getattr(res, "arg", ""), applied=bool(getattr(res, "ok", False)),
                        verdict="", reason=getattr(res, "note", "") or "")

    stop = getattr(record, "stop_reason", "")
    events.emit("final_verdict", verdict=f"turn ended: {stop}", log_path="",
                events_emitted=events.count, events_dropped=events.dropped_total)
    print(cl.format_turn_record(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
