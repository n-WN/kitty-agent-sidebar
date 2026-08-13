#!/usr/bin/env python3
"""Install, inspect, or remove Kitty Agent Sidebar without flattening HOME."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LABEL = "io.github.kitty-agent-sidebar.snapshot"
ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
BIN_DIR = HOME / ".local" / "bin"
KITTY_DIR = Path(os.environ.get("KITTY_CONFIG_DIRECTORY", HOME / ".config" / "kitty"))
SOCKET_DIR = HOME / ".local" / "state" / "kitty-agent-status"
CODEX_HOOKS = HOME / ".codex" / "hooks.json"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
PLIST = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
PRIVATE_ROOT = HOME / "Library" / "Application Support" / "kitty-agent-status.noindex"
INSTALL_ROOT = PRIVATE_ROOT / "install"
MANIFEST = INSTALL_ROOT / "manifest.json"
INSTALL_LOCK = INSTALL_ROOT / "install.lock"
BACKUP_ROOT = INSTALL_ROOT / "backups"
BRIDGE = BIN_DIR / "kitty-agent-status"
SNAPSHOT = BIN_DIR / "kitty-agent-snapshot"

MANAGED_SOURCES = {
    BRIDGE: ROOT / "src" / "kitty_agent_sidebar" / "hook_bridge.py",
    SNAPSHOT: ROOT / "src" / "kitty_agent_sidebar" / "snapshot.py",
    KITTY_DIR / "tab_bar.py": ROOT / "kitty" / "tab_bar.py",
    KITTY_DIR / "agent_status_watcher.py": ROOT / "kitty" / "agent_status_watcher.py",
    KITTY_DIR / "sidebar.conf": ROOT / "kitty" / "sidebar.conf",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_bytes(path: Path, raw: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temp, flags, mode)
    try:
        os.fchmod(fd, mode)
        written = 0
        while written < len(raw):
            written += os.write(fd, raw[written:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(
        path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(), 0o600
    )


def ensure_private_dir(path: Path) -> None:
    """Create a same-user directory without accepting a symlink target."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise OSError(f"unsafe private directory: {path}")
    os.chmod(path, 0o700)


def private_dir_ok(path: Path) -> bool:
    try:
        info = path.lstat()
        return (
            stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and
            info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
        )
    except OSError:
        return False


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"top-level JSON object required: {path}")
    return value


@contextmanager
def rollback_files(paths: list[Path]):
    """Restore configuration files if a multi-file Hook merge fails."""
    originals = {
        path: (path.read_bytes() if path.exists() else None)
        for path in paths
    }
    try:
        yield
    except Exception:
        for path, raw in originals.items():
            if raw is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                atomic_bytes(path, raw, 0o600)
        raise


def command_tokens(hook: Any) -> list[str]:
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
        return []
    try:
        return shlex.split(hook["command"]) + [str(item) for item in hook.get("args", [])]
    except (TypeError, ValueError):
        return []


def hook_owned(hook: Any, bridge_paths: set[str] | None = None) -> bool:
    tokens = command_tokens(hook)
    paths = bridge_paths or {str(BRIDGE)}
    return (
        len(tokens) >= 4 and tokens[0] in paths and
        tokens[1] == "managed-v1" and tokens[2] in ("codex", "claude")
    ) or (
        # Upgrade/uninstall compatibility for local v0 entries.
        len(tokens) >= 3 and tokens[0] in paths and tokens[1] in ("codex", "claude")
    )


def merge_hooks(path: Path, template: Path, bridge_paths: set[str] | None = None) -> None:
    current = load_json(path)
    rendered = template.read_text(encoding="utf-8").replace("@BRIDGE@", str(BRIDGE))
    desired = json.loads(rendered)["hooks"]
    hooks = current.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"hooks must be an object: {path}")
    for event, groups in desired.items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            existing = []
        kept = []
        for group in existing:
            handlers = group.get("hooks", []) if isinstance(group, dict) else []
            if any(hook_owned(item, bridge_paths) for item in handlers):
                continue
            kept.append(group)
        hooks[event] = kept + groups
    atomic_json(path, current)


