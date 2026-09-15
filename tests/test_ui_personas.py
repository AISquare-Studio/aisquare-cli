"""The Personas tab and its dialogs, driven headless.

docs/plans/spawn-personas.md §4.2 to §4.5 and §7 "P6". The catalogue the tab
reads is the real one — ``core.personas`` over persona directories this file
writes into the isolated home and a real ``git init`` repository — because
reading is what the tab is about. Every WRITE and every import is a recorder in
place of the ``services.personas`` function the UI calls, so what each test
asserts is the artefact: the exact keywords a recorder received, the rows the
table shows, the text of the preview, the toasts, and — for the editor — that
the file on disk is untouched when only the recorder was asked to save.

Import's two callbacks are driven for real: the recorder calls ``progress`` and
``confirm`` from the import worker's thread, so the confirm modal is opened by
``app.call_from_thread(app.push_screen_wait, …)`` exactly as in production.

No test reaches tmux: nothing here mounts a pane, and the guard asserts that no
tmux argv was issued at all.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable, Coroutine, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.notifications import SeverityLevel
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    OptionList,
    RadioButton,
    Select,
    Static,
    Switch,
    TextArea,
)

from aisquare.cli.ui import persona_dialogs as dialogs
from aisquare.cli.ui.attach import (
    SEAT_RULE,
    AttachTargetScreen,
    ConfirmAttachScreen,
    NewAccountRequested,
    NewBindScreen,
    Target,
)
from aisquare.cli.ui.persona_dialogs import (
    NAME_RULE,
    PARSE_DEBOUNCE,
    ConfirmDraftScreen,
    ConfirmRemoveScreen,
    EditPersonaScreen,
    ExportPersonaScreen,
    ImportPersonaScreen,
    NewPersonaScreen,
)
from aisquare.cli.ui.spawn import SpawnCompleted, SpawnDialog
from aisquare.cli.ui.views.personas_tab import (
    BUNDLED_REASON,
    AttachRequested,
    PersonasTab,
)
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import personas as core
from aisquare.core import tmux as tmux_core
from aisquare.core.personas import Layer, Persona, PersonaError
from aisquare.core.tmux import Completed
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeInstall,
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
)
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import personas as personas_service
from aisquare.services import settings as settings_service

T = TypeVar("T")
SIZE = (160, 50)
Kwargs = dict[str, object]


# --- fixtures and helpers -------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, ...]]]:
    """The persona UI mounts no pane: not one tmux argv may be issued."""
    ran: list[tuple[str, ...]] = []

    def record(argv: Sequence[str], stdin: bytes | None) -> Completed:
        ran.append(tuple(argv))
        return Completed(1, "", "no server running (the persona UI never runs tmux)\n")

    monkeypatch.setattr(tmux_core, "_tmux", record)
    yield ran
    assert ran == [], f"the persona UI ran tmux: {ran[:2]}"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git repository — ``git_common_root`` asks git — with the cwd elsewhere."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return root.resolve()


@pytest.fixture
def project(repo: Path) -> ProjectInfo:
    return ProjectInfo(id="prj_personas", root=repo, codename="amber-otter")


def user_layer() -> Path:
    return dict(core.layer_dirs(None))["user"]


def project_layer(repo: Path) -> Path:
    return dict(core.layer_dirs(repo))["project"]


def skill_text(
    description: str,
    *,
    body: str = "Work.",
    roles: str | None = None,
    tags: str | None = None,
    extra: str = "",
) -> str:
    metadata = ""
    if roles or tags:
        metadata = "metadata:\n"
        metadata += f"  persona-roles: {roles}\n" if roles else ""
        metadata += f"  persona-tags: {tags}\n" if tags else ""
    return f"---\ndescription: {description}\n{metadata}{extra}---\n{body}\n"


def write_persona(base: Path, name: str, text: str, files: dict[str, str] | None = None) -> Path:
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    for relative, content in (files or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return directory


@pytest.fixture
def catalogue(repo: Path) -> dict[str, Path]:
    """A project skeptic over the bundled one, a user pair, and a broken user directory."""
    return {
        "skeptic": write_persona(
            project_layer(repo), "skeptic", skill_text("Project skeptic.", roles="tester")
        ),
        "pair": write_persona(
            user_layer(),
            "pair",
            skill_text("Pairs on the work.", tags="pairing", extra="owner: me\n"),
            files={"references/notes.md": "notes\n"},
        ),
        "broken": write_persona(
            user_layer(), "broken", "---\nname: broken\n---\nNo description.\n"
        ),
    }


class Recorder:
    """A ``services.personas`` function replaced: every call's keywords, then a scripted answer."""

    def __init__(self, answer: Callable[..., Any]) -> None:
        self.calls: list[Kwargs] = []
        self.answer = answer

    def __call__(self, *args: object, **kwargs: object) -> Any:
        self.calls.append({"args": list(args), **kwargs})
        return self.answer(*args, **kwargs)


class Host(App[None]):
    """A bare app around one Personas tab — or around one dialog — recording what surfaces."""

    def __init__(self, project: ProjectInfo | None, dialog: Screen[Any] | None = None) -> None:
        super().__init__()
        self._project = project
        self._dialog = dialog
        self.notices: list[tuple[str, str]] = []
        self.attached: list[tuple[str, str]] = []
        self.results: list[object] = []
        self.spawned: list[object] = []
        self.new_accounts = 0

    def compose(self) -> ComposeResult:
        if self._project is not None:
            yield PersonasTab(self._project, id="personas")

    def on_mount(self) -> None:
        if self._dialog is not None:
            self.push_screen(self._dialog, callback=self.results.append)

    def on_attach_requested(self, event: AttachRequested) -> None:
        self.attached.append((event.persona, event.intent))

    def on_spawn_completed(self, event: SpawnCompleted) -> None:
        self.spawned.append(event.receipt)

    def on_new_account_requested(self, event: NewAccountRequested) -> None:
        self.new_accounts += 1

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notices.append((message, severity))
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)


