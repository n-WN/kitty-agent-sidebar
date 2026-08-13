"""Semantic, non-polling agent state for Kitty's vertical tab bar.

Official Codex/Claude hooks publish ``agent_status_v1``. The five-minute
collector can publish a separate, non-notifying ``agent_runtime_v1`` fallback
for sessions that started before hooks were installed. Manual tab titles stay
the human-readable label and are never parsed or replaced.
"""
from __future__ import annotations

import json
import time
from functools import lru_cache
from typing import Any, NamedTuple

from kitty.boss import get_boss
from kitty.fast_data_types import get_options
from kitty.notifications import Channel


AGENT_STATUS_KEY = 'agent_status_v1'
AGENT_RUNTIME_KEY = 'agent_runtime_v1'
AGENT_SEEN_KEY = 'agent_seen_v1'
VALID_KINDS = frozenset(('codex', 'claude'))
VALID_STATES = frozenset(('working', 'ready', 'action', 'error', 'disconnected', 'unknown'))
VALID_EVENTS = frozenset(('', 'complete', 'action', 'error'))
RUNTIME_MAX_AGE_NS = 10 * 60 * 1_000_000_000
HOOK_UNVERIFIED_MAX_AGE_NS = RUNTIME_MAX_AGE_NS
_visibility = Channel()


def _mark_seen_if_visible(window: Any, state: AgentState | None = None) -> None:
    """Advance the pane watermark only for the selected, visible tab.

    ``draw_title`` is also called for background rows. Checking selected-tab
    identity here avoids Kitty's serialized ``is_focused`` field, which can be
    stale or true for each tab's active pane. The write happens at most once per
    meaningful sequence; its watcher then triggers one final redraw with the
    unread dot removed.
    """
    if (
        state is None or state.provenance != 'hook' or
        not _is_active_tab_for_window(window) or
        not _visibility.ui_state(window.id).is_visible
    ):
        return
    seen = json.dumps({'v': 1, 'i': state.instance, 'q': state.sequence}, separators=(',', ':'))
    if window.user_vars.get(AGENT_SEEN_KEY) != seen:
        window.set_user_var(AGENT_SEEN_KEY, seen)


def _is_active_tab_for_window(window: Any) -> bool:
    tab = window.tabref()
    if tab is None:
        return False
    manager = tab.tab_manager_ref()
    return manager is not None and manager.active_tab is tab


class AgentState(NamedTuple):
    instance: str
    kind: str
    state: str
    sequence: int
    event: str
    detail: str
    session_id: str
    opened_ns: int
    updated_ns: int
    provenance: str
    captured_ns: int


@lru_cache(maxsize=512)
def _decode(raw: str) -> AgentState | None:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('v') != 1:
            return None
        instance = value.get('i')
        kind = value.get('k')
        state = value.get('s')
        sequence = value.get('q')
        event = value.get('e', '')
        detail = value.get('d', '')
        session_id = value.get('sid', '')
        opened_ns = value.get('o', 0)
        updated_ns = value.get('u', 0)
        provenance = value.get('p', '')
        captured_ns = value.get('c', updated_ns)
        if not isinstance(instance, str) or not instance or len(instance) > 128:
            return None
        if kind not in VALID_KINDS or state not in VALID_STATES or event not in VALID_EVENTS:
            return None
        if not isinstance(sequence, int) or isinstance(sequence, bool) or not 0 <= sequence < 2**63:
            return None
        if not isinstance(detail, str):
            return None
        if not isinstance(session_id, str) or len(session_id) > 128:
            return None
        if not isinstance(opened_ns, int) or isinstance(opened_ns, bool) or opened_ns < 0:
            return None
        if not isinstance(updated_ns, int) or isinstance(updated_ns, bool) or updated_ns < 0:
            return None
        if provenance not in ('hook', 'runtime'):
            return None
        if not isinstance(captured_ns, int) or isinstance(captured_ns, bool) or captured_ns < 0:
            return None
        # Hook envelopes from before local-receipt timestamps were introduced
        # used ``u`` for both fields. Treat them as fresh only when the legacy
        # timestamp is plausibly local; new bridge envelopes always contain c.
        if provenance == 'hook' and 'c' not in value:
            captured_ns = updated_ns
        detail = ' '.join(detail.split())[:160]
    except (TypeError, ValueError):
        return None
    return AgentState(
        instance, kind, state, sequence, event, detail, session_id,
        opened_ns, updated_ns, provenance, captured_ns,
    )


def _selected_state(window: Any) -> AgentState | None:
    """Select a source without letting periodic runtime data mask a Hook.

    A runtime row is written only for the live foreground session found by the
    latest collector. Equal session IDs therefore keep the low-latency Hook.
    Different IDs mean the pane has been reused and the live runtime identity
    supersedes the stale Hook slot.
    """
    hook = _decode(window.user_vars.get(AGENT_STATUS_KEY, ''))
    runtime = _decode(window.user_vars.get(AGENT_RUNTIME_KEY, ''))
    if hook is not None and hook.provenance != 'hook':
        hook = None
    if runtime is not None and runtime.provenance != 'runtime':
        runtime = None
    now_ns = time.time_ns()
    if runtime is not None and (
        runtime.captured_ns <= 0 or now_ns - runtime.captured_ns > RUNTIME_MAX_AGE_NS
    ):
        runtime = None
    if hook is None:
        return runtime
    hook_is_unverified = (
        hook.captured_ns <= 0 or
        now_ns - hook.captured_ns > HOOK_UNVERIFIED_MAX_AGE_NS
    )
    if runtime is None:
        # SessionEnd cannot run after SIGKILL, OOM, or a power loss. Do not
        # preserve that Hook forever after its direct-runtime proof expires.
        return None if hook_is_unverified else hook
    if runtime.session_id == hook.session_id:
        # A fresh direct observation proves that the old Hook still belongs
        # to a live session. If the collector started after the Hook and now
        # observes a different runtime state, runtime is the newer fact.
        if runtime.state != hook.state and runtime.captured_ns > hook.captured_ns:
            return runtime
        return hook
    # A new Hook can arrive between collector cycles. Its arrival is stronger
    # than an older runtime observation from the previous foreground session.
    if hook.captured_ns >= runtime.captured_ns:
        return hook
    return runtime


