#!/usr/bin/env python3
"""NDJSON progress events for the council engine -- the live-progress channel.

WHY THIS EXISTS. The engine emits nothing until it exits: `emit_output` is its single
output call site in `main()` and it follows every `asyncio.gather` barrier. Measured over
454 fires across three log days, the slowest seat alone runs median 133.8s / p90 236.0s /
max 415.0s, so anything watching a fire saw silence for minutes and then everything at
once. This module carries per-seat results out AS THEY LAND, changing neither what the
council computes nor the order it computes it in.

FOUR PROPERTIES, each of which exists because the alternative was measured and failed.

1. A SEPARATE FILE DESCRIPTOR, NEVER STDOUT. The PostToolUse hook, the VS Code extension
   and `council_leader`'s subprocess review all parse the engine's stdout, and
   `review_and_write` keys its fail-closed decision on the FIRST LINE of it. Progress
   records there would corrupt that contract silently -- reviews would still "work" while
   the verdict line moved. With no fd supplied this module is inert.

2. IT CAN NEVER BLOCK THE FIRE. A pipe holds 65536 bytes on this host (measured via
   F_GETPIPE_SZ); `os.write` past that with a consumer that is not draining BLOCKS. A
   stalled GUI would therefore stall a council review. So the fd is set O_NONBLOCK,
   EAGAIN means "consumer is behind" rather than "failure", and records are dropped and
   counted rather than waited on. A lost progress record costs nothing; a stalled review
   costs everything. Measured: 4000 records against a consumer that never reads return in
   0.05s instead of hanging.

3. THE STREAM CANNOT BE CORRUPTED. Records are buffered WHOLE and a partial write is
   retained and completed on the next emit, so a half-written line is never followed by a
   different record. On overflow NEW records are dropped rather than evicting older ones,
   because the front of the buffer may already be partly on the wire. When the backlog
   clears, one `dropped` record reports the loss -- silence about it would make a stalled
   consumer look like a quiet council.

4. REDACTION IS BY CONSTRUCTION. This is a THIRD sink for member text after the peer
   broadcast and `logs/`, both of which strip REQUEST_* ARGUMENTS (a member-supplied path,
   URL or command). The redactor is a REQUIRED constructor argument, and every string --
   including dict keys, nested containers, and non-strings rendered via `str()` -- passes
   through it before serialisation. No call site can forget, and no future field can
   quietly become the one unredacted channel. The engine passes its own
   `_redact_request_lines`; this module keeps no second copy of that pattern, because two
   copies drift.

A NOTE ON WHAT THIS DOES NOT FIX. A consumer typically also reads the engine's stdout, and
a child writing there through Python's `print()` into a pipe is BLOCK-buffered: measured,
four lines arrive together at 1.21s rather than at 0.01/0.41/0.81/1.21s as written. That
is a property of the consumer's own plumbing (`-u` / PYTHONUNBUFFERED), not of this module,
which writes raw and unbuffered.
"""

from __future__ import annotations

import fcntl
import json
import os
from typing import Any, Callable

# The event vocabulary. Consumers should IGNORE an unknown `ev` rather than fail, so this
# set can grow without breaking an older renderer.
#
#   run_started      layer, tool_name, target_path, voting[], inspectors[], fast_mode
#   round_started    round
#   member_started   member, tier, round
#   member_finished  member, tier, round, verdict, duration_s, model_used, cost,
#                    prompt_tokens, completion_tokens
#   member_corrected member, tier, was, verdict, why -- a seat's verdict CHANGED after it
#                    was already reported: the formatting retry runs after the round
#                    gather, so a seat streamed as UNPARSEABLE can end up counted as PASS.
#                    A consumer that ignores this will display a verdict the council did
#                    not aggregate.
#   round_finished   round, verdicts{}
#   tool_request     member, kind (file|url|exec), granted -- NEVER the argument itself
#   leader_action    action, target, verdict, applied
#   leader_round     round -- a leader round began
#   leader_text      round, text -- that round's COMPLETE reply (not token streaming)
#   leader_problem   round, problems[] -- an actions envelope rejected WHOLE; none of it ran
#   leader_reprompt  round -- the turn tried to end with no WRITE ever emitted and was sent
#                    back once (council_leader.run_leader_turn). Fires at most once per turn
#   leader_steer     round -- the OPERATOR's mid-turn STEER was ACCEPTED AND QUEUED. It says
#                    the message was read and will go into the NEXT prompt assembled; it does
#                    NOT promise a prompt was assembled. A turn that ends in that same round
#                    (final answer, call failure, abort) never delivers it, so a consumer must
#                    render this as "queued", never as "delivered".
#                    Distinct from leader_reprompt: that is the HARNESS nudging the leader,
#                    this is the HUMAN redirecting it. An ABORT emits no event of its own --
#                    it ends the turn, and final_verdict carries the stop_reason
#   leader_action_final  action, target, applied, verdict, reason -- end-of-turn recap
#   approval_request id, target, verdict, bytes -- an operator decision is pending
#   final_verdict    verdict, log_path
#   dropped          n -- records lost while the consumer was behind (emitted by us)
#   note             text -- free-form diagnostics
#   leader_family_overlap  leader, family, voting[], inspector[], undetermined[] -- the
#                    seated leader shares a model FAMILY with seats on its own review
#                    panel. ADVISORY AND NEVER BLOCKING (a single-family bench is a valid
#                    configuration); it exists so the result is not mistaken for
#                    independent review. `undetermined` lists seats whose family could not
#                    be resolved, which a consumer must NOT render as "no overlap"
# THIS LIST HAD GONE STALE, which is worth stating rather than quietly correcting: it is
# referenced by nothing (grep: this line is its only occurrence), so it is documentation and
# unenforced documentation at that. SIX names were missing when this was written --
# leader_round, leader_text, leader_problem, leader_action_final and approval_request had all
# been emitted for some time; leader_reprompt is new in the same change as this comment.
# Nothing broke, because the contract is that a consumer IGNORES an unknown `ev`. The cost is
# narrower than a bug and worth naming exactly: a reader asking "what does the engine emit?"
# and answering from this tuple got a wrong answer. That is NOT what caused the GUI to drop
# four leader events -- those were dropped by missing handler branches, and this tuple, being
# unused, could not have caused it. It is the same class of staleness, not the same defect.
EVENT_NAMES = (
    "run_started", "round_started", "member_started", "member_finished",
    "member_corrected", "round_finished", "tool_request", "leader_action",
    "leader_round", "leader_text", "leader_problem", "leader_reprompt", "leader_steer",
    "leader_action_final", "approval_request", "final_verdict", "dropped", "note",
    "leader_family_overlap",
)

