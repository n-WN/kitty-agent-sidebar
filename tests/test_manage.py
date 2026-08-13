from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


manage = load("manage_test", ROOT / "scripts/manage.py")


class ManageTests(unittest.TestCase):
    def sandbox_install(self, root: pathlib.Path):
        home = root / "home"
        bin_dir = home / ".local" / "bin"
        kitty_dir = home / ".config" / "kitty"
        socket_dir = home / ".local" / "state" / "kitty-agent-status"
        private = home / "private"
        install_root = private / "install"
        codex = home / ".codex" / "hooks.json"
        claude = home / ".claude" / "settings.json"
        plist = home / "Library" / "LaunchAgents" / f"{manage.LABEL}.plist"
        bridge = bin_dir / "kitty-agent-status"
        snapshot = bin_dir / "kitty-agent-snapshot"
        sources = {
            bridge: ROOT / "src/kitty_agent_sidebar/hook_bridge.py",
            snapshot: ROOT / "src/kitty_agent_sidebar/snapshot.py",
            kitty_dir / "tab_bar.py": ROOT / "kitty/tab_bar.py",
            kitty_dir / "agent_status_watcher.py": ROOT / "kitty/agent_status_watcher.py",
            kitty_dir / "sidebar.conf": ROOT / "kitty/sidebar.conf",
        }
        patches = {
            "HOME": home, "BIN_DIR": bin_dir, "KITTY_DIR": kitty_dir,
            "SOCKET_DIR": socket_dir,
            "CODEX_HOOKS": codex, "CLAUDE_SETTINGS": claude,
            "PLIST": plist, "PRIVATE_ROOT": private,
            "INSTALL_ROOT": install_root, "MANIFEST": install_root / "manifest.json",
            "INSTALL_LOCK": install_root / "install.lock",
            "BACKUP_ROOT": install_root / "backups", "BRIDGE": bridge,
            "SNAPSHOT": snapshot, "MANAGED_SOURCES": sources,
        }
        return patches

    def test_hook_ownership_is_exact_and_supports_v0_upgrade(self):
        path = str(manage.BRIDGE)
        self.assertTrue(manage.hook_owned({"command": f"{path} managed-v1 codex stop"}))
        self.assertTrue(manage.hook_owned({"command": path, "args": ["managed-v1", "claude", "stop"]}))
        self.assertTrue(manage.hook_owned({"command": f"{path} codex stop"}))
        self.assertFalse(manage.hook_owned({"command": f"/tmp/{manage.BRIDGE.name} codex stop"}))
        self.assertFalse(manage.hook_owned({"command": path, "args": ["other", "stop"]}))

    def test_uninstall_restores_original_but_preserves_later_user_edit(self):
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            target = td / "tab_bar.py"
            original = td / "original"
            original.write_text("original")
            target.write_text("installed")
            manifest = {
                "schema": 1,
                "bridge_paths": [],
                "files": {str(target): {
                    "original_backup": str(original),
                    "installed_sha256": manage.sha256(target),
                    "mode": 0o600,
                }},
            }
            with mock.patch.object(manage, "load_manifest", return_value=manifest), mock.patch.object(
                manage, "remove_hooks"
            ), mock.patch.object(manage, "launchctl"), mock.patch.object(
                manage, "MANIFEST", td / "manifest.json"
            ):
                with redirect_stdout(StringIO()):
                    manage.uninstall()
            self.assertEqual(target.read_text(), "original")

            target.write_text("user edit")
            original.write_text("original")
            manifest["files"][str(target)]["installed_sha256"] = "not-current"
            with mock.patch.object(manage, "load_manifest", return_value=manifest), mock.patch.object(
                manage, "remove_hooks"
            ), mock.patch.object(manage, "launchctl"), mock.patch.object(
                manage, "MANIFEST", td / "manifest.json"
            ):
                with redirect_stdout(StringIO()):
                    manage.uninstall()
            self.assertEqual(target.read_text(), "user edit")

    def test_merge_preserves_unrelated_hook(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "hooks.json"
            template = pathlib.Path(td) / "template.json"
            path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/bin/echo keep"}]}]}}))
            template.write_text(json.dumps({
                "hooks": {"Stop": [{"hooks": [{
                    "type": "command", "command": "@BRIDGE@ managed-v1 codex stop",
                }]}]},
            }))
            manage.merge_hooks(path, template)
            groups = json.loads(path.read_text())["hooks"]["Stop"]
            commands = [h["command"] for g in groups for h in g["hooks"]]
            self.assertIn("/bin/echo keep", commands)
            self.assertIn(f"{manage.BRIDGE} managed-v1 codex stop", commands)

    def test_hook_transaction_restores_files_after_failure(self):
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            one, two = td / "one.json", td / "two.json"
            one.write_text("one")
            with self.assertRaises(RuntimeError):
                with manage.rollback_files([one, two]):
                    one.write_text("changed")
                    two.write_text("created")
                    raise RuntimeError("fail")
            self.assertEqual(one.read_text(), "one")
            self.assertFalse(two.exists())

    def test_full_install_doctor_uninstall_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            values = self.sandbox_install(td)
            codex = values["CODEX_HOOKS"]
            claude = values["CLAUDE_SETTINGS"]
            tab_bar = values["KITTY_DIR"] / "tab_bar.py"
            codex.parent.mkdir(parents=True)
            claude.parent.mkdir(parents=True)
            tab_bar.parent.mkdir(parents=True)
            original_tab_bar = b"# user tab bar\n"
            tab_bar.write_bytes(original_tab_bar)
            codex.write_text(json.dumps({
                "hooks": {"Stop": [{"hooks": [{
                    "type": "command", "command": "/bin/echo keep-codex",
                }]}]},
            }))
            claude.write_text(json.dumps({"env": {"KEEP": "1"}}))
            with mock.patch.multiple(manage, **values), mock.patch.object(
                manage, "launchctl"
            ), redirect_stdout(StringIO()):
                manage.install()
                self.assertEqual(manage.doctor(), 0)
                self.assertTrue(manage.MANIFEST.is_file())
                self.assertEqual(values["SOCKET_DIR"].stat().st_mode & 0o777, 0o700)
                groups = json.loads(codex.read_text())["hooks"]["Stop"]
                commands = [item["command"] for group in groups for item in group["hooks"]]
                self.assertIn("/bin/echo keep-codex", commands)
                self.assertTrue(any("managed-v1 codex stop" in item for item in commands))
                self.assertEqual(json.loads(claude.read_text())["env"], {"KEEP": "1"})
                manage.uninstall()
            self.assertEqual(tab_bar.read_bytes(), original_tab_bar)
            self.assertFalse(values["BRIDGE"].exists())
            self.assertFalse(values["SNAPSHOT"].exists())
            groups = json.loads(codex.read_text())["hooks"]["Stop"]
            commands = [item["command"] for group in groups for item in group["hooks"]]
            self.assertEqual(commands, ["/bin/echo keep-codex"])
            self.assertEqual(json.loads(claude.read_text()), {"env": {"KEEP": "1"}})

    def test_private_socket_directory_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            target = td / "target"
            target.mkdir()
            link = td / "socket-dir"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(OSError):
                manage.ensure_private_dir(link)


if __name__ == "__main__":
    unittest.main()