def drive(
    scenario: Callable[[Pilot[None], Host], Coroutine[Any, Any, T]],
    *,
    project: ProjectInfo | None = None,
    dialog: Screen[Any] | None = None,
) -> T:
    async def run() -> T:
        host = Host(project, dialog)
        async with host.run_test(size=SIZE) as pilot:
            await settle(pilot)
            return await scenario(pilot, host)

    return asyncio.run(run())


async def settle(pilot: Pilot[Any]) -> None:
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


async def wait_for(pilot: Pilot[Any], kind: type[Screen[Any]], seconds: float = 5.0) -> Any:
    """Until ``kind`` is the active screen — a modal a worker thread opens arrives later."""
    for _ in range(int(seconds / 0.05)):
        if isinstance(pilot.app.screen, kind):
            return pilot.app.screen
        await pilot.pause(0.05)
    raise AssertionError(f"{kind.__name__} never opened; the screen is {pilot.app.screen!r}")


async def press(pilot: Pilot[Any], selector: str) -> None:
    """Click, then wait past the button's 0.2 s active effect so the next click counts."""
    await pilot.click(selector)
    await pilot.pause(0.3)


def shown(widget: Static) -> str:
    return str(widget.render()) if widget.display else ""


def tab(host: Host) -> PersonasTab:
    return host.query_one(PersonasTab)


def rows(host: Host) -> list[tuple[str, ...]]:
    table = host.query_one("#persona-table", DataTable)
    return [tuple(str(cell) for cell in table.get_row_at(i)) for i in range(table.row_count)]


async def select_row(pilot: Pilot[Any], host: Host, key: str) -> None:
    table = host.query_one("#persona-table", DataTable)
    table.move_cursor(row=table.get_row_index(key))
    await pilot.pause()


def persona(directory: Path, layer: Layer = "user") -> Persona:
    return core.load(directory, layer=layer)


# --- the catalogue --------------------------------------------------------------------


def test_the_catalogue_lists_layer_description_roles_and_marks(
    project: ProjectInfo, catalogue: dict[str, Path]
) -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, ...]]:
        return rows(host)

    listed = drive(scenario, project=project)
    names = [row[0] for row in listed]
    assert names == ["broken", "careful", "mentor", "minimalist", "pair", "skeptic"]
    by_name = {row[0]: row for row in listed}
    assert by_name["skeptic"] == (
        "skeptic",
        "project",
        "Project skeptic.",
        "tester",
        "⇧ shadows bundled",
    )
    assert by_name["pair"][:3] == ("pair", "user", "Pairs on the work.")
    assert by_name["careful"][1] == "bundled" and by_name["careful"][4] == ""
    broken = by_name["broken"]
    assert broken[1] == "user" and broken[4] == "✗ invalid"
    assert "description" in broken[2]  # the reason is the row's description


def test_search_narrows_by_name_description_and_tag_and_the_chips_by_layer(
    project: ProjectInfo, catalogue: dict[str, Path]
) -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> list[list[str]]:
        search = host.query_one("#persona-search", Input)
        seen: list[list[str]] = []
        for query in ("pai", "on the work", "pairing", "SKEPTIC"):
            search.value = query
            await pilot.pause()
            seen.append([row[0] for row in rows(host)])
        search.value = ""
        host.query_one("#layer-bundled", Checkbox).value = False
        await pilot.pause()
        seen.append([row[0] for row in rows(host)])
        host.query_one("#layer-user", Checkbox).value = False
        await pilot.pause()
        seen.append([row[0] for row in rows(host)])
        return seen

    assert drive(scenario, project=project) == [
        ["pair"],  # name
        ["pair"],  # description
        ["pair"],  # tag
        ["skeptic"],  # case-insensitive
        ["broken", "pair", "skeptic"],  # bundled chip off
        ["skeptic"],  # user chip off too
    ]


def test_the_preview_is_byte_equal_to_the_briefing_and_shows_where_it_came_from(
    project: ProjectInfo, catalogue: dict[str, Path], repo: Path
) -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, str, str, str]:
        await select_row(pilot, host, "project:skeptic")
        skeptic = shown(host.query_one("#persona-briefing", Static))
        await select_row(pilot, host, "user:pair")
        pair_briefing = shown(host.query_one("#persona-briefing", Static))
        pair_details = shown(host.query_one("#persona-details", Static))
        await select_row(pilot, host, "user:broken")
        return (
            skeptic,
            pair_briefing,
            pair_details,
            shown(host.query_one("#persona-briefing", Static)),
        )

    skeptic, pair_briefing, pair_details, broken = drive(scenario, project=project)
    assert skeptic == "\n".join(core.briefing(core.resolve("skeptic", repo)))
    assert pair_briefing == "\n".join(core.briefing(persona(catalogue["pair"])))
    assert str(catalogue["pair"]) in pair_details
    assert "references/notes.md" in pair_details  # supporting files
    assert "no .persona.json" in pair_details  # provenance, absent here
    assert "'owner' is not a documented Claude Code skill key" in pair_details  # warnings()
    assert broken.startswith(f"✗ {catalogue['broken']}:")  # the reason, not a briefing


def test_an_imported_persona_shows_its_provenance(project: ProjectInfo, tmp_path: Path) -> None:
    source = write_persona(tmp_path / "skills", "reviewer", skill_text("Reviews carefully."))
    personas_service.import_source(
        str(source),
        layer="user",
        root=None,
        name=None,
        force=False,
        llm="never",
        condense=False,
        engine=None,
        model=None,
        confirm=lambda view: False,
    )

    async def scenario(pilot: Pilot[None], host: Host) -> str:
        await select_row(pilot, host, "user:reviewer")
        return shown(host.query_one("#persona-details", Static))

    details = drive(scenario, project=project)
    assert f"{source} · copy" in details


# --- actions ----------------------------------------------------------------------------


