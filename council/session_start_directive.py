#!/usr/bin/env python3
"""SessionStart hook: inject a standing deep-research-style directive.

Claude Code has no supported way to make the bundled /deep-research
workflow run by default (verified against code.claude.com/docs: there
is no settings default for workflows, and hooks cannot invoke a slash
command). The supported nudge is to inject a standing directive at
session start that biases Claude toward deep-research-style,
multi-source, primary-source verification for substantive questions.
Claude still chooses whether to act on it.

Mechanism (verified against the official Claude Code hooks docs,
SessionStart "decision control" section): a SessionStart hook adds
context via the structured field
  {"hookSpecificOutput": {"hookEventName": "SessionStart",
                          "additionalContext": "<text>"}}
which is added to Claude's context at the start of the conversation,
before the first prompt.

This script ignores the payload, prints that JSON, and exits 0.
"""

from __future__ import annotations

import json
import sys

DIRECTIVE = (
    "Standing session directive (injected by the SessionStart "
    "deep-research hook):\n"
    "For substantive factual, research, or technical questions, "
    "especially those involving external facts, library or API "
    "behavior, licensing, current events, version numbers, or any "
    "claim that needs sourcing, default to deep-research-style "
    "verification before answering: fan out across multiple sources, "
    "cross-check them, and cite primary sources rather than answering "
    "from memory. Prefer the /deep-research workflow (or an equivalent "
    "multi-source verification pass) for such questions. For trivial, "
    "mechanical, or conversational turns, answer directly without the "
    "research pass."
)


def main() -> int:
    # Consume stdin if present (the SessionStart payload); not needed.
    try:
        sys.stdin.read()
    except Exception:
        pass
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": DIRECTIVE,
        }
    }
    json.dump(output, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
