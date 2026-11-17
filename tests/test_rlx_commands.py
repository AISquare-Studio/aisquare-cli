"""``rlx`` and ``relax`` (#133): two more names for the one CLI, beside ``aisquare`` and ``asq``.

Each starts ``aisquare.cli.app:main``, so it is the same program under another name: its
usage line and its completion read the name it was started by, and everything else is what
``asq`` does. What the commands meet on a machine is here too: another program's ``rlx`` or
``relax`` where uv puts a tool's executables (PyPI's ``relax`` is another project), which uv
replaces without a word on a reinstall and deletes on an uninstall (measured, uv 0.11.6), and
one earlier on PATH, which the doctor names. install.sh's side is in
tests/test_install_script_functions.py, the upgrade's and the uninstall's refusals in
tests/test_lifecycle_upgrade.py and tests/test_lifecycle_uninstall.py.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

from aisquare.cli import fleet as fleet_cli
from aisquare.cli.app import main
from aisquare.core import agents as agent_core
from aisquare.core import instance_scan, ledger
from aisquare.core.agent_adapters import claude_usage, lasting, usage_tally
from aisquare.core.version import __version__
from aisquare.services import diagnostics, install_route, lifecycle
from aisquare.services.install_route import Facts, InstallRoute, UvReceipt
from tests.rendered import plain

NAMES = ("aisquare", "asq", "rlx", "relax")
"""Every name the package's console scripts give ``main``."""


def _run_main(monkeypatch: pytest.MonkeyPatch, name: str, *args: str) -> int:
    """``main`` as the console script ``<name>`` runs it, its exit code. Click names the
    program after ``__main__``'s package when it has one (``python -m pytest`` here); a
    console script's ``__main__`` has none, so its name is ``argv[0]``'s."""
    monkeypatch.setattr(sys, "argv", [f"/opt/tools/aisquare-cli/bin/{name}", *args])
    monkeypatch.setitem(sys.modules, "__main__", types.ModuleType("__main__"))
    try:
        main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


# --- the commands ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_the_usage_line_reads_the_name_it_was_started_by(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    code = _run_main(monkeypatch, name, "--help")

    # Styling stripped at the assert site, as conftest says: typer forces a terminal under
    # GITHUB_ACTIONS, read at its import, and its help then carries ANSI codes.
    out = plain(capsys.readouterr().out)
    assert code == 0, out
    assert f"Usage: {name} [OPTIONS] COMMAND [ARGS]..." in out, out
    others = [other for other in NAMES if other != name and other not in name]
    assert not any(f"Usage: {other} " in out for other in others), out


@pytest.mark.parametrize("name", NAMES)
def test_version_is_the_packages_under_every_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """As ``aisquare --version`` prints it: install.sh reads the second word."""
    code = _run_main(monkeypatch, name, "--version")

    assert code == 0
    assert capsys.readouterr().out == f"rlx {__version__}\n"


@pytest.mark.parametrize("name", NAMES)
def test_the_bare_name_opens_what_bare_asq_opens(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)
    monkeypatch.setattr(fleet_cli, "ui", lambda: opened.append(name))

    code = _run_main(monkeypatch, name)

    assert (code, opened) == (0, [name])


