#!/usr/bin/env python3
"""Battery for explicit prompt-cache breakpoints. Run from Council/:
    python3 council/tests/test_cache_control.py

EXITS NON-ZERO ON ANY FAILURE. No API calls: urlopen is stubbed, so this runs free
and offline.

THE PROPERTY THAT MATTERS MOST is not that a breakpoint is emitted -- it is that
emitting one CANNOT CHANGE THE TEXT A MEMBER RECEIVES. Splitting one prompt string
into typed content parts is a request-shape change on the path every member's review
travels, so the battery asserts byte-exact reassembly on real prompt shapes, including
ones containing the "---" separator and non-ASCII text. If that invariant ever breaks,
a member is silently reviewing something other than what the harness built.

SELECTION IS THE SECOND PROPERTY: the seats measured on 2026-07-28 as already caching
well under the plain-string shape (glm 86.5%, deepseek 54.2%) must keep receiving a
PLAIN STRING, because OpenRouter's page does not document the array shape as neutral
for automatic-caching providers. A regression there would be invisible without this.

NOT ESTABLISHED BY THIS FILE: that a breakpoint actually raises cached_tokens for any
provider. That is a live measurement, not a unit test, and it is deliberately not
claimed here.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import consult_council as cc  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def wire_text(body: dict) -> str:
    """Every scrap of text in the request body, concatenated in order.

    THE INVARIANT THIS SERVES: whatever shape the harness chooses -- one plain-string
    user message, one message of typed parts, or a system/user split -- the text the
    member receives must reproduce build_prompt's output BYTE FOR BYTE. An earlier
    version of these checks joined parts within messages[0] only, so it started failing
    the moment the explicit path grew a second message: it was asserting the SHAPE, not
    the invariant. Concatenating across all messages asserts the invariant and stays
    true under any future reshaping.
    """
    out = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(p.get("text", "") for p in c if isinstance(p, dict))
    return "".join(out)


def explicit_slug() -> str:
    """A slug that IS on the explicit-breakpoint list, DERIVED from the constant.

    Hardcoding one here is how this battery went stale: it used qwen/qwen3.7-max as its
    positive example, and when qwen/ was removed on measurement (2026-07-29) five checks
    failed for a reason that had nothing to do with the property under test. Deriving the
    slug means the positive path keeps being exercised by whatever is actually on the
    list, and the battery only breaks if the list empties -- which is a real defect and
    is asserted separately below.
    """
    if not cc.CACHE_CONTROL_MODEL_PREFIXES:
        raise SystemExit("CACHE_CONTROL_MODEL_PREFIXES is empty: the explicit-breakpoint "
                         "path is unreachable and these checks would pass vacuously.")
    return cc.CACHE_CONTROL_MODEL_PREFIXES[0] + "probe-model"


def selection_checks() -> None:
    print("SELECTION: only documented explicit-breakpoint providers opt in")
    for slug, want in [
        ("anthropic/claude-opus-4", True),
        # qwen/ is INCLUDED again as of 2026-07-30, and the round trip is the point.
        # It was in on the doc alone; over 229 logged fires it wrote a cache on 211 and
        # read one back on ZERO, and a controlled probe priced that at +22.8% per call
        # for nothing, so it came OUT. It went back in only after probe_qwen_cache.py
        # --test-c measured the SPLIT shape writing 7,803 tokens bounded at the message
        # boundary and reading all of them back on a call with a DIFFERENT tail.
        # The breakpoint alone was never the fix; the message shape was. Anyone flipping
        # this row should have equivalent evidence for whichever direction they flip it.
        ("qwen/qwen3.7-max", True),
        # google/ is deliberately EXCLUDED: measured 2026-07-28, an explicit breakpoint
        # CAPPED gemini at 6,231 cached tokens where its implicit path reached ~57,300.
        # This row is the regression guard -- if google/ is ever re-added without new
        # evidence, this fails.
        ("google/gemini-3.6-flash", False),
        ("z-ai/glm-5.2", False),          # measured 86.5% cached on the plain string
        ("deepseek/deepseek-v4-pro", False),   # measured 54.2%
        ("x-ai/grok-4.5", False),
        ("moonshotai/kimi-k3", False),
        ("minimax/minimax-m3", False),
        ("nvidia/nemotron-3-ultra-550b-a55b", False),
        ("mistralai/mistral-medium-3-5", False),
        ("meta/muse-spark-1.1", False),
        ("xiaomi/mimo-v2.5-pro", False),
    ]:
        check(f"{slug} -> {'explicit' if want else 'plain string'}",
              cc._needs_explicit_cache_control([slug]) is want)

    check("a fallback slug alone can opt the request in",
          cc._needs_explicit_cache_control(["z-ai/glm-5.2", explicit_slug()]) is True)
    check("non-string entries do not raise",
          cc._needs_explicit_cache_control([None, 7, "z-ai/glm-5.2"]) is False)
    check("an empty model list is plain string",
          cc._needs_explicit_cache_control([]) is False)


def reassembly_checks() -> None:
    print("\nREASSEMBLY: the parts must reproduce the prompt BYTE FOR BYTE")
    prefix = "SYSTEM BAR\nrule one\nrule two"
    # Real prompts are joined with build_prompt's "\n\n---\n\n" separator and carry
    # non-ASCII (a member returned a refusal in Chinese on 2026-07-27), so both appear
    # here rather than a sanitised toy string.
    prompt = prefix + "\n\n---\n\n## Evidence\n" + "x" * 500 + \
        "\n\n---\n\nProposal under review:\n\n你好 -- diff here\n"

    parts = cc._message_content(prompt, prefix)
    check("a valid split yields two typed parts", isinstance(parts, list)
          and len(parts) == 2, f"got {type(parts).__name__}")
    if isinstance(parts, list) and len(parts) == 2:
        rebuilt = "".join(p["text"] for p in parts)
        check("REASSEMBLY IS BYTE-EXACT", rebuilt == prompt,
              f"len rebuilt={len(rebuilt)} vs prompt={len(prompt)}")
        check("reassembly is byte-exact after utf-8 encoding",
              rebuilt.encode() == prompt.encode())
        check("the breakpoint rides on the FIRST part only",
              parts[0].get("cache_control") == {"type": "ephemeral"}
              and "cache_control" not in parts[1])
        check("both parts declare type text",
              all(p.get("type") == "text" for p in parts))
        check("the cached part is exactly the prefix", parts[0]["text"] == prefix)
        check("the whole payload is JSON-serialisable",
              isinstance(json.dumps({"content": parts}), str))

    print("\n  fallbacks -- every one must yield the UNCHANGED plain string")
    check("empty prefix", cc._message_content(prompt, "") == prompt)
    check("prefix not actually leading",
          cc._message_content(prompt, "NOT THE START") == prompt)
    check("prefix equals the whole prompt (empty remainder)",
          cc._message_content(prompt, prompt) == prompt)
    check("prefix longer than the prompt", cc._message_content("ab", "abcdef") == "ab")


def delivery_checks() -> None:
    """Prove the shape reaches the WIRE, per seat. Extraction is not delivery."""
    print("\nDELIVERY: the real transport's request body, per seat")
    sent: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "model": "m",
                "choices": [{"message": {"content": "VERDICT: PASS\nREASONS:\n- ok"}}],
            }).encode()

    def _stub(req, timeout=None):
        sent["body"] = json.loads(req.data.decode())
        return _Resp()

    real_urlopen = cc.urllib.request.urlopen
    real_key = cc.os.environ.get(cc.OPENROUTER_KEY_ENV)
    cc.urllib.request.urlopen = _stub
    cc.os.environ[cc.OPENROUTER_KEY_ENV] = "test-not-a-real-key"
    prefix = "SYSTEM BAR TEXT"
    prompt = prefix + "\n\n---\n\nvariable part\n"
    try:
        cc._openrouter_call_blocking("probe", [explicit_slug()], prompt,
                                     cache_prefix=prefix)
        content = sent["body"]["messages"][0]["content"]
        check("an explicit-list slug receives typed parts with a breakpoint",
              isinstance(content, list)
              and content[0].get("cache_control") == {"type": "ephemeral"},
              f"got {type(content).__name__}")
        check("the text reassembles to the exact prompt ACROSS ALL MESSAGES",
              wire_text(sent["body"]) == prompt,
              f"got {len(wire_text(sent['body']))} B vs {len(prompt)} B")

        cc._openrouter_call_blocking("glm", ["z-ai/glm-5.2"], prompt,
                                     cache_prefix=prefix)
        content = sent["body"]["messages"][0]["content"]
        check("glm STILL receives an unchanged plain string (no regression)",
              content == prompt, f"got {type(content).__name__}")

        cc._openrouter_call_blocking("glm", ["z-ai/glm-5.2"], prompt)
        check("omitting cache_prefix keeps the legacy plain-string call working",
              sent["body"]["messages"][0]["content"] == prompt)
    finally:
        cc.urllib.request.urlopen = real_urlopen
        if real_key is None:
            cc.os.environ.pop(cc.OPENROUTER_KEY_ENV, None)
        else:
            cc.os.environ[cc.OPENROUTER_KEY_ENV] = real_key


def caller_checks() -> None:
    """Drive the REAL run_openrouter, not the transport directly.

    The transport-level test above proves _openrouter_call_blocking honours a
    cache_prefix it is HANDED. It cannot prove run_openrouter hands it one -- a caller
    that never passes the argument would leave the default "" and silently emit no
    breakpoint on every real fire, with every other test still green. That is the
    delivery gap this closes, and it is why the assertion is on the WIRE BODY rather
    than on run_openrouter's return value.
    """
    print("\nCALLER: run_openrouter -> transport -> wire")
    sent: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "model": "m",
                "choices": [{"message": {"content": "VERDICT: PASS\nREASONS:\n- ok"}}],
            }).encode()

    def _stub(req, timeout=None):
        sent["body"] = json.loads(req.data.decode())
        return _Resp()

    sysp = "SYSTEM BAR TEXT\nthe quality bar\n"
    real_urlopen = cc.urllib.request.urlopen
    real_key = cc.os.environ.get(cc.OPENROUTER_KEY_ENV)
    cc.urllib.request.urlopen = _stub
    cc.os.environ[cc.OPENROUTER_KEY_ENV] = "test-not-a-real-key"
    try:
        expected = cc.build_prompt(sysp, "PITCH TEXT", "## Evidence\nE",
                                   "## Directives\nD", "", "", "## Rules\nR", "")

        asyncio.run(cc.run_openrouter(
            "probe", [explicit_slug()], "PITCH TEXT", sysp,
            evidence_block="## Evidence\nE", user_directives_block="## Directives\nD",
            standing_rules_block="## Rules\nR"))
        content = sent["body"]["messages"][0]["content"]
        check("run_openrouter emits typed parts for an explicit-list slug "
              "(breakpoint delivered)",
              isinstance(content, list), f"got {type(content).__name__}")
        if isinstance(content, list):
            check("the cached part is exactly the system prompt",
                  content[0]["text"] == sysp)
            check("the breakpoint is ephemeral",
                  content[0].get("cache_control") == {"type": "ephemeral"})
            check("the wire text still equals build_prompt's output EXACTLY",
                  wire_text(sent["body"]) == expected,
                  f"got {len(wire_text(sent['body']))} B vs {len(expected)} B")
            # An explicit slug that does NOT need the message split (anthropic: its
            # cache_control is content-block granular) must keep the single user
            # message. Asserting ["system","user"] for every explicit slug -- which an
            # earlier version of this check did -- would lock in the very over-broad
            # reshaping the council caught.
            check("an explicit slug that does NOT need the split keeps ONE user message",
                  [m["role"] for m in sent["body"]["messages"]] == ["user"],
                  f"roles={[m['role'] for m in sent['body']['messages']]}")

        split_slug = cc.MESSAGE_SPLIT_MODEL_PREFIXES[0] + "probe-model"
        asyncio.run(cc.run_openrouter(
            "probe", [split_slug], "PITCH TEXT", sysp,
            evidence_block="## Evidence\nE", user_directives_block="## Directives\nD",
            standing_rules_block="## Rules\nR"))
        msgs = sent["body"]["messages"]
        check("a SPLIT slug gets a system message then a user message",
              [m["role"] for m in msgs] == ["system", "user"],
              f"roles={[m['role'] for m in msgs]}")
        check("the marker rides on the SYSTEM message's only part",
              msgs[0]["content"][0].get("cache_control") == {"type": "ephemeral"}
              and len(msgs[0]["content"]) == 1)
        check("the split shape is still byte-exact across messages",
              wire_text(sent["body"]) == expected,
              f"got {len(wire_text(sent['body']))} B vs {len(expected)} B")

        asyncio.run(cc.run_openrouter(
            "glm", ["z-ai/glm-5.2"], "PITCH TEXT", sysp,
            evidence_block="## Evidence\nE", user_directives_block="## Directives\nD",
            standing_rules_block="## Rules\nR"))
        content = sent["body"]["messages"][0]["content"]
        check("run_openrouter still sends glm an unchanged plain string",
              content == expected, f"got {type(content).__name__}")
    finally:
        cc.urllib.request.urlopen = real_urlopen
        if real_key is None:
            cc.os.environ.pop(cc.OPENROUTER_KEY_ENV, None)
        else:
            cc.os.environ[cc.OPENROUTER_KEY_ENV] = real_key


def main() -> int:
    selection_checks()
    reassembly_checks()
    delivery_checks()
    caller_checks()
    print(f"\nFAILURES: {len(FAILURES)}" + (f" -> {FAILURES}" if FAILURES else ""))
    return 1 if FAILURES else 0


sys.exit(main())
