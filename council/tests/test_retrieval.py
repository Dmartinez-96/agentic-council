# requires: openrouter
# NOT because this suite makes a live call -- it stubs the transports. It drives a REAL
# main(), and main() DROPS every openrouter member when OPENROUTER_API_KEY is unset
# (`council: <seat> skipped (OPENROUTER_API_KEY not set)`), so the stubs never intercept
# and the run dies on `transport stubs did not intercept`. MEASURED 2026-08-06: exit 1
# without the key, 31/31 with it. The key gates whether the seats are SEATED, not whether
# the network is touched -- which is the same degraded-bench trap the project has hit
# twice, here surfacing as a test failure instead of a silent eleven-seat drop.
"""Tests for phase-1 member file-retrieval.

Scope: exercises the REAL retrieval functions (read_repo_file,
collect_file_requests, capability_block) and a REAL run of main() with the two
transport calls stubbed (no network, no codex subprocess). The transport stubs
are proven to intercept by the assertions themselves: if the module-level
rebinding did not take effect, `captured` would be empty and section E's
assertions would fail. The only mock of code-under-test is os.readlink in
section A, to force the fail-closed branch a static filesystem cannot reach.
"""
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consult_council as cc

N = [0]
TMP = []


def ok(label):
    N[0] += 1
    print(f"  [ok] {label}")


def mkwork():
    d = Path(tempfile.mkdtemp(prefix="retr_"))
    TMP.append(d)
    return d


# ---- a real workspace ------------------------------------------------------
work = mkwork()
external = mkwork()
(work / "src").mkdir()
(work / "src" / "hello.py").write_text("print('hi')\n# a normal source file\n")
(work / "src" / "other.py").write_text("# OTHER FILE\nprint('other')\n")
(external / "secret.txt").write_text("SENSITIVE EXTERNAL DATA\n")
os.symlink(external / "secret.txt", work / "escape_link")     # -> outside jail
os.symlink(work / "src" / "hello.py", work / "inside_link")   # -> inside jail
(work / "linked_a.txt").write_text("hardlinked content\n")
os.link(work / "linked_a.txt", work / "linked_b.txt")         # nlink == 2
(work / "app_api_key.txt").write_text("KEY=abc\n")
(work / ".hidden").write_text("dot\n")

print("=== A. read_repo_file: security + correctness ===")
CASES = [
    ("src/hello.py", True, None),
    ("inside_link", True, None),                  # symlink resolving INSIDE -> ok
    ("../etc/passwd", False, "'..'"),
    # An absolute path is no longer refused for BEING absolute -- it is refused for pointing
    # outside the workdir, which is the property that was ever worth enforcing. The in-workdir
    # case, and the jail surviving the rewrite, are covered in test_retrieval_spans.py section F.
    ("/etc/passwd", False, "outside"),
    ("~/x", False, "home-relative"),
    ("escape_link", False, "outside"),            # pre-open resolve containment
    ("linked_a.txt", False, "multiply-linked"),   # hard-link escape (nlink!=1)
    ("app_api_key.txt", False, "denied pattern"),
    (".hidden", False, "dotfile"),
    ("nope.py", False, "not found"),
    ("src", False, "not a regular file"),
    ("z" * (cc.REQUEST_PATH_MAX_LEN + 1), False, "too long"),
]
for rel, grant, needle in CASES:
    content, note = cc.read_repo_file(work, rel)
    assert (content is not None) == grant, f"{rel!r}: {note!r}"
    if not grant:
        assert needle in note, f"{rel!r}: want {needle!r} in {note!r}"
    ok(f"{(rel[:38]):40} -> {'GRANT' if content is not None else 'DENY: ' + note}")

c, _ = cc.read_repo_file(work, "src/hello.py")
assert c == "print('hi')\n# a normal source file\n"
ok("granted content matches file byte-for-byte")

