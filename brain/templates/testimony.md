---
type: testimony
id: short-kebab-case-id
statement: what was said or decided, in one line
attributed_to: who said it
date: 2026-01-01
citable_as_ground_truth: false
supersedes: []
superseded_by:
tags: []
---

TESTIMONY records that someone said or decided something. It does NOT record that
the thing is true, and the validator pins `citable_as_ground_truth: false` so a note
cannot quietly promote itself.

Use it for the things a check cannot reach: a decision ("we ship the fixed allowlist
in v1"), a constraint someone imposed, a preference, a report from a person. These
are real and worth keeping. They are just not observations, and the whole point of
having two types is that the difference stays visible.

WHAT DOES NOT BELONG HERE: a judgment dressed as a record. "This flag is immaterial"
is not testimony about the world, it is an opinion, and attributing it to someone
does not make it a fact. If you find yourself reaching for testimony to store a
conclusion, the conclusion belongs in prose, and the prose should link to whatever
checkable notes it rests on.

There is no third note type. Reasoning -- design rationale, research synthesis,
lessons -- lives in prose documents, not in this vault. The vault holds the FACT
layer; prose holds the argument and links here for its numbers.
