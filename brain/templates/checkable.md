---
type: checkable
id: short-kebab-case-id
check_kind: file
check_path: path/relative/to/--root
expect: an exact substring that must appear
falsifier: what this check prints if the fact is FALSE
supersedes: []
superseded_by:
tags: []
---

Optional prose below the frontmatter is a GLOSS. It is for a human reader and is
never citable as the fact. The FACT is generated from the check above -- you do not
write it, and there is no field to write it in.

`falsifier` is required and the validator CANNOT check it. Its job is to make you
answer, before you save: "if this fact were false, what would this check print?" If
the answer is "the same thing", the check establishes nothing and you have written a
note that will pass forever while meaning nothing.

There is no `verified` field. A note cannot assert its own freshness. Verification
lives in `.attestations.json`, which only the validator writes, and any edit to the
check drops the old attestation rather than inheriting it.

There is no `stream` field either, and the validator REJECTS one. The sandbox
returns combined stdout+stderr with no way for a caller to separate them, so a
stream selector would be a promise the code cannot keep. Write command checks to be
unambiguous in combined output.

The three check kinds:

  check_kind: file      -> check_path, read relative to --root.

  check_kind: url       -> check_url, fetched through the engine's hardened fetcher.
                           LIMIT: only hosts on the engine's exact-host allowlist are
                           reachable. For any other source, stage the data locally and
                           use check_kind: file -- and understand that you are then
                           attesting the LOCAL ARTIFACT, not the remote source. That
                           is a weaker claim; write the note so it says so.

  check_kind: command   -> check_argv (a JSON ARRAY, never a shell string) plus a
                           REQUIRED exit_status. Runs ONLY under --run-commands and
                           ONLY inside the engine's bubblewrap sandbox, which has NO
                           NETWORK -- a command check is for facts about the local
                           tree, never about a remote source.

A command example, with every required field:

  ---
  type: checkable
  id: engine-has-no-undefined-names
  check_kind: command
  check_argv: ["python3", "-m", "pyflakes", "council/consult_council.py"]
  expect: ""
  exit_status: 0
  falsifier: an undefined name prints "undefined name '<x>'" and exit status becomes 1
  ---

Note what makes that example honest: an empty `expect` would match anything, so the
whole discriminating power sits in `exit_status: 0`, and the falsifier says exactly
what a false fact would look like. A check that cannot fail is worse than no note,
because the ledger will attest it and it will look authoritative.
