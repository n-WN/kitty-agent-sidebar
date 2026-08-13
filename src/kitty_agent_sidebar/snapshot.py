#!/usr/bin/env python3
"""Record a privacy-minimal map of live Codex/Claude sessions in Kitty.

This is a five-minute one-shot collector, not a daemon.  It does not inspect
prompts, screen text, cwd, or environment values. Identity comes from the
bridge's official-hook user variable first, then direct runtime artifacts owned
by the foreground agent process. The manual tab title is stored only as the
recovery label requested by the user. Ambiguous identity is recorded as
``session_id: null`` rather than guessed.
"""
from __future__ import annotations

import fcntl
import glob
import gzip
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, NamedTuple


RC_CLIENT_VERSION = (0, 30, 0)
STATUS_KEY = "agent_status_v1"
RUNTIME_STATUS_KEY = "agent_runtime_v1"
HOME = Path.home()
if sys.platform == "darwin":
    PRIVATE_ROOT = HOME / "Library" / "Application Support" / "kitty-agent-status.noindex"
else:
    PRIVATE_ROOT = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state")) / "kitty-agent-status"
SNAPSHOT_DIR = PRIVATE_ROOT / "snapshots"
LOCK_PATH = PRIVATE_ROOT / "snapshot.lock"
CURSOR_PATH = PRIVATE_ROOT / "cursor.json"
FIRST_SEEN_PATH = PRIVATE_ROOT / "first-seen.json"
STDERR_PATH = PRIVATE_ROOT / "collector-errors.jsonl"
MAX_TOTAL_BYTES = 128 * 1024 * 1024
RETENTION_SECONDS = 30 * 86400
MAX_SEGMENT_BYTES = 16 * 1024 * 1024
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
VALID_STATES = frozenset(("working", "ready", "action", "error", "disconnected", "unknown"))
ROLLOUT_SCAN_CHUNK = 1024 * 1024
ROLLOUT_SCAN_LIMIT = 64 * 1024 * 1024
CODEX_HISTORY_DB = HOME / ".codex" / "thread_history_1.sqlite"


def _boot_id() -> str:
    """Return a stable identifier for the current OS boot when available."""
    if sys.platform == "darwin":
        try:
            value = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                capture_output=True, text=True, timeout=1, check=False,
            ).stdout.strip()
            if UUID_RE.fullmatch(value):
                return value.lower()
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            if UUID_RE.fullmatch(value):
                return value.lower()
        except OSError:
            pass
    return "unknown"


BOOT_ID = _boot_id()


class KittyInstance(NamedTuple):
    instance_id: str
    pid: int | None
    process_start_ns: int
    socket_path: str


def _peer_pid(peer: socket.socket) -> int | None:
    """Return macOS LOCAL_PEERPID without depending on Python constants."""
    try:
        # Darwin sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=0x002.
        return int.from_bytes(peer.getsockopt(0, 2, 4), sys.byteorder, signed=True)
    except OSError:
        return None


def _mkdirs() -> None:
    PRIVATE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(PRIVATE_ROOT, 0o700)
    os.chmod(SNAPSHOT_DIR, 0o700)
    marker = PRIVATE_ROOT / ".metadata_never_index"
    try:
        marker.touch(mode=0o600, exist_ok=True)
        os.chmod(marker, 0o600)
    except OSError:
        pass


def _write_error(code: str, **fields: Any) -> None:
    try:
        record = {"ts_ns": time.time_ns(), "code": code}
        record.update(fields)
        with STDERR_PATH.open("a", encoding="utf-8") as stream:
            os.chmod(STDERR_PATH, 0o600)
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _safe_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return default


def _atomic_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, separators=(",", ":"), ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _remote_request(
    path: str, command: str, payload: dict[str, Any], expected_pid: int | None = None
) -> dict[str, Any]:
    message = {
        "cmd": command,
        "version": list(RC_CLIENT_VERSION),
        "no_response": False,
        "payload": payload,
    }
    body = ("@kitty-cmd" + json.dumps(message, separators=(",", ":"))).encode("ascii")
    wire = b"\x1bP" + body + b"\x1b\\"
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.settimeout(1.5)
    chunks: list[bytes] = []
    try:
        peer.connect(path)
        if expected_pid is not None and _peer_pid(peer) != expected_pid:
            raise RuntimeError("remote_control_peer_mismatch")
        peer.sendall(wire)
        peer.shutdown(socket.SHUT_WR)
        total = 0
        while total <= 4 * 1024 * 1024:
            try:
                chunk = peer.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        peer.close()
    raw = b"".join(chunks)
    prefix = b"\x1bP@kitty-cmd"
    start = raw.find(prefix)
    end = raw.find(b"\x1b\\", start + len(prefix)) if start >= 0 else -1
    if start < 0 or end < 0:
        raise RuntimeError("missing_remote_control_response")
    value = json.loads(raw[start + len(prefix) : end])
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise RuntimeError("remote_control_error")
    return value


