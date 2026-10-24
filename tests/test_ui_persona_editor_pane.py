"""Edit in the Personas tab opens ``$EDITOR`` in a pane rendered in place (#212).

The pane is the Accounts sign-in's: a window on the fleet's tmux server, shown by a
``TerminalPane``. Here the window is ``fleet.open_window`` replaced by a stand-in that RUNS
the editor command for real, on the real draft, and records what it was asked to open; the
pane's exit is what ``pane_facts_or_raise`` answers for it. Every other tmux argv the
dialog causes is recorded and fails, on a private socket. The SKILL.md, the provenance
sidecar and the catalogue are real files in the isolated home, so what each test asserts
is the artefact: the bytes on disk, the validate call, the re-read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

from aisquare.cli.ui import persona_dialogs as dialogs
from aisquare.cli.ui.persona_dialogs import EditorPaneScreen, EditPersonaScreen
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.cli.ui.views import personas_tab
from aisquare.core import paths
from aisquare.core import personas as core
from aisquare.core import tmux as tmux_core
from aisquare.core.tmux import Completed, PaneFacts, TmuxServer, WindowInfo
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import personas as personas_service
from tests.test_ui_personas import (
    Host,
    Recorder,
    catalogue,
    drive,
    press,
    project,
    repo,
    select_row,
    settle,
    shown,
    skill_text,
    wait_for,
)
from tests.ui_workers import settle_until_held

__all__ = ["catalogue", "project", "repo"]  # fixtures, imported for this file's tests

PRIVATE_SOCKET = f"asq-test-{os.getpid()}-persona-editor"
needs_the_pane = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the editor pane is a tmux window, and the fleet's tmux needs a POSIX host; on "
    "Windows Edit opens the text box (test_without_tmux_or_an_editor_edit_opens_…)",
)
PANE = "%9"


@pytest.fixture(autouse=True)
def private_tmux(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, ...]]]:
    """The fleet's server is a private socket, tmux "is installed", and every argv that
    reaches it is recorded and fails: no test here reaches a real server."""
    ran: list[tuple[str, ...]] = []

    def record(argv: Sequence[str], stdin: bytes | None) -> Completed:
        ran.append(tuple(argv))
        return Completed(1, "", "no server running (a UI test addresses no real fleet)\n")

    real_which = shutil.which
    monkeypatch.setattr(tmux_core, "_tmux", record)
    monkeypatch.setattr(fleet_service, "server", lambda config=None: TmuxServer(PRIVATE_SOCKET))
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: "/usr/bin/tmux" if name == "tmux" else real_which(name, *a, **k),
    )
    yield ran
    wrong = [argv for argv in ran if "-L" in argv and argv[argv.index("-L") + 1] != PRIVATE_SOCKET]
    assert not wrong, f"the editor pane addressed a socket that is not the test's: {wrong[:2]}"


class Editor:
    """``fleet.open_window`` replaced: the editor command run to its end on the draft, and
    the pane answered dead with its exit status, or alive while ``running``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, running: bool = False) -> None:
        self.opened: list[dict[str, Any]] = []
        self.status: int | None = None
        self.running = running
        self.killed: list[str] = []
        self.attached: list[str | None] = []
        monkeypatch.setattr(fleet_service, "open_window", self.open_window)
        monkeypatch.setattr(
            TmuxServer, "pane_facts_or_raise", lambda server, pane_id: self.facts(server, pane_id)
        )
        monkeypatch.setattr(
            TmuxServer, "kill_window", lambda server, pane: self.killed.append(pane)
        )
        real_attach = TerminalPane.attach

        def attach(pane: TerminalPane, pane_id: str | None) -> None:
            self.attached.append(pane_id)
            real_attach(pane, pane_id)

        monkeypatch.setattr(TerminalPane, "attach", attach)

    def open_window(
        self,
        server: TmuxServer,
        session: str,
        *,
        name: str,
        cwd: Path,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
        config: object = None,
    ) -> WindowInfo:
        self.opened.append(
            {
                "socket": server.socket,
                "session": session,
                "name": name,
                "cwd": cwd,
                "command": list(command),
            }
        )
        if not self.running:
            self.status = subprocess.run(list(command), check=False).returncode
        return WindowInfo(
            session=session,
            window_id="@9",
            name=name,
            pane_id=PANE,
            dead=False,
            dead_status=None,
            current_command="python",
            activity=False,
        )

    def facts(self, server: TmuxServer, pane_id: str) -> PaneFacts | None:
        return PaneFacts(
            pane_id=pane_id,
            width=80,
            height=24,
            cursor_x=0,
            cursor_y=0,
            cursor_visible=True,
            alternate_on=False,
            history_size=0,
            dead=not self.running,
            dead_status=None if self.running else self.status,
            in_mode=False,
            current_command="python",
            title="",
        )


