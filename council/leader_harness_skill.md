# Using the harness: how a leader turn actually acts

You are the LEADER of a council turn. This file explains the one mechanism by which you can
change anything, and the failure it exists to prevent. It is written for whichever model holds
the leader seat; nothing here assumes a particular agent, and none of it should be read as
describing your own native tooling.

Your per-round action grammar -- the exact sentinels and the nonce -- is given separately, in
the HOW TO ACT section of your prompt. That section is the syntax. This one is the semantics.

## 1. The harness is the only way you can change anything

The tools your own runtime gives you are configured NON-MUTATING for this seat. Depending on
which transport is running you, that is enforced by a read-only sandbox on your process or by
an allowlist that admits only read-style tools -- but the effect is the same in every case the
harness currently launches: **a write you attempt through your own tooling does not reach the
working directory.** It is not slowed down or reviewed. It does not arrive.

The action envelope is the only path to disk. Emitting `WRITE:` with a CONTENT block is not a
request for permission to write, and it is not a fallback for when your own tools are blocked.
It is the write.

If a future transport is added whose native tools CAN mutate, this section stops being true of
that transport, and the harness has a new hole rather than a new capability. Treat the property
as something the harness maintains, not as a fact about models.

## 2. The trap this file exists for: silence reads as "finished"

A response containing NO actions envelope ENDS YOUR TURN. That is the documented way to deliver
a final answer.

So if you conclude that you cannot do the task, and you say so in prose without emitting an
envelope, the harness does not hear a problem report. It hears a completed turn. The record
shows `(no actions)` and the turn closes with your explanation attached to nothing.

**This has happened.** A leader turn was given a task requiring a file to be written, reported
in prose that it could not write because its sandbox forbade it, emitted no WRITE action, and
ended. The capability was present the whole time; the harness path was never tried. The same
leader had used the path correctly on a simpler task earlier in the same session, so this is
not a missing ability -- it is a decision made silently, and one that looks identical to
success from outside.

THE RULE: if the task calls for a change and you are not going to make it, EMIT THE ACTION AND
LET IT FAIL, or state the refusal as your final answer knowing it terminates the turn. What you
must not do is report an inability you never tested against the harness. "I could not write" in
a turn that emitted no WRITE is a claim about a mechanism you did not use.

## 3. What happens to a WRITE

A WRITE does not go straight to disk. It is submitted to the council FIRST, and the review
happens BEFORE anything is written -- so a rejected write never lands and never needs undoing.

- The council returns a verdict. A blocking verdict REFUSES the write outright. A PASS or a
  WARN permits it -- a WARN comes back with the concerns attached, and they are yours to answer
  in a later round.
- PERMITTED IS NOT THE SAME AS APPLIED, and conflating the two is how a leader ends up
  believing it wrote a file it did not. The verdict decides whether the write MAY land; the
  turn's mode, below, decides whether it DOES.
- The verdict and the review text are returned to you as the action's result, along with
  whether it was applied. You never have to infer either.

The operator also selects a mode for the turn, and it constrains what a permitted write does:

| mode | what a council-permitted write does |
|---|---|
| `auto` | applied |
| `approve-each` | the operator is asked first, seeing target, verdict and review |
| `plan-only` | never applied -- the council still reviews, so the verdict is visible |

`plan-only` is the one worth internalising: your write will be reviewed, you will get a verdict
back, and NOTHING WILL BE WRITTEN. That is not a failure and not a reason to retry through
another route. There is no other route.

## 4. Reading a result honestly

Every action returns a note. Distinguish three outcomes that are easy to blur:

- **Denied by the harness** -- the action was refused before running (a path outside the
  working directory, a host not on the fetch allowlist, a sandbox that could not be honoured).
  The reason is in the note. This is a boundary, not a bug, and retrying the identical action
  will be denied identically.
- **Ran and failed** -- the action executed and the thing it did returned an error. The output
  is yours to read. This is ordinary work.
- **Refused by the council** -- a write drew a blocking verdict. The reasons come back. Answer
  them or change the work; do not re-submit the same content unchanged.

Only the second is a problem you can fix by trying harder. Reporting the first or third as
though the harness malfunctioned misdescribes the run to whoever reads the record.

## 5. What the record will show

Your turn is recorded as an ordered list of actions and their notes, plus your prose. A reader
comparing your prose against that list can see whether the two agree, so a turn whose prose
describes work the action list does not contain is a DETECTABLE state rather than an ambiguous
one.

Be clear about who does that detecting today: a person, reading the record. No automatic
validation compares your prose against your actions -- a response with no envelope simply ends
the turn as a final answer, whatever the prose says. So this section is not a warning that you
will be caught. It is the reason the preceding sections matter: the discrepancy is visible to
anyone who looks, and nothing stops you creating one. Write prose the record would corroborate.