(work / "big.txt").write_text("x" * (cc.RETRIEVAL_PER_FILE_CAP + 5000))
c, note = cc.read_repo_file(work, "big.txt")
assert len(c.encode()) == cc.RETRIEVAL_PER_FILE_CAP - 8
assert note.startswith(f"truncated to {cc.RETRIEVAL_PER_FILE_CAP - 8} of")
ok(f"per-file cap: {note}")

_orig = os.readlink
os.readlink = lambda *a, **k: (_ for _ in ()).throw(OSError("no /proc"))
try:
    c, note = cc.read_repo_file(work, "src/hello.py")
finally:
    os.readlink = _orig
assert c is None and "containment verification unavailable" in note, note
ok("fail-closed: fd containment unverifiable -> DENY (not served on weaker check)")

print("=== B. collect_file_requests: parsing, isolation, caps, accounting ===")


def r1(name, text):
    return {"role": name, "verdict": "WARN", "text": text}


# Look a NAMED seat up in DEFAULT_REGISTRY, not via member_by_name. member_by_name reads
# the ACTIVE registry, which is roster.json when the operator has one -- so any assertion
# naming a specific seat failed the moment anyone edited their roster, and the GUI now
# makes that a normal thing to do. A user's roster composition is their business; what
# this file tests is the ENGINE's behaviour, so it pins the built-in default, fixed in
# source. member_by_name is still the right call when the ACTIVE registry is the subject
# -- the "stranger" check below is exactly that, and stays.
def default_by_name(n):
    return next((m for m in cc.DEFAULT_REGISTRY if m.name == n), None)


assert "file_retrieval" in default_by_name("gemini").capabilities
# every default member holds file_retrieval, so the "ignored" case uses a name that
# is NOT in the registry (lookup -> None -> no capabilities -> ignored).
assert cc.member_by_name("stranger") is None

# isolation (distinct files) + unknown/non-capability member ignored
blocks, log = cc.collect_file_requests([
    r1("gemini", "REQUEST_FILE: src/hello.py"),
    r1("deepseek", "REQUEST_FILE: linked_a.txt"),
    r1("stranger", "REQUEST_FILE: src/hello.py"),     # not in registry -> ignored
], work)
assert set(blocks) == {"gemini", "deepseek"}
assert "print('hi')" in blocks["gemini"] and "print('hi')" not in blocks["deepseek"]
assert "multiply-linked" in blocks["deepseek"] and "multiply-linked" not in blocks["gemini"]
ok("isolation: each member sees only its own files; non-capability member ignored")

# EXACT accounting: reconstruct the expected block byte-for-byte
ga, gbtxt = "AAA\n", "BBBBB\n"
(work / "a.txt").write_text(ga)
(work / "b.txt").write_text(gbtxt)
blocks, _ = cc.collect_file_requests(
    [r1("gemini", "REQUEST_FILE: a.txt\nREQUEST_FILE: b.txt")], work)
WRAPPER = ("## Requested repo files (your round-1 REQUEST_FILE lines)\n\n"
           "Delivered to YOU alone; other members did not receive these.\n\n")
sec_a = f"### a.txt ({len(ga)} bytes)\n```\n{ga}\n```"
sec_b = f"### b.txt ({len(gbtxt)} bytes)\n```\n{gbtxt}\n```"
expected = WRAPPER + sec_a + "\n\n" + sec_b
assert blocks["gemini"] == expected, repr(blocks["gemini"])
ok(f"byte-exact block: {len(expected.encode())} bytes reconstructed identically")

# per-member cap: 3 processed + 1 summary, then stop
blocks, log = cc.collect_file_requests(
    [r1("gemini", "\n".join(f"REQUEST_FILE: f{i}.py" for i in range(10)))], work)
assert blocks["gemini"].count("### ") == cc.RETRIEVAL_MAX_REQUESTS_PER_MEMBER + 1
assert "further requests ignored" in blocks["gemini"]
ig = [e for e in log["requests"] if e.get("over_cap_ignored")]
assert ig[0]["over_cap_ignored"] == 10 - cc.RETRIEVAL_MAX_REQUESTS_PER_MEMBER
ok(f"per-member cap: 3 processed + 1 summary; {ig[0]['over_cap_ignored']} ignored")

