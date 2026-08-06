#!/usr/bin/env python3
"""council_session.py -- persistence for a MULTI-TURN leader conversation.

A leader turn is a bounded one-shot: `run_leader_turn` accepts a `prior_handoff` that, until
now, no production caller ever set, and `author_handoff` had no production caller at all. The
pieces of a conversation existed and nothing connected them. This module is the connection.

WHAT A CONVERSATION IS HERE: an explicitly-started sequence of turns sharing one directory,
one scratch, and one carried context. It is started explicitly and never inferred -- the user
ruled that no model may judge whether a new task is "related enough" to continue an old
conversation, because a wrong call silently discards the context you needed.

WHAT IS CARRIED, and the rule that decides it:
    ANYTHING CARRIED ACROSS TURNS IS EITHER DETERMINISTIC OR VERBATIM -- NEVER RE-SUMMARIZED.
Re-summarizing is where drift comes from: a summary of a summary can be checked against
nothing. So:
  - THE SPINE is DERIVED, freshly, from each turn's round NOTES. It cannot drift because it is
    recomputed from the record every time. It also cannot carry intent -- it is paths, verdicts
    and denials.
  - THE DECIDED LEDGER is VERBATIM. Lines the leader declared, carried unchanged, never
    rewritten by a later turn.
  - THE PANEL HANDOFF is carried verbatim from the previous turn.

THE SPINE READS ROUND NOTES, NOT `format_turn_record` OUTPUT, and that distinction is
load-bearing rather than fussy: format_turn_record APPENDS the leader's own summary, which its
docstring says "could quote content it saw, so it is NOT metadata-clean the way the notes
are." Deriving a supposedly-deterministic spine from that would pipe unverified model prose
into derived state -- laundering drift through the mechanism built to prevent it.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

CONVERSATIONS_ROOT = Path.home() / ".council" / "sessions"

# The DECIDED ledger's byte budget. AT THE CAP A NEW DECISION REQUIRES SUPERSEDING AN OLD ONE
# rather than silently evicting the oldest -- the user's ruling, and the reason is that
# oldest-first eviction discards the FOUNDATIONAL decisions the ledger exists to preserve. The
# pressure is surfaced to the leader instead of resolved behind its back.
DECIDED_LEDGER_MAX_BYTES = 16_000
# The spine's budget. Unlike the ledger this MAY drop oldest-first: it is DERIVED, so a
# dropped line is recoverable by reading the turn record it came from, and the drop is stated.
SPINE_MAX_BYTES = 8_000


def _root() -> Path:
    CONVERSATIONS_ROOT.mkdir(parents=True, exist_ok=True)
    return CONVERSATIONS_ROOT


def conversation_dir(cid: str) -> Path:
    """The directory for a conversation id, WITHOUT creating it.

    The id is confined to one path component: a `/` or `..` in a caller-supplied id would
    otherwise escape the root, and this path is used to write files.
    """
    if not cid or "/" in cid or "\\" in cid or cid.startswith("."):
        raise ValueError(f"unusable conversation id {cid!r}")
    return _root() / cid


def new_conversation(workdir: Path, leader: str, cid: str | None = None) -> str:
    """Create a conversation directory and return its id.

    `cid` is injectable so a caller (or a test) can name the conversation; otherwise the id is
    a UTC timestamp, which sorts chronologically in a directory listing.
    """
    cid = cid or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    d = conversation_dir(cid)
    (d / "turns").mkdir(parents=True, exist_ok=True)
    (d / "scratch").mkdir(parents=True, exist_ok=True)
    meta = {"conversation_id": cid, "workdir": str(workdir), "leader": leader,
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _atomic_json(d / "meta.json", meta)
    return cid


def scratch_dir(cid: str) -> Path:
    """The ONE scratch shared by every turn of this conversation.

    Per-turn scratch was the old behaviour and it made multi-turn build work impossible: turn
    1 installs, turn 3 trains, turn 5 reads the results, and a fresh mkdtemp each turn meant
    turn 3 could not see turn 1's work.
    """
    d = conversation_dir(cid) / "scratch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_json(path: Path, obj) -> None:
    """Write JSON via tmp+replace, so a reader never sees a half-written file."""
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def turn_numbers(cid: str) -> list:
    """Completed turn numbers, ascending. A `.partial` directory is NOT a completed turn."""
    t = conversation_dir(cid) / "turns"
    if not t.is_dir():
        return []
    out = []
    for p in t.iterdir():
        if p.is_dir() and p.name.isdigit():
            out.append(int(p.name))
    return sorted(out)


def persist_turn(cid: str, record, task: str, handoff: str = "") -> Path:
    """Write one turn's state, and make it visible ONLY when it is complete.

    THE WHOLE DIRECTORY IS THE UNIT. Files are written into `NNNN.partial/` and the DIRECTORY
    is renamed into place at the end, so a reader either sees a complete turn or no turn. A
    per-file atomic replace would not be enough here: a turn is several files, and an
    interrupt landing between them would leave a directory that exists, looks finished, and is
    missing its handoff -- which the NEXT turn would then read as its context.

    Returns the final directory path.
    """
    n = (turn_numbers(cid)[-1] + 1) if turn_numbers(cid) else 1
    turns = conversation_dir(cid) / "turns"
    turns.mkdir(parents=True, exist_ok=True)
    final = turns / f"{n:04d}"
    partial = turns / f"{n:04d}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)

    (partial / "task.txt").write_text(task, encoding="utf-8")
    (partial / "handoff.md").write_text(handoff, encoding="utf-8")
    # NOTES ONLY -- see the module docstring. This is what the spine is derived from, and it
    # must stay metadata-clean.
    (partial / "notes.json").write_text(json.dumps(
        [{"round": r["round"], "notes": list(r["notes"])} for r in record.rounds],
        indent=2), encoding="utf-8")
    state = {
        "stop_reason": record.stop_reason,
        "reprompted": bool(record.reprompted),
        "writes": {k: list(v) for k, v in (record.writes or {}).items()},
        "claims": [dict(c) for c in (record.claims or ())],
        "intent": record.intent,
    }
    _atomic_json(partial / "state.json", state)
    os.replace(partial, final)
    return final


def prior_handoff(cid: str) -> str:
    """The most recent completed turn's handoff, or "" when there is none."""
    ns = turn_numbers(cid)
    if not ns:
        return ""
    p = conversation_dir(cid) / "turns" / f"{ns[-1]:04d}" / "handoff.md"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def decided_ledger(cid: str) -> list:
    """Every DECIDED line declared in this conversation, VERBATIM, oldest first.

    Each entry is {"turn", "decided", "superseded_by", "failed_execs"}. The last carries the
    commands from THAT SAME TURN which exited non-zero or were killed, so a decision never
    travels without the record that bears on it. A decision a later turn superseded is
    RETAINED AND MARKED, never deleted -- the same rule the brain vault applies to a
    superseded note, and for the same reason: that a decision was reversed is itself something
    the next turn needs to know.

    SUPERSEDES is matched by TURN NUMBER, which is why the number is shown to the leader.
    """
    out = []
    for n in turn_numbers(cid):
        p = conversation_dir(cid) / "turns" / f"{n:04d}" / "state.json"
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        it = st.get("intent") or {}
        if it.get("decided"):
            # THE RECORD TRAVELS WITH THE CLAIM. A DECIDED line is the leader's own words and
            # is carried VERBATIM, which means a false one is carried faithfully too --
            # MEASURED 2026-08-05, a leader wrote "the exact requested command completed
            # successfully" for a command the harness had recorded as `exit -9, WALL-TIMEOUT,
            # group killed`, and the ledger would have repeated that to every later turn.
            # SO THE SAME TURN'S FAILED COMMANDS ARE ATTACHED. BE EXACT ABOUT WHAT THAT IS,
            # because an earlier version of this comment called it "the contradicting fact"
            # and codex was right to refuse that: a non-zero command in the same turn is NOT
            # necessarily RELATED to this decision, let alone contradictory of it. A turn can
            # run a failing test and then decide something unconnected to it.
            # WHAT IS ESTABLISHED: this decision was made in a turn that also had commands
            # which did not succeed. That is co-occurrence, not refutation.
            # WHY IT IS STILL WORTH CARRYING: the ledger repeats a leader's words VERBATIM to
            # every later turn, so without this a reader gets the assertion and none of the
            # turn's outcomes. Nothing here reads the prose or judges the claim -- that would
            # be a substring hypothesis about meaning, which this project has refused
            # elsewhere and should refuse here.
            failed = (st.get("writes") or {}).get("failed_execs") or []
            out.append({"turn": n, "decided": it["decided"], "superseded_by": [],
                        "failed_execs": [
                            {"command": f.get("command"), "exit_status": f.get("exit_status")}
                            for f in failed]})
        for s in it.get("supersedes") or []:
            # "turn <N> -- reason"; take the first integer as the target.
            digits = "".join(ch if ch.isdigit() else " " for ch in s).split()
            if digits:
                target = int(digits[0])
                for e in out:
                    if e["turn"] == target:
                        e["superseded_by"].append({"turn": n, "reason": s})
    return out


