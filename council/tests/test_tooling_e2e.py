"""End-to-end test of the phases 2/3 verification tooling wired through main().

Exercises a REAL main() fire with the two dispatched transports stubbed (run_openrouter
+ run_codex, so no network / no codex subprocess) and fetch_web_url / run_exec_sandbox
stubbed to canned results. Scope: this test proves the WIRING only (their live
network/sandbox behaviour is out of scope here). The WIRING:
- voting members' round-1 REQUEST_FILE/REQUEST_URL/REQUEST_EXEC -> round-2 per-requester
  delivery (file read is REAL/jailed; web+exec are the stubbed canned results);
- the INSPECTOR pass-1 -> pass-2 request/deliver leg (the user's "even inspectors");
- REDACTION: the shared round-1 block and the log carry NO raw URL/command/path, only
  <redacted> + the private per-requester delivery;
- LOG provenance: web / exec / shadow_tooling fields populated, zero raw arg persisted.

Gitignored (survives compaction). Re-run: python3 council/tests/test_tooling_e2e.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consult_council as cc

wd = Path(tempfile.mkdtemp(prefix="e2e_"))
(wd / "src").mkdir()
(wd / "src" / "app.py").write_text("print('APPCONTENT')\n")
logroot = Path(tempfile.mkdtemp(prefix="e2elog_"))
cap: dict = {"deliver": {}}


def _emit(role: str) -> str:
    # a member requests one of each capability in its request phase
    return ("VERDICT: WARN\n"
            "REQUEST_FILE: src/app.py\n"
            "REQUEST_URL: https://arxiv.org/abs/SENSITIVE123\n"
            f"REQUEST_EXEC: echo SECRETCMD-{role}")


async def stub_or(role, models, pitch, sp, ev="", ud="", r1="", ab="", srr="", ccb=""):
    # r1 non-empty == a DELIVERY phase (voting round 2 / inspector pass 2): capture it.
    if r1:
        cap["deliver"][role] = r1
    text = "VERDICT: PASS" if r1 else _emit(role)
    return {"role": role, "text": text, "stderr": "", "returncode": 0,
            "verdict": cc.parse_verdict(text), "duration_s": 0.0, "model_used": models[0]}


async def stub_codex(pitch, sp, cwd, ev="", ud="", r1="", ab="", srr="", ccb=""):
    if r1:
        cap["deliver"]["codex"] = r1
    text = "VERDICT: PASS" if r1 else _emit("codex")
    return {"role": "codex", "text": text, "stderr": "", "returncode": 0,
            "verdict": cc.parse_verdict(text), "duration_s": 0.0}


cc.run_openrouter = stub_or
cc.run_codex = stub_codex
cc.fetch_web_url = lambda url, prompt_text="": ("WEBBODY", "status 200, 7 bytes")
cc.run_exec_sandbox = lambda cmd, workdir: ("EXECOUT", "exit 0, 7 bytes read")
cc.LOGS_ROOT = logroot
_shadow = wd / "SHADOW"
_shadow.write_text("")
cc.SHADOW_PATH = _shadow                     # enable layer-2 inspectors
os.environ["OPENROUTER_API_KEY"] = "x"       # so openrouter members are not dropped

pf = wd / "pitch.txt"
pf.write_text("Review this change. (e2e)\n")
sys.argv = ["consult_council.py", "--layer", "reasoning",
            "--workdir", str(wd), "--prompt-file", str(pf)]
rc = asyncio.run(cc.main())
assert rc == 0, rc

# 1. a VOTING member's round-2 prompt carries all three deliveries
g = cap["deliver"]["gemini"]
assert "APPCONTENT" in g and "WEBBODY" in g and "EXECOUT" in g, "voting delivery missing"
print("1. voting round-1 -> round-2 delivery (file+web+exec): PASS")

# 2. the SHARED round-1 block (peers) is REDACTED: no raw arg, and <redacted> present
shared = g.split("## Requested repo files")[0]     # everything before the private block
for s in ("SENSITIVE123", "SECRETCMD", "src/app.py", "REQUEST_URL: https"):
    assert s not in shared, f"raw {s!r} leaked into the peer-shared round-1 block"
assert "<redacted>" in shared
print("2. shared round-1 block redacted (no raw url/cmd/path to peers): PASS")

# 3. an INSPECTOR received a pass-2 delivery block (the inspector leg works)
insp = [r for r in cap["deliver"] if r in ("kimi", "glm", "grok")]
assert insp, "no inspector received a pass-2 delivery (inspector leg broken)"
ib = cap["deliver"][insp[0]]
assert "WEBBODY" in ib and "EXECOUT" in ib and "APPCONTENT" in ib, "inspector delivery missing"
print(f"3. inspector pass-1 -> pass-2 delivery ({sorted(insp)}): PASS")

# 4. the LOG: web/exec/shadow_tooling populated; ZERO raw url/command persisted anywhere
logfile = sorted(logroot.glob("*/*.json"))[-1]
raw = logfile.read_text()
entry = json.loads(raw)
assert entry["web"]["any_granted"] and entry["exec"]["any_granted"], "voting web/exec log empty"
assert entry["shadow_tooling"]["web"]["any_granted"], "inspector tooling log empty"
# the URL path/query (SENSITIVE123) and command (SECRETCMD) never persist -- web is logged
# host+sha, exec sha-only; and the raw REQUEST_* LINES are stripped from the logged round-1
# text (the file PATH may still appear in retrieval provenance by design, so it is NOT
# asserted absent -- only the raw request-line forms are).
assert "SENSITIVE123" not in raw and "SECRETCMD" not in raw, "raw url/command persisted in log"
assert "REQUEST_URL: https" not in raw and "REQUEST_EXEC: echo" not in raw, \
    "raw REQUEST_* line persisted in logged round-1 text"
assert "<redacted>" in raw, "round-1 text was not redacted in the log"
print("4. log: web/exec/shadow_tooling populated; raw args + request lines redacted: PASS")

shutil.rmtree(wd, ignore_errors=True)
shutil.rmtree(logroot, ignore_errors=True)
print("\nEND-TO-END (voting + inspector, all 3 caps, redaction, logging): ALL PASS")