# THE PER-FIRE CAP MUST NOT BIND BEFORE THE OTHER TWO. That is the whole point of deriving it
# (m * n * z, the user's ruling 2026-08-03), and it is the property that replaced this block's
# old assertion. The old test pinned the OPPOSITE: three files whose content fit under a flat
# 64,000 while the third was denied on overhead. That denial was the defect -- a member using
# its full allowance of 3 was starved by a bench-wide budget smaller than the allowance -- so
# the test asserting it had to be rewritten, not merely re-tuned.
# Each file here is LARGER than the per-file cap, so every grant is a maximum-size one: the
# most a single member can possibly spend.
S = cc.RETRIEVAL_PER_FILE_CAP + 5000
for i in range(3):
    (work / f"g{i}.txt").write_text("y" * S)
blocks, log = cc.collect_file_requests(
    [r1("gemini", "\n".join(f"REQUEST_FILE: g{i}.txt" for i in range(3)))], work)
granted = [e for e in log["requests"] if e.get("granted")]
denied_budget = [e for e in log["requests"]
                 if e.get("reason") == "per-fire delivery budget exhausted"]
assert len(granted) == cc.RETRIEVAL_MAX_REQUESTS_PER_MEMBER and not denied_budget, (
    len(granted), len(denied_budget))
assert log["fire_cap"] == cc.retrieval_fire_cap(1) and log["retrievers"] == 1, log["fire_cap"]
# OVERHEAD IS STILL CHARGED EXACTLY -- the reason the old test existed. Comparing the summed
# per-request `delivered_bytes` against the block's SECTIONS proves nothing about overhead:
# both sides are section bytes, so it passes even if the wrapper and joins were never charged.
# The discriminating comparison is against delivered_total, which is what the budget gate
# actually spends, versus the block's FULL byte length including wrapper and joins.
gbytes = sum(e["delivered_bytes"] for e in granted)
grant_section_bytes = sum(
    len(s.encode()) for s in blocks["gemini"].split("\n\n")
    if s.startswith("### ") and "```" in s)
assert gbytes == grant_section_bytes, (gbytes, grant_section_bytes)
whole_block = len(blocks["gemini"].encode())
assert log["delivered_total"] == whole_block, (log["delivered_total"], whole_block)
assert log["delivered_total"] > gbytes, "wrapper/join overhead was not charged at all"
ok(f"per-fire cap does NOT bind before the per-member/per-file caps: "
   f"{len(granted)}/{cc.RETRIEVAL_MAX_REQUESTS_PER_MEMBER} max-size grants fit under "
   f"{log['fire_cap']}B; charged total {log['delivered_total']}B == whole block, "
   f"{log['delivered_total'] - gbytes}B of it wrapper/join overhead")

# THE CAP SCALES WITH THE BENCH, m * n * z. Asserting only n=1 would pass on an implementation
# that ignored n entirely, so this checks two bench sizes AND the counting of retrievers from
# the batch. The exclusion arm covered here is the UNKNOWN-NAME one (member_by_name -> None),
# not the has-no-capability one; the note below says why the latter is unreachable on this
# roster, and it is left uncovered rather than pretended.
per_one = cc.retrieval_fire_cap(1)
assert cc.retrieval_fire_cap(2) == 2 * per_one, cc.retrieval_fire_cap(2)
assert cc.retrieval_fire_cap(6) == 6 * per_one, cc.retrieval_fire_cap(6)
assert cc.retrieval_fire_cap(0) == 0
# The third entry is a name the roster does not know, so member_by_name returns None. EVERY
# seat on the real roster holds file_retrieval (checked: 12 of 12), so an unknown name is the
# only way to exercise the exclusion branch at all -- without this arm, an implementation that
# counted len(round1_results) blindly would pass.
blocks2, log2 = cc.collect_file_requests([
    r1("gemini", "REQUEST_FILE: g0.txt"),
    r1("deepseek", "REQUEST_FILE: g1.txt"),
    r1("not_a_seat", "REQUEST_FILE: g2.txt"),
], work)
assert log2["retrievers"] == 2 and log2["fire_cap"] == cc.retrieval_fire_cap(2), log2["fire_cap"]
assert set(blocks2) == {"gemini", "deepseek"}, set(blocks2)   # the unknown seat got nothing
assert not any(e["member"] == "not_a_seat" for e in log2["requests"])
ok(f"fire cap scales m*n*z: n=1 -> {per_one}B, n=2 -> {cc.retrieval_fire_cap(2)}B, "
   f"and a 2-member batch is counted as n=2")