def test_attach_buttons_post_the_request_and_open_the_picker_for_that_intent(
    project: ProjectInfo, catalogue: dict[str, Path], targets: list[FleetAgentStatus]
) -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        seen: list[Any] = []
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-attach-existing")
        picker = await wait_for(pilot, AttachTargetScreen)
        await settle(pilot)
        seen.append((picker.persona_name, picker.attach_intent, option_ids(picker)[0]))
        await pilot.press("escape")
        await pilot.pause()
        host.query_one("#persona-table", DataTable).focus()
        await pilot.press("n")
        picker = await wait_for(pilot, AttachTargetScreen)
        await settle(pilot)
        seen.append((picker.persona_name, picker.attach_intent, option_ids(picker)[0]))
        return [host.attached, seen]

    attached, seen = drive(scenario, project=project)
    assert attached == [("pair", "existing"), ("pair", "new")]  # the message seam still holds
    assert seen == [("pair", "existing", "section:agents"), ("pair", "new", "section:binds")]


def test_bundled_rows_disable_edit_and_remove_and_invalid_rows_cannot_be_attached(
    project: ProjectInfo, catalogue: dict[str, Path]
) -> None:
    def state(host: Host) -> dict[str, bool]:
        return {
            name: not host.query_one(f"#persona-{name}", Button).disabled
            for name in ("attach-existing", "attach-new", "edit", "export", "remove", "validate")
        }

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await select_row(pilot, host, "bundled:careful")
        bundled = (state(host), shown(host.query_one("#persona-note", Static)))
        host.query_one("#persona-table", DataTable).focus()
        await pilot.press("e")  # the key obeys the same rule as the button
        await pilot.press("delete")
        await pilot.pause()
        opened = type(host.screen).__name__
        await select_row(pilot, host, "user:broken")
        broken = state(host)
        return [bundled, opened, broken]

    (bundled_state, note), opened, broken = drive(scenario, project=project)
    assert bundled_state == {
        "attach-existing": True,
        "attach-new": True,
        "edit": False,
        "export": True,
        "remove": False,
        "validate": True,
    }
    assert note == BUNDLED_REASON
    assert opened == "Screen"  # neither key opened a dialog
    assert broken == {
        "attach-existing": False,
        "attach-new": False,
        "edit": True,
        "export": False,
        "remove": True,
        "validate": True,
    }