def existing_conversations() -> list:
    """Conversation ids that already have a directory, newest-named last."""
    if not CONVERSATIONS_ROOT.is_dir():
        return []
    return sorted(p.name for p in CONVERSATIONS_ROOT.iterdir() if p.is_dir())


def carried_context(cid: str) -> str:
    """Everything this conversation hands to its next turn, as one block.

    THREE PARTS, EACH WITH A DIFFERENT PROVENANCE, and they are labelled separately because a
    reader (human or model) must be able to tell which is which:
      SPINE    -- DERIVED from turn records. Deterministic; cannot drift; carries no intent.
      DECIDED  -- the leader's OWN words, VERBATIM, never rewritten. Carries intent; cannot
                  drift because nothing ever re-summarizes it. Superseded lines are MARKED and
                  KEPT, since a reversal is itself something the next turn needs.
      HANDOFF  -- the previous turn's panel-authored handoff, verbatim.
    NOTHING HERE IS RE-SUMMARIZED. That is the whole rule: a summary of a summary can be
    checked against nothing.
    """
    parts = []
    spine = conversation_spine(cid)
    if spine:
        parts.append("## CONVERSATION SO FAR (derived from the turn records; not authored)\n"
                     + spine)
    led = decided_ledger(cid)
    if led:
        lines = []
        for e in led:
            mark = ""
            if e["superseded_by"]:
                who = ", ".join(f"turn {s['turn']}" for s in e["superseded_by"])
                mark = f"  [SUPERSEDED by {who}]"
            # The caution rides WITH the line, not in a footnote a reader may not reach.
            fx = e.get("failed_execs") or []
            if fx:
                # "ALSO IN THAT TURN", not "weigh this against that". The earlier wording
                # presupposed the failed commands bore on the decision, which is precisely the
                # relevance the code cannot establish -- and this is the string a later leader
                # READS, so it matters more than any comment about it.
                mark += ("  [ALSO IN THAT TURN, relevance not established: " + ", ".join(
                    f"`{f['command']}` exited {f['exit_status']}" for f in fx[:3]) + "]")
            lines.append(f"- turn {e['turn']}: {e['decided']}{mark}")
        parts.append("## DECISIONS THIS CONVERSATION HAS MADE (verbatim; superseded ones are\n"
                     "## kept and marked, because a reversal is information)\n" + "\n".join(lines))
    h = prior_handoff(cid)
    if h:
        parts.append("## HANDOFF FROM THE PREVIOUS TURN\n" + h)
    return "\n\n".join(parts)