# THE GATE ITSELF STILL EXISTS, and the budget is still SHARED across members rather than
# reset per member. Both were previously demonstrated by starvation under the flat cap; with a
# derived cap that cannot starve anyone, they are demonstrated by shrinking the cap instead --
# testing the mechanism rather than the number. A per-member reset would grant deepseek here.
_real_cap = cc.retrieval_fire_cap
# DERIVED from a real grant, not a round number: each file here is larger than the per-file
# cap, so one grant costs about that cap plus its section overhead. A literal (30_000 was the
# first attempt) stopped fitting even ONE grant the moment the per-file cap rose to 128k, and
# the test then failed for the wrong reason -- gemini denied too, rather than deepseek alone.
_one_grant = cc.RETRIEVAL_PER_FILE_CAP + cc.RETRIEVAL_WRAPPER_ALLOWANCE
cc.retrieval_fire_cap = lambda n: _one_grant    # room for one max-size grant, not two
try:
    blocks, log = cc.collect_file_requests([
        r1("gemini", "REQUEST_FILE: g0.txt"),
        r1("deepseek", "REQUEST_FILE: g1.txt"),
    ], work)
finally:
    cc.retrieval_fire_cap = _real_cap
g_ok = [e for e in log["requests"] if e["member"] == "gemini" and e.get("granted")]
d_deny = [e for e in log["requests"] if e["member"] == "deepseek"
          and e.get("reason") == "per-fire delivery budget exhausted"]
assert len(g_ok) == 1 and len(d_deny) == 1, (len(g_ok), len(d_deny))
assert cc.retrieval_fire_cap is _real_cap, "the patched cap leaked out of the test"
ok("per-fire budget is SHARED and the gate still fires when the cap IS exceeded "
   "(deepseek denied after gemini spent a deliberately shrunken budget)")

# long path: display truncated in block, FULL path kept in the log
longp = "d/" + "a" * 300 + ".py"
blocks, log = cc.collect_file_requests([r1("gemini", f"REQUEST_FILE: {longp}")], work)
assert "..." in blocks["gemini"] and len(blocks["gemini"]) < len(longp) + 400
assert any(e.get("path") == longp for e in log["requests"])
ok("long path: block display truncated; full path preserved in audit log")

print("=== C. capability_block generated from the record ===")
codex, gemini = default_by_name("codex"), default_by_name("gemini")
assert "read-only sandbox" in cc.capability_block(codex).lower()
ok("codex_subprocess -> sandbox text")
fb = cc.capability_block(codex, fallback_route=True).lower()
# the fallback drops codex's OWN repo sandbox paragraph, but its harness-mediated caps
# (file/web/exec) survive the transport swap, so REQUEST_FILE is still offered. ("sandbox"
# alone now appears via the exec_sandbox paragraph, so we check the codex-specific text.)
assert "codex exec" not in fb and "read-only sandbox over the real repository" not in fb
assert "request_file:" in fb and "request_url:" in fb and "request_exec:" in fb
ok("codex fallback_route -> drops codex sandbox, keeps all 3 harness caps")
assert "REQUEST_FILE:" in cc.capability_block(gemini)
ok("file_retrieval member -> REQUEST_FILE channel text")
bare = cc.capability_block(cc.Member("bare", cc.VOTING, "openrouter", "x/y")).lower()
assert "no filesystem" in bare and "request_file" not in bare
ok("no-capability member -> no-access text")

