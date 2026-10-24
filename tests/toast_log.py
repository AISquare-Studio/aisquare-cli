"""What an app's toasts said, recorded as each is raised: a read no toast's timeout can end.

Not a test module. A toast leaves the screen at its timeout, and Textual's own
``app._notifications`` drops it then too. Read after ``settle``, Settings ▸ Fleet's 5 s
"✓ stopped" toasts were gone on windows-latest, and the test found none (PR #266, CI run
39363641808). A test about what was said calls :func:`record` first, settles, and reads
the list; a test about what is still on screen reads the screen.
"""

from __future__ import annotations

from typing import Any

from textual.app import App


def record(app: App[Any]) -> list[str]:
    """Every toast ``app`` raises from now on, oldest first. Each is still shown as before."""
    told: list[str] = []
    notify = app.notify

    def recorded(message: str, **kwargs: Any) -> None:
        told.append(message)
        notify(message, **kwargs)

    app.notify = recorded  # type: ignore[method-assign]
    return told


def record_severities(app: App[Any]) -> list[tuple[str, str]]:
    """:func:`record`, each toast with its severity: ``(message, severity)``, oldest first."""
    told: list[tuple[str, str]] = []
    notify = app.notify

    def recorded(message: str, **kwargs: Any) -> None:
        told.append((str(message), str(kwargs.get("severity", "information"))))
        notify(message, **kwargs)

    app.notify = recorded  # type: ignore[method-assign]
    return told