FIELD_MAX = 4000            # chars per string field before truncation
PENDING_MAX = 1 << 20       # bytes of unflushed backlog before new records are dropped
_TRUNC = "...[truncated]"


def _bound(s: str) -> str:
    """Bound one field, charging the marker against the budget (never appending past it)."""
    if len(s) <= FIELD_MAX:
        return s
    return s[: FIELD_MAX - len(_TRUNC)] + _TRUNC


def _normalise(value: Any, redact: Callable[[str], str]) -> Any:
    """Recursively render JSON-safe, redacting EVERY string produced along the way.

    Non-primitives are stringified HERE rather than by `json.dumps(default=str)`, because
    that runs AFTER redaction and would let `str(obj)` carry an unredacted argument out.
    """
    if isinstance(value, str):
        return _bound(redact(value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {_bound(redact(str(k))): _normalise(v, redact) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(v, redact) for v in value]
    return _bound(redact(str(value)))


class EventEmitter:
    """Write NDJSON records to `fd`. Inert when `fd` is None or after a hard failure.

    `count` means records ACCEPTED into the buffer, not records delivered -- delivery is
    observable only at the consumer, never from this side.

    TWO drop counters, because one cannot answer both questions. `dropped` is the running
    arrears used to build the next `dropped` accounting record, and it is RESET to zero
    once that record is queued -- so it reports what has not yet been confessed, and reads
    0 after a confession even though records were genuinely lost. `dropped_total` is the
    lifetime figure and is never reset. A summary that wants "was anything lost this run?"
    must read `dropped_total`; reading `dropped` there would report 0 after loss.
    """

    def __init__(self, fd: int | None, redactor: Callable[[str], str]) -> None:
        if not callable(redactor):
            raise TypeError("EventEmitter requires a callable redactor "
                            "(pass the engine's _redact_request_lines)")
        self.fd = fd
        self._redact = redactor
        self.disabled_reason: str | None = None
        self.count = 0
        self.dropped = 0
        self.dropped_total = 0
        self._pending = bytearray()

    @property
    def active(self) -> bool:
        return self.fd is not None and self.disabled_reason is None

    def _drain(self) -> None:
        """Push as much of the backlog as the fd will take, without ever waiting."""
        while self._pending:
            try:
                n = os.write(self.fd, self._pending)
            except BlockingIOError:
                return                      # consumer behind; backlog stays intact
            except OSError as e:
                self.disabled_reason = f"{type(e).__name__}: {e}"
                self._pending.clear()
                return
            if n <= 0:
                return
            del self._pending[:n]

    def emit(self, ev: str, **fields: Any) -> bool:
        """Queue one record. Returns True iff it was accepted and the stream is still
        alive. Never raises -- progress reporting must not be able to fail a review."""
        if not self.active:
            return False
        try:
            rec: dict[str, Any] = {"ev": _bound(self._redact(str(ev)))}
            for k, v in fields.items():
                rec[k] = _normalise(v, self._redact)
            payload = (json.dumps(rec, ensure_ascii=True) + "\n").encode()
        except Exception as e:              # noqa: BLE001 -- see the never-raises contract
            self.disabled_reason = f"{type(e).__name__}: {e}"
            return False
        if len(self._pending) + len(payload) > PENDING_MAX:
            self.dropped += 1          # arrears, reset once confessed below
            self.dropped_total += 1    # lifetime, never reset
            self._drain()
            return False
        if self.dropped and not self._pending:
            self._pending.extend(
                (json.dumps({"ev": "dropped", "n": self.dropped}) + "\n").encode())
            self.dropped = 0
        self._pending.extend(payload)
        self.count += 1
        self._drain()
        # `_drain` can discover a dead fd and disable the stream synchronously; reporting
        # True then would tell the caller a record is on its way to a fd we just gave up on.
        return self.active


def emitter_from_fd(fd: int | None, redactor: Callable[[str], str]) -> EventEmitter:
    """Build an emitter, proving the fd is WRITABLE before the fire starts.

    Measured: `os.fstat` answers "ok" for a READ-ONLY fd as readily as a writable one, so
    it cannot tell a usable events fd from an unusable one -- a void check. F_GETFL's
    access mode discriminates. An unusable fd degrades to an inert emitter carrying
    `disabled_reason`, so the council still runs.
    """
    if fd is None:
        return EventEmitter(None, redactor)
    em = EventEmitter(fd, redactor)
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if flags & os.O_ACCMODE not in (os.O_WRONLY, os.O_RDWR):
            em.disabled_reason = f"fd {fd} is not open for writing"
            return em
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)   # property 2
    except OSError as e:
        em.disabled_reason = f"fd {fd} is not usable: {type(e).__name__}: {e}"
    return em