print("=== E. FULL main() run: request in round 1 -> delivered in round 2 ===")
captured = {"r2": {}, "r1_sys": {}}
REQ = {"gemini": "src/hello.py", "deepseek": "src/other.py"}   # DISTINCT files


def _text(role, r1_voting):
    if r1_voting and role in REQ:
        return f"VERDICT: WARN\nREQUEST_FILE: {REQ[role]}"
    return "VERDICT: PASS"


async def stub_or(role, models, pitch, system_prompt, ev="", ud="", r1="",
                  ab="", sr="", ccb=""):
    r1_voting = (r1 == "" and ccb == "")
    if r1_voting:
        captured["r1_sys"][role] = system_prompt
    if r1:
        captured["r2"][role] = r1
    t = _text(role, r1_voting)
    return {"role": role, "text": t, "stderr": "", "returncode": 0,
            "verdict": cc.parse_verdict(t), "duration_s": 0.0,
            "model_used": models[0] if models else ""}


async def stub_codex(pitch, system_prompt, cwd, ev="", ud="", r1="", ab="",
                     sr="", ccb=""):
    if r1 == "" and ccb == "":
        captured["r1_sys"]["codex"] = system_prompt
    if r1:
        captured["r2"]["codex"] = r1
    return {"role": "codex", "text": "VERDICT: PASS", "stderr": "",
            "returncode": 0, "verdict": "PASS", "duration_s": 0.0}


cc.run_openrouter = stub_or
cc.run_codex = stub_codex
cc.SHADOW_PATH = Path("/nonexistent-disable-layer2")   # no shadow calls in test
logroot = mkwork()
cc.LOGS_ROOT = logroot
pitchfile = work / "pitch.txt"
pitchfile.write_text("Review this change. (test pitch)\n")
sys.argv = ["consult_council.py", "--layer", "reasoning",
            "--workdir", str(work), "--prompt-file", str(pitchfile)]
rc = asyncio.run(cc.main())
print(f"  main() returned rc={rc}")

# stubs proven to intercept: captured is populated only if they ran
assert captured["r1_sys"] and captured["r2"], "transport stubs did not intercept"

# 1. round-1 prompts carried each member's capability block.
# DERIVED, not named. This block used to assert on "gemini" and "codex" literally, which
# tied it to one roster: the moment codex moved to the LEADER slot it stopped being a
# dispatched voter and this KeyError'd. The property under test is that a direct-vendor
# SUBPROCESS seat's block differs from an OPENROUTER seat's -- whichever seats those are.
_disp = captured["r1_sys"]
_or_seat = next((m.name for m in cc.voting_members()
                 if m.transport == "openrouter" and m.name in _disp), None)
_sub_seat = next((m.name for m in cc.voting_members()
                  if m.transport == "codex_subprocess" and m.name in _disp), None)
assert _or_seat, f"no openrouter voting seat was dispatched; got {sorted(_disp)}"
assert "## Your capabilities" in _disp[_or_seat]
assert "REQUEST_FILE:" in _disp[_or_seat]
if _sub_seat:
    assert "read-only sandbox" in _disp[_sub_seat].lower()
    assert "read-only sandbox" not in _disp[_or_seat].lower()
    ok(f"round-1 prompt: capability_block differs by transport "
       f"({_sub_seat} subprocess vs {_or_seat} openrouter)")
else:
    # Reported, not asserted: a roster with no codex_subprocess voter is legitimate (this
    # one has codex as LEADER), and failing on it would re-create the coupling just removed.
    print("  [skip] no codex_subprocess VOTING seat on the active roster -- "
          "the transport-differs leg was not exercised")
    ok(f"round-1 prompt: capability_block appended ({_or_seat}, openrouter)")

