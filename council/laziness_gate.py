#!/usr/bin/env python3
"""PreToolUse laziness gate.

Fires before Write / Edit / NotebookEdit. Scans the proposed content for council
rule-11 trigger phrases ("out of scope", "GPU required", "compute required", "fetch
too large", "not feasible", "smoke-tested by reading", "would require", "not run
end-to-end"). For each trigger with a probe-marker list, it checks the per-session
evidence file at ~/.claude/state/<session_id>/evidence.jsonl -- ONLY the last
EVIDENCE_RECENCY_EVENTS events, so a stale probe from an unrelated earlier task does
NOT back a fresh caveat (relevance fix) -- for a matching Bash command. If a trigger is
present and no recent matching probe is found, the write is denied via the PreToolUse
permissionDecision output schema documented in the plugin-dev hook-development SKILL.md
(a PreToolUse hook denies by returning JSON with hookSpecificOutput.permissionDecision
set to "deny").

BLIND-CASE POLICY (fail-open, the user sign-off 2026-07-22). If the gate cannot READ the
evidence at all -- no session_id in the payload, no evidence file, or a read error -- it
cannot AFFIRM that a caveat is unbacked ("unbacked" requires having looked and not found,
not "could not look"). It therefore ALLOWS the write and prints an observable note naming
the reason, instead of denying with the false claim that no probe was found. Enforcement
is not lost so much as SOFTENED: the write still reaches the PostToolUse council and the
Stop-hook prose audit, which are instructed by rule 11 to flag an unbacked caveat -- an
advisory / quorum review after the write lands, not a pre-write deny. (That downstream
behaviour under blindness is the design's expectation, not a guarantee this gate can
enforce.) Fail-closed here would permanently block any non-exempt write containing a
trigger phrase, in any environment that cannot supply a session_id.

Some triggers carry an EMPTY probe list ("out of scope", "not feasible"): no command
clears them, so the ONLY way past the gate is to REWRITE the content -- remove the bare
hedge or replace it with a concrete reference (a spec quote, or the specific failed run).
"would require" keeps recency-gated compute markers but no longer clears on a bare shell
command.

Why this is a hard gate rather than a council WARN: rule 11 violations waste the user's
API spend, time, and trust. The council catches them at PostToolUse, but by then the lazy
write has already landed and reverting is on me. PreToolUse refuses the write before it
happens, synchronously, without an LLM call: pure regex + a recent-evidence lookup, in
milliseconds.

Conservative design: for a trigger the gate CAN evaluate, it is better to over-block
(Claude is forced to run the probe first) than under-block. The fail-open path above is
NOT a weakening of that -- it applies only when the gate is blind and cannot evaluate a
trigger at all.

Output schema (Claude Code PreToolUse hook):
- Exit 0 with NO stdout = allow.
- Exit 0 with stdout JSON `{"hookSpecificOutput": {"permissionDecision":
  "deny", ...}, "systemMessage": "..."}` = deny with explanation.

The PreToolUse output schema and hook exit-code semantics are documented in the plugin-dev
hook-development SKILL.md (which details the JSON payload format for
hookSpecificOutput/permissionDecision and specifies that exit code 2 blocks execution,
routing stderr back to the assistant).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

STATE_ROOT = Path.home() / ".claude" / "state"
# Relevance window (Fix 2a, 2026-07-22): only the last N evidence events are scanned for a
# backing probe, so a stale probe from an unrelated earlier task does not clear a fresh
# caveat. 30 accommodates the natural probe->write loop (a probe and the caveat that cites
# it are usually a few turns apart) while purging commands from tasks run long before.
# Tunable.
EVIDENCE_RECENCY_EVENTS = 30


TRIGGERS: list[tuple[str, str, list[str]]] = [
    # (regex_pattern, human_label, probe_markers)
    # probe_markers: any of these substrings in the `command` field of a recent Bash
    # event (or a recent tool name) satisfies the probe requirement; an EMPTY list means
    # there is no command that cleanly backs the caveat, so it is always denied when
    # present -- the ONLY way past it is to REWRITE the content so the trigger phrase is
    # gone (a spec read or a concrete failed run is context for HOW to rewrite, not a
    # clearance path).
    (
        r"\bGPU\s+(required|needed|necessary)\b",
        "GPU required without probe",
        ["nvidia-smi"],
    ),
    (
        r"\b(fetch|download)\s+(too|is too)\s+large\b",
        "fetch-too-large without probe",
        ["curl", "wget", "WebFetch"],
    ),
    (
        r"\bcompute\s+(would\s+be\s+)?required\b",
        "compute-required without a recent probe",
        ["python", "python3", "uv ", "pip ", "cuda", "venv"],
    ),
    (
        # NARROWED 2026-07-22 (the user "split" decision): a vague feasibility hedge is not
        # cleanly backed by any bare command, so this is always-deny like "out of scope".
        r"\bnot\s+feasible\b",
        "not-feasible (always-deny: only removing/rephrasing the phrase passes the gate)",
        [],
    ),
    (
        r"\bnot\s+run\s+end[- ]to[- ]end\b",
        "not-run-end-to-end without a recent probe",
        ["python", "python3", "uv ", "pip "],
    ),
    (
        r"\bsmoke[- ]tested\s+by\s+reading\b",
        "smoke-tested-by-reading without an actual recent test",
        ["python", "python3", "pytest", "uv ", "pip "],
    ),
    (
        r"\bout\s+of\s+scope\b",
        "out-of-scope (always-deny: only removing/rephrasing the phrase passes the gate)",
        [],
    ),
    (
        # NARROWED 2026-07-22 (the user "split" decision): dropped the bare "Bash" marker
        # (it matched ANY shell command); recency-gated so only a RECENT compute probe
        # clears it. "would require" is common prose, so it is NOT made always-deny.
        r"\bwould\s+(need\s+to\s+run|require)\b",
        "would-need-to-run / would-require without a recent probe",
        ["python", "python3", "uv ", "pip "],
    ),
]


def collect_evidence_commands(session_id: str) -> tuple[str, str | None]:
    """Return (recent_commands, blind_reason).

    blind_reason is None when the evidence file was READ (recent_commands may still be ""
    if it held no relevant events); it is a human-readable reason string in the three
    BLIND cases where the gate cannot read the evidence at all: no session_id, no evidence
    file, or a read error. Only the last EVIDENCE_RECENCY_EVENTS events are scanned, so a
    stale probe from an unrelated earlier task does not back a fresh caveat.
    """
    if not session_id:
        return "", "no session_id in the hook payload"
    p = STATE_ROOT / session_id / "evidence.jsonl"
    if not p.exists():
        return "", f"no evidence file at {p}"
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return "", f"evidence file unreadable ({e.__class__.__name__})"
    recent = lines[-EVIDENCE_RECENCY_EVENTS:]
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
    return " || ".join(parts), None


def proposed_content_from_tool_input(tool_name: str, tool_input: dict) -> str:
    """Extract the textual proposed content from the tool input."""
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        # Scan only new_string -- the text being WRITTEN. Scanning old_string (the text
        # being REMOVED) would deny an Edit that DELETES an always-deny caveat like "out
        # of scope" / "not feasible", blocking the gate's own prescribed remedy (rewrite
        # to remove the hedge). Caught by the council 2026-07-22.
        return tool_input.get("new_string", "") or ""
    if tool_name == "NotebookEdit":
        return json.dumps(tool_input, default=str)
    return ""


def emit_deny(reasons: list[str], required_probes: list[str]) -> int:
    """Print PreToolUse deny output and exit 0 (deny is signaled via
    the structured stdout JSON, not via exit code)."""
    body_lines = [
        "COUNCIL PreToolUse LAZINESS GATE: write denied.",
        "",
        "Council rule 11 (caveat-phrases-require-probe) triggers were "
        "detected in your proposed write/edit content, and the session's "
        "RECENT evidence does not contain a matching probe for at "
        "least one of them. The denial is enforced before the write "
        "lands so you can run the probe FIRST, then re-attempt.",
        "",
        "Triggers detected without a recent backing probe:",
    ]
    for r in reasons:
        body_lines.append(f"  - {r}")
    if required_probes:
        body_lines.append("")
        body_lines.append("Probes that would satisfy the gate (run one "
                          "or more of these via Bash, then re-attempt "
                          "the write):")
        for p in required_probes:
            body_lines.append(f"  - {p}")
    body_lines.append("")
    body_lines.append(
        "Some triggers ('out of scope', 'not feasible') have NO "
        "auto-detectable probe, so no command clears the gate: the only "
        "way past it is to REWRITE the proposed content -- remove the bare "
        "hedge, or replace it with a concrete reference (a spec quote that "
        "scopes the work out, or the specific failed run that shows it)."
    )

    body = "\n".join(body_lines)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": body,
        },
        "systemMessage": body,
    }
    json.dump(output, sys.stdout)
    sys.stdout.write("\n")
    return 0


def main() -> int:
    # Kill switch: `touch <council dir>/DISABLED` silences this hook;
    # `rm` re-enables it. Checked per call, so it works mid-session.
    if (Path(__file__).resolve().parent / "DISABLED").exists():
        return 0

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    session_id = payload.get("session_id", "")

    if tool_name not in ("Write", "Edit", "NotebookEdit"):
        return 0

    content = proposed_content_from_tool_input(tool_name, tool_input)
    if not content:
        return 0

    target_path = tool_input.get("file_path", "") or ""
    target_lower = target_path.lower()

    # Exempt files: this script, the council code itself, and memory
    # files. Memory entries codify rules using the trigger phrases by
    # name; blocking those would prevent the rules from being written
    # at all. The council source code likewise references the triggers
    # by name in its quality-bar implementation.
    exempt_substrings = [
        "/professional/council/",
        "/professional/agentic-council/",
        "/.claude/projects/",
        "/.claude/commands/",
        "/memory/feedback_",
        "/memory/memory.md",
        "/council_system_prompt.md",
    ]
    if any(s in target_lower for s in exempt_substrings):
        return 0

    triggers_hit: list[tuple[str, str, list[str]]] = []
    for pattern, label, probes in TRIGGERS:
        if re.search(pattern, content, re.IGNORECASE):
            triggers_hit.append((pattern, label, probes))
    if not triggers_hit:
        return 0

    evidence_commands, blind_reason = collect_evidence_commands(session_id)
    if blind_reason is not None:
        # FAIL-OPEN when blind (the user sign-off 2026-07-22): the gate can only DENY an
        # affirmatively-unbacked caveat, and it cannot affirm anything without reading the
        # evidence. Allow the write and emit an OBSERVABLE note naming the reason (never
        # the false "no matching probe"); the PostToolUse council + Stop audit still review
        # this write under the same blindness and are told by rule 11 to flag an unbacked
        # caveat there too.
        labels = ", ".join(label for _, label, _ in triggers_hit)
        print(f"laziness-gate: could not read this session's evidence ({blind_reason}); "
              f"allowing the write without a pre-write probe check. Triggers present but "
              f"unverifiable here: {labels}", file=sys.stderr)
        return 0

    unbacked: list[str] = []
    required_probes_acc: list[str] = []
    for _, label, probes in triggers_hit:
        if not probes:
            unbacked.append(label)
            continue
        if not any(p.lower() in evidence_commands.lower() for p in probes):
            unbacked.append(label)
            required_probes_acc.extend(probes)

    if not unbacked:
        return 0

    return emit_deny(unbacked, sorted(set(required_probes_acc)))


if __name__ == "__main__":
    sys.exit(main())
