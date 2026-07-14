#!/usr/bin/env python3
"""Evidence logger: PostToolUse hook for tool calls.

Appends a structured record of each tool call to a per-session JSONL
file at ~/.claude/state/<session_id>/evidence.jsonl, consumed by the
council wrapper.

Behavior:
- Reads the Claude Code PostToolUse JSON payload from stdin.
- Truncates long output keeping both head and tail (see truncate_tail).
- Exits 0 on every path; a top-level guard catches unexpected errors.
  Diagnostics go to stderr.

Tools with a verified input shape get a dedicated branch. Allowlisted
tools without a verified shape (Task, Agent, NotebookEdit, MultiEdit)
fall through to a bounded generic capture that serializes whatever
tool_input contains, so they are visible without assuming field names.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_ROOT = Path.home() / ".claude" / "state"
MAX_TAIL_LINES = 150
MAX_TAIL_BYTES = 16384
MAX_HEAD_LINES = 50
MAX_HEAD_BYTES = 6144


def truncate_tail(text: str) -> str:
    """Keep the head and the tail of long text, eliding the middle, so
    neither the start nor the end of a long output is lost. Name kept for
    the existing call sites."""
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    if len(lines) > MAX_TAIL_LINES:
        tail_n = MAX_TAIL_LINES - MAX_HEAD_LINES
        elided = len(lines) - MAX_HEAD_LINES - tail_n
        text = ("".join(lines[:MAX_HEAD_LINES])
                + f"\n[... {elided} lines elided ...]\n"
                + "".join(lines[-tail_n:]))
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > MAX_TAIL_BYTES:
        tail_b = MAX_TAIL_BYTES - MAX_HEAD_BYTES
        dropped = len(encoded) - MAX_HEAD_BYTES - tail_b
        text = (encoded[:MAX_HEAD_BYTES].decode("utf-8", errors="replace")
                + f"\n[... {dropped} bytes elided ...]\n"
                + encoded[-tail_b:].decode("utf-8", errors="replace"))
    return text


def coerce_to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def read_output_from_response(tool_response) -> str:
    if tool_response is None:
        return ""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        file_block = tool_response.get("file")
        if isinstance(file_block, dict):
            return coerce_to_text(file_block.get("content"))
        for key in ("content", "output", "text", "result"):
            val = tool_response.get(key)
            if val:
                return coerce_to_text(val)
        results = tool_response.get("results")
        if results:
            try:
                return json.dumps(results, default=str)
            except (TypeError, ValueError):
                return coerce_to_text(results)
        return ""
    if isinstance(tool_response, list):
        try:
            return json.dumps(tool_response, default=str)
        except (TypeError, ValueError):
            return coerce_to_text(tool_response)
    return str(tool_response)


def extract_event(tool_name: str, tool_input: dict, tool_response) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    event = {"tool": tool_name, "at": now}

    if tool_name == "Read":
        event["file_path"] = tool_input.get("file_path", "")
        event["offset"] = tool_input.get("offset")
        event["limit"] = tool_input.get("limit")
        event["output_tail"] = truncate_tail(read_output_from_response(tool_response))
        return event

    if tool_name == "Bash":
        event["command"] = tool_input.get("command", "")
        event["description"] = tool_input.get("description", "")
        if isinstance(tool_response, dict):
            exit_code = tool_response.get("exitCode")
            if exit_code is None:
                exit_code = tool_response.get("exit_code")
            event["exit_code"] = exit_code
            event["stdout_tail"] = truncate_tail(coerce_to_text(tool_response.get("stdout")))
            event["stderr_tail"] = truncate_tail(coerce_to_text(tool_response.get("stderr")))
            event["interrupted"] = tool_response.get("interrupted")
        else:
            event["stdout_tail"] = truncate_tail(coerce_to_text(tool_response))
        return event

    if tool_name in ("Grep", "Glob"):
        relevant_keys = (
            "pattern", "path", "glob", "type", "output_mode", "head_limit",
            "-A", "-B", "-C", "-i", "-n", "multiline",
        )
        event["args"] = {k: tool_input[k] for k in relevant_keys if k in tool_input}
        event["output_tail"] = truncate_tail(read_output_from_response(tool_response))
        return event

    if tool_name == "Write":
        event["file_path"] = tool_input.get("file_path", "")
        event["content_digest"] = truncate_tail(coerce_to_text(tool_input.get("content", "")))
        return event

    if tool_name == "Edit":
        event["file_path"] = tool_input.get("file_path", "")
        event["replace_all"] = bool(tool_input.get("replace_all", False))
        event["old_digest"] = truncate_tail(coerce_to_text(tool_input.get("old_string", "")))
        event["new_digest"] = truncate_tail(coerce_to_text(tool_input.get("new_string", "")))
        return event

    if tool_name == "WebFetch":
        event["url"] = tool_input.get("url", "")
        event["prompt"] = tool_input.get("prompt", "")
        event["output_tail"] = truncate_tail(read_output_from_response(tool_response))
        return event

    if tool_name == "WebSearch":
        event["query"] = tool_input.get("query", "")
        if tool_input.get("allowed_domains"):
            event["allowed_domains"] = tool_input["allowed_domains"]
        if tool_input.get("blocked_domains"):
            event["blocked_domains"] = tool_input["blocked_domains"]
        event["output_tail"] = truncate_tail(read_output_from_response(tool_response))
        return event

    if tool_name == "ToolSearch":
        event["query"] = tool_input.get("query", "")
        event["max_results"] = tool_input.get("max_results")
        out = read_output_from_response(tool_response)
        if not out:
            out = coerce_to_text(tool_response)
        event["output_tail"] = truncate_tail(out)
        return event

    if tool_name == "AskUserQuestion":
        # Verified against a real hook payload: tool_response is
        # {"answers": {<question>: <answer>}, "questions": [...]}.
        answers = {}
        if isinstance(tool_response, dict):
            a = tool_response.get("answers")
            if isinstance(a, dict):
                answers = a
        event["answers"] = answers
        qs = tool_input.get("questions")
        if isinstance(qs, list):
            event["questions"] = [
                q.get("question", "") for q in qs if isinstance(q, dict)
            ]
        return event

    # Bounded generic capture for allowlisted tools without a verified
    # input shape (Task, Agent, NotebookEdit, MultiEdit) and any other
    # tool. Serializes the whole tool_input, truncated, so nothing is
    # assumed about field names.
    try:
        input_text = json.dumps(tool_input, default=str)
    except (TypeError, ValueError):
        input_text = coerce_to_text(tool_input)
    event["tool_input_digest"] = truncate_tail(input_text)
    out = read_output_from_response(tool_response)
    if not out:
        out = coerce_to_text(tool_response)
    event["output_tail"] = truncate_tail(out)
    return event


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"evidence-logger: invalid JSON on stdin: {e}", file=sys.stderr)
        return 0

    session_id = payload.get("session_id", "")
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    # Documentation sources disagree on the PostToolUse output field
    # name; check both `tool_result` and `tool_response` defensively.
    tool_output = payload.get("tool_result")
    if tool_output is None:
        tool_output = payload.get("tool_response")

    if not session_id or not tool_name:
        return 0

    if tool_name not in ("Read", "Bash", "Grep", "Glob", "Write", "Edit",
                         "WebFetch", "WebSearch", "AskUserQuestion",
                         "ToolSearch", "Task", "Agent", "NotebookEdit",
                         "MultiEdit"):
        return 0

    event = extract_event(tool_name, tool_input, tool_output)

    state_dir = STATE_ROOT / session_id
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"evidence-logger: mkdir failed: {e}", file=sys.stderr)
        return 0

    evidence_file = state_dir / "evidence.jsonl"
    try:
        with open(evidence_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except OSError as e:
        print(f"evidence-logger: write failed: {e}", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never let a hook crash propagate
        print(f"evidence-logger: unexpected error: {e}", file=sys.stderr)
        sys.exit(0)
