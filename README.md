# agentic-council

A Claude Code integration that puts an adversarial-critic council of up to three
models on every `Write` / `Edit` / `NotebookEdit`, holds Claude's claims to an
explicit quality bar, and refuses lazy "out of scope" / "GPU required" /
"not feasible" caveats unless a backing probe actually ran.

The council does not trust Claude's account of its own work. Neither should you.

## The members

| member | model | effort | transport |
|---|---|---|---|
| codex | `gpt-5.6-sol` | reasoning `high` | `codex exec`, read-only sandbox, has read tools |
| gemini | `gemini-3.5-flash` | `thinkingLevel: high` | Gemini REST API, no tools |
| deepseek | `deepseek-v4-pro` | reasoning `max` | HTTPS, no tools |

Membership is key-gated. gemini is dropped when `GEMINI_API_KEY` is absent and
deepseek when `DEEPSEEK_API_KEY` is absent, so the council runs with one to three
members depending on your environment. Whoever is present runs concurrently for
two rounds: in round 1 each member sees the proposal independently, and in round
2 each sees the others' round-1 verdicts and may revise. That is two model calls
per active member per fire, so two to six calls in total.

**A council member must never be able to mutate state.** This is not a stylistic
preference. The removal rationale recorded in `council/consult_council.py` (at
the `GEMINI_API_URL` definition) reports that an agentic gemini CLI, used as a
read-only critic, autonomously read council files by absolute path and rewrote
six of them plus `settings.json` in a single review, then reported the edits as
done. gemini and deepseek are therefore stateless HTTP completions with no
filesystem access, and gemini is *dropped* rather than falling back to any CLI.
codex is the only subprocess member and runs under `--sandbox read-only`. Do not
"improve" this by giving a member tools.

## What fires, and when

- **PreToolUse** (`laziness_gate.py`). Regex-scans the proposed content for
  caveat triggers ("out of scope", "GPU required", "not feasible",
  "would require", and similar). If a trigger is present and the session's
  evidence file holds no matching probe, the write is **denied before it lands**,
  and the denial names the probe to run. This is the only layer that can stop a
  write. Being a regex, it catches the literal phrases and not paraphrases.
- **PostToolUse** (`council_advisor.py`). Fires the council on the edit, with the
  enclosing function or class supplied as context. The edit is located from the
  hook payload's `structuredPatch`, not by searching the file for the new text
  (a search cannot tell which occurrence changed, and fails on partial-line
  edits). `evidence_logger.py` records the event.
- **PostToolUse, read-type tools** (`evidence_logger.py`). Logs every `Read`,
  `Bash`, `Grep`, `Glob`, `WebFetch`, `WebSearch` and `AskUserQuestion` into
  `~/.claude/state/<session_id>/evidence.jsonl`, so the council can credit
  verification that genuinely happened.
- **Stop** (`stop_audit.py`). Scans the final assistant message for unbacked
  caveats.
- **SessionStart** (`session_start_probe.py`). Runs `uname`, `nproc`, `free`,
  `df`, `nvidia-smi` and similar into the evidence file, so hardware claims have
  a probe from minute zero.

Verdicts are `PASS` (silent), `WARN` (surfaced to Claude), and `BLOCK`. `BLOCK`
is reserved for one thing: a caveat asserted without a probe. Consensus is
strict. Any `BLOCK` blocks, `PASS` requires unanimity, and a lost or unparseable
vote forces `WARN` rather than being silently discarded.

## AUTO-REVERT: read this before you install

**On a `BLOCK`, this software will undo Claude's write.** It is on by default
(`AUTO_REVERT_ON_BLOCK` in `council_advisor.py`). You should know exactly what
that means before you let it near your files.

The council reviews at PostToolUse, which is AFTER the write lands, so a `BLOCK`
could historically only ASK Claude to revert -- and Claude could ignore it. The
PreToolUse gate, the only layer that can deny a write outright, is a regex and
catches only the literal trigger phrases. Auto-revert is what makes a `BLOCK`
mean something.

Two guarantees, both implemented and tested:

- **It preserves the rejected work.** The rejected content is copied to
  `<council_root>/reverted/<timestamp>-<8-hex>-<filename>` BEFORE the original is
  restored, and if that copy fails, nothing is overwritten at all. If the council
  is wrong, the cost is a `cp`, not your afternoon. (It is a backup, not a
  transaction: a concurrent writer touching the same file between the copy and
  the restore is not defended against.)
- **It never deletes a file.** A write that CREATES a file has no prior content
  to restore, so undoing it would mean deletion. It declines and says so.
  (Measured: Claude Code reports both a genuinely new file and an existing EMPTY
  file as `type: "create"` with `originalFile: None`, so these two cases are not
  distinguishable from the payload -- and they want opposite undo operations.
  Guessing would risk deleting a file you made. It does not guess.)

Restores are atomic (temp file plus `os.replace`, which issues `rename(2)`) and
preserve the file's mode, so restoring an executable does not silently strip its
`+x` bit.

**Turn it off** with `touch <council_root>/NO_AUTO_REVERT`, checked per call, or
set `AUTO_REVERT_ON_BLOCK = False`.

Whether you should enable this depends on how often YOUR council is wrong when it
says `BLOCK`, which is exactly what `council_outcome.py` measures. Measure it
before you trust it.

## Two tools beyond the hooks

**`council_dialogue.py`** escalates a fire into a round-table. The members
question you, you answer with evidence, and convergence is computed rather than
declared. The thread ends in a `FINAL` artifact that the tool does not rewrite
afterwards.

