#!/usr/bin/env python3
"""Bridge official coding-agent lifecycle events into a Kitty user variable.

The bridge is deliberately event-driven: Codex/Claude Code starts it only for
an official hook (or Codex's legacy ``notify`` callback), it publishes one
atomic JSON envelope to the originating Kitty window, then exits.  It never
reads or changes the terminal title and it has no resident polling process.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUS_KEY = "agent_status_v1"
VALID_KINDS = frozenset(("codex", "claude"))
VALID_STATES = frozenset(("working", "ready", "action", "error", "disconnected", "unknown"))
if sys.platform == "darwin":
    STATE_DIR = Path.home() / "Library" / "Caches" / "kitty-agent-status"
else:
    STATE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "kitty-agent-status"
LOG_PATH = STATE_DIR / "events.jsonl"
MAX_LOG_BYTES = 512 * 1024
MAX_DETAIL = 80


def _private_open(path: Path, flags: int, text_mode: str) -> Any:
    """Open a private regular file without following attacker-controlled links."""
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("unsafe state file")
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, text_mode, encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def _log(level: str, message: str, **fields: Any) -> None:
    """Best-effort bounded diagnostics; never contaminate hook stdout/stderr."""
    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)
        with _private_open(
            STATE_DIR / ".events.lock", os.O_RDWR | os.O_CREAT, "r+"
        ) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
                rotated = LOG_PATH.with_suffix(".jsonl.1")
                try:
                    rotated.unlink()
                except FileNotFoundError:
                    pass
                LOG_PATH.replace(rotated)
            record = {"ts": time.time_ns(), "level": level, "message": message}
            record.update(fields)
            with _private_open(
                LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, "a"
            ) as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _clean_text(value: Any, limit: int = MAX_DETAIL) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _read_input(explicit_event: str, args: list[str] | None = None) -> dict[str, Any]:
    if explicit_event == "notify":
        args = sys.argv[1:] if args is None else args
        if len(args) < 3:
            return {}
        try:
            value = json.loads(args[-1])
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if not raw or len(raw) > 2 * 1024 * 1024:
            return {}
        value = json.loads(raw)
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _window_id() -> int | None:
    raw = os.environ.get("KITTY_WINDOW_ID", "")
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _inside_kitty() -> bool:
    """Require Kitty's inherited pane identity, not a generic terminal name."""
    raw = os.environ.get("KITTY_PID", "")
    return _window_id() is not None and raw.isdecimal() and int(raw) > 0


def _kitty_identity() -> str:
    """Return the Kitty GUI PID inherited by this pane."""
    raw = os.environ.get("KITTY_PID", "")
    return f"p{int(raw)}" if raw.isdecimal() and int(raw) > 0 else "unknown"


def _state_path(kind: str, window_id: int) -> Path:
    return STATE_DIR / f"{kind}-{_kitty_identity()}-w{window_id}.json"


def _open_state(kind: str, window_id: int) -> Any:
    return _private_open(_state_path(kind, window_id), os.O_RDWR | os.O_CREAT, "r+")


def _official_baseline(previous: dict[str, Any], kind: str, session_id: str) -> dict[str, Any]:
    """Preserve attachment identity from runtime seeds, never their ordering.

    Only hook-provenance state participates in hook-to-hook ordering. A stale
    cache from another session keeps no ordering authority.
    """
    envelope = previous.get("envelope")
    if (
        previous.get("session") == session_id and isinstance(envelope, dict) and
        envelope.get("k") == kind and envelope.get("p") == "hook"
    ):
        return previous
    return {
        "session": previous.get("session"),
        "instance": previous.get("instance"),
        "sequence": previous.get("sequence", 0),
        "event_key": previous.get("event_key", ""),
        "envelope": envelope if isinstance(envelope, dict) else {},
        "opened_ns": previous.get("opened_ns", 0),
        "updated_ns": 0,
    }


