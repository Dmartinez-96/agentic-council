#!/usr/bin/env bash
# Load the council's environment, then exec the real hook.
#
# WHY THIS EXISTS. Hooks run as children of the Claude Code or Codex process and inherit whatever
# environment it was launched with. A common place to export OPENROUTER_API_KEY is ~/.bashrc,
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
# wrapper removes the dependency instead. The private env file may also hold other
# council-specific overrides; a nonempty caller OPENROUTER_API_KEY takes precedence.
#
# The optional first pair, --harness claude|codex, selects process-local
# roster/state defaults. It never rewrites roster.json, so simultaneous Claude
# and Codex sessions cannot race by changing a shared configuration file.
#
# NOT A SECRET-SCRUBBING BOUNDARY. This ADDS the key the members need; it removes nothing.
# What a member subprocess inherits is `_member_env()`'s question -- it strips VSCODE_*,
# TERM_PROGRAM* and everything in MEMBER_SCRUB_ENV.
#
# `exec`, so the hook keeps this process's pid, stdin, stdout and exit status. A hook
# communicates through all four -- the payload arrives on stdin, the harness reads the exit
# status -- so running it as a CHILD and forwarding by hand would be a second place for those
# to drift. There is no such place.
#
# The env file is deliberately separate from shell rc files, so no secret needs to sit in a
# world-readable dotfile. Keep it mode 600.
set -u

_council_harness=claude
if [ "${1:-}" = "--harness" ]; then
    if [ "$#" -lt 3 ]; then
        echo "hook_env.sh: --harness requires claude or codex plus a command" >&2
        exit 2
    fi
    _council_harness=$2
    shift 2
fi
case "$_council_harness" in
    claude|codex) ;;
    *)
        echo "hook_env.sh: unknown harness $_council_harness" >&2
        exit 2
        ;;
esac

_council_saved_openrouter=${OPENROUTER_API_KEY:-}
if [ -r "$HOME/.config/council/env" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.config/council/env"
fi
if [ -n "$_council_saved_openrouter" ]; then
    OPENROUTER_API_KEY=$_council_saved_openrouter
    export OPENROUTER_API_KEY
fi

_council_here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

# An inherited harness identity came from an outer council member subprocess,
# not from this CLI's operator. Drop only the harness-derived values; explicit
# operator overrides without COUNCIL_HARNESS remain authoritative.
if [ -n "${COUNCIL_HARNESS:-}" ] && [ "$COUNCIL_HARNESS" != "$_council_harness" ]; then
    unset COUNCIL_ROSTER_PATH COUNCIL_STATE_ROOT
fi
COUNCIL_HARNESS=$_council_harness
export COUNCIL_HARNESS

if [ -z "${COUNCIL_ROSTER_PATH:-}" ]; then
    COUNCIL_ROSTER_PATH="$_council_here/roster.${_council_harness}-led.json"
    export COUNCIL_ROSTER_PATH
fi
if [ -z "${COUNCIL_STATE_ROOT:-}" ]; then
    if [ "$_council_harness" = codex ]; then
        COUNCIL_STATE_ROOT="$HOME/.codex/state"
    else
        COUNCIL_STATE_ROOT="$HOME/.claude/state"
    fi
    export COUNCIL_STATE_ROOT
fi
if [ -z "${COUNCIL_STANDING_RULES_PATH:-}" ]; then
    if [ -f "$HOME/.config/agentic-council/standing-rules.md" ]; then
        COUNCIL_STANDING_RULES_PATH="$HOME/.config/agentic-council/standing-rules.md"
    fi
    export COUNCIL_STANDING_RULES_PATH
fi
if [ -z "${COUNCIL_BRAIN_VAULT:-}" ] && [ -d "$_council_here/_brain" ]; then
    COUNCIL_BRAIN_VAULT="$_council_here/_brain"
    export COUNCIL_BRAIN_VAULT
fi

exec "$@"
