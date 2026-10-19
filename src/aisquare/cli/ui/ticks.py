"""One guard for the callbacks a timer calls: nothing once the app has begun to shut down.

Textual's ``App._shutdown`` sets ``is_running`` False, then prunes the screens, and a timer
that was already due still runs. Landing in between, a two-second ``refresh_data`` tick
found the sidebar gone and raised ``NoMatches('Sidebar')`` out of the quit — a traceback
on exit, more likely the more timers asq runs (the status line's headroom, the Accounts
page's minute). :func:`while_running` makes such a callback return quietly when its app is
no longer running, or its widget is no longer on the screen; a call from code that runs
while the app does is unchanged.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Concatenate, ParamSpec, TypeVar

from textual.app import App
from textual.message_pump import MessagePump

P = ParamSpec("P")
N = TypeVar("N", bound=MessagePump)


def live(node: MessagePump) -> bool:
    """Whether ``node`` is still worth painting: its app runs, and a widget is still in its DOM
    (``is_attached`` is false once the app was asked to exit, or the widget was removed)."""
    if isinstance(node, App):
        return node.is_running
    return node.is_attached and node.app.is_running


def while_running(method: Callable[Concatenate[N, P], None]) -> Callable[Concatenate[N, P], None]:
    """``method``, doing nothing when its node is not :func:`live` — for a timer's callback."""

    @functools.wraps(method)
    def tick_when_live(node: N, /, *args: P.args, **kwargs: P.kwargs) -> None:
        if live(node):
            method(node, *args, **kwargs)

    return tick_when_live
