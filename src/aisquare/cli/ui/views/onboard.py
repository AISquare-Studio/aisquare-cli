"""The Onboard view: pick a directory, run `init` + `doctor` in the background, list it.

docs/plans/fleet-tui.md §0 item 3, §4.2 and §5.6. A ``DirectoryTree`` rooted at
the home directory beside a path ``Input`` (selecting or typing fills it), a live
verdict line under it (which root will be registered, "already registered",
"not a directory"), and an **Onboard** button that runs
:func:`aisquare.services.onboarding.onboard` in a thread worker — the CLI as a
subprocess with ``cwd=<path>``; this process never ``chdir``s — streaming its
log into a ``RichLog``. On success the view posts :class:`ProjectOnboarded`
(the shell selects the card) and shows the doctor's findings with fix buttons
through an embedded :class:`DoctorView`; on failure :class:`OnboardFailed` with
the reason. It never prompts: ``init`` is non-interactive and the explainability
consent is one of those buttons, never a question.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, DirectoryTree, Input, RichLog, Static

from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.core import selfcli
from aisquare.services import onboarding
from aisquare.services.onboarding import OnboardOutcome, PathVerdict, Runner

Validate = Callable[[str], PathVerdict]


class ProjectOnboarded(Message):
    """A project was initialised and is ready to be selected."""

    def __init__(self, project_id: str, path: Path) -> None:
        self.project_id = project_id
        self.path = path
        super().__init__()


class OnboardFailed(Message):
    """Onboarding stopped; ``reason`` is what to show the user."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__()


class ProjectTree(DirectoryTree):
    """The home directory, dotfiles hidden unless asked — a project lives in the open."""

    def __init__(self, path: Path, *, show_hidden: bool = False, id: str | None = None) -> None:
        super().__init__(path, id=id)
        self.show_hidden = show_hidden

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        if self.show_hidden:
            return paths
        return [path for path in paths if not path.name.startswith(".")]


def render_verdict(verdict: PathVerdict) -> Text:
    """The verdict line, styled by what it says; the words come from the service."""
    if verdict.path is None:
        return Text(verdict.describe(), style="dim")
    if not verdict.ok:
        return Text(f"✗ {verdict.describe()}", style="red")
    style = "yellow" if verdict.registered is not None or verdict.store_error else "green"
    return Text(f"✓ {verdict.describe()}", style=style)


class OnboardView(Vertical):
    """Directory picker + path input + a live log of `init` and `doctor`."""

    DEFAULT_CSS = """
    OnboardView { padding: 1 2; }
    OnboardView #onboard-body { height: 1fr; }
    OnboardView #onboard-tree { width: 40%; min-width: 24; border-right: solid $primary; }
    OnboardView #onboard-form { width: 1fr; padding: 0 0 0 1; }
    OnboardView #onboard-path { margin-bottom: 1; }
    OnboardView #onboard-verdict { height: auto; margin-bottom: 1; }
    OnboardView #onboard-run { margin-bottom: 1; }
    OnboardView #onboard-log { height: 1fr; min-height: 4; border: round $primary; }
    OnboardView #onboard-doctor { height: auto; max-height: 50%; padding: 0; }
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        run: Runner = selfcli.run,
        validate: Validate = onboarding.validate_path,
        show_hidden: bool = False,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.root = root if root is not None else Path.home()
        self._run = run
        self._validate = validate
        self.show_hidden = show_hidden
        self.verdict: PathVerdict = validate("")
        self.outcome: OnboardOutcome | None = None
        self.log_lines: list[str] = []
        self.running = False

    def compose(self) -> ComposeResult:
        yield Static("Onboard a project — type a directory, or browse:", classes="hint")
        with Horizontal(id="onboard-body"):
            yield ProjectTree(self.root, show_hidden=self.show_hidden, id="onboard-tree")
            with Vertical(id="onboard-form"):
                yield Input(placeholder="~/Code/your-repo", id="onboard-path")
                yield Static(render_verdict(self.verdict), id="onboard-verdict")
                yield Button("Onboard", variant="primary", id="onboard-run", disabled=True)
                yield RichLog(id="onboard-log", wrap=True, markup=False, highlight=False)
                doctor = DoctorView(run=self._run, id="onboard-doctor")
                doctor.display = False
                yield doctor

    # ---------------------------------------------------------------- the path

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        event.stop()
        self.query_one("#onboard-path", Input).value = str(event.path)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        event.stop()  # the verdict will say "a file, not a directory" — better than guessing
        self.query_one("#onboard-path", Input).value = str(event.path)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "onboard-path":
            return
        event.stop()
        self.set_path(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "onboard-path":
            return
        event.stop()
        self.start()

    def set_path(self, text: str) -> None:
        """Re-judge ``text`` and show the verdict; the button follows it."""
        try:
            self.verdict = self._validate(text)
        except Exception as exc:  # a verdict must never take the view down
            self.verdict = PathVerdict(
                text=text,
                path=None,
                exists=False,
                is_dir=False,
                root=None,
                registered=None,
                is_git=False,
                store_error=str(exc) or type(exc).__name__,
            )
        self.query_one("#onboard-verdict", Static).update(render_verdict(self.verdict))
        self.query_one("#onboard-run", Button).disabled = not self.verdict.ok or self.running

    # ---------------------------------------------------------------- the run

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "onboard-run":
            return
        event.stop()
        self.start()

    def start(self) -> bool:
        """Begin onboarding the current path; False when there is nothing valid to run."""
        if self.running or not self.verdict.ok or self.verdict.path is None:
            return False
        self.running = True
        self.outcome = None
        self.log_lines = []
        self.query_one("#onboard-run", Button).disabled = True
        self.query_one("#onboard-log", RichLog).clear()
        self.query_one("#onboard-doctor", DoctorView).display = False
        self._onboard(self.verdict.path)
        return True

    @work(thread=True, exclusive=True, group="onboard", exit_on_error=False)
    def _onboard(self, path: Path) -> None:
        """The subprocesses run here, off the UI thread; every line goes back onto it."""
        app = self.app
        try:
            outcome = onboarding.onboard(
                path,
                run=self._run,
                on_line=lambda line: app.call_from_thread(self._append_log, line),
            )
        except Exception as exc:  # `onboard` does not raise; this is the belt to its braces
            outcome = OnboardOutcome(
                path=path, reason=f"onboarding crashed: {exc or type(exc).__name__}"
            )
        app.call_from_thread(self._finish, outcome)

    def _append_log(self, line: str) -> None:
        self.log_lines.append(line)
        self.query_one("#onboard-log", RichLog).write(line)

    def _finish(self, outcome: OnboardOutcome) -> None:
        self.running = False
        self.outcome = outcome
        self.query_one("#onboard-run", Button).disabled = not self.verdict.ok
        if outcome.project_id is None:
            self.post_message(OnboardFailed(outcome.path, outcome.reason or "onboarding failed"))
            return
        doctor = self.query_one("#onboard-doctor", DoctorView)
        doctor.show(outcome.checks, cwd=outcome.path)
        doctor.display = True
        self.post_message(ProjectOnboarded(outcome.project_id, outcome.path))