def _editor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> None:
    """``$EDITOR`` set to a Python stand-in: ``source`` runs with the draft as ``sys.argv[1]``."""
    script = tmp_path / "editor.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", f"{sys.executable} {script}")


async def _opened(pilot: Pilot[None], editor: Editor) -> None:
    """Until the editor's window was opened. Not a wait for the dialog itself: an editor
    that exits at once can close it before a wait for it first looks (as under load)."""
    await settle_until_held(
        pilot.app, lambda: bool(editor.opened), what="the editor's window to open"
    )


async def _until_closed(pilot: Pilot[None]) -> None:
    await settle_until_held(
        pilot.app,
        lambda: not isinstance(pilot.app.screen, EditorPaneScreen),
        what="the editor pane to see its editor exit and close",
    )


def _drafts() -> list[Path]:
    folder = paths.cache_dir() / "persona-edits"
    return sorted(folder.iterdir()) if folder.is_dir() else []


@needs_the_pane
def test_edit_runs_the_editor_in_a_pane_and_an_unchanged_draft_writes_nothing(
    catalogue: dict[str, Path],
    project: ProjectInfo,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#212's acceptance: Edit with an editor that changes nothing (``EDITOR=true``'s
    shape) runs the pane, re-validates the draft and re-reads the catalogue; the SKILL.md
    and its provenance sidecar are untouched."""
    directory = catalogue["pair"]
    personas_service._write_provenance(
        directory,
        source="https://example.com/pair",
        skill_bytes=(directory / core.SKILL_FILE).read_bytes(),
    )
    skill, sidecar = directory / core.SKILL_FILE, directory / core.PROVENANCE_FILE
    before = (
        skill.read_bytes(),
        skill.stat().st_mtime_ns,
        sidecar.read_bytes(),
        sidecar.stat().st_mtime_ns,
    )
    _editor(monkeypatch, tmp_path, "import sys\n")
    editor = Editor(monkeypatch)
    check = Recorder(personas_service.check)
    monkeypatch.setattr(personas_service, "check", check)
    reread = Recorder(personas_tab.read_entries)
    monkeypatch.setattr(personas_tab, "read_entries", reread)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await select_row(pilot, host, "user:pair")
        reads = len(reread.calls)
        await press(pilot, "#persona-edit")
        await _opened(pilot, editor)
        await _until_closed(pilot)
        await settle(pilot)
        return [len(reread.calls) - reads, list(host.notices)]

    rereads, notices = drive(scenario, project=project)
    (opened,) = editor.opened
    assert opened["socket"] == PRIVATE_SOCKET and opened["session"] == dialogs.EDITOR_SESSION
    assert opened["name"] == "persona-pair" and opened["cwd"] == directory
    draft = Path(opened["command"][-1])
    assert opened["command"][:-1] == [sys.executable, str(tmp_path / "editor.py")]
    assert draft.parent == paths.cache_dir() / "persona-edits" and draft.suffix == ".md"
    assert editor.attached[0] == PANE, "the pane showed the editor's window"
    assert [call["args"] for call in check.calls] == [["pair", before[0].decode()]]
    assert rereads >= 1, "the catalogue was read again"
    after = (
        skill.read_bytes(),
        skill.stat().st_mtime_ns,
        sidecar.read_bytes(),
        sidecar.stat().st_mtime_ns,
    )
    assert after == before
    assert ("pair: unchanged", "information") in notices
    assert _drafts() == [], "the draft is dropped"


@needs_the_pane
def test_an_edit_saves_through_the_service_and_bad_frontmatter_is_refused_then_reopened(
    catalogue: dict[str, Path],
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stand-in editor that writes bad frontmatter: the dialog shows the validation
    error, nothing is saved under the catalogue, and *Reopen editor* runs the editor again
    on the same draft, whose second pass is saved through the service."""
    directory = catalogue["pair"]
    skill = directory / core.SKILL_FILE
    original = skill.read_bytes()
    fixed = skill_text("Pairs, edited in my editor.", extra="owner: me\n")
    _editor(
        monkeypatch,
        tmp_path,
        "import sys\n"
        "draft = open(sys.argv[1], encoding='utf-8').read()\n"
        "text = 'no frontmatter at all\\n' if draft.startswith('---') else " + repr(fixed) + "\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(text)\n",
    )
    editor = Editor(monkeypatch)
    layers = sorted(p.name for p in directory.parent.iterdir())

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-edit")
        dialog = await wait_for(pilot, EditorPaneScreen)
        status = dialog.query_one("#editor-status", Static)
        await settle_until_held(
            pilot.app, lambda: shown(status).startswith("✗ "), what="the refusal in the dialog"
        )
        seen: list[Any] = [
            shown(status),
            dialog.query_one("#editor-reopen", Button).has_class("shown"),
            skill.read_bytes(),
            sorted(p.name for p in directory.parent.iterdir()),
        ]
        await press(pilot, "#editor-reopen")
        await _until_closed(pilot)
        await settle(pilot)
        seen.append(skill.read_text(encoding="utf-8"))
        return seen

    refusal, reopen_shown, kept, names, saved = drive(scenario, project=project)
    assert refusal.startswith("✗ ") and "not saved" in refusal
    assert reopen_shown is True
    assert kept == original and names == layers, "nothing was written under the catalogue"
    assert len(editor.opened) == 2
    assert editor.opened[0]["command"][-1] == editor.opened[1]["command"][-1], "the same draft"
    assert saved == fixed
    assert _drafts() == []


