"""Esc or Cancel on a running import: once the user has backed out, nothing is published.

Review of #240 ("also confirmed": the Import dialog, ``persona_dialogs.py`` L357). The
dialog runs ``services.personas.import_source`` in a thread worker, and Esc cancelled the
WORKER and closed the dialog while the THREAD ran on: a recognised skill, which needs no
confirmation, was copied into the layer after the user had backed out, and with Force it
replaced the persona that was there. Nothing said so.

The import in these tests is the real one, over real directories in the isolated home and
a ``git init`` repository, stopped in the dialog's worker at a named place by a
``threading.Event`` (:class:`Held`). So each test reads the disk the user would find
afterwards:

- backed out before the import's last question: the dialog closes and nothing is written,
  not a staging directory, and under Force the persona that was there is byte-identical;
- backed out once the publish has begun: that cannot be taken back, so the dialog waits
  for it, as the Spawn dialog waits for a started spawn, and a toast says what was written;
- nobody backs out: the import publishes as it always did, from the dialog and from
  ``aisquare persona import``, which is asked no such question.

``cancelled`` is that last question, the hook ``import_source`` asks right before it
publishes a recognised skill; the LLM path's last question was always ``confirm``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Literal, TypeVar

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, RadioButton, Static, Switch
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.persona_dialogs import ConfirmDraftScreen, ImportPersonaScreen
from aisquare.core import personas as core
from aisquare.core.personas import Layer, PersonaError
from aisquare.models import ProjectInfo
from aisquare.services import persona_import
from aisquare.services import personas as personas_service
from tests import test_ui_personas as personas_suite
from tests.test_ui_personas import (
    Host,
    drive,
    press,
    settle,
    shown,
    skill_text,
    tab,
    user_layer,
    wait_for,
    write_persona,
)

# The suite's fixtures, bound here so pytest finds them for this module's tests.
no_real_tmux = personas_suite.no_real_tmux
repo = personas_suite.repo
project = personas_suite.project

T = TypeVar("T")
Tree = dict[str, bytes | None]
Where = Literal["start", "publish", "published"]

WAIT = 10.0
"""Seconds a held import waits for the test, and the test for the import: a bound, not a pace."""

OLD = skill_text("The persona that was there.", body="Old ways.")
NEW = skill_text("Reviews carefully.", body="Name the risk first.")
TOO_LATE = "too late to cancel — the persona is already being written"
"""The status line of a dialog that was asked to cancel once the publish had begun."""
DRAFT = persona_import.PersonaDraft(
    name="kind-reviewer",
    description="Reviews diffs kindly and precisely.",
    body="You are a kind reviewer. Name the risk first, then the fix.",
)
"""What the stand-in engine drafts from a file that is no skill."""


def layers(root: Path) -> Tree:
    """Every path under the project and the user layer: a file's bytes, ``None`` for a
    directory, the layer's own directory included. Everywhere an import could write, and
    where a staging directory would be left behind."""
    found: Tree = {}
    for layer, base in core.layer_dirs(root):
        if layer == "bundled" or not base.exists():
            continue
        found[f"{layer}:."] = None
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(base).as_posix()
            found[f"{layer}:{relative}"] = path.read_bytes() if path.is_file() else None
    return found


def layer_base(root: Path, layer: Layer) -> Path:
    return dict(core.layer_dirs(root))[layer]


def source_skill(tmp_path: Path) -> Path:
    """A recognised skill outside every layer: a SKILL.md and one supporting file."""
    return write_persona(
        tmp_path / "skills", "reviewer", NEW, files={"references/notes.md": "notes\n"}
    )


class Held:
    """The REAL ``import_source`` in the dialog's worker, stopped at one place until released.

    ``start``: before the import has read anything, so all of it runs after the release.
    ``publish``: as the persona's ``_publish`` begins, the last question behind it and
    nothing written yet. ``published``: the persona written and the result in hand, the
    worker not yet returned. ``ended`` is the THREAD having left the import: Textual calls
    a cancelled worker finished while its thread still runs, and that thread is the one
    that published. ``outcome`` is what the import answered: its result, or what it raised.
    ``fails`` is what the held ``_publish`` raises when it is let go, once, in place of
    publishing: a disk that filled up.
    """

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, where: Where, *, fails: OSError | None = None
    ) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self.ended = threading.Event()
        self.outcome: list[object] = []
        failures = [fails] if fails is not None else []
        real_import = personas_service.import_source
        real_publish = personas_service._publish

        def stop() -> None:
            self.reached.set()
            assert self.release.wait(WAIT), "the test never released the held import"

        def held_import(source: str, **kwargs: Any) -> personas_service.ImportResult:
            try:
                if where == "start":
                    stop()
                result = real_import(source, **kwargs)
                if where == "published":
                    stop()
            except BaseException as exc:
                self.outcome.append(exc)
                raise
            else:
                self.outcome.append(result)
                return result
            finally:
                self.ended.set()

        def held_publish(dest: Path, fill: Callable[[Path], object]) -> bool:
            if dest.parent.name != personas_service.DRAFTS_DIR:  # a kept draft is no persona
                stop()
                if failures:
                    raise failures.pop()
            return real_publish(dest, fill)

        monkeypatch.setattr(personas_service, "import_source", held_import)
        if where == "publish":
            monkeypatch.setattr(personas_service, "_publish", held_publish)


def drive_held(
    held: Held,
    scenario: Callable[[Pilot[None], Host], Coroutine[Any, Any, T]],
    *,
    project: ProjectInfo | None = None,
    dialog: ImportPersonaScreen | None = None,
) -> T:
    """``drive``, with the held import let go whatever the scenario does: a failed
    assertion must not leave a thread waiting out its bound under the app's shutdown."""

    async def released(pilot: Pilot[None], host: Host) -> T:
        try:
            return await scenario(pilot, host)
        finally:
            held.release.set()

    return drive(released, project=project, dialog=dialog)


