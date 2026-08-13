#!/usr/bin/env python3
"""Ordered Codex lifecycle adapter for the agentic council.

Codex runs matching handlers concurrently, so one dispatcher owns the ordering
inside each event: evidence before review, pending recovery before a new tool,
and Brain/probes/directive inside one SessionStart result.

The rollback boundary is deliberately narrow. Council-managed patches are
serialized by a shared pending-path index and Codex serializes apply_patch
itself. A private prediction detects post-state mismatches caused by outside
writers. An uncooperative process can still write between the final identity
check and os.replace; this hook does not call that check atomic.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import brain_index
import evidence_logger
import laziness_gate
import session_start_directive
import session_start_probe
import stop_audit
import tier0_gate

COUNCIL_ROOT = Path(__file__).resolve().parent
WRAPPER = COUNCIL_ROOT / "consult_council.py"
STATE_VERSION = 1
# The hook template grants 1200 seconds. Reserve 100 seconds for process teardown,
# output delivery, and Codex's own handler deadline after the council subprocess exits.
REVIEW_TIMEOUT = 1100
# The local predictor should be fast; if the helper hangs, denying the real patch after
# one minute is safer than holding the hook until the outer 1200-second deadline.
PREDICT_TIMEOUT = 60
# Each target contributes marked head-and-tail context. There is deliberately no later
# global prefix truncation: every target must remain visible in the one complete fire.
CONTEXT_PER_FILE = 24_000
EXPECTED_VOTERS = {"claude", "gemini", "deepseek", "kimi", "glm", "grok"}
EXPECTED_INSPECTORS = {"hunyuan", "qwen", "minimax", "mimo", "nemotron", "mistral"}


class HookError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_root() -> Path:
    raw = os.environ.get("COUNCIL_STATE_ROOT") or str(Path.home() / ".codex" / "state")
    return Path(raw).expanduser().absolute() / "agentic-council"


def state_key(payload: dict) -> str:
    values = [str(payload.get(k) or "") for k in ("session_id", "turn_id", "tool_use_id")]
    if not all(values):
        raise HookError("hook payload is missing session_id, turn_id, or tool_use_id")
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def _under(root: Path, child: Path) -> bool:
    try:
        return os.path.commonpath((str(root.absolute()), str(child.absolute()))) == str(root.absolute())
    except ValueError:
        return False


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if not os.path.lexists(current):
            break
        if stat.S_ISLNK(os.lstat(current).st_mode):
            raise HookError(f"refusing symlink in council state path: {current}")


def ensure_private_dir(path: Path) -> Path:
    _reject_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise HookError(f"council state path is not a real directory: {path}")
    os.chmod(path, 0o700)
    return path


def _open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise HookError(f"state file is not regular: {path}")
    return fd


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    tmp = path.parent / (".partial-" + uuid.uuid4().hex)
    fd = _open_regular(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def atomic_json(path: Path, value: dict) -> None:
    atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def read_json(path: Path, limit: int = 10_000_000) -> dict:
    fd = _open_regular(path, os.O_RDONLY)
    try:
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(1_048_576, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise HookError(f"state file exceeds {limit} bytes: {path}")
    finally:
        os.close(fd)
    value = json.loads(b"".join(chunks))
    if not isinstance(value, dict):
        raise HookError(f"state file is not a JSON object: {path}")
    return value


@contextlib.contextmanager
def global_lock():
    root = ensure_private_dir(state_root())
    fd = _open_regular(root / "state.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def path_locks(paths: list[str]):
    lock_dir = ensure_private_dir(state_root() / "locks")
    fds = []
    try:
        for path in sorted(set(paths)):
            name = hashlib.sha256(path.encode()).hexdigest() + ".lock"
            fd = _open_regular(lock_dir / name, os.O_RDWR | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX)
            fds.append(fd)
        yield
    finally:
        for fd in reversed(fds):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _identity(path: Path, include_data: bool = False) -> tuple[dict, bytes | None]:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return {"kind": "absent"}, None
    if stat.S_ISLNK(before.st_mode):
        raise HookError(f"refusing symlink target: {path}")
    if not stat.S_ISREG(before.st_mode):
        raise HookError(f"refusing non-regular target: {path}")
    fd = _open_regular(path, os.O_RDONLY)
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise HookError(f"target changed while opening snapshot: {path}")
        data = bytearray()
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1_048_576)
            if not chunk:
                break
            digest.update(chunk)
            if include_data:
                data.extend(chunk)
    finally:
        os.close(fd)
    ident = {
        "kind": "file", "sha256": digest.hexdigest(), "size": opened.st_size,
        "mode": stat.S_IMODE(opened.st_mode), "dev": opened.st_dev, "ino": opened.st_ino,
        "mtime_ns": opened.st_mtime_ns, "ctime_ns": opened.st_ctime_ns,
    }
    return ident, bytes(data) if include_data else None


def semantic(identity: dict, *, include_mode: bool = True) -> dict:
    if identity.get("kind") != "file":
        return {"kind": identity.get("kind")}
    keys = ("kind", "sha256", "size", "mode") if include_mode else ("kind", "sha256", "size")
    return {key: identity.get(key) for key in keys}


def _target_path(raw: str, cwd: Path) -> str:
    if not raw or "\0" in raw:
        raise HookError("empty or NUL-containing patch path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    result = Path(os.path.abspath(candidate))
    if _under(state_root(), result):
        raise HookError("apply_patch may not modify the council hook state directory")
    return str(result)


def analyze_patch(patch: str, cwd: Path) -> dict:
    """Extract targets and rewrite only grammar-recognized path markers."""
    lines = patch.strip().splitlines()
    if len(lines) < 2 or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
        raise HookError("invalid apply_patch boundaries")
    mode, current = "started", None
    operations, targets, rewritten = [], {}, []
    synthetic = {}

    def name_for(path: str) -> str:
        if path not in synthetic:
            synthetic[path] = f"target-{len(synthetic):04d}"
        return synthetic[path]

    def add_target(path: str, role: str, op_index: int) -> None:
        rec = targets.setdefault(path, {"path": path, "roles": [], "operations": []})
        if op_index not in rec["operations"] and rec["operations"]:
            raise HookError(f"ambiguous duplicate target in one patch: {path}")
        rec["roles"].append(role)
        if op_index not in rec["operations"]:
            rec["operations"].append(op_index)

    for index, line in enumerate(lines):
        if index == 0:
            rewritten.append("*** Begin Patch")
            continue
        token = line.rstrip() if mode == "update" else line.strip()
        if token == "*** End Patch":
            rewritten.append("*** End Patch")
            mode = "ended"
            continue
        if token.startswith("*** Environment ID:"):
            raise HookError("remote Environment ID patches are not supported by the local snapshot hook")
        matched = re.match(r"^\*\*\* (Add|Delete|Update) File: (.+)$", token)
        if matched:
            kind, raw = matched.group(1).lower(), matched.group(2)
            path = _target_path(raw, cwd)
            current = {"kind": kind, "source": path, "destination": None, "added": []}
            operations.append(current)
            add_target(path, "source", len(operations) - 1)
            rewritten.append(f"*** {matched.group(1)} File: {name_for(path)}")
            mode = kind
            continue
        if mode == "update" and token.startswith("*** Move to: "):
            if current is None or current["destination"] is not None:
                raise HookError("invalid or duplicate Move to marker")
            destination = _target_path(token[len("*** Move to: "):], cwd)
            if destination == current["source"]:
                raise HookError("move source and destination resolve to the same path")
            current["destination"] = destination
            add_target(destination, "move_destination", len(operations) - 1)
            rewritten.append(f"*** Move to: {name_for(destination)}")
            continue
        if token.startswith("*** ") and mode != "update":
            raise HookError(f"unknown patch marker: {token}")
        if current is not None and mode in ("add", "update") and line.startswith("+"):
            current["added"].append(line[1:])
        rewritten.append(line)
    if mode != "ended" or not operations:
        raise HookError("patch contains no complete file operation")
    return {
        "operations": operations,
        "targets": list(targets.values()),
        "synthetic": synthetic,
        "rewritten": "\n".join(rewritten) + "\n",
    }


def evidence_file(session_id: str) -> Path:
    base = ensure_private_dir(state_root() / "sessions" / session_hash(session_id))
    metadata = base / "session.json"
    if not metadata.exists():
        atomic_json(metadata, {"session_id": session_id, "created_at": now_iso()})
    return base / "evidence.jsonl"


def append_evidence(payload: dict, extra: dict | None = None) -> None:
    session_id = str(payload.get("session_id") or "")
    tool_name = str(payload.get("tool_name") or "")
    if not session_id or not tool_name:
        return
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tool_response = payload.get("tool_response")
    lower = tool_name.lower()
    if lower in {"exec_command", "unified_exec", "shell", "shell_command"}:
        normalized = {"command": tool_input.get("cmd") or tool_input.get("command") or ""}
        event = evidence_logger.extract_event("Bash", normalized, tool_response)
        event["codex_tool_name"] = tool_name
    else:
        event = evidence_logger.extract_event(tool_name, tool_input, tool_response)
    if extra:
        event.update(extra)
    path = evidence_file(session_id)
    fd = _open_regular(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.write(fd, (json.dumps(event, default=str) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _profile_error() -> str | None:
    raw = os.environ.get("COUNCIL_ROSTER_PATH")
    if not raw:
        return "COUNCIL_ROSTER_PATH is unset"
    path = Path(raw).expanduser()
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return f"Codex council profile unreadable: {exc}"
    members = value.get("members") if isinstance(value, dict) else None
    leader = value.get("leader") if isinstance(value, dict) else None
    if not isinstance(members, list) or not isinstance(leader, dict):
        return "Codex council profile lacks leader/members"
    voters = {m.get("name") for m in members if isinstance(m, dict) and m.get("tier") == "voting"}
    inspectors = {m.get("name") for m in members if isinstance(m, dict) and m.get("tier") == "inspector"}
    if leader.get("name") != "codex" or voters != EXPECTED_VOTERS or inspectors != EXPECTED_INSPECTORS:
        return f"Codex profile is not the required codex + 6 + 6 bench: leader={leader.get('name')}, voters={sorted(voters)}, inspectors={sorted(inspectors)}"
    return None


def _is_exempt(path: str) -> bool:
    p = Path(path).absolute()
    roots = (COUNCIL_ROOT, COUNCIL_ROOT.parent / "agentic-council",
             COUNCIL_ROOT.parent / "agentic-council-codex")
    if any(_under(root.absolute(), p) for root in roots):
        return True
    text = str(p).lower()
    return any(part in text for part in ("/.claude/projects/", "/.claude/commands/", "/memory/"))


def laziness_check(analysis: dict, session_id: str) -> tuple[str | None, str | None]:
    content = "\n".join("\n".join(op["added"]) for op in analysis["operations"]
                        if not _is_exempt(op["destination"] or op["source"]))
    if not content:
        return None, None
    path = evidence_file(session_id)
    if not path.exists():
        commands, blind = "", f"no evidence file at {path}"
    else:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
            parts = []
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                parts.extend((str(event.get("tool") or ""), str(event.get("command") or "")))
            commands, blind = " || ".join(parts), None
        except OSError as exc:
            commands, blind = "", f"evidence unreadable ({exc})"
    hits, probes, trigger_seen = [], set(), False
    for pattern, label, markers in laziness_gate.TRIGGERS:
        if not re.search(pattern, content, re.IGNORECASE):
            continue
        trigger_seen = True
        if blind is None and markers and any(marker.lower() in commands.lower() for marker in markers):
            continue
        if blind is None:
            hits.append(label)
            probes.update(markers)
    if hits:
        return ("COUNCIL laziness gate denied this patch. Unbacked trigger(s): "
                + "; ".join(hits) + (". Run a matching probe first: " + ", ".join(sorted(probes)) if probes else ". Rewrite the bare hedge with concrete evidence.")), None
    if blind and trigger_seen:
        return None, "COUNCIL laziness gate was evidence-blind and therefore allowed the patch: " + blind
    return None, None


def _pending_dirs() -> list[Path]:
    root = ensure_private_dir(state_root() / "pending")
    result = []
    for path in sorted(root.iterdir()):
        if not re.fullmatch(r"[0-9a-f]{64}", path.name):
            raise HookError(f"unexpected entry in pending state: {path}")
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise HookError(f"pending entry is not a real directory: {path}")
        result.append(path)
    return result


def load_manifest(directory: Path) -> dict:
    value = read_json(directory / "manifest.json")
    if value.get("version") != STATE_VERSION or value.get("key") != directory.name:
        raise HookError(f"invalid manifest identity at {directory}")
    targets = value.get("targets")
    if not isinstance(targets, list) or not targets:
        raise HookError(f"manifest has no targets at {directory}")
    seen = set()
    for target in targets:
        path = target.get("path") if isinstance(target, dict) else None
        if not isinstance(path, str) or not Path(path).is_absolute() or path in seen:
            raise HookError(f"invalid or duplicate manifest target at {directory}")
        seen.add(path)
    return value


def prepare_snapshot(payload: dict, patch: str, analysis: dict) -> tuple[dict, str | None]:
    key = state_key(payload)
    root = ensure_private_dir(state_root())
    pending = ensure_private_dir(root / "pending")
    staging_root = ensure_private_dir(root / "staging")
    final = pending / key
    staging = staging_root / (key + "-" + uuid.uuid4().hex)
    with global_lock():
        wanted = {target["path"] for target in analysis["targets"]}
        for directory in _pending_dirs():
            other = load_manifest(directory)
            overlap = wanted & {target["path"] for target in other["targets"]}
            if overlap:
                raise HookError("another council-managed patch is pending for: " + ", ".join(sorted(overlap)))
        if os.path.lexists(final):
            raise HookError(f"snapshot key already exists: {key}")
        ensure_private_dir(staging)
        blobs = ensure_private_dir(staging / "blobs")
        predictor = ensure_private_dir(staging / "predictor")
        try:
            targets = []
            by_path = {record["path"]: record for record in analysis["targets"]}
            for index, path in enumerate(sorted(by_path)):
                pre, data = _identity(Path(path), include_data=True)
                record = {**by_path[path], "pre": pre, "blob": None}
                if data is not None:
                    blob_name = f"{index:04d}.bin"
                    atomic_bytes(blobs / blob_name, data)
                    record["blob"] = blob_name
                    synthetic = predictor / analysis["synthetic"][path]
                    atomic_bytes(synthetic, data, pre["mode"])
                targets.append(record)
            helper = os.environ.get("COUNCIL_APPLY_PATCH_BIN") or shutil.which("apply_patch")
            if not helper:
                raise HookError("installed apply_patch helper not found on PATH")
            proc = subprocess.run([helper], input=analysis["rewritten"], text=True,
                                  capture_output=True, cwd=predictor, timeout=PREDICT_TIMEOUT)
            if proc.returncode != 0:
                detail = (proc.stdout + "\n" + proc.stderr).strip()[-4000:]  # stale-ok: an error-tail slice for one HookError message; unrelated to council_events.FIELD_MAX despite sharing the number, and nothing reads the two together
                raise HookError("private apply_patch prediction failed; real patch denied: " + detail)
            for record in targets:
                expected, _ = _identity(predictor / analysis["synthetic"][record["path"]])
                record["expected"] = expected
            manifest = {
                "version": STATE_VERSION, "key": key, "status": "pending",
                "created_at": now_iso(), "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"), "tool_use_id": payload.get("tool_use_id"),
                "cwd": str(Path(payload.get("cwd") or ".").absolute()), "patch": patch,
                "operations": analysis["operations"], "targets": targets,
            }
            shutil.rmtree(predictor)
            atomic_json(staging / "manifest.json", manifest)
            os.replace(staging, final)
            _fsync_dir(pending)
            return manifest, None
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise


def _context(path: str) -> str:
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        return f"[{path}: unavailable: {exc}]"
    text = data.decode("utf-8", errors="replace")
    if len(text) <= CONTEXT_PER_FILE:
        return text
    half = (CONTEXT_PER_FILE - 120) // 2
    return text[:half] + f"\n[... {len(text) - 2 * half} characters elided ...]\n" + text[-half:]


def build_pitch(manifest: dict, actual: dict[str, dict], reason: str) -> str:
    lines = [
        "Tool: Codex apply_patch", f"Lifecycle: {reason}",
        "Review the complete patch as one unit. Every target and resulting context is below.",
        "Context is capped per file and any truncation is marked explicitly.", "",
        "--- Raw patch begin ---", manifest.get("patch", ""), "--- Raw patch end ---", "",
        "--- Target identities ---",
    ]
    for target in manifest["targets"]:
        path = target["path"]
        lines.append(json.dumps({"path": path, "roles": target.get("roles"),
                                 "pre": semantic(target.get("pre", {})),
                                 "expected": semantic(target.get("expected", {})),
                                 "actual": semantic(actual[path])}, sort_keys=True))
        lines.extend((f"--- Resulting context: {path} ---", _context(path), ""))
    return "\n".join(lines)


def run_council(manifest: dict, pitch: str) -> dict:
    cmd = [sys.executable, str(WRAPPER), "--layer", "posttool", "--tool-name", "apply_patch",
           "--target-path", json.dumps([target["path"] for target in manifest["targets"]]),
           "--workdir", manifest.get("cwd") or ".", "--session-id", manifest.get("session_id") or ""]
    ev = evidence_file(str(manifest.get("session_id") or ""))
    if ev.exists():
        cmd.extend(("--evidence-file", str(ev)))
    try:
        proc = subprocess.run(cmd, input=pitch, text=True, capture_output=True,
                              cwd=manifest.get("cwd") or ".", timeout=REVIEW_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"rc": 3, "text": "Council timed out; no clean verdict exists.", "bench_complete": False}
    text = "\n\n".join(part.rstrip() for part in (proc.stdout, proc.stderr) if part)
    voters = set(re.findall(r"^# member: (\S+)", proc.stdout, re.MULTILINE))
    inspectors = set(re.findall(r"^# layer-2 \(NON-VOTING\): (\S+)", proc.stdout, re.MULTILINE))
    complete = voters == EXPECTED_VOTERS and inspectors == EXPECTED_INSPECTORS
    if not complete:
        text += ("\n\nCOUNCIL BENCH INCOMPLETE: expected voters " + str(sorted(EXPECTED_VOTERS))
                 + " and inspectors " + str(sorted(EXPECTED_INSPECTORS)) + "; observed "
                 + str(sorted(voters)) + " / " + str(sorted(inspectors)) + ".")
    return {"rc": proc.returncode, "text": text, "bench_complete": complete}


def _same_full(left: dict, right: dict) -> bool:
    return left == right


def _blob(directory: Path, target: dict) -> bytes:
    name = target.get("blob")
    if not isinstance(name, str) or not re.fullmatch(r"\d{4}\.bin", name):
        raise HookError(f"missing safe snapshot blob for {target.get('path')}")
    path = directory / "blobs" / name
    fd = _open_regular(path, os.O_RDONLY)
    try:
        data = bytearray()
        while True:
            chunk = os.read(fd, 1_048_576)
            if not chunk:
                break
            data.extend(chunk)
    finally:
        os.close(fd)
    result = bytes(data)
    if hashlib.sha256(result).hexdigest() != target["pre"].get("sha256"):
        raise HookError(f"snapshot blob digest mismatch for {target.get('path')}")
    return result


def _park(path: Path, identity: dict, data: bytes) -> Path:
    configured = os.environ.get("COUNCIL_REVERT_ROOT")
    root = ensure_private_dir(Path(configured).expanduser() if configured else COUNCIL_ROOT / "reverted")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)[:100] or "file"
    target = root / f"{stamp}-{uuid.uuid4().hex[:10]}-{safe_name}"
    atomic_bytes(target, data, identity.get("mode", 0o600))
    return target


def restore(directory: Path, manifest: dict, reviewed: dict[str, dict]) -> list[str]:
    """Best-effort per-file restore after immediate identity rechecks.

    Locks serialize council-managed writers. They cannot stop an unrelated
    process that ignores those locks, so every note names the remaining
    check-to-replace window rather than presenting this as atomic CAS.
    """
    notes, parked = [], {}
    # Park every available rejected byte sequence before the first replacement.
    try:
        for target in manifest["targets"]:
            path = Path(target["path"])
            pre = target["pre"]
            if pre.get("kind") != "file":
                continue
            current, data = _identity(path, include_data=True)
            if not _same_full(current, reviewed[str(path)]):
                raise HookError(f"identity changed since review for {path}")
            if data is not None:
                parked[str(path)] = str(_park(path, current, data))
    except Exception as exc:
        return [f"AUTO-RESTORE ABORTED before modifying any target: rejected bytes could not all be parked ({exc})."]

    for target in manifest["targets"]:
        path = Path(target["path"])
        pre = target["pre"]
        if pre.get("kind") == "absent":
            notes.append(f"AUTO-RESTORE DECLINED for new path {path}: new files and move destinations are never deleted.")
            continue
        try:
            current, _ = _identity(path)
            if not _same_full(current, reviewed[str(path)]):
                notes.append(f"AUTO-RESTORE DECLINED for {path}: identity changed after review.")
                continue
            data = _blob(directory, target)
            ensure_private_dir(path.parent) if _under(state_root(), path.parent) else path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".council-")
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, pre["mode"])
                os.replace(tmp, path)
                _fsync_dir(path.parent)
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink()
            notes.append(f"AUTO-RESTORED {path} per-file; rejected bytes parked at {parked.get(str(path), '(file was absent)')}. Residual: an uncooperative writer can race the final check-to-replace window.")
        except Exception as exc:
            notes.append(f"AUTO-RESTORE FAILED for {path}: {exc}. Earlier per-file restores, if named above, remain applied.")
    return notes


def archive(directory: Path, manifest: dict) -> None:
    manifest["archived_at"] = now_iso()
    atomic_json(directory / "manifest.json", manifest)
    receipts = ensure_private_dir(state_root() / "receipts")
    destination = receipts / (directory.name + "-" + uuid.uuid4().hex[:8])
    with global_lock():
        os.replace(directory, destination)
        _fsync_dir(receipts)


def prediction_matches(manifest: dict, actual: dict[str, dict]) -> bool:
    for target in manifest["targets"]:
        include_mode = target["pre"].get("kind") == "file"
        if semantic(actual[target["path"]], include_mode=include_mode) != semantic(target["expected"], include_mode=include_mode):
            return False
    return True


def review_directory(directory: Path, reason: str) -> dict:
    manifest = load_manifest(directory)
    paths = [target["path"] for target in manifest["targets"]]
    with path_locks(paths):
        actual = {path: _identity(Path(path))[0] for path in paths}
        manifest["status"] = "reviewing"
        manifest["actual_before_review"] = actual
        atomic_json(directory / "manifest.json", manifest)
        result = run_council(manifest, build_pitch(manifest, actual, reason))
        restore_notes = []
        if result["rc"] == 2:
            if prediction_matches(manifest, actual):
                restore_notes = restore(directory, manifest, actual)
            else:
                restore_notes = ["AUTO-RESTORE DECLINED: actual post state does not match the private pre-execution prediction; prior bytes will not be guessed over possible external or partial changes."]
        manifest["status"] = "reviewed"
        manifest["review"] = {"at": now_iso(), "rc": result["rc"],
                              "bench_complete": result["bench_complete"],
                              "restore_notes": restore_notes}
        archive(directory, manifest)
    if restore_notes:
        result["text"] += "\n\n" + "\n".join(restore_notes)
    return result


def review_without_snapshot(payload: dict, patch: str, error: str) -> dict:
    try:
        analysis = analyze_patch(patch, Path(payload.get("cwd") or "."))
        targets = []
        for record in analysis["targets"]:
            actual, _ = _identity(Path(record["path"]))
            targets.append({**record, "pre": {"kind": "unknown"},
                            "expected": {"kind": "unknown"}, "actual": actual})
    except Exception:
        targets = [{"path": "unknown", "roles": [], "pre": {"kind": "unknown"},
                    "expected": {"kind": "unknown"}, "actual": {"kind": "unknown"}}]
    manifest = {"patch": patch, "targets": targets, "cwd": payload.get("cwd") or ".",
                "session_id": payload.get("session_id") or ""}
    actual = {target["path"]: target["actual"] for target in targets}
    result = run_council(manifest, build_pitch(manifest, actual, "snapshot unavailable: " + error))
    result["text"] += "\n\nAUTO-RESTORE DECLINED: no validated pre-edit snapshot exists."
    result["state_error"] = True
    return result


def reconcile_pending(session_id: str) -> list[dict]:
    results = []
    with global_lock():
        directories = _pending_dirs()
        selected = []
        for directory in directories:
            manifest = load_manifest(directory)
            if manifest.get("session_id") == session_id:
                selected.append(directory)
    for directory in selected:
        manifest = load_manifest(directory)
        changed = False
        for target in manifest["targets"]:
            actual, _ = _identity(Path(target["path"]))
            if semantic(actual) != semantic(target["pre"]):
                changed = True
                break
        if changed:
            results.append(review_directory(directory, "delayed recovery after PostToolUse was absent"))
        else:
            manifest["status"] = "unchanged-attempt"
            archive(directory, manifest)
    return results


def emit_pre_deny(reason: str) -> int:
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                      "permissionDecision": "deny",
                                      "permissionDecisionReason": reason},
               "systemMessage": reason}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def emit_pre_context(text: str) -> int:
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                      "permissionDecision": "allow",
                                      "additionalContext": text},
               "systemMessage": text}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def emit_post(result: dict) -> int:
    text = result.get("text") or "Council review returned no text."
    if result.get("state_error") or result.get("rc") not in (0, 1, 2):
        json.dump({"decision": "block", "reason": text}, sys.stdout)
    elif result["rc"] == 2:
        json.dump({"decision": "block", "reason": text}, sys.stdout)
    elif result["rc"] == 1 or not result.get("bench_complete"):
        json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                          "additionalContext": text}}, sys.stdout)
    else:
        return 0
    sys.stdout.write("\n")
    return 0


def pre_tool(payload: dict) -> int:
    session_id = str(payload.get("session_id") or "")
    try:
        recovered = reconcile_pending(session_id)
    except Exception as exc:
        return emit_pre_deny(f"COUNCIL pending-state reconciliation failed: {exc}")
    if recovered:
        text = "\n\n".join(result["text"] for result in recovered)
        if any(result["rc"] == 2 or result["rc"] not in (0, 1, 2) for result in recovered):
            return emit_pre_deny("A prior apply_patch required council attention before this tool:\n" + text)
        if any(result["rc"] == 1 or not result["bench_complete"] for result in recovered):
            return emit_pre_context("A prior apply_patch was reconciled before this tool:\n" + text)

    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if tool_name != "apply_patch":
        lower = tool_name.lower()
        if lower in {"exec_command", "unified_exec", "shell", "shell_command", "bash"}:
            command = str(tool_input.get("cmd") or tool_input.get("command") or "")
            # tier0_gate.bash_write_targets tokenises with shlex and ties each target to the
            # construct that writes it, so a `>` unattached to a real target does not count.
            # Probed directly:
            #   `python3 x.py >/dev/null`            -> []
            #   `echo hi > /tmp/scratch/notes.md`    -> []
            #   `python3 -c "print('ok',b>a)"`       -> []      (a comparison operator)
            #   `echo hi > notes.md`                 -> [('notes.md', 'shell redirect')]
            # To compare candidate SCOPE boundaries over this install's real commands, run
            # `_nogit/bash_scope_measure.py`; it prints its own corpus size, which grows with use.
            writes = tier0_gate.bash_write_targets(command)
            if writes:
                detail = "; ".join(f"{tgt} [{why}]" for tgt, why in writes)
                return emit_pre_context(
                    "COUNCIL GUARD (advisory; nothing blocked): this shell call appears to WRITE "
                    f"to {detail}. Use apply_patch for reviewed changes, or explicitly fire the "
                    "council on scripted results.")
        return 0
    profile_error = _profile_error()
    if profile_error:
        return emit_pre_deny("Codex council profile validation failed: " + profile_error)
    patch = tool_input.get("command")
    if not isinstance(patch, str):
        return emit_pre_deny("apply_patch hook input lacks the raw command string")
    try:
        analysis = analyze_patch(patch, Path(payload.get("cwd") or "."))
        denied, blind = laziness_check(analysis, session_id)
        if denied:
            return emit_pre_deny(denied)
        prepare_snapshot(payload, patch, analysis)
    except Exception as exc:
        return emit_pre_deny("COUNCIL pre-edit snapshot/prediction failed; patch denied: " + str(exc))
    return emit_pre_context(blind) if blind else 0


def post_tool(payload: dict) -> int:
    append_evidence(payload)
    if payload.get("tool_name") != "apply_patch":
        return 0
    patch = (payload.get("tool_input") or {}).get("command") if isinstance(payload.get("tool_input"), dict) else ""
    try:
        key = state_key(payload)
        directory = state_root() / "pending" / key
        if not directory.is_dir():
            return emit_post(review_without_snapshot(payload, str(patch or ""), f"manifest {key} missing"))
        result = review_directory(directory, "successful PostToolUse")
    except Exception as exc:
        result = review_without_snapshot(payload, str(patch or ""), str(exc))
    append_evidence(payload, {"council_review_rc": result.get("rc"),
                              "council_bench_complete": result.get("bench_complete")})
    return emit_post(result)


def stop(payload: dict) -> int:
    if payload.get("stop_hook_active"):
        return 0
    session_id = str(payload.get("session_id") or "")
    messages = []
    try:
        recovered = reconcile_pending(session_id)
        for result in recovered:
            if result["rc"] != 0 or not result["bench_complete"]:
                messages.append(result["text"])
    except Exception as exc:
        messages.append("Pending apply_patch reconciliation failed at Stop: " + str(exc))
    text = payload.get("last_assistant_message")
    if isinstance(text, str) and text:
        commands = ""
        ev = evidence_file(session_id)
        if ev.exists():
            with contextlib.suppress(OSError):
                commands = ev.read_text(encoding="utf-8", errors="replace")
        hits = stop_audit.detect_laziness_triggers(text, commands)
        if hits:
            messages.append("Rule-11 triggers in the last assistant message lack matching evidence: " + ", ".join(hits))
        for block in stop_audit.extract_tagged_blocks(text):
            manifest = {"patch": block, "targets": [{"path": "outward-prose", "roles": ["prose"],
                        "pre": {"kind": "unknown"}, "expected": {"kind": "unknown"}}],
                        "cwd": payload.get("cwd") or ".", "session_id": session_id}
            result = run_council(manifest, "Tagged outward-facing prose:\n\n" + block)
            if result["rc"] != 0 or not result["bench_complete"]:
                messages.append(result["text"])
    if messages:
        json.dump({"decision": "block", "reason": "Stop-hook council audit requires continuation:\n\n" + "\n\n---\n\n".join(messages)}, sys.stdout)
        sys.stdout.write("\n")
    return 0


def session_start(payload: dict) -> int:
    session_id = str(payload.get("session_id") or "")
    if session_id:
        for display, argv in session_start_probe.PROBES:
            probe = session_start_probe.run_probe(display, argv)
            event_payload = {"session_id": session_id, "tool_name": "Bash",
                             "tool_input": {"command": display},
                             "tool_response": {"exitCode": probe.get("exit_code"),
                                               "stdout": probe.get("stdout_tail", ""),
                                               "stderr": probe.get("stderr_tail", ""),
                                               "interrupted": probe.get("interrupted")}}
            append_evidence(event_payload)
    profile_error = _profile_error()
    panel_directive = (
        "Council leader identity: Codex. The active panel must be 1 leader + 6 voting "
        "members + 6 non-voting inspectors, with Claude Opus 5 in the cross-family "
        "voting seat. Treat silence, a short bench, or a crashed review as unverified."
    )
    if profile_error:
        panel_directive += "\nPROFILE ERROR: " + profile_error
    context = (session_start_directive.DIRECTIVE + "\n\n" + panel_directive
               + "\n\n" + brain_index.build_index())
    json.dump({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                      "additionalContext": context}}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"pre-tool", "post-tool", "stop", "session-start"}:
        print("usage: codex_hook.py pre-tool|post-tool|stop|session-start", file=sys.stderr)
        return 2
    if (COUNCIL_ROOT / "DISABLED").exists():
        return 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except Exception as exc:
        print(f"codex-hook: invalid JSON payload: {exc}", file=sys.stderr)
        return 1
    return {"pre-tool": pre_tool, "post-tool": post_tool,
            "stop": stop, "session-start": session_start}[sys.argv[1]](payload)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"codex-hook: unhandled failure: {exc}", file=sys.stderr)
        sys.exit(1)
