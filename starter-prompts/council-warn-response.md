# Council WARN response

When the council emits WARN, the default response is verify-or-refute-or-converge, not capitulate-and-weaken.

**Why:** The council exists to catch real failures (invention, extrapolation, speculation as fact, etc.) and also produces some false positives, particularly when a council member cannot see prior tool outputs that already verified the flagged claim. Weakening a claim to satisfy a WARN that's actually a false positive removes verified content from the deliverable without improving correctness, and ships an artifact weaker than the underlying evidence supports. The council system prompt itself codifies the resolution path: members are asked to follow evidence over priors once Claude surfaces verification, and Claude is asked not to weaken claims that verified evidence supports. Capitulation-without-verification breaks both halves of the rule.

**How to apply:** On every WARN, run this loop:

1. Identify the specific claim the council member flagged.
2. Check the per-session evidence file at `~/.claude/state/<session_id>/evidence.jsonl` for a Read, Bash, or other tool output that backs the claim.
3. If evidence exists: surface it inline (cite the file path and the relevant lines, or paste the verbatim Bash output that supports the claim). Push back on the WARN with the receipts. Do NOT weaken the claim solely to satisfy the WARN.
4. If evidence does NOT exist: either (a) run the verification now (Read the file, run the Bash, fetch the doc) so the next council fire can see it in the evidence trail, or (b) accept the WARN and revise the claim to match what is actually verified. Do NOT hedge with weasel words ("probably", "likely", "may") alone; that is the speculation-as-fact pattern the council is designed to catch.
5. If a WARN persists after evidence has been surfaced: the disagreement is escalated to the user for arbitration. Do not silently weaken to make the WARN go away.

One false-positive shape worth watching for: a council member flags a path-or-line-level claim as unsourced when the Read or Bash that established it happened earlier in the same session and is either truncated or absent from the evidence block the member sees. The fix in that case is to either re-Read the relevant lines (which puts a fresh entry in the evidence file) or to quote the verifying content verbatim in the response, so the next council fire can credit the verification.

**Banned response shapes on WARN:**
- "I'll weaken the claim to satisfy the council." (capitulation)
- "The council can't see what I verified, so I'll ship with the WARN." (silent disagreement)
- "I'll hedge with 'probably' and re-run." (speculation dressed up)

**Valid response shapes on WARN:**
- "Verified by Read of $PATH at line $N earlier in session: <verbatim line>." (refutation with receipts)
- "Re-verifying now via Bash..." followed by the actual command and its output (verification on demand)
- "Accepted, revising to <weaker but supported claim>." (legitimate weakening when verification fails)
- "Council and I disagree after evidence exchange; surfacing to the user." (arbitration)
