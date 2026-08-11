"""Deterministic regression for council_leader.run_leader_actions (driver piece b2).

Exercises the executor via INJECTED seams (read/fetch/run_exec/apply_write) -- no live
primitives, no council, no cost. Locks the contract the design review + the write-review
fix demand:
  - ORDER: actions execute in document order (WRITE before an EXEC of what it wrote).
  - WALL: only WRITE goes through apply_write (review_and_write); its council review text
    is echoed into `content` so the leader sees WHY a write warned/blocked (the bug the
    council caught), while `note` stays compact metadata.
  - ISOLATION: `note` never carries a retrieved body, and a fetch URL is reduced to host.
  - BOUNDS: per-call count caps and a per-kind byte budget deny (explicitly, ok=False),
    never silently skip.
  - exfil_context is forwarded to fetch as its anti-exfiltration comparison text.

Re-run:  python3 council/tests/test_leader_exec.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consult_council as cc
import council_leader as cl

P = []


def check(label, cond):
    P.append(bool(cond))
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")


WD = Path("/tmp")   # no real IO happens -- every primitive is an injected seam
LEADER = cc.Member("lead", cc.LEADER, "openrouter", "x/y", capabilities=cc.LEADER_CAPS)


def A(kind, arg, body=""):
    return cl.LeaderAction(kind, arg, body)


def run(actions, **seams):
    return cl.run_leader_actions(actions, WD, LEADER, **seams)


# ---------------------------------------------------------------------------
# 1. ORDER: write-then-exec runs in that order (the interleaving fix)
# ---------------------------------------------------------------------------
calls = []
spy_args = {}


def spy_apply(leader, path, content, workdir, *, session_id="", transcript_path="", review=None):
    spy_args["leader"] = leader
    calls.append(("write", path))
    return {"applied": True, "verdict": "PASS", "target": path, "review": "VERDICT: PASS\nok"}


def spy_exec(cmd, workdir):
    calls.append(("exec", cmd))
    return ("exec output", "exit 0")


res = run([A("write", "t.py", "print(1)"), A("exec", "python3 t.py")],
          apply_write=spy_apply, run_exec=spy_exec)
check("write-then-exec results in document order",
      [r.kind for r in res] == ["write", "exec"])
check("write actually executed BEFORE the exec (not reordered)",
      calls == [("write", "t.py"), ("exec", "python3 t.py")])
check("the EXACT leader passed to the write wall is the mutate-capable LEADER",
      spy_args.get("leader") is LEADER
      and LEADER.tier == cc.LEADER and cc.MUTATE in LEADER.capabilities)


# ---------------------------------------------------------------------------
# 2. read: content delivered, note is metadata-only (NO body -- isolation)
# ---------------------------------------------------------------------------
res = run([A("read", "f.py")], read=lambda wd, p: ("SECRET-BODY-XYZ", "13 bytes"))
check("read: full content delivered to the leader",
      res[0].ok and res[0].content == "SECRET-BODY-XYZ")
check("read: note carries metadata, NOT the body (confused-deputy isolation)",
      "13 bytes" in res[0].note and "SECRET-BODY" not in res[0].note)


# ---------------------------------------------------------------------------
# 3. fetch: note redacts URL to host; content has the page; exfil_context forwarded
# ---------------------------------------------------------------------------
seen = {}


def spy_fetch(url, ctx):
    seen["ctx"] = ctx
    return ("PAGE BODY", "status 200")


res = run([A("fetch", "https://docs.python.org/3/secret/path?token=abc")],
          fetch=spy_fetch, exfil_context="LEADER CONTEXT")
check("fetch: note keeps only the host, not the path/query (redaction)",
      "docs.python.org" in res[0].note
      and "secret/path" not in res[0].note and "token=abc" not in res[0].note)
check("fetch: content carries the page body", res[0].content == "PAGE BODY")
check("fetch: exfil_context forwarded to fetch_web_url as its comparison text",
      seen.get("ctx") == "LEADER CONTEXT")


# ---------------------------------------------------------------------------
# 4. denial (primitive returns None) -> ok=False, DENIED note, empty content
# ---------------------------------------------------------------------------
res = run([A("read", ".env")], read=lambda wd, p: (None, "dotfile denied"))
check("read denial -> ok=False, DENIED in note, no content",
      not res[0].ok and "DENIED" in res[0].note and "dotfile denied" in res[0].note
      and res[0].content == "")


# ---------------------------------------------------------------------------
# 5. per-call COUNT cap: cap+1 reads -> the extra is denied explicitly
# ---------------------------------------------------------------------------
ncap = cc.RETRIEVAL_MAX_REQUESTS_PER_MEMBER
res = run([A("read", f"f{i}.py") for i in range(ncap + 1)],
          read=lambda wd, p: ("x", "1 byte"))
check(f"read count cap ({ncap}): first {ncap} granted",
      all(r.ok for r in res[:ncap]))
check("read count cap: the extra read is DENIED (not silently skipped)",
      not res[ncap].ok and "read cap" in res[ncap].note)


# ---------------------------------------------------------------------------
# 6. per-call BYTE budget: reads past the leader's derived read budget are denied
# ---------------------------------------------------------------------------
# Taken from the SAME expression the leader uses, not a literal: the budget is now derived
# from the number of retrievers sharing it, and a hardcoded number here would silently stop
# tracking it.
_read_budget = cl._TOOL_CAPS["read"][1]
big = "x" * (_read_budget // 2 + 100)   # two of these overflow the cap
res = run([A("read", "a"), A("read", "b"), A("read", "c")],
          read=lambda wd, p: (big, f"{len(big)} bytes"))
check("byte budget: first read fits", res[0].ok)
check("byte budget: the read that overflows the budget is DENIED",
      not res[1].ok and "byte budget" in res[1].note)


# ---------------------------------------------------------------------------
# 7. WRITE PASS: review echoed into content (leader can read it); note stays compact
# ---------------------------------------------------------------------------
def apply_pass(leader, path, content, workdir, *, session_id="", transcript_path="", review=None):
    return {"applied": True, "verdict": "PASS", "target": path,
            "review": "VERDICT: PASS\nlooks correct"}


res = run([A("write", "f.py", "x")], apply_write=apply_pass)
check("write PASS -> ok, applied", res[0].ok)
check("write PASS -> council review echoed into content for the leader",
      "council review" in res[0].content and "looks correct" in res[0].content)
check("write PASS -> note stays compact metadata (no review body)",
      "verdict=PASS" in res[0].note and "looks correct" not in res[0].note)


# ---------------------------------------------------------------------------
# 8. WRITE BLOCK: leader MUST see the reasons (the load-bearing bug the council caught)
# ---------------------------------------------------------------------------
def apply_block(leader, path, content, workdir, *, session_id="", transcript_path="", review=None):
    return {"applied": False, "verdict": "BLOCK", "target": path,
            "review": "VERDICT: BLOCK\nunsafe because it deletes data"}


res = run([A("write", "f.py", "rm stuff")], apply_write=apply_block)
check("write BLOCK -> ok=False, not applied", not res[0].ok)
check("write BLOCK -> the council's REASONS reach the leader via content",
      "unsafe because it deletes data" in res[0].content and "verdict=BLOCK" in res[0].note)


# ---------------------------------------------------------------------------
# 9. WRITE DENIED (jail/cap): reason surfaced in note
# ---------------------------------------------------------------------------
def apply_denied(leader, path, content, workdir, *, session_id="", transcript_path="", review=None):
    return {"applied": False, "verdict": "DENIED", "path": path, "reason": "traversal denied"}


res = run([A("write", "../x", "x")], apply_write=apply_denied)
check("write DENIED -> reason in note",
      not res[0].ok and "traversal denied" in res[0].note)


# ---------------------------------------------------------------------------
# 10. WRITE count cap: more than LEADER_MAX_WRITES_PER_TURN -> extra denied, wall not called
# ---------------------------------------------------------------------------
wcalls = []


def apply_count(leader, path, content, workdir, *, session_id="", transcript_path="", review=None):
    wcalls.append(path)
    return {"applied": True, "verdict": "PASS", "review": "ok"}


over = cl.LEADER_MAX_WRITES_PER_TURN + 2
res = run([A("write", f"f{i}.py", "x") for i in range(over)], apply_write=apply_count)
check("write cap: only LEADER_MAX_WRITES_PER_TURN reach the wall",
      len(wcalls) == cl.LEADER_MAX_WRITES_PER_TURN)
check("write cap: the extra writes are DENIED (never applied)",
      all(not r.ok and "write cap" in r.note
          for r in res[cl.LEADER_MAX_WRITES_PER_TURN:]))


print(f"\n=== run_leader_actions: {sum(P)}/{len(P)} PASS ===")
sys.exit(0 if all(P) else 1)
