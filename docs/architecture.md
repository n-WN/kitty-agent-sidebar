# Architecture

## Live path

1. Codex CLI or Claude Code invokes an official command Hook.
2. `kitty-agent-status` reads at most 2 MiB of Hook JSON from stdin.
3. It validates `KITTY_PID`, `KITTY_WINDOW_ID`, agent kind, and Session ID.
4. It serializes the per-pane state with a private `flock` record.
5. It writes one OSC 1337 `SetUserVar` escape to `/dev/tty`.
6. Kitty updates the current pane only. The watcher marks that tab bar dirty.
7. `tab_bar.py` validates the envelope and renders fixed-width cells.

The Hook path does not require remote control, a socket path, or a Kitty version tuple.
It does not read or change the terminal or tab title.

## Five-minute path

The LaunchAgent starts `kitty-agent-snapshot` every 300 seconds. It is not resident.
It finds all Kitty GUI processes owned by the user. It gets their filesystem Unix sockets
from `lsof`, connects, verifies `LOCAL_PEERPID`, and sends one `ls` request per instance.

Evidence priority is:

1. Official-Hook user variable.
2. Direct live runtime evidence.
3. Unknown. The collector does not guess.

For a legacy Codex pane, direct evidence is the foreground native Codex PID and the
canonical root rollout file that this PID has open. For Claude Code, direct evidence is a
PID-validated live registry when that release provides one.

## Identity

A pane attachment has a random attachment ID. It is not the Session ID. A resumed Session
can have a new attachment, and one Session can be visible in more than one pane.

A Kitty instance key contains:

- OS boot ID, when available;
- GUI PID;
- GUI process start time;
- socket device number;
- socket inode.

Snapshots also contain OS-window, tab, pane IDs, and the bounded manual tab label. Titles
are recovery labels only. They are never identity or status channels.

## State and unread

The official Hook envelope is one atomic JSON value in `agent_status_v1`:

```json
{"v":1,"i":"attachment","k":"codex","s":"working","q":4,"e":"","d":"","sid":"session","o":0,"u":0,"c":0,"p":"hook"}
```

The collector writes direct fallback evidence to the separate `agent_runtime_v1` key.
It never writes the Hook key. For one Session, the Hook always wins. If a pane is reused
and the directly verified live Session differs, runtime wins until that Session emits its
first Hook. Runtime sequence is always zero and cannot create unread state.

`q` increases only for meaningful official-Hook notification events. `unread` is `q > seen_q`.
The focus watcher stores `seen_q` in a second pane-local Kitty user variable. Kitty's
`Window.is_focused` value is true for the active pane of every tab, so it is not a selected-tab
test. The watcher and renderer use `Channel.ui_state(window.id).is_visible`; the renderer also
checks active-tab identity. When a selected pane is visible, the renderer can make one
idempotent pane-local `SetUserVar` write for each new sequence. Rendering has no filesystem
I/O, network I/O, or locks.

Hook envelopes contain both `u` (the optional agent event timestamp) and `c` (the local bridge
receipt timestamp). Cross-source ordering uses `c`, which is in the same clock domain as the
collector. Direct runtime evidence has a 10-minute TTL. For the same Session and state, the
Hook stays authoritative and retains unread semantics. If a later direct observation proves a
different state for the same Session, runtime repairs the stale Hook. If both direct evidence
and the Hook receipt are older than the TTL, the renderer hides the state. This prevents a
SIGKILL, OOM, or power loss from leaving a permanent `working` state when `SessionEnd` cannot
run.

## Optional Kitty patch

Stock Kitty 0.48 or later provides vertical tabs. The patch adds only:

- `vertical_tab_bar_width`;
- an opt-in drag handle that is entirely inside the sidebar;
- persistence of the selected width.

The session/status integration does not require this patch.

The macOS `>` menu separator is kept in a second optional patch. It is deliberately not
mixed into the resize patch.