def conversation_spine(cid: str, cap: int = SPINE_MAX_BYTES) -> str:
    """One line per completed turn, DERIVED from that turn's notes and state.

    Never model-authored, so it cannot drift. Oldest lines are dropped first under `cap` WITH
    AN EXPLICIT NOTICE -- a silent truncation would read as "this is the whole conversation".
    """
    lines = []
    for n in turn_numbers(cid):
        d = conversation_dir(cid) / "turns" / f"{n:04d}"
        try:
            st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task = ""
        try:
            task = (d / "task.txt").read_text(encoding="utf-8").strip().splitlines()[0][:70]
        except (OSError, IndexError):
            pass
        w = st.get("writes") or {}
        applied = [Path(t).name for t in (w.get("applied") or [])]
        unapplied = [u.get("path") for u in (w.get("unapplied") or [])]
        bits = [f"turn {n}: {task}"]
        if applied:
            bits.append(f"applied={applied}")
        if unapplied:
            bits.append(f"UNAPPLIED={unapplied}")
        if w.get("altered"):
            bits.append(f"ALTERED={len(w['altered'])}")
        lines.append("  ".join(bits))
    if not lines:
        return ""
    out, dropped = [], 0
    total = 0
    for ln in reversed(lines):                       # keep the NEWEST under the cap
        if total + len(ln) + 1 > cap:
            dropped += 1
            continue
        out.append(ln)
        total += len(ln) + 1
    out.reverse()
    head = f"({dropped} earlier turn(s) omitted for size)\n" if dropped else ""
    return head + "\n".join(out)
