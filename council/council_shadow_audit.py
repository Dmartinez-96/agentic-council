#!/usr/bin/env python3
"""Shadow-critic vetting: do kimi/glm/grok EARN a voting seat, or are they noise?

The layer-2 shadow critics (consult_council.py: SHADOW_MEMBERS) run alongside the
real council but do NOT vote. Their verdicts and full reasoning are logged in each
fire's `shadow` field. This tool is how you decide whether a shadow member has
proven itself enough to promote to a voting member -- and it is built around the
ONE way that question can be answered honestly.

THE TRAP THIS TOOL EXISTS TO AVOID (read before trusting any number here).
The tempting metric is a shadow member's SOLO-CATCH RATE: how often it flags
something the voting council missed. That metric is a VOID CHECK. State the
falsifier first: if the shadow member were pure noise -- hallucinating unique false
positives on every fire -- what would its solo-catch rate be? HIGH. A noisy model
and a genuinely decorrelated expert produce the IDENTICAL signal here: "flags
nobody else raised". The count cannot tell them apart, so on its own it proves
nothing about value.

A solo catch becomes EVIDENCE OF VALUE only once someone rules on whether it was
RIGHT. That ruling must come from OUTSIDE the LLM correlation structure -- from you
(the user), or from reality (an executable test, a later-confirmed bug). It must
NOT come from the council: `council_outcome.py adjudicate` asks the council to
rule, and the council shares the shadow members' blind spots, so using it to grade
a shadow catch is circular. That is why `adjudicate` here records YOUR ruling and
never fires the council.

The pipeline:
  1. scan   -- unique catches per member, and how many you have adjudicated
               (coverage first, because a cherry-picked subset is the easy cheat).
  2. list   -- READ the actual arguments from kimi/glm/grok, in context.
  3. adjudicate <catch> correct|noise|unsure --note "<why>" -- YOUR ground-truth
               ruling on a catch.
  4. stats  -- per-member precision on the catches you adjudicated. THIS is the
               "panned out vs noise" number, only as good as your coverage.

WHAT "UNIQUE" MEANS HERE, and its limit. A shadow catch is counted UNIQUE when the
voting council flagged NOTHING on that fire (no voting member cast WARN/BLOCK) yet
the shadow member raised a concern. That is the cleanest solo catch. A shadow
concern on a fire where the voting council ALSO flagged is classed OVERLAP and held
OUT of the unique-precision number. That exclusion is a deliberate METRIC
DEFINITION, not a claim that overlap catches are worthless: `unique` is drawn at
the FIRE level -- the council raised nothing at all -- which is a conservative,
checkable line that needs no guess about whether two differently-worded concerns
are "the same issue". A finer ISSUE-level split is possible future work; the
fire-level line is chosen here because it cannot be gamed by wording. Overlap
catches stay listable and adjudicable; they simply do not count toward the clean
unique-catch precision.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from council_outcome import reasons_of, depth_of, now_iso  # noqa: E402
from consult_council import SHADOW_MEMBERS  # noqa: E402

COUNCIL_ROOT = Path(__file__).resolve().parent
SHADOW_OUTCOMES = COUNCIL_ROOT / "shadow_outcomes.jsonl"

RULINGS = {
    "correct": "a REAL catch the voting council missed (say why, in --note)",
    "noise":   "a false positive / hallucinated concern (say why, in --note)",
    "unsure":  "looked at, could not decide (excluded from precision, still counts)",
}
MIN_NOTE = 12  # a ruling with no reasoning is worth as little as a bare label


def catch_id(log_name: str, member: str, idx: int) -> str:
    """Stable identity for ONE reason from one shadow member on one fire."""
    return f"{log_name}::shadow::{member}::{idx}"


def voting_flagged(log_entry: dict) -> bool:
    """True if any VOTING member (members[]) cast WARN or BLOCK on this fire.

    Deliberately keys on the voting members' verdicts, not the fire's
    final_verdict: final_verdict can be WARN from a lost/errored vote (fail-loud)
    with no member having actually objected, and that is not the council 'seeing'
    the shadow's concern.
    """
    return any(m.get("verdict") in ("WARN", "BLOCK")
               for m in log_entry.get("members", []))


def shadow_catches(since: str = "") -> dict[str, dict]:
    """Every WARN/BLOCK reason from every shadow member, keyed by catch_id.

    `unique` is True when the voting council flagged nothing on that fire (see the
    module docstring for why that distinction is load-bearing, and its limit).
    """
    out: dict[str, dict] = {}
    for f in sorted(COUNCIL_ROOT.glob("logs/*/*.json")):
        if since and f.parent.name < since:
            continue
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        shadow = d.get("shadow") or []
        if not shadow:
            continue
        unique = not voting_flagged(d)
        for s in shadow:
            if s.get("verdict") not in ("WARN", "BLOCK"):
                continue
            member = s.get("role", "?")
            reasons = reasons_of(s) or ["(no REASONS: parsed; read --full)"]
            for i, reason in enumerate(reasons):
                cid = catch_id(f.name, member, i)
                out[cid] = {
                    "catch_id": cid,
                    "log": str(f),
                    "log_date": f.parent.name,
                    "timestamp": d.get("timestamp", ""),
                    "member": member,
                    "verdict": s.get("verdict"),
                    "model_used": s.get("model_used", ""),
                    "reason": reason,
                    "full_text": s.get("text", ""),
                    "target_path": d.get("target_path"),
                    "pitch": d.get("pitch", ""),
                    "unique": unique,
                    "depth": depth_of(d),
                }
    return out


def read_shadow_outcomes() -> list[dict]:
    if not SHADOW_OUTCOMES.exists():
        return []
    out = []
    for line in SHADOW_OUTCOMES.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def latest_rulings() -> dict[str, dict]:
    """Newest ruling per catch_id. Appending is history; only the last counts."""
    latest: dict[str, dict] = {}
    for e in read_shadow_outcomes():
        cid = e.get("catch_id")
        if cid:
            latest[cid] = e
    return latest


def _members_present(catches: dict[str, dict]) -> list[str]:
    return sorted({c["member"] for c in catches.values()})


def cmd_scan(args) -> int:
    catches = shadow_catches(args.since)
    rulings = latest_rulings()
    if not catches:
        print("No shadow WARN/BLOCK catches logged yet. Enable the shadow tier "
              "(`touch SHADOW` + OPENROUTER_API_KEY) and let it run.")
        return 0
    print("=" * 72)
    print("SHADOW-CRITIC VETTING -- coverage first. A solo-catch COUNT is NOT")
    print("evidence of value until you have ruled on whether the catch was RIGHT.")
    print("A noisy model and a real expert both 'flag what others missed'.")
    print("=" * 72)
    for m in _members_present(catches):
        mine = [c for c in catches.values() if c["member"] == m]
        uniq = [c for c in mine if c["unique"]]
        adj = [c for c in uniq
               if (rulings.get(c["catch_id"]) or {}).get("ruling")]
        correct = sum(1 for c in adj
                      if rulings[c["catch_id"]]["ruling"] == "correct")
        noise = sum(1 for c in adj
                    if rulings[c["catch_id"]]["ruling"] == "noise")
        cov = f"{len(adj)}/{len(uniq)}" if uniq else "0/0"
        print(f"\n{m}:")
        print(f"  flagged reasons total  : {len(mine)}")
        print(f"  UNIQUE catches         : {len(uniq)}  (voting council saw nothing)")
        print(f"  adjudicated (coverage) : {cov}")
        print(f"  ruled correct / noise  : {correct} / {noise}")
    print("\nNext: `list` to read the arguments, then `adjudicate`.")
    return 0


def cmd_list(args) -> int:
    catches = shadow_catches(args.since)
    rulings = latest_rulings()
    rows = list(catches.values())
    if args.member:
        rows = [c for c in rows if c["member"] == args.member]
    if not args.include_overlap:
        rows = [c for c in rows if c["unique"]]
    if not args.include_adjudicated:
        rows = [c for c in rows
                if not (rulings.get(c["catch_id"]) or {}).get("ruling")]
    rows.sort(key=lambda c: c["timestamp"], reverse=True)
    total = len(rows)
    rows = rows[: args.n]
    if not rows:
        print("Nothing to show with these filters.")
        return 0
    seen_full: set[tuple[str, str]] = set()
    for c in rows:
        r = rulings.get(c["catch_id"]) or {}
        tag = "UNIQUE" if c["unique"] else "overlap"
        print("-" * 72)
        print(c["catch_id"])
        print(f"  {c['member']} {c['verdict']} via {c['model_used']}  "
              f"[{tag}]  {c['timestamp']}")
        print(f"  file: {c['target_path']}")
        if r.get("ruling"):
            print(f"  ALREADY RULED: {r['ruling']} -- {r.get('note', '')}")
        print(f"  concern: {c['reason']}")
        if args.full:
            key = (c["log"], c["member"])
            if key in seen_full:
                print("  (full response already printed above for this "
                      "member on this fire)")
            else:
                seen_full.add(key)
                print("  --- full shadow response ---")
                print(textwrap.indent(c["full_text"].strip() or "(empty)",
                                      "  | "))
        if args.context:
            snip = (c["pitch"] or "")[: args.context]
            print("  --- pitch context (truncated) ---")
            print(textwrap.indent(snip, "  > "))
    print("-" * 72)
    shown = f"{len(rows)} of {total}" if total > len(rows) else str(len(rows))
    print(f"{shown} shown. Record a ruling with:")
    print('  council_shadow_audit.py adjudicate <catch_id> correct|noise|unsure '
          '--note "<why>"')
    return 0


def cmd_adjudicate(args) -> int:
    catches = shadow_catches()
    c = catches.get(args.catch_id)
    if c is None:
        print(f"Unknown catch_id: {args.catch_id}\nRun `list` to see valid ids.",
              file=sys.stderr)
        return 2
    note = (args.note or "").strip()
    if len(note) < MIN_NOTE:
        print(f"--note must be at least {MIN_NOTE} chars: say WHY it is "
              f"{args.ruling}. This note IS the ground-truth evidence; a bare "
              f"ruling with no reasoning is exactly the self-serving label this "
              f"tool exists to prevent.", file=sys.stderr)
        return 2
    if not c["unique"] and args.ruling in ("correct", "noise"):
        print("NOTE: this is an OVERLAP catch (the voting council also flagged on "
              "that fire). It is recorded, but held OUT of the unique-catch "
              "precision in `stats`.", file=sys.stderr)
    rec = {
        "catch_id": args.catch_id,
        "ruling": args.ruling,
        "note": note,
        "member": c["member"],
        "verdict": c["verdict"],
        "unique": c["unique"],
        "log_date": c["log_date"],
        "adjudicated_at": now_iso(),
        "adjudicator": "user",
    }
    with SHADOW_OUTCOMES.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(f"recorded: {args.catch_id} -> {args.ruling}")
    return 0


def cmd_stats(args) -> int:
    catches = shadow_catches(args.since)
    rulings = latest_rulings()
    if not catches:
        print("No shadow catches logged yet.")
        return 0
    print("=" * 72)
    print("SHADOW-CRITIC PRECISION on the UNIQUE catches YOU adjudicated.")
    print("precision = correct / (correct + noise); 'unsure' is excluded from")
    print("precision but counts toward coverage. High precision on a THIN sample")
    print("is not a promotion case -- read the n. Solo-catch COUNT is not value;")
    print("only these adjudicated rulings are (see the module docstring).")
    print("=" * 72)
    for m in _members_present(catches):
        uniq = [c for c in catches.values()
                if c["member"] == m and c["unique"]]
        adj = [(c, rulings[c["catch_id"]]) for c in uniq
               if (rulings.get(c["catch_id"]) or {}).get("ruling")]
        correct = sum(1 for _, r in adj if r["ruling"] == "correct")
        noise = sum(1 for _, r in adj if r["ruling"] == "noise")
        unsure = sum(1 for _, r in adj if r["ruling"] == "unsure")
        decisive = correct + noise
        prec = f"{correct / decisive:.0%}" if decisive else "n/a"
        print(f"\n{m}:")
        print(f"  unique catches          : {len(uniq)}")
        print(f"  adjudicated (coverage)  : {len(adj)}/{len(uniq)}")
        print(f"  correct / noise / unsure: {correct} / {noise} / {unsure}")
        print(f"  unique-catch precision  : {prec}  (n={decisive} decisive)")
    print("\nA member 'pans out' when precision is high AND coverage is not thin.")
    print("Thin coverage = you have not looked at enough to say. Adjudicate more.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="overview + coverage per shadow member")
    p_scan.add_argument("--since", default="", help="YYYY-MM-DD log-dir floor")

    p_list = sub.add_parser("list", help="read the shadow arguments, in context")
    p_list.add_argument("--member", choices=tuple(SHADOW_MEMBERS))
    p_list.add_argument("--include-overlap", action="store_true",
                        help="also show catches where the voting council flagged")
    p_list.add_argument("--include-adjudicated", action="store_true",
                        help="also show catches you already ruled on")
    p_list.add_argument("--full", action="store_true",
                        help="print each shadow member's FULL response text")
    p_list.add_argument("--context", type=int, default=0, metavar="N",
                        help="also print the first N chars of the pitch")
    p_list.add_argument("-n", type=int, default=20, help="max rows (default 20)")
    p_list.add_argument("--since", default="")

    p_adj = sub.add_parser("adjudicate",
                           help="record YOUR ground-truth ruling on a catch")
    p_adj.add_argument("catch_id")
    p_adj.add_argument("ruling", choices=tuple(RULINGS))
    p_adj.add_argument("--note", required=True,
                       help="WHY it is correct/noise -- the ground-truth evidence")

    p_stats = sub.add_parser("stats", help="per-member unique-catch precision")
    p_stats.add_argument("--since", default="")

    args = p.parse_args()
    return {
        "scan": cmd_scan,
        "list": cmd_list,
        "adjudicate": cmd_adjudicate,
        "stats": cmd_stats,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
