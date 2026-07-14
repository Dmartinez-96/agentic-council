# starter-prompts/

Two starter rule files that describe the disciplined responses to the council's WARN and BLOCK verdicts. They are templates intended for adaptation; they are not used at runtime by the council itself.

The council runtime (`council/*.py` plus `council_system_prompt.md`) enforces rules 1-11 from `council_system_prompt.md`. These two starter prompts describe the BEHAVIORAL pattern Claude should follow when those rules fire:

- `council-warn-response.md` walks through how to react to a `VERDICT: WARN`. Default response is verify-or-refute-or-converge, not capitulate-and-weaken. Enumerates banned response shapes and valid response shapes.
- `caveat-phrases.md` enumerates the rule-11 trigger phrases, the per-phrase probe markers the laziness gate scans for in evidence, and the disciplined response when a BLOCK fires.

## How to use

Either file can be:

1. Copied verbatim into a per-project agent-instruction store (whatever your Claude Code instance uses for persistent agent rules). The disciplined-response content is generic and ready as-is.
2. Trimmed or extended to match your own workflow. The shipped rule-11 trigger list reflects coding-work patterns; security researchers and others may legitimately use phrases like "out of scope" in scoping documents and will want to relax the list locally.
3. Skipped entirely. The council runtime works without these starter prompts in place; they are the documented BEHAVIORAL counterpart to the council's quality bar, not a prerequisite for the council to fire.

## Customization tips

- Review the eleven rules in `council_system_prompt.md` and remove or rewrite any that do not match your work. Rules that target a specific domain (rather than general intellectual-honesty failures) are good candidates for removal when that domain does not apply.
- The rule-11 trigger phrase list lives in two files (`council/laziness_gate.py` for the PreToolUse gate, `council/stop_audit.py` for the Stop hook). If you add or remove a trigger phrase, change both files so the gate and the Stop hook agree.
- The "Banned response shapes" / "Valid response shapes" enumeration in `council-warn-response.md` is the easiest place to adapt voice and phrasing to your team's preferred terminology.
