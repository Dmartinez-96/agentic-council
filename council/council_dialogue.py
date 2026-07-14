#!/usr/bin/env python3
"""Workers' Council - dialogue mode (Claude-driven round-table).

A multi-turn, round-table dialogue between Claude (the lead worker and
spokesperson to the user) and the council members (codex, gemini,
deepseek). Unlike the one-shot PostToolUse council, here Claude is a
participant: members can interrogate Claude, Claude answers with
evidence, and the thread iterates to convergence.

Claude drives it from the conversation via Bash, so every member
response lands natively in Claude's context:

    council_dialogue.py start  [--evidence-file P] [--attach F ...] "<proposal>"
    council_dialogue.py say    <thread-id> [--attach F ...] "<Claude's reply>"
    council_dialogue.py resolve <thread-id> [--force]
    council_dialogue.py show   <thread-id>

Design (hardened against the design-review findings):
  - Convergence and consensus are MACHINE-COMPUTED, never Claude-judged.
    Every command prints an authoritative CONVERGENCE / CONSENSUS line.
  - A member is terminal iff verdict in {PASS,WARN,BLOCK} AND it has no
    open QUESTIONS. An open question blocks convergence regardless of
    verdict. DELIBERATING requires >=1 question, else normalized to WARN.
  - Consensus (only meaningful once converged): BLOCK if any BLOCK; PASS
    if all PASS; else WARN.
  - Hard MAX_TURNS cap; at the cap `say` refuses and points to resolve.
    resolve coerces non-terminal members to WARN (never silently PASS)
    and stamps PREMATURE if forced before convergence.
  - Roster frozen at start (deepseek dropped once if no key). Mid-dialogue
    ERROR carries forward the member's last good verdict (marked stale);
    a member dropped after MAX_CONSEC_ERRORS consecutive errors.
  - resolve writes an immutable ASCII FINAL artifact under logs/<date>/.
  - Thread state mutations are atomic (os.replace) under an flock.

Reuses consult_council's member runners unchanged; the one-shot wrapper
is not modified. The full dialogue transcript is passed to each member
via consult_council's round1_block prompt slot.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import consult_council as cc

COUNCIL_ROOT = Path(__file__).resolve().parent
THREADS_DIR = COUNCIL_ROOT / "threads"
LOGS_ROOT = COUNCIL_ROOT / "logs"
BASE_PROMPT_PATH = COUNCIL_ROOT / "council_system_prompt.md"
DIALOGUE_PROMPT_PATH = COUNCIL_ROOT / "council_dialogue_prompt.md"

MAX_TURNS = 6
MAX_CONSEC_ERRORS = 2
DIALOGUE_TIMEOUT_S = 300
THREAD_BLOCK_MAX_BYTES = 200_000

# Tighten the per-member timeout for the interactive dialogue (the
# one-shot default is 600s). Both call sites in consult_council read this
# module global at call time, so reassigning it here takes effect.
cc.PER_CRITIC_TIMEOUT_S = DIALOGUE_TIMEOUT_S

DIALOGUE_VERDICT_RE = re.compile(
    r"^VERDICT:\s*(PASS|WARN|BLOCK|DELIBERATING)\s*$", re.MULTILINE)
TERMINAL_VERDICTS = ("PASS", "WARN", "BLOCK")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Parsing member output
# --------------------------------------------------------------------------

def parse_dialogue_verdict(text: str) -> str:
    """Return the LAST VERDICT token (so a member quoting a peer's verdict
    earlier in its prose does not win). UNPARSEABLE if none."""
    if not text:
        return "UNPARSEABLE"
    matches = DIALOGUE_VERDICT_RE.findall(text)
    return matches[-1] if matches else "UNPARSEABLE"


_SECTION_HEADER_RE = re.compile(r"^(QUESTIONS|REASONS|NOTES|VERDICT)\s*:",
                                re.MULTILINE)


def extract_questions(text: str) -> list[str]:
    """Return the non-empty bullet lines under a QUESTIONS: header, up to
    the next section header. Empty list if there is no question."""
    if not text:
        return []
    m = re.search(r"^QUESTIONS\s*:\s*$", text, re.MULTILINE)
    if not m:
        # tolerate "QUESTIONS: <inline>" on the same line
        m2 = re.search(r"^QUESTIONS\s*:\s*(\S.*)$", text, re.MULTILINE)
        if m2:
            return [m2.group(1).strip()]
        return []
    start = m.end()
    nxt = _SECTION_HEADER_RE.search(text, start)
    body = text[start:nxt.start()] if nxt else text[start:]
    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("-"):
            s = s[1:].strip()
        if s:
            out.append(s)
    return out


# --------------------------------------------------------------------------
# Thread state (atomic + flock)
# --------------------------------------------------------------------------

def thread_path(tid: str) -> Path:
    return THREADS_DIR / f"{tid}.json"


def lock_path(tid: str) -> Path:
    return THREADS_DIR / f"{tid}.lock"


class ThreadLock:
    def __init__(self, tid: str):
        self._lp = lock_path(tid)

    def __enter__(self):
        THREADS_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._lp, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def load_thread(tid: str) -> dict | None:
    p = thread_path(tid)
    if not p.exists():
        return None
    import json
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save_thread(thread: dict) -> None:
    import json
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    p = thread_path(thread["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(thread, indent=2, default=str))
    os.replace(tmp, p)


def new_thread_id() -> str:
    import uuid
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]


# --------------------------------------------------------------------------
# Dialogue system prompt + thread rendering
# --------------------------------------------------------------------------

def load_dialogue_system_prompt() -> str:
    base = BASE_PROMPT_PATH.read_text()
    addendum = DIALOGUE_PROMPT_PATH.read_text()
    return base.rstrip() + "\n\n" + addendum.lstrip()


def render_attachments(attach: list[str]) -> str:
    parts: list[str] = []
    for fp in attach or []:
        p = Path(fp)
        try:
            content = p.read_text(errors="replace")
        except OSError as e:
            content = f"(could not read {fp}: {e})"
        parts.append(f"## Attached file: {fp}\n```\n{content}\n```")
    return "\n\n".join(parts)


def format_thread_block(thread: dict, current_claude_msg: str,
                        current_round: int) -> str:
    """Render the dialogue so far for a member's prompt. Goes in the
    round1_block slot, i.e. AFTER 'Proposal under review:'. Round-1 member
    turns get no thread block (they see only the proposal)."""
    members = thread["members"]
    lines = [
        f"## Dialogue thread so far (round-table; members: {', '.join(members)})",
        "",
        ("You are in a multi-turn dialogue. Re-evaluate from scratch each "
         "turn; a prior PASS does not carry forward. Respond per the "
         "dialogue output format."),
        "",
    ]
    for rnd in thread["rounds"]:
        n = rnd["n"]
        if n >= 2 and rnd.get("claude"):
            lines.append(f"### Round {n} - Claude:")
            lines.append(rnd["claude"])
            lines.append("")
        for role in members:
            rec = rnd.get("members", {}).get(role)
            if not rec:
                continue
            tag = "errored" if rec.get("errored") else rec.get("verdict", "?")
            lines.append(f"### Round {n} - {role} (verdict {tag}):")
            lines.append((rec.get("text") or "").strip() or "(no text)")
            lines.append("")
    lines.append(f"### Round {current_round} - Claude:")
    lines.append(current_claude_msg.strip() or "(no message)")
    lines.append("")
    lines.append(f"It is now your turn for Round {current_round}.")
    block = "\n".join(lines)
    b = block.encode("utf-8")
    if len(b) > THREAD_BLOCK_MAX_BYTES:
        head = b[:20_000].decode("utf-8", errors="replace")
        tail = b[-(THREAD_BLOCK_MAX_BYTES - 20_000):].decode("utf-8", errors="replace")
        block = (head
                 + "\n\n[... older dialogue elided for length ...]\n\n"
                 + tail)
    return block


# --------------------------------------------------------------------------
# Running a turn
# --------------------------------------------------------------------------

def build_directives_block(transcript_path: str | None,
                           evidence_file: str | None) -> str:
    """Recent user directives + Claude's own recent messages.

    Mirrors what the one-shot advisor path builds (consult_council.py:
    1043-1051) so a dialogue member sees the same standing instructions a
    PostToolUse member does. Bar item 12 tells members the user's session
    directives are "visible to you in the Recent user directives block";
    without this the dialogue made that promise and did not keep it.
    Returns "" when no transcript is available.
    """
    if not transcript_path:
        return ""
    try:
        ev = Path(evidence_file) if evidence_file else None
        block = cc.format_user_directives(Path(transcript_path), ev)
        assistant = cc.format_assistant_messages(Path(transcript_path))
    except Exception as e:  # noqa: BLE001
        print(f"# note: directives load failed: {e}", file=sys.stderr)
        return ""
    if assistant:
        block = block + "\n\n---\n\n" + assistant if block else assistant
    return block


async def _run_members(active: list[str], sys_prompt: str, proposal: str,
                       evidence_block: str, directives_block: str,
                       thread_block: str) -> list[dict]:
    cwd = Path(tempfile.mkdtemp(prefix="council_member_"))
    try:
        results = await asyncio.gather(*[
            cc.MEMBER_RUNNERS[m](proposal, sys_prompt, cwd,
                                 evidence_block, directives_block,
                                 thread_block)
            for m in active
        ])
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    out: list[dict] = []
    for r in results:
        text = r.get("text") or ""
        errored = r.get("verdict") == "ERROR"
        verdict = "ERROR" if errored else parse_dialogue_verdict(text)
        out.append({
            "role": r["role"],
            "text": text,
            "stderr": r.get("stderr", ""),
            "duration_s": r.get("duration_s"),
            "errored": errored,
            "verdict": verdict,
            "questions": [] if errored else extract_questions(text),
        })
    return out


def live_context(thread: dict) -> tuple[str, str]:
    """Rebuild the evidence and directives blocks from source, every round.

    Previously both were built once in cmd_start and frozen into the thread
    dict, and every round replayed that same stale string. The effect was that
    a member could not see ANY probe Claude ran after the thread opened, no
    matter how many rounds the dialogue went: it would ask for evidence, Claude
    would run the command, and the member would still be looking at the round-1
    snapshot. deepseek hit exactly this during the item-1 goal check and had to
    ask for a test run to be pasted inline.

    So the thread now stores the SOURCE PATHS and we re-read them each round.
    The frozen blocks are kept only as a fallback for threads created before
    this change, which have the paths absent.
    """
    ev_path = thread.get("evidence_file") or ""
    tr_path = thread.get("transcript_path") or ""

    if ev_path:
        try:
            evidence_block = cc.format_evidence_block(Path(ev_path))
        except Exception as e:  # noqa: BLE001
            print(f"# note: evidence re-read failed: {e}", file=sys.stderr)
            evidence_block = thread.get("evidence_block", "")
    else:
        evidence_block = thread.get("evidence_block", "")

    if tr_path:
        directives_block = build_directives_block(tr_path, ev_path or None)
    else:
        directives_block = thread.get("directives_block", "")

    return evidence_block, directives_block


def run_round(thread: dict, claude_msg: str, round_n: int) -> dict:
    sys_prompt = load_dialogue_system_prompt()
    proposal = thread["proposal"]
    evidence_block, directives_block = live_context(thread)
    active = active_roster(thread)
    thread_block = "" if round_n == 1 else format_thread_block(
        thread, claude_msg, round_n)
    recs = asyncio.run(_run_members(active, sys_prompt, proposal,
                                    evidence_block, directives_block,
                                    thread_block))
    return {"n": round_n, "claude": claude_msg,
            "members": {r["role"]: r for r in recs}}


# --------------------------------------------------------------------------
# Roster, standings, convergence, consensus
# --------------------------------------------------------------------------

def active_roster(thread: dict) -> list[str]:
    dropped = set(thread.get("dropped", []))
    return [m for m in thread["members"] if m not in dropped]


def member_standing(thread: dict, role: str) -> dict:
    """Most-recent NON-errored record for a member (carried forward if its
    latest turn errored), plus trailing consec-error count and staleness."""
    recs = [rnd["members"][role] for rnd in thread["rounds"]
            if role in rnd.get("members", {})]
    consec = 0
    for rec in reversed(recs):
        if rec.get("errored"):
            consec += 1
        else:
            break
    good = None
    for rec in reversed(recs):
        if not rec.get("errored"):
            good = rec
            break
    latest = recs[-1] if recs else None
    stale = good is not None and latest is not None and good is not latest
    return {"good": good, "latest": latest, "consec_errors": consec,
            "stale": stale, "rounds_for": [rnd["n"] for rnd in thread["rounds"]
                                           if role in rnd.get("members", {})]}


def effective_state(standing: dict) -> tuple[str, str, str]:
    """Return (terminality, effective_verdict, note) for convergence.
    terminality in {'terminal','pending','dead'}."""
    good = standing["good"]
    if standing["consec_errors"] >= MAX_CONSEC_ERRORS and good is None:
        return ("dead", "ERROR", "errored repeatedly, never produced a verdict")
    if good is None:
        return ("pending", "ERROR", "errored, no prior verdict yet")
    v = good["verdict"]
    qs = good.get("questions", [])
    stale = " (stale, carried from an earlier round)" if standing["stale"] else ""
    if v == "UNPARSEABLE":
        return ("pending", "UNPARSEABLE", "output did not parse to a verdict")
    if v == "DELIBERATING":
        if qs:
            return ("pending", "DELIBERATING", f"{len(qs)} open question(s)" + stale)
        return ("terminal", "WARN",
                "DELIBERATING without a question -> normalized to WARN" + stale)
    # PASS / WARN / BLOCK
    if qs:
        return ("pending", v, f"{v} but {len(qs)} open question(s)" + stale)
    return ("terminal", v, ("verdict held" + stale) if stale else "")


def assess(thread: dict) -> dict:
    """Compute convergence + consensus over the active roster."""
    roster = active_roster(thread)
    per = {}
    newly_dropped = []
    for role in roster:
        st = member_standing(thread, role)
        term, eff, note = effective_state(st)
        if term == "dead":
            newly_dropped.append(role)
        per[role] = {"terminality": term, "verdict": eff, "note": note,
                     "consec_errors": st["consec_errors"],
                     "questions": (st["good"] or {}).get("questions", []),
                     "duration_s": (st["latest"] or {}).get("duration_s")}
    live = [r for r in roster if r not in newly_dropped]
    converged = bool(live) and all(
        per[r]["terminality"] == "terminal" for r in live)
    if converged:
        verdicts = [per[r]["verdict"] for r in live]
        if "BLOCK" in verdicts:
            consensus = "BLOCK"
        elif all(v == "PASS" for v in verdicts):
            consensus = "PASS"
        else:
            consensus = "WARN"
    else:
        consensus = "PENDING"
    return {"per": per, "converged": converged, "consensus": consensus,
            "live": live, "newly_dropped": newly_dropped}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def print_round_responses(rnd: dict, members: list[str]) -> None:
    print(f"===== Round {rnd['n']} member responses =====\n")
    for role in members:
        rec = rnd.get("members", {}).get(role)
        if not rec:
            continue
        dur = rec.get("duration_s")
        durs = f"{dur}s" if dur is not None else "?"
        if rec.get("errored"):
            tail = (rec.get("stderr") or "").strip().splitlines()
            hint = tail[-1][:200] if tail else ""
            print(f"## {role} (ERROR, {durs}) {hint}\n")
            continue
        print(f"## {role} (verdict {rec['verdict']}, {durs})\n")
        print((rec.get("text") or "").strip())
        print()


def print_status(thread: dict, a: dict) -> None:
    rounds = thread["rounds"]
    rn = rounds[-1]["n"] if rounds else 0
    print(f"===== STATUS: thread {thread['id']} | round {rn}/{MAX_TURNS} "
          f"| status={thread['status']} =====")
    for role in thread["members"]:
        if role in thread.get("dropped", []) and role not in a["newly_dropped"]:
            print(f"#   {role:<9} DROPPED")
            continue
        p = a["per"].get(role)
        if not p:
            print(f"#   {role:<9} DROPPED")
            continue
        dur = p.get("duration_s")
        durs = f"{dur}s" if dur is not None else "?"
        q = len(p["questions"])
        qstr = f" q:{q}" if q else ""
        mark = {"terminal": "", "pending": " [pending]",
                "dead": " [DROPPED]"}[p["terminality"]]
        note = f"  - {p['note']}" if p["note"] else ""
        print(f"#   {role:<9} {p['verdict']:<13} ({durs}){qstr}{mark}{note}")
    if a["converged"]:
        verds = ", ".join(f"{r}={a['per'][r]['verdict']}" for r in a["live"])
        print(f"CONVERGENCE: YES")
        print(f"CONSENSUS: {a['consensus']}  ({verds})")
    else:
        pend = [r for r in a["live"] if a["per"][r]["terminality"] != "terminal"]
        print(f"CONVERGENCE: NO  (still pending: {', '.join(pend) or 'none'})")
        print(f"CONSENSUS: PENDING")


def write_final_artifact(thread: dict, a: dict, premature: bool) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = LOGS_ROOT / today
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{thread['id']}-FINAL.md"
    lines = [
        f"# Council dialogue FINAL - {thread['id']}",
        f"Resolved: {now_iso()}",
        f"Rounds: {len(thread['rounds'])}",
        f"Members: {', '.join(thread['members'])}"
        + (f" (dropped: {', '.join(thread['dropped'])})" if thread.get("dropped") else ""),
        f"Premature: {'YES - forced before convergence' if premature else 'no'}",
        f"Consensus: {a['consensus']}",
        "",
        "## Per-member final verdict",
    ]
    for role in thread["members"]:
        p = a["per"].get(role)
        if not p:
            lines.append(f"- {role}: DROPPED")
            continue
        extra = f" - {p['note']}" if p["note"] else ""
        lines.append(f"- {role}: {p['verdict']}{extra}")
        for q in p["questions"]:
            lines.append(f"    (open question) {q}")
    lines.append("")
    lines.append("## Unresolved concerns (verbatim REASONS from final standings)")
    any_concern = False
    for role in thread["members"]:
        st = member_standing(thread, role)
        good = st["good"]
        if not good:
            continue
        if good["verdict"] in ("WARN", "BLOCK") or good.get("questions"):
            any_concern = True
            lines.append(f"### {role} ({good['verdict']})")
            lines.append((good.get("text") or "").strip())
            lines.append("")
    if not any_concern:
        lines.append("(none)")
    lines.append("")
    lines.append(f"Full thread: council_dialogue.py show {thread['id']}")
    path.write_text("\n".join(lines))
    return path


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------

def cmd_start(args) -> int:
    proposal = args.proposal
    attach_block = render_attachments(args.attach)
    if attach_block:
        proposal = proposal + "\n\n" + attach_block
    if not proposal.strip():
        print("ERROR: empty proposal", file=sys.stderr)
        return 2

    members = list(cc.ALL_MEMBERS)
    if "deepseek" in members and not os.environ.get("DEEPSEEK_API_KEY"):
        members = [m for m in members if m != "deepseek"]
        print("# note: deepseek dropped (DEEPSEEK_API_KEY not set)",
              file=sys.stderr)

    evidence_block = ""
    if args.evidence_file:
        try:
            evidence_block = cc.format_evidence_block(Path(args.evidence_file))
        except Exception as e:  # noqa: BLE001
            print(f"# note: evidence load failed: {e}", file=sys.stderr)

    directives_block = build_directives_block(
        getattr(args, "transcript_path", None), args.evidence_file)

    tid = new_thread_id()
    with ThreadLock(tid):
        thread = {
            "id": tid,
            "created": now_iso(),
            "status": "open",
            "members": members,
            "dropped": [],
            "proposal": proposal,
            # Source paths, so every round can re-read them and see probes
            # Claude ran mid-dialogue. The rendered blocks below are kept as
            # the round-1 snapshot and as a fallback for threads created
            # before live_context existed.
            "evidence_file": args.evidence_file or "",
            "transcript_path": getattr(args, "transcript_path", None) or "",
            "evidence_block": evidence_block,
            "directives_block": directives_block,
            "rounds": [],
        }
        rnd = run_round(thread, proposal, 1)
        thread["rounds"].append(rnd)
        a = assess(thread)
        if a["newly_dropped"]:
            thread["dropped"] = sorted(set(thread.get("dropped", []))
                                       | set(a["newly_dropped"]))
        save_thread(thread)

    print(f"# thread started: {tid}")
    print(f"# members: {', '.join(members)}\n")
    print_round_responses(rnd, members)
    print_status(thread, a)
    print(f"\n# next: council_dialogue.py say {tid} \"<your reply / evidence>\""
          f"   (or resolve {tid} when converged)")
    return 0


def cmd_say(args) -> int:
    tid = args.thread_id
    msg = args.message
    attach_block = render_attachments(args.attach)
    if attach_block:
        msg = msg + "\n\n" + attach_block
    with ThreadLock(tid):
        thread = load_thread(tid)
        if thread is None:
            print(f"ERROR: no such thread: {tid}", file=sys.stderr)
            return 2
        if thread["status"] == "resolved" and not args.reopen:
            print(f"ERROR: thread {tid} is resolved. Use --reopen to continue "
                  f"(this voids the prior FINAL artifact).", file=sys.stderr)
            return 2
        if thread["status"] == "resolved" and args.reopen:
            thread["status"] = "open"
        if not msg.strip():
            print("ERROR: empty message", file=sys.stderr)
            return 2
        last_n = thread["rounds"][-1]["n"] if thread["rounds"] else 0
        round_n = last_n + 1
        if round_n > MAX_TURNS:
            print(f"ERROR: turn cap reached ({MAX_TURNS} rounds). The council "
                  f"has not converged. Run: council_dialogue.py resolve {tid} "
                  f"--force   to force final verdicts.", file=sys.stderr)
            return 2
        rnd = run_round(thread, msg, round_n)
        thread["rounds"].append(rnd)
        a = assess(thread)
        if a["newly_dropped"]:
            thread["dropped"] = sorted(set(thread.get("dropped", []))
                                       | set(a["newly_dropped"]))
        save_thread(thread)

    print_round_responses(rnd, thread["members"])
    print_status(thread, a)
    if a["converged"]:
        print(f"\n# converged. Run: council_dialogue.py resolve {tid}  "
              f"to finalize and write the consensus artifact.")
    else:
        print(f"\n# next: answer the open questions, then "
              f"council_dialogue.py say {tid} \"...\"")
    return 0


def cmd_resolve(args) -> int:
    tid = args.thread_id
    with ThreadLock(tid):
        thread = load_thread(tid)
        if thread is None:
            print(f"ERROR: no such thread: {tid}", file=sys.stderr)
            return 2
        a = assess(thread)
        premature = not a["converged"]
        if premature and not args.force:
            pend = [r for r in a["live"]
                    if a["per"][r]["terminality"] != "terminal"]
            print(f"ERROR: thread {tid} has NOT converged "
                  f"(pending: {', '.join(pend)}). Answer their open questions "
                  f"with `say`, or re-run `resolve {tid} --force` to force "
                  f"final verdicts (non-terminal members coerced to WARN).",
                  file=sys.stderr)
            print_status(thread, a)
            return 1
        # Coerce non-terminal members to WARN for the final consensus.
        if premature:
            for role in a["live"]:
                if a["per"][role]["terminality"] != "terminal":
                    a["per"][role]["verdict"] = "WARN"
                    a["per"][role]["note"] = (
                        "coerced to WARN (forced resolve before convergence; "
                        + a["per"][role]["note"] + ")")
            verds = [a["per"][r]["verdict"] for r in a["live"]]
            a["consensus"] = ("BLOCK" if "BLOCK" in verds
                              else "PASS" if all(v == "PASS" for v in verds)
                              else "WARN")
            a["converged"] = True
        thread["status"] = "resolved"
        save_thread(thread)
        artifact = write_final_artifact(thread, a, premature)

    print(f"===== COUNCIL DIALOGUE RESOLVED: {tid} =====")
    if premature:
        print("# PREMATURE RESOLVE: forced before convergence; non-terminal "
              "members were coerced to WARN.")
    print(f"CONSENSUS: {a['consensus']}")
    for role in thread["members"]:
        p = a["per"].get(role)
        if not p:
            print(f"#   {role:<9} DROPPED")
            continue
        extra = f"  - {p['note']}" if p["note"] else ""
        print(f"#   {role:<9} {p['verdict']}{extra}")
    print(f"\n# FINAL artifact (surface this to the user): {artifact}")
    print(f"# raw thread: council_dialogue.py show {tid}")
    return 0


def cmd_show(args) -> int:
    tid = args.thread_id
    thread = load_thread(tid)
    if thread is None:
        print(f"ERROR: no such thread: {tid}", file=sys.stderr)
        return 2
    print(f"===== THREAD {tid} | status={thread['status']} | "
          f"members={', '.join(thread['members'])}"
          + (f" | dropped={', '.join(thread['dropped'])}" if thread.get("dropped") else "")
          + " =====\n")
    print("### PROPOSAL:")
    print(thread["proposal"].strip())
    print()
    for rnd in thread["rounds"]:
        n = rnd["n"]
        if n >= 2 and rnd.get("claude"):
            print(f"### Round {n} - Claude:")
            print(rnd["claude"].strip())
            print()
        for role in thread["members"]:
            rec = rnd.get("members", {}).get(role)
            if not rec:
                continue
            tag = "ERROR" if rec.get("errored") else rec.get("verdict")
            dur = rec.get("duration_s")
            print(f"### Round {n} - {role} ({tag}, {dur}s):")
            print((rec.get("text") or "").strip() or "(no text)")
            print()
    a = assess(thread)
    print_status(thread, a)
    return 0


def cmd_escalate(args) -> int:
    """Open a dialogue thread seeded from a one-shot council log (e.g. a
    PostToolUse WARN/BLOCK exchange). The logged members' critiques become
    round 1, so Claude can respond with `say` without re-running the
    members. Claude-initiated: nothing creates a thread automatically;
    Claude runs this when it wants to discuss a council verdict."""
    import json
    log_path = Path(args.log_path)
    if not log_path.exists():
        print(f"ERROR: no such log file: {log_path}", file=sys.stderr)
        return 2
    try:
        log = json.loads(log_path.read_text())
    except (OSError, ValueError) as e:
        print(f"ERROR: could not read log {log_path}: {e}", file=sys.stderr)
        return 2

    proposal = log.get("pitch", "")
    if not proposal.strip():
        print(f"ERROR: log has no pitch to escalate: {log_path}", file=sys.stderr)
        return 2
    log_members = log.get("members", [])
    members: list[str] = []
    for m in log_members:
        r = m.get("role")
        if r and r not in members:
            members.append(r)
    if not members:
        print(f"ERROR: log has no members: {log_path}", file=sys.stderr)
        return 2

    evidence_block = ""
    if args.evidence_file:
        try:
            evidence_block = cc.format_evidence_block(Path(args.evidence_file))
        except Exception as e:  # noqa: BLE001
            print(f"# note: evidence load failed: {e}", file=sys.stderr)

    # Seed round 1 from the logged one-shot critiques. Re-parse each
    # verdict with the dialogue-aware parser; one-shot critiques carry no
    # QUESTIONS section, so questions is normally [].
    seeded: dict[str, dict] = {}
    for m in log_members:
        role = m.get("role")
        if not role or role not in members or role in seeded:
            continue
        text = m.get("text") or ""
        errored = m.get("verdict") == "ERROR"
        seeded[role] = {
            "role": role,
            "text": text,
            "stderr": m.get("stderr", ""),
            "duration_s": m.get("duration_s"),
            "errored": errored,
            "verdict": "ERROR" if errored else parse_dialogue_verdict(text),
            "questions": [] if errored else extract_questions(text),
        }

    directives_block = build_directives_block(
        getattr(args, "transcript_path", None), args.evidence_file)

    tid = new_thread_id()
    with ThreadLock(tid):
        thread = {
            "id": tid,
            "created": now_iso(),
            "status": "open",
            "members": members,
            "dropped": [],
            "proposal": proposal,
            # Source paths, so every round can re-read them and see probes
            # Claude ran mid-dialogue. The rendered blocks below are kept as
            # the round-1 snapshot and as a fallback for threads created
            # before live_context existed.
            "evidence_file": args.evidence_file or "",
            "transcript_path": getattr(args, "transcript_path", None) or "",
            "evidence_block": evidence_block,
            "directives_block": directives_block,
            "rounds": [{"n": 1, "claude": proposal, "members": seeded}],
            "escalated_from": str(log_path),
        }
        a = assess(thread)
        if a["newly_dropped"]:
            thread["dropped"] = sorted(set(thread.get("dropped", []))
                                       | set(a["newly_dropped"]))
        save_thread(thread)

    print(f"# dialogue escalated from log: {log_path}")
    print(f"# thread: {tid}")
    print(f"# members: {', '.join(members)} (round 1 seeded from the logged "
          f"one-shot critiques; not re-run)\n")
    print_round_responses(thread["rounds"][0], members)
    print_status(thread, a)
    print(f"\n# next: respond to the council's concerns with\n"
          f"#   council_dialogue.py say {tid} \"<your reply / evidence>\"\n"
          f"# then resolve {tid} once converged.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Workers' Council - dialogue mode")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="start a new dialogue thread")
    p_start.add_argument("proposal")
    p_start.add_argument("--evidence-file")
    p_start.add_argument("--transcript-path",
                         help="session transcript JSONL; supplies the "
                              "Recent user directives block (bar item 12)")
    p_start.add_argument("--attach", action="append", default=[])
    p_start.set_defaults(func=cmd_start)

    p_say = sub.add_parser("say", help="add Claude's reply and run a round")
    p_say.add_argument("thread_id")
    p_say.add_argument("message")
    p_say.add_argument("--attach", action="append", default=[])
    p_say.add_argument("--reopen", action="store_true")
    p_say.set_defaults(func=cmd_say)

    p_res = sub.add_parser("resolve", help="finalize and write consensus")
    p_res.add_argument("thread_id")
    p_res.add_argument("--force", action="store_true")
    p_res.set_defaults(func=cmd_resolve)

    p_show = sub.add_parser("show", help="print the full thread")
    p_show.add_argument("thread_id")
    p_show.set_defaults(func=cmd_show)

    p_esc = sub.add_parser("escalate",
                           help="open a dialogue seeded from a council log")
    p_esc.add_argument("log_path")
    p_esc.add_argument("--evidence-file")
    p_esc.add_argument("--transcript-path",
                       help="session transcript JSONL; supplies the "
                            "Recent user directives block (bar item 12)")
    p_esc.set_defaults(func=cmd_escalate)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
