"""Deterministic regression for council_leader.parse_leader_actions (driver piece b1).

Contract this LOCKS (the parser must satisfy it; the fixes the design review demanded):
  - ORDER: actions come out in the leader's DOCUMENT ORDER, so a WRITE then an EXEC of
    what it wrote run in that order (not all-reads-then-all-writes).
  - ENVELOPE SCOPE: only lines inside the nonce envelope are parsed as actions, so prose,
    examples, or quoted grammar elsewhere are not; and an action-looking line INSIDE a
    WRITE body is DATA, not an action.
  - NO SILENT LOSS: a malformed WRITE, an unterminated envelope, an unterminated WRITE
    body, and over-cap overflow are all reported EXPLICITLY via .problems / .overflow,
    never dropped silently.
  - COLON-SAFE args: an EXEC command may contain colons (arg is everything after "VERB:").

No live calls; fully re-runnable:  python3 council/tests/test_leader_actions.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import council_leader as cl

P = []


def check(label, cond):
    P.append(bool(cond))
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")


N = "turnN0nce"
AB, AE = cl._actions_sentinels(N)      # BEGIN/END ACTIONS <nonce>
CB, CE = cl._write_sentinels(N)        # BEGIN/END CONTENT <nonce> (reused for WRITE bodies)


def envelope(*lines):
    return AB + "\n" + "\n".join(lines) + "\n" + AE


def kinds(parse):
    return [(a.kind, a.arg) for a in parse.actions]


# ---------------------------------------------------------------------------
# 1. single actions of each kind; argument captured; clean parse has no problems
# ---------------------------------------------------------------------------
p = cl.parse_leader_actions(envelope("READ: src/x.py"), N)
check("READ parsed, path captured", kinds(p) == [("read", "src/x.py")])
check("clean parse: no problems, no overflow", p.problems == () and not p.overflow)
p = cl.parse_leader_actions(envelope("FETCH: https://docs.python.org/3/"), N)
check("FETCH parsed, url (with '://') captured",
      kinds(p) == [("fetch", "https://docs.python.org/3/")])
p = cl.parse_leader_actions(envelope("EXEC: python3 -c 'print(1+1)'"), N)
check("EXEC parsed, command with spaces captured",
      kinds(p) == [("exec", "python3 -c 'print(1+1)'")])
# COLON-SAFE: a command containing colons must survive intact
p = cl.parse_leader_actions(envelope("EXEC: sed -e 's:a:b:' file.txt"), N)
check("EXEC command with embedded colons captured intact (not split on ':')",
      kinds(p) == [("exec", "sed -e 's:a:b:' file.txt")])


# ---------------------------------------------------------------------------
# 2. document ORDER preserved across kinds (WRITE then EXEC of it)
# ---------------------------------------------------------------------------
env = envelope("WRITE: build/test.py", CB, "print('hi')", CE, "EXEC: python3 build/test.py")
p = cl.parse_leader_actions(env, N)
check("WRITE-then-EXEC preserved IN ORDER (not reordered)",
      [x.kind for x in p.actions] == ["write", "exec"])
check("WRITE body + path captured exactly",
      p.actions[0].body == "print('hi')" and p.actions[0].arg == "build/test.py")
check("EXEC after the write captured", p.actions[1].arg == "python3 build/test.py")

env = envelope("READ: a.py", "WRITE: b.py", CB, "B", CE,
               "FETCH: https://example.com", "EXEC: ls")
p = cl.parse_leader_actions(env, N)
check("four-action mix preserved in order",
      [x.kind for x in p.actions] == ["read", "write", "fetch", "exec"])


# ---------------------------------------------------------------------------
# 3. actions OUTSIDE the envelope are IGNORED (prose/examples)
# ---------------------------------------------------------------------------
text = ("Plan: for example one might write READ: /etc/passwd but I will not.\n"
        "EXEC: rm -rf /   <- prose, not inside the envelope\n\n"
        + envelope("READ: safe/file.py") +
        "\nAfterthought: FETCH: https://evil.example ignored too.")
p = cl.parse_leader_actions(text, N)
check("only the in-envelope action parsed; surrounding prose ignored",
      kinds(p) == [("read", "safe/file.py")] and not p.problems)


# ---------------------------------------------------------------------------
# 4. an action-looking line INSIDE a WRITE body is DATA, not an action
# ---------------------------------------------------------------------------
env = envelope("WRITE: script.sh", CB,
               "#!/bin/sh", "EXEC: this is file content", "READ: also content", CE)
p = cl.parse_leader_actions(env, N)
check("body lines that look like actions are NOT parsed as actions",
      [x.kind for x in p.actions] == ["write"])
check("the body preserves those lines verbatim",
      p.actions[0].body == "#!/bin/sh\nEXEC: this is file content\nREAD: also content")


# ---------------------------------------------------------------------------
# 5. NO SILENT LOSS: malformed WRITE, unterminated envelope, unterminated body
# ---------------------------------------------------------------------------
# 5a. WRITE with no CONTENT block -> dropped, but with an explicit problem, siblings parse
env = envelope("WRITE: nobody.py", "READ: real.py")
p = cl.parse_leader_actions(env, N)
check("malformed WRITE (no CONTENT) dropped, sibling READ still parsed",
      kinds(p) == [("read", "real.py")])
check("malformed WRITE reported in .problems (not silent)",
      any("nobody.py" in pr and "CONTENT" in pr for pr in p.problems))

# 5b. ACTIONS envelope opened but never closed -> explicit problem, not "no actions"
opened = AB + "\nREAD: x.py\n(no END ACTIONS line here)"
p = cl.parse_leader_actions(opened, N)
check("unterminated ACTIONS envelope: no actions BUT an explicit problem",
      p.actions == () and any("never closed" in pr for pr in p.problems))

# 5c. unterminated WRITE CONTENT block -> explicit problem naming collateral loss
env_lines = [AB, "WRITE: x.py", CB, "some body", "no end content sentinel",
             "EXEC: trailing action", AE]
p = cl.parse_leader_actions("\n".join(env_lines), N)
check("unterminated WRITE body: reported, and says following actions not parsed",
      any("unterminated CONTENT" in pr and "following actions" in pr for pr in p.problems))
check("unterminated WRITE body: the body 'EXEC:' line is NOT executed as an action",
      all(a.kind != "exec" for a in p.actions))


# ---------------------------------------------------------------------------
# 6. wrong / empty nonce; no-envelope final answer
# ---------------------------------------------------------------------------
wb, we = cl._actions_sentinels("different")
p = cl.parse_leader_actions(wb + "\nREAD: x\n" + we, N)
check("envelope with a DIFFERENT nonce -> no actions (nonce scopes the envelope)",
      p.actions == () and not p.problems)
try:
    cl.parse_leader_actions("whatever", "")
    check("empty nonce raises", False)
except ValueError:
    check("empty nonce raises", True)
p = cl.parse_leader_actions("Just my final answer, no actions.", N)
check("no envelope -> clean no-actions final answer (no problem)",
      p.actions == () and not p.problems and not p.overflow)


# ---------------------------------------------------------------------------
# 7. trailing-newline body round-trips (blank line before END CONTENT)
# ---------------------------------------------------------------------------
for body in ("a\nb", "a\nb\n", "", "x\n\n"):
    env = envelope("WRITE: f.txt", CB, *body.split("\n"), CE)
    p = cl.parse_leader_actions(env, N)
    check(f"WRITE body {body!r} round-trips exactly",
          len(p.actions) == 1 and p.actions[0].body == body)


# ---------------------------------------------------------------------------
# 8. overflow: capped at LEADER_MAX_ACTIONS_PER_TURN, and it is REPORTED
# ---------------------------------------------------------------------------
many = envelope(*[f"READ: f{i}.py" for i in range(cl.LEADER_MAX_ACTIONS_PER_TURN + 8)])
p = cl.parse_leader_actions(many, N)
check("action count capped at LEADER_MAX_ACTIONS_PER_TURN",
      len(p.actions) == cl.LEADER_MAX_ACTIONS_PER_TURN)
check("overflow reported explicitly (flag + problem), not silent truncation",
      p.overflow and any("cap" in pr for pr in p.problems))
# exactly the cap -> no overflow
exact = envelope(*[f"READ: f{i}.py" for i in range(cl.LEADER_MAX_ACTIONS_PER_TURN)])
p = cl.parse_leader_actions(exact, N)
check("exactly the cap -> all parsed, no overflow",
      len(p.actions) == cl.LEADER_MAX_ACTIONS_PER_TURN and not p.overflow)


print(f"\n=== parse_leader_actions: {sum(P)}/{len(P)} PASS ===")
sys.exit(0 if all(P) else 1)
