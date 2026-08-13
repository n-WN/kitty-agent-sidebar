"""Redraw tabs and advance unread state on real Kitty focus events."""
from __future__ import annotations

import json
from typing import Any

from kitty.notifications import Channel

AGENT_STATUS_KEY = 'agent_status_v1'
AGENT_RUNTIME_KEY = 'agent_runtime_v1'
AGENT_SEEN_KEY = 'agent_seen_v1'
SNAPSHOT_TICK_KEY = 'agent_snapshot_tick'
_visibility = Channel()


def _mark_seen(window: Any) -> None:
    try:
        value = json.loads(window.user_vars.get(AGENT_STATUS_KEY, ''))
        if not isinstance(value, dict) or value.get('p') != 'hook':
            return
        attachment = value.get('i')
        sequence = value.get('q')
        if not isinstance(attachment, str) or not attachment or len(attachment) > 128:
            return
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            return
        seen = json.dumps({'v': 1, 'i': attachment, 'q': sequence}, separators=(',', ':'))
        if window.user_vars.get(AGENT_SEEN_KEY) != seen:
            window.set_user_var(AGENT_SEEN_KEY, seen)
    except (TypeError, ValueError):
        pass


def on_set_user_var(boss: Any, window: Any, data: dict[str, Any]) -> None:
    key = data.get('key')
    if key not in (AGENT_STATUS_KEY, AGENT_RUNTIME_KEY, AGENT_SEEN_KEY, SNAPSHOT_TICK_KEY):
        return
    # Channel visibility includes the selected tab, layout visibility, and
    # focused Kitty OS window. Window.is_focused does not provide that test.
    if key == AGENT_STATUS_KEY and _visibility.ui_state(window.id).is_visible:
        _mark_seen(window)
    tab = window.tabref()
    if tab is not None:
        tab.mark_tab_bar_dirty()


def on_focus_change(boss: Any, window: Any, data: dict[str, Any]) -> None:
    if data.get('focused') is True and _visibility.ui_state(window.id).is_visible:
        _mark_seen(window)
    tab = window.tabref()
    if tab is not None:
        tab.mark_tab_bar_dirty()


def on_tab_bar_dirty(boss: Any, window: Any, data: dict[str, Any]) -> None:
    """Mark the selected visible pane read when the user clicks its tab.

    Kitty's `Window.is_focused` remains true for every tab's active pane in the
    current implementation, so it cannot by itself identify the selected tab.
    `Channel.ui_state().is_visible` checks OS-window visibility, selected tab,
    and layout visibility together.
    """
    if _visibility.ui_state(window.id).is_visible:
        _mark_seen(window)
