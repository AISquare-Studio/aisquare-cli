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
import contextlib
import importlib
import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.content import Content
from textual.geometry import Region
from textual.message import Message
from textual.pilot import Pilot
from textual.widgets import Button, DirectoryTree, Input, RichLog, Static
from textual.widgets.directory_tree import DirEntry
from textual.widgets.tree import TreeNode

from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui.views.doctor import DoctorRefreshed, DoctorView, FixApplied
from aisquare.cli.ui.views.onboard import OnboardFailed, OnboardView, ProjectOnboarded, ProjectTree
from aisquare.core.selfcli import CliResult
from aisquare.core.workspace import project_id_for
from aisquare.models import CheckStatus, DoctorCheck, ProjectInfo, SetupReport
from aisquare.services import onboarding
from tests.test_ui_shell import drive, fleet_app, script  # noqa: F401 — `script` is a fixture
from tests.ui_workers import settle_page, settle_until

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
# No doctor row offers a project-scoped fix today; this one stands in for one.
WARN_FACTS = DoctorCheck(
    name="facts",
    status=CheckStatus.warn,
    detail="no project facts seeded",
    fix="Seed them: aisquare project onboard",
)
WARN_TMUX = DoctorCheck(
    name="tmux",
    status=CheckStatus.warn,
    detail="tmux not found",
    fix="Install it: brew install tmux",
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
            "doctor": _result(_checks_json([OK, WARN_CONNECT, WARN_TMUX])),
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


# --------------------------------------------------------------------------- reloading the tree


def _node(tree: DirectoryTree, path: Path) -> TreeNode[DirEntry] | None:
    """The tree's node for ``path``, among every node it has listed, open or not."""
    nodes = [tree.root]
    while nodes:
        node = nodes.pop()
        if node.data is not None and node.data.path == path:
            return node
        nodes.extend(node.children)
    return None


def _listed(tree: DirectoryTree, path: Path) -> bool:
    """``path`` has a node and no reload is still running (it holds the tree's lock to the
    end, the cursor put back last)."""
    return _node(tree, path) is not None and not tree.lock.is_locked


@pytest.mark.parametrize("linked", [False, True], ids=["root", "root-through-a-symlink"])
def test_reload_lists_folders_made_since_and_keeps_what_was_open(
    tmp_path: Path, linked: bool
) -> None:
    home = tmp_path / "home"
    (home / "code" / "old").mkdir(parents=True)
    home = home.resolve()  # the tree lists resolved paths
    root = home
    if linked:
        # macOS's /var is one: the children are listed under /private/var, and a root
        # left unresolved lost the cursor on every reload.
        root = tmp_path / "link"
        try:
            root.symlink_to(home, target_is_directory=True)
        except OSError:
            pytest.skip("no symlinks here (Windows without the privilege)")
    code, old = home / "code", home / "code" / "old"
    cloned, later = home / "cloned", home / "code" / "later"

    async def drive_view() -> dict[str, object]:
        view = OnboardView(root, run=Scripted({}), validate=_validate)
        async with Host(view).run_test(size=(120, 40)) as pilot:
            tree = view.query_one("#onboard-tree", ProjectTree)
            await _settle(pilot, lambda: _listed(tree, code))
            folder = _node(tree, code)
            assert folder is not None
            folder.expand()
            await _settle(pilot, lambda: _listed(tree, old))
            opened = _node(tree, old)
            assert opened is not None
            tree.move_cursor(opened)
            await pilot.pause()
            cloned.mkdir()
            later.mkdir()
            await pilot.pause(0.1)
            unread = (_node(tree, cloned), _node(tree, later))
            await pilot.click("#onboard-reload")
            await _settle(pilot, lambda: _listed(tree, cloned) and _listed(tree, later))
            folder = _node(tree, code)
            cursor = tree.cursor_node
            seen: dict[str, object] = {
                "unread": unread,
                "open": folder is not None and folder.is_expanded,
                "cursor": cursor.data.path if cursor and cursor.data else None,
            }
            fresh = _node(tree, cloned)
            assert fresh is not None
            tree.move_cursor(fresh)
            tree.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            seen["typed"] = view.query_one("#onboard-path", Input).value
            seen["verdict"] = _text(view.query_one("#onboard-verdict", Static))
            return seen

    seen = asyncio.run(drive_view())
    assert seen["unread"] == (None, None), "control: the tree read the disk once, at mount"
    assert seen["open"], "an open folder stays open across the reload"
    assert seen["cursor"] == old, "and the cursor stays on the row it was on"
    assert seen["typed"] == str(cloned), "the new folder can be selected"
    assert str(seen["verdict"]).startswith("✓ will register")


def test_ctrl_r_reloads_and_judges_the_typed_path_again(tmp_path: Path) -> None:
    later = tmp_path / "later"

    async def drive_view() -> tuple[str, str, str, bool, str | None]:
        view = OnboardView(tmp_path, run=Scripted({}), validate=_validate)
        async with Host(view).run_test(size=(120, 40)) as pilot:
            tree = view.query_one("#onboard-tree", ProjectTree)
            verdict = view.query_one("#onboard-verdict", Static)
            box = view.query_one("#onboard-path", Input)
            await _settle(pilot, lambda: not tree.lock.is_locked)
            box.value = str(later)
            box.focus()
            await pilot.pause()
            missing = _text(verdict)
            later.mkdir()
            await pilot.pause(0.1)
            stale = _text(verdict)
            await pilot.press("ctrl+r")  # from the path box, which takes every printable key
            await _settle(pilot, lambda: _listed(tree, later))
            focused = view.app.focused
            return (
                missing,
                stale,
                _text(verdict),
                view.query_one("#onboard-run", Button).disabled,
                focused.id if focused is not None else None,
            )

    missing, stale, after, disabled, focused = asyncio.run(drive_view())
    assert missing.startswith("✗") and "does not exist" in missing
    assert stale == missing, "control: nothing judges the path again on its own"
    assert after.startswith("✓ will register") and str(later) in after
    assert not disabled, "the folder now exists: the button is armed"
    assert focused == "onboard-path", "the keyboard stays in the box"


async def _until(done: Callable[[], bool], *, timeout: float = 5.0) -> bool:
    """Whether ``done()`` holds within ``timeout`` seconds, looked for with ``asyncio.sleep``
    alone: a tree whose message pump is stuck stalls every ``Pilot.pause``."""
    deadline = time.monotonic() + timeout
    while not done():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def test_a_folder_opened_as_a_reload_runs_does_not_stop_the_tree(tmp_path: Path) -> None:
    """Why ``reload_node`` on the root, not the tree's own ``reload``.

    That one gives the tree a new load queue and cancels its loader. A folder opened
    while a reload holds the tree's lock is taken off the old queue by the loader, which
    then waits on the lock: cancelled there, it is never marked done, the tree waits on it
    for ever, and no folder opened after it is listed.
    """
    for name in ("a", "b"):
        (tmp_path / name / "inner").mkdir(parents=True)

    async def drive_view() -> tuple[bool, bool]:
        view = OnboardView(tmp_path, run=Scripted({}), validate=_validate)
        async with Host(view).run_test(size=(120, 40)) as pilot:
            tree = view.query_one("#onboard-tree", ProjectTree)
            await _settle(pilot, lambda: _listed(tree, tmp_path / "b"))
            stranded = tree._load_queue  # the queue the folder opened next is put on
            await tree.lock.acquire()  # as a reload still listing a slow folder holds it
            try:
                first = _node(tree, tmp_path / "a")
                assert first is not None
                first.expand()
                await asyncio.sleep(0.2)  # the loader has taken it and waits on the lock
                view.reload()
                await asyncio.sleep(0)
            finally:
                tree.lock.release()
            reopened = await _until(lambda: _listed(tree, tmp_path / "a" / "inner"))
            then = _node(tree, tmp_path / "b")
            assert then is not None
            then.expand()
            listed = await _until(lambda: _listed(tree, tmp_path / "b" / "inner"))
            if not listed:
                # Mark the stranded folder done by hand, or the stuck tree never lets the
                # app close either: this fails rather than hangs.
                with contextlib.suppress(ValueError):
                    while True:
                        stranded.task_done()
            return reopened, listed

    reopened, listed = asyncio.run(drive_view())
    assert reopened, "the folder opened as the reload ran is listed, and stays open"
    assert listed, "and a folder opened after the reload is listed too"


def test_a_reload_while_onboarding_leaves_the_run_its_log_and_its_button(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    cloned = tmp_path / "cloned"
    scripted = Scripted({"init": _result(_report(proj)), "doctor": _result(_checks_json([OK]))})
    started, release = threading.Event(), threading.Event()

    def held(args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
        if "init" in args:
            started.set()
            release.wait(30)
        return scripted(args, cwd=cwd)

    async def drive_view() -> dict[str, object]:
        view = OnboardView(tmp_path, run=held, validate=_validate)
        host = Host(view)
        async with host.run_test(size=(120, 50)) as pilot:
            tree = view.query_one("#onboard-tree", ProjectTree)
            run_button = view.query_one("#onboard-run", Button)
            log = view.query_one("#onboard-log", RichLog)
            view.query_one("#onboard-path", Input).value = str(proj)
            await pilot.pause()
            try:
                await pilot.click("#onboard-run")
                await _settle(pilot, started.is_set)
                before = (list(view.log_lines), len(log.lines))
                cloned.mkdir()
                await pilot.click("#onboard-reload")
                await _settle(pilot, lambda: _listed(tree, cloned))
                during = {
                    "running": view.running,
                    "log": (list(view.log_lines), len(log.lines)),
                    "run disabled": run_button.disabled,
                    "reload disabled": view.query_one("#onboard-reload", Button).disabled,
                }
            finally:
                release.set()
            await _settle(pilot, lambda: view.outcome is not None)
            return {
                "before": before,
                "during": during,
                "received": [type(m).__name__ for m in host.received],
                "log": list(view.log_lines),
                "run disabled": run_button.disabled,
            }

    seen = asyncio.run(drive_view())
    during = seen["during"]
    assert isinstance(during, dict)
    assert during["running"], "the run goes on"
    assert during["log"] == seen["before"], "its log is not touched"
    assert during["run disabled"], "nor its button, which stays off until the run ends"
    assert not during["reload disabled"], "↻ is allowed during a run"
    assert seen["received"] == ["ProjectOnboarded"]
    assert "$ aisquare --json doctor" in list(seen["log"])  # type: ignore[call-overload]
    assert not seen["run disabled"], "the run's end arms the button as ever"


def _home(tmp_path: Path) -> Path:
    """A home directory with one folder in it, resolved as the tree lists it."""
    home = (tmp_path / "home").resolve()
    (home / "old").mkdir(parents=True)
    return home


def _onboard_at(home: Path, monkeypatch: pytest.MonkeyPatch) -> type[OnboardView]:
    """The shell's Onboard view, rooted at ``home`` rather than the real home directory."""

    class AtHome(OnboardView):
        def __init__(self, **options: object) -> None:
            super().__init__(home, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(ui_app, "OnboardView", AtHome)
    return AtHome


def test_opening_onboard_again_lists_the_folders_made_since(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: object,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
) -> None:
    """The shell builds the Onboard view on the first ``+`` and keeps it: the second ``+``
    shows the same view, and a folder cloned while it was away is in its tree."""
    home = _home(tmp_path)
    AtHome = _onboard_at(home, monkeypatch)
    cloned = home / "cloned"

    async def fn(pilot: Pilot[None]) -> tuple[object, bool, str | None, bool]:
        app = fleet_app(pilot)
        await pilot.press("plus")
        await settle_page(app)
        view = app.query_one(AtHome)
        tree = view.query_one(ProjectTree)
        await settle_until(app, lambda: _listed(tree, home / "old"))
        app.sidebar.focus()
        await pilot.press("w")
        await settle_page(app)
        cloned.mkdir()
        await settle_page(app)
        away = _node(tree, cloned)
        app.sidebar.focus()
        await pilot.press("plus")
        await settle_until(app, lambda: _listed(tree, cloned))
        kept = app.query_one(AtHome) is view
        return away, kept, app.content.current, _node(tree, cloned) is not None

    away, kept, current, listed = drive(fn)
    assert away is None, "control: a view not shown does not read the disk"
    assert kept, "the second + shows the view the first one built"
    assert current == "onboard"
    assert listed, "and its tree lists the folder cloned since"


def test_at_80_columns_the_reload_button_is_on_the_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: object,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
) -> None:
    """The narrowest terminal the shell is laid out for: its content pane is 45 columns
    and the hint 48, so a ↻ after the hint sat at x=82 on an 80-column screen."""
    _onboard_at(_home(tmp_path), monkeypatch)

    async def fn(pilot: Pilot[None]) -> tuple[Region, Region, Region, Region]:
        app = fleet_app(pilot)
        await pilot.press("plus")
        await settle_page(app)
        head = app.query_one("#onboard-head")
        return (
            app.screen.region,
            head.region,
            app.query_one("#onboard-reload", Button).region,
            app.query_one("#onboard-head .hint", Static).region,
        )

    screen, head, button, hint = drive(fn, size=(80, 24))
    assert button.area and screen.contains_region(button), f"↻ at {button} on {screen}"
    assert head.contains_region(button), f"↻ at {button} is inside its line, {head}"
    assert head.contains_region(hint), f"the hint, {hint}, wraps rather than clips in {head}"


def test_ctrl_r_reaches_the_view_from_each_of_its_widgets_and_nothing_outside_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: object,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
) -> None:
    """Read off the mounted shell's binding chain: from every widget of the view that can
    take the keyboard, ctrl+r is the view's reload (no widget of its own takes it first);
    from the sidebar, outside the view, nothing has it, and ``r`` and ``R`` are still the
    shell's refresh and remote."""
    home = _home(tmp_path)
    _onboard_at(home, monkeypatch)
    cloned = home / "cloned"

    async def fn(
        pilot: Pilot[None],
    ) -> tuple[dict[str, tuple[bool, str] | None], dict[str, str | None], bool]:
        app = fleet_app(pilot)
        await pilot.press("plus")
        await settle_page(app)
        view = app.query_one(OnboardView)
        box = view.query_one("#onboard-path", Input)
        box.value = str(home / "old")  # arms Onboard: one more widget to take the keyboard
        await settle_page(app)
        reached: dict[str, tuple[bool, str] | None] = {}
        for widget in [w for w in view.query("*") if w.focusable]:
            widget.focus()
            await settle_page(app)
            active = app.active_bindings.get("ctrl+r")
            reached[widget.id or type(widget).__name__] = (
                None if active is None else (active.node is view, active.binding.action)
            )
        app.sidebar.focus()
        await settle_page(app)
        outside = {
            key: (active.binding.action if (active := app.active_bindings.get(key)) else None)
            for key in ("ctrl+r", "r", "R")
        }
        cloned.mkdir()
        box.focus()
        await settle_page(app)
        await pilot.press("ctrl+r")
        tree = view.query_one(ProjectTree)
        await settle_until(app, lambda: _listed(tree, cloned))
        return reached, outside, _node(tree, cloned) is not None

    reached, outside, listed = drive(fn)
    assert {"onboard-path", "onboard-tree", "onboard-reload", "onboard-run"} <= set(reached)
    assert set(reached.values()) == {(True, "reload")}, reached
    assert outside == {"ctrl+r": None, "r": "refresh_now", "R": "remote_panel"}
    assert listed, "pressed in the path box, ctrl+r reloads the tree"


# --------------------------------------------------------------------------- DoctorView


def test_doctor_view_renders_a_button_only_for_known_fixes(tmp_path: Path) -> None:
    async def drive() -> tuple[str, list[tuple[str, bool]]]:
        view = DoctorView(cwd=tmp_path, run=Scripted({}))
        async with Host(view).run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view.show([OK, WARN_CONNECT, WARN_TMUX, WARN_FACTS])
            await pilot.pause()
            buttons = [
                (str(b.label), b.disabled)
                for b in view.query("#doctor-fixes Button").results(Button)
            ]
            return _text(view.query_one("#doctor-report", Static)), buttons

    report, buttons = asyncio.run(drive())
    assert "⚠ claude-code: hooks are missing" in report and "→ (Re)connect it" in report
    assert "→ Install it: brew install tmux" in report, "an unknown fix stays text"
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

    from aisquare.services import install_route

    label, tooltip = asyncio.run(drive())
    # The label names the command as a shell would read it, quoted where its path needs it.
    argv = ["aisquare", "agents", "connect", "claude-code", "--config-dir"]
    expected = install_route.command_line([*argv, "/home/me/[archive]/.claude"])
    assert label == expected
    assert "[archive]" in label and "[archive]" in tooltip
    # Control: the same string handed to Textual AS A STR loses the segment — this
    # is the failure the assertion above exists to catch, and it is real.
    assert Content.from_text(expected).plain == expected.replace("[archive]", "")


def test_doctor_view_without_a_cwd_disables_project_scoped_fixes() -> None:
    async def drive() -> list[tuple[str, bool]]:
        view = DoctorView(run=Scripted({}))
        async with Host(view).run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            view.show([WARN_CONNECT, WARN_FACTS])
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
