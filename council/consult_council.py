#!/usr/bin/env python3
"""Workers' Council wrapper (advisory mode).

Runs configured council members (default: codex + gemini) in parallel
as critics of a proposal, reports each member's raw verdict, and emits
a final consensus verdict. PASS only when every member returns
`VERDICT: PASS`. Anything else -> WARN.

The wrapper is advisory: WARN does not deny anything. The hook script
that invokes this wrapper on a Claude Code PostToolUse event surfaces
the WARN text to Claude via the hook's structured `additionalContext`
output, and Claude is expected to read it and revise / revert / proceed
as appropriate.

External verdicts can be added with `--external-verdict NAME=PATH`
(e.g. from a Claude Agent-tool substitute when a built-in critic
errors).
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# All council scripts are installed side by side in one directory, so a
# script's own directory is the council root. Deriving it here (rather
# than hardcoding an absolute path) is what lets the package install
# anywhere; see install.py, which copies every script into one dir.
COUNCIL_ROOT = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = COUNCIL_ROOT / "council_system_prompt.md"
LOGS_ROOT = COUNCIL_ROOT / "logs"
# codex is the ONLY subprocess council member. It is launched in
# _run_subprocess with this dir prepended to PATH; the dir holds a `git`
# stub that exits non-zero, so a bare `git ...` resolves to the stub and
# fails (verified: exit 127). Verified scope and limits:
#  - codex --sandbox read-only blocks git writes: `git checkout -b` and
#    a file edit both failed with "read-only filesystem" (tested). That
#    is the protection against branch checkout / edits.
#  - gemini and deepseek are stateless HTTP calls with no subprocess and
#    no tools, so this stub is irrelevant to them.
#  - The PATH stub only shadows a bare `git`; an absolute-path call
#    (/usr/bin/git) bypasses it and still runs (verified). So an
#    absolute-path READ git by codex is not blocked by the stub; the
#    read-only sandbox still prevents any write through that path.
#
# This block used to claim gemini was a subprocess member made safe by
# `--approval-mode plan`, "(tested)". That was BOTH stale and false, and it
# is the most dangerous comment this file has carried: it is exactly the
# justification a future reader would use to restore a CLI transport. The
# agentic gemini CLI (agy) rewrote six council files plus settings.json
# during a read-only review -- see the removal rationale below at the
# GEMINI_API_URL definition. A council member must never be able to mutate
# state. Do not reintroduce a tool-capable transport for any member.
NOGIT_DIR = COUNCIL_ROOT / "_nogit"

# gpt-5.6-sol: the flagship tier of the GPT-5.6 family (GA 2026-07-09;
# tiers are sol > terra > luna). Verified on this machine: it requires
# codex-cli >= 0.144.1 (0.130.0 returned "requires a newer version of
# Codex"), and the bare `gpt-5.6` alias is rejected on ChatGPT-account
# auth ("not supported when using Codex with a ChatGPT account"), so the
# full tier id must be pinned. Emits a clean parseable VERDICT line.
CODEX_MODEL = "gpt-5.6-sol"
CODEX_REASONING = "high"

# DeepSeek member. Verified against the live API and api-docs.deepseek.com
# this session: POST https://api.deepseek.com/chat/completions, Bearer
# auth, OpenAI-compatible body, answer in choices[0].message.content,
# reasoning in a separate reasoning_content field, thinking enabled by
# default. Requires DEEPSEEK_API_KEY in the environment; main() drops
# the member at runtime when the key is absent.
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-pro"
# "max" is DeepSeek's Think Max tier, the deepest reasoning mode the API
# exposes. Verified live this session: reasoning_effort="max" returns HTTP
# 200, and an invalid value returns 400 naming the exact enum -- `high`,
# `low`, `medium`, `max`, `xhigh`. Per api-docs.deepseek.com only "high" and
# "max" are semantically distinct (low/medium map to high; xhigh maps to
# max), and "high" is the default, so this is a genuine one-notch upgrade.
#
# Think Max is a PARAMETER VALUE, not a model id -- there is no
# deepseek-v4-pro-max. The server injects the Think Max system prompt for
# you: on one probe pair here, an identical request went from 5 to 84
# prompt_tokens under "max". So "max" adds a billed prompt-token overhead
# (+79 on that probe; whether that size is invariant across prompts was not
# measured). It is small but not free, and the bulk of the added cost is the
# deeper reasoning trace, billed at the same per-token rate as "high".
#
# Tradeoff: latency. Across 117 logged runs today (logs/2026-07-13/*.json)
# deepseek is already the slowest member -- median 22.25s vs gemini 13.41s
# and codex 6.55s -- and a probe showed "max" roughly doubling its time on a
# critic-style prompt. Set this back to "high" if per-edit council latency
# starts costing more than the extra depth is worth.
DEEPSEEK_REASONING = "max"

# Gemini member: the direct Gemini REST API (generateContent), used when
# GEMINI_API_KEY is set; otherwise main() drops gemini from the roster.
# It is a single stateless HTTP call (like the deepseek member), so it is
# fast, predictable, and -- critically -- CANNOT touch the filesystem.
#
# The earlier agy / Antigravity-CLI gemini path was REMOVED for safety.
# agy is an agentic coding tool with full filesystem write access; the
# empty member_cwd only sets the working directory, it does not sandbox
# file access. As a council "critic" running a review, agy autonomously
# read council files by absolute path and rewrote six of them (plus
# settings.json) in a single review, then reported the edits as done. A
# council member must never be able to mutate state. An HTTP completion
# cannot; an agentic CLI can. So gemini now runs ONLY via the API, and is
# dropped (not fallen back to agy) when the key is absent.
#
# Endpoint/auth/body verified this session against ai.google.dev: POST
# https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent
# with an `x-goog-api-key` header and a {"contents":[{"parts":[{"text"}]}]}
# body; the reply text is candidates[0].content.parts[].text. Implicit
# context caching is on by default for gemini-2.5+ (no code change), and
# build_prompt places the stable system prompt first, so the repeated
# prefix is cached automatically.
#
# Model id re-verified live this session: `gemini-3.5-flash` is GA and
# resolves (modelVersion echoes back "gemini-3.5-flash"). Do NOT "upgrade"
# this to gemini-3.5-pro: that id is a hard 404 on the Developer API today
# ("models/gemini-3.5-pro is not found for API version v1beta"). The
# highest Pro tier that actually resolves is gemini-3.1-pro-preview, which
# is preview-only and materially more expensive; see HANDOFF.md.
GEMINI_API_MODEL = "gemini-3.5-flash"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_API_MODEL + ":generateContent"
)

# We pin thinkingLevel so the member's reasoning depth is an explicit,
# stable choice rather than an inherited default that can shift under us
# between model revisions.
#
# Verified live this session against gemini-3.5-flash. The request path is
# generationConfig.thinkingConfig.thinkingLevel. A bogus value returns HTTP
# 400 naming the enum type
# (google.ai.generativelanguage.v1beta.ThinkingConfig.ThinkingLevel), so
# the field is genuinely read, not silently ignored. Allowed on 3.5-flash:
# minimal | low | medium | high. Thought tokens on one critic-style prompt
# rose monotonically with the level -- minimal 0, low 758, medium 1110,
# high 1396 -- which is consistent with the level modulating depth (one
# sample per level, so a trend, not a measurement).
#
# Two honest caveats. (1) Omitting the field entirely produced 1455 thought
# tokens on that same prompt, i.e. MORE than "high", even though the docs
# describe the 3.5 default as "medium". Those are single samples and thought
# counts are stochastic, so do not read too much into it -- but it means
# pinning "high" is NOT established to buy more thinking than the default
# would have. The value here is determinism, not extra depth. (2) None of
# this shows "high" yields better criticism; it shows the API accepts and
# acts on the field. Quality was not measured.
GEMINI_THINKING_LEVEL = "high"

VERDICT_RE = re.compile(r"^VERDICT:\s*(PASS|WARN|BLOCK)\s*$", re.MULTILINE)

PER_CRITIC_TIMEOUT_S = 600
PITCH_LOG_MAX_BYTES = 200_000

ALL_MEMBERS = ("codex", "gemini", "deepseek")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_verdict(text: str) -> str:
    """The member's verdict, or UNPARSEABLE.

    AMBIGUITY IS NOT RESOLVED, IT IS REPORTED.

    The old implementation used VERDICT_RE.search, taking the FIRST match
    anywhere in the response. That silently misattributes: a member that quotes
    a peer's verdict line and then disagrees with it in prose has the PEER'S
    vote recorded as its own, and it looks like a genuine vote. Demonstrated
    live -- a response quoting codex's "VERDICT: PASS" and then arguing against
    it parsed as PASS.

    Two guards, and both are the same principle: when the response does not say
    unambiguously what this member voted, ASK, do not guess. For a BUILT-IN
    member, UNPARSEABLE routes to the formatting-only retry, which asks it for
    the one line it meant, so the guard costs a call rather than a vote. An
    EXTERNAL verdict (--external-verdict) has no runner to ask, so there the
    guard does cost the vote -- and it is reported as a lost vote rather than
    silently guessed at.

      1. The verdict must be the FIRST non-empty line. That is already what the
         system prompt demands ("Your VERDICT line must be the FIRST line"), so
         this makes the parser agree with the instruction instead of quietly
         accepting a token from anywhere in the prose. Measured over 17,456
         logged responses before imposing it: 98.4% already comply, 1.0% put
         the verdict later (those now take a retry, which recovers the vote),
         and 103 had no verdict line at all.
      2. Verdict lines that DISAGREE with each other are ambiguous no matter
         where they sit, so they are refused too. Of those 17,456 responses, 2
         carry more than one verdict line and 0 carry conflicting ones -- so
         this guard closes a hole without adding noise today.

    Not fully closed, and worth being honest about: if a member's FIRST line is
    itself a quoted peer verdict, that is indistinguishable from its own vote
    and will still be taken. The system prompt's "never reproduce a peer's
    VERDICT: line" rule remains the only defence there.
    """
    if not text:
        return "UNPARSEABLE"
    hits = VERDICT_RE.findall(text)
    if not hits or len(set(hits)) > 1:
        return "UNPARSEABLE"          # absent, or conflicting: ask, do not guess
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return "UNPARSEABLE"
    m = VERDICT_RE.match(lines[0].strip())
    if not m:
        return "UNPARSEABLE"          # verdict is not the first line: ask
    return m.group(1)


# The regex is deliberately NOT loosened. Counted from logs/ this session:
# 18,598 member-votes recorded, of which 102 are UNPARSEABLE and 1,179 ERROR.
# Each UNPARSEABLE is a vote that was silently thrown away.
#
# The obvious fix -- accept any line starting with "VERDICT:" -- is a trap. A
# silently discarded vote is bad; a silently MISATTRIBUTED one is worse, because
# it looks like a real vote. parse_verdict above therefore got STRICTER, not
# looser (first non-empty line only, and conflicting lines refused).
#
# Instead of a looser parser: one formatting-only retry, then fail LOUD.
VERDICT_REFORMAT_SYSTEM = (
    "You fix formatting. You do not review, judge, or reconsider anything."
)

VERDICT_REFORMAT_PROMPT = """Your previous response to a council review did not
contain a parseable verdict line, so your vote was DISCARDED and the council
lost it entirely.

