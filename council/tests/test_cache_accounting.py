#!/usr/bin/env python3
"""Battery for prompt-cache/token accounting. Run from Council/:
    python3 council/tests/test_cache_accounting.py

EXITS NON-ZERO ON ANY FAILURE. No API calls: the transport's urlopen is stubbed, so
this runs free and offline.

WHAT IT PINS, in three layers:
  UNIT     -- _cache_accounting over the response shapes a provider can actually send.
  DELIVERY -- the real _openrouter_call_blocking against a stubbed urlopen, proving the
              fields reach the RECORD rather than merely being extractable. A helper
              nothing delivers is dead code, which is what this layer exists to refute.
  LOG      -- write_log's {**r} spread carries an arbitrary extra key through to the
              written JSON, which is the claim that let the accounting ship without a
              write_log change.

THE DISCRIMINATION THAT MATTERS: absent vs zero. A provider that reports
cached_tokens=0 said "nothing was cached"; a provider that omits the field said
nothing at all. Case D fails if the two are collapsed, because collapsing them would
let a silent provider be read as a measured cache miss.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import consult_council as cc  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def unit_checks() -> None:
    print("UNIT: _cache_accounting over real response shapes")

    full = {"usage": {"prompt_tokens": 41000, "completion_tokens": 800,
                      "total_tokens": 41800, "cost": 0.0123, "cache_discount": 0.0041,
                      "prompt_tokens_details": {"cached_tokens": 28000,
                                                "cache_write_tokens": 13000}}}
    got = cc._cache_accounting(full)
    check("A full payload yields every field",
          got == {"prompt_tokens": 41000, "completion_tokens": 800,
                  "total_tokens": 41800, "cost": 0.0123, "cache_discount": 0.0041,
                  "cached_tokens": 28000, "cache_write_tokens": 13000},
          f"got {got}")

    got = cc._cache_accounting({"usage": {"prompt_tokens": 900}})
    check("B usage without prompt_tokens_details yields only usage fields",
          got == {"prompt_tokens": 900}, f"got {got}")

    got = cc._cache_accounting({"choices": []})
    check("C no usage at all yields an empty dict (never a zero-filled one)",
          got == {}, f"got {got}")

    got = cc._cache_accounting(
        {"usage": {"prompt_tokens_details": {"cached_tokens": 0}}})
    check("D cached_tokens=0 is PRESENT with value 0 (absent != zero)",
          got == {"cached_tokens": 0}, f"got {got}")
    check("D' and an omitted cached_tokens stays absent",
          "cached_tokens" not in cc._cache_accounting({"usage": {}}))

    got = cc._cache_accounting({"cache_discount": 0.5, "usage": {"prompt_tokens": 10}})
    check("E cache_discount is read from the body top level when usage lacks it",
          got == {"prompt_tokens": 10, "cache_discount": 0.5}, f"got {got}")

    got = cc._cache_accounting(
        {"usage": {"cache_discount": 0.9}, "cache_discount": 0.1})
    check("E' the usage value wins over the body value when both exist",
          got == {"cache_discount": 0.9}, f"got {got}")

    got = cc._cache_accounting({"usage": {"prompt_tokens": "41000",
                                          "completion_tokens": None,
                                          "total_tokens": True}})
    check("F non-numeric values are DROPPED, never coerced",
          got == {}, f"got {got}  (True is an int in Python -- bools must not pass)")

    for bad in ({"usage": None}, {"usage": []}, {"usage": {"prompt_tokens_details": 7}},
                {}):
        try:
            cc._cache_accounting(bad)
        except Exception as e:  # noqa: BLE001
            check(f"G malformed payload {bad} does not raise", False, repr(e))
            break
    else:
        check("G malformed payloads do not raise", True)


def delivery_check() -> None:
    """The real transport function, stubbed at urlopen. Proves ARRIVAL, not parseability."""
    print("\nDELIVERY: the real _openrouter_call_blocking reaches the record")

    payload = {
        "model": "z-ai/glm-5.2",
        "choices": [{"message": {"content": "VERDICT: PASS\nREASONS:\n- fine"}}],
        "usage": {"prompt_tokens": 40123, "completion_tokens": 77,
                  "prompt_tokens_details": {"cached_tokens": 28000}},
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    real_urlopen = cc.urllib.request.urlopen
    real_key = cc.os.environ.get(cc.OPENROUTER_KEY_ENV)
    cc.urllib.request.urlopen = lambda req, timeout=None: _Resp()
    # A syntactically valid placeholder so the key-presence guard passes. Never a
    # real credential, and never printed.
    cc.os.environ[cc.OPENROUTER_KEY_ENV] = "test-not-a-real-key"
    try:
        rec = cc._openrouter_call_blocking("glm", ["z-ai/glm-5.2"], "prompt text")
    finally:
        cc.urllib.request.urlopen = real_urlopen
        if real_key is None:
            cc.os.environ.pop(cc.OPENROUTER_KEY_ENV, None)
        else:
            cc.os.environ[cc.OPENROUTER_KEY_ENV] = real_key

    check("H the record carries prompt_tokens", rec.get("prompt_tokens") == 40123,
          f"got {rec.get('prompt_tokens')}")
    check("H' the record carries cached_tokens", rec.get("cached_tokens") == 28000,
          f"got {rec.get('cached_tokens')}")
    check("H'' accounting did not disturb the existing contract",
          rec.get("verdict") == "PASS" and rec.get("model_used") == "z-ai/glm-5.2"
          and rec.get("returncode") == 0,
          f"verdict={rec.get('verdict')} model_used={rec.get('model_used')}")
    check("H''' a field the provider omitted is absent, not zeroed",
          "cache_write_tokens" not in rec)


def log_check() -> None:
    """write_log's {**r} spread is what lets accounting ship with no write_log change."""
    print("\nLOG: an extra record key survives into the written JSON")
    tmp = Path(tempfile.mkdtemp(prefix="cache_acct_"))
    real_logs = cc.LOGS_ROOT          # write_log builds LOGS_ROOT / <utc-date>
    cc.LOGS_ROOT = tmp
    try:
        rec = {"role": "glm", "text": "VERDICT: PASS", "stderr": "",
               "returncode": 0, "verdict": "PASS", "duration_s": 1.0,
               "prompt_tokens": 40123, "cached_tokens": 28000}
        cc.write_log("posttool", "Edit", "f.py", "pitch", [rec], "PASS")
        written = list(tmp.rglob("*.json"))
        if not written:
            check("I write_log produced a log file", False, f"none under {tmp}")
            return
        d = json.loads(written[0].read_text())
        m = (d.get("members") or [{}])[0]
        check("I prompt_tokens survives into the log", m.get("prompt_tokens") == 40123,
              f"got {m.get('prompt_tokens')}")
        check("I' cached_tokens survives into the log", m.get("cached_tokens") == 28000,
              f"got {m.get('cached_tokens')}")
    finally:
        cc.LOGS_ROOT = real_logs


def main() -> int:
    unit_checks()
    delivery_check()
    log_check()
    print(f"\nFAILURES: {len(FAILURES)}" + (f" -> {FAILURES}" if FAILURES else ""))
    return 1 if FAILURES else 0


sys.exit(main())