def remove_hooks(path: Path, bridge_paths: set[str], remove_empty_root: bool = False) -> None:
    if not path.exists():
        return
    current = load_json(path)
    hooks = current.get("hooks")
    if not isinstance(hooks, dict):
        return
    changed = False
    for event in tuple(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            handlers = group.get("hooks", []) if isinstance(group, dict) else []
            if any(hook_owned(item, bridge_paths) for item in handlers):
                changed = True
                continue
            kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            if groups:
                changed = True
            hooks.pop(event, None)
    if remove_empty_root and not hooks:
        current.pop("hooks", None)
        changed = True
    if changed:
        atomic_json(path, current)


def load_manifest() -> dict[str, Any]:
    value = load_json(MANIFEST)
    if value.get("schema") != 1:
        return {"schema": 1, "files": {}, "bridge_paths": [], "hook_roots": {}}
    value.setdefault("files", {})
    value.setdefault("bridge_paths", [])
    value.setdefault("hook_roots", {})
    return value


def backup_file(path: Path) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    token = hashlib.blake2s(str(path).encode(), digest_size=8).hexdigest()
    target = BACKUP_ROOT / stamp / f"{token}-{path.name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_bytes(target, path.read_bytes(), 0o600)
    return str(target)


def find_managed_conflicts(manifest: dict[str, Any]) -> list[str]:
    """Return paths changed since the previous install without writing."""
    conflicts: list[str] = []
    records = manifest["files"]
    for target in (*MANAGED_SOURCES, PLIST):
        record = records.get(str(target), {})
        expected = record.get("installed_sha256") if isinstance(record, dict) else None
        if target.exists() and expected and sha256(target) != expected:
            conflicts.append(str(target))
    return conflicts


def install_managed_files(manifest: dict[str, Any], force: bool) -> None:
    records = manifest["files"]
    for target, source in MANAGED_SOURCES.items():
        record = records.get(str(target), {})
        if target.exists():
            current = sha256(target)
            if not record:
                record = {"original_backup": backup_file(target)}
            elif current != record.get("installed_sha256") and force:
                record["forced_user_backup"] = backup_file(target)
        else:
            record.setdefault("original_backup", None)
        mode = 0o700 if target in (BRIDGE, SNAPSHOT) else 0o600
        atomic_bytes(target, source.read_bytes(), mode)
        record["installed_sha256"] = sha256(target)
        record["mode"] = mode
        records[str(target)] = record


def render_plist() -> bytes:
    template = ROOT / "launchd" / "com.example.kitty-agent-sidebar.snapshot.plist.in"
    raw = template.read_text(encoding="utf-8")
    raw = raw.replace("@SNAPSHOT@", str(SNAPSHOT)).replace("@HOME@", str(HOME)).replace("@PRIVATE_ROOT@", str(PRIVATE_ROOT))
    return plistlib.dumps(plistlib.loads(raw.encode("utf-8")), sort_keys=False)


def install_plist(manifest: dict[str, Any], force: bool) -> None:
    record = manifest["files"].get(str(PLIST), {})
    if PLIST.exists() and record and sha256(PLIST) != record.get("installed_sha256") and not force:
        raise RuntimeError(f"managed file changed after install; rerun with --force after review: {PLIST}")
    if PLIST.exists() and not record:
        record = {"original_backup": backup_file(PLIST)}
    elif not PLIST.exists():
        record.setdefault("original_backup", None)
    elif force and sha256(PLIST) != record.get("installed_sha256"):
        record["forced_user_backup"] = backup_file(PLIST)
    atomic_bytes(PLIST, render_plist(), 0o600)
    record.update(installed_sha256=sha256(PLIST), mode=0o600)
    manifest["files"][str(PLIST)] = record


def launchctl(action: str) -> None:
    if sys.platform != "darwin":
        return
    uid = os.getuid()
    domain = f"gui/{uid}"
    if action == "load":
        subprocess.run(["/bin/launchctl", "bootout", domain, str(PLIST)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["/bin/launchctl", "bootstrap", domain, str(PLIST)], check=True)
    else:
        subprocess.run(["/bin/launchctl", "bootout", domain, str(PLIST)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install(force: bool = False) -> None:
    ensure_private_dir(INSTALL_ROOT)
    # Kitty binds this filesystem socket after config reload. bind(2) does not
    # create its parent, and socket-only RC is unconditional, so keep the
    # parent private before Kitty starts listening.
    ensure_private_dir(SOCKET_DIR)
    manifest = load_manifest()
    conflicts = find_managed_conflicts(manifest)
    if conflicts and not force:
        raise RuntimeError(
            "managed files changed after install; rerun with --force after review:\n  "
            + "\n  ".join(conflicts)
        )
    old_paths = {str(item) for item in manifest.get("bridge_paths", []) if isinstance(item, str)}
    old_paths.add(str(BRIDGE))
    # Hook JSON and the manifest form one logical install. Roll them back if a
    # later merge fails. Managed binaries remain safe because their previous
    # versions are retained in the existing manifest backup records.
    with rollback_files([CODEX_HOOKS, CLAUDE_SETTINGS, MANIFEST]):
        install_managed_files(manifest, force)
        hook_roots = manifest.setdefault("hook_roots", {})
        for path in (CODEX_HOOKS, CLAUDE_SETTINGS):
            if str(path) not in hook_roots:
                hook_roots[str(path)] = {"hooks_present": "hooks" in load_json(path)}
        merge_hooks(CODEX_HOOKS, ROOT / "hooks" / "codex-hooks.template.json", old_paths)
        merge_hooks(CLAUDE_SETTINGS, ROOT / "hooks" / "claude-hooks.template.json", old_paths)
        install_plist(manifest, force)
        manifest["bridge_paths"] = sorted(old_paths | {str(BRIDGE)})
        manifest["installed_at_ns"] = time.time_ns()
        atomic_json(MANIFEST, manifest)
    launchctl("load")
    print("installed; add 'include sidebar.conf' to kitty.conf, then reload Kitty config")


def uninstall() -> None:
    manifest = load_manifest()
    paths = {str(item) for item in manifest.get("bridge_paths", []) if isinstance(item, str)}
    paths.add(str(BRIDGE))
    hook_roots = manifest.get("hook_roots", {})
    for config in (CODEX_HOOKS, CLAUDE_SETTINGS):
        baseline = hook_roots.get(str(config), {}) if isinstance(hook_roots, dict) else {}
        remove_hooks(
            config, paths,
            remove_empty_root=isinstance(baseline, dict) and baseline.get("hooks_present") is False,
        )
    launchctl("unload")
    preserved: list[str] = []
    for raw_path, record in manifest.get("files", {}).items():
        path = Path(raw_path)
        expected = record.get("installed_sha256") if isinstance(record, dict) else None
        if path.exists() and expected and sha256(path) != expected:
            preserved.append(str(path))
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        backup = record.get("original_backup") if isinstance(record, dict) else None
        if isinstance(backup, str) and Path(backup).is_file():
            mode = int(record.get("mode", 0o600))
            atomic_bytes(path, Path(backup).read_bytes(), mode)
    try:
        MANIFEST.unlink()
    except FileNotFoundError:
        pass
    print("uninstalled; private snapshots were retained")
    if preserved:
        print("preserved user-modified files:\n  " + "\n  ".join(preserved))


def doctor() -> int:
    manifest = load_manifest()
    checks = {
        "python": sys.version_info >= (3, 9),
        "bridge": BRIDGE.is_file(), "snapshot": SNAPSHOT.is_file(),
        "tab_bar": (KITTY_DIR / "tab_bar.py").is_file(),
        "watcher": (KITTY_DIR / "agent_status_watcher.py").is_file(),
        "codex_hooks": CODEX_HOOKS.is_file(), "claude_hooks": CLAUDE_SETTINGS.is_file(),
        "launch_agent": sys.platform != "darwin" or PLIST.is_file(),
        "socket_directory": private_dir_ok(SOCKET_DIR),
        "manifest": MANIFEST.is_file() and manifest.get("schema") == 1,
    }
    for name, ok in checks.items():
        print(f"{'OK' if ok else 'MISS':4} {name}")
    return 0 if all(checks.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("install", "uninstall", "doctor"))
    parser.add_argument("--force", action="store_true", help="back up and replace user-modified managed files")
    args = parser.parse_args()
    ensure_private_dir(INSTALL_ROOT)
    with INSTALL_LOCK.open("a+", encoding="utf-8") as lock:
        os.chmod(INSTALL_LOCK, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.command == "install":
            install(args.force); return 0
        if args.command == "uninstall":
            uninstall(); return 0
        return doctor()


if __name__ == "__main__":
    raise SystemExit(main())
