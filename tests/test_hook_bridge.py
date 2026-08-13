from __future__ import annotations

import base64
import importlib.util
import json
import os
import pathlib
import pty
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


bridge = load("hook_bridge_test", ROOT / "src/kitty_agent_sidebar/hook_bridge.py")


class HookBridgeTests(unittest.TestCase):
    def test_event_table(self):
        self.assertEqual(bridge._event_transition("codex", "session-start", {"source": "startup"})[0], "working")
        self.assertEqual(bridge._event_transition("claude", "session-start", {"source": "startup"})[0], "ready")
        self.assertEqual(bridge._event_transition("codex", "session-start", {"source": "compact"})[0], "")
        self.assertEqual(bridge._event_transition("codex", "permission-request", {"tool_name": "Bash"})[:2], ("action", "action"))
        self.assertEqual(bridge._event_transition("codex", "stop", {})[:2], ("ready", "complete"))

    def test_managed_marker_is_accepted(self):
        with mock.patch.object(bridge, "_read_input", return_value={}), mock.patch.object(
            bridge, "_update", return_value=0
        ) as update, mock.patch.object(sys, "argv", [
            "kitty-agent-status", "managed-v1", "codex", "stop"
        ]):
            self.assertEqual(bridge.main(), 0)
        update.assert_called_once_with("codex", "stop", {})

    def test_osc_user_var_round_trip(self):
        raw = bridge._osc_user_var("agent_status_v1", '{"v":1,"text":"汉字"}')
        self.assertTrue(raw.startswith(b"\x1b]1337;SetUserVar=agent_status_v1="))
        self.assertTrue(raw.endswith(b"\x1b\\"))
        payload = raw.split(b"=", 2)[2][:-2]
        self.assertEqual(base64.b64decode(payload).decode(), '{"v":1,"text":"汉字"}')

    def test_sequence_is_serialized_and_duplicate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            old = bridge.STATE_DIR
            bridge.STATE_DIR = pathlib.Path(td)
            published = []
            payload = {"session_id": "s-1", "turn_id": "t-1"}
            env = {"KITTY_PID": "123", "KITTY_WINDOW_ID": "7"}
            try:
                with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
                    bridge, "_publish", side_effect=lambda value: published.append(value) or True
                ):
                    bridge._update("codex", "user-prompt-submit", payload)
                    bridge._update("codex", "stop", payload)
                    bridge._update("codex", "stop", payload)
                self.assertEqual([item["q"] for item in published], [0, 1])
                state = json.loads(next(pathlib.Path(td).glob("codex-*.json")).read_text())
                self.assertEqual(state["sequence"], 1)
                self.assertEqual(state["envelope"]["s"], "ready")
                self.assertGreater(state["envelope"]["c"], 0)
            finally:
                bridge.STATE_DIR = old

    def test_state_file_is_private(self):
        with tempfile.TemporaryDirectory() as td:
            old = bridge.STATE_DIR
            bridge.STATE_DIR = pathlib.Path(td)
            with mock.patch.dict(os.environ, {"KITTY_PID": "123"}, clear=False):
                with bridge._open_state("codex", 9) as stream:
                    stream.write("{}")
            path = next(pathlib.Path(td).glob("*.json"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            bridge.STATE_DIR = old


if __name__ == "__main__":
    unittest.main()