def _remote_set_vars(
    path: str, by_window: dict[int, list[str]], expected_pid: int | None = None
) -> None:
    """Publish many per-window vars over one Kitty socket connection."""
    if not by_window:
        return
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.settimeout(1.5)
    try:
        peer.connect(path)
        if expected_pid is not None and _peer_pid(peer) != expected_pid:
            raise RuntimeError("remote_control_peer_mismatch")
        for window_id, variables in by_window.items():
            message = {
                "cmd": "set-user-vars", "version": list(RC_CLIENT_VERSION),
                "no_response": False,
                "payload": {"match": f"id:{window_id}", "self": False, "var": variables},
            }
            body = ("@kitty-cmd" + json.dumps(message, separators=(",", ":"))).encode("ascii")
            peer.sendall(b"\x1bP" + body + b"\x1b\\")
            response = _read_remote_response(peer)
            if response.get("ok") is not True:
                raise RuntimeError("set_user_vars_failed")
    finally:
        peer.close()


def _read_remote_response(peer: socket.socket) -> dict[str, Any]:
    """Read one framed Kitty response from an already connected peer."""
    prefix = b"\x1bP@kitty-cmd"
    suffix = b"\x1b\\"
    raw = bytearray()
    while len(raw) <= 1024 * 1024:
        chunk = peer.recv(8192)
        if not chunk:
            break
        raw.extend(chunk)
        start = raw.find(prefix)
        if start >= 0:
            end = raw.find(suffix, start + len(prefix))
            if end >= 0:
                value = json.loads(raw[start + len(prefix) : end])
                return value if isinstance(value, dict) else {}
    return {}


def _kitty_gui_pids() -> set[int]:
    """Find this user's Kitty GUI processes from their open Unix sockets."""
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-a", "-U", "-u", str(os.getuid()),
             "-c", "kitty", "-Fpcn"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    found: set[int] = set()
    pid: int | None = None
    command = ""
    has_filesystem_socket = False
    def commit() -> None:
        if pid is not None and command == "kitty" and has_filesystem_socket:
            found.add(pid)
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].isdecimal():
            commit(); pid = int(line[1:]); command = ""; has_filesystem_socket = False
        elif line.startswith("c"):
            command = line[1:]
        elif line.startswith("n/"):
            has_filesystem_socket = True
    commit()
    return found


def _lsof_unix_paths(pid: int) -> list[str]:
    """List filesystem Unix sockets held by one Kitty process."""
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-U", "-p", str(pid), "-Fn"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("n/"):
            continue
        path = line[1:]
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if info.st_uid == os.getuid() and stat.S_ISSOCK(info.st_mode):
            paths.append(path)
    return paths


def _socket_candidates() -> Iterator[KittyInstance]:
    """Discover every live owned Kitty filesystem RC socket.

    The caller sends the single read-only ``ls`` request for this collection
    cycle. Peer PID plus owned socket metadata authenticates the endpoint here;
    a stale or non-RC socket is rejected naturally by that one request.
    """
    uid = os.getuid()
    gui_pids = _kitty_gui_pids()
    seen: set[tuple[int, int]] = set()
    configured = os.environ.get("KITTY_AGENT_SIDEBAR_SOCKET_GLOB", "")
    state_home = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state"))
    paths: list[tuple[str, int | None]] = []
    for pid in gui_pids:
        paths.extend((path, pid) for path in _lsof_unix_paths(pid))
    for pattern in filter(None, (
        configured, str(state_home / "kitty-agent-status" / "kitty-rc-*"),
        str(Path(os.environ.get("TMPDIR", "/tmp")) / "kitty-rc-*"), "/tmp/kitty-rc-*",
    )):
        paths.extend((path, None) for path in glob.glob(pattern))
    for path, owner_pid in paths:
        peer = None
        try:
            info = os.lstat(path)
            if info.st_uid != uid or not stat.S_ISSOCK(info.st_mode):
                continue
            key = (info.st_dev, info.st_ino)
            if key in seen:
                continue
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peer.settimeout(0.5)
            peer.connect(path)
            peer_pid = _peer_pid(peer)
            if peer_pid is not None and peer_pid not in gui_pids:
                continue
            pid = peer_pid or owner_pid
            if pid is None or pid not in gui_pids:
                continue
            process_start_ns = _proc_start_ns(pid)
            seen.add(key)
            identity = f"{BOOT_ID}:{pid}:{process_start_ns}:{info.st_dev}:{info.st_ino}"
            yield KittyInstance(identity, pid, process_start_ns, path)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        finally:
            if peer is not None:
                peer.close()


def _agent_kind(processes: list[dict[str, Any]]) -> tuple[str | None, int | None]:
    found: list[tuple[str, int]] = []
    for process in processes:
        cmdline = process.get("cmdline")
        pid = process.get("pid")
        if not isinstance(cmdline, list) or not cmdline or not isinstance(pid, int):
            continue
        args = [str(part) for part in cmdline]
        executable = os.path.basename(args[0]).lower()
        joined = "\0".join(args).lower()
        if executable == "codex" or "@openai/codex" in joined:
            # Prefer the native binary over its Node launcher.
            rank = 0 if "vendor/" in joined or executable == "codex" else 1
            found.append((f"codex:{rank}", pid))
        elif executable == "claude" or "@anthropic-ai/claude-code" in joined:
            rank = 0 if executable == "claude" else 1
            found.append((f"claude:{rank}", pid))
    if not found:
        return None, None
    found.sort(key=lambda item: (int(item[0].split(":")[1]), item[1]))
    kind = found[0][0].split(":")[0]
    return kind, found[0][1]


