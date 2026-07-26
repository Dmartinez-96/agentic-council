# brain -- scaffolding for a checkable fact ledger

This directory is **scaffolding, not content**. It ships a schema, two note
templates, and a validator so you can build your own vault. No vault ships with it,
and you should gitignore yours.

## The problem it addresses

Prose goes stale silently. A number written into a paragraph is correct the day it
is written and stays there long after it stops being true, and nothing about the
paragraph changes when the world does. A brain note does not restate a fact -- it
stores the **check that produces** the fact, and the validator re-runs it.

**Honest standing of that premise:** it is a hypothesis. It comes from watching a
2,000-line append-mostly handoff propagate stale numbers, twice, in a single working
session. Whether atomic checkable notes actually prevent that has **not been tested**
here, and this design has not been compared against any alternative. Try it because
the failure is real, not because the fix is proven.

## What belongs here, and what does not

The vault holds the **FACT layer only**. It holds no reasoning.

Design rationale, research synthesis, and lessons are neither observations nor
attributed statements. Forcing them into this schema would launder opinion as fact,
which is precisely what the schema exists to prevent. Keep them in prose documents --
and have the prose **link to notes instead of restating their numbers**. That link is
the whole mechanism: it is what makes a stale number visible instead of invisible.

## Two note types. There is no third

**CHECKABLE** -- the claim *is* the observation. You store a check; the validator
renders the claim from it. There is no field to write a claim in, deliberately: an
authored sentence beside a check re-opens laundering because nothing forces the two
to correspond.

**TESTIMONY** -- someone said or decided something. Attributed, dated, and never
citable as ground truth (the validator rejects a note that tries to set
`citable_as_ground_truth: true`).

See `templates/checkable.md` and `templates/testimony.md` for the full field list.

## Rules the validator enforces

- **A note cannot assert its own freshness.** There is no `verified` field. The
  validator owns `<vault>/.attestations.json`; verification is a property of that
  ledger, never a claim inside a note.
- **An attestation is keyed to the whole check.** Editing `expect`, `exit_status`, a
  path, or the falsifier drops the previous PASS rather than inheriting it.
- **An INVALID note loses its attestation**, so a broken note cannot keep a badge
  earned before it broke.
- **A failing check is not a false fact.** The world may have changed, the check may
  have rotted, or it may be transient -- indistinguishable here, and only the first
  is a fact change. Failures report `NEEDS_ADJUDICATION`, for a human.
- **An unrun check is never a PASS.** Budget exhaustion reports `NOT_RUN`.
- **Self-echoing checks are rejected**, where `expect` appears inside the check
  itself and can match the command being echoed rather than a result.
- **Link integrity**: duplicate ids, dangling or non-reciprocal supersession, and
  supersession cycles are reported.

## What this does NOT fix

**It does not close the void-check hole.** A check whose output is the same whether
the fact is true or false will pass, be attested, and look authoritative. The
required `falsifier` field is an **authoring discipline**, not a mechanism: it forces
you to write down what a false fact would print, where a human can see the omission.
The validator cannot check it. Two specific void shapes *are* caught mechanically --
self-echo, and a command whose error text contains `expect` (a matching exit status
is required).

A named-stream check was in the accepted design and turned out **impossible**: the
sandbox returns combined stdout+stderr with no way for a caller to separate them, so
it was dropped rather than shipped as a field the code ignores.

## Safety

`check_kind: command` executes text from note files. It runs **only** under
`--run-commands` (off by default) and **only** inside the engine's bubblewrap sandbox
-- network off, environment cleared, tmpfs, rlimits, process-group kill, over a
scrubbed ephemeral copy, fail-closed when bubblewrap is absent.

The authored form is an **argv array**, shell-quoted before it reaches the sandbox.
That removes shell-metacharacter surface at the authoring layer. It is **not** an
argv-only execution path -- the sandbox interface is `sh -c` -- and should not be
described as one.

**The real boundary is authorship.** A vault is authored by you or your harness,
never by a council member. Importing someone else's vault means agreeing to run
their commands.

## Limits you will hit

- `check_kind: url` reaches only hosts on the engine's exact-host allowlist. For any
  other source, stage the data locally and use `check_kind: file` -- and understand
  that you are then attesting the **local artifact**, not the remote source.
- The sandbox has **no network**, so a command check can never verify a remote fact.
- Per-check time is bounded by the engine's own sandbox timeout. `--budget-seconds`
  adds an optional total cap and has **no default**, because a total budget is a
  number only you can justify for your vault.

## Use

```
python3 brain/validate_brain.py <vault-dir> [--root DIR] [--run-commands]
                               [--budget-seconds N] [--json]
```

`--root` is the directory that `check_path` and command checks resolve against.
Exit status is non-zero if any note is INVALID or any integrity problem is found.

## If you feed a vault to the council

Only notes that are **current** (not superseded), **not expired**, and backed by a
**last-run PASS** may be injected as facts. Testimony may be injected when explicitly
requested, always labelled non-citable. `NEEDS_ADJUDICATION`, failing, expired, and
`NOT_RUN` notes must **never** reach a member as facts -- that would make the vault a
laundering channel into the council itself, which is worse than having no vault.
