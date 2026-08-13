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


if __name__ == "__main__":
    unittest.main()
