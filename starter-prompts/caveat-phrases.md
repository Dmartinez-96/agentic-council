# Caveat phrases require probe evidence

Caveat phrases that declare work undone, unfeasible, or out of scope are forbidden unless a corresponding probe in the session evidence backs the caveat. The council enforces this via rule 11 in `council_system_prompt.md` and returns `VERDICT: BLOCK` (not WARN) on violation.

**Why:** Capitulating to "compute required" or "out of scope" instead of attempting the work wastes the user's API spend, time, and trust. The cost is theirs, not the model's. "Cheap for the model" is expensive for the user. This rule exists because Claude has a pattern of declaring work undone instead of running the probe that would either complete the work or honestly justify the caveat.

**How to apply:**

For each of these phrases (and equivalents in spirit), the corresponding probe must be in the session evidence file at `~/.claude/state/<session_id>/evidence.jsonl` BEFORE the phrase appears in any Write or Edit:

- `GPU required`, `GPU needed`, `compute requires GPU` -> `nvidia-smi` (or equivalent) in evidence showing GPU state.
- `fetch too large`, `fetch would be too large` -> the actual WebFetch or curl attempt in evidence showing the response size.
- `compute required`, `compute would be needed`, `not feasible`, `cannot run` -> the actual attempt in evidence (Bash with timing or error output) demonstrating failure or infeasibility.
- `out of scope` -> a Read of the spec / task-definition file in evidence with a direct quote that scopes the work out.
- `not run end-to-end`, `partially run` -> the partial run attempt in evidence with the output where it stopped.
- `smoke-tested by reading`, `validated by inspection` -> at minimum the Read in evidence AND an explicit justification of why a runtime smoke is unnecessary (rare; usually a runtime smoke IS required).
- `would require`, `would need to run`, `skipping verification` -> default to actually running the probe; do not ship the phrase without a backing probe.

**Council BLOCK semantics (PostToolUse):**

- The Write or Edit being reviewed has already landed on disk by the time the PostToolUse hook runs. This is documented in the local hook-development SKILL.md under the heading `### PostToolUse` (the version checked in this session has the heading at line 155).
- When BLOCK surfaces (hook exit 2 with the BLOCK body on stderr; exit-code semantics are documented in the same SKILL.md under the heading `### Exit Codes`, line 294 in the version checked here), the expected response is documented in `council_system_prompt.md` rule 11: revert the change just made (re-Write the prior content, or Edit the offending phrasing out), run the missing probe so it lands in evidence, then re-attempt the change with the probe entry present.
- Proceeding past a BLOCK without reverting goes against the rule that produced it. The same verify-or-refute discipline that applies to WARN extends to BLOCK; capitulation-without-verification is the heavier-cost case of the same failure mode.

**Pre-Write self-check (do this BEFORE the hook fires):**

Before any Write or Edit that contains a candidate caveat phrase, mentally run: "Does the session evidence already contain the probe that would back this caveat?" If no, run the probe FIRST via Bash, then write the caveat (now backed) or rewrite the prose to remove the caveat (because the probe revealed the work is feasible).