def test_validate_goes_through_the_service_and_reports_both_ways(
    project: ProjectInfo, catalogue: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    answers: list[Any] = [
        (persona(catalogue["pair"]), ["a warning worth a look"]),
        PersonaError("no description", path=catalogue["broken"], code="not_recognised"),
    ]

    def answer(path: Path) -> Any:
        result = answers.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    validate = Recorder(answer)
    monkeypatch.setattr(personas_service, "validate", validate)

    async def scenario(pilot: Pilot[None], host: Host) -> list[tuple[str, str]]:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-validate")
        await select_row(pilot, host, "user:broken")
        await press(pilot, "#persona-validate")
        return host.notices

    notices = drive(scenario, project=project)
    assert [call["args"] for call in validate.calls] == [[catalogue["pair"]], [catalogue["broken"]]]
    assert notices == [
        ("✓ pair is a valid persona", "information"),
        ("a warning worth a look", "warning"),
        (f"✗ {catalogue['broken']}: no description", "error"),
    ]


def test_remove_asks_once_naming_the_directory_then_calls_the_service(
    project: ProjectInfo, catalogue: dict[str, Path], repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remove = Recorder(lambda name, *, layer, root: user_layer() / name)
    monkeypatch.setattr(personas_service, "remove", remove)

    async def scenario(pilot: Pilot[None], host: Host) -> tuple[str, int, list[Any]]:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-remove")
        question = shown((await wait_for(pilot, ConfirmRemoveScreen)).query_one("#remove-question"))
        await press(pilot, "#remove-cancel")
        after_cancel = len(remove.calls)
        await press(pilot, "#persona-remove")
        await wait_for(pilot, ConfirmRemoveScreen)
        await press(pilot, "#remove-confirm")
        return question, after_cancel, host.notices

    question, after_cancel, notices = drive(scenario, project=project)
    assert str(catalogue["pair"]) in question and "user persona 'pair'" in question
    assert after_cancel == 0  # Cancel removes nothing
    assert remove.calls == [{"args": ["pair"], "layer": "user", "root": repo}]
    assert (f"✓ removed {user_layer() / 'pair'}", "information") in notices


# --- import -----------------------------------------------------------------------------


def skill_ref(name: str, *, imported: bool = False) -> personas_service.SkillRef:
    return personas_service.SkillRef(
        name=name,
        description="Reviews code.",
        path=Path(f"/claude/skills/{name}"),
        scope="user",
        recognised=True,
        imported=imported,
    )


@pytest.fixture
def skills(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder(lambda root: [skill_ref("code-review", imported=True)])
    monkeypatch.setattr(personas_service, "importable_skills", recorder)
    return recorder


def test_the_import_dialog_browses_skills_and_guards_the_layer_and_the_name(
    skills: Recorder, repo: Path
) -> None:
    async def outside_git(pilot: Pilot[None], host: Host) -> list[Any]:
        dialog = host.screen
        assert isinstance(dialog, ImportPersonaScreen)
        project_radio = dialog.query_one("#import-project", RadioButton)
        browse: Select[str] = dialog.query_one("#import-browse", Select)
        prompts = [str(option[0]) for option in browse._options]
        browse.value = "/claude/skills/code-review"
        await pilot.pause()
        source = dialog.query_one("#import-source", Input).value
        submit = dialog.query_one("#import-submit", Button)
        dialog.query_one("#import-name", Input).value = "Bad Name"
        await pilot.pause()
        bad = (submit.disabled, shown(dialog.query_one("#import-name-rule", Static)))
        dialog.query_one("#import-name", Input).value = "good-name"
        await pilot.pause()
        return [project_radio.disabled, prompts, source, bad, submit.disabled]

    disabled, prompts, source, bad, good_disabled = drive(
        outside_git, dialog=ImportPersonaScreen(None)
    )
    assert disabled is True  # no git repository: no project layer
    assert any("code-review" in p and "imported" in p for p in prompts)
    assert source == "/claude/skills/code-review"  # Browse fills the Source field
    assert bad == (True, NAME_RULE)
    assert good_disabled is False
    assert skills.calls == [{"args": [None]}]

    async def inside_git(pilot: Pilot[None], host: Host) -> bool:
        return host.screen.query_one("#import-project", RadioButton).disabled

    assert drive(inside_git, dialog=ImportPersonaScreen(repo)) is False


def test_a_skill_description_with_brackets_is_listed_verbatim_not_parsed_as_markup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bracketed = personas_service.SkillRef(
        name="odd",
        description="Reviews [docs] and [/] code.",
        path=Path("/claude/skills/odd"),
        scope="user",
        recognised=True,
    )
    monkeypatch.setattr(personas_service, "importable_skills", Recorder(lambda root: [bracketed]))

    async def scenario(pilot: Pilot[None], host: Host) -> list[str]:
        browse: Select[str] = host.screen.query_one("#import-browse", Select)
        browse.expanded = True  # render the options the way the reader sees them
        await pilot.pause()
        await pilot.pause()
        overlay = browse.query_one(OptionList)
        # The border rows are not in virtual_size: leave room for them and every option.
        height = max(overlay.virtual_size.height, overlay.size.height) + 4
        region = Region(0, 0, overlay.size.width, height)
        return [strip.text.strip() for strip in overlay.render_lines(region)]

    rendered = drive(scenario, dialog=ImportPersonaScreen(None))
    # A str label is parsed as markup: "[docs]" and "[/]" vanish from the rendered row.
    assert any("Reviews [docs] and [/]" in line for line in rendered), rendered


def result_for(directory: Path, engine: str = "copy") -> personas_service.ImportResult:
    loaded = persona(directory)
    return personas_service.ImportResult(persona=loaded, engine=engine, source="test")


def test_an_import_result_dismisses_refreshes_selects_the_row_and_toasts(
    project: ProjectInfo,
    catalogue: dict[str, Path],
    skills: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(source: str, **_: object) -> personas_service.ImportResult:
        return result_for(write_persona(user_layer(), "imported-one", skill_text("Fresh.")))

    importer = Recorder(answer)
    monkeypatch.setattr(personas_service, "import_source", importer)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await press(pilot, "#persona-import")
        dialog = await wait_for(pilot, ImportPersonaScreen)
        dialog.query_one("#import-source", Input).value = "./skills/imported-one"
        await pilot.pause()
        await press(pilot, "#import-submit")
        await settle(pilot)
        return [
            type(host.screen).__name__,
            tab(host).selected_key,
            [r[0] for r in rows(host)],
            host.notices,
        ]

    screen, selected, names, notices = drive(scenario, project=project)
    assert screen == "Screen"  # dismissed
    assert "imported-one" in names  # the tab re-read the disk
    assert selected == "user:imported-one"
    assert ("✓ imported imported-one (copy)", "information") in notices
    assert len(importer.calls) == 1


DRAFT = personas_service.PersonaDraftView(
    name="drafted",
    description="Brief.",
    body="Be brief.",
    skill_md="---\ndescription: Brief.\n---\nBe brief.\n",
    engine="manager",
    model="opus",
    notes=["dropped: the tool list"],
    draft_path=Path("/home/me/.aisquare/personas/.drafts/drafted"),
)


def confirming_importer(answers: list[bool]) -> Callable[..., personas_service.ImportResult]:
    """An ``import_source`` that reports progress twice, asks once, and acts on the answer."""

    def answer(source: str, **kwargs: Any) -> personas_service.ImportResult:
        kwargs["progress"]("manager engine: claude -p … (up to 180 s)")
        kwargs["progress"]("api engine: claude-opus-5")
        saved = kwargs["confirm"](DRAFT)
        answers.append(saved)
        if not saved:
            raise PersonaError(
                "not saved — the draft stays under .drafts", code="needs_confirmation"
            )
        return result_for(write_persona(user_layer(), "drafted", DRAFT.skill_md), engine="manager")

    return answer


@pytest.mark.parametrize("save", [True, False])
def test_progress_lines_and_the_confirm_modal_are_the_import_seam(
    save: bool, skills: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers: list[bool] = []
    monkeypatch.setattr(personas_service, "import_source", Recorder(confirming_importer(answers)))
    dialog = ImportPersonaScreen(None)

    async def scenario(pilot: Pilot[None], host: Host) -> dict[str, Any]:
        dialog.query_one("#import-source", Input).value = "./notes.txt"
        await pilot.pause()
        await press(pilot, "#import-submit")
        confirm = await wait_for(pilot, ConfirmDraftScreen)
        seen: dict[str, Any] = {
            "progress": shown(dialog.query_one("#import-status", Static)),
            "header": shown(confirm.query_one("#draft-header", Static)),
            "frontmatter": shown(confirm.query_one("#draft-frontmatter", Static)),
            "body": shown(confirm.query_one("#draft-body-text", Static)),
            "notes": shown(confirm.query_one("#draft-notes", Static)),
            "path": shown(confirm.query_one("#draft-path", Static)),
        }
        await press(pilot, "#draft-save" if save else "#draft-discard")
        await settle(pilot)
        seen["screen"] = type(host.screen).__name__
        seen["results"] = list(host.results)
        if not save:
            seen["status"] = shown(dialog.query_one("#import-status", Static))
            seen["draft"] = shown(dialog.query_one("#import-draft", Static))
            seen["import_enabled"] = not dialog.query_one("#import-submit", Button).disabled
        return seen

    seen = drive(scenario, dialog=dialog)
    assert seen["progress"] == (
        "manager engine: claude -p … (up to 180 s)\napi engine: claude-opus-5"
    )  # both, in order
    assert seen["header"].endswith("manager · opus · 9 characters")
    assert "description: Brief." in seen["frontmatter"]
    assert seen["body"] == "Be brief."
    assert seen["notes"] == "dropped: the tool list"
    assert seen["path"] == f"draft kept at {DRAFT.draft_path}"
    assert answers == [save]  # the modal's answer is what the service received
    if save:
        assert seen["screen"] == "Screen"
        assert isinstance(seen["results"][0], personas_service.ImportResult)
    else:
        assert seen["screen"] == "ImportPersonaScreen" and seen["results"] == []
        assert seen["status"] == "not saved — the draft stays under .drafts"
        assert seen["draft"] == f"discarded — the draft stays at {DRAFT.draft_path}"
        assert seen["import_enabled"] is True


def test_an_import_refusal_stays_in_the_dialog_and_import_re_enables(
    skills: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(source: str, **_: object) -> personas_service.ImportResult:
        raise PersonaError(
            f"no such file, directory or Claude Code skill: {source}", code="source_not_found"
        )

    importer = Recorder(refuse)
    monkeypatch.setattr(personas_service, "import_source", importer)
    dialog = ImportPersonaScreen(None)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        dialog.query_one("#import-source", Input).value = "nope"
        await pilot.pause()
        await press(pilot, "#import-submit")
        await settle(pilot)
        seen: list[Any] = [
            type(host.screen).__name__,
            shown(dialog.query_one("#import-status", Static)),
            dialog.query_one("#import-submit", Button).disabled,
        ]
        await press(pilot, "#import-submit")
        await settle(pilot)
        return seen

    screen, status, disabled = drive(scenario, dialog=dialog)
    assert screen == "ImportPersonaScreen"
    assert status == "no such file, directory or Claude Code skill: nope"
    assert disabled is False
    assert len(importer.calls) == 2  # re-enabled for real


def test_the_import_recorder_receives_exactly_the_chosen_options(
    skills: Recorder, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = Recorder(
        lambda source, **_: result_for(write_persona(user_layer(), "x", skill_text("X.")))
    )
    monkeypatch.setattr(personas_service, "import_source", importer)

    async def untouched(pilot: Pilot[None], host: Host) -> None:
        host.screen.query_one("#import-source", Input).value = "./skills/x"
        await pilot.pause()
        await press(pilot, "#import-submit")
        await settle(pilot)

    drive(untouched, dialog=ImportPersonaScreen(repo))

    async def chosen(pilot: Pilot[None], host: Host) -> None:
        dialog = host.screen
        dialog.query_one("#import-source", Input).value = "./skills/y"
        dialog.query_one("#import-project", RadioButton).value = True
        dialog.query_one("#import-name", Input).value = "renamed"
        dialog.query_one("#import-force", Switch).value = True
        dialog.query_one("#import-llm", Switch).value = False
        dialog.query_one("#import-condense", Switch).value = True
        dialog.query_one("#import-engine", Select).value = "api"
        await pilot.pause()
        dialog.query_one("#import-model", Input).value = "claude-sonnet-5"
        await pilot.pause()
        await press(pilot, "#import-submit")
        await settle(pilot)

    drive(chosen, dialog=ImportPersonaScreen(repo))
    first, second = importer.calls
    for call in (first, second):
        assert callable(call.pop("confirm")) and callable(call.pop("progress"))
    assert first == {
        "args": [],
        "source": "./skills/x",
        "layer": "user",
        "root": repo,
        "name": None,
        "force": False,
        "llm": "auto",
        "condense": False,
        "engine": None,  # untouched: the service's ladder decides
        "model": None,
    }
    assert second == {
        "args": [],
        "source": "./skills/y",
        "layer": "project",
        "root": repo,
        "name": "renamed",
        "force": True,
        "llm": "never",
        "condense": True,
        "engine": "api",
        "model": "claude-sonnet-5",
    }


# --- new, edit, save as -----------------------------------------------------------------


def test_new_scaffolds_through_the_service_then_opens_the_editor_on_it(
    project: ProjectInfo, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_new = personas_service.new
    new = Recorder(lambda name, *, layer, root: real_new(name, layer=layer, root=root))
    monkeypatch.setattr(personas_service, "new", new)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await press(pilot, "#persona-new")
        dialog = await wait_for(pilot, NewPersonaScreen)
        create = dialog.query_one("#new-create", Button)
        dialog.query_one("#new-name", Input).value = "Pair Programmer"
        await pilot.pause()
        bad = create.disabled
        dialog.query_one("#new-name", Input).value = "pair-programmer"
        dialog.query_one("#new-description", Input).value = "Pairs, one keyboard."
        await pilot.pause()
        await press(pilot, "#new-create")
        editor = await wait_for(pilot, EditPersonaScreen)
        return [bad, editor.query_one("#edit-text", TextArea).text, tab(host).selected_key]

    bad, text, selected = drive(scenario, project=project)
    assert bad is True
    assert new.calls == [{"args": ["pair-programmer"], "layer": "user", "root": repo}]
    assert "description: Pairs, one keyboard." in text  # the editor opens on the scaffold
    assert selected == "user:pair-programmer"


def test_the_editor_saves_only_text_that_parses_and_only_through_the_service(
    catalogue: dict[str, Path], repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = catalogue["pair"]
    before = (directory / "SKILL.md").read_bytes()
    save = Recorder(lambda name, text, *, root, layer: persona(directory))
    monkeypatch.setattr(personas_service, "save", save)
    valid = skill_text("Pairs, now.", extra="owner: me\n")

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        dialog = host.screen
        area = dialog.query_one("#edit-text", TextArea)
        button = dialog.query_one("#edit-save", Button)
        area.clear()
        area.insert("no frontmatter at all\n")
        await pilot.pause(PARSE_DEBOUNCE + 0.2)
        seen: list[Any] = [button.disabled, shown(dialog.query_one("#edit-status", Static))]
        await pilot.click("#edit-save")  # disabled: nothing may reach the service
        await pilot.pause()
        seen.append(len(save.calls))
        area.clear()
        area.insert(valid)
        await pilot.pause(PARSE_DEBOUNCE + 0.2)
        seen += [button.disabled, shown(dialog.query_one("#edit-status", Static))]
        await press(pilot, "#edit-save")
        seen.append(list(host.results))
        return seen

    disabled, error, calls_while_invalid, enabled_disabled, status, results = drive(
        scenario, dialog=EditPersonaScreen("pair", "user", directory, repo)
    )
    assert disabled is True and error.startswith("✗ ")
    assert calls_while_invalid == 0
    assert enabled_disabled is False
    assert status.startswith("✓ parses") and "'owner' is not a documented" in status
    assert save.calls == [{"args": ["pair", valid], "root": repo, "layer": "user"}]
    assert results == [True]
    assert (directory / "SKILL.md").read_bytes() == before  # the UI wrote nothing itself


def test_a_bundled_persona_opens_read_only_and_saves_as_a_copy_into_a_layer(
    project: ProjectInfo, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = Recorder(lambda name, *, root, to, skill, force: to / name)
    monkeypatch.setattr(personas_service, "export", export)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await select_row(pilot, host, "bundled:skeptic")
        table = host.query_one("#persona-table", DataTable)
        table.focus()
        await pilot.press("enter")  # the row opens even though Edit is disabled
        editor = await wait_for(pilot, EditPersonaScreen)
        seen: list[Any] = [
            editor.query_one("#edit-text", TextArea).read_only,
            len(editor.query("#edit-save")),
        ]
        await press(pilot, "#edit-save-as")
        await settle(pilot)
        seen.append(type(host.screen).__name__)
        return seen

    read_only, save_buttons, screen = drive(scenario, project=project)
    assert read_only is True and save_buttons == 0
    assert export.calls == [
        {"args": ["skeptic"], "root": repo, "to": user_layer(), "skill": None, "force": False}
    ]
    assert screen == "Screen"


# --- export -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("destination", "expected", "claude_hint"),
    [
        ("#export-personal", {"to": None, "skill": "user"}, True),
        ("#export-project", {"to": None, "skill": "project"}, True),
        ("#export-directory", {"to": Path("/tmp/persona-copies"), "skill": None}, False),
    ],
)
def test_export_sends_each_destination_and_the_toast_names_the_path(
    destination: str,
    expected: dict[str, object],
    claude_hint: bool,
    project: ProjectInfo,
    catalogue: dict[str, Path],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = Path("/written/pair")
    export = Recorder(lambda name, **_: written)
    monkeypatch.setattr(personas_service, "export", export)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-export")
        dialog = await wait_for(pilot, ExportPersonaScreen)
        dialog.query_one(destination, RadioButton).value = True
        await pilot.pause()
        submit = dialog.query_one("#export-submit", Button)
        blocked = submit.disabled
        if destination == "#export-directory":
            dialog.query_one("#export-dir", Input).value = "/tmp/persona-copies"
            await pilot.pause()
        await press(pilot, "#export-submit")
        return [blocked, host.notices]

    blocked, notices = drive(scenario, project=project)
    assert blocked is (destination == "#export-directory")  # a directory needs a path first
    assert export.calls == [{"args": ["pair"], "root": repo, "force": False, **expected}]
    toast = f"✓ exported pair to {written}"
    if claude_hint:
        toast += " — it is /pair in Claude Code now"
    assert (toast, "information") in notices


# --- a write the filesystem refuses: every handler keeps the TUI up and names the error ---

DENIED_TEXT = "[Errno 13] Permission denied: '/read-only/personas'"


def denied(*args: object, **kwargs: object) -> Any:
    """A write the filesystem refuses, raised the way ``shutil`` and ``Path`` raise it."""
    raise PermissionError(13, "Permission denied", "/read-only/personas")


def test_a_save_the_filesystem_refuses_keeps_the_editor_open_and_names_the_error(
    catalogue: dict[str, Path], repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save = Recorder(denied)
    monkeypatch.setattr(personas_service, "save", save)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        dialog = host.screen
        area = dialog.query_one("#edit-text", TextArea)
        area.clear()
        area.insert(skill_text("Pairs, now."))
        await pilot.pause(PARSE_DEBOUNCE + 0.2)
        await press(pilot, "#edit-save")
        status = shown(dialog.query_one("#edit-status", Static))
        return [type(host.screen).__name__, status, list(host.results)]

    screen, status, results = drive(
        scenario, dialog=EditPersonaScreen("pair", "user", catalogue["pair"], repo)
    )
    assert len(save.calls) == 1
    assert screen == "EditPersonaScreen" and results == []  # still open, nothing dismissed
    assert status == f"PermissionError: {DENIED_TEXT}"


def test_a_save_as_the_filesystem_refuses_keeps_the_editor_open_and_names_the_error(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = Recorder(denied)
    monkeypatch.setattr(personas_service, "export", export)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await select_row(pilot, host, "bundled:skeptic")
        host.query_one("#persona-table", DataTable).focus()
        await pilot.press("enter")
        editor = await wait_for(pilot, EditPersonaScreen)
        await press(pilot, "#edit-save-as")
        await settle(pilot)
        return [type(host.screen).__name__, shown(editor.query_one("#edit-status", Static))]

    screen, status = drive(scenario, project=project)
    assert len(export.calls) == 1
    assert screen == "EditPersonaScreen"
    assert status == f"PermissionError: {DENIED_TEXT}"


def test_an_export_the_filesystem_refuses_keeps_the_dialog_open_and_names_the_error(
    project: ProjectInfo, catalogue: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    export = Recorder(denied)
    monkeypatch.setattr(personas_service, "export", export)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-export")
        dialog = await wait_for(pilot, ExportPersonaScreen)
        dialog.query_one("#export-directory", RadioButton).value = True
        await pilot.pause()
        dialog.query_one("#export-dir", Input).value = "/read-only/personas"
        await pilot.pause()
        await press(pilot, "#export-submit")
        status = shown(dialog.query_one("#export-status", Static))
        return [type(host.screen).__name__, status, list(host.notices)]

    screen, status, notices = drive(scenario, project=project)
    assert len(export.calls) == 1
    assert screen == "ExportPersonaScreen"
    assert status == f"PermissionError: {DENIED_TEXT}"
    assert not any(message.startswith("✓ exported") for message, _ in notices)


def test_a_remove_the_filesystem_refuses_is_an_error_toast_and_the_tab_stays_up(
    project: ProjectInfo, catalogue: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    remove = Recorder(denied)
    monkeypatch.setattr(personas_service, "remove", remove)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-remove")
        await wait_for(pilot, ConfirmRemoveScreen)
        await press(pilot, "#remove-confirm")
        await settle(pilot)
        return [list(host.notices), len(rows(host))]

    notices, row_count = drive(scenario, project=project)
    assert len(remove.calls) == 1
    assert (f"PermissionError: {DENIED_TEXT}", "error") in notices
    assert row_count > 0  # the tab re-read the catalogue and is still there


def test_export_offers_the_project_skills_only_inside_a_git_repository() -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> bool:
        return host.screen.query_one("#export-project", RadioButton).disabled

    assert drive(scenario, dialog=ExportPersonaScreen("pair", None)) is True


def test_the_dialog_module_calls_the_service_by_attribute() -> None:
    """A recorder only proves something if the UI looks the function up at call time."""
    source = Path(dialogs.__file__).read_text(encoding="utf-8")
    for name in ("import_source", "importable_skills", "new", "save", "export"):
        assert f"personas_service.{name}(" in source


# --- the target picker (P7) -----------------------------------------------------------------


def option_ids(picker: AttachTargetScreen) -> list[str]:
    listing = picker.query_one("#picker-list", OptionList)
    return [listing.get_option_at_index(i).id or "" for i in range(listing.option_count)]


def option_prompts(picker: AttachTargetScreen) -> dict[str, str]:
    listing = picker.query_one("#picker-list", OptionList)
    options = [listing.get_option_at_index(i) for i in range(listing.option_count)]
    return {option.id or "": str(option.prompt).strip() for option in options}


def highlighted_id(picker: AttachTargetScreen) -> str:
    listing = picker.query_one("#picker-list", OptionList)
    assert listing.highlighted is not None
    return listing.get_option_at_index(listing.highlighted).id or ""


MANAGED = ClaudeAccount(
    slot=2, config_dir=Path("/accounts/2"), tmp_dir=Path("/accounts/2/tmp"), managed=True
)


@pytest.fixture
def targets(project: ProjectInfo, monkeypatch: pytest.MonkeyPatch) -> list[FleetAgentStatus]:
    """One live agent running mentor, two binds, two account slots — every read a recorder."""
    agent = FleetAgent(
        id="agt_01pickerscripted",
        project_id=project.id,
        label="coder-auth",
        role="coder",
        binary="claude",
        pane_id="%4",
        cwd=project.root,
        created_at=datetime.now(tz=UTC),
        persona="mentor",
    )
    agents = [FleetAgentStatus(agent=agent, state="waiting")]
    monkeypatch.setattr(
        fleet_service, "list_agents", lambda target, *, live_only=True: list(agents)
    )
    overview = AccountsOverview(
        claude=ClaudeInstall(installed=True, binary="/usr/bin/claude"),
        accounts=[
            ClaudeAccountStatus(
                account=ClaudeAccount(slot=1, config_dir=Path("/home/me/.claude")),
                label="default",
                identity=ClaudeIdentity(email="me@example.com"),
                signed_in=True,
                subscription="max",
            ),
            ClaudeAccountStatus(
                account=MANAGED,
                label="account 2",
                identity=ClaudeIdentity(email="two@example.com"),
                signed_in=True,
            ),
        ],
    )
    monkeypatch.setattr(accounts_service, "overview", lambda: overview)
    settings_service.bind_role(
        "coder2", agent_bin="claude2", env={"CLAUDE_CONFIG_DIR": "/x/.claude2"}
    )
    settings_service.bind_role("tester1", agent_bin="claude")
    return agents


def picker_for(project: ProjectInfo, intent: str) -> AttachTargetScreen:
    return AttachTargetScreen(
        project,
        persona="pair",
        intent="existing" if intent == "existing" else "new",
        accounts=accounts_service.overview,
    )


def test_the_picker_orders_its_sections_by_intent_and_highlights_the_first_target(
    project: ProjectInfo, targets: list[FleetAgentStatus]
) -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> tuple[list[str], str, bool]:
        picker = host.screen
        assert isinstance(picker, AttachTargetScreen)
        return option_ids(picker), highlighted_id(picker), isinstance(host.focused, OptionList)

    existing = drive(scenario, dialog=picker_for(project, "existing"))
    assert existing == (
        [
            "section:agents",
            "agent:coder-auth",
            "section:binds",
            "bind:coder2",
            "bind:tester1",
            "section:accounts",
            "account:1",
            "account:2",
        ],
        "agent:coder-auth",  # Agents first, and focused
        True,
    )
    new_ids, new_highlight, _ = drive(scenario, dialog=picker_for(project, "new"))
    assert [i for i in new_ids if i.startswith("section:")] == [
        "section:binds",
        "section:accounts",
        "section:agents",
    ]
    assert new_highlight == "bind:coder2"
    assert sorted(new_ids) == sorted(existing[0])  # every section selectable either way


def test_the_filter_narrows_every_section_and_agent_rows_carry_the_persona(
    project: ProjectInfo, targets: list[FleetAgentStatus]
) -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> tuple[dict[str, str], list[str]]:
        picker = host.screen
        assert isinstance(picker, AttachTargetScreen)
        prompts = option_prompts(picker)
        picker.query_one("#picker-filter", Input).value = "two@"
        await pilot.pause()
        return prompts, option_ids(picker)

    prompts, filtered = drive(scenario, dialog=picker_for(project, "existing"))
    assert prompts["agent:coder-auth"] == "coder-auth · coder · waiting · mentor"
    assert prompts["bind:coder2"] == "coder2 · claude2 · .claude2"
    assert prompts["bind:tester1"] == "tester1 · claude · this shell's account"
    assert prompts["account:1"] == "1 · me@example.com · max"
    assert filtered == [
        "section:agents",
        "empty:agents",
        "section:binds",
        "empty:binds",
        "section:accounts",
        "account:2",
    ]


def test_choosing_an_agent_confirms_then_attaches_and_says_how_it_was_delivered(
    project: ProjectInfo,
    catalogue: dict[str, Path],
    targets: list[FleetAgentStatus],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def attach(target: ProjectInfo, label: str, name: str) -> fleet_service.AttachReceipt:
        calls.append((target.id, label, name))
        agent = targets[0].agent.model_copy(update={"persona": name})
        return fleet_service.AttachReceipt(
            agent=agent, persona=name, replaced="mentor", delivered="noted", how="a board note"
        )

    monkeypatch.setattr(fleet_service, "attach_persona", attach)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-attach-existing")
        await wait_for(pilot, AttachTargetScreen)
        await settle(pilot)
        await pilot.press("enter")  # the highlighted agent
        confirm = await wait_for(pilot, ConfirmAttachScreen)
        question = shown(confirm.query_one("#attach-question", Static))
        await press(pilot, "#attach-confirm")
        await settle(pilot)
        return [question, host.notices, type(host.screen).__name__]

    question, notices, screen = drive(scenario, project=project)
    assert question.startswith("Attach pair to coder-auth?") and "It replaces mentor." in question
    assert calls == [(project.id, "coder-auth", "pair")]
    assert ("✓ attached pair to coder-auth (noted)", "information") in notices
    assert screen == "Screen"


def test_cancelling_the_attach_confirmation_attaches_nothing(
    project: ProjectInfo,
    catalogue: dict[str, Path],
    targets: list[FleetAgentStatus],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(fleet_service, "attach_persona", lambda *a, **k: calls.append(a))

    async def scenario(pilot: Pilot[None], host: Host) -> None:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-attach-existing")
        await wait_for(pilot, AttachTargetScreen)
        await settle(pilot)
        await pilot.press("enter")
        await wait_for(pilot, ConfirmAttachScreen)
        await press(pilot, "#attach-cancel")
        await settle(pilot)

    drive(scenario, project=project)
    assert calls == []


@pytest.mark.parametrize("pick", ["bind:coder2", "account:2"])
def test_choosing_a_bind_or_an_account_opens_the_spawn_dialog_preset_with_the_persona(
    pick: str,
    project: ProjectInfo,
    catalogue: dict[str, Path],
    targets: list[FleetAgentStatus],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns: list[tuple[str, dict[str, object]]] = []

    def spawn(target: ProjectInfo, role: str, **kwargs: object) -> fleet_service.SpawnReceipt:
        spawns.append((role, kwargs))
        return fleet_service.SpawnReceipt(
            agent=targets[0].agent, asked_label=None, tmux_session="asq-amber-otter"
        )

    monkeypatch.setattr(fleet_service, "spawn", spawn)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-attach-new")
        picker = await wait_for(pilot, AttachTargetScreen)
        await settle(pilot)
        listing = picker.query_one("#picker-list", OptionList)
        listing.highlighted = option_ids(picker).index(pick)
        await pilot.press("enter")
        dialog = await wait_for(pilot, SpawnDialog)
        await settle(pilot)
        shown_values = [
            dialog.query_one("#spawn-role", Select).value,
            dialog.query_one("#spawn-binary", Input).value,
            dialog.query_one("#spawn-account", Select).value,
            dialog.query_one("#spawn-persona", Select).value,
        ]
        await press(pilot, "#spawn-submit")
        await settle(pilot)
        return [shown_values, host.spawned]

    shown_values, spawned = drive(scenario, project=project)
    ((role, kwargs),) = spawns
    if pick == "bind:coder2":
        assert shown_values == ["coder2", "claude2", "", "pair"]
        assert role == "coder2" and kwargs["binary"] == "claude2"
    else:
        assert shown_values == ["coder", "", "2", "pair"]
        assert role == "coder" and kwargs["account"] == "2"
    assert kwargs["persona"] == "pair"  # a preset is a choice: it is sent
    assert len(spawned) == 1  # the receipt went on to the shell's receipt path


def test_new_bind_checks_seat_and_binary_then_saves_through_bind_role_and_selects_it(
    project: ProjectInfo, targets: list[FleetAgentStatus], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[tuple[str, dict[str, object]]] = []
    real_bind = settings_service.bind_role

    def bind_role(role: str, **kwargs: Any) -> Any:
        saved.append((role, kwargs))
        return real_bind(role, **kwargs)

    monkeypatch.setattr(settings_service, "bind_role", bind_role)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        picker = host.screen
        assert isinstance(picker, AttachTargetScreen)
        await press(pilot, "#picker-new-bind")
        form = await wait_for(pilot, NewBindScreen)
        save = form.query_one("#bind-save", Button)
        rule = form.query_one("#bind-rule", Static)
        seen: list[Any] = []
        form.query_one("#bind-seat", Input).value = "Bad Seat"
        await pilot.pause()
        seen.append((save.disabled, SEAT_RULE in shown(rule)))
        form.query_one("#bind-seat", Input).value = "coder3"
        form.query_one("#bind-binary", Input).value = "definitely-not-a-binary-p7"
        await pilot.pause()
        seen.append((save.disabled, "is not on your PATH" in shown(rule)))
        form.query_one("#bind-binary", Input).value = "sh"
        form.query_one("#bind-account", Select).value = "2"
        form.query_one("#bind-env", TextArea).insert("EXTRA=1\n")
        form.query_one("#bind-args", Input).value = "--model opus"
        await pilot.pause()
        seen.append(save.disabled)
        await press(pilot, "#bind-save")
        await wait_for(pilot, AttachTargetScreen)
        await settle(pilot)
        seen.append(highlighted_id(picker))
        return seen

    seen = drive(scenario, dialog=picker_for(project, "new"))
    assert seen[0] == (True, True)  # an unknown seat
    assert seen[1] == (True, True)  # a binary nothing on PATH answers to
    assert seen[2] is False
    assert saved == [
        (
            "coder3",
            {
                "agent_bin": "sh",
                "env": {**accounts_core.launch_env(MANAGED), "EXTRA": "1"},
                "args": ["--model", "opus"],
            },
        )
    ]
    assert seen[3] == "bind:coder3"  # re-read, the new bind selected


def test_new_account_hands_over_to_the_accounts_page(
    project: ProjectInfo, catalogue: dict[str, Path], targets: list[FleetAgentStatus]
) -> None:
    async def from_the_picker(pilot: Pilot[None], host: Host) -> list[object]:
        await press(pilot, "#picker-new-account")
        return list(host.results)

    assert drive(from_the_picker, dialog=picker_for(project, "new")) == [Target("new-account")]

    async def from_the_tab(pilot: Pilot[None], host: Host) -> int:
        await select_row(pilot, host, "user:pair")
        await press(pilot, "#persona-attach-new")
        await wait_for(pilot, AttachTargetScreen)
        await settle(pilot)
        await press(pilot, "#picker-new-account")
        return host.new_accounts

    assert drive(from_the_tab, project=project) == 1
