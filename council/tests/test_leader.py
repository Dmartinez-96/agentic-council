"""Durable regression for the INTERCHANGEABLE-LEADER registry seam in
consult_council.py: the shared transport/model validator (_validate_transport_model,
now used by BOTH the members loop and the leader), the leader validator
(_validate_leader), and load_registry's leader return + whole-roster rejection.

Runs against the ACTUAL functions with a TEMP ROSTER_PATH. The ONLY read of the
live roster.json is the unavoidable one at `import consult_council` (module-level
load_registry()); every load_registry() call the TEST itself makes is swapped onto
a temp path first and restored in a finally, and the test never WRITES the live
roster.json -- so it is safe to run while the council is active (each council fire
is a separate process anyway). Ships in council/tests/ (synced from the development tree,
sync_to_package.py). Re-run: python3 council/tests/test_leader.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consult_council as cc

P = []


def check(label, cond):
    P.append(bool(cond))
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")


# ---------------------------------------------------------------------------
# 1. _validate_transport_model directly: the extracted transport/model rules. These
#    call the helper in isolation; section 3b additionally drives a bad MEMBER record
#    THROUGH _validate_roster to prove the members loop still routes via this helper.
#    errors is the accumulator the helper appends to.
# ---------------------------------------------------------------------------
def tm(rec, name):
    errs = []
    out = cc._validate_transport_model(rec, name, "where", errs)
    return out, errs


out, errs = tm({"transport": "openrouter", "model": "x/y"}, "gemini")
check("openrouter member: (transport, model, fallback) with no error",
      out == ("openrouter", "x/y", None) and not errs)

out, errs = tm({"transport": "openrouter", "model": "x/y",
                "fallback_model": "x/z"}, "gemini")
check("openrouter member keeps its fallback slug",
      out == ("openrouter", "x/y", "x/z") and not errs)

out, errs = tm({"transport": "codex_subprocess"}, "codex")
check("codex_subprocess reads its pinned module constant, name==canonical",
      out == ("codex_subprocess", cc.CODEX_MODEL, None) and not errs)

out, errs = tm({"transport": "codex_subprocess"}, "notcodex")
check("canonical-name coupling: codex_subprocess under a wrong name is rejected",
      out is None and any("usable only by" in e for e in errs))

out, errs = tm({"transport": "openrouter"}, "gemini")
check("openrouter without a model slug is rejected",
      out is None and any("requires a model slug" in e for e in errs))

out, errs = tm({"transport": "deepseek_https", "model": "some/other"}, "deepseek")
check("direct-vendor transport rejects a divergent model (must use openrouter)",
      out is None and any("reads its model from the module constant" in e for e in errs))

out, errs = tm({"transport": "gemini_rest", "fallback_model": "x"}, "gemini")
check("fallback on a non-codex direct transport is rejected (dead field)",
      out is None and any("fallback_model is not supported" in e for e in errs))

out, errs = tm({"transport": "bogus", "model": "x"}, "x")
check("unknown transport rejected", out is None and any("transport must be one of" in e for e in errs))


# ---------------------------------------------------------------------------
# 2. _validate_leader: the NEW surface.
# ---------------------------------------------------------------------------
def leader(raw):
    errs, warns = [], []
    m = cc._validate_leader(raw, errs, warns)
    return m, errs, warns


m, errs, warns = leader({"members": []})   # no leader key at all
check("no leader key -> None, no error (harness leads by default)",
      m is None and not errs)

m, errs, warns = leader({"leader": {"name": "grok", "transport": "openrouter",
                                    "model": "x-ai/grok-4.5"}})
check("valid openrouter leader -> Member(tier=LEADER)",
      m is not None and m.tier == cc.LEADER and m.name == "grok"
      and m.transport == "openrouter" and m.model == "x-ai/grok-4.5")
check("leader holds LEADER_CAPS including 'mutate'",
      m is not None and tuple(m.capabilities) == cc.LEADER_CAPS
      and cc.MUTATE in m.capabilities)

m, errs, warns = leader({"leader": {"name": "codex", "transport": "codex_subprocess"}})
check("codex_subprocess leader honored under its canonical name",
      m is not None and m.tier == cc.LEADER and m.model == cc.CODEX_MODEL)

m, errs, warns = leader({"leader": {"name": "codex", "transport": "codex_subprocess",
                                    "capabilities": ["file_retrieval"]}})
check("leader 'capabilities' field is IGNORED (warning) -> still full LEADER_CAPS",
      m is not None and tuple(m.capabilities) == cc.LEADER_CAPS
      and any("capabilities ignored" in w for w in warns))

m, errs, warns = leader({"leader": {"name": "grok", "transport": "openrouter",
                                    "model": "x/y", "tier": "voting"}})
check("leader 'tier' field is IGNORED (warning) -> tier stays LEADER",
      m is not None and m.tier == cc.LEADER
      and any("tier" in w and "ignored" in w for w in warns))

m, errs, warns = leader({"leader": "not-an-object"})
check("leader not an object -> error", m is None and any("must be an object" in e for e in errs))

m, errs, warns = leader({"leader": {"transport": "openrouter", "model": "x/y"}})
check("leader missing name -> error", m is None and any("missing/empty name" in e for e in errs))

m, errs, warns = leader({"leader": {"name": "x", "transport": "codex_subprocess"}})
check("leader on codex_subprocess under a NON-canonical name -> error",
      m is None and any("usable only by" in e for e in errs))


# ---------------------------------------------------------------------------
# 3. load_registry end-to-end via a TEMP ROSTER_PATH (never the live roster.json).
# ---------------------------------------------------------------------------
_saved = cc.ROSTER_PATH
tmp = Path(tempfile.mkdtemp(prefix="leadertest_"))
try:
    cc.ROSTER_PATH = tmp / "roster.json"

    # 3a. valid roster (default-shaped members) + a configured leader
    good = {"members": [
        {"name": "codex", "tier": "voting", "transport": "codex_subprocess"},
        {"name": "gemini", "tier": "voting", "transport": "openrouter", "model": "google/gemini-3.5-flash"},
        {"name": "deepseek", "tier": "voting", "transport": "openrouter", "model": "deepseek/deepseek-v4-pro"},
    ], "leader": {"name": "grok", "transport": "openrouter", "model": "x-ai/grok-4.5"}}
    cc.ROSTER_PATH.write_text(json.dumps(good))
    members, ldr, source, errors, warnings, _cap = cc.load_registry()
    check("load_registry: valid roster accepted, source is the filename",
          source == cc.ROSTER_PATH.name and not errors and len(members) == 3)
    check("load_registry: returns the configured leader Member",
          ldr is not None and ldr.name == "grok" and ldr.tier == cc.LEADER)
    check("INVARIANT: no returned MEMBER holds 'mutate' (mutation is leader-only)",
          all(cc.MUTATE not in m.capabilities for m in members)
          and all(tuple(m.capabilities) == cc._DEFAULT_CAPS for m in members))

    # 3b. Prove the members loop actually ROUTES through the shared
    # _validate_transport_model with a CALL-GRAPH probe: wrap the module-global helper
    # in a spy, run load_registry, and assert the spy recorded the member. FALSIFIER
    # (rule 6): if the members loop carried leftover inline validation and did NOT call
    # the helper, `calls` would be empty and this check FAILS. A bad member (direct-vendor
    # transport under a non-canonical name) also proves the routed helper still rejects.
    # The roster has no leader, so the only caller of the spy is the members loop.
    calls = []
    _orig_tm = cc._validate_transport_model

    def _spy(rec, name, where, errors):
        calls.append(name)
        return _orig_tm(rec, name, where, errors)

    cc._validate_transport_model = _spy
    try:
        badmem = {"members": [{"name": "notcodex", "tier": "voting",
                               "transport": "codex_subprocess"}]}
        cc.ROSTER_PATH.write_text(json.dumps(badmem))
        members, ldr, source, errors, warnings, _cap = cc.load_registry()
    finally:
        cc._validate_transport_model = _orig_tm
    check("load_registry: members loop ROUTES a member through the shared helper "
          "(spy recorded 'notcodex'), and the routed helper rejects the bad member",
          "notcodex" in calls and members is cc.DEFAULT_REGISTRY and ldr is None
          and any("usable only by" in e for e in errors))

    # 3c. a roster whose LEADER is malformed rejects the WHOLE file -> default panel, no leader
    bad = {"members": good["members"],
           "leader": {"name": "x", "transport": "codex_subprocess"}}  # canonical mismatch
    cc.ROSTER_PATH.write_text(json.dumps(bad))
    members, ldr, source, errors, warnings, _cap = cc.load_registry()
    check("load_registry: a bad leader rejects the whole roster -> DEFAULT_REGISTRY",
          members is cc.DEFAULT_REGISTRY and ldr is None and errors
          and "rejected" in source)

    # 3d. no roster.json -> default bench AND the shipped DEFAULT_LEADER.
    # CHANGED 2026-08-06 and the change is the point: this asserted `ldr is None` until
    # DEFAULT_LEADER shipped, so a fresh install had no leader and the GUI's Leader tab was
    # dead. An ABSENT roster now yields claude. The two REJECTION cases above still assert
    # None, deliberately -- a broken roster must not silently GAIN a leader it never had,
    # which is what keeps ROSTER_ERRORS meaningful.
    cc.ROSTER_PATH.unlink()
    members, ldr, source, errors, warnings, _cap = cc.load_registry()
    check("load_registry: absent roster -> DEFAULT_REGISTRY + DEFAULT_LEADER, "
          "source 'default'",
          members is cc.DEFAULT_REGISTRY and ldr is cc.DEFAULT_LEADER
          and source == "default" and not errors)
    check("the default leader is claude on claude_subprocess",
          ldr is not None and ldr.name == "claude"
          and ldr.transport == "claude_subprocess")
finally:
    cc.ROSTER_PATH = _saved
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n=== leader registry seam: {sum(P)}/{len(P)} PASS ===")
sys.exit(0 if all(P) else 1)
