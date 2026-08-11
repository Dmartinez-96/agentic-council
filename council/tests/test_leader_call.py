"""Deterministic regression for consult_council._call_leader (driver piece a).

_call_leader is the transport-dispatched leader-model call: it returns the leader's
RAW text and NEVER consumes or exposes a verdict (a leader is a doer, never a voter).
This test monkeypatches the transport primitives -- no live model calls, no cost -- and
asserts:
  - dispatch by transport to the correct primitive (openrouter/gemini/deepseek/codex),
  - the prompt is passed THROUGH UNWRAPPED (NOT run through build_prompt, which would
    prepend "Proposal under review:"); the spy captures the exact prompt the primitive
    received and asserts equality with the raw input -- the falsifier is: if _call_leader
    wrapped it, captured != raw and the check FAILS,
  - the returned dict carries {ok,text,error,transport,model_used} and has NO "verdict"
    key (the no-verdict-consumption contract, made literal),
  - failures return ok=False when there is NO usable response: empty/blank text, or an
    unknown transport. A non-zero rc is NOT itself a failure -- a leader that answered
    while its CLI exited non-zero is a success, and there is a check pinning that,
  - codex dispatch takes+releases the codex lock and feeds the raw prompt on stdin.

Ships in council/tests/ (synced from the development tree). Re-run:
    python3 council/tests/test_leader_call.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consult_council as cc

P = []


def check(label, cond):
    P.append(bool(cond))
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")


DUMMY_CWD = Path("/tmp")
RAW = "GROUND RULES\n\nTASK: do the thing\n\nEmit actions in the envelope."


def call(leader, prompt=RAW, cwd=DUMMY_CWD):
    return asyncio.run(cc._call_leader(leader, prompt, cwd))


# ---------------------------------------------------------------------------
# 1. openrouter transport: dispatch, raw text, model_used, prompt-unwrapped
# ---------------------------------------------------------------------------
seen = {}


def fake_openrouter(role, models, prompt):
    seen["role"] = role
    seen["models"] = models
    seen["prompt"] = prompt
    return {"role": role, "text": "LEADER SAID X", "stderr": "",
            "returncode": 0, "verdict": "PASS", "duration_s": 0.1,
            "model_used": models[0]}


cc._openrouter_call_blocking = fake_openrouter
lead_or = cc.Member("lead", cc.LEADER, "openrouter",
                    "anthropic/claude-opus-4", fallback_model="anthropic/claude-3.7",
                    capabilities=cc.LEADER_CAPS)
r = call(lead_or)
check("openrouter: ok + raw text returned",
      r["ok"] and r["text"] == "LEADER SAID X")
check("openrouter: dispatched with [primary, fallback] models",
      seen.get("models") == ["anthropic/claude-opus-4", "anthropic/claude-3.7"])
check("openrouter: prompt passed THROUGH UNWRAPPED (no build_prompt framing)",
      seen.get("prompt") == RAW and "Proposal under review" not in seen.get("prompt", ""))
check("openrouter: model_used propagated", r["model_used"] == "anthropic/claude-opus-4")
check("openrouter: transport recorded", r["transport"] == "openrouter")
check("result dict has NO 'verdict' key (no-verdict-consumption contract)",
      "verdict" not in r)

# a leader with no fallback -> single-element models list
seen.clear()
lead_nofb = cc.Member("lead", cc.LEADER, "openrouter", "x/y", capabilities=cc.LEADER_CAPS)
call(lead_nofb)
check("openrouter: no fallback -> single-element models list",
      seen.get("models") == ["x/y"])


# ---------------------------------------------------------------------------
# 2. gemini_rest and deepseek_https transports
# ---------------------------------------------------------------------------
gseen = {}


def fake_gemini(prompt):
    gseen["prompt"] = prompt
    return {"role": "gemini", "text": "GEM OUT", "stderr": "",
            "returncode": 0, "verdict": "WARN", "duration_s": 0.1}


cc._gemini_api_call_blocking = fake_gemini
# BOGUS Member.model on purpose: the gemini_rest primitive runs a FIXED model
# (GEMINI_API_URL embeds GEMINI_API_MODEL), so model_used must report the constant,
# NOT this Member.model. Falsifier: if _call_leader echoed leader.model, model_used
# would be "bogus-gemini-model" and the model_used check below would FAIL.
lead_g = cc.Member("lead", cc.LEADER, "gemini_rest", "bogus-gemini-model",
                   capabilities=cc.LEADER_CAPS)
r = call(lead_g)
check("gemini_rest: ok + raw text, prompt unwrapped, no verdict key",
      r["ok"] and r["text"] == "GEM OUT" and gseen.get("prompt") == RAW
      and "verdict" not in r)
check("gemini_rest: model_used is the PINNED constant, not the (bogus) Member.model",
      r["model_used"] == cc.GEMINI_API_MODEL and r["model_used"] != "bogus-gemini-model")

dseen = {}


def fake_deepseek(prompt):
    dseen["prompt"] = prompt
    return {"role": "deepseek", "text": "DS OUT", "stderr": "",
            "returncode": 0, "verdict": "PASS", "duration_s": 0.1}


cc._deepseek_call_blocking = fake_deepseek
# BOGUS Member.model (same rationale as gemini): the deepseek body sends DEEPSEEK_MODEL.
lead_d = cc.Member("lead", cc.LEADER, "deepseek_https", "bogus-deepseek-model",
                   capabilities=cc.LEADER_CAPS)
r = call(lead_d)
check("deepseek_https: ok + raw text, prompt unwrapped, no verdict key",
      r["ok"] and r["text"] == "DS OUT" and dseen.get("prompt") == RAW
      and "verdict" not in r)
check("deepseek_https: model_used is the PINNED constant, not the (bogus) Member.model",
      r["model_used"] == cc.DEEPSEEK_MODEL and r["model_used"] != "bogus-deepseek-model")


# ---------------------------------------------------------------------------
# 3. FAIL-CLOSED paths: primitive failure, empty text, unknown transport
# ---------------------------------------------------------------------------
def fake_openrouter_fail(role, models, prompt):
    return {"role": role, "text": "", "stderr": "HTTPError 429: rate limited",
            "returncode": -1, "verdict": "ERROR", "duration_s": 0.1}


cc._openrouter_call_blocking = fake_openrouter_fail
r = call(lead_or)
# NOTE ON WHAT THIS DOES AND DOES NOT DISCRIMINATE: this stub returns BOTH rc=-1 and
# empty text, so a passing assertion cannot attribute ok=False to either one. It is kept
# because it is the realistic shape of a primitive failure (a transport that errors
# returns no text), and because the error must still reach the caller. The two cases
# below separate the variables, which this one cannot.
check("primitive failure (rc=-1, no text) -> ok=False with error surfaced",
      not r["ok"] and "429" in r["error"])


# THE REGRESSION THIS FILE PREVIOUSLY HAD NO COVER FOR. A CLI can exit non-zero for a
# reason unrelated to the answer and still have produced a complete one.
# WHAT WAS OBSERVED 2026-08-01, stated to match consult_council.py rather than
# over-claiming: a codex leader turn returned a full actions block and "tokens used
# 7,634" and was still reported as "leader call failed". That run's exit status was never
# captured, so whether it failed on rc or on an empty --output-last-message file is
# UNKNOWN. Keying `ok` on the RESPONSE closes the RC HALF ONLY -- an
# --output-last-message file that EXISTS but is empty still yields text="" and still
# fails, because the stdout fallback fires only when the file is absent. This check pins
# the half that is closed: if someone reinstates `rc == 0 and text`, it goes red.
def fake_openrouter_rc_nonzero_with_text(role, models, prompt):
    return {"role": role, "text": "LEADER ANSWERED ANYWAY", "stderr": "WARN: stale cache",
            "returncode": 1, "verdict": "PASS", "duration_s": 0.1, "model_used": "x/y"}


cc._openrouter_call_blocking = fake_openrouter_rc_nonzero_with_text
r = call(lead_or)
check("rc!=0 BUT usable text -> ok=True (the answer is not discarded)",
      r["ok"] and r["text"] == "LEADER ANSWERED ANYWAY")
check("rc!=0 with usable text -> no error surfaced (it succeeded)", not r["error"])


def fake_openrouter_empty(role, models, prompt):
    return {"role": role, "text": "   \n", "stderr": "", "returncode": 0,
            "verdict": "UNPARSEABLE", "duration_s": 0.1, "model_used": "x/y"}


cc._openrouter_call_blocking = fake_openrouter_empty
r = call(lead_or)
check("rc=0 but blank text -> ok=False (a blank leader turn is not a success)",
      not r["ok"])

lead_bad = cc.Member("lead", cc.LEADER, "no_such_transport", "x",
                     capabilities=cc.LEADER_CAPS)
r = call(lead_bad)
check("unknown transport -> ok=False with a clear error, no crash",
      not r["ok"] and "transport" in r["error"].lower() and "verdict" not in r)


# ---------------------------------------------------------------------------
# 4. codex_subprocess: lock taken+released, raw prompt on stdin, codex_cmd used
# ---------------------------------------------------------------------------
codex_spy = {"lock_acquired": 0, "lock_released": [], "run_args": None}


def fake_lock_acquire():
    codex_spy["lock_acquired"] += 1
    return "LOCK_FH"


def fake_lock_release(fh):
    codex_spy["lock_released"].append(fh)


async def fake_run_subprocess(cmd, cwd, role, post_read=None, stdin_data=None):
    codex_spy["run_args"] = {"cmd": cmd, "cwd": cwd, "role": role,
                             "post_read": post_read, "stdin_data": stdin_data}
    return {"role": role, "text": "CODEX LED", "stderr": "", "returncode": 0,
            "verdict": "PASS", "duration_s": 0.2}


cc._codex_lock_acquire = fake_lock_acquire
cc._codex_lock_release = fake_lock_release
cc._run_subprocess = fake_run_subprocess

lead_cx = cc.Member("lead", cc.LEADER, "codex_subprocess", "bogus-codex-model",
                    capabilities=cc.LEADER_CAPS)
r = call(lead_cx, cwd=DUMMY_CWD)
check("codex: ok + raw text returned", r["ok"] and r["text"] == "CODEX LED")
check("codex: model_used is CODEX_MODEL (codex_cmd pins it), not the (bogus) Member.model",
      r["model_used"] == cc.CODEX_MODEL and r["model_used"] != "bogus-codex-model")
check("codex: lock acquired exactly once", codex_spy["lock_acquired"] == 1)
check("codex: lock released with the acquired handle (finally-safe)",
      codex_spy["lock_released"] == ["LOCK_FH"])
ra = codex_spy["run_args"] or {}
check("codex: dispatched via codex_cmd (cmd starts with ['codex','exec'])",
      (ra.get("cmd") or [])[:2] == ["codex", "exec"])
check("codex: the command actually pins model=CODEX_MODEL (measured, not narrated)",
      any(f'model="{cc.CODEX_MODEL}"' in str(x) for x in (ra.get("cmd") or [])))
check("codex: RAW prompt fed on stdin (not build_prompt-wrapped)",
      ra.get("stdin_data") == RAW)
check("codex: post_read set (output-last-message file) and cwd threaded",
      ra.get("post_read") is not None and ra.get("cwd") == DUMMY_CWD)
check("codex: result dict has NO 'verdict' key", "verdict" not in r)

# codex FAIL path still releases the lock
codex_spy["lock_acquired"] = 0
codex_spy["lock_released"] = []


async def fake_run_subprocess_fail(cmd, cwd, role, post_read=None, stdin_data=None):
    return {"role": role, "text": "", "stderr": "codex boom", "returncode": 1,
            "verdict": "ERROR", "duration_s": 0.2}


cc._run_subprocess = fake_run_subprocess_fail
r = call(lead_cx, cwd=DUMMY_CWD)
check("codex failure -> ok=False AND lock still released (finally)",
      not r["ok"] and codex_spy["lock_released"] == ["LOCK_FH"])



# ---------------------------------------------------------------------------
# 5. claude_subprocess: TOOL-LESS argv, raw prompt on stdin, no codex lock taken
# ---------------------------------------------------------------------------
# THE POINT OF THESE CHECKS IS THE TOOL LIST. A claude LEADER runs with an EMPTY --tools,
# not the voting seat's Read,Glob,Grep, because the CLI's native Read enforces neither the
# workdir jail nor the secrets deny-list that the harness READ path does. If someone
# "helpfully" reuses claude_cmd() here, the guard check below goes red.
claude_spy = {}
codex_spy["lock_acquired"] = 0


async def fake_run_subprocess_claude(cmd, cwd, role, post_read=None, stdin_data=None,
                                     drop_env=None):
    claude_spy.update({"cmd": cmd, "role": role, "stdin_data": stdin_data,
                       "post_read": post_read, "drop_env": drop_env})
    return {"role": role, "text": "CLAUDE LED", "stderr": "", "returncode": 0,
            "verdict": "PASS", "duration_s": 0.3}


cc._run_subprocess = fake_run_subprocess_claude
lead_cl = cc.Member("lead", cc.LEADER, "claude_subprocess", "bogus-claude-model",
                    capabilities=cc.LEADER_CAPS)
r = call(lead_cl, cwd=DUMMY_CWD)
check("claude: ok + raw text returned", r["ok"] and r["text"] == "CLAUDE LED")
check("claude: model_used is CLAUDE_MODEL, not the (bogus) Member.model",
      r["model_used"] == cc.CLAUDE_MODEL and r["model_used"] != "bogus-claude-model")
cargs = claude_spy.get("cmd") or []
check("claude: dispatched via claude_leader_cmd (argv starts ['claude','-p'])",
      cargs[:2] == ["claude", "-p"])
check("claude: the tool list is EMPTY -- the leader guard, not the voting seat's guard",
      list(cc.CLAUDE_LEADER_TOOL_GUARD) == ["--tools", ""]
      and cargs[-2:] == ["--tools", ""]
      and cargs != cc.claude_cmd())
check("claude: RAW prompt on stdin (argv-bounded prompts are how codex hit Errno 7)",
      claude_spy.get("stdin_data") == RAW)
check("claude: ANTHROPIC_* dropped so the CLI's own login serves",
      tuple(claude_spy.get("drop_env") or ()) == cc.CLAUDE_DROP_ENV)
check("claude: does NOT take the codex auth lock (that lock is for codex's own races)",
      codex_spy["lock_acquired"] == 0)
check("claude: result dict has NO 'verdict' key", "verdict" not in r)
# THE TUPLE AND THE CHAIN MUST AGREE -- the file says so in a comment, so assert it.
check("claude_subprocess is offerable as a leader (LEADER_TRANSPORTS lists it)",
      "claude_subprocess" in cc.LEADER_TRANSPORTS)
# THE TUPLE-VS-CHAIN INVARIANT, ACTUALLY EXERCISED. A membership test over VALID_TRANSPORTS
# would prove only that the names are spelled right -- it cannot tell an offered transport
# that dispatches from one that falls through to the else. So DRIVE each offered transport
# and assert none of them lands on the unknown-transport error.
# THE FALSIFIER IS PINNED FIRST, and it has to be. The earlier `lead_bad` check asserts only
# `"transport" in error.lower()`, so it does NOT establish the exact wording this loop keys
# on -- if the else branch were reworded, the loop would report "dispatched" for a transport
# with no branch and pass vacuously. The control below pins the literal, so the loop's
# negative test means something.
UNROUTED = "unknown leader transport"
check(f"CONTROL: an unrouted transport produces the literal {UNROUTED!r} the loop keys on",
      UNROUTED in (call(lead_bad).get("error") or ""))
dispatched = {}
for _t in cc.LEADER_TRANSPORTS:
    _m = cc.Member("lead", cc.LEADER, _t, "x/y", capabilities=cc.LEADER_CAPS)
    dispatched[_t] = UNROUTED not in (call(_m).get("error") or "")
check(f"EVERY offered leader transport actually dispatches (not just spelled right): "
      f"{dispatched}", all(dispatched.values()) and len(dispatched) == len(cc.LEADER_TRANSPORTS))
check("every LEADER_TRANSPORTS entry is also a registered transport",
      all(t in cc.VALID_TRANSPORTS for t in cc.LEADER_TRANSPORTS))

print(f"\n=== _call_leader: {sum(P)}/{len(P)} PASS ===")
sys.exit(0 if all(P) else 1)
