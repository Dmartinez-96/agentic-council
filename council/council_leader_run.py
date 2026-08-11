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
import tempfile
from pathlib import Path

import consult_council as cc
import council_events
import council_leader as cl
import council_session as cs

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
    ap.add_argument("--interrupt-fd", type=int, default=None, metavar="N",
                    help="Read operator interrupts from already-open fd N, one per line: "
                         "'ABORT' or 'STEER <text>'. A SEPARATE CHANNEL FROM --control-fd on "
                         "purpose: that one is a BLOCKING request/response for write "
                         "approvals, so a steering message typed while an approval was "
                         "pending would be consumed as that approval's answer and silently "
                         "decline a write the council had permitted.")
    ap.add_argument("--conversation", default="", metavar="ID",
                    help="Continue (or start) a multi-turn conversation with this id. "
                         "Carries the prior turn's handoff, the derived spine and the "
                         "DECIDED ledger into this turn, and shares ONE scratch across the "
                         "whole conversation. DISTINCT FROM --session-id, which is the "
                         "Claude Code session forwarded to the council review. Omitted, a "
                         "turn behaves exactly as it always has: fresh scratch, no context.")
    ap.add_argument("--session-id", default="")
    ap.add_argument("--transcript-path", default="")
    # ELEVATION IS OPT-IN, PER TURN, FROM THE OPERATOR. Not a roster key and not a default:
    # members are fired unattended by a hook, the leader is driven by a person who is looking
    # at the screen, and only that person can widen the sandbox. Absent these flags a leader
    # turn's EXEC is byte-for-byte the member sandbox.
    ap.add_argument("--gpu", action="store_true",
                    help="let EXEC reach the host GPU. Refused at preflight if no GPU device "
                         "is present or the memory bound cannot be imposed.")
    ap.add_argument("--exec-wall", type=int, default=None, metavar="S",
                    help="wall-clock seconds per EXEC when elevated (default "
                         f"{cc.EXEC_ELEVATED_WALL_TIMEOUT}). Only meaningful with --gpu, "
                         "which is what selects the elevated profile.")
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
    # FAMILY OVERLAP, announced at SEAT TIME -- the moment a leader is actually about to
    # act, which is why it lives here and not in the council fire. In a Claude Code
    # session a leader may be CONFIGURED without acting at all, so announcing it on every
    # PostToolUse fire would be noise on every edit. Never blocks: the user's ruling is
    # that a single-family bench is a legitimate configuration, so this is surfaced and
    # the run proceeds.
    overlap = cc.leader_family_overlap(leader)
    banner = cc.format_family_overlap_banner(overlap)
    if banner:
        print(banner, file=sys.stderr)
        events.emit("leader_family_overlap", leader=overlap["leader"],
                    family=overlap["family"] or "", voting=overlap["voting"],
                    inspector=overlap["inspector"],
                    undetermined=overlap["undetermined"])

    if not args.workdir.is_dir():
        print(f"workdir is not a directory: {args.workdir}", file=sys.stderr)
        return 2

    events.emit("run_started", layer="leader_turn", tool_name=args.mode,
                target_path=str(args.workdir), voting=[leader.name], inspectors=[],
                # THE SWITCH STATE, not a hardcoded False. This was `fast_mode=False`
                # unconditionally, so a leader turn started with FAST armed reported full depth
                # and at least one consumer -- council_gui's FAST badge, `rec.get("fast_mode")` --
                # could not show it. Other consumers of the event stream were not traced.
                # WHAT IT DOES AND DOES NOT MEAN HERE: FAST governs MEMBER effort through
                # effort_for(), and this path runs no members, so the flag records that the switch
                # is ARMED for this install rather than that anything in this turn ran reduced.
                # Read it as the switch, not as an applied depth.
                fast_mode=cc.fast_mode())

    pending = {"n": 0}
    control = None
    # THE INTERRUPT CHANNEL IS NON-BLOCKING, which is the whole difference from --control-fd.
    # That one BLOCKS waiting for a specific approval answer; this one is POLLED many times a
    # second from inside the turn loop, so a read that waited would freeze the turn it exists
    # to interrupt. Set O_NONBLOCK and treat "nothing there" as "no interrupt".
    interrupt_fn = None
    if args.interrupt_fd is not None:
        try:
            os.set_blocking(args.interrupt_fd, False)
            ifh = os.fdopen(args.interrupt_fd, "r", buffering=1)
        except OSError as e:
            print(f"--interrupt-fd {args.interrupt_fd} is unusable: {e}", file=sys.stderr)
            return 2

        def _read_interrupt():
            """Return (verb, text) or None. NEVER blocks, never raises into the turn."""
            try:
                line = ifh.readline()
            except (OSError, ValueError):       # nothing available, or the fd went away
                return None
            if not line:
                return None
            line = line.strip()
            if not line:
                return None
            if line.upper() == "ABORT":
                events.emit("note", text="operator: ABORT")
                return (cl.INTERRUPT_ABORT, "")
            if line.upper().startswith("STEER "):
                msg = line[6:]
                events.emit("note", text=f"operator: STEER ({len(msg)} chars)")
                return (cl.INTERRUPT_STEER, msg)
            # UNRECOGNISED IS REPORTED, NOT SWALLOWED: an operator who typed the wrong verb
            # must not be left believing the turn was told something.
            events.emit("note", text=f"operator: unrecognised interrupt {line[:40]!r}; ignored")
            return None

        interrupt_fn = _read_interrupt

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

    # THE ELEVATED PROFILE, built and PREFLIGHTED BEFORE the turn starts. Refusing here rather
    # than at the first EXEC matters: an operator who ticked "use the GPU" should be told at
    # once that this host cannot, not after the leader has spent several model calls building
    # up to a command that then fails.
    profile = None
    if args.gpu:
        kw = {"gpu": True}
        if args.exec_wall is not None:
            kw["wall_timeout"] = args.exec_wall
        profile = cc.elevated_exec_profile(**kw)
        ok, why = cc.exec_profile_preflight(profile)
        if not ok:
            print(f"--gpu was requested but this host cannot honour it: {why}",
                  file=sys.stderr)
            events.emit("note", text=f"elevated profile refused: {why}")
            return 2
        events.emit("note", text=f"elevated profile {profile.name}: GPU on, network OFF, "
                                 f"memory {profile.mem_max}, wall {profile.wall_timeout}s")

    def on_event(name: str, **fields) -> None:
        """Bridge run_leader_turn's live callbacks onto the NDJSON stream.

        WRITES ARE DROPPED HERE, and that is not a gap. This process has TWO live sources for
        a write: the `apply_write` wrapper above, which fires at the moment of the write and
        carries the council's verdict and the approve/decline outcome; and this callback,
        which fires just after and carries only a note. Forwarding both put two records with
        DIFFERENT SCHEMAS on the stream for one write -- `verdict`/`applied` from one,
        `ok`/`note` from the other -- so a consumer would show every write twice and could
        read the verdict from whichever arrived last. The richer source wins.

        `leader_text` carries MODEL PROSE, which is why it goes through this channel at all:
        council_events redacts every string it serialises with the engine's own redactor, so
        a REQUEST_* argument quoted by the leader cannot ride out on it.
        """
        if name == "leader_action" and fields.get("action") == "write":
            return
        events.emit(name, **fields)

    # ONE SCRATCH DIRECTORY FOR THE WHOLE TURN, which is the point of it: install in round 1,
    # train in round 3, read the results in round 5. It is created OUTSIDE the workdir --
    # run_exec_sandbox refuses a scratch aimed at the tree under review, and this is what
    # makes that refusal satisfiable rather than a wall.
    # NOT deleted on exit, deliberately: a training run's artifacts are the deliverable, and a
    # turn that vanished its own outputs would be useless. The path is printed and recorded on
    # the TurnRecord so the operator knows where they are.
    # A CONVERSATION CHANGES BOTH THE SCRATCH AND THE PROMPT. Without --conversation this is
    # exactly the old one-shot turn: a fresh scratch, no prior context. With one, the scratch
    # is the conversation's (so turn 3 can see what turn 1 installed) and the previous turn's
    # handoff, spine and DECIDED ledger are carried in.
    # STARTED EXPLICITLY, NEVER INFERRED -- no model judges whether a task is "related
    # enough" to continue an older conversation, because a wrong call silently discards the
    # context the operator needed.
    prior = ""
    if args.conversation:
        if args.conversation not in cs.existing_conversations():
            cs.new_conversation(args.workdir, leader.name, cid=args.conversation)
            events.emit("note", text=f"conversation {args.conversation}: started")
        scratch = cs.scratch_dir(args.conversation)
        prior = cs.carried_context(args.conversation)
        events.emit("note", text=(
            f"conversation {args.conversation}: turn "
            f"{len(cs.turn_numbers(args.conversation)) + 1}, "
            f"carrying {len(prior)} chars of prior context"))
    else:
        scratch = Path(tempfile.mkdtemp(prefix="council_leader_scratch_"))
    events.emit("note", text=f"scratch (read-write, persists after the turn): {scratch}")

    record = asyncio.run(cl.run_leader_turn(
        leader, args.task, args.workdir, session_id=args.session_id,
        transcript_path=args.transcript_path, max_rounds=args.max_rounds,
        prior_handoff=prior,
        apply_write=apply_write, profile=profile, scratch=scratch, on_event=on_event,
        interrupt=interrupt_fn))

    # The end-of-turn summary. Every non-write action ALREADY streamed live via on_event, so
    # this is a recap, not the only report -- which is what it used to be, and it was empty:
    # this loop read `getattr(record, "results", [])` while TurnRecord had no such field, so
    # it emitted nothing on every turn ever run. TurnRecord.results now exists and is
    # populated, and the getattr is gone with it.
    for res in record.results:
        if res.kind != "write":                 # writes already reported by apply_write
            events.emit("leader_action_final", action=res.kind, target=res.arg,
                        applied=bool(res.ok), verdict="", reason=res.note or "")

    # PERSIST THE TURN, and author its handoff FIRST so the handoff is part of the same
    # atomic directory rename. The user ruled the panel runs EVERY turn (R1), over my
    # suggestion to make it opt-in so its value could be measured against the bare record.
    # THE PANEL IS LEADERLESS BY CONSTRUCTION -- author_handoff drops the turn's own leader
    # from the panel, so the actor never writes its own report card.
    # A FAILED PANEL MUST NOT LOSE THE TURN: the record is the receipts and the handoff is
    # commentary on it, so a panel that errors persists an empty handoff rather than
    # discarding everything the turn actually did.
    if args.conversation:
        # author_handoff returns a DICT, not a string -- {"handoff", "record", "panel",
        # "panelist_notes"} -- and `handoff` is the assembled text (the verbatim record plus
        # the union of the panel's notes). Checked rather than assumed: verifying a symbol
        # EXISTS is not verifying its CONTRACT, and a first version of this call passed the
        # whole dict where a string was expected.
        handoff = ""
        try:
            handoff = (asyncio.run(cl.author_handoff(record, args.workdir)) or {}).get(
                "handoff", "")
        except Exception as e:                  # noqa: BLE001 -- see above
            events.emit("note", text=f"handoff panel failed ({type(e).__name__}); "
                                     "persisting the turn record without it")
        turn_dir = cs.persist_turn(args.conversation, record, args.task, handoff)
        events.emit("note", text=f"conversation {args.conversation}: turn persisted to "
                                 f"{turn_dir}")

    stop = getattr(record, "stop_reason", "")
    events.emit("final_verdict", verdict=f"turn ended: {stop}", log_path=str(scratch),
                events_emitted=events.count, events_dropped=events.dropped_total)
    print(cl.format_turn_record(record))
    print(f"\nscratch directory (kept): {scratch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