This is a formatting repair, not a re-review.

The parser requires the verdict token to stand ALONE on its own line, exactly
one of:
VERDICT: PASS
VERDICT: WARN
VERDICT: BLOCK

No trailing words, no parentheses, no qualifiers on that line.

Below is your own previous response. Do NOT re-evaluate the proposal. Do NOT
change your position. Do NOT add reasoning. Read the position you already took
and emit ONLY the single verdict line that expresses it. Output nothing else.

If your previous response genuinely never reached a position, emit
VERDICT: WARN.

--- your previous response ---
{text}
--- end of your previous response ---
"""


async def reformat_unparseable(results: list[dict], cwd: Path) -> list[dict]:
    """One formatting-only retry for any member whose verdict did not parse.

    Recovers the vote when the member took a position but wrote the line wrong.
    Costs one extra call, and only on a fire where a verdict failed to parse.

    This is CONTEXT-RESTRICTED. The runner is called as
    runner(prompt, VERDICT_REFORMAT_SYSTEM, cwd), leaving evidence_block,
    user_directives_block and round1_block at their empty defaults, so nothing
    re-SUPPLIES the proposal, the diff, the evidence or the peer verdicts.

    That reduces the risk of a fresh review; it does not eliminate it, and
    codex was right to refuse both stronger wordings I tried. The member's own
    prior text is the input, and that text can itself quote the proposal or the
    evidence -- so the merits can still be present, and a fresh call can still
    reinterpret its own ambiguous prose. (Of the 40 responses replayed below,
    none contained the literal strings "Proposal under review" or "Session
    evidence". That is a narrow substring check on two headers, and NOT a
    finding that they quoted nothing of the proposal; I did not measure that.)

    So position preservation is measured, not guaranteed. Replaying 40 real
    historical responses that the first-line rule newly rejects: the retry
    recovered a parseable verdict 40/40 and preserved the originally recorded
    position 40/40. Sample composition, checked rather than assumed: PASS 23,
    WARN 16, BLOCK 1; gemini 30, deepseek 10. Strong evidence, not a proof.

    Mutates and returns `results`.
    """
    async def retry(r: dict) -> None:
        runner = MEMBER_RUNNERS.get(r.get("role", ""))
        if runner is None:
            return
        prompt = VERDICT_REFORMAT_PROMPT.format(text=(r.get("text") or "")[:8000])
        try:
            out = await runner(prompt, VERDICT_REFORMAT_SYSTEM, cwd)
        except Exception as e:  # noqa: BLE001
            r["reformat_error"] = str(e)
            return
        v = parse_verdict(out.get("text") or "")
        if v == "UNPARSEABLE":
            r["reformat_failed"] = True
            return
        r["verdict"] = v
        r["reformatted"] = True

    targets = [r for r in results if r.get("verdict") == "UNPARSEABLE"]
    if targets:
        await asyncio.gather(*[retry(r) for r in targets])
    return results


# Tier 1: events rendered IN FULL (args + output tails).
EVIDENCE_MAX_EVENTS = 250

# Total byte budget for the whole evidence block, both tiers. Unchanged: the
# two-tier split reallocates this budget, it does not enlarge it.
EVIDENCE_MAX_BYTES = 120_000

# Tier 2 (the one-line index) gets a fixed slice of that budget, and tier 1 is
# rendered into whatever remains. The split is what stops the index from
# evicting the detail. A first draft capped the index at a made-up 1500 EVENTS
# instead of bytes; gemini did the arithmetic and caught that it would blow the
# whole budget, and the measurement backed it up -- across 75,093 real events an
# index line averages 107 bytes, so 1500 of them is ~161 KB, i.e. 134% of the
# entire block. The index would have head-truncated the full-detail tier out of
# existence. Hence a byte cap, not an event count.
#
# 25 KB buys roughly 230 index lines at the measured average. That does not
# reach the head of a very long session (sessions here run to a p90 of ~1400
# events and a max of ~6400), so the index is itself bounded and the preamble
# still tells members that absence is not proof.
EVIDENCE_INDEX_MAX_BYTES = 25_000

# The N most recent events are always rendered in full, whatever file they
# touch. Rationale, stated as the design choice it is rather than as a claim
# about what "usually" happens: a member cannot ask for an output it cannot
# see, and the run immediately before an action is the cheapest place to look
# for the justification of that action.
EVIDENCE_RECENT_FULL = 40
USER_DIRECTIVES_MAX_MESSAGES = 20
USER_DIRECTIVES_MAX_BYTES = 40_000
USER_DIRECTIVES_PER_MSG_TAIL_CHARS = 6_000
# Assistant-message window mirrors the user-directive window budget.
ASSISTANT_MAX_MESSAGES = USER_DIRECTIVES_MAX_MESSAGES
ASSISTANT_MAX_BYTES = USER_DIRECTIVES_MAX_BYTES
ASSISTANT_PER_MSG_TAIL_CHARS = USER_DIRECTIVES_PER_MSG_TAIL_CHARS


def _ask_user_question_directives(evidence_path: Path) -> list[tuple[str, str]]:
    """Extract AskUserQuestion answers from the per-session evidence
    file as (timestamp, directive_text) tuples.

    Verified against a real hook payload in this build: evidence_logger
    records AskUserQuestion events as
    {"tool": "AskUserQuestion", "at": <iso>, "answers": {<question>: <answer>}, ...}
    where "answers" maps each question's text to the user's selected
    option label or free-text answer. A user's menu selection is a
    directive and belongs in this block per the user's routing choice.
    """
    if evidence_path is None or not evidence_path.exists():
        return []
    try:
        lines = evidence_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("tool") != "AskUserQuestion":
            continue
        answers = ev.get("answers")
        if not isinstance(answers, dict) or not answers:
            continue
        pieces = ["User menu selections (AskUserQuestion):"]
        for q, a in answers.items():
            pieces.append(f"  Q: {q}")
            pieces.append(f"  A: {a}")
        out.append((ev.get("at", ""), "\n".join(pieces)))
    return out


def format_user_directives(transcript_path: Path,
                           evidence_path: Path | None = None) -> str:
    """Format the last N user directives as a markdown block. Returns
    empty string on any failure or absence.

    Two directive sources are merged and sorted by timestamp:

    1. Plain user messages from the transcript. The Claude Code hooks
       documentation (mirrored locally in the plugin-dev hook-development
       SKILL.md) documents transcript_path as a common hook field; the
       schema below is verified empirically by
       Bash inspection of an actual transcript in this session: the
       file is newline-delimited JSON; user turns are
       `{"type": "user", "message": {...}}`; message.content is either
       a string or a list whose item types include `text` and
       `tool_result`. This function takes string content and `text`
       items, and skips `tool_result` items.
    2. AskUserQuestion answers from the per-session evidence file (see
       `_ask_user_question_directives`). A menu selection is a user
       decision; per the user's routing choice it is surfaced in this
       block as well as in the evidence block.
    """
    items: list[tuple[str, str]] = []

    if transcript_path.exists():
        try:
            lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            otype = obj.get("type")

            # MID-TURN messages. When the user types while Claude is still
            # working, Claude Code does NOT record a {"type":"user"} turn: it
            # writes {"type":"queue-operation","operation":"enqueue",
            # "content":"<text>"} (mirrored in a {"type":"attachment"} record
            # whose attachment.prompt holds the same text). Verified by reading
            # this session's transcript.
            #
            # Reading only type=="user" therefore DROPS every mid-turn
            # instruction. That is not cosmetic. In this session the user
            # approved a feature mid-turn, the council never saw the approval,
            # and all three members accused Claude of FABRICATING it -- while
            # citing a directive from hours earlier as "the most recent". A
            # council judging compliance against stale orders will manufacture
            # exactly that kind of false accusation.
            #
            # Take queue-operation (not attachment) as the source: it holds the
            # text as a plain top-level string, and using both would double every
            # message.
            if otype == "queue-operation":
                if obj.get("operation") != "enqueue":
                    continue
                content = obj.get("content")
                if isinstance(content, str) and content.strip():
                    items.append((obj.get("timestamp", ""), content.strip()))
                continue

            if otype != "user":
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            text_parts: list[str] = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        t = item.get("text", "")
                        if isinstance(t, str):
                            text_parts.append(t)
            text = "\n".join(text_parts).strip()
            if not text:
                continue
            items.append((obj.get("timestamp", ""), text))

    items.extend(_ask_user_question_directives(evidence_path))

    if not items:
        return ""

    # Sort by timestamp string. The two sources use different ISO-8601
    # representations (verified this session: transcript uses
    # "...699Z" with millisecond precision and a Z suffix; evidence
    # uses "...537197+00:00" with microsecond precision and an offset).
    # Both are fixed-width and identical through the seconds field, so
    # lexicographic sort orders correctly to the second; ordering below
    # the second between the two sources can be imprecise, which is
    # immaterial for a "recent directives" window.
    items.sort(key=lambda it: it[0])

    # Dedup is REQUIRED, and measured: in this session 17 texts appear as BOTH a
    # queue-operation and a type=user turn. (Whether that is the queued message
    # being replayed once the queue drains, or the user genuinely saying the same
    # thing twice, is NOT established -- and does not matter, because latest-wins
    # is the right answer either way.) Without dedup the block shows those twice.
    #
    # Keep the LATEST occurrence, never the earliest. A user repeats directives
    # ("continue", "proceed", "keep going"), and collapsing those onto the FIRST
    # timestamp would date a live instruction to hours ago and let it fall out of
    # the recent window entirely -- deleting the newest directive in the name of
    # tidiness. The most recent utterance of a repeated order is the one that is
    # actually in force.
    seen_texts: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for ts, text in reversed(items):        # newest first
        if text in seen_texts:
            continue
        seen_texts.add(text)
        deduped.append((ts, text))
    deduped.reverse()                       # back to chronological
    items = deduped[-USER_DIRECTIVES_MAX_MESSAGES:]

    body_lines: list[str] = [
        "## Recent user directives",
        "",
        ("The following are the most recent user directives in this "
         "Claude Code session (typed messages and AskUserQuestion menu "
         "selections), in chronological order. Use these to check "
         "whether the proposal contradicts a user directive (e.g. the "
         "user asked for full verification but the proposal contains "
         "'out of scope' caveats), and to surface contradictions in the "
         "WARN or BLOCK reasons."),
        "",
    ]
    for ts, text in items:
        header = f"### {ts}" if ts else "### user directive"
        body_lines.append(header)
        if len(text) > USER_DIRECTIVES_PER_MSG_TAIL_CHARS:
            text = ("[head truncated]\n"
                    + text[-USER_DIRECTIVES_PER_MSG_TAIL_CHARS:])
        body_lines.append(text)
        body_lines.append("")

    block = "\n".join(body_lines).rstrip() + "\n"
    encoded = block.encode("utf-8", errors="replace")
    if len(encoded) > USER_DIRECTIVES_MAX_BYTES:
        tail_bytes = encoded[-USER_DIRECTIVES_MAX_BYTES:]
        block = ("## Recent user directives (head truncated)\n\n"
                 + tail_bytes.decode("utf-8", errors="replace"))
    return block


def _head_tail_chars(text: str, budget: int) -> str:
    """Keep the head and tail of an over-budget string, eliding the
    middle, so neither end is lost."""
    if len(text) <= budget:
        return text
    head = budget // 3
    tail = budget - head
    elided = len(text) - head - tail
    return text[:head] + f"\n[... {elided} chars elided ...]\n" + text[-tail:]


def format_assistant_messages(transcript_path: Path) -> str:
    """Recent assistant (Claude) text messages from the transcript, kept
    as a block distinct from user directives. Returns empty string when
    the transcript is missing, unreadable, or has no assistant text."""
    if not transcript_path.exists():
        return ""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    items: list[tuple[str, str]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        text_parts: list[str] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    t = item.get("text", "")
                    if isinstance(t, str):
                        text_parts.append(t)
        text = "\n".join(text_parts).strip()
        if not text:
            continue
        items.append((obj.get("timestamp", ""), text))
    if not items:
        return ""
    items = items[-ASSISTANT_MAX_MESSAGES:]
    body_lines: list[str] = [
        "## Claude's own claims (UNDER REVIEW -- not evidence, not directives)",
        "",
        ("This section is Claude's own recent messages. It is deliberately "
         "placed AFTER the evidence, because it used to sit inside the Recent "
         "user directives block and ahead of the facts, which let Claude's "
         "framing anchor you before you had seen anything."),
        "",
        ("Treat every sentence here as a CLAIM UNDER REVIEW, at exactly the "
         "same standard as the proposal itself. Claude asserting something "
         "confidently is not evidence that it is true, and Claude having "
         "already reasoned his way to a conclusion is not a reason for you to "
         "start from that conclusion. If a statement here is load-bearing and "
         "the evidence block does not support it, that is a finding, not a "
         "premise."),
        "",
        ("Use it for INTENT -- what Claude was trying to do, and what he says "
         "he checked -- and then verify the checking against the evidence. Do "
         "not inherit his framing of what the problem is."),
        "",
    ]
    for ts, text in items:
        body_lines.append(f"### {ts}" if ts else "### assistant")
        body_lines.append(_head_tail_chars(text, ASSISTANT_PER_MSG_TAIL_CHARS))
        body_lines.append("")
    block = "\n".join(body_lines).rstrip() + "\n"
    encoded = block.encode("utf-8", errors="replace")
    if len(encoded) > ASSISTANT_MAX_BYTES:
        # The warning header must survive truncation. This path used to re-emit
        # the old neutral header, so on a long session the claims-under-review
        # framing silently vanished (codex and gemini caught that).
        #
        # The header is CHARGED AGAINST the budget, not prepended after the
        # slice. Prepending after slicing to the cap is guaranteed to overshoot
        # it. This is the SAME bug already fixed in format_evidence_block (see
        # its trunc_header / utf8_slack handling, consult_council.py:951-958)
        # and reintroduced here a few hours later -- a repeat, which is exactly
        # the kind of thing that belongs in a standing rule, not a lesson I
        # relearn per-function.
        hdr = ("## Claude's own claims (UNDER REVIEW -- not evidence, not "
               "directives; most recent kept)\n\n"
               "Every sentence below is a CLAIM UNDER REVIEW, held to the same "
               "standard as the proposal. Claude asserting something "
               "confidently is not evidence that it is true. Use it for intent; "
               "verify the claims against the evidence block above.\n\n")
        hdr_bytes = hdr.encode("utf-8")
        # Measured, not guessed: slicing raw bytes can sever a multi-byte
        # character, which decodes to U+FFFD and re-encodes to THREE bytes, so
        # the slice can come back LARGER than the budget. Probing every possible
        # cut offset for 2-, 3- and 4-byte characters, the worst-case expansion
        # is 6 bytes. 8 covers it.
        utf8_slack = 8
        budget = max(ASSISTANT_MAX_BYTES - len(hdr_bytes) - utf8_slack, 0)
        tail_bytes = encoded[-budget:] if budget else b""
        block = hdr + tail_bytes.decode("utf-8", errors="replace")
    return block


def _event_file(ev: dict) -> str:
    """The file an event touches, if any."""
    return ev.get("file_path") or ev.get("notebook_path") or ""


def _mentions_target(ev: dict, target: str) -> bool:
    """True when a Bash command names the target file (path or basename)."""
    if not target:
        return False
    cmd = ev.get("command") or ""
    if not cmd:
        return False
    return target in cmd or os.path.basename(target) in cmd


def _is_tier1(ev: dict, target: str) -> bool:
    """Does this event get shown in full, rather than as an index line?

    Tier 1 is deliberately wider than "events touching the target file". The
    members asked for a targeted block, but the block is also where rule-3
    verification is credited, so demoting the wrong things would CAUSE the
    false WARNs this is meant to remove:

      - Events on the target file: the direct context for the edit.
      - Bash commands naming the target: the probes that justify it.
      - WebFetch: the fetched text is what a rule-3 claim actually gets
        credited against, so a one-line index of it is useless. Kept in full.
        (Strictly, a fetch is only PRIMARY when the URL itself is canonical --
        vendor docs, a licence file, a REST response. codex is right that the
        tool does not make it primary; the member still has to judge the
        source. But the text has to be present to be judged at all.)
      - WebSearch: NOT kept in full. It goes to the index. The bar is explicit
        that search snippets are SECONDARY, so they cannot credit a claim on
        their own, and the index still shows that the search happened and what
        was queried.

        The reason is that principle, and NOT any cost saving. I tried three
        cost arguments and codex demolished all three, each time by demanding
        the measurement instead of the story:
          1. "Searches are rare (11 of 584 events), so keeping them is
             negligible." Rarity is not cost.
          2. "Those 11 events are 32,725 bytes of raw payload, 27% of the
             block." True of the payload, but misleading about the block: the
             detail section is already AT its byte cap and head-truncated.
             Marginal rendered cost of putting WebSearch back in tier 1: -68
             bytes. Nothing.
          3. "Then the cost is displacement -- WebSearch tails push target-file
             evidence off the head." Measured the composition, not just the
             size: events surviving the rendered detail section, WebSearch
             demoted vs not, 50 vs 50. Displaced: ZERO.

        So demoting WebSearch buys no measurable budget on this session, and
        the code should not pretend otherwise. It is demoted for one reason
        only: a snippet cannot credit a rule-3 claim, so rendering it in full
        cannot settle anything a member is entitled to be settled by.

        That is not free. The index keeps the QUERY but drops the snippet text,
        and a snippet can carry the quote that points at the primary source. A
        member who wants it must ask. That is the trade being made here, stated
        rather than hidden. If a future session is search-heavy, the cost
        argument may also become real; today it is not, and saying so is
        cheaper than being caught believing it.
      - AskUserQuestion: authoritative for what the user actually directed, so
        it is never demoted. (Cheap in this session -- 2 events -- though that
        is one session, not a law about all sessions.)
    """
    tool = ev.get("tool", "")
    if tool in ("WebFetch", "AskUserQuestion"):
        return True
    f = _event_file(ev)
    if target and f and (f == target or os.path.basename(f) == os.path.basename(target)):
        return True
    return _mentions_target(ev, target)


def _index_line(ev: dict) -> str:
    """One compact line proving an event happened, without its payload."""
    tool = ev.get("tool", "?")
    at = (ev.get("at") or "")[11:19]        # HH:MM:SS
    if tool == "Bash":
        cmd = " ".join((ev.get("command") or "").split())[:90]
        rc = ev.get("exit_code")
        return f"- {at} Bash (exit {rc}): `{cmd}`"
    if tool in ("Read", "Write", "Edit", "NotebookEdit"):
        return f"- {at} {tool}: {_event_file(ev)}"
    if tool in ("Grep", "Glob"):
        return f"- {at} {tool}: {str(ev.get('args', ''))[:90]}"
    if tool == "WebFetch":
        return f"- {at} WebFetch: {ev.get('url', '')}"
    if tool == "WebSearch":
        return f"- {at} WebSearch: {ev.get('query', '')}"
    return f"- {at} {tool}"


def format_evidence_block(evidence_path: Path, target_path: str = "") -> str:
    """Read a per-session JSONL evidence file and format as a markdown block.

    Two tiers. Tier 1 is the events relevant to the proposal, rendered in full.
    Tier 2 is a one-line index of every other event.

    The members asked for this: all three called the flat block noisy, and said
    the one check that mattered had often scrolled out of it. But a naive
    relevance FILTER would have made things worse, because the bar tells them
    "absence is weak proof" precisely because the block is truncated -- and a
    filtered block makes absence look like proof while hiding the evidence
    that would refute it. The index is what resolves that: a demoted event is
    still visibly THERE, so a member can see the check happened and ask for it,
    instead of concluding it never did.

    It also buys reach. Full rendering is capped at EVIDENCE_MAX_EVENTS, but
    the index is one line each, so we can index far more history than we could
    ever render. An old-but-relevant probe that used to vanish off the head of
    the block now still shows up as a line.
    """
    if not evidence_path.exists():
        return ""
    try:
        lines = evidence_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    events: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not events:
        return ""
    # Partition. The most recent events are shown in full whatever they touch.
    recent = set(range(max(0, len(events) - EVIDENCE_RECENT_FULL), len(events)))
    tier1: list[dict] = []
    tier2: list[dict] = []
    for i, ev in enumerate(events):
        if i in recent or _is_tier1(ev, target_path):
            tier1.append(ev)
        else:
            tier2.append(ev)

    # Overflow from tier 1 is DEMOTED to the index rather than discarded. A
    # first draft wrote `tier1 = tier1[-EVIDENCE_MAX_EVENTS:]`, which silently
    # dropped the oldest relevant events: they left tier 1 and never reached
    # tier 2, so they vanished without a trace. gemini caught it. That bug
    # would have defeated the entire point of this change, which is that a
    # demoted event stays visible enough to be asked for.
    #
    # Demotion is not an absolute guarantee of visibility: the index is itself
    # byte-capped below, so on a very long session the oldest index lines are
    # still cut. The block says so, and the bar already tells members that
    # absence is weak proof. The claim here is only that overflow goes to the
    # index instead of being thrown away at this step.
    if len(tier1) > EVIDENCE_MAX_EVENTS:
        overflow = tier1[:-EVIDENCE_MAX_EVENTS]
        tier1 = tier1[-EVIDENCE_MAX_EVENTS:]
        tier2 = sorted(tier2 + overflow, key=lambda e: e.get("at") or "")

    scope = (f" Tier 1 holds the events relevant to `{target_path}`, plus all "
             f"web lookups and the most recent activity."
             if target_path else
             " Tier 1 holds web lookups and the most recent activity.")

    body_lines: list[str] = [
        "## Session evidence",
        "",
        ("The following tool calls happened earlier in this Claude Code "
         "session, in chronological order. Before flagging any claim in the "
         "proposal as unsourced or unverified, check whether this evidence "
         "already supports it. A Read of the cited file, a Bash command "
         "whose output supports the claim, or a WebFetch of a primary "
         "source (the fetched text is under Output) counts as "
         "primary-source verification. A WebSearch shows what was looked "
         "up but its result snippets are secondary, not primary-source "
         "proof of a fact. An AskUserQuestion answer is authoritative for "
         "what the user decided or directed, not for external facts about "
         "APIs, licenses, or code."),
        "",
        ("This block is in two tiers." + scope + " Everything else is listed "
         "as a one-line index at the end, WITHOUT its output. An indexed event "
         "still happened: if one of them looks like it would settle a claim, "
         "ask Claude to surface its full output rather than treating the claim "
         "as unverified. Absence of an output is not absence of the check."),
        "",
        ("PROVENANCE WARNING, and it is not hypothetical. Claude WRITES the "
         "commands in this block, so their output is not automatically "
         "independent of Claude. A Bash command that prints Claude's own "
         "reasoning, hypothesis, or draft text produces output that LOOKS like "
         "evidence and is not: it is Claude's claim, echoed by a shell. Two "
         "things here were OBSERVED, not theorised: a codex member reviewing "
         "this council quoted a line of the council's own SOURCE CODE into its "
         "stderr while returning a PASS, and Claude's own analysis of an error "
         "printed that error's text into this block, from where a member could "
         "quote it back. Text can circulate between Claude, this block, and a "
         "member's output without ever touching an independent fact."),
        "",
        ("So judge each event by what it INDEPENDENTLY establishes. A Read of a "
         "file, a command whose output comes from the system or a third party "
         "(a compiler, a test runner, an API response, nvidia-smi), or a "
         "WebFetch of a primary source -- those are evidence. A command whose "
         "output is merely text Claude chose to print, or a program Claude wrote "
         "that asserts its own conclusion, is NOT verification of that "
         "conclusion, however official the output looks. Ask what the machine "
         "would have said if Claude were wrong."),
        "",
    ]
    for ev in tier1:
        tool = ev.get("tool", "?")
        at = ev.get("at", "")
        header = f"### {at} - {tool}"
        body_lines.append(header)
        if tool == "Bash":
            body_lines.append(f"Command: `{ev.get('command', '')}`")
            if ev.get("description"):
                body_lines.append(f"Description: {ev['description']}")
            ec = ev.get("exit_code")
            if ec is not None:
                body_lines.append(f"Exit: {ec}")
            if ev.get("interrupted"):
                body_lines.append("Interrupted: true")
            stdout_tail = ev.get("stdout_tail", "")
            if stdout_tail:
                body_lines.append("Stdout (tail):")
                body_lines.append("```")
                body_lines.append(stdout_tail.rstrip("\n"))
                body_lines.append("```")
            stderr_tail = ev.get("stderr_tail", "")
            if stderr_tail:
                body_lines.append("Stderr (tail):")
                body_lines.append("```")
                body_lines.append(stderr_tail.rstrip("\n"))
                body_lines.append("```")
        elif tool == "Read":
            body_lines.append(f"File: {ev.get('file_path', '')}")
            offset = ev.get("offset")
            limit = ev.get("limit")
            if offset is not None or limit is not None:
                body_lines.append(f"Range: offset={offset} limit={limit}")
            out = ev.get("output_tail", "")
            if out:
                body_lines.append("Content (tail):")
                body_lines.append("```")
                body_lines.append(out.rstrip("\n"))
                body_lines.append("```")
        elif tool in ("Grep", "Glob"):
            body_lines.append(f"Args: {ev.get('args', {})}")
            out = ev.get("output_tail", "")
            if out:
                body_lines.append("Output (tail):")
                body_lines.append("```")
                body_lines.append(out.rstrip("\n"))
                body_lines.append("```")
        elif tool in ("Write", "Edit"):
            body_lines.append(f"File: {ev.get('file_path', '')}")
            if ev.get("content_digest"):
                body_lines.append("Content (digest):")
                body_lines.append("```")
                body_lines.append(ev["content_digest"].rstrip("\n"))
                body_lines.append("```")
            if ev.get("old_digest") or ev.get("new_digest"):
                body_lines.append("Old (digest):")
                body_lines.append("```")
                body_lines.append((ev.get("old_digest") or "").rstrip("\n"))
                body_lines.append("```")
                body_lines.append("New (digest):")
                body_lines.append("```")
                body_lines.append((ev.get("new_digest") or "").rstrip("\n"))
                body_lines.append("```")
        elif tool == "WebFetch":
            body_lines.append(f"URL: {ev.get('url', '')}")
            if ev.get("prompt"):
                body_lines.append(f"Prompt: {ev['prompt']}")
            out = ev.get("output_tail", "")
            if out:
                body_lines.append("Output (tail):")
                body_lines.append("```")
                body_lines.append(out.rstrip("\n"))
                body_lines.append("```")
        elif tool == "WebSearch":
            body_lines.append(f"Query: {ev.get('query', '')}")
            if ev.get("allowed_domains"):
                body_lines.append(f"Allowed domains: {ev['allowed_domains']}")
            if ev.get("blocked_domains"):
                body_lines.append(f"Blocked domains: {ev['blocked_domains']}")
            out = ev.get("output_tail", "")
            if out:
                body_lines.append("Output (tail):")
                body_lines.append("```")
                body_lines.append(out.rstrip("\n"))
                body_lines.append("```")
        elif tool == "AskUserQuestion":
            answers = ev.get("answers")
            if isinstance(answers, dict) and answers:
                body_lines.append("User answered:")
                for q, a in answers.items():
                    body_lines.append(f"  Q: {q}")
                    body_lines.append(f"  A: {a}")
            else:
                body_lines.append("(AskUserQuestion with no recorded answers)")
        elif tool == "ToolSearch":
            body_lines.append(f"Query: {ev.get('query', '')}")
            out = ev.get("output_tail", "")
            if out:
                body_lines.append("Matched tools (tail):")
                body_lines.append("```")
                body_lines.append(out.rstrip("\n"))
                body_lines.append("```")
        else:
            digest = ev.get("tool_input_digest", "")
            if digest:
                body_lines.append("Input (digest):")
                body_lines.append("```")
                body_lines.append(digest.rstrip("\n"))
                body_lines.append("```")
            out = ev.get("output_tail", "")
            if out:
                body_lines.append("Output (tail):")
                body_lines.append("```")
                body_lines.append(out.rstrip("\n"))
                body_lines.append("```")
            if not digest and not out:
                body_lines.append("(tool entry)")
        body_lines.append("")

    # --- Tier 2: the one-line index -------------------------------------
    # Built first so we know exactly how many bytes it costs, and rendered
    # newest-first into its own budget. Tier 1 then gets whatever is left of
    # EVIDENCE_MAX_BYTES, which is what stops the index from evicting the
    # detail it is supposed to complement.
    index_lines: list[str] = []
    index_bytes = 0
    dropped = 0
    # enumerate over the reversed list rather than calling tier2.index(ev):
    # that was O(n) per step and, worse, returned the FIRST match, so two
    # identical events (same tool, same args, same tail) would report the wrong
    # position and understate the dropped count. gemini caught it.
    for idx, ev in enumerate(reversed(tier2)):     # newest first
        line = _index_line(ev)
        cost = len(line.encode("utf-8", errors="replace")) + 1
        if index_bytes + cost > EVIDENCE_INDEX_MAX_BYTES:
            dropped = len(tier2) - idx             # everything older than here
            break
        index_lines.append(line)
        index_bytes += cost
    index_lines.reverse()                          # back to chronological

    index_section: list[str] = []
    if index_lines:
        index_section = [
            "",
            "### Other session activity (index only, no output)",
            "",
            ("These calls happened but are not rendered in full above. They "
             "are listed so you can SEE that they happened. If one of them "
             "looks like it would settle a claim in the proposal, ask Claude "
             "to surface its output; do not treat the claim as unverified "
             "merely because the output is not here."),
            "",
        ]
        if dropped:
            index_section.append(
                f"(plus {dropped} older event(s) not listed: this index is "
                f"itself capped, so it does not reach the start of a long "
                f"session)")
            index_section.append("")
        index_section.extend(index_lines)

    index_text = "\n".join(index_section)
    index_size = len(index_text.encode("utf-8", errors="replace"))

    # Tier 1 gets the remainder of the shared budget. Three things are charged
    # against it that an earlier version added on top of it, so the block could
    # exceed EVIDENCE_MAX_BYTES:
    #   - the truncation header, prepended after the slice (gemini's arithmetic)
    #   - the trailing newline appended to the return value (codex)
    #   - UTF-8 slack: slicing raw bytes can cut a multi-byte character, and
    #     decoding with errors="replace" turns the fragment into U+FFFD, which
    #     re-encodes to THREE bytes. So a byte slice can come back bigger than
    #     the slice. Reserve a few bytes rather than assert it cannot happen.
    trunc_header = b"## Session evidence (head truncated)\n\n"
    utf8_slack = 8                      # >= 2 replacement chars at the cut
    tier1_budget = max(EVIDENCE_MAX_BYTES - index_size - 1 - utf8_slack,
                       EVIDENCE_MAX_BYTES // 4)
    head = "\n".join(body_lines).rstrip() + "\n"
    encoded = head.encode("utf-8", errors="replace")
    if len(encoded) > tier1_budget:
        tail_budget = max(tier1_budget - len(trunc_header), 0)
        tail_bytes = encoded[-tail_budget:] if tail_budget else b""
        head = (trunc_header.decode("utf-8")
                + tail_bytes.decode("utf-8", errors="replace"))
    return head + index_text + "\n" if index_text else head


def build_prompt(system_prompt: str, pitch: str, evidence_block: str = "",
                 user_directives_block: str = "",
                 round1_block: str = "",
                 assistant_block: str = "") -> str:
    """Assemble a member's prompt. SECTION ORDER IS LOAD-BEARING.

    Claude's own prior messages used to be CONCATENATED onto the end of the
    Recent user directives block, and the whole thing was placed before the
    evidence. Two consequences, both bad:

      - Positionally, Claude's prose sat inside the section carrying the user's
        authority. The block's own text said "NOT authoritative", but the layout
        said otherwise, and layout is what a reader actually follows.
      - It arrived BEFORE the evidence, so a member met Claude's framing of the
        problem before it met a single fact. That is anchoring, and it means
        Claude was partly grading his own exam.

    Order is now: the bar, then THE USER'S directives, then the EVIDENCE, then
    Claude's claims (demoted, after the facts), then the proposal. Facts before
    framing.

    Provenance for the design, since it came from the members rather than from
    me: the introspection round-table, resolved and recorded at
    logs/2026-07-13/20260713T042437Z-7f9c23-FINAL.md. Deleting Claude's messages
    outright was the alternative and remains the fallback if anchoring persists;
    re-labelling twice is not a plan.
    """
    sections = [system_prompt]
    if user_directives_block:
        sections.append(user_directives_block)
    if evidence_block:
        sections.append(evidence_block)
    if assistant_block:
        sections.append(assistant_block)
    sections.append(f"Proposal under review:\n\n{pitch}")
    if round1_block:
        sections.append(round1_block)
    return "\n\n---\n\n".join(sections) + "\n"


def format_round1_block(round1_results: list[dict]) -> str:
    """Build the round-1 summary that each round-2 member receives.

    Includes every member's round-1 verdict and text. Members are
    instructed to either revise based on stronger reasoning from
    others or hold their position if their reasoning still stands.
    The final aggregate verdict comes from round 2.
    """
    if not round1_results:
        return ""
    body_lines: list[str] = [
        "## Round 1 of council deliberation",
        "",
        ("Each member's round-1 verdict and reasoning is shown below "
         "(including your own round-1 verdict, if you participated). "
         "You are now in ROUND 2. Re-evaluate the proposal in light "
         "of these round-1 findings. Update your verdict if another "
         "member's reasoning is more grounded than yours; hold your "
         "verdict if yours still stands after considering theirs."),
        "",
        ("Do not pad. Do not change a PASS to WARN/BLOCK to look "
         "diligent; do not change a WARN/BLOCK to PASS to look "
         "agreeable. Follow the evidence and the quality bar."),
        "",
    ]
    for r in round1_results:
        role = r.get("role", "?")
        verdict = r.get("verdict", "?")
        text = (r.get("text") or "").strip()
        body_lines.append(f"### Round-1 verdict: {role}")
        body_lines.append(f"(parsed verdict: {verdict})")
        body_lines.append("")
        if text:
            body_lines.append(text)
        else:
            body_lines.append("(no text returned)")
        body_lines.append("")
    return "\n".join(body_lines).rstrip() + "\n"


def _ensure_nogit_stub() -> Path:
    """Create (idempotently) a `git` stub under NOGIT_DIR that refuses to
    run. Returns NOGIT_DIR. Prepending it to a member subprocess's PATH
    makes a bare `git ...` resolve to this stub and fail."""
    try:
        NOGIT_DIR.mkdir(parents=True, exist_ok=True)
        stub = NOGIT_DIR / "git"
        if not stub.exists():
            stub.write_text(
                "#!/bin/sh\n"
                "echo 'git is disabled for council members' >&2\n"
                "exit 127\n"
            )
            stub.chmod(0o755)
    except OSError:
        pass
    return NOGIT_DIR


def _member_env() -> dict:
    """Environment for member subprocesses: NOGIT_DIR prepended to PATH so
    a bare `git` is shadowed by the refusing stub. VS Code / editor
    variables are stripped as a precaution against IDE-context leakage; a
    direct gemini probe showed no IDE access with or without them, so the
    empty member working directory is the primary isolation."""
    env = dict(os.environ)
    env["PATH"] = str(_ensure_nogit_stub()) + os.pathsep + env.get("PATH", "")
    for key in list(env):
        if key.startswith("VSCODE_") or key in ("TERM_PROGRAM",
                                                 "TERM_PROGRAM_VERSION"):
            del env[key]
    return env


async def _run_subprocess(cmd: list[str], cwd: Path, role: str,
                          post_read: Path | None = None,
                          stdin_data: str | None = None) -> dict:
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=_member_env(),
        )
    except FileNotFoundError as e:
        return {
            "role": role, "text": "", "stderr": f"exec failed: {e}",
            "returncode": -1, "verdict": "ERROR",
            "duration_s": round(time.monotonic() - t0, 2),
        }
    input_bytes = stdin_data.encode("utf-8") if stdin_data is not None else b""
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=input_bytes), timeout=PER_CRITIC_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "role": role, "text": "",
            "stderr": f"TIMEOUT after {PER_CRITIC_TIMEOUT_S}s",
            "returncode": -1, "verdict": "ERROR",
            "duration_s": round(time.monotonic() - t0, 2),
        }
    duration = round(time.monotonic() - t0, 2)
    stderr = stderr_b.decode("utf-8", errors="replace")
    if post_read and post_read.exists():
        text = post_read.read_text(errors="replace")
        try:
            post_read.unlink()
        except OSError:
            pass
    else:
        text = stdout_b.decode("utf-8", errors="replace")
    verdict = parse_verdict(text) if proc.returncode == 0 else "ERROR"
    return {
        "role": role, "text": text, "stderr": stderr,
        "returncode": proc.returncode, "verdict": verdict,
        "duration_s": duration,
    }


def codex_cmd(out_path: Path) -> list[str]:
    # The prompt is delivered on stdin (the trailing "-" tells codex to
    # read instructions from stdin, per `codex exec --help`), not as an
    # argv string. A single argv string is capped at MAX_ARG_STRLEN
    # (32 * page size = 131072 bytes / 128 KB on this system); once the
    # evidence caps were raised the prompt exceeded that as one argv
    # element and raised "Argument list too long" (Errno 7) this
    # session. stdin has no per-argument size cap.
    return [
        "codex", "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--color", "never",
        "--output-last-message", str(out_path),
        "-c", f'model="{CODEX_MODEL}"',
        "-c", f'model_reasoning_effort="{CODEX_REASONING}"',
        "-",
    ]


# --- codex auth serialisation ---------------------------------------------
#
# WHAT IS ACTUALLY KNOWN, because this fix is insurance and should not pretend
# to be more:
#
#   - 387 council fires have failed with codex's "refresh token was already
#     used". 15 of them were TODAY, in one 28-minute window; the rest were an
#     auth outage on 2026-06-01..03. So it is live, not historical.
#   - What that message IMPLIES about codex's credential lifecycle, I could not
#     source. I searched the installed npm package for readable auth code and
#     found none (its bin/ holds only a launcher script); I did NOT establish
#     where the auth logic actually lives, only that I could not read it here.
#     So I have no primary reference for how tokens rotate or what survives a
#     failed refresh, and nothing below should be read as if I did. Taking
#     the message at face value, the credential we sent had already been used,
#     and the two obvious ways that happens are a race (two processes refreshing
#     at once) or a persistence failure (a rotated credential never written back,
#     so the next run re-sends a stale one). Both are HYPOTHESES.
#   - ~/.codex/auth.json was last written 2026-07-04, and its id_token expired
#     the same day, while the access_token is still valid. That is CONSISTENT
#     with the persistence theory but does not establish it.
#   - I could NOT reproduce the failure: 10 consecutive --ephemeral runs gave
#     0 errors and did not touch auth.json, because no refresh was due. That is
#     an inconclusive test, not a passing one.
#   - Correlating today's errors against concurrent council FIRES showed no
#     concurrency signal -- but that instrument is blind to codex processes
#     spawned by council_dialogue.py, which do not write to logs/, and dialogue
#     rounds were running during that exact window.
#
# So the root cause is UNCONFIRMED. The lock below is cheap when uncontended
# (measured 0.0ms). If the real cause turns out to be persistence rather than a
# race, the lock is INEFFECTIVE, not free: under contention it can still add up
# to CODEX_LOCK_TIMEOUT_S of waiting for no benefit. It is defence, not a
# diagnosis.
#
# Scope, narrowly: it serialises the codex invocations made by COUNCIL
# processes, because those are the only ones that take this lock. A `codex`
# run started by hand in another terminal, or any other client on this box,
# does not take it and can still race us. codex made me narrow this; the
# earlier wording said "a race cannot happen", which was never true.
CODEX_LOCK_PATH = Path(tempfile.gettempdir()) / "council_codex_auth.lock"

# Derived from how long a holder actually holds it, NOT from the critic timeout.
# I first set this to PER_CRITIC_TIMEOUT_S (600s) on the theory that a holder
# cannot outlive its own kill timer. gemini pointed out the consequence, and it
# is disqualifying: this code runs inside a PostToolUse hook, so a 600s lock
# wait would hang the user's session for ten minutes -- far worse than the
# failure it is guarding against. A lock that stalls the tool is not a fix.
#
# So it is sized to how long a holder is OBSERVED to hold it. Measured over the
# 200 successful codex runs logged today: median 6.7s, p90 16.4s, p99 25.2s,
# observed max 25.5s, and 100% under 60s. So a 60s wait absorbs roughly two
# observed-maximum holders queued ahead of us. "Observed maximum" is not
# "worst case" -- codex could hang for longer, and that is precisely the case
# the fail-open below exists for.
#
# Beyond 60s we fail OPEN and run unserialised. This is best-effort
# serialisation: it removes lock-wait as a LOCAL cause of a lost vote. It does
# not promise the unserialised call then succeeds, and it cannot stop an outer
# timeout from killing the hook while we wait.
CODEX_LOCK_TIMEOUT_S = 60
CODEX_AUTH_ERROR_MARKERS = (
    "refresh token was already used",
    "refresh token was revoked",
    "access token could not be refreshed",
)


def _codex_auth_failed(result: dict) -> bool:
    """Did codex fail because of auth, as opposed to merely TALKING about auth?

    Two guards, and both exist because the first version had neither and codex
    caught it using an insight I had just handed it:

      1. The run must have actually FAILED (non-zero exit). A member that
         returned a clean verdict did not have an auth failure, whatever its
         prose says.
      2. Only stderr is scanned, never the member's text. The text is model
         OUTPUT, and a member reviewing this very file will quote the auth
         phrases in it.

    What is VERIFIED about the old version: a result with returncode 0 and the
    phrase in its text returned True, and the phrase IS present in the live
    evidence block (my own analysis of these errors printed it there, and the
    evidence block is fed to members). So the false-positive path was reachable.

    What is NOT verified, and I have now overclaimed it in both directions: I
    first reported that the live retries had caught a real auth failure, then
    swung to calling them "in all likelihood spurious". The honest position is
    that I did not record the first attempt's return code, so I CANNOT classify
    those live retries either way. The retry logging below exists so the next
    one is not a mystery.
    """
    if result.get("returncode") == 0:
        return False
    stderr = (result.get("stderr") or "").lower()
    return any(m in stderr for m in CODEX_AUTH_ERROR_MARKERS)


def _codex_lock_acquire():
    """Take the cross-process codex lock. Blocking; returns the open handle.

    Returns None if the lock cannot be taken (including on timeout): a lock we
    cannot get must not turn into a lost vote, so we run unserialised rather
    than fail. Blocking flock is fine here because the caller runs it in a
    worker thread, leaving the event loop free for the other members.
    """
    try:
        fh = open(CODEX_LOCK_PATH, "w")
    except OSError as e:
        print(f"council: codex auth lock unavailable ({e}); running anyway",
              file=sys.stderr)
        return None
    start = time.monotonic()
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            # Contention, and ONLY contention. Verified this session: a
            # non-blocking flock held by another process raises BlockingIOError
            # (EAGAIN), while a permanent failure such as a bad descriptor
            # raises a plain OSError (EBADF). An earlier version caught OSError
            # here, which would have spun the full timeout on an error that was
            # never going to clear -- codex, gemini and deepseek all flagged it.
            if time.monotonic() - start > CODEX_LOCK_TIMEOUT_S:
                print("council: codex auth lock timed out; running anyway",
                      file=sys.stderr)
                fh.close()
                return None
            time.sleep(0.25)
        except OSError as e:
            # Any non-contention error. We fail open immediately by CHOICE, not
            # because such errors are proven permanent -- I verified EBADF here
            # and nothing more. Whether retrying some transient non-contention
            # error would help is UNVERIFIED. Fail-open is the chosen policy,
            # and its floor is known: running unserialised is exactly the risk
            # we ran before this lock existed.
            print(f"council: codex auth lock unusable ({e}); running anyway",
                  file=sys.stderr)
            fh.close()
            return None


def _codex_lock_release(fh) -> None:
    if fh is None:
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass
    fh.close()


async def run_codex(pitch: str, system_prompt: str, cwd: Path,
                    evidence_block: str = "",
                    user_directives_block: str = "",
                    round1_block: str = "",
                    assistant_block: str = "") -> dict:
    prompt = build_prompt(system_prompt, pitch, evidence_block,
                          user_directives_block, round1_block,
                          assistant_block)

    async def attempt() -> dict:
        out_path = Path(f"/tmp/council_codex_{uuid.uuid4().hex}.txt")
        # The lock is held across the WHOLE subprocess, not just a notional
        # refresh window: from out here we cannot see when codex decides to
        # refresh, so the only safe span is the entire call. Acquired in a
        # thread so the blocking flock never stalls the event loop -- gemini
        # and deepseek keep running concurrently while codex waits its turn.
        fh = await asyncio.to_thread(_codex_lock_acquire)
        try:
            return await _run_subprocess(
                codex_cmd(out_path), cwd, role="codex",
                post_read=out_path, stdin_data=prompt,
            )
        finally:
            await asyncio.to_thread(_codex_lock_release, fh)

    result = await attempt()
    if _codex_auth_failed(result):
        # Retry once. The lock and the retry cover DIFFERENT failures, and an
        # earlier comment here confused them: it said the retry helps because
        # "the lock has serialised us behind whoever won the race". That is
        # incoherent, as all three members pointed out -- if two COUNCIL
        # processes could race, the lock is what stops it, so that race cannot
        # be what the retry is rescuing us from.
        #
        #   the LOCK  prevents races between processes that TAKE it, i.e. the
        #             council's own codex invocations.
        #   the RETRY is aimed at what the lock cannot reach. The cases where it
        #             MIGHT help are hypotheses, not demonstrated recoveries: a
        #             codex started outside the council (which never takes our
        #             lock), a transient server-side failure, or -- IF
        #             credentials rotate on disk, which I could not source -- a
        #             second attempt reading one the first attempt refreshed.
        #             I have not yet observed it recover a confirmed auth
        #             failure; the fields recorded below are what will tell us.
        #
        # Retrying once is a chosen policy: simple, bounded so it cannot loop.
        # It is not a measured optimum. A second failure is reported as a lost
        # vote rather than swallowed.
        rc = result.get("returncode")
        first_err = (result.get("stderr") or "").strip().split("\n")[-1][:160]
        print(f"council: codex auth failure (rc={rc}); retrying once. "
              f"first attempt stderr: {first_err!r}", file=sys.stderr)
        await asyncio.sleep(2)
        retried = await attempt()
        # Keep the first attempt's evidence ON the result, so a retry can be
        # classified after the fact instead of being a mystery. Without this I
        # could not tell whether the retries I saw fire were real auth failures
        # or false positives, and I guessed -- twice, in opposite directions.
        retried["codex_auth_retry"] = True
        retried["codex_first_returncode"] = rc
        retried["codex_first_stderr_tail"] = first_err
        retried["codex_retry_recovered"] = retried.get("verdict") not in (
            "ERROR", "UNPARSEABLE")
        return retried
    return result


def _gemini_api_call_blocking(prompt: str) -> dict:
    """Blocking HTTP call to the Gemini generateContent REST API. Returns
    a member-result dict in the same shape as _run_subprocess. Any
    failure (missing key, HTTP error, non-JSON, safety block, empty
    content) maps to a verdict of ERROR so the member degrades gracefully
    rather than crashing the council."""
    t0 = time.monotonic()

    def fail(msg: str) -> dict:
        return {
            "role": "gemini", "text": "", "stderr": msg,
            "returncode": -1, "verdict": "ERROR",
            "duration_s": round(time.monotonic() - t0, 2),
        }

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return fail("GEMINI_API_KEY not set in environment")

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": GEMINI_THINKING_LEVEL},
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_API_URL, data=body, method="POST",
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=PER_CRITIC_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return fail(f"HTTPError {e.code}: {detail}")
    except Exception as e:
        return fail(f"request failed: {e}")

    try:
        data = json.loads(raw)
    except Exception:
        return fail(f"non-JSON response: {raw[:300]}")
    if isinstance(data, dict) and data.get("error"):
        return fail(f"api error: {json.dumps(data['error'])[:400]}")
    try:
        parts = data["candidates"][0]["content"]["parts"]
        content = "".join(p.get("text", "") for p in parts
                          if isinstance(p, dict))
    except (KeyError, IndexError, TypeError):
        content = ""
    if not content:
        finish = ""
        try:
            finish = data["candidates"][0].get("finishReason", "")
        except (KeyError, IndexError, TypeError):
            pass
        feedback = data.get("promptFeedback", "") if isinstance(data, dict) else ""
        return fail(f"empty content (finishReason={finish}, "
                    f"promptFeedback={json.dumps(feedback)[:200]}): {raw[:300]}")

    return {
        "role": "gemini", "text": content, "stderr": "",
        "returncode": 0, "verdict": parse_verdict(content),
        "duration_s": round(time.monotonic() - t0, 2),
    }


async def run_gemini(pitch: str, system_prompt: str, cwd: Path,
                     evidence_block: str = "",
                     user_directives_block: str = "",
                     round1_block: str = "",
                     assistant_block: str = "") -> dict:
    prompt = build_prompt(system_prompt, pitch, evidence_block,
                          user_directives_block, round1_block,
                          assistant_block)
    # API only. main() drops gemini from the roster when GEMINI_API_KEY is
    # absent, so this is normally reached only with a key present; the
    # blocking call still fails closed (verdict ERROR) if the key is gone.
    return await asyncio.to_thread(_gemini_api_call_blocking, prompt)


def _deepseek_call_blocking(prompt: str) -> dict:
    """Blocking HTTP call to the DeepSeek chat-completions API. Returns
    a member-result dict in the same shape as _run_subprocess. Any
    failure (missing key, HTTP error, non-JSON, empty content) maps to
    a verdict of ERROR so the member degrades gracefully rather than
    crashing the council."""
    t0 = time.monotonic()

    def fail(msg: str) -> dict:
        return {
            "role": "deepseek", "text": "", "stderr": msg,
            "returncode": -1, "verdict": "ERROR",
            "duration_s": round(time.monotonic() - t0, 2),
        }

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return fail("DEEPSEEK_API_KEY not set in environment")

    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "reasoning_effort": DEEPSEEK_REASONING,
    }).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_URL, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=PER_CRITIC_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return fail(f"HTTPError {e.code}: {detail}")
    except Exception as e:
        return fail(f"request failed: {e}")

    try:
        data = json.loads(raw)
    except Exception:
        return fail(f"non-JSON response: {raw[:300]}")
    if isinstance(data, dict) and data.get("error"):
        return fail(f"api error: {json.dumps(data['error'])[:400]}")
    try:
        content = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        content = ""
    if not content:
        return fail(f"empty content in response: {raw[:300]}")

    return {
        "role": "deepseek", "text": content, "stderr": "",
        "returncode": 0, "verdict": parse_verdict(content),
        "duration_s": round(time.monotonic() - t0, 2),
    }


async def run_deepseek(pitch: str, system_prompt: str, cwd: Path,
                       evidence_block: str = "",
                       user_directives_block: str = "",
                       round1_block: str = "",
                       assistant_block: str = "") -> dict:
    prompt = build_prompt(system_prompt, pitch, evidence_block,
                          user_directives_block, round1_block,
                          assistant_block)
    return await asyncio.to_thread(_deepseek_call_blocking, prompt)


MEMBER_RUNNERS = {
    "codex": run_codex,
    "gemini": run_gemini,
    "deepseek": run_deepseek,
}


def load_external_verdicts(specs: list[str]) -> list[dict]:
    out: list[dict] = []
    for spec in specs:
        if "=" not in spec:
            raise SystemExit(
                f"--external-verdict requires NAME=PATH form, got: {spec!r}"
            )
        name, _, path_str = spec.partition("=")
        path = Path(path_str)
        if not path.exists():
            raise SystemExit(f"external-verdict file not found: {path}")
        text = path.read_text(errors="replace")
        out.append({
            "role": name,
            "text": text,
            "stderr": "",
            "returncode": 0,
            "verdict": parse_verdict(text),
            "duration_s": 0.0,
            "source": f"external:{path}",
        })
    return out


def lost_votes(results: list[dict]) -> list[dict]:
    """Members whose vote did not make it into the consensus.

    UNPARSEABLE: the member answered, but its verdict line could not be read.
    For a built-in member the formatting retry has already been tried and
    failed; an EXTERNAL verdict (--external-verdict) is not retried at all, so
    it can arrive here unparsed without ever having had a second chance.

    ERROR: the member's runner reported failure. For the subprocess member
    (codex) that is `verdict = parse_verdict(...) if proc.returncode == 0 else
    "ERROR"`, i.e. a non-zero exit; the HTTP members (gemini, deepseek) reach
    ERROR through their own fail() paths. In neither case does it necessarily
    mean no text came back.

    1,179 of the 18,598 logged votes are ERROR. Classifying them by stderr
    substring (a heuristic, not a parsed cause) gives: codex ChatGPT usage
    limit 407; codex auth-token refresh failure, "refresh token was already
    used" 269; exec/cwd failure 353; missing API key 60; "model at capacity"
    only 12. That totals 1,101, leaving 78 unclassified. The point of writing
    it down is that "at capacity" -- which I had assumed was the dominant
    failure -- is 1% of it.

    Both used to vanish into the `else: return "WARN"` branch below with
    nothing said about them, so a WARN could be produced by nobody actually
    warning.
    """
    return [r for r in results
            if r.get("verdict") in ("UNPARSEABLE", "ERROR")]


def determine_final_verdict(active_results: list[dict]) -> str:
    if not active_results:
        return "ERROR"
    verdicts = [r["verdict"] for r in active_results]
    if any(v == "BLOCK" for v in verdicts):
        return "BLOCK"
    # A lost vote (UNPARSEABLE or ERROR) is handled CONSERVATIVELY: it cannot
    # count as a PASS, so it prevents an all-PASS consensus and the fire comes
    # out as WARN. That is the pre-existing behaviour and it is deliberately
    # unchanged here -- silently upgrading a fire to PASS because a member
    # crashed would be worse. What IS new is that it no longer happens
    # silently: emit_output prints a LOST VOTE banner naming the member, so a
    # WARN that nobody actually voted for is visible as such rather than
    # looking like a substantive concern.
    if any(v in ("UNPARSEABLE", "ERROR") for v in verdicts):
        return "WARN"
    if all(v == "PASS" for v in verdicts):
        return "PASS"
    return "WARN"


def write_log(layer: str, tool_name: str | None, target_path: str | None,
              pitch: str, all_results: list[dict], final_verdict: str,
              session_id: str = "") -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_dir = LOGS_ROOT / today
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"{ts}-{uuid.uuid4().hex[:8]}.json"
    pitch_b = pitch.encode("utf-8")
    if len(pitch_b) > PITCH_LOG_MAX_BYTES:
        head = pitch_b[:PITCH_LOG_MAX_BYTES // 2].decode("utf-8", errors="replace")
        pitch_logged = (
            head
            + f"\n... [pitch truncated in log; original {len(pitch_b)} bytes]"
        )
    else:
        pitch_logged = pitch
    entry = {
        "timestamp": now_iso(),
        "session_id": session_id,   # so a log can be traced to the session that
                                    # produced it; without it, deleting a session
                                    # leaves its logs orphaned and unattributable
        "layer": layer,
        "tool_name": tool_name,
        "target_path": target_path,
        "pitch": pitch_logged,
        "members": all_results,
        "final_verdict": final_verdict,
    }
    log_path.write_text(json.dumps(entry, indent=2, default=str))
    return log_path


def emit_output(results: list[dict], final_verdict: str, log_path: Path) -> None:
    def extract_error_reason(stderr: str) -> str:
        if not stderr:
            return ""
        lines = [l.strip() for l in stderr.split("\n") if l.strip()]
        error_lines = [l for l in lines if "error" in l.lower()]
        if error_lines:
            return error_lines[-1]
        return lines[-1] if lines else ""

    print(f"VERDICT: {final_verdict}")
    print(f"# log: {log_path}")
    for r in results:
        line = f"# member: {r['role']} verdict={r['verdict']}"
        if r.get("reformatted"):
            line += " (verdict line was malformed; recovered on retry)"
        if r["verdict"] == "ERROR":
            hint = extract_error_reason(r["stderr"])
            line += f" stderr_hint={hint[:200]!r}"
        print(line)
    print()

    # Fail LOUD. A lost vote used to disappear into the WARN branch of
    # determine_final_verdict with nothing said, so a fire could come back WARN
    # when not one member had actually warned -- indistinguishable, from the
    # outside, from a substantive concern. Now it is named.
    lost = lost_votes(results)
    if lost:
        counted = len(results) - len(lost)
        print("#" + "=" * 68)
        print(f"# LOST VOTES: {len(lost)} of {len(results)} members did not vote.")
        for r in lost:
            if r["verdict"] == "UNPARSEABLE":
                why = ("no parseable verdict line, and the formatting retry "
                       "did not recover it"
                       if r.get("reformat_failed") else
                       "no parseable verdict line")
                if r.get("reformat_error"):
                    why += f" (retry itself failed: {r['reformat_error'][:80]})"
            else:
                why = f"runner error: {extract_error_reason(r['stderr'])[:120]}"
            print(f"#   {r['role']}: {why}")
        print(f"# {counted} member(s) returned a readable verdict.")
        print("# A lost vote does NOT abstain: it cannot count as a PASS, so it "
              "prevents an\n#   all-PASS consensus. That is deliberately "
              "conservative -- a member that\n#   failed must not silently buy "
              "a PASS.")
        readable_concern = any(r["verdict"] in ("WARN", "BLOCK")
                               for r in results)
        if final_verdict == "WARN" and not readable_concern:
            print("# NOTE: this fire is WARN *only* because a vote was lost. No "
                  "readable WARN\n#   or BLOCK was parsed from any member, so "
                  "no substantive concern about\n#   the work has been "
                  "ESTABLISHED here. Fix the malfunction first.")
            if any(r["verdict"] == "UNPARSEABLE" for r in lost):
                print("#   CAVEAT: an UNPARSEABLE member may still have written "
                      "a real concern\n#   in its prose that simply failed to "
                      "parse. Read its text in the log\n#   before dismissing "
                      "this.")
        print("#" + "=" * 68)
        print()

    if final_verdict == "PASS":
        for r in results:
            print(f"# {r['role']}: PASS ({r['duration_s']}s)")
        return

    for r in results:
        print(f"## {r['role']} (verdict {r['verdict']}, {r['duration_s']}s)")
        print()
        text = r["text"].strip()
        if not text and r["verdict"] == "ERROR":
            hint = extract_error_reason(r["stderr"])
            if hint:
                print(f"[no text returned; possible error line from stderr: {hint}]")
            else:
                print(f"[no text returned]")
            print(f"[stderr tail (last 2000 chars):]")
            print(r["stderr"].strip()[-2000:])
        else:
            print(text)
        print()


def parse_members(raw: str) -> list[str]:
    if raw == "":
        return []
    members = [m.strip() for m in raw.split(",") if m.strip()]
    bad = [m for m in members if m not in MEMBER_RUNNERS]
    if bad:
        raise SystemExit(
            f"unknown council members: {bad}; valid: {list(MEMBER_RUNNERS)}"
        )
    return members


async def main() -> int:
    parser = argparse.ArgumentParser(description="Workers' Council wrapper (advisory mode)")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--layer", choices=["posttool", "reasoning", "stop_prose"], default="reasoning")
    parser.add_argument("--tool-name", default=None)
    parser.add_argument("--target-path", default=None)
    parser.add_argument("--session-id", default="",
                        help="Claude Code session id, recorded in the log. "
                             "Without it a council log cannot be traced back to "
                             "the session that produced it, so a deleted session "
                             "leaves its logs orphaned and unattributable "
                             "(council_outcome.py audit).")
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument("--members", default=",".join(ALL_MEMBERS),
                        help=f"Comma-separated built-in members to run "
                             f"(choices: {','.join(MEMBER_RUNNERS)}; "
                             f"empty string disables all built-ins)")
    parser.add_argument("--external-verdict", action="append", default=[],
                        metavar="NAME=PATH",
                        help="Append an externally-supplied verdict "
                             "(e.g. from a Claude Agent substitute). Repeatable.")
    parser.add_argument("--evidence-file", type=Path, default=None,
                        help="Path to a per-session JSONL evidence file "
                             "produced by evidence_logger.py. Contents are "
                             "injected into each member's prompt as a "
                             "## Session evidence block before the proposal.")
    parser.add_argument("--transcript-path", type=Path, default=None,
                        help="Path to the Claude Code session transcript "
                             "JSONL. The last N user messages are extracted "
                             "and injected as a ## Recent user directives "
                             "block in each member's prompt.")
    args = parser.parse_args()

    if args.prompt_file:
        pitch = args.prompt_file.read_text()
    else:
        pitch = sys.stdin.read()
    if not pitch.strip():
        print("ERROR: empty pitch", file=sys.stderr)
        return 3
    if not SYSTEM_PROMPT_PATH.exists():
        print(f"ERROR: missing system prompt at {SYSTEM_PROMPT_PATH}", file=sys.stderr)
        return 3
    system_prompt = SYSTEM_PROMPT_PATH.read_text()

    members = parse_members(args.members)
    # Drop deepseek when its key is absent from the environment, so a
    # key-less session (e.g. one launched before DEEPSEEK_API_KEY was
    # exported) runs the council on codex + gemini. Without this, the
    # deepseek runner returns an ERROR verdict; per
    # determine_final_verdict (all-PASS -> PASS, else -> WARN) a single
    # ERROR forces the final verdict to WARN on every fire.
    if "deepseek" in members and not os.environ.get("DEEPSEEK_API_KEY"):
        members = [m for m in members if m != "deepseek"]
        print("council: deepseek skipped (DEEPSEEK_API_KEY not set)",
              file=sys.stderr)
    # Drop gemini when its API key is absent. gemini runs ONLY via the
    # Gemini REST API; the agentic agy CLI fallback was removed for safety
    # (it could mutate the filesystem). A key-less session runs the
    # council on codex + deepseek, both of which cannot write state
    # (codex is sandboxed read-only; deepseek is an HTTP call).
    if "gemini" in members and not os.environ.get("GEMINI_API_KEY"):
        members = [m for m in members if m != "gemini"]
        print("council: gemini skipped (GEMINI_API_KEY not set; "
              "agy fallback removed for safety)", file=sys.stderr)
    external = load_external_verdicts(args.external_verdict)
    if not members and not external:
        print("ERROR: no council members to consult "
              "(use --members and/or --external-verdict)", file=sys.stderr)
        return 3

    evidence_block = ""
    if args.evidence_file is not None:
        evidence_block = format_evidence_block(args.evidence_file,
                                               args.target_path or "")

    # Both default to empty. assistant_block used to be assigned ONLY inside the
    # branch below, so any run without --transcript-path crashed with
    # UnboundLocalError when it was later passed to the members. That is a bug I
    # introduced in the de-anchoring change and the council never saw. The
    # advisor passes --transcript-path only when the hook payload carries one
    # (council_advisor.py: `if transcript_path:`), so the crash was reachable
    # from a hook too; it simply never fired, because in practice the fires that
    # reviewed this work all had a transcript. It surfaced the first time I ran
    # the wrapper straight from the CLI.
    user_directives_block = ""
    assistant_block = ""
    if args.transcript_path is not None:
        user_directives_block = format_user_directives(
            args.transcript_path, args.evidence_file)
        assistant_block = format_assistant_messages(args.transcript_path)

    # Members run in a fresh empty working directory, not the session's
    # project dir, so a member CLI that auto-explores its cwd (e.g.
    # gemini: verified this session) finds no project files to surface.
    # This closes the cwd auto-discovery vector (verified this session);
    # it is not a full filesystem sandbox.
    member_cwd = Path(tempfile.mkdtemp(prefix="council_member_"))

    # Round 1: each member sees the proposal independently and emits
    # an initial verdict.
    round1_results = await asyncio.gather(*[
        MEMBER_RUNNERS[m](pitch, system_prompt, member_cwd,
                          evidence_block, user_directives_block,
                          "", assistant_block)
        for m in members
    ]) if members else []

    # Round 2: each member sees the round-1 verdicts of all members
    # (including their own) and is asked to re-evaluate. This is the
    # cross-member dialogue round. Final aggregation uses round-2
    # results. Skip the round if there are fewer than two members; a
    # single-member fire has nobody to dialogue with.
    if len(round1_results) >= 2:
        round1_block = format_round1_block(round1_results)
        builtin_results = await asyncio.gather(*[
            MEMBER_RUNNERS[m](pitch, system_prompt, member_cwd,
                              evidence_block, user_directives_block,
                              round1_block, assistant_block)
            for m in members
        ])
    else:
        builtin_results = round1_results

    # One formatting-only retry for any member whose verdict line did not
    # parse, so a member that HAD a position does not lose its vote to a stray
    # "VERDICT: PASS (with caveats)". This must run BEFORE member_cwd is
    # removed -- the retry spawns a member in that directory, and gemini caught
    # that the rmtree used to sit above this point and would have deleted the
    # cwd out from under it.
    builtin_results = await reformat_unparseable(list(builtin_results),
                                                 member_cwd)

    shutil.rmtree(member_cwd, ignore_errors=True)
    all_results = list(builtin_results) + external
    final_verdict = determine_final_verdict(all_results)
    log_path = write_log(args.layer, args.tool_name, args.target_path,
                         pitch, all_results, final_verdict,
                         session_id=args.session_id)
    emit_output(all_results, final_verdict, log_path)

    return {"PASS": 0, "WARN": 1, "BLOCK": 2}.get(final_verdict, 3)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
