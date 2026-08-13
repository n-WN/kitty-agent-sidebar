from __future__ import annotations

import json
import pathlib
import runpy
import sys
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_tab_bar():
    boss = types.ModuleType("kitty.boss")
    boss.get_boss = lambda: types.SimpleNamespace(window_id_map={}, tab_for_id=lambda _id: None)
    fdt = types.ModuleType("kitty.fast_data_types")
    fdt.get_options = lambda: types.SimpleNamespace(
        tab_title_max_lines=1,
        tab_bar_background=None,
        background=types.SimpleNamespace(is_dark=False),
    )
    notifications = types.ModuleType("kitty.notifications")
    notifications.Channel = lambda: types.SimpleNamespace(
        ui_state=lambda window_id: types.SimpleNamespace(is_visible=window_id == 1)
    )
    modules = {
        "kitty.boss": boss, "kitty.fast_data_types": fdt,
        "kitty.notifications": notifications,
    }
    with mock.patch.dict(sys.modules, modules):
        return runpy.run_path(str(ROOT / "kitty/tab_bar.py"))


class FakeColor:
    def __getattr__(self, name):
        return f"<{name}>"


class FakeFmt:
    def __init__(self):
        self.fg = FakeColor()


def load_watcher():
    notifications = types.ModuleType("kitty.notifications")
    notifications.Channel = lambda: types.SimpleNamespace(
        ui_state=lambda window_id: types.SimpleNamespace(is_visible=window_id == 1)
    )
    with mock.patch.dict(sys.modules, {"kitty.notifications": notifications}):
        return runpy.run_path(str(ROOT / "kitty/agent_status_watcher.py"))


class FakeWindow:
    def __init__(self, status: str):
        self.id = 1
        self.user_vars = {"agent_status_v1": status}
        self.is_focused = True
        self.dirty = 0
        self.set_calls = 0

    def set_user_var(self, key, value):
        self.set_calls += 1
        self.user_vars[key] = value

    def tabref(self):
        return types.SimpleNamespace(mark_tab_bar_dirty=lambda: setattr(self, "dirty", self.dirty + 1))


