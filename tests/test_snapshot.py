from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


snap = load("snapshot_test", ROOT / "src/kitty_agent_sidebar/snapshot.py")


class SnapshotTests(unittest.TestCase):
    def test_minimum_rc_version(self):
        self.assertEqual(snap.RC_CLIENT_VERSION, (0, 30, 0))
        self.assertEqual(snap.RUNTIME_STATUS_KEY, "agent_runtime_v1")

    def test_root_rollout_rejects_subagent(self):
        root = {"type": "session_meta", "payload": {
            "id": "12345678-1234-1234-1234-123456789abc",
            "session_id": "12345678-1234-1234-1234-123456789abc",
            "originator": "codex-tui", "thread_source": "user",
        }}
        child = {"type": "session_meta", "payload": {
            "id": "12345678-1234-1234-1234-123456789abd",
            "session_id": "12345678-1234-1234-1234-123456789abc",
            "originator": "codex-tui", "thread_source": "subagent",
            "parent_thread_id": "parent",
        }}
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "rollout.jsonl"
            p.write_text(json.dumps(root) + "\n")
            self.assertEqual(snap._read_rollout_root(str(p))[0], root["payload"]["id"])
            p.write_text(json.dumps(child) + "\n")
            self.assertIsNone(snap._read_rollout_root(str(p)))

    def test_codex_runtime_boundaries(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "rollout.jsonl"
            boundaries = (
                ("task_started", "working"), ("task_complete", "ready"),
                ("turn_aborted", "ready"),
            )
            for event, expected in boundaries:
                p.write_text(json.dumps({
                    "timestamp": "2026-08-14T00:00:00Z", "type": "event_msg",
                    "payload": {"type": event, "error": None},
                }) + "\n")
                self.assertEqual(snap._codex_rollout_state(str(p))[0], expected)

    def test_codex_history_state_requires_projection_at_rollout_end(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            rollout = root / "rollout.jsonl"
            rollout.write_bytes(b"line one\n")
            database = root / "thread_history.sqlite"
            import sqlite3
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE thread_history_projection_state (
                        thread_id TEXT PRIMARY KEY,
                        next_rollout_byte_offset INTEGER NOT NULL,
                        next_rollout_ordinal INTEGER NOT NULL
                    );
                    CREATE TABLE thread_turns (
                        thread_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        rollout_ordinal INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        started_at INTEGER,
                        completed_at INTEGER
                    );
                """)
                connection.execute(
                    "INSERT INTO thread_history_projection_state VALUES (?, ?, ?)",
                    ("session", rollout.stat().st_size, 2),
                )
                connection.execute(
                    "INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?)",
                    ("session", "turn", 1, "completed", 10, 20),
                )
            with mock.patch.object(snap, "CODEX_HISTORY_DB", database):
                self.assertEqual(
                    snap._codex_history_state("session", str(rollout)),
                    ("ready", 20_999_999_999, None),
                )
                rollout.write_bytes(b"line one\nnew line\n")
                self.assertIsNone(snap._codex_history_state("session", str(rollout)))

    def test_codex_history_state_maps_live_and_failed_turns(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            rollout = root / "rollout.jsonl"
            rollout.write_bytes(b"x\n")
            database = root / "thread_history.sqlite"
            import sqlite3
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE thread_history_projection_state (
                        thread_id TEXT PRIMARY KEY,
                        next_rollout_byte_offset INTEGER NOT NULL,
                        next_rollout_ordinal INTEGER NOT NULL
                    );
                    CREATE TABLE thread_turns (
                        thread_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        rollout_ordinal INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        started_at INTEGER,
                        completed_at INTEGER
                    );
                """)
                connection.execute(
                    "INSERT INTO thread_history_projection_state VALUES (?, ?, ?)",
                    ("session", rollout.stat().st_size, 2),
                )
                connection.execute(
                    "INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?)",
                    ("session", "turn", 1, "inProgress", 11, None),
                )
            with mock.patch.object(snap, "CODEX_HISTORY_DB", database):
                self.assertEqual(
                    snap._codex_history_state("session", str(rollout)),
                    ("working", 11_999_999_999, None),
                )
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "UPDATE thread_turns SET status = 'failed', completed_at = 12"
                    )
                self.assertEqual(
                    snap._codex_history_state("session", str(rollout)),
                    ("error", 12_999_999_999, None),
                )

    def test_append_is_durable_and_deduplicates_slot(self):
        with tempfile.TemporaryDirectory() as td:
            old = (snap.SNAPSHOT_DIR, snap.CURSOR_PATH)
            snap.SNAPSHOT_DIR = pathlib.Path(td) / "snapshots"
            snap.SNAPSHOT_DIR.mkdir()
            snap.CURSOR_PATH = pathlib.Path(td) / "cursor.json"
            data = {"slot": 42, "captured_at_ns": 1, "agents": []}
            try:
                self.assertTrue(snap._append(dict(data)))
                self.assertFalse(snap._append(dict(data)))
                lines = list(snap.SNAPSHOT_DIR.glob("*.jsonl"))[0].read_text().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertEqual(json.loads(lines[0])["slot"], 42)
            finally:
                snap.SNAPSHOT_DIR, snap.CURSOR_PATH = old

    def test_tab_title_is_bounded(self):
        title = "x" * 1000
        self.assertEqual(len(" ".join(title.split())[:256]), 256)

    def test_runtime_envelope_has_separate_provenance_and_no_unread(self):
        value = snap._runtime_envelope("codex", "session", "working", 5, 4, 3, 2, 1)
        self.assertEqual(value["p"], "runtime")
        self.assertEqual(value["q"], 0)
        self.assertEqual(value["ps"], 1)

    def test_hook_state_keeps_local_receipt_time(self):
        raw = json.dumps({
            "v": 1, "i": "a", "k": "codex", "s": "working", "q": 0,
            "sid": "session", "o": 1, "u": 2, "c": 3, "p": "hook",
        })
        self.assertEqual(snap._hook_state(raw)["captured_ns"], 3)

    def test_publish_clears_only_legacy_runtime_hook_slot(self):
        seen = {}
        snapshot = {
            "slot": 1, "agents": [], "_sockets": {"k": "/tmp/k"},
            "_pids": {"k": 1}, "_runtime_updates": [{
                "kitty_instance": "k", "window_id": 7,
                "envelope": snap._runtime_envelope("codex", "s", "ready", 1, 1, 7, 2, 1),
                "clear_legacy_hook_slot": True,
            }],
        }
        with mock.patch.object(
            snap, "_remote_set_vars", side_effect=lambda _p, value, _pid: seen.update(value)
        ):
            snap._publish_ui_updates(snapshot)
        self.assertTrue(seen[7][0].startswith("agent_runtime_v1="))
        self.assertIn("agent_status_v1", seen[7])

    def test_runtime_update_does_not_duplicate_redraw_tick(self):
        seen = {}
        snapshot = {
            "slot": 1,
            "agents": [{"recency": "fresh", "location": {"kitty_instance": "k", "window_id": 7}}],
            "_sockets": {"k": "/tmp/k"}, "_pids": {"k": 1},
            "_runtime_updates": [{
                "kitty_instance": "k", "window_id": 7,
                "envelope": snap._runtime_envelope("codex", "s", "ready", 1, 1, 7, 2, 1),
            }],
        }
        with mock.patch.object(
            snap, "_remote_set_vars", side_effect=lambda _p, value, _pid: seen.update(value)
        ):
            snap._publish_ui_updates(snapshot)
        self.assertEqual(len(seen[7]), 1)
        self.assertTrue(seen[7][0].startswith("agent_runtime_v1="))

    def test_socket_discovery_does_not_issue_duplicate_ls(self):
        fake_info = types.SimpleNamespace(st_uid=os.getuid(), st_mode=stat.S_IFSOCK, st_dev=1, st_ino=2)
        peer = mock.MagicMock()
        peer.getsockopt.return_value = (123).to_bytes(4, sys.byteorder, signed=True)
        with mock.patch.object(snap, "_kitty_gui_pids", return_value={123}), mock.patch.object(
            snap, "_lsof_unix_paths", return_value=["/tmp/kitty-test.sock"]
        ), mock.patch.object(snap.os, "lstat", return_value=fake_info), mock.patch.object(
            snap.socket, "socket", return_value=peer
        ), mock.patch.object(snap, "_proc_start_ns", return_value=9), mock.patch.object(
            snap, "_remote_request"
        ) as request:
            items = list(snap._socket_candidates())
        self.assertEqual(len(items), 1)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
