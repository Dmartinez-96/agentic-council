# starter-prompts/

Starter files you can copy and adapt. They are templates; the council does not use them at runtime.

- `standing-rules.md.template` -- an agent-agnostic starter for your agent's STANDING rules / context file (see below).
- `council-warn-response.md` and `caveat-phrases.md` -- starters describing the disciplined responses to the council's WARN and BLOCK verdicts.

The council runtime (`council/*.py` plus `council_system_prompt.md`) enforces the quality bar defined in `council_system_prompt.md`. The two response starters describe the BEHAVIORAL pattern the agent should follow when those rules fire:

- `council-warn-response.md` walks through how to react to a `VERDICT: WARN`. Default response is verify-or-refute-or-converge, not capitulate-and-weaken. Enumerates banned response shapes and valid response shapes.
- `caveat-phrases.md` enumerates the rule-11 trigger phrases, the per-phrase probe markers the laziness gate scans for in evidence, and the disciplined response when a BLOCK fires.

## standing-rules.md.template (your agent's standing rules)

An agent-agnostic starter for the standing-rules / context file the council injects in front of every member (bar item 12 makes your standing instructions binding, so a member can cite the exact rule a proposal broke instead of inferring one). It is model-agnostic on purpose -- nothing in it assumes a particular agent.

The filename matters. It ships as `standing-rules.md.template`, NOT `CLAUDE.md`, precisely because it is not tied to any one agent. Two ways to install it:

- Put it at `~/.claude/CLAUDE.md` -- the path the council reads by default. If your access method is Claude Code, that same path is also auto-loaded into the leader every session (code.claude.com/docs/en/memory.md, checked 2026-07-23), so rename this file to `CLAUDE.md` and place it there.
- Or set `COUNCIL_STANDING_RULES_PATH` to this file's path and keep any name -- the council reads it there, no `CLAUDE.md` required.

Read the file's own header before copying -- its failure-mode list was observed on one project and may not be your agent's.

## How to use the response starters

Either of `council-warn-response.md` / `caveat-phrases.md` can be:

1. Copied verbatim into a per-project agent-instruction store (whatever your harness uses for persistent agent rules; for Claude Code that is `~/.claude/CLAUDE.md`). The disciplined-response content is generic and ready as-is.
2. Trimmed or extended to match your own workflow. The shipped rule-11 trigger list reflects coding-work patterns; security researchers and others may legitimately use phrases like "out of scope" in scoping documents and will want to relax the list locally.
3. Skipped entirely. The council runtime works without these starter prompts in place; they are the documented BEHAVIORAL counterpart to the council's quality bar, not a prerequisite for the council to fire.

## Customization tips

- Review the rules in `council_system_prompt.md` and remove or rewrite any that do not match your work. Rules that target a specific domain (rather than general intellectual-honesty failures) are good candidates for removal when that domain does not apply.
- The rule-11 trigger phrase list lives in two files (`council/laziness_gate.py` for the PreToolUse gate, `council/stop_audit.py` for the Stop hook). If you add or remove a trigger phrase, change both files so the gate and the Stop hook agree.
- The "Banned response shapes" / "Valid response shapes" enumeration in `council-warn-response.md` is the easiest place to adapt voice and phrasing to your team's preferred terminology.