def _event_time_ns(payload: dict[str, Any]) -> int:
    """Normalize optional official timestamps; arrival time is the fallback."""
    for key in ("timestamp_ns", "event_timestamp_ns"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    for key in ("triggered_at", "timestamp", "event_timestamp"):
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
            delta = parsed.astimezone(timezone.utc) - epoch
            return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
        except ValueError:
            pass
    return time.time_ns()


def _same_envelope(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in ("v", "i", "k", "s", "q", "e", "d", "sid", "o", "p"))


def _new_instance(kind: str, window_id: int, session_id: str) -> str:
    seed = f"{kind}\0{window_id}\0{session_id}\0{time.time_ns()}\0{os.getpid()}"
    token = hashlib.blake2s(seed.encode(), digest_size=8).hexdigest()
    return f"{kind}:{session_id[:64]}:{token}"[:128]


def _load_state(stream: Any) -> dict[str, Any]:
    try:
        stream.seek(0)
        value = json.load(stream)
        if isinstance(value, dict):
            return value
    except (OSError, TypeError, ValueError):
        pass
    return {}


def _write_state(stream: Any, value: dict[str, Any]) -> None:
    stream.seek(0)
    stream.truncate()
    json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
    stream.flush()
    os.fsync(stream.fileno())


def _event_transition(
    kind: str, explicit_event: str, payload: dict[str, Any]
) -> tuple[str, str, str, bool]:
    hook_name = _clean_text(payload.get("hook_event_name"), 64)
    event = explicit_event.lower().replace("_", "-")
    if event in ("auto", "hook"):
        event = hook_name.lower().replace("_", "-")

    if event == "sessionstart" or event == "session-start":
        source = _clean_text(payload.get("source"), 32).lower()
        if source in ("compact", "compaction"):
            # Compaction happens inside an existing turn. Re-emitting Ready
            # here would erase a valid Working state before that turn ends.
            return "", "", "", False
        # Codex dispatches SessionStart from inside an already-started first
        # turn, after task_started has been written.  Publishing Ready here
        # creates a false one-frame idle flash before UserPromptSubmit. Claude
        # dispatches SessionStart while the process is idle.
        return ("working", "", "", False) if kind == "codex" else ("ready", "", "", False)
    if event in (
        "userpromptsubmit", "user-prompt-submit", "pretooluse", "pre-tool-use",
        "posttooluse", "post-tool-use",
    ):
        return "working", "", "", False
    if event in ("permissionrequest", "permission-request"):
        tool = _clean_text(payload.get("tool_name"), 48)
        return "action", "action", f"Needs approval{': ' + tool if tool else ''}", True
    if event == "stop":
        # StopCommandInput has no background_tasks field. Codex runs this hook
        # before it emits task_complete and another Stop hook can still request
        # continuation. The collector verifies the final rollout boundary; this
        # hook remains the low-latency hint for the common accepted-stop path.
        return "ready", "complete", "Complete", True
    if event == "notify":
        if payload.get("type") != "agent-turn-complete":
            return "", "", "", False
        return "ready", "complete", "Complete", True
    if event in ("stopfailure", "stop-failure"):
        error = _clean_text(payload.get("error"), 48) or "API error"
        return "error", "error", error.replace("_", " "), True
    if event in ("sessionend", "session-end"):
        reason = _clean_text(payload.get("reason"), 48)
        if reason in ("clear", "resume"):
            # Both agents immediately emit a new SessionStart for these
            # in-process session switches; suppress a distracting one-frame
            # disconnected flash.
            return "", "", "", False
        return "disconnected", "", "", False
    return "", "", "", False


def _osc_user_var(key: str, value: str) -> bytes:
    """Encode one Kitty OSC 1337 SetUserVar update for the current pane."""
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"\x1b]1337;SetUserVar={key}={encoded}\x1b\\".encode("ascii")


def _publish(envelope: dict[str, Any]) -> bool:
    """Publish through the pane TTY; no RC socket or Kitty version is needed."""
    if not _inside_kitty():
        return False
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    wire = _osc_user_var(STATUS_KEY, encoded)
    try:
        flags = os.O_WRONLY | getattr(os, "O_NOCTTY", 0)
        fd = os.open("/dev/tty", flags)
        try:
            written = 0
            while written < len(wire):
                written += os.write(fd, wire[written:])
        finally:
            os.close(fd)
        return True
    except OSError as error:
        _log("error", "kitty tty publish failed", error=type(error).__name__)
        return False


def _update(kind: str, explicit_event: str, payload: dict[str, Any]) -> int:
    window_id = _window_id()
    if window_id is None or not _inside_kitty():
        return 0
    state, semantic, detail, increment = _event_transition(kind, explicit_event, payload)
    if not state:
        return 0
    session_id = _clean_text(
        payload.get("session_id") or payload.get("thread-id") or payload.get("thread_id"),
        96,
    )
    if not session_id:
        # A pane ID is a location, never a session identity.  Official hooks
        # normally provide session_id/thread-id; dropping a malformed event is
        # safer than publishing a durable but false mapping.
        _log("warning", "event missing session identity", kind=kind, event=explicit_event, window=window_id)
        return 0

    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    with _open_state(kind, window_id) as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        previous = _load_state(stream)
        previous = _official_baseline(previous, kind, session_id)
        previous_updated_ns = previous.get("updated_ns", 0)
        incoming_event_ns = _event_time_ns(payload)
        # Claude's hook runner can execute callbacks asynchronously on older
        # installs.  A late callback must not roll the pane back from a newer
        # semantic state.  Official payload timestamps are preferred when
        # present; otherwise acquisition order is the deterministic fallback.
        if (
            kind == "claude" and isinstance(previous_updated_ns, int) and
            incoming_event_ns < previous_updated_ns
        ):
            return 0
        previous_session = previous.get("session")
        normalized_explicit = explicit_event.lower().replace("_", "-")
        hook_name = _clean_text(payload.get("hook_event_name"), 64).lower().replace("_", "-")
        is_session_start = normalized_explicit in ("sessionstart", "session-start") or (
            normalized_explicit in ("auto", "hook") and hook_name in ("sessionstart", "session-start")
        )
        source = _clean_text(payload.get("source"), 32).lower()
        # Compaction re-emits SessionStart for the same attached pane.  It must
        # not make an old session look freshly opened; real startup/resume or a
        # new session ID is a new attachment and resets the age marker.
        # SessionStart can repeat for compaction. A new session ID always creates
        # a new attachment. Resume/startup creates one only when the previous
        # cached record is already disconnected; duplicate starts are idempotent.
        previous_state = (previous.get("envelope") or {}).get("s") if isinstance(previous.get("envelope"), dict) else None
        new_attachment = previous_session != session_id or (
            is_session_start and source in ("startup", "resume", "clear") and
            previous_state == "disconnected"
        )
        now_ns = time.time_ns()
        if new_attachment:
            instance = _new_instance(kind, window_id, session_id)
            sequence = 0
            last_event_key = ""
            opened_ns = now_ns
        else:
            instance = str(previous.get("instance") or _new_instance(kind, window_id, session_id))[:128]
            sequence = previous.get("sequence", 0)
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                sequence = 0
            last_event_key = str(previous.get("event_key") or "")
            opened_ns = previous.get("opened_ns", now_ns)
            if not isinstance(opened_ns, int) or opened_ns <= 0:
                opened_ns = now_ns

        turn_id = _clean_text(payload.get("turn_id") or payload.get("turn-id"), 96)
        event_key = f"{semantic}:{turn_id}" if semantic and turn_id else semantic
        # Codex's official Stop hook and legacy notify callback describe the
        # same accepted turn boundary.  Canonicalize both to one unread event.
        if kind == "codex" and semantic == "complete" and turn_id:
            event_key = f"complete:{turn_id}"
        if increment and event_key and event_key != last_event_key:
            sequence += 1
            last_event_key = event_key

        envelope = {
            "v": 1,
            "i": instance,
            "k": kind,
            "s": state,
            "q": sequence,
            "e": semantic,
            "d": detail,
            # Extra v1 keys are intentionally optional/backwards compatible.
            # They make snapshots exact without parsing or overwriting titles.
            "sid": session_id,
            "o": opened_ns,
            "u": incoming_event_ns,
            # Local receipt time is in the same clock domain as the periodic
            # collector's capture time. Keep it separate from the optional
            # producer timestamp in ``u`` so cross-source arbitration is not
            # affected by an SSH host's clock.
            "c": now_ns,
            # Provenance is explicit.  A non-zero timestamp alone cannot prove
            # that a value came from an official agent hook (a former manual
            # verifier demonstrated why that distinction matters).
            "p": "hook",
        }
        previous_envelope = previous.get("envelope")
        same_envelope = (
            isinstance(previous_envelope, dict) and not new_attachment and
            _same_envelope(previous_envelope, envelope)
        )
        if same_envelope and previous.get("published", True):
            # A duplicate Stop/notify or repeated Ready event does not need a
            # TTY write or a state-file rewrite. Preserve the original
            # update time so ordering remains meaningful.
            return 0
        record = {
            "session": session_id,
            "instance": instance,
            "sequence": sequence,
            "event_key": last_event_key,
            "envelope": envelope,
            "opened_ns": opened_ns,
            "updated_ns": incoming_event_ns,
            # A failed TTY write must remain retryable. Older records have no
            # flag and are treated as successfully published for compatibility.
            "published": False,
        }
        _write_state(stream, record)
        # Keep the generation/window lock through publication.  Otherwise two
        # concurrent callbacks can write A then B but publish B then A, rolling
        # the visible pane back even though the durable record is newer.
        published = _publish(envelope)
        if published:
            record["published"] = True
            _write_state(stream, record)

    if published:
        _log("info", "published", kind=kind, event=explicit_event, window=window_id, state=state, sequence=sequence)
        return 0
    return 0  # status telemetry must never change agent behavior


def _fanout_delegate(args: list[str]) -> None:
    """Preserve the notification command that was configured before us."""
    if not args:
        return
    try:
        os.execv(args[0], args)
    except OSError as error:
        _log("error", "notify delegate failed", error=str(error), command=args[0])


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "managed-v1":
        args = args[1:]
    if len(args) < 2 or args[0] not in VALID_KINDS:
        return 64
    kind, event = args[0], args[1]
    payload = _read_input(event, args)
    try:
        _update(kind, event, payload)
    except Exception as error:
        # Status telemetry must never change agent behavior or make a hook fail.
        _log("error", "unhandled bridge error", error=type(error).__name__)
    if event == "notify":
        # argv: KIND notify DELEGATE [FIXED_ARGS...] PAYLOAD
        # The pre-existing delegate must receive the official payload too; only
        # our own KIND/notify prefix is removed.
        _fanout_delegate(args[2:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
