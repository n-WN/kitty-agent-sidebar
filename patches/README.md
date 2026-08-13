# Optional Kitty patches

Both patches were generated and checked against Kitty commit:

```text
e95da80fdbbf317917a106c8e1bcf8032c875c80
```

This commit was 35 commits after the local `nightly` tag on 2026-08-10. It is not a promise
that every Kitty 0.48.x release has identical context.

Check applicability before changing a source tree:

```sh
git -C /path/to/kitty apply --check /path/to/kitty-agent-sidebar/patches/kitty-0.48-vertical-sidebar-resize.patch
git -C /path/to/kitty apply --check /path/to/kitty-agent-sidebar/patches/kitty-macos-menubar-chevron.patch
```

If `apply --check` fails, do not force the patch. Rebase it against the target source and run
Kitty's option-generation tests and a macOS build before installation.

The resize patch and menu-separator patch are independent. The status, unread, snapshot, and
Hook integration works without either patch.
