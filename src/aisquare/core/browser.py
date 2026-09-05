"""Open a URL in the user's browser, or say honestly that we could not.

``webbrowser.open`` returns ``True`` far more often than a browser actually
appears: over SSH it hands the URL to ``xdg-open`` on a machine with no
display, in a container it finds nothing and still reports success on some
platforms, and under WSL it may launch a Windows browser or nothing. So the
verification URL is ALWAYS printed by the caller, and this module's job is to
decide whether launching is worth attempting at all, then attempt it without
blocking the sign-in.

Precedence, deliberately in this order:

1. ``BROWSER`` set: the user has told us what to do, so no heuristic overrides
   it. The values ``echo``, ``true`` and ``:`` mean "print only" (a common way
   to disable auto-open).
2. Headless: an SSH session, CI, a Codespace, a non-TTY stdout, or Linux with
   neither ``DISPLAY`` nor ``WAYLAND_DISPLAY``. Nothing is launched.
3. A text-mode browser (lynx, w3m, links, elinks, www-browser) or a bare
   ``GenericBrowser`` would take over the terminal that is showing the code, so
   it is refused; anything else is launched in a daemon thread.

Every ``webbrowser`` import is inside a function: that module pulls in
``shlex`` and ``subprocess``, and the CLI's import-cost ratchet pins ``shlex``
as unique to the explainability integration.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Mapping

#: Any of these set means the terminal is not on a machine with a browser.
HEADLESS_MARKERS = ("SSH_CONNECTION", "SSH_TTY", "CI", "CODESPACES")

#: Browsers that run INSIDE the terminal and would hide the code being shown.
TEXT_BROWSERS = frozenset({"lynx", "w3m", "links", "elinks", "www-browser"})

#: ``BROWSER`` values that mean "do not open anything, I will copy the URL".
PRINT_ONLY = frozenset({"", "echo", "true", ":"})


def is_headless(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    stdout_isatty: bool | None = None,
) -> bool:
    """Whether launching a browser from this process could reach the user."""
    env = os.environ if environ is None else environ
    system = sys.platform if platform is None else platform
    isatty = sys.stdout.isatty() if stdout_isatty is None else stdout_isatty
    if any(env.get(marker) for marker in HEADLESS_MARKERS):
        return True
    if not isatty:
        return True
    return bool(
        system.startswith("linux") and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))
    )


def open_url(url: str, environ: Mapping[str, str] | None = None) -> bool:
    """Try to open ``url``. ``True`` means a launch was attempted, not that it worked."""
    env = os.environ if environ is None else environ
    preferred = env.get("BROWSER")
    if preferred is not None:
        if preferred.strip() in PRINT_ONLY:
            return False
        return _launch(url, preferred.strip())
    if is_headless(env):
        return False
    return _launch(url, None)


def _launch(url: str, using: str | None) -> bool:
    import webbrowser

    try:
        controller = webbrowser.get(using)
    except webbrowser.Error:
        return False
    name = str(getattr(controller, "name", "") or "")
    basename = os.path.basename(name.split()[0]) if name.strip() else ""
    if basename in TEXT_BROWSERS or type(controller) is webbrowser.GenericBrowser:
        return False

    def _open() -> None:
        # A browser failing must never fail the sign-in.
        with contextlib.suppress(Exception):
            controller.open(url, new=2)

    threading.Thread(target=_open, name="aisquare-open-browser", daemon=True).start()
    return True
