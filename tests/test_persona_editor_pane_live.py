"""The ``$EDITOR`` pane (#212) on a REAL tmux server: a private socket, never ``asq``.

tests/test_ui_persona_editor_pane.py drives the dialog with the window stubbed. This
opens the editor's window for real through ``fleet.open_window``, on a server the fleet's
own config starts (``remain-on-exit`` included), and lets the dialog see the editor exit
through ``pane_facts_or_raise``, as it will in ``asq``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot

from aisquare.cli.ui.persona_dialogs import EditorPaneScreen
from aisquare.core import personas as core
from aisquare.core.tmux import TmuxError, TmuxServer, WindowInfo
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service
from tests.test_ui_personas import (
    Host,
    catalogue,
    drive,
    press,
    project,
    repo,
    select_row,
)
from tests.ui_workers import settle_until_held

__all__ = ["catalogue", "project", "repo"]  # fixtures, imported for this file's test

SOCKET = f"asq-test-{os.getpid()}-editor-live"

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None or sys.platform == "win32",
    reason="tmux is not installed; the live editor pane needs it",
)


@pytest.fixture
def live_socket() -> Iterator[str]:
    """The fleet's socket, private to this test, and its server killed after."""
    settings_service.set_value("fleet.tmux_socket", SOCKET)
    try:
        yield SOCKET
    finally:
        with contextlib.suppress(TmuxError):  # no server left is what we want
            TmuxServer(SOCKET).run("kill-server")


def test_the_editor_runs_in_a_real_window_and_its_exit_closes_the_pane(
    live_socket: str,
    catalogue: dict[str, Path],
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = catalogue["pair"] / core.SKILL_FILE
    original = skill.read_bytes()
    fixed = "---\ndescription: Pairs, edited for real.\n---\nWork.\n"
    script = tmp_path / "editor.py"
    script.write_text(
        f"import sys\nopen(sys.argv[1], 'w', encoding='utf-8').write({fixed!r})\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", f"{sys.executable} {script}")
    opened: list[str] = []
    real_open = fleet_service.open_window

    def recording(server: TmuxServer, session: str, **kwargs: Any) -> WindowInfo:
        opened.append(f"{server.socket}/{session}/{kwargs['name']}")
        return real_open(server, session, **kwargs)

    monkeypatch.setattr(fleet_service, "open_window", recording)

    async def scenario(pilot: Pilot[None], host: Host) -> bool:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-edit")
        await settle_until_held(
            pilot.app,
            lambda: bool(opened) and not isinstance(pilot.app.screen, EditorPaneScreen),
            what="the real editor window to open, exit, and the pane to close",
            timeout=20,
        )
        return True

    assert drive(scenario, project=project) is True
    assert opened == [f"{SOCKET}/asq-persona-edit/persona-pair"]
    assert original != fixed.encode()
    assert skill.read_text(encoding="utf-8") == fixed