def _read_rollout_root(path: str) -> tuple[str, int] | None:
    try:
        with open(path, "rb") as stream:
            raw = stream.readline(512 * 1024)
        value = json.loads(raw)
        payload = value.get("payload") if isinstance(value, dict) else None
        if not isinstance(payload, dict):
            return None
        sid = payload.get("id")
        if (
            value.get("type") != "session_meta" or not isinstance(sid, str) or
            not UUID_RE.fullmatch(sid) or payload.get("session_id") != sid or
            payload.get("originator") != "codex-tui" or
            payload.get("thread_source") != "user" or payload.get("parent_thread_id")
        ):
            return None
        timestamp = payload.get("timestamp") or value.get("timestamp")
        opened_ns = 0
        if isinstance(timestamp, str):
            try:
                opened_ns = int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1e9)
            except ValueError:
                pass
        return sid, opened_ns
    except (OSError, TypeError, ValueError):
        return None


def _timestamp_ns(value: Any) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Avoid float timestamp rounding: provenance ordering must not move a
        # boundary by a few dozen nanoseconds merely because Python 3.9 uses a
        # binary float for datetime.timestamp(). JSON timestamps are ms/us.
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = parsed.astimezone(timezone.utc) - epoch
        return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
    except ValueError:
        return 0


def _reverse_rollout_lines(
    path: str, limit: int = ROLLOUT_SCAN_LIMIT
) -> Iterator[bytes]:
    """Yield complete JSONL records newest-first with bounded reverse I/O.

    Rollouts can exceed 100 MiB and a single model/tool record can exceed one
    chunk.  ``carry`` preserves such records across chunk boundaries.  The
    caller never reads the whole transcript and never examines content fields.
    """
    with open(path, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        remaining = min(position, limit)
        carry = b""
        while position > 0 and remaining > 0:
            size = min(ROLLOUT_SCAN_CHUNK, position, remaining)
            position -= size
            remaining -= size
            stream.seek(position)
            parts = (stream.read(size) + carry).split(b"\n")
            carry = parts[0]
            for raw in reversed(parts[1:]):
                if raw:
                    yield raw
        if position == 0 and carry:
            yield carry


def _codex_history_state(
    session_id: str, rollout_path: str
) -> tuple[str, int, str | None] | None:
    """Read Codex's own fully projected latest-turn state when available.

    Codex 0.147 materializes durable ``TurnStarted``/``TurnComplete`` records
    into ``thread_history_1.sqlite`` and advances the projection checkpoint in
    the same transaction.  A row is authoritative only when that checkpoint
    exactly equals the current rollout size; otherwise the database is behind
    the live JSONL and the bounded rollout parser remains the source of truth.
    """
    try:
        rollout_size = os.path.getsize(rollout_path)
        uri = f"file:{CODEX_HISTORY_DB}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.1) as connection:
            projection = connection.execute(
                "SELECT next_rollout_byte_offset "
                "FROM thread_history_projection_state WHERE thread_id = ?",
                (session_id,),
            ).fetchone()
            if projection is None or projection[0] != rollout_size:
                return None
            turn = connection.execute(
                "SELECT status, started_at, completed_at "
                "FROM thread_turns WHERE thread_id = ? "
                "ORDER BY rollout_ordinal DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if turn is None:
            return None
        status, started_at, completed_at = turn
        timestamp = completed_at if status != "inProgress" else started_at
        # SQLite stores lifecycle seconds. Put the boundary at the end of that
        # second so it compares conservatively with a Hook's nanosecond receipt
        # timestamp from the same event.
        state_ns = (
            (int(timestamp) + 1) * 1_000_000_000 - 1
            if isinstance(timestamp, int) and not isinstance(timestamp, bool) and timestamp > 0
            else 0
        )
        states = {
            "inProgress": "working", "completed": "ready",
            "interrupted": "ready", "failed": "error",
        }
        state = states.get(status)
        return (state, state_ns, None) if state else None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def _codex_rollout_state(
    path: str, session_id: str | None = None
) -> tuple[str, int, str | None]:
    """Read Codex's latest root-turn boundary, not title/screen heuristics.

    ``task_started``/``turn_started`` is No-Ready/working until the same root
    rollout records a later terminal boundary. A successful completion or
    ``turn_aborted`` leaves the live TUI ready for input; a terminal completion
    carrying Codex's structured error is Error. If no boundary is found inside
    the bounded tail, report unknown rather than inventing a state.
    """
    if session_id is not None:
        projected = _codex_history_state(session_id, path)
        if projected is not None:
            return projected
    try:
        for raw in _reverse_rollout_lines(path):
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict) or value.get("type") != "event_msg":
                continue
            payload = value.get("payload")
            if not isinstance(payload, dict):
                continue
            event_type = payload.get("type")
            if event_type in ("task_started", "turn_started"):
                return "working", _timestamp_ns(value.get("timestamp")), None
            if event_type in ("task_complete", "turn_complete"):
                if payload.get("error") is not None:
                    return "error", _timestamp_ns(value.get("timestamp")), None
                return "ready", _timestamp_ns(value.get("timestamp")), None
            if event_type == "turn_aborted":
                return "ready", _timestamp_ns(value.get("timestamp")), None
        return "unknown", 0, "codex_runtime_boundary_not_found"
    except OSError:
        return "unknown", 0, "codex_rollout_read_failed"


def _codex_direct(pid: int) -> tuple[str | None, int, str, int, str, str | None]:
    """Map a foreground Codex PID to one root rollout and its live state."""
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-Fn", "-p", str(pid)], capture_output=True,
            text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, 0, "unknown", 0, "unavailable", "codex_lsof_failed"
    roots: dict[str, tuple[int, str]] = {}
    session_ids: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("n") or "/.codex/sessions/" not in line or not line.endswith(".jsonl"):
            continue
        root = _read_rollout_root(line[1:])
        if root is not None:
            roots[root[0]] = (root[1], line[1:])
        try:
            with open(line[1:], "rb") as stream:
                raw = stream.readline(512 * 1024)
            meta = json.loads(raw)
            payload = meta.get("payload") if isinstance(meta, dict) else None
            tree_id = payload.get("session_id") if isinstance(payload, dict) else None
            if isinstance(tree_id, str) and UUID_RE.fullmatch(tree_id):
                session_ids.add(tree_id)
        except (OSError, TypeError, ValueError):
            pass
    # Subagent rollout files inherit the root's session_id. Require one unique
    # tree identity and an open canonical root for that exact ID; this remains
    # exact even when the foreground Codex process holds many subagent files.
    if len(session_ids) == 1 and len(roots) == 1:
        sid, (opened_ns, path) = next(iter(roots.items()))
        if sid in session_ids:
            state, state_ns, state_error = _codex_rollout_state(path, sid)
            return sid, opened_ns, state, state_ns, "codex_open_rollout_runtime", state_error
    return (
        None, 0, "unknown", 0, "unavailable",
        "codex_root_ambiguous" if roots else "codex_root_missing",
    )


def _proc_start_ns(pid: int) -> int:
    try:
        raw = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
            text=True, timeout=1, check=False,
        ).stdout.strip()
        value = datetime.strptime(raw, "%a %b %d %H:%M:%S %Y").astimezone()
        return int(value.timestamp() * 1e9)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _claude_direct(pid: int) -> tuple[str | None, int, str, str | None, str]:
    """Read Claude Code's live registry, feature-detected and PID-validated."""
    path = HOME / ".claude" / "sessions" / f"{pid}.json"
    value = _safe_json(path, {})
    if not isinstance(value, dict) or value.get("pid", pid) != pid:
        return None, _proc_start_ns(pid), "unavailable", "claude_registry_missing", "unknown"
    # Claude 2.1.228 validates procStart before accepting a registry row.  Do
    # the same when the field is available; otherwise PID reuse could bind a
    # stale session to a new process.
    registry_proc_start = value.get("procStart")
    if isinstance(registry_proc_start, str) and registry_proc_start:
        try:
            actual = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                text=True, timeout=1, check=False, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            actual = ""
        # Release formats differ (raw ps text vs normalized token), so reject
        # only when both are raw ps-style timestamps and demonstrably differ.
        if " " in registry_proc_start and actual and registry_proc_start != actual:
            return None, 0, "unavailable", "claude_registry_pid_reused", "unknown"
    sid = value.get("sessionId")
    if not isinstance(sid, str) or not sid or len(sid) > 128:
        return None, _proc_start_ns(pid), "unavailable", "claude_registry_invalid", "unknown"
    started = value.get("startedAt")
    opened_ns = _proc_start_ns(pid)
    # Claude Code 2.1.228 writes Date.now() here, so the live format is epoch
    # milliseconds. Keep ISO support for older/future feature-detected rows.
    if isinstance(started, (int, float)) and not isinstance(started, bool) and started > 0:
        opened_ns = int(started * 1_000_000)
    elif isinstance(started, str):
        try:
            opened_ns = int(datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() * 1e9)
        except ValueError:
            pass
    raw_state = str(value.get("status") or "unknown").lower()
    direct_state = {
        "busy": "working", "idle": "ready", "shell": "working",
        "waiting": "action", "blocked": "action",
    }.get(raw_state, "unknown")
    return sid, opened_ns, "claude_live_registry", None, direct_state