def _seen_sequence(window: Any, state: AgentState) -> int:
    """Read the focus watcher's pane-local, attachment-scoped watermark."""
    try:
        value = json.loads(window.user_vars.get(AGENT_SEEN_KEY, ''))
    except (TypeError, ValueError):
        return 0
    if not isinstance(value, dict) or value.get('v') != 1 or value.get('i') != state.instance:
        return 0
    sequence = value.get('q')
    return sequence if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0 else 0


def _states_for_tab(tab_id: int) -> list[tuple[Any, AgentState, bool]]:
    tab = get_boss().tab_for_id(tab_id)
    if tab is None:
        return []
    ans: list[tuple[Any, AgentState, bool]] = []
    for window in tab:
        state = _selected_state(window)
        if state is None:
            continue
        _mark_seen_if_visible(window, state)
        # Runtime evidence communicates status only. It never creates unread.
        unread = state.provenance == 'hook' and state.sequence > _seen_sequence(window, state)
        ans.append((window, state, unread))
    return ans


def _display_state(items: list[tuple[Any, AgentState, bool]]) -> AgentState | None:
    if not items:
        return None
    priority = {'error': 0, 'action': 1, 'working': 2, 'ready': 3, 'unknown': 4, 'disconnected': 5}
    return min(items, key=lambda item: (priority[item[1].state], not item[2], not item[0].is_active))[1]


def _newest_opened_ns(items: list[tuple[Any, AgentState, bool]]) -> int:
    return max((item[1].opened_ns for item in items), default=0)


def _is_active_tab(tab_id: int) -> bool:
    """Use tab identity, not Window.is_active (true once in every tab)."""
    tab = get_boss().tab_for_id(tab_id)
    if tab is None:
        return False
    manager = tab.tab_manager_ref()
    return manager is not None and manager.active_tab is tab


def _palette(fmt: Any) -> dict[str, Any]:
    """Preserve the established Anthropic Soft Light sidebar palette."""
    return {
        'working': fmt.fg._356596, 'ready': fmt.fg._4e6a32,
        'action': fmt.fg._7f5b16, 'error': fmt.fg._a34b34,
        'muted': fmt.fg._69645c, 'accent': fmt.fg._d97757,
        'age': (fmt.fg._37342e, fmt.fg._4b4740, fmt.fg._5c584f, fmt.fg._69645c),
        'detail': fmt.fg._857f74,
    }


def _age_title_color(opened_ns: int, colors: dict[str, Any], active: bool) -> Any | None:
    if active or opened_ns <= 0:
        return None
    age = max(0.0, time.time() - opened_ns / 1_000_000_000)
    levels = colors['age']
    if age < 15 * 60:
        return levels[0]
    if age < 60 * 60:
        return levels[1]
    if age < 6 * 60 * 60:
        return levels[2]
    return levels[3]


def draw_title(data: dict[str, Any]) -> str:
    tab_id = int(data['tab_id'])
    items = _states_for_tab(tab_id)
    state = _display_state(items)
    unread = any(item[2] for item in items) or bool(data.get('bell_symbol'))
    is_active = _is_active_tab(tab_id)
    fmt = data['fmt']
    colors = _palette(fmt)

    # Fixed grid: attention(1) + state(1) + spacer(1) + manual title.
    if unread:
        attention = f"{colors['error']}•{fmt.fg.tab}"
    elif is_active:
        attention = f"{colors['accent']}▎{fmt.fg.tab}"
    else:
        attention = ' '
    glyphs = {
        'working': ('·', colors['working']),
        'ready': ('○', colors['ready']),
        'action': ('!', colors['action']),
        'error': ('×', colors['error']),
        'disconnected': ('–', colors['muted']),
        'unknown': ('?', colors['muted']),
    }
    if state is None:
        status = ' '
    else:
        glyph, color = glyphs[state.state]
        status = f'{color}{glyph}{fmt.fg.tab}'

    age_color = _age_title_color(_newest_opened_ns(items), colors, is_active)
    title = str(data.get('title', ''))
    if age_color is not None:
        title = f'{age_color}{title}{fmt.fg.tab}'
    result = f'{attention}{status} {title}'
    max_lines = getattr(get_options(), 'tab_title_max_lines', 1)
    if state is not None and state.state in ('action', 'error') and max_lines > 1:
        detail = state.detail or ('Needs input' if state.state == 'action' else 'Failed')
        result += f"\n   {colors['detail']}{detail}{fmt.fg.tab}"
    return result
