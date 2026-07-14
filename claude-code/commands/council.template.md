---
description: Invoke the council on a design / scoping / approach decision before implementing
argument-hint: [design or scoping subject - free text]
allowed-tools: Bash, Read
---

The user has invoked /council on the following reasoning subject:

$ARGUMENTS

Your job: run the council wrapper at `{{COUNCIL_ROOT}}/consult_council.py` with `--layer reasoning` against that subject as the proposal pitch, then surface the per-member verdicts to the user verbatim before taking any action on the design.

Concrete steps (do all of them; do NOT skip any):

1. Locate the current session transcript via Bash. Transcripts live as `<session_id>.jsonl` files in a per-project subdirectory under `~/.claude/projects/`. Discover the right one dynamically by finding the most recently modified `*.jsonl` file under that root. For example: `find ~/.claude/projects/ -maxdepth 2 -name "*.jsonl" -printf "%T@ %p\n" 2>/dev/null | sort -n | tail -1 | awk "{print \$2}"`. The basename without `.jsonl` is the session_id; the full path is the transcript_path.

2. Build the evidence file path: `~/.claude/state/<session_id>/evidence.jsonl`. Verify existence via `test -f` before passing it; if it does not exist, skip the `--evidence-file` flag.

3. Build the pitch. The pitch should be the subject text above PLUS, if helpful for the council's evaluation, a short framing of (a) what the design decision is, (b) what alternatives are on the table, (c) what you (Claude) are leaning toward and why. Do NOT add unverified claims; if you do not have a leaning yet, just state the question.

4. Invoke the wrapper via Bash, piping the pitch on stdin. Pattern:

```
python3 {{COUNCIL_ROOT}}/consult_council.py \
  --layer reasoning \
  --transcript-path <transcript_path> \
  --evidence-file <evidence_file_if_present> \
  <<'PITCHEOF'
<pitch text here>
PITCHEOF
```

5. Read the wrapper's stdout. It contains a top-line `VERDICT:` plus per-member sections. Surface the entire output to the user verbatim, then state your own response:
   - On `VERDICT: PASS`: proceed with the design.
   - On `VERDICT: WARN`: do NOT proceed past the WARN without addressing it. For each WARN reason, either surface primary-source evidence inline that refutes the concern, OR accept the refutation and revise the design. Do not weaken claims to satisfy the WARN when verified evidence supports the original claim; do not silently ignore the WARN either.
   - On `VERDICT: BLOCK`: revert any tentative work and run the required probe before re-attempting the design.

6. Treat the council's verdict as a load-bearing input to the design decision, not advisory chatter.

If the wrapper exits non-zero with a setup error (missing system prompt, no members, etc.), report the wrapper's stderr to the user and ask whether to retry or proceed without the council fire. Do not silently skip the council.