class TabBarTests(unittest.TestCase):
    def setUp(self):
        now = time.time_ns()
        self.raw = json.dumps({"v": 1, "i": "a", "k": "codex", "s": "ready", "q": 2, "sid": "s", "o": 1, "u": now, "c": now, "p": "hook"})

    def test_decode_validation(self):
        m = load_tab_bar()
        self.assertEqual(m["_decode"](self.raw).sequence, 2)
        self.assertIsNone(m["_decode"](json.dumps({"v": 1, "i": "a", "k": "other", "s": "ready", "q": 2})))

    def test_focus_watcher_persists_seen_only_for_visible_pane(self):
        m = load_watcher(); window = FakeWindow(self.raw)
        m["on_focus_change"](None, window, {"focused": True})
        self.assertEqual(json.loads(window.user_vars["agent_seen_v1"]), {"v": 1, "i": "a", "q": 2})
        self.assertEqual(window.dirty, 1)

    def test_stale_focus_callback_does_not_mark_hidden_pane_seen(self):
        m = load_watcher(); window = FakeWindow(self.raw); window.id = 2
        m["on_focus_change"](None, window, {"focused": True})
        self.assertNotIn("agent_seen_v1", window.user_vars)

    def test_tab_selection_marks_visible_window_seen(self):
        m = load_watcher(); window = FakeWindow(self.raw)
        m["on_tab_bar_dirty"](None, window, {})
        self.assertEqual(json.loads(window.user_vars["agent_seen_v1"]), {"v": 1, "i": "a", "q": 2})

    def test_hidden_tab_is_not_marked_seen(self):
        m = load_watcher(); window = FakeWindow(self.raw); window.id = 2
        m["on_tab_bar_dirty"](None, window, {})
        self.assertNotIn("agent_seen_v1", window.user_vars)

    def test_unread_is_attachment_scoped(self):
        m = load_tab_bar(); state = m["_decode"](self.raw); window = FakeWindow(self.raw)
        self.assertEqual(m["_seen_sequence"](window, state), 0)
        window.user_vars["agent_seen_v1"] = json.dumps({"v": 1, "i": "other", "q": 99})
        self.assertEqual(m["_seen_sequence"](window, state), 0)
        window.user_vars["agent_seen_v1"] = json.dumps({"v": 1, "i": "a", "q": 2})
        self.assertEqual(m["_seen_sequence"](window, state), 2)

    def test_newer_direct_state_repairs_same_session_hook(self):
        m = load_tab_bar(); window = FakeWindow(self.raw)
        window.user_vars["agent_runtime_v1"] = json.dumps({
            "v": 1, "i": "r", "k": "codex", "s": "working", "q": 0,
            "sid": "s", "o": 1, "u": 999, "p": "runtime", "c": time.time_ns(),
        })
        self.assertEqual(m["_selected_state"](window).provenance, "runtime")
        self.assertEqual(m["_selected_state"](window).state, "working")

    def test_live_runtime_replaces_stale_other_session(self):
        m = load_tab_bar(); window = FakeWindow(self.raw)
        window.user_vars["agent_runtime_v1"] = json.dumps({
            "v": 1, "i": "r", "k": "codex", "s": "working", "q": 0,
            "sid": "new-session", "o": 1, "u": 3, "p": "runtime", "c": time.time_ns(),
        })
        self.assertEqual(m["_selected_state"](window).session_id, "new-session")

    def test_runtime_cannot_create_unread(self):
        m = load_tab_bar(); window = FakeWindow("")
        window.user_vars["agent_runtime_v1"] = json.dumps({
            "v": 1, "i": "r", "k": "codex", "s": "ready", "q": 99,
            "sid": "s", "o": 1, "u": 3, "p": "runtime", "c": time.time_ns(),
        })
        state = m["_selected_state"](window)
        self.assertEqual(state.provenance, "runtime")
        self.assertFalse(state.provenance == "hook" and state.sequence > m["_seen_sequence"](window, state))

    def test_new_hook_beats_previous_session_runtime(self):
        m = load_tab_bar(); now = time.time_ns()
        hook = json.dumps({"v": 1, "i": "h", "k": "codex", "s": "working", "q": 0,
                           "sid": "new", "o": now, "u": now, "c": now, "p": "hook"})
        window = FakeWindow(hook)
        window.user_vars["agent_runtime_v1"] = json.dumps({
            "v": 1, "i": "r", "k": "codex", "s": "ready", "q": 0,
            "sid": "old", "o": 1, "u": 1, "c": now - 1, "p": "runtime",
        })
        self.assertEqual(m["_selected_state"](window).session_id, "new")

    def test_stale_hook_without_live_runtime_is_hidden(self):
        m = load_tab_bar(); old = time.time_ns() - m["HOOK_UNVERIFIED_MAX_AGE_NS"] - 1
        window = FakeWindow(json.dumps({
            "v": 1, "i": "h", "k": "codex", "s": "working", "q": 1,
            "sid": "dead", "o": 1, "u": old, "c": old, "p": "hook",
        }))
        self.assertIsNone(m["_selected_state"](window))

    def test_fresh_hook_without_runtime_is_kept(self):
        m = load_tab_bar(); now = time.time_ns()
        window = FakeWindow(json.dumps({
            "v": 1, "i": "h", "k": "codex", "s": "working", "q": 1,
            "sid": "live", "o": 1, "u": now, "c": now, "p": "hook",
        }))
        self.assertEqual(m["_selected_state"](window).state, "working")

    def test_new_direct_state_repairs_old_same_session_hook(self):
        m = load_tab_bar(); old = time.time_ns() - m["HOOK_UNVERIFIED_MAX_AGE_NS"] - 1
        window = FakeWindow(json.dumps({
            "v": 1, "i": "h", "k": "codex", "s": "ready", "q": 1,
            "sid": "live", "o": 1, "u": old, "c": old, "p": "hook",
        }))
        window.user_vars["agent_runtime_v1"] = json.dumps({
            "v": 1, "i": "r", "k": "codex", "s": "working", "q": 0,
            "sid": "live", "o": 1, "u": 1, "c": time.time_ns(), "p": "runtime",
        })
        self.assertEqual(m["_selected_state"](window).state, "working")

    def test_same_state_direct_proof_keeps_hook_unread_semantics(self):
        m = load_tab_bar(); old = time.time_ns() - m["HOOK_UNVERIFIED_MAX_AGE_NS"] - 1
        window = FakeWindow(json.dumps({
            "v": 1, "i": "h", "k": "codex", "s": "ready", "q": 4,
            "sid": "live", "o": 1, "u": old, "c": old, "p": "hook",
        }))
        window.user_vars["agent_runtime_v1"] = json.dumps({
            "v": 1, "i": "r", "k": "codex", "s": "ready", "q": 0,
            "sid": "live", "o": 1, "u": 1, "c": time.time_ns(), "p": "runtime",
        })
        selected = m["_selected_state"](window)
        self.assertEqual(selected.provenance, "hook")
        self.assertEqual(selected.sequence, 4)

    def test_local_receipt_time_orders_hook_against_runtime(self):
        m = load_tab_bar(); now = time.time_ns()
        window = FakeWindow(json.dumps({
            "v": 1, "i": "h", "k": "codex", "s": "working", "q": 0,
            "sid": "new", "o": 1, "u": 1, "c": now, "p": "hook",
        }))
        window.user_vars["agent_runtime_v1"] = json.dumps({
            "v": 1, "i": "r", "k": "codex", "s": "ready", "q": 0,
            "sid": "old", "o": 1, "u": now, "c": now - 1, "p": "runtime",
        })
        self.assertEqual(m["_selected_state"](window).session_id, "new")

    def test_expired_runtime_is_hidden(self):
        m = load_tab_bar(); window = FakeWindow("")
        window.user_vars["agent_runtime_v1"] = json.dumps({
            "v": 1, "i": "r", "k": "codex", "s": "ready", "q": 0,
            "sid": "old", "o": 1, "u": 1,
            "c": time.time_ns() - m["RUNTIME_MAX_AGE_NS"] - 1, "p": "runtime",
        })
        self.assertIsNone(m["_selected_state"](window))

    def test_rendering_selected_tab_marks_hook_seen_once(self):
        m = load_tab_bar(); window = FakeWindow(self.raw)
        tab = types.SimpleNamespace()
        manager = types.SimpleNamespace(active_tab=tab)
        tab.tab_manager_ref = lambda: manager
        window.tabref = lambda: tab
        state = m["_decode"](self.raw)
        m["_mark_seen_if_visible"](window, state)
        self.assertEqual(json.loads(window.user_vars["agent_seen_v1"]), {"v": 1, "i": "a", "q": 2})
        m["_mark_seen_if_visible"](window, state)
        self.assertEqual(window.set_calls, 1)

    def test_rendering_hidden_tab_does_not_mark_seen(self):
        m = load_tab_bar(); window = FakeWindow(self.raw); window.id = 2
        tab = types.SimpleNamespace()
        manager = types.SimpleNamespace(active_tab=tab)
        tab.tab_manager_ref = lambda: manager
        window.tabref = lambda: tab
        m["_mark_seen_if_visible"](window, m["_decode"](self.raw))
        self.assertNotIn("agent_seen_v1", window.user_vars)

    def test_active_marker_and_unread_marker_are_independent(self):
        m = load_tab_bar()
        base = {"tab_id": 7, "title": "T", "fmt": FakeFmt(), "bell_symbol": ""}
        with mock.patch.dict(m["draw_title"].__globals__, {
            "_states_for_tab": lambda _id: [], "_is_active_tab": lambda _id: True,
        }):
            self.assertTrue(m["draw_title"](base).startswith("<_d97757>▎"))
        state = m["_decode"](self.raw)
        item = (types.SimpleNamespace(is_active=True), state, True)
        with mock.patch.dict(m["draw_title"].__globals__, {
            "_states_for_tab": lambda _id: [item], "_is_active_tab": lambda _id: True,
        }):
            self.assertTrue(m["draw_title"](base).startswith("<_a34b34>•"))


if __name__ == "__main__":
    unittest.main()