# 2 and 3, rewritten as one unit so no statement here outlives what it describes.
#
# WHAT IS FIXTURE AND WHAT IS ROSTER, since conflating the two is what kept breaking this
# file: the stub above emits REQUEST_FILE lines for the role names "gemini" and "deepseek"
# specifically, so those two are the FIXTURE and the delivery/log legs are only meaningful
# when both were dispatched. Everything else -- which other seats exist, and whether any
# exist -- is the operator's roster and is DISCOVERED, never named. An earlier version
# named "codex" in section 2 (section 3 only ever named the fixture pair). What was
# OBSERVED when codex moved from voting seat to
# LEADER and stopped being dispatched: a KeyError at the capability-block line, which
# aborted the run before these sections executed. Their own "codex" references had not run
# yet, so their failure mode is inferred from the code, not measured.
_r2 = captured["r2"]
_fixture_ran = "gemini" in _r2 and "deepseek" in _r2

if _fixture_ran:
    g2, d2 = _r2["gemini"], _r2["deepseek"]
    assert "print('hi')" in g2 and "print('other')" not in g2      # gemini's only
    assert "print('other')" in d2 and "print('hi')" not in d2      # deepseek's only
    ok("round-2: each requester received ONLY its own file")
else:
    print("  [skip] fixture requesters (gemini, deepseek) not both dispatched on this "
          f"roster ({sorted(_r2)}) -- per-requester delivery not exercised")

# Non-requesting seats get no delivery. REPORTED when there are none: a roster consisting
# only of the two fixture requesters is legitimate, and failing on it would rebuild the
# roster coupling this rewrite removes.
_quiet = [n for n in _r2 if n not in ("gemini", "deepseek")]
for _n in _quiet:
    assert "Requested repo files" not in _r2[_n], _n
if _quiet:
    ok(f"round-2: {len(_quiet)} non-requesting seat(s) received no delivery")
else:
    print("  [skip] no non-requesting seat dispatched -- no-delivery leg not exercised")

# 3. the fire's log recorded retrieval + capability provenance. Gated on the same fixture:
# without those two requests there is no grant to find in the log.
entry = json.load(open(sorted(logroot.glob("*/*.json"))[-1]))
if _fixture_ran:
    reqs = entry["retrieval"]["requests"]
    assert entry["retrieval"]["any_granted"] is True
    assert any(e["member"] == "gemini" and e["path"] == "src/hello.py"
               and e["granted"] for e in reqs)
    assert any(e["member"] == "deepseek" and e["path"] == "src/other.py"
               and e["granted"] for e in reqs)
    gem = [m for m in entry["roster"]["members"] if m["name"] == "gemini"][0]
    assert gem["capabilities"] == list(cc._DEFAULT_CAPS)  # every member holds every cap
    ok("log: retrieval provenance (both grants) + roster capabilities recorded")
else:
    print("  [skip] log retrieval-provenance leg needs the fixture requesters")

print("=== F. advisor delivers --workdir through to the wrapper (rule 2) ===")
import council_advisor as ca

cap = {}


class _Proc:
    stdout = "VERDICT: PASS\n"
    stderr = ""
    returncode = 0


def _fake_run(cmd, **kw):
    cap["cmd"] = list(cmd)
    return _Proc()


ca.subprocess.run = _fake_run
payload = {"tool_name": "Write",
           "tool_input": {"file_path": str(work / "x.py"), "content": "print(1)\n"},
           "tool_response": {}, "cwd": str(work), "session_id": "advtest",
           "transcript_path": ""}
_stdin = sys.stdin
sys.stdin = io.StringIO(json.dumps(payload))
try:
    rc = ca.main()
finally:
    sys.stdin = _stdin
assert "--workdir" in cap.get("cmd", []), cap.get("cmd")
wi = cap["cmd"].index("--workdir")
assert cap["cmd"][wi + 1] == str(work), cap["cmd"][wi + 1]
ok(f"advisor cmd carries --workdir={str(work)!r} to the engine (rc={rc})")

for d in TMP:
    shutil.rmtree(d, ignore_errors=True)
print(f"\nALL {N[0]} CHECKS PASSED")
