#!/usr/bin/env python3
"""Read-only compact index over currently attested CHECKABLE Brain notes."""

from __future__ import annotations

import importlib.util
import json
import os
from collections import Counter
from pathlib import Path

COUNCIL_ROOT = Path(__file__).resolve().parent
DEFAULT_LIMIT = 16_000


def _validator():
    candidates = (
        COUNCIL_ROOT / "brain" / "validate_brain.py",
        COUNCIL_ROOT.parent / "brain" / "validate_brain.py",
        COUNCIL_ROOT.parent / "agentic-council" / "brain" / "validate_brain.py",
    )
    for path in candidates:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("council_validate_brain", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return None


def _vault() -> Path:
    configured = os.environ.get("COUNCIL_BRAIN_VAULT")
    return Path(configured).expanduser() if configured else COUNCIL_ROOT / "_brain"


def build_index(limit: int = DEFAULT_LIMIT) -> str:
    """Return validated ledger rows without executing checks or writing the ledger."""
    vault = _vault()
    vb = _validator()
    if vb is None:
        return "Council Brain index unavailable: validate_brain.py was not found."
    if not vault.is_dir():
        return f"Council Brain index: no vault at {vault}."
    try:
        ledger = json.loads((vault / ".attestations.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"Council Brain index unavailable: no readable attestation ledger at {vault}."
    if not isinstance(ledger, dict):
        return "Council Brain index unavailable: attestation ledger is not an object."

    parsed = []
    for path in sorted(vault.glob("*.md")):
        if path.name.startswith("_") or path.name == "README.md":
            continue
        fields, _body, errors = vb.parse_note(path)
        if not errors:
            errors = vb.validate_fields(fields)
        parsed.append((path, fields, errors))
    counts = Counter(fields.get("id") for _path, fields, errors in parsed
                     if not errors and fields.get("id"))

    rows = []
    for _path, fields, errors in parsed:
        note_id = fields.get("id")
        if errors or fields.get("type") != "checkable" or not note_id:
            continue
        if counts[note_id] != 1 or fields.get("superseded_by"):
            continue
        entry = ledger.get(note_id)
        if not isinstance(entry, dict) or entry.get("last_status") != "PASS":
            continue
        if entry.get("spec_sha256") != vb.spec_hash(fields):
            continue
        timestamp = entry.get("last_run") or entry.get("last_pass") or "timestamp-missing"
        claim = " ".join(vb.rendered_claim(fields).splitlines())
        rows.append(f"- {timestamp} | {note_id} | {claim}")

    head = (
        "Council Brain compact validated index (READ-ONLY; checks were NOT rerun):\n"
        "Rows require one structurally valid, unsuperseded CHECKABLE note, a matching "
        "full-spec ledger hash, and last_status PASS. TESTIMONY is excluded. Timestamps "
        "are last_run with last_pass fallback. The existing ledger defines no expiry "
        "and binds no artifact digest, so this index claims neither.\n"
        f"Vault: {vault}\nRows: {len(rows)}\n"
    )
    text = head + ("\n".join(rows) if rows else "(no rows satisfy the index predicate)")
    if len(text) <= limit:
        return text
    suffix = "\n[Brain index truncated to the configured character budget.]"
    return text[: max(0, limit - len(suffix))] + suffix


if __name__ == "__main__":
    print(build_index())
