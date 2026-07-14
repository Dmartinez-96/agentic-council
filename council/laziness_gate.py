#!/usr/bin/env python3
"""PreToolUse laziness gate.

Fires before Write / Edit / NotebookEdit. Scans the
proposed content for council rule-11 trigger phrases ("out of scope",
"GPU required", "compute required", "fetch too large", "not feasible",
"smoke-tested by reading", "would require", "not run end-to-end").
For each trigger, checks the per-session evidence file at
~/.claude/state/<session_id>/evidence.jsonl for a matching probe
event. If a trigger is present and no matching probe is in evidence,
denies the tool call via the PreToolUse permissionDecision output schema
documented in the plugin-dev hook-development SKILL.md (which specifies
that a PreToolUse hook can deny permission by returning a JSON payload
with hookSpecificOutput.permissionDecision set to "deny").

Why this is a hard gate rather than a council WARN: rule 11 violations
waste Dakotah's API spend, time, and trust. The council exists to
catch them at PostToolUse, but at that point the lazy write has
already landed and reverting is on me. PreToolUse can refuse the
write before it happens. This file enforces the rule synchronously
without an LLM call: pure regex + evidence-file lookup, runs in
milliseconds.

Conservative design: it is better to over-block (Claude is forced to
run the probe before the caveat can land) than under-block (a lazy
caveat slips through). False positives are addressable by running the
required probe, which is what the rule asks for in the first place.

Output schema (Claude Code PreToolUse hook):
- Exit 0 with NO stdout = allow.
- Exit 0 with stdout JSON `{"hookSpecificOutput": {"permissionDecision":
  "deny", ...}, "systemMessage": "..."}` = deny with explanation.

The PreToolUse output schema and hook exit-code semantics are documented
in the plugin-dev hook-development SKILL.md (which details the JSON payload format
for hookSpecificOutput/permissionDecision and specifies that exit code 2 blocks
execution, routing stderr back to the assistant).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

STATE_ROOT = Path.home() / ".claude" / "state"
EVIDENCE_SCAN_LAST_N_EVENTS = 250


TRIGGERS: list[tuple[str, str, list[str]]] = [
    # (regex_pattern, human_label, probe_markers)
    # probe_markers: any of these substrings in the `command` field of a
    # Bash event in evidence satisfies the probe requirement; an empty
    # list means there is no easy auto-detectable probe and the trigger
    # is always denied when present.
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
        "compute-required without probe",
        ["python", "python3", "uv ", "pip ", "cuda", "venv"],
    ),
    (
        r"\bnot\s+feasible\b",
        "not-feasible without probe",
        ["python", "python3", "uv ", "pip ", "Bash"],
    ),
    (
        r"\bnot\s+run\s+end[- ]to[- ]end\b",
        "not-run-end-to-end without probe",
        ["python", "python3", "uv ", "pip "],
    ),
    (
        r"\bsmoke[- ]tested\s+by\s+reading\b",
        "smoke-tested-by-reading without an actual test",
        ["python", "python3", "pytest", "uv ", "pip "],
    ),
    (
        r"\bout\s+of\s+scope\b",
        "out-of-scope without spec read",
        [],
    ),
    (
        r"\bwould\s+(need\s+to\s+run|require)\b",
        "would-need-to-run without probe",
        ["python", "python3", "uv ", "pip ", "Bash"],
    ),
]


def collect_evidence_commands(session_id: str) -> str:
    """Return a single concatenated string of recent Bash commands and
    tool descriptors from the per-session evidence file. Empty string
    if file does not exist.
    """
    if not session_id:
        return ""
    p = STATE_ROOT / session_id / "evidence.jsonl"
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


def proposed_content_from_tool_input(tool_name: str, tool_input: dict) -> str:
    """Extract the textual proposed content from the tool input."""
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        old = tool_input.get("old_string", "") or ""
        new = tool_input.get("new_string", "") or ""
        return new + "\n" + old
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
        "detected in your proposed write/edit content, and the "
        "session evidence does not contain a matching probe for at "
        "least one of them. The denial is enforced before the write "
        "lands so you can run the probe FIRST, then re-attempt.",
        "",
        "Triggers detected without backing probe:",
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
        "If a trigger phrase has no auto-detectable probe (e.g. 'out "
        "of scope'), the disciplined response is to Read the spec or "
        "task-definition file with a quote that explicitly scopes the "
        "work out, OR to rewrite the proposal without the caveat."
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

    evidence_commands = collect_evidence_commands(session_id)
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
