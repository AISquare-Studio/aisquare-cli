"""The Onboard and Doctor views, driven headless through Textual's Pilot.

The runner is the same scripted fake as ``test_onboarding.py`` uses: what these
tests pin is that the WIDGETS ask for the right command in the right cwd, post
the messages the shell relies on (``ProjectOnboarded`` / ``OnboardFailed`` /
``DoctorRefreshed``), and show the reason when something goes wrong — with a
negative control beside each positive one.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.content import Content
from textual.message import Message
from textual.pilot import Pilot
from textual.widgets import Button, DirectoryTree, Input, Static

from aisquare.cli.ui.views.doctor import DoctorRefreshed, DoctorView, FixApplied
from aisquare.cli.ui.views.onboard import OnboardFailed, OnboardView, ProjectOnboarded, ProjectTree
from aisquare.core.selfcli import CliResult
from aisquare.core.workspace import project_id_for
from aisquare.models import CheckStatus, DoctorCheck, ProjectInfo, SetupReport
from aisquare.services import onboarding

# --------------------------------------------------------------------------- fakes


@dataclass
class Scripted:
    answers: dict[str, CliResult]
    calls: list[tuple[list[str], Path | None]] = field(default_factory=list)

    def __call__(self, args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
        argv = list(args)
        self.calls.append((argv, cwd))
        words = [word for word in argv if word != "--json"]
        answer = self.answers[words[0]]
        return CliResult(
            argv=argv, returncode=answer.returncode, stdout=answer.stdout, stderr=answer.stderr
        )


def _result(stdout: str = "", *, code: int = 0, stderr: str = "") -> CliResult:
    return CliResult(argv=[], returncode=code, stdout=stdout, stderr=stderr)


def _report(root: Path) -> str:
    project = ProjectInfo(id=project_id_for(root), root=root)
    return SetupReport(
        home=root / ".home", already_initialized=False, project=project
    ).model_dump_json()


def _checks_json(checks: Sequence[DoctorCheck]) -> str:
    return json.dumps([check.model_dump(mode="json") for check in checks])


OK = DoctorCheck(name="python", status=CheckStatus.ok, detail="3.12")
WARN_CONNECT = DoctorCheck(
    name="claude-code",
    status=CheckStatus.warn,
    detail="hooks are missing",
    fix="(Re)connect it: aisquare agents connect claude-code",
)
WARN_SNAPSHOT = DoctorCheck(
    name="snapshot",
    status=CheckStatus.warn,
    detail="no codebase snapshot",
    fix="Pack one: aisquare project onboard",
)
WARN_REPOMIX = DoctorCheck(
    name="repomix",
    status=CheckStatus.warn,
    detail="repomix not found",
    fix="Install Node.js, then: npm install -g repomix",
)
CONNECTED = DoctorCheck(name="claude-code", status=CheckStatus.ok, detail="connected")


class Host(App[None]):
    """Mounts one view and records every message it posts to its parent."""

    def __init__(self, view: OnboardView | DoctorView) -> None:
        super().__init__()
        self.view = view
        self.received: list[Message] = []

    def compose(self) -> ComposeResult:
        yield self.view

    def on_project_onboarded(self, message: ProjectOnboarded) -> None:
        self.received.append(message)

    def on_onboard_failed(self, message: OnboardFailed) -> None:
        self.received.append(message)

    def on_doctor_refreshed(self, message: DoctorRefreshed) -> None:
        self.received.append(message)

    def on_fix_applied(self, message: FixApplied) -> None:
        self.received.append(message)


def _text(static: Static) -> str:
    return str(static.render())


def _no_store(project_id: str) -> ProjectInfo | None:
    return None


def _validate(text: str) -> onboarding.PathVerdict:
    return onboarding.validate_path(text, lookup=_no_store)


async def _settle(pilot: Pilot[None], done: Callable[[], bool], *, timeout: float = 15.0) -> None:
    """Poll until the view's worker has reported back on the UI thread.

    Not ``workers.wait_for_complete()``: that waits on EVERY worker, and the
    ``DirectoryTree`` runs a loader worker for the life of the widget, so a test
    with a tree in it would wait forever (it did — a hang, found by timeout).
    """
    deadline = time.monotonic() + timeout
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError("the worker never reported back to the UI thread")
        await pilot.pause(0.05)
    await pilot.pause()


# --------------------------------------------------------------------------- OnboardView


def test_typing_a_path_updates_the_verdict_and_the_button(tmp_path: Path) -> None:
    (tmp_path / "proj").mkdir()

    async def drive() -> tuple[str, bool, str, bool, str, bool]:
        view = OnboardView(tmp_path, run=Scripted({}), validate=_validate)
        async with Host(view).run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            verdict = view.query_one("#onboard-verdict", Static)
            button = view.query_one("#onboard-run", Button)
            blank = (_text(verdict), button.disabled)
            # The directory part is set, the name is TYPED: every keystroke re-judges
            # the path (a `git rev-parse` each), and the claim is about the keystrokes.
            view.query_one("#onboard-path", Input).value = str(tmp_path) + "/"
            await pilot.click("#onboard-path")
            await pilot.press("end")
            for char in "proj":
                await pilot.press(char)
            await pilot.pause()
            good = (_text(verdict), button.disabled)
            for char in "-nope":
                await pilot.press(char)
            await pilot.pause()
            bad = (_text(verdict), button.disabled)
            return (*blank, *good, *bad)

    blank_text, blank_disabled, good_text, good_disabled, bad_text, bad_disabled = asyncio.run(
        drive()
    )
    assert "type a path" in blank_text and blank_disabled
    assert good_text.startswith("✓ will register") and str(tmp_path / "proj") in good_text
    assert not good_disabled, "a valid directory arms the button"
    assert bad_text.startswith("✗") and "does not exist" in bad_text
    assert bad_disabled, "an invalid path disarms it again"


def test_selecting_in_the_tree_fills_the_input(tmp_path: Path) -> None:
    (tmp_path / "proj").mkdir()

    async def drive() -> tuple[str, str]:
        view = OnboardView(tmp_path, run=Scripted({}), validate=_validate)
        async with Host(view).run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            tree = view.query_one("#onboard-tree", DirectoryTree)
            tree.post_message(DirectoryTree.DirectorySelected(tree.root, tmp_path / "proj"))
            await pilot.pause()
            await pilot.pause()
            path = view.query_one("#onboard-path", Input).value
            return path, _text(view.query_one("#onboard-verdict", Static))

    path, verdict = asyncio.run(drive())
    assert path == str(tmp_path / "proj")
    assert verdict.startswith("✓ will register")


def test_the_tree_hides_dotfiles_unless_asked(tmp_path: Path) -> None:
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "shown").mkdir()

    async def names(show_hidden: bool) -> list[str]:
        view = OnboardView(tmp_path, run=Scripted({}), validate=_validate, show_hidden=show_hidden)
        async with Host(view).run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            tree = view.query_one("#onboard-tree", ProjectTree)
            return sorted(path.name for path in tree.filter_paths(tmp_path.iterdir()))

    assert asyncio.run(names(False)) == ["shown"], "dotfiles are hidden by default"
    assert asyncio.run(names(True)) == [".hidden", "shown"], "and shown when asked"


def test_pressing_onboard_runs_init_and_doctor_and_posts_project_onboarded(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    run = Scripted(
        {
            "init": _result(_report(proj)),
            "doctor": _result(_checks_json([OK, WARN_CONNECT, WARN_REPOMIX])),
        }
    )

    async def drive() -> tuple[list[Message], list[str], bool, list[str], bool]:
        view = OnboardView(tmp_path, run=run, validate=_validate)
        host = Host(view)
        async with host.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            view.query_one("#onboard-path", Input).value = str(proj)
            await pilot.pause()
            await pilot.click("#onboard-run")
            await _settle(pilot, lambda: view.outcome is not None)
            doctor = view.query_one("#onboard-doctor", DoctorView)
            labels = [str(b.label) for b in doctor.query("#doctor-fixes Button").results(Button)]
            return host.received, list(view.log_lines), doctor.display, labels, view.running

    received, log, doctor_shown, fix_labels, running = asyncio.run(drive())
    assert run.calls == [
        (["--json", "init", "--no-explainability", str(proj)], proj),
        (["--json", "doctor"], proj),
    ]
    assert [type(m).__name__ for m in received] == ["ProjectOnboarded"]
    onboarded = received[0]
    assert isinstance(onboarded, ProjectOnboarded)
    assert onboarded.project_id == project_id_for(proj) and onboarded.path == proj
    assert log[0] == f"$ aisquare --json init --no-explainability {proj}"
    assert "$ aisquare --json doctor" in log
    assert any("⚠ claude-code" in line for line in log)
    assert doctor_shown, "the findings appear under the log"
    assert fix_labels == ["aisquare agents connect claude-code"], (
        "one button for the known fix, none for npm"
    )
    assert not running


def test_a_failing_runner_posts_onboard_failed_with_the_reason(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    run = Scripted(
        {
            "init": _result(
                json.dumps({"error": "store_unopenable", "detail": "file is not a database"}),
                code=1,
            )
        }
    )

    async def drive() -> tuple[list[Message], list[str], bool, bool]:
        view = OnboardView(tmp_path, run=run, validate=_validate)
        host = Host(view)
        async with host.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view.query_one("#onboard-path", Input).value = str(proj)
            await pilot.pause()
            await pilot.click("#onboard-run")
            await _settle(pilot, lambda: view.outcome is not None)
            doctor = view.query_one("#onboard-doctor", DoctorView)
            button = view.query_one("#onboard-run", Button)
            return host.received, list(view.log_lines), doctor.display, button.disabled

    received, log, doctor_shown, disabled = asyncio.run(drive())
    assert len(run.calls) == 1, "doctor never runs after a failed init"
    assert [type(m).__name__ for m in received] == ["OnboardFailed"]
    failed = received[0]
    assert isinstance(failed, OnboardFailed)
    assert failed.path == proj
    assert "init failed" in failed.reason and "store_unopenable" in failed.reason
    assert "file is not a database" in failed.reason
    assert log[-1] == f"✗ {failed.reason}", "the reason is the last line of the log the user sees"
    assert not doctor_shown, "no findings for a project that was not registered"
    assert not disabled, "the user can fix the cause and press Onboard again"


def test_a_crashing_runner_is_an_onboard_failed_not_an_app_crash(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()

    def explode(args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
        raise OSError("no interpreter")

    async def drive() -> list[Message]:
        view = OnboardView(tmp_path, run=explode, validate=_validate)
        host = Host(view)
        async with host.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view.query_one("#onboard-path", Input).value = str(proj)
            await pilot.pause()
            assert view.start()
            await _settle(pilot, lambda: view.outcome is not None)
            return host.received

    received = asyncio.run(drive())
    assert len(received) == 1 and isinstance(received[0], OnboardFailed)
    assert "no interpreter" in received[0].reason


def test_start_refuses_without_a_valid_path(tmp_path: Path) -> None:
    run = Scripted({})

    async def drive() -> bool:
        view = OnboardView(tmp_path, run=run, validate=_validate)
        async with Host(view).run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view.query_one("#onboard-path", Input).value = str(tmp_path / "missing")
            await pilot.pause()
            return view.start()

    assert asyncio.run(drive()) is False
    assert run.calls == [], "nothing runs for a path that does not exist"


# --------------------------------------------------------------------------- DoctorView


def test_doctor_view_renders_a_button_only_for_known_fixes(tmp_path: Path) -> None:
    async def drive() -> tuple[str, list[tuple[str, bool]]]:
        view = DoctorView(cwd=tmp_path, run=Scripted({}))
        async with Host(view).run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view.show([OK, WARN_CONNECT, WARN_REPOMIX, WARN_SNAPSHOT])
            await pilot.pause()
            buttons = [
                (str(b.label), b.disabled)
                for b in view.query("#doctor-fixes Button").results(Button)
            ]
            return _text(view.query_one("#doctor-report", Static)), buttons

    report, buttons = asyncio.run(drive())
    assert "⚠ claude-code: hooks are missing" in report and "→ (Re)connect it" in report
    assert "→ Install Node.js, then: npm install -g repomix" in report, "an unknown fix stays text"
    assert buttons == [
        ("aisquare agents connect claude-code", False),
        ("aisquare project onboard --refresh", False),
    ]


def test_a_bracketed_config_dir_survives_into_the_button(tmp_path: Path) -> None:
    """The label and tooltip are DATA — a path — and a Button parses a str as markup.

    CONTRIBUTING's rule, measured here: a ``--config-dir`` under ``[archive]``
    would otherwise read as a different directory on screen.
    """
    hint = DoctorCheck(
        name="claude-code",
        status=CheckStatus.warn,
        detail="hooks are missing",
        fix="aisquare agents connect claude-code --config-dir /home/me/[archive]/.claude",
    )

    async def drive() -> tuple[str, str]:
        view = DoctorView(cwd=tmp_path, run=Scripted({}))
        async with Host(view).run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view.show([hint])
            await pilot.pause()
            button = view.query_one("#fix-0", Button)
            return str(button.label), str(button.tooltip)

    label, tooltip = asyncio.run(drive())
    expected = "aisquare agents connect claude-code --config-dir /home/me/[archive]/.claude"
    assert label == expected
    assert "[archive]" in tooltip
    # Control: the same string handed to Textual AS A STR loses the segment — this
    # is the failure the assertion above exists to catch, and it is real.
    assert Content.from_text(expected).plain == (
        "aisquare agents connect claude-code --config-dir /home/me//.claude"
    )


def test_doctor_view_without_a_cwd_disables_project_scoped_fixes() -> None:
    async def drive() -> list[tuple[str, bool]]:
        view = DoctorView(run=Scripted({}))
        async with Host(view).run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view.show([WARN_CONNECT, WARN_SNAPSHOT])
            await pilot.pause()
            return [
                (str(b.label), b.disabled)
                for b in view.query("#doctor-fixes Button").results(Button)
            ]

    assert asyncio.run(drive()) == [
        ("aisquare agents connect claude-code", False),
        ("aisquare project onboard --refresh", True),
    ]


def test_clicking_a_fix_runs_it_in_the_cwd_then_refreshes_the_doctor(tmp_path: Path) -> None:
    run = Scripted(
        {
            "agents": _result('{"agent":"claude-code"}'),
            "doctor": _result(_checks_json([OK, CONNECTED])),
        }
    )

    async def drive() -> tuple[list[Message], str, str, int]:
        view = DoctorView(cwd=tmp_path, run=run)
        host = Host(view)
        async with host.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view.show([OK, WARN_CONNECT])
            await pilot.pause()
            await pilot.click("#fix-0")
            await _settle(pilot, lambda: not view.busy)
            await pilot.pause()
            buttons = len(list(view.query("#doctor-fixes Button").results(Button)))
            return (
                host.received,
                _text(view.query_one("#doctor-status", Static)),
                _text(view.query_one("#doctor-report", Static)),
                buttons,
            )

    received, status, report, buttons = asyncio.run(drive())
    assert run.calls == [
        (["--json", "agents", "connect", "claude-code"], tmp_path),
        (["--json", "doctor"], tmp_path),
    ]
    assert [type(m).__name__ for m in received] == ["FixApplied", "DoctorRefreshed"]
    refreshed = received[1]
    assert isinstance(refreshed, DoctorRefreshed)
    assert [c.name for c in refreshed.checks] == [
        "python",
        "claude-code",
    ] and refreshed.cwd == tmp_path
    assert status == "✓ aisquare agents connect claude-code"
    assert "✓ claude-code: connected" in report, "the report is the re-run's, not the old one"
    assert buttons == 0, "a fixed check has no button left"


def test_a_failed_fix_shows_the_reason_and_keeps_the_buttons(tmp_path: Path) -> None:
    run = Scripted(
        {"agents": _result('{"error":"not_installed"}', code=1), "doctor": _result("garbage")}
    )

    async def drive() -> tuple[list[str], str, list[tuple[str, bool]], str]:
        view = DoctorView(cwd=tmp_path, run=run)
        host = Host(view)
        async with host.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view.show([OK, WARN_CONNECT])
            await pilot.pause()
            await pilot.click("#fix-0")
            await _settle(pilot, lambda: not view.busy)
            await pilot.pause()
            buttons = [
                (str(b.label), b.disabled)
                for b in view.query("#doctor-fixes Button").results(Button)
            ]
            return (
                [type(m).__name__ for m in host.received],
                _text(view.query_one("#doctor-status", Static)),
                buttons,
                _text(view.query_one("#doctor-report", Static)),
            )

    kinds, status, buttons, report = asyncio.run(drive())
    assert kinds == ["FixApplied"], "no DoctorRefreshed when the re-check gave no answer"
    assert (
        status.startswith("✗ aisquare agents connect claude-code: ") and "not_installed" in status
    )
    assert "re-check failed, showing the previous report" in status
    assert buttons == [("aisquare agents connect claude-code", False)], (
        "the button is back for another try"
    )
    assert "⚠ claude-code: hooks are missing" in report


def test_a_shell_supplied_refresh_replaces_the_cli_doctor(tmp_path: Path) -> None:
    run = Scripted({"agents": _result("{}")})
    asked_for: list[Path | None] = []

    def refresh(cwd: Path | None) -> list[DoctorCheck]:
        asked_for.append(cwd)
        return [CONNECTED]

    async def drive() -> list[Message]:
        view = DoctorView(cwd=tmp_path, run=run, refresh=refresh)
        host = Host(view)
        async with host.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view.show([WARN_CONNECT])
            await pilot.pause()
            await pilot.click("#fix-0")
            await _settle(pilot, lambda: not view.busy)
            return host.received

    received = asyncio.run(drive())
    assert asked_for == [tmp_path]
    assert [call[0] for call in run.calls] == [["--json", "agents", "connect", "claude-code"]], (
        "no doctor subprocess"
    )
    assert isinstance(received[-1], DoctorRefreshed) and received[-1].checks == [CONNECTED]


def test_show_before_mount_renders_on_mount(tmp_path: Path) -> None:
    async def drive() -> str:
        view = DoctorView(cwd=tmp_path, run=Scripted({}))
        view.show([WARN_CONNECT])
        async with Host(view).run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            return _text(view.query_one("#doctor-report", Static))

    assert "⚠ claude-code" in asyncio.run(drive())


def _imports_subprocess(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == "subprocess" for alias in node.names)
    return isinstance(node, ast.ImportFrom) and node.module == "subprocess"


def _calls_chdir(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name == "chdir"


def _cwd_sites(tree: ast.AST) -> list[int]:
    """Lines that call ``chdir`` (``os.chdir``, a bare ``chdir``) or import ``subprocess``."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt | ast.expr)
        and (_calls_chdir(node) or _imports_subprocess(node))
    ]


def test_the_cwd_matcher_sees_a_chdir_and_ignores_the_word() -> None:
    """Positive control: the guard below is AST-based because the docstrings SAY 'chdir'."""
    assert _cwd_sites(ast.parse("import os\nos.chdir('/x')\n")) == [2]
    assert _cwd_sites(ast.parse("from os import chdir\nchdir('/x')\n")) == [2]
    assert _cwd_sites(ast.parse("import subprocess\n")) == [1]
    assert _cwd_sites(ast.parse('"""never chdir here"""\nimport os\nos.getcwd()\n')) == []


@pytest.mark.parametrize(
    "module", ["aisquare.cli.ui.views.onboard", "aisquare.cli.ui.views.doctor"]
)
def test_the_views_never_change_directory(module: str) -> None:
    """§5.6: the UI process hosts many projects; cwd is a subprocess argument, never ours."""
    path = Path(importlib.import_module(module).__file__ or "")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert _cwd_sites(tree) == []
