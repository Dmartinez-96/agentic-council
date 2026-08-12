#!/usr/bin/env python3
"""Stop-hook council audit for tagged outward-facing prose drafts.

Fires when Claude finishes a turn. Scans the most recent assistant
message in the session transcript for a tagged block:

    **[council-audit-begin]**

    ... draft content ...

    **[council-audit-end]**

If a tagged block is found, this script:
- Writes the extracted content to /tmp/council_outward_drafts/<file>.md
- Invokes consult_council.py with --layer stop_prose and (if a
  per-session evidence file exists) --evidence-file pointing at it
- On PASS: exits 0 silently
- On WARN: exits 2, with the wrapper output written to stderr

Exit-code semantics: The local plugin-dev hook-development SKILL.md
documents hook exit code 0 as "Success (stdout shown in transcript)"
and exit code 2 as "Blocking error (stderr fed back to Claude)". This
script relies on the exit-2 semantics to surface WARN bodies to
Claude on the next turn.

If no tagged block is found, the script exits 0 silently. The
audit-invoking branch only runs when at least one tagged block is
present in the most recent assistant message; otherwise the script
falls through to a no-op exit.

Loop prevention: the script reads an optional `stop_hook_active`
boolean from the payload and exits 0 immediately if it is true. The
field is NOT documented in the hook-development SKILL.md (which describes
Stop hook inputs as carrying a `reason` field). The defensive
.get() pattern means the check no-ops harmlessly if Claude Code does
not emit `stop_hook_active` in this build, so the script behaves
correctly whether or not the field is present.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

COUNCIL_ROOT = Path(__file__).resolve().parent
WRAPPER = COUNCIL_ROOT / "consult_council.py"
EVIDENCE_STATE_ROOT = Path.home() / ".claude" / "state"
DRAFT_TEMP_ROOT = Path("/tmp/council_outward_drafts")

BEGIN_MARKER = "**[council-audit-begin]**"
END_MARKER = "**[council-audit-end]**"

BLOCK_RE = re.compile(
    re.escape(BEGIN_MARKER) + r"(.*?)" + re.escape(END_MARKER),
    re.DOTALL,
)

# Rule-11 (caveat-phrases-require-probe) trigger list. Mirrors the
# TRIGGERS table in laziness_gate.py but is duplicated here so this
# script does not need to import a sibling hook script. Keep both in
# sync when adding new triggers.
LAZINESS_TRIGGERS: list[tuple[str, str, list[str]]] = [
    (r"\bGPU\s+(required|needed|necessary)\b",
     "GPU required without probe", ["nvidia-smi"]),
    (r"\b(fetch|download)\s+(too|is too)\s+large\b",
     "fetch-too-large without probe",
     ["curl", "wget", "WebFetch"]),
    (r"\bcompute\s+(would\s+be\s+)?required\b",
     "compute-required without probe",
     ["python", "python3", "uv ", "pip ", "cuda", "venv"]),
    (r"\bnot\s+feasible\b",
     "not-feasible without probe",
     ["python", "python3", "uv ", "pip ", "Bash"]),
    (r"\bnot\s+run\s+end[- ]to[- ]end\b",
     "not-run-end-to-end without probe",
     ["python", "python3", "uv ", "pip "]),
    (r"\bsmoke[- ]tested\s+by\s+reading\b",
     "smoke-tested-by-reading without an actual test",
     ["python", "python3", "pytest", "uv ", "pip "]),
    (r"\bout\s+of\s+scope\b",
     "out-of-scope without spec read", []),
    (r"\bwould\s+(need\s+to\s+run|require)\b",
     "would-need-to-run without probe",
     ["python", "python3", "uv ", "pip ", "Bash"]),
]
EVIDENCE_SCAN_LAST_N_EVENTS = 250


def last_assistant_text(transcript_path: Path) -> str:
    """Return the concatenated text-content of the most recent assistant
    message in the transcript, or empty string on any failure."""
    if not transcript_path.exists():
        return ""
    try:
        lines = transcript_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return ""

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if isinstance(text, str):
                    text_parts.append(text)
        if text_parts:
            return "\n".join(text_parts)
        return ""

    return ""


def extract_tagged_blocks(text: str) -> list[str]:
    """Return all tagged-block contents from the given text, in order."""
    if not text:
        return []
    matches = BLOCK_RE.findall(text)
    return [m.strip() for m in matches if m.strip()]


def evidence_file_for(session_id: str) -> Path | None:
    if not session_id:
        return None
    candidate = EVIDENCE_STATE_ROOT / session_id / "evidence.jsonl"
    return candidate if candidate.exists() else None


def collect_evidence_commands(session_id: str) -> str:
    """Return concatenated recent Bash commands and tool descriptors
    from the per-session evidence file, for keyword-based probe
    matching. Mirrors the analogous helper in laziness_gate.py.
    """
    if not session_id:
        return ""
    p = EVIDENCE_STATE_ROOT / session_id / "evidence.jsonl"
    if not p.exists():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    recent = lines[-EVIDENCE_SCAN_LAST_N_EVENTS:]
    parts: list[str] = []
    for line in recent:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        tool = ev.get("tool", "")
        parts.append(tool)
        if tool == "Bash":
            parts.append(ev.get("command", ""))
        elif tool == "WebFetch":
            parts.append(ev.get("url", ""))
        elif tool == "WebSearch":
            parts.append(ev.get("query", ""))
    return " || ".join(parts)


def detect_laziness_triggers(text: str, evidence_commands: str) -> list[str]:
    """Scan text for rule-11 trigger phrases without a matching probe
    in evidence. Returns a list of human-readable labels for the
    unbacked triggers. Empty list = no concerns.
    """
    if not text:
        return []
    unbacked: list[str] = []
    for pattern, label, probes in LAZINESS_TRIGGERS:
        if not re.search(pattern, text, re.IGNORECASE):
            continue
        if not probes:
            unbacked.append(label)
            continue
        if not any(p.lower() in evidence_commands.lower() for p in probes):
            unbacked.append(label)
    return unbacked


def write_draft_tempfile(session_id: str, content: str) -> Path:
    DRAFT_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{session_id or 'nosess'}-{ts}-{uuid.uuid4().hex[:8]}.md"
    path = DRAFT_TEMP_ROOT / name
    path.write_text(content, encoding="utf-8")
    return path


def _stop_heartbeat_quietly(handle, probe_path: Path | None = None) -> None:
    """Stop a probe heartbeat and RECORD THAT IT STOPPED CLEANLY. Swallows everything.

    Its own function rather than an inline call because the handle is produced inside a
    try block whose import may have failed, so the module alias is not reliably bound at
    the call sites. Re-importing here is cheap (sys.modules hit) and cannot raise past
    this frame. An instrument that can break the Stop hook is worse than no instrument:
    this hook runs at the end of every turn.

    WHY THE TERMINAL RECORD EXISTS, found by running the probe rather than by reading it:
    without it the sidecar CANNOT ANSWER THE QUESTION IT WAS BUILT FOR. A Stop hook that
    finishes normally simply stops beating, and so does one that is killed -- both leave a
    file whose last beat is merely old. council_advisor does not have this problem because
    deleting its marker is the success signal; the probe writes no marker, so it needs an
    explicit one. A sidecar whose final line has `"end": "clean"` SURVIVED; a sidecar
    without one was killed mid-audit, and its last beat dates the kill.

    The record is appended AFTER the join, so it cannot interleave with a live beat."""
    if handle is None:
        return
    try:
        import council_advisor as _ca_hb
        _ca_hb.stop_heartbeat(handle)
    except Exception:  # noqa: BLE001
        pass
    if probe_path is None:
        return
    try:
        beats = Path(str(probe_path) + ".beats")
        if beats.exists():
            with open(beats, "a") as fh:
                fh.write(json.dumps({
                    "t": datetime.now(timezone.utc).isoformat(),
                    "end": "clean",
                }) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
    except Exception:  # noqa: BLE001
        pass


def audit_one_block(content: str, session_id: str, cwd: str) -> tuple[int, str, str]:
    """Run the council wrapper on a single tagged block.

    Returns (returncode, stdout, stderr). Caller decides how to surface.
    """
    draft_path = write_draft_tempfile(session_id, content)
    cmd = [
        sys.executable, str(WRAPPER),
        "--layer", "stop_prose",
        "--tool-name", "StopProse",
        "--target-path", str(draft_path),
    ]
    ev = evidence_file_for(session_id)
    if ev is not None:
        cmd.extend(["--evidence-file", str(ev)])
    pitch = (
        f"Tool: StopProse\n"
        f"Target: {draft_path}\n\n"
        f"--- Tagged outward-prose block begin ---\n"
        f"{content}\n"
        f"--- Tagged outward-prose block end ---\n"
    )
    # DOES THE STOP HOOK HAVE A KILL DEADLINE OF ITS OWN? PostToolUse fires were measured
    # dying in a 0.2s window near 596s elapsed, and the natural fix for that -- letting the
    # Stop hook wait at end of turn for reviews still in flight -- is only safe if the Stop
    # hook is not killed the same way. THAT IS UNKNOWN, so measure it before designing
    # around it rather than assuming either answer.
    #
    # Same heartbeat mechanism as council_advisor, deliberately: a sidecar whose last beat
    # dates the death. A SEPARATE DIRECTORY, `stop-probe/`, NOT `pending-review/`, because
    # orphan_markers() globs `*.json` there and loss_analysis globs `*.beats` there -- a
    # probe left in that directory would be reported as a lost REVIEW, which it is not, and
    # would contaminate the death table this probe exists to extend.
    #
    # NO MARKER FILE IS WRITTEN, only beats. There is nothing here to reconcile: the audit's
    # outcome already reaches the agent through this function's return value.
    beat = None
    probe_path = None
    events_file = None
    events_fd = None
    # ONE IMPORT FOR THE WHOLE FUNCTION, bound before anything needs it. The heartbeat, the
    # cap and the salvage all live in council_advisor, and three separate lazy imports would
    # each need their own "what if it failed" branch -- and a name bound only inside a `try`
    # is a NameError waiting for the path that skipped it.
    ca_mod = None
    try:
        import council_advisor as ca_mod
    except Exception:  # noqa: BLE001  the audit must run with or without the advisor module
        ca_mod = None
    # THE SAME CAP AS THE PostToolUse ADVISOR, taken from it rather than repeated. Both hooks
    # run the same engine against the same per-member limits, and this one previously carried
    # its own literal 900 in two places -- the argument and the message a reader sees -- so a
    # change to one could leave the other quoting a number that was no longer true.
    cap = getattr(ca_mod, "FIRE_TIMEOUT_S", 1500)
    try:
        probe_dir = (ca_mod.EVIDENCE_STATE_ROOT
                     / (session_id or "_no_session") / "stop-probe")
        probe_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        probe_path = probe_dir / f"{stamp}.stopprobe"
        beat = ca_mod.start_heartbeat(probe_path)
        # THE EVENTS SIDECAR SITS BESIDE THE PROBE, in stop-probe/ and not pending-review/,
        # for exactly the reason the probe does: nothing in this directory is a review marker,
        # so nothing here can be mistaken for a lost review or counted as a fire in flight.
        events_file = Path(str(probe_path) + ".events")
        events_fd = os.open(events_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        cmd.extend(["--events-fd", str(events_fd)])
    except Exception:  # noqa: BLE001  instrumentation must never break the hook
        beat = None
        probe_path = None
        if events_fd is not None:
            try:
                os.close(events_fd)
            except OSError:
                pass
        events_fd = None
        events_file = None
    # AN EXPLICIT FLAG, NOT sys.exc_info(), decides whether the sidecar is evidence. The first
    # version of the cleanup below asked `isinstance(sys.exc_info()[1], TimeoutExpired)`, which
    # MEASURES AS None: once an `except` block completes -- including by returning -- the
    # exception is no longer current, so the check read "this was not a timeout" on exactly the
    # path that was one, and would have deleted the only surviving record of the seats that
    # reported. Probed directly before this was rewritten.
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            input=pitch,
            text=True,
            capture_output=True,
            cwd=cwd or ".",
            timeout=cap,
            pass_fds=(events_fd,) if events_fd is not None else (),
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_heartbeat_quietly(beat, probe_path)
        # SALVAGE, for the same reason the advisor does it: the engine writes its log entry
        # only at the end, so a killed fire loses every round that HAD finished. Here the
        # loss is worse in one respect -- this hook's whole job is to check outward prose
        # before a turn ends, and "the audit did not complete" gives the reader nothing to
        # act on, while the seats that did report gave concrete findings.
        salvaged = ""
        if events_fd is not None:
            try:
                os.close(events_fd)
            except OSError:
                pass
            events_fd = None
        if events_file is not None and ca_mod is not None:
            try:
                summary = ca_mod.summarise_partial(ca_mod.read_events(events_file))
                if summary["seats"]:
                    salvaged = ca_mod.format_partial(summary, "StopProse",
                                                     str(draft_path), cap)
                    ca_mod.record_partial(summary, "StopProse", str(draft_path),
                                          session_id, cap)
            except Exception:  # noqa: BLE001  salvage must never break the hook
                salvaged = ""
        if salvaged:
            return 2, "", (
                f"stop-audit: council timed out (>{cap}s) auditing draft at {draft_path}, "
                f"but the seats that finished were recovered:\n\n{salvaged}"
            )
        return 2, "", (
            f"stop-audit: council timed out (>{cap}s) auditing draft "
            f"at {draft_path}. Stopping anyway, but the audit did not "
            f"complete, and NO completed seat-round could be recovered -- which is "
            f"silence about this prose, not approval of it."
        )
    finally:
        if events_fd is not None:
            try:
                os.close(events_fd)
            except OSError:
                pass
        # THE SIDECAR IS KEPT ONLY WHEN IT IS EVIDENCE. A completed audit reports through
        # this function's return value, so its sidecar is litter; a killed one holds the only
        # record of what the seats had said, and deleting that would throw away the thing the
        # salvage path exists to preserve.
        if events_file is not None and not timed_out:
            try:
                events_file.unlink()
            except OSError:
                pass
    _stop_heartbeat_quietly(beat)
    return proc.returncode, proc.stdout, proc.stderr


def emit_block_decision(text: str) -> int:
    """Exit 2 with the given text on stderr.

    Per Claude Code Stop-hook semantics, exit 2 + stderr keeps the
    conversation alive and surfaces the message to Claude.
    """
    sys.stderr.write(text)
    if not text.endswith("\n"):
        sys.stderr.write("\n")
    return 2


def main() -> int:
    # Kill switch: `touch <council dir>/DISABLED` silences this hook;
    # `rm` re-enables it. Checked per call, so it works mid-session.
    if (COUNCIL_ROOT / "DISABLED").exists():
        return 0

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"stop-audit: invalid JSON on stdin: {e}", file=sys.stderr)
        return 0

    if payload.get("stop_hook_active"):
        return 0

    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    cwd = payload.get("cwd", "")

    # LOST REVIEWS -- checked BEFORE the transcript guard below, deliberately. A turn with
    # no transcript path still made edits, and a review that never finished is the one
    # finding that must not be suppressed by an unrelated early return. The advisor
    # reports orphans only on its next PASSING fire; a turn whose remaining fires all WARN
    # would otherwise never surface them, so this is the catch-all.
    # IMPORTED, NOT REIMPLEMENTED: a second copy of this rule would drift from the first,
    # which is a failure this codebase has already paid for twice.
    lost_notice = ""
    lost_orphans: list = []
    try:
        import council_advisor as _ca
        lost_orphans = _ca.orphan_markers(session_id)
        if lost_orphans:
            lost_notice = _ca.format_orphan_notice(lost_orphans)
    except Exception:  # noqa: BLE001
        # Never let the instrument break the hook it instruments.
        lost_notice = ""
        lost_orphans = []

    def _retire_lost() -> None:
        """Archive the orphans -- ONLY on a path that actually surfaces their notice.

        ORDER IS LOAD-BEARING AND A FIRST DRAFT GOT IT WRONG: archiving at detection time
        retires the marker even when the notice is then dropped, and since archiving is
        what removes it from `orphan_markers`, neither this hook nor the advisor's
        next-PASS path could ever report it again. That turns a loud loss into a
        permanent silent one -- strictly worse than not instrumenting at all.
        """
        try:
            import council_advisor as _ca2
            for _o in lost_orphans:
                _ca2.archive_pending_marker(Path(_o["marker_path"]))
        except Exception:  # noqa: BLE001
            pass

    if not transcript_path:
        # Still report a loss: it does not depend on the transcript in any way.
        if lost_notice:
            _retire_lost()
            return emit_block_decision(lost_notice)
        return 0

    text = last_assistant_text(Path(transcript_path))
    blocks = extract_tagged_blocks(text)

    # Always scan the full assistant message for rule-11 trigger
    # phrases regardless of whether tagged-block markers are present.
    evidence_commands = collect_evidence_commands(session_id)
    laziness_hits = detect_laziness_triggers(text, evidence_commands)

    aggregated_warn_messages: list[str] = []
    any_warn = False
    for i, block in enumerate(blocks, start=1):
        rc, stdout, stderr = audit_one_block(block, session_id, cwd)
        if rc == 0:
            continue
        any_warn = True
        body_parts = []
        body_parts.append(
            f"## Block {i} of {len(blocks)} (tagged outward prose)"
        )
        if stdout:
            body_parts.append(stdout.rstrip("\n"))
        if stderr:
            body_parts.append(stderr.rstrip("\n"))
        aggregated_warn_messages.append("\n\n".join(body_parts))

    if laziness_hits:
        body_parts = ["## Rule-11 (laziness) triggers in last message"]
        body_parts.append(
            "Caveat phrases were detected in your last assistant "
            "message without a matching probe in the session "
            "evidence. The disciplined response is to revert or "
            "rewrite the response to remove the unbacked caveat, OR "
            "to run the required probe and re-state the claim with "
            "the probe in evidence."
        )
        body_parts.append("Triggers detected without backing probe:")
        for label in laziness_hits:
            body_parts.append(f"  - {label}")
        aggregated_warn_messages.append("\n\n".join(body_parts))
        any_warn = True

    # The catch-all, on the MAIN path. Without these two lines the notice computed above
    # is discarded on every turn that has a transcript -- which is every normal turn.
    if lost_notice:
        aggregated_warn_messages.append(lost_notice)
        any_warn = True

    if not any_warn:
        return 0

    _retire_lost()
    surface_text = (
        "Stop-hook council audit found concerns in your last "
        "message. Address each finding with verification or revision "
        "before stopping. The findings are below.\n\n"
        + "\n\n---\n\n".join(aggregated_warn_messages)
    )
    return emit_block_decision(surface_text)


if __name__ == "__main__":
    sys.exit(main())
