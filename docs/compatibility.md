# Compatibility

| Component | Minimum | Notes |
|---|---:|---|
| OSC `SetUserVar` Hook transport | Kitty 0.29 | Current pane; no remote control |
| Collector `ls` user variables | Kitty 0.30 | Collector sends RC client version 0.30 |
| Vertical sidebar | Kitty 0.48 | `tab_bar_edge left` or `right` |
| Optional drag-resize patch | Kitty 0.48.x source | Rebase and test for later Kitty source |
| Python | 3.9 | macOS system Python is supported |
| macOS | 13+ target | launchd, `lsof`, and `LOCAL_PEERPID` are used |

The macOS 13 floor is a target, not a full release matrix. The initial live validation used
macOS 26.5.1, system Python 3.9.6, and Kitty 0.48.2. CI validates Python 3.9 and a current
Python release on a hosted macOS runner. Reports from other supported macOS releases are
welcome.

The live Hook and renderer are not tied to one Kitty process. Each Kitty pane inherits its
own `KITTY_PID` and `KITTY_WINDOW_ID`; OSC targets that pane. The collector enumerates all
owned Kitty GUI instances.

Linux can use the Hook and renderer. The current scheduled collector package is macOS-first;
a systemd timer and Linux peer-credential adapter are not included in version 0.1.