def _hook_state(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("v") != 1 or value.get("k") not in ("codex", "claude"):
        return None
    sid = value.get("sid")
    if not isinstance(sid, str) or not sid or len(sid) > 128:
        return None
    opened_ns = value.get("o", 0)
    updated_ns = value.get("u", 0)
    captured_ns = value.get("c", updated_ns)
    sequence = value.get("q", 0)
    state = value.get("s")
    attachment_id = value.get("i")
    if state not in VALID_STATES or not isinstance(attachment_id, str) or not attachment_id or len(attachment_id) > 128:
        return None
    if not isinstance(opened_ns, int) or isinstance(opened_ns, bool) or opened_ns < 0:
        opened_ns = 0
    if not isinstance(updated_ns, int) or isinstance(updated_ns, bool) or updated_ns < 0:
        updated_ns = 0
    if not isinstance(captured_ns, int) or isinstance(captured_ns, bool) or captured_ns < 0:
        captured_ns = updated_ns
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        sequence = 0
    provenance = value.get("p")
    return {
        "kind": value["k"], "session_id": sid,
        # Only an explicit marker written by the bridge proves official-hook
        # origin. A timestamp is data, not provenance.
        "source": "official_hook_user_var" if provenance == "hook" else "runtime_user_var",
        "state": state, "sequence": sequence,
        "event": value.get("e", "") if value.get("e", "") in ("", "complete", "action", "error") else "",
        "opened_ns": opened_ns, "last_event_ns": updated_ns,
        "captured_ns": captured_ns,
        "attachment_id": attachment_id, "provenance": provenance,
    }


def _runtime_envelope(
    kind: str, session_id: str, state: str, state_ns: int,
    opened_ns: int, window_id: int, agent_pid: int, process_start_ns: int,
    captured_ns: int | None = None,
) -> dict[str, Any]:
    token = hashlib.blake2s(
        f"{kind}\0{session_id}\0{window_id}\0{agent_pid}".encode(), digest_size=8
    ).hexdigest()
    return {
        "v": 1, "i": f"{kind}:{session_id[:64]}:{token}"[:128], "k": kind,
        "s": state if state in VALID_STATES else "unknown",
        "q": 0, "e": "", "d": "", "sid": session_id,
        "o": opened_ns, "u": state_ns, "p": "runtime",
        # Capture time is freshness, independent of the rollout event time.
        "c": captured_ns if captured_ns is not None else time.time_ns(), "pid": agent_pid,
        "ps": process_start_ns,
    }


def _recency(opened_ns: int, now_ns: int) -> str:
    if opened_ns <= 0:
        return "unknown"
    age = max(0, now_ns - opened_ns)
    if age < 15 * 60 * 1_000_000_000:
        return "fresh"
    if age < 60 * 60 * 1_000_000_000:
        return "recent"
    if age < 6 * 60 * 60 * 1_000_000_000:
        return "warm"
    return "old"


def _collect() -> dict[str, Any]:
    started_ns = time.time_ns()
    errors: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    candidates = list(_socket_candidates())
    instances = 0
    tabs = 0
    panes = 0
    first_seen = _safe_json(FIRST_SEEN_PATH, {})
    if not isinstance(first_seen, dict):
        first_seen = {}
    runtime_updates: list[dict[str, Any]] = []
    current_first_seen_keys: set[str] = set()
    sockets: dict[str, str] = {}
    pids: dict[str, int | None] = {}
    codex_cache: dict[int, tuple[str | None, int, str, int, str, str | None]] = {}
    claude_cache: dict[int, tuple[str | None, int, str, str | None, str]] = {}
    proc_start_cache: dict[int, int] = {}

    def codex_direct(pid: int) -> tuple[str | None, int, str, int, str, str | None]:
        result = codex_cache.get(pid)
        if result is None:
            result = codex_cache[pid] = _codex_direct(pid)
        return result

    def claude_direct(pid: int) -> tuple[str | None, int, str, str | None, str]:
        result = claude_cache.get(pid)
        if result is None:
            result = claude_cache[pid] = _claude_direct(pid)
        return result

    def proc_start(pid: int) -> int:
        result = proc_start_cache.get(pid)
        if result is None:
            result = proc_start_cache[pid] = _proc_start_ns(pid)
        return result

    if not candidates:
        errors.append({"code": "kitty_socket_unavailable"})

    for kitty in candidates:
        kitty_pid, path = kitty.pid, kitty.socket_path
        sockets[kitty.instance_id] = path
        pids[kitty.instance_id] = kitty_pid
        try:
            response = _remote_request(path, "ls", {}, kitty_pid)
            tree = json.loads(response.get("data", "[]"))
            if not isinstance(tree, list):
                raise ValueError("ls_not_list")
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
            errors.append({"code": "kitty_ls_failed", "kitty_instance": kitty.instance_id, "detail": type(error).__name__})
            sockets.pop(kitty.instance_id, None)
            pids.pop(kitty.instance_id, None)
            continue
        instances += 1
        for os_window in tree:
            if not isinstance(os_window, dict):
                continue
            for tab in os_window.get("tabs", []):
                if not isinstance(tab, dict):
                    continue
                tabs += 1
                for window in tab.get("windows", []):
                    if not isinstance(window, dict):
                        continue
                    panes += 1
                    processes = window.get("foreground_processes")
                    if not isinstance(processes, list):
                        processes = []
                    detected_kind, agent_pid = _agent_kind(processes)
                    user_vars = window.get("user_vars")
                    if not isinstance(user_vars, dict):
                        user_vars = {}
                    hook = _hook_state(user_vars.get(STATUS_KEY))
                    legacy_runtime_in_hook_slot = (
                        hook is not None and hook.get("provenance") != "hook"
                    )
                    if legacy_runtime_in_hook_slot:
                        # Old collector versions wrote runtime evidence into
                        # the Hook slot. It has no official ordering authority.
                        hook = None
                    direct_codex: tuple[str | None, int, str, int, str, str | None] | None = None
                    if detected_kind == "codex" and agent_pid is not None:
                        direct_codex = codex_direct(agent_pid)
                    # Window IDs can be reused and a previous agent kind can
                    # leave a final disconnected envelope behind.  Live
                    # foreground process identity wins over that stale slot.
                    if hook is not None and detected_kind is not None and hook["kind"] != detected_kind:
                        hook = None
                    # Snapshot identity/state is reconciled with the direct
                    # root rollout. The renderer performs its own source
                    # selection from two independent Kitty user variables.
                    if hook is not None and detected_kind == "codex" and direct_codex is not None:
                        direct_sid, _, direct_state, direct_state_ns, _, direct_error = direct_codex
                        if (
                            hook.get("source") != "official_hook_user_var" or
                            direct_sid is None or direct_sid != hook.get("session_id") or
                            (
                                direct_state != hook.get("state") and
                                (
                                    direct_state_ns > hook.get("last_event_ns", 0) or
                                    # Codex Stop runs before task_complete and can
                                    # be blocked by another Stop hook. A rollout
                                    # that still ends at task_started proves the
                                    # pane is not Ready regardless of hook time.
                                    (
                                        hook.get("event") == "complete" and
                                        direct_state == "working"
                                    )
                                )
                            )
                        ):
                            hook = None
                        bootstrap_validation_error = direct_error
                    else:
                        bootstrap_validation_error = None
                    if hook is None and detected_kind is None:
                        continue

                    fallback_error: str | None = None
                    if hook is not None:
                        identity = hook
                        if detected_kind is None:
                            # User vars outlive their process. This inventory is
                            # explicitly about currently attached live agents,
                            # so do not log a dead session forever.
                            continue
                    elif detected_kind == "codex" and agent_pid is not None:
                        assert direct_codex is not None
                        sid, session_created_ns, direct_state, state_ns, source, fallback_error = direct_codex
                        if fallback_error is None and bootstrap_validation_error:
                            fallback_error = bootstrap_validation_error
                        opened_ns = proc_start(agent_pid)
                        identity = {
                            "kind": "codex", "session_id": sid, "source": source,
                            "state": direct_state, "sequence": 0, "opened_ns": opened_ns,
                            "opened_source": "process_start", "session_created_ns": session_created_ns,
                            "last_event_ns": state_ns, "attachment_id": f"codex:p{agent_pid}",
                        }
                    elif detected_kind == "claude" and agent_pid is not None:
                        sid, opened_ns, source, fallback_error, direct_state = claude_direct(agent_pid)
                        identity = {
                            "kind": "claude", "session_id": sid, "source": source,
                            "state": direct_state, "sequence": 0, "opened_ns": opened_ns,
                            "last_event_ns": 0, "attachment_id": f"claude:p{agent_pid}",
                        }
                    else:
                        continue

                    key = f"{kitty.instance_id}:{window.get('id')}:{identity['kind']}:{identity.get('session_id') or agent_pid}"
                    current_first_seen_keys.add(key)
                    observed = first_seen.get(key)
                    if not isinstance(observed, int) or observed <= 0:
                        observed = started_ns
                        first_seen[key] = observed
                    opened_ns = identity.get("opened_ns", 0)
                    if not isinstance(opened_ns, int) or opened_ns <= 0:
                        opened_ns = observed
                        identity["opened_ns"] = opened_ns
                        identity["opened_source"] = "first_observed"
                    else:
                        identity.setdefault("opened_source", identity["source"])
                    identity["first_seen_ns"] = observed
                    identity["recency"] = _recency(opened_ns, started_ns)
                    identity["agent_pid"] = agent_pid
                    tab_title = tab.get("title", "")
                    if not isinstance(tab_title, str):
                        tab_title = ""
                    tab_title = " ".join(tab_title.split())[:256]
                    identity["location"] = {
                        "kitty_instance": kitty.instance_id,
                        "kitty_pid": kitty_pid,
                        "kitty_process_start_ns": kitty.process_start_ns,
                        "os_window_id": os_window.get("id"),
                        "tab_id": tab.get("id"),
                        "tab_title": tab_title,
                        "window_id": window.get("id"),
                        "os_window_active": bool(os_window.get("is_active")),
                        "tab_active": bool(tab.get("is_active")),
                        "window_active": bool(window.get("is_active")),
                        "focused": bool(window.get("is_focused")),
                    }
                    if fallback_error:
                        identity["evidence_gap"] = fallback_error
                    agents.append(identity)
                    # Runtime evidence lives in a separate key, so a delayed
                    # five-minute writer can never overwrite a newer Hook.
                    # Publish only when the current cycle has direct session
                    # evidence. A Hook-only identity is not runtime evidence.
                    direct_sid: str | None = None
                    direct_opened_ns = opened_ns
                    direct_state_value = identity["state"]
                    direct_state_ns = int(identity.get("last_event_ns") or 0)
                    if detected_kind == "codex" and direct_codex is not None:
                        direct_sid = direct_codex[0]
                        direct_opened_ns = proc_start(agent_pid) if agent_pid is not None else opened_ns
                        direct_state_value = direct_codex[2]
                        direct_state_ns = direct_codex[3]
                    elif detected_kind == "claude" and agent_pid is not None:
                        direct_sid, direct_opened_ns, _, direct_error, direct_state_value = claude_direct(agent_pid)
                        if direct_error is not None:
                            direct_sid = None
                        direct_state_ns = started_ns
                    if direct_sid and agent_pid is not None:
                        runtime_updates.append({
                            "kitty_instance": kitty.instance_id,
                            "window_id": window.get("id"),
                            "clear_legacy_hook_slot": legacy_runtime_in_hook_slot,
                            "envelope": _runtime_envelope(
                                detected_kind, direct_sid, direct_state_value,
                                direct_state_ns, direct_opened_ns,
                                int(window.get("id")), agent_pid, proc_start(agent_pid),
                                started_ns,
                            ),
                        })

    # The inventory only needs attachment observations that still exist now.
    # Keeping closed panes for 30 days leaks stale bookkeeping and lets restart
    # collisions inherit an obsolete fallback age.
    cutoff_ns = started_ns - RETENTION_SECONDS * 1_000_000_000
    first_seen = {
        key: value for key, value in first_seen.items()
        if key in current_first_seen_keys and isinstance(value, int) and value >= cutoff_ns
    }
    _atomic_json(FIRST_SEEN_PATH, first_seen)
    finished_ns = time.time_ns()
    counts = {
        "kitty_instances": instances, "tabs": tabs, "windows": panes,
        "agents": len(agents), "codex": sum(a["kind"] == "codex" for a in agents),
        "claude": sum(a["kind"] == "claude" for a in agents),
        "exact_session_ids": sum(bool(a.get("session_id")) for a in agents),
        "unknown_session_ids": sum(not bool(a.get("session_id")) for a in agents),
    }
    return {
        "schema": "kitty-agent-snapshot/v1", "boot_id": BOOT_ID,
        "captured_at_ns": started_ns,
        "captured_at": datetime.fromtimestamp(started_ns / 1e9, timezone.utc).isoformat().replace("+00:00", "Z"),
        "slot": started_ns // (300 * 1_000_000_000), "duration_ms": round((finished_ns - started_ns) / 1e6, 3),
        "complete": not errors, "errors": errors, "counts": counts, "agents": agents,
        "_runtime_updates": runtime_updates, "_sockets": sockets, "_pids": pids,
    }


def _segment_path(captured_ns: int) -> Path:
    date = datetime.fromtimestamp(captured_ns / 1e9).strftime("%Y-%m-%d")
    base = SNAPSHOT_DIR / f"{date}.jsonl"
    if not base.exists() or base.stat().st_size < MAX_SEGMENT_BYTES:
        return base
    index = 2
    while True:
        candidate = SNAPSHOT_DIR / f"{date}.{index:02d}.jsonl"
        if not candidate.exists() or candidate.stat().st_size < MAX_SEGMENT_BYTES:
            return candidate
        index += 1


def _append(snapshot: dict[str, Any]) -> bool:
    cursor = _safe_json(CURSOR_PATH, {})
    slot = snapshot["slot"]
    if isinstance(cursor, dict) and cursor.get("slot") == slot:
        return False
    canonical_agents = sorted([
        {key: agent.get(key) for key in ("kind", "session_id", "state", "recency", "location")}
        for agent in snapshot["agents"]
    ], key=lambda item: (
        str(item.get("kind") or ""), str(item.get("session_id") or ""),
        str((item.get("location") or {}).get("kitty_instance") or ""),
        int((item.get("location") or {}).get("window_id") or 0),
    ))
    digest = hashlib.blake2s(
        json.dumps(canonical_agents, sort_keys=True, separators=(",", ":")).encode(), digest_size=12
    ).hexdigest()
    snapshot["content_hash"] = digest
    snapshot["same_as_previous"] = isinstance(cursor, dict) and cursor.get("content_hash") == digest
    previous_slot = cursor.get("slot") if isinstance(cursor, dict) else None
    snapshot["missed_slots"] = max(0, slot - previous_slot - 1) if isinstance(previous_slot, int) else 0
    path = _segment_path(snapshot["captured_at_ns"])
    # If append completed but the machine stopped before cursor replacement,
    # recognize the durable tail and avoid a duplicate row for this slot.
    if path.exists():
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                end = stream.tell()
                stream.seek(max(0, end - 256 * 1024))
                tail = stream.read().splitlines()
            if tail:
                last = json.loads(tail[-1])
                if isinstance(last, dict) and last.get("slot") == slot:
                    _atomic_json(CURSOR_PATH, {
                        "slot": slot, "content_hash": last.get("content_hash"), "path": str(path),
                    })
                    return False
        except (OSError, TypeError, ValueError):
            pass
    persisted = {key: value for key, value in snapshot.items() if not key.startswith("_")}
    line = json.dumps(persisted, separators=(",", ":"), ensure_ascii=False) + "\n"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        raw = line.encode("utf-8")
        written = 0
        while written < len(raw):
            written += os.write(fd, raw[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    _atomic_json(CURSOR_PATH, {"slot": slot, "content_hash": digest, "path": str(path)})
    return True


def _maintain() -> None:
    now = time.time()
    files: list[Path] = []
    for path in list(SNAPSHOT_DIR.glob("*.jsonl")) + list(SNAPSHOT_DIR.glob("*.jsonl.gz")):
        try:
            if now - path.stat().st_mtime > RETENTION_SECONDS:
                path.unlink()
                continue
            # Closed daily segments become immutable after 24h, so gzip them
            # once.  Never rename the current day's live append target.
            if path.suffix == ".jsonl" and now - path.stat().st_mtime > 86400:
                target = path.with_suffix(path.suffix + ".gz")
                temp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
                with path.open("rb") as source, gzip.open(temp, "wb", compresslevel=6) as sink:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        sink.write(block)
                os.chmod(temp, 0o600)
                os.replace(temp, target)
                path.unlink()
                path = target
            files.append(path)
        except OSError:
            pass
    total = sum(path.stat().st_size for path in files if path.exists())
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        if total <= MAX_TOTAL_BYTES:
            break
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except OSError:
            pass
    for path in (STDERR_PATH, PRIVATE_ROOT / "launchd.stdout.log", PRIVATE_ROOT / "launchd.stderr.log"):
        try:
            if path.stat().st_size > 1024 * 1024:
                with path.open("r+b") as stream:
                    stream.seek(-256 * 1024, os.SEEK_END)
                    tail = stream.read()
                    newline = tail.find(b"\n")
                    if newline >= 0:
                        tail = tail[newline + 1 :]
                    stream.seek(0)
                    stream.write(tail)
                    stream.truncate()
        except (FileNotFoundError, OSError):
            pass


def _publish_ui_updates(snapshot: dict[str, Any]) -> None:
    """Batch runtime fallback plus redraw tick into one connection per Kitty."""
    by_instance: dict[str, dict[int, list[str]]] = {}
    sockets = snapshot.get("_sockets", {})
    if not isinstance(sockets, dict):
        sockets = {}
    pids = snapshot.get("_pids", {})
    if not isinstance(pids, dict):
        pids = {}
    runtime_locations = {
        (item.get("kitty_instance"), item.get("window_id"))
        for item in snapshot.get("_runtime_updates", [])
        if isinstance(item, dict)
    }
    for agent in snapshot.get("agents", []):
        # Only young attachments can cross a visible age bucket. Old sessions
        # no longer need a five-minute invalidation forever.
        if agent.get("recency") not in ("fresh", "recent", "warm"):
            continue
        location = agent.get("location", {})
        instance_id = location.get("kitty_instance")
        window_id = location.get("window_id")
        # A runtime envelope below already invalidates this row's renderer.
        if (instance_id, window_id) in runtime_locations:
            continue
        if isinstance(instance_id, str) and isinstance(sockets.get(instance_id), str) and isinstance(window_id, int):
            by_instance.setdefault(instance_id, {}).setdefault(window_id, []).append(
                f"agent_snapshot_tick={snapshot['slot']}"
            )
    for item in snapshot.get("_runtime_updates", []):
        instance_id = item.get("kitty_instance")
        window_id = item.get("window_id")
        envelope = item.get("envelope")
        if (
            not isinstance(instance_id, str) or
            not isinstance(sockets.get(instance_id), str) or
            not isinstance(window_id, int) or not isinstance(envelope, dict)
        ):
            continue
        encoded = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
        variables = by_instance.setdefault(instance_id, {}).setdefault(window_id, [])
        variables.insert(0, f"{RUNTIME_STATUS_KEY}={encoded}")
        # Version 0.1 pre-release builds placed runtime envelopes in the Hook
        # slot. Clear only those explicitly marked values during migration;
        # a real Hook in the same slot is never touched.
        if item.get("clear_legacy_hook_slot") is True:
            variables.append(STATUS_KEY)
    for instance_id, updates in by_instance.items():
        try:
            socket_path = sockets[instance_id]
            expected_pid = pids.get(instance_id)
            if not isinstance(expected_pid, int) or expected_pid <= 0:
                expected_pid = None
            _remote_set_vars(socket_path, updates, expected_pid)
        except (OSError, RuntimeError):
            pass


def _cleanup_bridge_cache(snapshot: dict[str, Any]) -> None:
    """Remove stale v0/test state without touching live generation records."""
    if sys.platform == "darwin":
        state_dir = HOME / "Library" / "Caches" / "kitty-agent-status"
    else:
        state_dir = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "kitty-agent-status"
    live_kitty_pids = {
        location.get("kitty_pid")
        for agent in snapshot.get("agents", [])
        for location in (agent.get("location", {}),)
        if isinstance(location.get("kitty_pid"), int)
    }
    live = {
        (location.get("kitty_pid"), location.get("window_id"), agent.get("kind"))
        for agent in snapshot.get("agents", [])
        for location in (agent.get("location", {}),)
    }
    now = time.time()
    try:
        paths = tuple(state_dir.glob("*.json"))
    except OSError:
        return
    generation = re.compile(r"^(codex|claude)-p(\d+)-w(\d+)\.json$")
    legacy = re.compile(r"^(codex|claude)-(\d+)\.json$")
    for path in paths:
        if path.name in ("seen-v1.json", "seen-v2.json"):
            continue
        try:
            match = generation.fullmatch(path.name)
            remove = False
            if match:
                kind, pid, window = match.group(1), int(match.group(2)), int(match.group(3))
                # PID+window generation keys make non-live rows unambiguous;
                # remove immediately only while that same Kitty generation is
                # still inventoried. For a dead generation, wait a day in case
                # another live instance had a partial snapshot.
                remove = (pid, window, kind) not in live and (
                    pid in live_kitty_pids or now - path.stat().st_mtime > 86400
                )
            elif legacy.fullmatch(path.name):
                remove = now - path.stat().st_mtime > 3600
        except OSError:
            continue
        if remove:
            try:
                path.unlink()
            except OSError:
                pass


def main() -> int:
    # This one-shot can also be invoked manually outside launchd. Set the
    # privacy boundary before creating any lock, cursor, JSONL, gzip temp or
    # diagnostic file, rather than relying only on the LaunchAgent Umask.
    os.umask(0o077)
    _mkdirs()
    snapshot: dict[str, Any] | None = None
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        os.chmod(LOCK_PATH, 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if "--print" not in sys.argv:
                return 0
            # A diagnostic caller should never print an empty/non-JSON stream.
            # Wait briefly for the scheduled writer, then take the lock.
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            except OSError:
                return 1
        started = time.monotonic()
        try:
            snapshot = _collect()
            if time.monotonic() - started > 30:
                _write_error("collector_deadline_exceeded")
            _append(snapshot)
            # Snapshot slot de-duplication is a storage concern only. A manual
            # reconciliation or restarted collector in the same five-minute
            # slot must still repair stale Kitty user variables.
            _publish_ui_updates(snapshot)
            if snapshot.get("complete") is True:
                _cleanup_bridge_cache(snapshot)
            _maintain()
        except Exception as error:
            _write_error("collector_exception", error=type(error).__name__, detail=str(error)[:160])
            return 1
    if "--print" in sys.argv and snapshot is not None:
        public = {key: value for key, value in snapshot.items() if not key.startswith("_")}
        print(json.dumps(public, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