**`council_outcome.py`** is the honesty instrument. A high WARN rate is
uninterpretable on its own: it is equally consistent with "Claude's work is bad"
and with "the council over-flags", and nothing in the verdicts separates them.
This tool separates them, and every control in it assumes **Claude will cheat if
allowed to**:

- `sample` deals a **random cohort**. You do not choose what to label. The seed
  is cryptographic and there is deliberately no `--seed` flag (that would be
  seed-shopping), the pool is persisted so the draw can be replayed, and no new
  cohort can be drawn until the current one is fully labelled AND adjudicated.
  `--force` can abandon a cohort, but the abandonment is written into the cohort
  record in `sample_cohorts.jsonl` (`council_outcome.py`, `abandoned_cohort`) and
  `stats` prints a banner for it. Abandoning DEFERS a cohort, it does not escape
  one: `open_cohort()` returns the newest cohort that is not fully settled, and
  abandoning a cohort does not settle it, so it resurfaces and blocks the next
  unforced draw as soon as the cohort that displaced it is finished. It stops
  blocking only once its own concerns are labelled and adjudicated, like any
  other cohort. That
  file is ordinary JSONL, so the record survives only as long as nobody edits it
  out; see the caveat below.
- `adjudicate` has **the council rule on Claude's label**, with the member who
  raised the concern excluded from judging it. A tie fails closed, so a single
  dissent disputes a label, and `DISPUTED` labels are excluded from the outcome
  columns entirely.
- `stats` prints coverage first, refuses to pool a `--verdict` stratum into the
  cross-section numbers, and raises a banner on any label re-written after the
  council disputed it.

None of this is cryptographically enforced against the agent it audits. Claude
can edit these files. What the tool actually provides is that faking a clean
history requires deliberately rewriting an audit record, rather than merely
staying quiet.

## Install

```
git clone <this-repo>
cd agentic-council
python3 install.py            # --dry-run to preview, --council-root to relocate
```

Then **restart Claude Code**, because hook configuration is read at session start.

The installer checks Python 3.10+, the codex CLI and `GEMINI_API_KEY`; live-probes
codex on the same model the council actually uses and the Gemini REST endpoint;
copies the scripts; and merges the hook block into `~/.claude/settings.json`
**idempotently**, so re-running it replaces the council's own hooks rather than
appending a second copy of each. It prunes at the handler level and by exact
script basename, so other tools' hooks survive even when they share an entry with
the council's. The one hole, stated plainly: a hook of your own whose script is
named exactly `stop_audit.py` (or any other council filename) would also be
pruned, and nothing detects that. Your prior `settings.json` is backed up first.

### Prerequisites

- **Python 3.10+**.
- **codex CLI**, authenticated. Verified working here: `codex-cli 0.144.1` with
  `gpt-5.6-sol`. An older CLI may not know the model, and a rejection can also
  come from your account tier rather than from the CLI version.
- **`GEMINI_API_KEY`**: a key, NOT the gemini CLI.
- **`DEEPSEEK_API_KEY`**: optional.

Both keys must be exported **in the environment Claude Code itself launches
under**, because the council runs as a child process and inherits that
environment. A common trap is exporting them below the early-return guard in
`~/.bashrc`, where a non-interactive shell never reaches them.

### Kill switch

`touch <council_root>/DISABLED` silences every hook, and `rm` re-enables it. It
is checked per call, so it works mid-session.

## What this costs you

- **Two to six model calls per Write/Edit/NotebookEdit**, depending on how many
  members your keys enable. Check your vendors' pricing.
- **Latency**: members run in parallel, so wall time per fire is roughly twice the
  slowest member's round trip, not the sum of all of them.
- **Noise**: `WARN` is common and `BLOCK` is not. Whether that is signal or
  over-flagging is exactly the question `council_outcome.py` exists to answer.
  Measure it on your own work rather than trusting this README.

## Tuning

- **The quality bar**, at `<council_root>/council_system_prompt.md`. Read it
  before you deploy this. It is opinionated, and it is the whole product.
- **The trigger phrases**, in `laziness_gate.py` and `stop_audit.py`. The list is
  duplicated in both, so edit both. A domain such as security research uses
  "out of scope" perfectly legitimately.
- **The SessionStart probes**, in `session_start_probe.py`, which are
  Linux-shaped. macOS users will want to swap `free -h` for `vm_stat`, and so on.

## A warning about the logs

Every fire writes a JSON log containing **the full pitch**: the actual file
content under review, the diff, and the user's directives. Measured on the
machine this was developed on, that reached 1.1 GB across 7,224 fires, covering
real client work. The runtime artifacts (`logs/`, `threads/`, `outcomes.jsonl`,
`sample_cohorts.jsonl`) are gitignored, but if you fork this or relocate the
install, **check before you push**.

## License

MIT. See `LICENSE`.

## Layout

```
agentic-council/
  install.py                          # bootstrap installer
  council/
    consult_council.py                # two-round wrapper: members, prompts, verdicts
    council_advisor.py                # PostToolUse hook -> fires the council
    council_dialogue.py               # round-table escalation
    council_outcome.py                # labelling, adjudication, statistics
    laziness_gate.py                  # PreToolUse hook -> the only layer that can DENY
    stop_audit.py                     # Stop hook
    evidence_logger.py                # PostToolUse hook -> the evidence file
    session_start_probe.py            # SessionStart hook -> environment probes
    session_start_directive.py        # SessionStart hook -> research directive
    council_system_prompt.md          # THE QUALITY BAR
    council_dialogue_prompt.md
  claude-code/
    settings.hooks.template.json
    commands/council.template.md      # the /council slash command
  starter-prompts/
```