@pytest.mark.parametrize("name", NAMES)
def test_completion_answers_to_the_name_it_was_started_by(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """Typer keys completion on the program's name (``_RLX_COMPLETE``), so ``rlx
    --install-completion`` installs a script for ``rlx`` that completes its commands."""
    variable = f"_{name.upper()}_COMPLETE"
    monkeypatch.setenv(variable, "source_zsh")
    _run_main(monkeypatch, name)
    script = capsys.readouterr().out
    monkeypatch.setenv(variable, "complete_zsh")
    monkeypatch.setenv("_TYPER_COMPLETE_ARGS", f"{name} fle")
    _run_main(monkeypatch, name)
    completed = capsys.readouterr().out

    assert f"#compdef {name}\n" in script, script
    assert f"{variable}=complete_zsh {name}" in script, script
    assert '"fleet"' in completed, completed


def test_a_hook_connected_from_rlx_or_relax_runs_the_aisquare_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hooks name the ``aisquare`` script whichever name connected them: another program can
    take ``relax`` over, and a hook naming it would then run that program. The matcher knows
    every name, so a hook a person wrote with ``rlx`` is ours too."""
    bin_dir = tmp_path / "tools" / "aisquare-cli" / "bin"
    bin_dir.mkdir(parents=True)
    for name in NAMES:
        (bin_dir / name).write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(agent_core, "current_install", lambda: bin_dir / "aisquare")
    monkeypatch.setattr(agent_core, "_starts", lambda _path: True)
    written: dict[str, str] = {}
    for name in NAMES:
        monkeypatch.setattr(sys, "argv", [str(bin_dir / name), "agents", "connect"])
        written[name] = agent_core._aisquare_command()

    # Quoted as the hook's shell reads it, the product's own rule (`_quote`): POSIX quotes
    # where sh runs it, none (or double quotes) where cmd.exe does, unchanged since 0.10.0.
    aisquare = agent_core._quote(str((bin_dir / "aisquare").resolve()))
    assert written["rlx"] == written["relax"] == aisquare
    assert written["asq"] == agent_core._quote(str((bin_dir / "asq").resolve())), "control"
    for name in NAMES:
        assert agent_core._is_aisquare_hook_command(f"{written[name]} hook stop"), name
        assert agent_core._is_aisquare_hook_command(f"{bin_dir / name} hook stop"), name
        assert agent_core._is_aisquare_hook_command(f"{name} --no-color hook stop"), name
    assert not agent_core._is_aisquare_hook_command(f"{bin_dir / 'relaxed'} hook stop")
    assert not agent_core._is_aisquare_hook_command(f"{bin_dir / 'relax'} hook nothing")


@pytest.mark.parametrize("name", ["rlx", "relax"])
def test_the_usage_and_board_readers_know_every_name(name: str) -> None:
    """An agent's shell may run ``rlx repo …`` or ``relax board``, and a hook line ``rlx hook
    …``: counted as the ``aisquare`` forms are (#133)."""
    assert claude_usage._AISQUARE_HOOK.search(f"/home/u/.local/bin/{name} hook stop")
    assert claude_usage._LOOKUP_COMMAND.search(f"cd /w && {name} repo find auth")
    assert usage_tally.is_lookup_command(f"{name} repo map")
    assert ledger.BOARD_READ.match(f"{name} --json task list")
    assert ledger.BOARD_READ.match(f"/home/u/.local/bin/{name} board")
    assert not usage_tally.is_lookup_command(f"grep {name} repo/"), "a mention, not a command"
    assert not ledger.BOARD_READ.match(f"{name}x board")


@pytest.mark.parametrize(
    ("spec", "runs", "fetches"),
    [
        ({"command": "/home/u/.local/bin/rlx", "args": ["serve"]}, True, False),
        ({"command": r"C:\Users\u\.local\bin\relax.EXE", "args": ["serve"]}, True, False),
        ({"command": "uvx", "args": ["--from", "aisquare-cli", "rlx", "serve"]}, True, True),
        (
            {"command": "uvx", "args": ["--from", "aisquare-cli[serve]==0.11.0", "relax"]},
            True,
            True,
        ),
        ({"command": "uv", "args": ["tool", "run", "--from", "aisquare_cli", "rlx"]}, True, True),
        # PyPI's `relax` is another project: a bare name alone is not ours.
        ({"command": "uvx", "args": ["relax", "serve"]}, False, False),
        ({"command": "uvx", "args": ["--from", "relax-tools", "rlx"]}, False, False),
        ({"command": "node", "args": ["/srv/relax/index.js"]}, False, False),
        ({"command": "uvx", "args": ["--from", "aisquare-cli", "aisquare", "serve"]}, True, True),
    ],
    ids=[
        "rlx-path",
        "relax-exe",
        "uvx-rlx",
        "uvx-relax-pinned",
        "uv-tool-run",
        "uvx-relax-alone",
        "uvx-other-package",
        "a-path-holding-relax",
        "control",
    ],
)
def test_an_mcp_server_runs_aisquare_by_rlx_only_from_this_package(
    spec: dict[str, object], runs: bool, fetches: bool
) -> None:
    assert lasting.runs_aisquare(spec) is runs
    assert (runs and lasting.fetches_aisquare(spec)) is fetches


@pytest.mark.parametrize("name", ["rlx", "relax"])
def test_a_process_named_rlx_or_relax_is_no_asq_from_before_the_registry(name: str) -> None:
    """The look in ``ps`` is for asq UIs older than the registry (#21), and none of those had
    these names: an rlx UI registers itself. A ``relax`` there may be anybody's."""
    assert not instance_scan.ui_command(f"/home/u/.local/bin/{name}")
    assert not instance_scan.ui_command(f"/usr/bin/python3 /home/u/.local/bin/{name} ui")
    assert instance_scan.ui_command("/home/u/.local/bin/asq"), "control"


# --- another program's rlx or relax where uv puts this tool's executables -------------------


def _route(
    tmp_path: Path, *, platform: str = "linux", listed: tuple[str, ...] = ()
) -> tuple[InstallRoute, Path, Path]:
    """A uv tool route whose receipt puts ``aisquare`` (and ``listed``) in ``~/.local/bin``."""
    prefix = tmp_path / "share" / "uv" / "tools" / "aisquare-cli"
    (prefix / "bin").mkdir(parents=True)
    bin_dir = tmp_path / "home" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    exe = ".exe" if platform == "win32" else ""
    receipt = UvReceipt(
        bin_dir=bin_dir,
        entrypoints=tuple(bin_dir / f"{name}{exe}" for name in ("aisquare", *listed)),
    )
    facts = Facts(
        prefix=prefix,
        base_prefix=tmp_path / "base",
        executable=prefix / "bin" / "python",
        platform=platform,
        python_version="3.13",
    )
    return InstallRoute(install_route.UV_TOOL, facts, receipt=receipt), bin_dir, prefix


def _hand_made(path: Path) -> None:
    path.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")


def test_uvs_own_links_to_aisquare_cli_are_not_taken(tmp_path: Path) -> None:
    """As uv leaves them: links to ``<tools>/aisquare-cli/bin/<name>``, whether or not the
    tool directory is reached by the spelling the receipt uses, and with nothing at the end
    of one (an environment being replaced)."""
    route, bin_dir, prefix = _route(tmp_path)
    (bin_dir / "rlx").symlink_to(prefix / "bin" / "rlx")
    (bin_dir / "relax").symlink_to(Path("/elsewhere/uv/tools/aisquare-cli/bin/relax"))

    assert install_route.taken_commands(route) == ()


def test_nothing_there_is_nothing_taken(tmp_path: Path) -> None:
    route, _bin_dir, _prefix = _route(tmp_path)
    assert install_route.taken_commands(route) == ()


def test_every_other_rlx_or_relax_is_another_programs(tmp_path: Path) -> None:
    """A hand-made script; another uv tool's link (``uv tool install --force`` of a package
    with a ``relax``); a link to nothing of ours; one to aisquare-cli's script of another
    name."""
    route, bin_dir, prefix = _route(tmp_path)
    _hand_made(bin_dir / "rlx")
    (bin_dir / "relax").symlink_to(prefix.parent / "relax-tool" / "bin" / "relax")
    first = install_route.taken_commands(route)
    (bin_dir / "rlx").unlink()
    (bin_dir / "rlx").symlink_to(tmp_path / "gone" / "rlx")
    (bin_dir / "relax").unlink()
    (bin_dir / "relax").symlink_to(prefix / "bin" / "rlx")
    second = install_route.taken_commands(route)

    assert first == (bin_dir / "rlx", bin_dir / "relax")
    assert second == (bin_dir / "rlx", bin_dir / "relax")


def test_on_windows_a_copy_the_receipt_records_is_ours(tmp_path: Path) -> None:
    """uv copies the scripts there (``rlx.exe``) instead of linking them: the one the receipt
    records is ours, a file it does not record another program's."""
    route, bin_dir, _prefix = _route(tmp_path, platform="win32", listed=("relax",))
    _hand_made(bin_dir / "rlx.exe")
    _hand_made(bin_dir / "relax.exe")
    _hand_made(bin_dir / "rlx")  # no .exe: not where Windows runs `rlx` from

    assert install_route.taken_commands(route) == (bin_dir / "rlx.exe",)


def test_a_route_without_a_receipt_has_nothing_taken(tmp_path: Path) -> None:
    """pipx leaves another program's executable where it is, and the other routes' commands
    are run by hand: only a uv tool's reinstall and removal are guarded."""
    route, bin_dir, _prefix = _route(tmp_path)
    _hand_made(bin_dir / "rlx")
    pipx = InstallRoute(install_route.PIPX, route.facts)
    nameless = InstallRoute(install_route.UV_TOOL, route.facts, receipt=UvReceipt())

    assert install_route.taken_commands(pipx) == ()
    assert install_route.taken_commands(nameless) == ()
    assert install_route.taken_commands(route) == (bin_dir / "rlx",), "control"


def test_the_receipt_records_every_entrypoint_by_its_path(tmp_path: Path) -> None:
    """As uv 0.11.6 writes it (measured in a scratch HOME)."""
    prefix = tmp_path / "aisquare-cli"
    prefix.mkdir()
    bin_dir = tmp_path / "bin"
    # A TOML basic string, escaped as uv writes one: a Windows path's backslashes doubled.
    rows = ",\n".join(
        f'    {{ name = "{name}", install-path = {json.dumps(str(bin_dir / name))}, '
        'from = "aisquare-cli" }'
        for name in ("aisquare", "asq", "asq-statusline", "relax", "rlx")
    )
    (prefix / install_route.RECEIPT_NAME).write_text(
        '[tool]\nrequirements = [\n    { name = "aisquare-cli" },\n]\npython = "3.12"\n'
        f"entrypoints = [\n{rows},\n]\n",
        encoding="utf-8",
    )

    receipt = install_route.read_receipt(prefix)

    assert receipt is not None and receipt.bin_dir == bin_dir
    names = ("aisquare", "asq", "asq-statusline", "relax", "rlx")
    assert receipt.entrypoints == tuple(bin_dir / name for name in names)


@pytest.mark.parametrize(
    ("command", "taken", "said"),
    [
        (
            "upgrade",
            ("/h/.local/bin/relax",),
            "/h/.local/bin/relax is another program, which the reinstall would replace "
            "without a word: rename or move it first, then run `aisquare upgrade` again.",
        ),
        (
            "upgrade",
            ("/h/.local/bin/rlx", "/h/.local/bin/relax"),
            "/h/.local/bin/rlx and /h/.local/bin/relax are other programs, which the "
            "reinstall would replace without a word: rename or move them first, then run "
            "`aisquare upgrade` again.",
        ),
        (
            "uninstall",
            ("/h/.local/bin/rlx",),
            "/h/.local/bin/rlx is another program, which uv would delete with the package: "
            "move it aside first, run `aisquare uninstall` again, then put it back.",
        ),
    ],
    ids=["upgrade-one", "upgrade-two", "uninstall"],
)
def test_the_refusal_is_one_line_naming_each_and_what_to_do(
    command: str, taken: tuple[str, ...], said: str
) -> None:
    # Each file named as this platform writes its path: `\h\.local\bin\rlx` on Windows.
    for path in taken:
        said = said.replace(path, str(Path(path)))
    assert lifecycle.taken_refusal(command, [Path(path) for path in taken]) == said
    assert lifecycle.taken_refusal(command, []) is None


# --- the doctor -----------------------------------------------------------------------------


def _path_with(monkeypatch: pytest.MonkeyPatch, *directories: Path) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(str(directory) for directory in directories))


def _scripts(directory: Path, *names: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        script = directory / name
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)
    return directory


@pytest.fixture
def ours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This install's scripts, as uv leaves them, and ~/.local/bin's links to them."""
    tools = _scripts(tmp_path / "tools" / "aisquare-cli" / "bin", *NAMES)
    monkeypatch.setattr(agent_core, "current_install", lambda: tools / "aisquare")
    local = tmp_path / "local-bin"
    local.mkdir()
    for name in NAMES:
        (local / name).symlink_to(tools / name)
    return local


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executables on PATH")
def test_the_doctor_says_nothing_of_commands_that_run_this_install_or_are_not_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ours: Path
) -> None:
    _path_with(monkeypatch, ours)
    linked = diagnostics._check_commands()
    _path_with(monkeypatch, tmp_path / "nowhere")

    assert linked is None
    assert diagnostics._check_commands() is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executables on PATH")
def test_the_doctor_names_another_programs_command_earlier_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ours: Path
) -> None:
    """ok, not a warning: a program of the user's own is no fault, and an amber row would
    fail install.sh's run on such a machine (exit 2). But said, as the name is not ours."""
    brew = _scripts(tmp_path / "brew-bin", "relax")
    _path_with(monkeypatch, brew, ours)
    one = diagnostics._check_commands()
    both = _scripts(tmp_path / "other-bin", "rlx", "relax")
    _path_with(monkeypatch, both, ours)
    two = diagnostics._check_commands()

    assert one is not None and one.name == "commands" and one.status == "ok"
    assert one.detail == (
        f"relax on your PATH runs another program ({brew / 'relax'}), not this aisquare: "
        "start it with aisquare, asq or rlx"
    )
    assert two is not None and two.detail == (
        f"rlx and relax on your PATH run other programs ({both / 'rlx'}, {both / 'relax'}), "
        "not this aisquare: start it with aisquare or asq"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executables on PATH")
def test_the_commands_row_is_in_the_doctors_report_beside_the_install_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ours: Path
) -> None:
    brew = _scripts(tmp_path / "brew-bin", "relax")
    _path_with(monkeypatch, brew, ours)
    names = [check.name for check in diagnostics.doctor()]
    _path_with(monkeypatch, ours)
    without = [check.name for check in diagnostics.doctor()]

    assert names[names.index("install") + 1] == "commands", names
    assert "commands" not in without, "control: only while one is another program's"


# --- the notes ------------------------------------------------------------------------------


def test_the_notes_name_the_two_commands_the_guard_and_the_title() -> None:
    """The [Unreleased] entry says what this ships, under the heading it ships as."""
    text = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text.split("## [Unreleased]")[1].split("\n## [")[0]
    added = " ".join(unreleased.split("### Added")[1].split("### ")[0].split())
    changed = " ".join(unreleased.split("### Changed")[1].split("### ")[0].split())

    assert "**`rlx` and `relax` start aisquare, as `asq` and `aisquare` do.**" in added
    assert "**Another program's `rlx` or `relax` is never replaced or deleted.**" in added
    assert "Hooks still call `aisquare`" in added
    assert "**asq's top-left reads `rlx`, the product's name, where it read `Fleet`.**" in changed
