#!/usr/bin/env bash
# Load the council's environment, then exec the real hook.
#
# WHY THIS EXISTS. Hooks run as children of the Claude Code process and inherit whatever
# environment IT was launched with. A common place to export OPENROUTER_API_KEY is ~/.bashrc,
# and a ~/.bashrc that returns early for non-interactive shells never runs that export for a
# hook child -- so whether the council saw the key depended on how Claude Code happened to be
# started. Launched from an interactive shell it worked; launched from a desktop menu, a
# service, or a session predating the export, it did not.
#
# WHAT THAT COSTS, WHICH IS THE POINT: not an error, but a council that still looks like one.
# Every OpenRouter-routed seat is dropped for a fire when the key is absent, and the drop is
# announced on stderr and recorded nowhere -- a skipped seat is removed from the dispatch list
# before any member runs, so it leaves no entry in the log at all. On a roster where only codex
# is non-OpenRouter, that yields a single voting member. BLOCK then becomes unreachable by
# arithmetic, whatever that member finds -- and the reason is worth stating exactly, because
# the obvious reading gets it backwards. The quorum is ceil(n/2) over the CONFIGURED voting
# roster, not over the seats that actually reported: the skip removes a seat from a local
# dispatch list while the quorum is computed from the registry. Six configured seats therefore
# keep a quorum of 3 even when one seat runs. (Were it computed from reporters, ceil(1/2)
# would be 1 and a lone survivor could auto-revert files -- the opposite conclusion, from the
# same formula.) Verified by running the degraded case: one BLOCK -> WARN, three -> BLOCK.
# Observed once during development: a twelve-seat roster logged members ['codex'],
# round1 ['codex'], shadow [], verdict WARN.
# The README documents the launch-context problem and tells you to fix your environment; this
# wrapper removes the dependency instead.
#
# ONE VARIABLE, AND ONLY WHEN UNSET OR EMPTY. This sets OPENROUTER_API_KEY and nothing else.
# The test is `[ -z "${OPENROUTER_API_KEY:-}" ]`, so it fills an UNSET variable and equally an
# explicitly-EMPTY one -- a caller who deliberately exported the empty string DOES get
# overridden. That is intended, since an empty key authenticates nothing, but it is stated
# because "never overrides a value the caller chose" would be false of `-z`. A non-empty value
# is always passed through untouched. This is not a general environment loader and should not
# grow into one without a reason stated here.
#
# NOT A SECRET-SCRUBBING BOUNDARY. This ADDS the key the members need; it removes nothing.
# What a member subprocess inherits is `_member_env()`'s question -- it strips VSCODE_*,
# TERM_PROGRAM* and everything in MEMBER_SCRUB_ENV.
#
# `exec`, so the hook keeps this process's pid, stdin, stdout and exit status. A hook
# communicates through all four -- the payload arrives on stdin, Claude Code reads the exit
# status -- so running it as a CHILD and forwarding by hand would be a second place for those
# to drift. There is no such place.
#
# The key file is deliberately separate from the shell rc files, so one 600-mode file is the
# single source and no secret needs to sit in a world-readable dotfile.
set -u

if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -r "$HOME/.config/council/env" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.config/council/env"
fi

exec "$@"
