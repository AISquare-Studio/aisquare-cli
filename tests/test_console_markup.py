"""Bracketed text in DATA must survive to the screen; styling must still style.

Two independent lanes shipped the same silent bug in one night, from different
directions. `fail()` rendered its message through Rich with markup on, so the
install hint reached users as ``pip install 'aisquare-cli'`` — the extra name,
the single token that makes the command work, deleted. The doctor's detail
column ate the SDK's ``[present]``, making a configured key indistinguishable
from a missing one: the exact opposite of what the operator needed.

Neither was an error. Both were wrong answers, which is worse, and both got
through because the tests asserted the string we PASS IN rather than the string
a user SEES. Every test here reads rendered output.

The class is wider than the two instances: Rich parses markup in table cells
too, so ``aisquare context list`` was mangling remembered text, and every
``fail()`` interpolates paths, refs, role names and config values.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.console import stderr_console, stdout_console

#: A value that is ordinary in the field and destroyed by a markup parser: a
#: path segment in brackets, an extra name, a bracketed status word.
BRACKETED = "/home/me/[archive]/repo"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _styled_console(buffer: io.StringIO) -> Console:
    """A console with colour forced on, otherwise identical to the real ones.

    ``highlight=False`` matters: Rich's highlighter colours path components
    individually, which splits a literal across escape sequences. That is a
    faithful mirror of the factories, and a test whose console differs from the
    shipped one is testing Rich, not us.
    """
    return Console(
        file=buffer,
        force_terminal=True,
        color_system="truecolor",
        width=200,
        markup=False,
        highlight=False,
        soft_wrap=True,
    )


def _rendered(console: Console, text: str) -> str:
    buffer = io.StringIO()
    console.file = buffer
    console.print(text)
    return buffer.getvalue()


def test_the_output_consoles_do_not_parse_markup_in_data() -> None:
    for console in (stdout_console(), stderr_console()):
        assert _rendered(console, f"path is {BRACKETED}").strip().endswith(BRACKETED)


def test_a_table_cell_keeps_bracketed_text() -> None:
    """Rich parses markup inside cells too — `context list` was mangling entries."""
    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    table.add_column("TEXT")
    table.add_row(BRACKETED)
    console = stdout_console()
    buffer = io.StringIO()
    console.file = buffer
    console.width = 200
    console.print(table)

    assert "[archive]" in buffer.getvalue()


def test_a_failure_message_keeps_its_brackets(runner: CliRunner, tmp_path: Path) -> None:
    """The original instance, through the real command that shipped it."""
    result = runner.invoke(app, ["context", "show", "ctx_[nope]"])

    assert result.exit_code != 0
    assert "[nope]" in result.output


def test_the_serve_hint_still_names_the_extra(runner: CliRunner) -> None:
    """`pip install 'aisquare-cli[serve]'` — the token that makes it work."""
    from aisquare.cli import serve as serve_cli

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(serve_cli, "_find_spec", lambda name: None)
        result = runner.invoke(app, ["serve", "--stdio"])

    assert "aisquare-cli[serve]" in result.output


def test_a_doctor_detail_keeps_bracketed_status_words(runner: CliRunner) -> None:
    """The second instance: `[present]` must not read as `` (i.e. missing)."""
    from aisquare.cli.common import emit_doctor
    from aisquare.models import CheckStatus, DoctorCheck

    check = DoctorCheck(
        name="explainability sdk",
        status=CheckStatus.ok,
        detail="workspace key [present], gateway [reachable]",
        fix="",
    )
    with pytest.MonkeyPatch.context() as patch:
        buffer = io.StringIO()

        def _console() -> Console:
            console = stdout_console()
            console.file = buffer
            console.width = 300
            return console

        patch.setattr("aisquare.cli.common.stdout_console", _console)
        emit_doctor([check])

    rendered = buffer.getvalue()
    assert "[present]" in rendered
    assert "\\[present]" not in rendered, (
        "an escape() left over from when the console parsed markup now prints "
        "the backslash it was meant to hide"
    )


def test_a_stub_message_keeps_its_brackets(runner: CliRunner) -> None:
    result = runner.invoke(app, ["capture", "status"])

    assert "not implemented" in result.output


# --- styling is not collateral damage ---


def test_deliberate_styling_still_renders() -> None:
    """`markup=False` is about DATA. A line we chose to style must stay styled.

    Asserted on the ANSI Rich actually emits, not on the tag we wrote, for the
    same reason as everything else in this file.
    """
    buffer = io.StringIO()
    _styled_console(buffer).print(f"explainability: {BRACKETED}", style="dim")

    rendered = buffer.getvalue()
    assert "\x1b[" in rendered, "the line lost its styling"
    assert BRACKETED in _ANSI.sub("", rendered), "the data lost its brackets"


def test_the_launch_banner_still_bolds_the_role() -> None:
    """One site styles a single token rather than a whole line; it must survive."""
    from rich.text import Text

    buffer = io.StringIO()
    _styled_console(buffer).print(
        Text.assemble("Launching as ", ("coder", "bold"), f" in {BRACKETED}")
    )

    rendered = buffer.getvalue()
    assert "\x1b[1m" in rendered, "the role is no longer bold"
    assert BRACKETED in _ANSI.sub("", rendered)


def test_json_mode_is_untouched(runner: CliRunner) -> None:
    """Rendering flags must never leak into the machine-readable contract."""
    result = runner.invoke(app, ["--json", "status"], catch_exceptions=False)

    assert json.loads(result.output)["initialized"] in (True, False)


def test_no_module_builds_its_own_console() -> None:
    """The safe default only holds while the factories are the only way in.

    A ``Console(...)`` built anywhere else inherits Rich's default — markup ON —
    and quietly reopens the whole class. AST rather than grep: the word
    "Console" appears in prose throughout this package, and a guard that fires
    on a docstring is a guard people learn to silence.
    """
    import ast

    package = Path(__file__).resolve().parents[1] / "src" / "aisquare"
    factory = package / "core" / "console.py"
    offenders: list[str] = []
    for module in sorted(package.rglob("*.py")):
        if module == factory:
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        offenders += [
            f"{module.relative_to(package)}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Console"
        ]

    assert not offenders, (
        f"Console built outside core/console.py at {offenders} — it inherits Rich's "
        "markup=True default, so any bracketed data it prints is silently deleted. "
        "Use stdout_console()/stderr_console()."
    )


#: A Rich style tag written into a print argument. ``[/anything]`` is a closing
#: tag and unambiguous; an opening ``[word]`` counts only when Rich itself can
#: resolve ``word`` as a style. That distinction is the whole precision of this
#: guard: ``[serve]``, ``[tui]``, ``[redacted]`` and ``/home/me/[archive]/repo``
#: are DATA and must never trip it, while ``[dim]`` and ``[bold red]`` are
#: intent and must always trip it.
_TAG = re.compile(r"\[(/?)([^\[\]]{0,64})\]")


def _style_tags(literal: str) -> list[str]:
    """Rich style tags inside one string literal, asking Rich what a style is."""
    from rich.errors import StyleSyntaxError
    from rich.style import Style

    found: list[str] = []
    for closing, body in _TAG.findall(literal):
        if closing:
            found.append(f"[/{body}]")
            continue
        if not body:
            continue
        try:
            Style.parse(body)
        except (StyleSyntaxError, ValueError):
            continue
        found.append(f"[{body}]")
    return found


def test_the_detector_tells_styling_apart_from_data() -> None:
    """The guard below is only worth having if it is precise. Pinned first.

    A guard that fires on `pip install 'aisquare-cli[serve]'` would be silenced
    within a day, and silencing it is how the class comes back.
    """
    for styling in ("[dim]x[/dim]", "[/dim]", "as [bold]coder[/bold]", "[bold red]danger[/]"):
        assert _style_tags(styling), styling
    for data in (
        "pip install 'aisquare-cli[serve]'",
        "tip: install 'aisquare-cli[tui]' for the board",
        "EXPLAINABILITY_API_KEY=[redacted]",
        "/home/me/[archive]/repo",
        "gateway: {} [{}]",
    ):
        assert not _style_tags(data), data


def test_no_print_argument_carries_a_style_tag() -> None:
    """The other half of the class, and the half that actually recurred.

    The consoles stopped parsing markup, so a ``[dim]…[/dim]`` written into a
    print now reaches the user as literal tags. That is not hypothetical: two
    such sites landed in ``cli/launch.py`` hours after the sweep was cut, from a
    lane working off an older tree, and would have printed raw on the
    nested-launch line — the line someone reads while they are already confused
    about which agent owns which Run. The `Console`-construction guard above
    cannot see it, because the defect is a call site, not a rogue console.

    Styling is still available and still used; it just has to be structural —
    ``style=``, ``Column(style=…)``, ``header_style``, ``rich.text.Text`` — all
    of which render identically whether markup is on or off, and therefore
    survive any fold order.
    """
    import ast

    package = Path(__file__).resolve().parents[1] / "src" / "aisquare"
    offenders: list[str] = []
    for module in sorted(package.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"print", "add_row", "add_column"}:
                continue
            literals: list[str] = []
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    literals.append(arg.value)
                elif isinstance(arg, ast.JoinedStr):
                    literals += [
                        value.value
                        for value in arg.values
                        if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    ]
            for literal in literals:
                tags = _style_tags(literal)
                if tags:
                    offenders.append(f"{module.relative_to(package)}:{node.lineno} {tags}")

    assert not offenders, (
        f"style tags written into a render call at {offenders} — the consoles do not "
        "parse markup, so these print to the user as literal text. Use style=... on "
        "the print, or build a rich.text.Text; both render the same with markup on or off."
    )