@needs_the_pane
def test_an_editor_that_exits_non_zero_saves_nothing(
    catalogue: dict[str, Path],
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As ``persona edit``: vi's ``:cq``, or any failure, leaves the persona as it was."""
    skill = catalogue["pair"] / core.SKILL_FILE
    original = skill.read_bytes()
    _editor(
        monkeypatch,
        tmp_path,
        "import sys\nopen(sys.argv[1], 'w').write('changed')\nsys.exit(3)\n",
    )
    editor = Editor(monkeypatch)

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-edit")
        await _opened(pilot, editor)
        await _until_closed(pilot)
        return list(host.notices)

    notices = drive(scenario, project=project)
    assert skill.read_bytes() == original
    assert ("pair: the editor exited 3: nothing saved", "information") in notices


@needs_the_pane
def test_cancel_closes_the_editors_window_and_drops_the_draft(
    catalogue: dict[str, Path],
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = catalogue["pair"] / core.SKILL_FILE
    original = skill.read_bytes()
    _editor(monkeypatch, tmp_path, "import sys\n")
    editor = Editor(monkeypatch, running=True)

    async def scenario(pilot: Pilot[None], host: Host) -> bool:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-edit")
        await wait_for(pilot, EditorPaneScreen)
        await pilot.pause(dialogs.EDITOR_POLL * 2)
        still_open = isinstance(pilot.app.screen, EditorPaneScreen)
        await press(pilot, "#editor-cancel")
        await settle(pilot)
        return still_open and not isinstance(pilot.app.screen, EditorPaneScreen)

    assert drive(scenario, project=project) is True
    assert editor.killed == [PANE]
    assert skill.read_bytes() == original and _drafts() == []


@pytest.mark.parametrize("missing", ["tmux", "editor", "a posix host"])
def test_without_tmux_or_an_editor_edit_opens_the_text_box_editor(
    missing: str,
    catalogue: dict[str, Path],
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#212: the TextArea editor stays the fallback when there is no tmux, no editor
    (``$VISUAL``, ``$EDITOR`` and ``vi`` all missing), or no POSIX host: Windows has no uid,
    so the fleet's tmux has no socket, even where a ``tmux`` is on PATH (as here)."""
    editor = Editor(monkeypatch)
    monkeypatch.delenv("VISUAL", raising=False)
    if missing == "tmux":
        real_which = shutil.which
        monkeypatch.setattr(
            shutil, "which", lambda name, *a, **k: None if name == "tmux" else real_which(name)
        )
    elif missing == "editor":
        monkeypatch.setenv("EDITOR", "no-such-editor-anywhere")
    else:
        monkeypatch.delattr(os, "getuid", raising=False)
    reason = dialogs.editor_unavailable()

    async def scenario(pilot: Pilot[None], host: Host) -> str:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-edit")
        await wait_for(pilot, EditPersonaScreen)
        return type(pilot.app.screen).__name__

    assert drive(scenario, project=project) == "EditPersonaScreen"
    assert editor.opened == []
    said = {"tmux": "not installed", "editor": "$EDITOR", "a posix host": "POSIX host"}
    assert reason is not None and said[missing] in reason


@needs_the_pane
def test_a_window_that_will_not_open_hands_over_to_the_text_box_and_says_why(
    catalogue: dict[str, Path],
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _editor(monkeypatch, tmp_path, "import sys\n")

    def refused(*args: object, **kwargs: object) -> WindowInfo:
        raise fleet_service.FleetError("the fleet's socket was taken by another server")

    monkeypatch.setattr(fleet_service, "open_window", refused)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-edit")
        await wait_for(pilot, EditPersonaScreen)
        return [type(pilot.app.screen).__name__, list(host.notices)]

    screen, notices = drive(scenario, project=project)
    assert screen == "EditPersonaScreen"
    assert any(
        "socket was taken" in message and "text box instead" in message and severity == "warning"
        for message, severity in notices
    )
    assert _drafts() == []