async def reached(held: Held) -> None:
    assert await asyncio.to_thread(held.reached.wait, WAIT), "the import never reached its hold"


async def start_import(
    pilot: Pilot[None],
    dialog: ImportPersonaScreen,
    source: Path,
    held: Held,
    *,
    layer: Layer = "user",
    force: bool = False,
) -> None:
    """Fill the form and press Import; the import is at its hold when this returns."""
    dialog.query_one("#import-source", Input).value = str(source)
    if layer == "project":
        dialog.query_one("#import-project", RadioButton).value = True
    dialog.query_one("#import-force", Switch).value = force
    await pilot.pause()
    await press(pilot, "#import-submit")
    await reached(held)


async def back_out(pilot: Pilot[None], way: str) -> None:
    """Esc, or the Cancel button: the two ways out of the dialog."""
    if way == "escape":
        await pilot.press("escape")
    else:
        await pilot.click("#import-cancel")
    await pilot.pause()


async def let_go(pilot: Pilot[None], held: Held) -> None:
    """Release the import and wait for its thread to leave it, then for the page."""
    held.release.set()
    assert await asyncio.to_thread(held.ended.wait, WAIT), "the import's thread never ended"
    await settle(pilot)


def the_dialog(host: Host) -> ImportPersonaScreen:
    dialog = host.screen
    assert isinstance(dialog, ImportPersonaScreen)
    return dialog


# --- backed out in time: nothing is published -------------------------------------------


@pytest.mark.parametrize("way_out", ["escape", "cancel"])
def test_backing_out_of_a_running_import_closes_the_dialog_and_publishes_nothing(
    way_out: str, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_skill(tmp_path)
    held = Held(monkeypatch, "start")
    before = layers(repo)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, list[object]]:
        await start_import(pilot, the_dialog(host), source, held)
        await back_out(pilot, way_out)
        closed = (type(host.screen).__name__, list(host.results))
        await let_go(pilot, held)
        return closed

    closed = drive_held(held, scenario, dialog=ImportPersonaScreen(repo))

    assert closed == ("Screen", [None])  # the dialog closed on the key, as it always did
    assert held.ended.is_set()  # … and the import behind it has run to its end since
    assert not (user_layer() / "reviewer").exists(), "published after the user backed out"
    assert layers(repo) == before  # no file in either layer, not a staging directory
    [outcome] = held.outcome
    assert isinstance(outcome, PersonaError) and outcome.code == "cancelled"


@pytest.mark.parametrize("layer", ["user", "project"])
def test_backing_out_of_a_forced_import_leaves_the_persona_that_was_there_byte_identical(
    layer: Layer, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = write_persona(
        layer_base(repo, layer), "reviewer", OLD, files={"references/old.md": "kept\n"}
    )
    was = (existing / "SKILL.md").read_bytes()
    source = source_skill(tmp_path)
    held = Held(monkeypatch, "start")
    before = layers(repo)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, list[object]]:
        await start_import(pilot, the_dialog(host), source, held, layer=layer, force=True)
        await back_out(pilot, "escape")
        closed = (type(host.screen).__name__, list(host.results))
        await let_go(pilot, held)
        return closed

    closed = drive_held(held, scenario, dialog=ImportPersonaScreen(repo))

    assert closed == ("Screen", [None])
    assert held.ended.is_set()
    assert (existing / "SKILL.md").read_bytes() == was, "replaced after the user backed out"
    assert layers(repo) == before  # the supporting file kept, no sidecar added, no staging


def test_backing_out_while_an_engine_drafts_saves_no_persona_as_before(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LLM path was never the hole: its last question is ``confirm``, which a
    cancelled import has always answered no. The draft is kept, as every draft is."""
    notes = tmp_path / "notes.txt"
    notes.write_text("Be kind in reviews. Name risks before style.\n", encoding="utf-8")
    drafting, drafted = threading.Event(), threading.Event()

    def draft(text: str, **_: object) -> tuple[persona_import.PersonaDraft, str, str]:
        drafting.set()
        assert drafted.wait(WAIT), "the test never released the engine"
        return DRAFT, "manager", "opus"

    monkeypatch.setattr(persona_import, "draft", draft)
    held = Held(monkeypatch, "published")  # never reached: the import ends in a refusal

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, list[object]]:
        the_dialog(host).query_one("#import-source", Input).value = str(notes)
        await pilot.pause()
        await press(pilot, "#import-submit")
        assert await asyncio.to_thread(drafting.wait, WAIT), "the engine was never asked"
        await back_out(pilot, "escape")
        closed = (type(host.screen).__name__, list(host.results))
        drafted.set()
        await let_go(pilot, held)
        return closed

    try:
        closed = drive_held(held, scenario, dialog=ImportPersonaScreen(repo))
    finally:
        drafted.set()

    assert closed == ("Screen", [None])
    assert not (user_layer() / "kind-reviewer").exists()
    [outcome] = held.outcome
    assert isinstance(outcome, personas_service.DraftKept) and outcome.code == "not_confirmed"
    assert outcome.draft_path.is_file()


# --- nobody backs out: the import publishes as before -----------------------------------


@pytest.mark.parametrize("force", [False, True])
def test_an_import_nobody_backs_out_of_publishes_as_before(
    force: bool, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if force:
        write_persona(user_layer(), "reviewer", OLD, files={"references/old.md": "kept\n"})
    source = source_skill(tmp_path)
    held = Held(monkeypatch, "start")

    async def scenario(
        pilot: Pilot[None], host: Host
    ) -> tuple[str, list[object], list[tuple[str, str]]]:
        await start_import(pilot, the_dialog(host), source, held, force=force)
        await let_go(pilot, held)
        return type(host.screen).__name__, list(host.results), list(host.notices)

    screen, results, notices = drive_held(held, scenario, dialog=ImportPersonaScreen(repo))

    assert screen == "Screen"  # dismissed, with the result
    [result] = results
    assert isinstance(result, personas_service.ImportResult)
    persona = result.persona
    assert (persona.name, persona.layer, result.engine) == ("reviewer", "user", "copy")
    assert result.replaced is force
    dest = user_layer() / "reviewer"
    assert (dest / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
    assert (dest / "references" / "notes.md").is_file()
    assert (dest / ".persona.json").is_file()
    assert not (dest / "references" / "old.md").exists()  # replaced whole, never merged
    assert notices == []  # nothing was too late: the dialog says nothing, the tab toasts


def test_the_cli_import_is_asked_no_such_question_and_publishes_as_before(
    runner: CliRunner, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_skill(tmp_path)
    real = personas_service.import_source
    sent: list[set[str]] = []

    def recorded(source: str, **kwargs: Any) -> personas_service.ImportResult:
        sent.append(set(kwargs))
        return real(source, **kwargs)

    monkeypatch.setattr(personas_service, "import_source", recorded)

    result = runner.invoke(app, ["persona", "import", str(source)])

    assert result.exit_code == 0, result.output
    dest = user_layer() / "reviewer"
    assert result.stdout.strip() == f"✓ imported reviewer (copy) into user: {dest}"
    assert (dest / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
    assert sent == [
        {
            "layer",
            "root",
            "name",
            "force",
            "llm",
            "condense",
            "engine",
            "model",
            "confirm",
            "progress",
        }
    ]


# --- backed out too late: the import completes, and the app says so ----------------------


@pytest.mark.parametrize("where", ["publish", "published"])
def test_backing_out_once_the_publish_has_begun_waits_and_a_toast_says_what_was_written(
    where: Where, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_skill(tmp_path)
    held = Held(monkeypatch, where)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        dialog = the_dialog(host)
        await start_import(pilot, dialog, source, held)
        await back_out(pilot, "escape")
        waited = host.screen is dialog
        seen: list[Any] = [
            waited,
            list(host.results),
            shown(dialog.query_one("#import-status", Static)) if waited else None,
        ]
        await let_go(pilot, held)
        return [*seen, type(host.screen).__name__, list(host.results), list(host.notices)]

    waited, told, status, screen, results, notices = drive_held(
        held, scenario, dialog=ImportPersonaScreen(repo)
    )

    dest = user_layer() / "reviewer"
    assert (dest / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()  # written
    assert notices == [
        (
            "too late to cancel: reviewer was already being written — it is now a user persona",
            "warning",
        )
    ], "a persona was written after the user backed out, and nothing said so"
    assert waited is True and told == [] and status == TOO_LATE  # Esc waited, and said why
    assert screen == "Screen"  # … and the dialog closed once the import had answered
    [result] = results
    assert isinstance(result, personas_service.ImportResult)  # the opener gets it as ever


def test_a_forced_import_that_cancel_came_too_late_for_says_what_it_replaced(
    project: ProjectInfo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the Personas tab, with the Cancel button: the dialog's toast, then the
    tab's own, and the replaced row selected. Nothing is lost on the way."""
    write_persona(user_layer(), "reviewer", OLD)
    source = source_skill(tmp_path)
    held = Held(monkeypatch, "published")

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await press(pilot, "#persona-import")
        dialog = await wait_for(pilot, ImportPersonaScreen)
        await start_import(pilot, dialog, source, held, force=True)
        await back_out(pilot, "cancel")
        waited = host.screen is dialog
        await let_go(pilot, held)
        return [waited, type(host.screen).__name__, tab(host).selected_key, list(host.notices)]

    waited, screen, selected, notices = drive_held(held, scenario, project=project)

    dest = user_layer() / "reviewer"
    assert (dest / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()  # replaced
    assert notices == [
        (
            "too late to cancel: reviewer was already being written — it replaced the "
            "user persona of that name",
            "warning",
        ),
        ("✓ imported reviewer (copy)", "information"),
    ], "a persona was replaced after the user backed out, and nothing said so"
    assert waited is True and screen == "Screen"
    assert selected == "user:reviewer"


def test_backing_out_after_saving_a_draft_waits_and_a_toast_says_what_was_written(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LLM path's last question is ``confirm``: once Save has answered it, the
    publish has begun, and Esc on the Import dialog behind the modal is too late."""
    notes = tmp_path / "notes.txt"
    notes.write_text("Be kind in reviews. Name risks before style.\n", encoding="utf-8")
    monkeypatch.setattr(persona_import, "draft", lambda text, **_: (DRAFT, "manager", "opus"))
    held = Held(monkeypatch, "publish")

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        dialog = the_dialog(host)
        dialog.query_one("#import-source", Input).value = str(notes)
        await pilot.pause()
        await press(pilot, "#import-submit")
        await wait_for(pilot, ConfirmDraftScreen)
        await press(pilot, "#draft-save")
        await reached(held)  # saved, and the persona's publish has begun
        await back_out(pilot, "escape")
        waited = host.screen is dialog
        await let_go(pilot, held)
        return [waited, type(host.screen).__name__, list(host.results), list(host.notices)]

    waited, screen, results, notices = drive_held(held, scenario, dialog=ImportPersonaScreen(repo))

    assert (user_layer() / "kind-reviewer" / "SKILL.md").is_file()  # written
    assert notices == [
        (
            "too late to cancel: kind-reviewer was already being written — it is now a "
            "user persona",
            "warning",
        )
    ], "a persona was written after the user backed out, and nothing said so"
    assert waited is True and screen == "Screen"
    [result] = results
    assert isinstance(result, personas_service.ImportResult) and result.engine == "manager"


@pytest.mark.parametrize("backs_out", [True, False])
def test_the_next_import_in_the_same_dialog_starts_afresh(
    backs_out: bool, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal leaves the dialog open, and Import can be pressed again. That the import
    before was past its last question when Esc came, and then failed to publish, says
    nothing about this one: it can still be backed out of, and when nobody backs out its
    result is not announced as a cancel that came too late."""
    source = source_skill(tmp_path)
    first = Held(monkeypatch, "publish", fails=OSError("No space left on device"))
    before = layers(repo)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        dialog = the_dialog(host)
        await start_import(pilot, dialog, source, first)
        await back_out(pilot, "escape")  # too late: the publish has begun …
        assert host.screen is dialog, "the dialog closed on a persona it could no longer stop"
        await let_go(pilot, first)  # … and then it fails
        refused = shown(dialog.query_one("#import-status", Static))
        second = Held(monkeypatch, "start")
        try:
            await press(pilot, "#import-submit")
            await reached(second)
            if backs_out:
                await back_out(pilot, "escape")
            meanwhile = (type(host.screen).__name__, list(host.results))
            await let_go(pilot, second)
        finally:
            second.release.set()
        return [refused, meanwhile, type(host.screen).__name__, host.results, host.notices]

    refused, meanwhile, screen, results, notices = drive_held(
        first, scenario, dialog=ImportPersonaScreen(repo)
    )

    assert refused == "OSError: No space left on device"  # the first import, in the form
    assert notices == []  # nothing was written late: the first failed, the second was on time
    assert screen == "Screen"
    if backs_out:
        assert meanwhile == ("Screen", [None])  # Esc closed it: this import could be stopped
        assert results == [None]
        assert layers(repo) == before, "published after the user backed out"
    else:
        assert meanwhile == ("ImportPersonaScreen", [])
        [result] = results
        assert isinstance(result, personas_service.ImportResult)
        dest = user_layer() / "reviewer"
        assert (dest / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()


# --- the last question itself: services.personas.import_source ---------------------------


def import_skill(
    source: Path, root: Path, *, cancelled: Callable[[], bool], force: bool = False
) -> personas_service.ImportResult:
    return personas_service.import_source(
        str(source),
        layer="user",
        root=root,
        name=None,
        force=force,
        llm="never",
        condense=False,
        engine=None,
        model=None,
        confirm=lambda view: False,
        cancelled=cancelled,
    )


def test_the_last_question_is_asked_once_with_nothing_written_and_a_no_publishes(
    repo: Path, tmp_path: Path
) -> None:
    source = source_skill(tmp_path)
    before = layers(repo)
    asked: list[Tree] = []

    def cancelled() -> bool:
        asked.append(layers(repo))
        return False

    result = import_skill(source, repo, cancelled=cancelled)

    assert asked == [before]  # once, and the disk as it was: the question comes first
    dest = user_layer() / "reviewer"
    assert result.persona.path == dest and result.engine == "copy"
    assert (dest / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()


@pytest.mark.parametrize("force", [False, True])
def test_a_yes_to_the_last_question_ends_the_import_with_nothing_written(
    force: bool, repo: Path, tmp_path: Path
) -> None:
    if force:
        write_persona(user_layer(), "reviewer", OLD, files={"references/old.md": "kept\n"})
    source = source_skill(tmp_path)
    before = layers(repo)

    with pytest.raises(PersonaError) as refused:
        import_skill(source, repo, force=force, cancelled=lambda: True)

    assert refused.value.code == "cancelled"
    assert layers(repo) == before  # under Force too: what was there is byte-identical


def test_an_import_that_is_refused_anyway_never_reaches_its_last_question(
    repo: Path, tmp_path: Path
) -> None:
    """It is the LAST question: a caller that hears it knows the publish is next, and the
    dialog stops offering to cancel from that moment. An import that ends in a refusal of
    its own (here: the name is taken, and no Force) must not have asked."""
    write_persona(user_layer(), "reviewer", OLD)
    source = source_skill(tmp_path)
    asked: list[bool] = []

    def cancelled() -> bool:
        asked.append(True)
        return False

    with pytest.raises(PersonaError) as refused:
        import_skill(source, repo, cancelled=cancelled)

    assert refused.value.code == "persona_exists"
    assert asked == []
