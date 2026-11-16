"""Codex ships as beta in 0.10.0 through one adapter slot, and Claude Code reads as it did.

``AgentAdapter.release_stage`` (``types.ReleaseStage``) is ``beta`` for Codex and ``stable``
for Claude Code. Wherever a person chooses or sees an agent, the shared code writes
``types.stage_mark`` after its name: `` (beta)`` for a beta agent, nothing for a stable one.
tests/test_third_agent_seams.py shows a synthetic third agent's mark reaching those surfaces
through the slot. This file pins the owner's other half of the bar for the release ("nothing
that works today in claude code breaks"): with the slot in, what Claude Code shows is byte for
byte what the head before it showed, and Codex's differs by its mark alone. Where a person
lists or chooses agents (SMOKE14, seq 11065), Codex is named with its mark: ``agents list``'s
row, ``aisquare accounts``' last line, every ``--agent`` help that chooses a coding agent, and
the persona Export dialog's skill destinations (GATE #110's F1, seq 11514).
The ``HEAD_`` goldens below were rendered at 11cd75ab, the PR's head before the slot, by these
same calls.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console
from typer.main import get_command
from typer.testing import CliRunner

from aisquare.cli import accounts as accounts_cli
from aisquare.cli import common
from aisquare.cli.app import app
from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.cli.ui.views import app_settings
from aisquare.cli.ui.views.doctor import render_checks
from aisquare.core import claude_accounts as core
from aisquare.core.agent_adapters import adapters, get_adapter, stage_mark_of
from aisquare.core.agent_adapters.types import ReleaseStage, stage_mark, staged_label
from aisquare.models import (
    AccountsOverview,
    AgentConnection,
    AgentInfo,
    CheckStatus,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeInstall,
    DoctorCheck,
)

MARK = " (beta)"

HEAD_SPAWN = [
    ("(role's: Claude Code)", ""),
    ("Claude Code", "claude-code"),
    ("Codex", "codex"),
]
HEAD_SETTINGS = [("Use default", ""), ("Claude Code", "claude-code"), ("Codex", "codex")]
HEAD_DOCTOR = (
    "✓ python: Python 3.12.0\n"
    "✓ claude-code: Claude Code: configured in /h/.claude\n"
    "✓ codex: Codex not detected on this machine\n"
)
HEAD_ACCOUNTS_CLAUDE_ONLY = (
    "✓ Claude Code 2.1.266 · /opt/bin/claude\n"
    "slot  label         signed in as   plan  hooks  directory                     \n"
    "1     plain claude  1@example.com  max   ✓      /h/.aisquare/claude-accounts/1\n"
    "add one: aisquare accounts add [--agent codex] · open one: aisquare accounts run <slot> · "
    "the Accounts page in asq does both\n"
)
HEAD_ACCOUNTS_BOTH = (
    "✓ Claude Code 2.1.266 · /opt/bin/claude\n"
    "✓ Codex 0.164.0 · /opt/bin/codex\n"
    "slot  agent        label         signed in as   plan  hooks  directory                     \n"
    "1     Claude Code  plain claude  1@example.com  max   ✓      /h/.aisquare/claude-accounts/1\n"
    "2     Codex        account 2     2@example.com  max   ✓      /h/.aisquare/claude-accounts/2\n"
    "add one: aisquare accounts add [--agent codex] · open one: aisquare accounts run <slot> · "
    "the Accounts page in asq does both\n"
)
HEAD_AGENTS_LIST = (
    "┏━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┓\n"
    "┃ AGENT       ┃ DETECTED ┃ CONNECTED ┃ INTEGRATION   ┃ HOOKS IN ┃ CONTEXT ┃\n"
    "┡━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━┩\n"
    "│ claude-code │ yes      │ yes       │ configured    │ —        │ —       │\n"
    "│ codex       │ no       │ no        │ not connected │ —        │ —       │\n"
    "└─────────────┴──────────┴───────────┴───────────────┴──────────┴─────────┘\n"
)
HEAD_CONNECT_CLAUDE = "✓ connected claude-code — hooks installed; imported 2 entries\n"
HEAD_CONNECT_CODEX = "✓ connected codex — hooks installed; imported 0 entries\n"
#: Every ``--agent`` option's help, by its command's path.
HEAD_AGENT_HELPS = {
    "init": "Agent to connect; repeat for several.",
    "launch": "Coding agent family.",
    "accounts default": "Whose account: claude-code (the default) or codex.",
    "accounts add": "Whose account: claude-code (the default) or codex.",
    "team spawn": "Coding agent family.",
    "team bind": "Coding agent family.",
    "fleet spawn": "Coding agent family.",
    "fleet switch": "Coding agent to move it onto (claude-code, codex, …). No conversation "
    "moves between agents: it starts fresh there with a hand-off brief of its work.",
    "persona export": "The coding agent whose skills --skill writes into (default: the one this "
    "directory launches).",
    "captain since": "One agent's events and its pane tail.",
    "hook event": "The adapter that installed this hook.",
}
#: ``aisquare accounts add --help`` at 100 columns, each line's trailing padding aside: every
#: line but ``--agent``'s, whose help is the one that names the agents.
HEAD_ACCOUNTS_ADD_HELP = """\

 Usage: root accounts add [OPTIONS]

 Add an account: a fresh slot, signed in through the agent's own login.

 Claude Code's first session signs in; ``--agent codex`` runs ``codex login``
 in the slot's own ``CODEX_HOME``. Either way the slot's hooks are connected after.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --agent             AGENT  Whose account: claude-code (the default) or codex.                    │
│ --verbose   -v             Enable verbose output.                                                │
│ --quiet     -q             Suppress non-essential output.                                        │
│ --json                     Emit machine-readable JSON on stdout. Put --json before the           │
│                            subcommand to also get usage errors (unknown command/option) as JSON. │
│ --profile           NAME   Configuration profile to use.                                         │
│ --no-color                 Disable coloured output.                                              │
│ --help      -h             Show this message and exit.                                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
"""


@pytest.fixture
def printed(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., str]]:
    """What a CLI emitter prints, through one 160-column console with no colour, as the
    goldens were rendered."""

    def run(emit: Callable[..., None], *args: Any, **kwargs: Any) -> str:
        buffer = io.StringIO()
        console = Console(
            file=buffer,
            width=160,
            color_system=None,
            markup=False,
            highlight=False,
            legacy_windows=False,  # the boxes a modern terminal draws, on Windows too
        )
        monkeypatch.setattr(common, "stdout_console", lambda: console)
        monkeypatch.setattr(accounts_cli, "stdout_console", lambda: console)
        emit(*args, **kwargs)
        return buffer.getvalue()

    yield run


def _flat(text: str) -> str:
    return " ".join(text.split())


def _slot_dir(slot: int) -> Path:
    return Path(f"/h/.aisquare/claude-accounts/{slot}")


def _native(text: str) -> str:
    """``text`` with the goldens' slot directories as this platform writes a ``Path``: the
    same length, so the head's columns stand (backslashes on Windows)."""
    for slot in (1, 2):
        text = text.replace(f"/h/.aisquare/claude-accounts/{slot}", str(_slot_dir(slot)))
    return text


def _status(slot: int, agent: str) -> ClaudeAccountStatus:
    account = ClaudeAccount(
        slot=slot,
        config_dir=_slot_dir(slot),
        managed=True,
        agent=agent,
    )
    return ClaudeAccountStatus(
        account=account,
        label=core.label(account),
        identity=ClaudeIdentity(email=f"{slot}@example.com"),
        signed_in=True,
        subscription="max",
        hooks_installed=True,
    )


CLAUDE = ClaudeInstall(installed=True, binary="/opt/bin/claude", version="2.1.266")
CODEX = ClaudeInstall(installed=True, binary="/opt/bin/codex", version="0.164.0")


def test_claude_code_is_stable_and_codex_beta_in_their_own_adapters() -> None:
    """The one line each, so the release that takes Codex out of beta flips one word; a stable
    agent's mark is nothing at all, and every registered agent declares a stage."""
    assert get_adapter("claude-code").release_stage == "stable"
    assert get_adapter("codex").release_stage == "beta"
    stages: dict[ReleaseStage, str] = {"stable": "", "beta": MARK}
    for adapter in adapters():
        assert stage_mark(adapter) == stages[adapter.release_stage], adapter.id
        assert staged_label(adapter) == adapter.label + stages[adapter.release_stage]
        assert stage_mark_of(adapter.id) == stage_mark(adapter)
    assert stage_mark_of("no-such-agent") == ""


def test_the_spawn_and_settings_agent_pickers_read_as_the_head_but_for_codexs_mark() -> None:
    picking = SimpleNamespace(_role_agent=lambda role, binary: "claude-code")
    spawn = SpawnDialog._agent_options(picking, "coder", "")  # type: ignore[arg-type]
    assert spawn[:2] == HEAD_SPAWN[:2]  # Claude Code's own rows, byte for byte
    assert spawn == [*HEAD_SPAWN[:2], ("Codex (beta)", "codex")]
    on_codex = SimpleNamespace(_role_agent=lambda role, binary: "codex")
    assert SpawnDialog._agent_options(on_codex, "coder", "")[0][0] == "(role's: Codex (beta))"  # type: ignore[arg-type]
    settings = app_settings._agent_choices()
    assert settings == [*HEAD_SETTINGS[:2], ("Codex (beta)", "codex")]


def test_doctors_rows_read_as_the_head_but_for_codexs_mark(
    printed: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The row's name, in ``aisquare doctor`` and asq's Doctor page; ``--json`` keeps every
    name and detail as they were (install.sh reads ``Codex: unverified in`` off the detail)."""
    checks = [
        DoctorCheck(name="python", status=CheckStatus.ok, detail="Python 3.12.0"),
        DoctorCheck(
            name="claude-code",
            status=CheckStatus.ok,
            detail="Claude Code: configured in /h/.claude",
        ),
        DoctorCheck(
            name="codex", status=CheckStatus.ok, detail="Codex not detected on this machine"
        ),
    ]
    marked = HEAD_DOCTOR.replace("✓ codex:", "✓ codex (beta):")
    assert printed(common.emit_doctor, checks) == marked
    assert render_checks(checks).plain == marked
    head_lines, lines = HEAD_DOCTOR.splitlines(), marked.splitlines()
    assert lines[:2] == head_lines[:2]  # python's row and Claude Code's, unchanged

    monkeypatch.setattr(common, "get_state", lambda: SimpleNamespace(json_output=True))
    capsys.readouterr()
    common.emit_doctor(checks)
    said = capsys.readouterr().out
    assert MARK not in said and '"name": "codex"' in said


def test_aisquare_accounts_reads_as_the_head_on_a_claude_only_machine(
    printed: Callable[..., str],
) -> None:
    """A machine without Codex: the head's lines exactly, Claude Code's install line and
    table byte for byte, but for the last, whose list of the agents ``accounts add`` takes
    marks Codex (SMOKE14, seq 11065). With Codex's accounts too, its install line and its
    agent cell carry the mark, Claude Code's words none."""
    alone = AccountsOverview(
        claude=CLAUDE, accounts=[_status(1, "claude-code")], installs={"claude-code": CLAUDE}
    )
    head = _native(HEAD_ACCOUNTS_CLAUDE_ONLY).splitlines()
    lines = printed(accounts_cli._emit_overview, alone, with_usage=False).splitlines()
    assert lines[:-1] == head[:-1]
    assert lines[-1] == head[-1].replace("[--agent codex]", "[--agent codex (beta)]")

    both = AccountsOverview(
        claude=CLAUDE,
        accounts=[_status(1, "claude-code"), _status(2, "codex")],
        installs={"claude-code": CLAUDE, "codex": CODEX},
    )
    said = printed(accounts_cli._emit_overview, both, with_usage=False)
    assert said.splitlines()[0] == HEAD_ACCOUNTS_BOTH.splitlines()[0]  # Claude Code's install
    assert said.splitlines()[1] == "✓ Codex (beta) 0.164.0 · /opt/bin/codex"
    # The table pads its agent column to the wider name; its words are the head's but the mark.
    expected = (
        _flat(_native(HEAD_ACCOUNTS_BOTH))
        .replace("✓ Codex 0.164.0", "✓ Codex (beta) 0.164.0")
        .replace("2 Codex account 2", "2 Codex (beta) account 2")
        .replace("[--agent codex]", "[--agent codex (beta)]")
    )
    assert _flat(said) == expected
    assert said.count(MARK) == 3


def test_aisquare_agents_reads_as_the_head_but_for_codexs_mark(
    printed: Callable[..., str],
) -> None:
    """``agents connect``'s line marks Codex alone. ``agents list`` lists Codex with its mark
    (SMOKE14, seq 11065) on a line of its row under its id, the column as wide as the ids
    alone make it: the header and Claude Code's row are the head's, byte for byte."""
    claude = AgentConnection(name="claude-code", hooks_installed=True, imported=2)
    codex = AgentConnection(name="codex", hooks_installed=True, imported=0)
    assert printed(common.emit_connected, claude) == HEAD_CONNECT_CLAUDE
    assert printed(common.emit_connected, codex) == HEAD_CONNECT_CODEX.replace(
        "codex —", "codex (beta) —"
    )
    infos = [
        AgentInfo(name="claude-code", detected=True, connected=True, readiness="configured"),
        AgentInfo(name="codex", detected=False),
    ]
    head = HEAD_AGENTS_LIST.splitlines()
    lines = printed(common.emit_agents, infos).splitlines()
    codex_row = head[4]
    assert codex_row.startswith("│ codex ")
    blank = "".join(char if char == "│" else " " for char in codex_row)
    assert lines == [*head[:5], "│ (beta)" + blank[len("│ (beta)") :], *head[5:]]
    infos[1] = AgentInfo(name="codex", detected=True, detail="found at /opt/bin/codex")
    assert printed(common.emit_agents, infos).endswith("\ncodex (beta): found at /opt/bin/codex\n")


def _agent_helps() -> dict[str, str]:
    helps: dict[str, str] = {}

    def walk(command: Any, path: tuple[str, ...]) -> None:
        for param in command.params:
            if "--agent" in getattr(param, "opts", ()):
                helps[" ".join(path)] = param.help or ""
        if hasattr(command, "list_commands"):
            context = type(command).context_class(command)
            for name in command.list_commands(context):
                sub = command.get_command(context, name)
                if sub is not None:
                    walk(sub, (*path, name))

    walk(get_command(app), ())
    return helps


#: Where ``--agent`` chooses a coding agent, its help lists them, Codex with its mark
#: (SMOKE14, seq 11065); ``captain since`` (a fleet agent's label) and ``hook event`` (the
#: adapter a hook was installed by, which only a hook passes) choose none, and read as the head.
AGENTS = "claude-code, codex (beta)"
NOW_AGENT_HELPS = {
    "init": f"Agent to connect ({AGENTS}); repeat for several.",
    "launch": f"Coding agent family ({AGENTS}).",
    "accounts default": "Whose account: claude-code (the default) or codex (beta).",
    "accounts add": "Whose account: claude-code (the default) or codex (beta).",
    "team spawn": f"Coding agent family ({AGENTS}).",
    "team bind": f"Coding agent family ({AGENTS}).",
    "fleet spawn": f"Coding agent family ({AGENTS}).",
    "fleet switch": f"Coding agent to move it onto ({AGENTS}, …). No conversation moves "
    "between agents: it starts fresh there with a hand-off brief of its work.",
    "persona export": f"The coding agent whose skills --skill writes into ({AGENTS}; default: "
    "the one this directory launches).",
    "captain since": HEAD_AGENT_HELPS["captain since"],
    "hook event": HEAD_AGENT_HELPS["hook event"],
}


def test_every_agent_options_help_names_codex_with_its_mark_and_claude_code_without() -> None:
    """Every ``--agent`` option, none added or lost since the head: each that chooses a coding
    agent names Codex once, with its mark, and Claude Code unmarked; the rest are the head's."""
    helps = _agent_helps()
    assert helps.keys() == HEAD_AGENT_HELPS.keys()
    assert helps == NOW_AGENT_HELPS
    for path, said in helps.items():
        lists = "codex" in said
        assert said.count(MARK) == (1 if lists else 0), path
        assert "claude-code (beta)" not in said, path
        assert lists or said == HEAD_AGENT_HELPS[path], path


@pytest.fixture
def help_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """typer draws ``--help`` on one console on every OS, as the goldens were drawn: 100
    columns, no colour, and a modern terminal's boxes and width (Windows' legacy console takes
    a column off and draws other boxes; CI forces a terminal, which styles the page)."""
    import typer.rich_utils

    monkeypatch.setattr(
        typer.rich_utils,
        "_get_rich_console",
        lambda stderr=False: Console(
            width=100,
            color_system=None,
            force_terminal=False,
            legacy_windows=False,
            highlight=False,
            stderr=stderr,
        ),
    )


#: ``accounts add --help``'s one new option, Codex's ``--browser`` (R13 of #113): the only
#: rows a Claude Code user finds there that the head did not draw.
BROWSER_OPTION_ROWS = [
    "│ --browser                  Codex: sign in with the browser (`codex login`), not a device "
    "code,   │",
    "│                            for a ChatGPT login that does not allow device codes.         "
    "        │",
]


@pytest.mark.usefixtures("help_console")
def test_the_accounts_add_help_page_reads_as_the_head_but_for_codexs_mark() -> None:
    result = CliRunner().invoke(app, ["accounts", "add", "--help"])
    assert result.exit_code == 0, result.output
    # Each line as the head drew it, the padding Rich ends a plain line with aside (a box's
    # line ends in its border, so it is compared whole), and Codex's --browser after --agent.
    head = [line.rstrip() for line in HEAD_ACCOUNTS_ADD_HELP.rstrip("\n").splitlines()]
    now = [line.rstrip() for line in result.output.rstrip("\n").splitlines()]
    at = next(i for i, line in enumerate(now) if "--browser" in line)
    assert now[at : at + len(BROWSER_OPTION_ROWS)] == BROWSER_OPTION_ROWS, result.output
    assert "--agent  " in now[at - 1], "--browser follows --agent"
    del now[at : at + len(BROWSER_OPTION_ROWS)]
    assert len(now) == len(head), result.output
    for was, line in zip(head, now, strict=True):
        if "--agent  " in was:  # its help grew by the mark, and the box's padding shrank by it
            assert _flat(line) == _flat(was).replace("or codex.", "or codex (beta)."), line
        else:
            assert line == was, result.output
    root = CliRunner().invoke(app, ["--help"])
    assert root.exit_code == 0 and MARK not in root.output


#: ``aisquare metrics show`` with no turns, as 0.9.2 printed it (6a77e650's cli/common.py).
HEAD_092_NO_TURNS = (
    "No turns recorded yet. Connect Claude Code: aisquare agents connect claude-code\n"
)


@pytest.mark.parametrize("codex_here", ["none", "home", "binary"])
def test_the_empty_metrics_line_names_codex_only_where_it_is_and_marks_it(
    printed: Callable[..., str], monkeypatch: pytest.MonkeyPatch, codex_here: str
) -> None:
    """The connect hint names a stable agent always and a beta one only on a machine that has
    it (its home or a context file, or its binary on PATH), through the adapter's stage and
    ``core.agents``' detection, never a name. So a machine without Codex reads 0.9.2's line
    byte for byte (seq 11130), and one with Codex names both, Codex with its mark."""
    from aisquare.core import agents as agent_core
    from aisquare.models import MetricsSummary

    monkeypatch.setattr(
        agent_core, "detected", lambda spec: codex_here == "home" and spec.name == "codex"
    )
    monkeypatch.setattr(
        agent_core,
        "on_path",
        lambda name: f"/opt/bin/{name}" if codex_here == "binary" and name == "codex" else None,
    )
    said = printed(common.emit_metrics_summary, MetricsSummary(turns=0))
    if codex_here == "none":
        assert said == HEAD_092_NO_TURNS
        assert common.connect_hint() == "Connect Claude Code: aisquare agents connect claude-code"
    else:
        assert said == (
            "No turns recorded yet. Connect your coding agent: aisquare agents connect "
            "claude-code or aisquare agents connect codex (beta)\n"
        )


#: The persona Export dialog's destinations (``ExportPersonaScreen._destinations``), by radio
#: id, as the head before Codex's rows took their mark rendered them (865bfcdc): each agent's
#: personal skills, then each one's project skills. ``{…}`` is where the persona goes.
HEAD_EXPORT = [
    ("export-personal-claude-code", "Claude Code personal skills — {claude_user}"),
    ("export-personal-codex", "Codex personal skills — {codex_user}"),
    ("export-project-claude-code", "Claude Code project skills — {claude_project}"),
    ("export-project-codex", "Codex project skills — {codex_project}"),
]


@pytest.mark.parametrize("in_a_repo", [True, False])
def test_the_persona_export_dialogs_rows_read_as_the_head_but_for_codexs_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_a_repo: bool
) -> None:
    """Export chooses which agent's skills a persona goes to (GATE #110's F1, seq 11514).
    Claude Code's rows read as the head's byte for byte, Codex's differ by the mark after its
    name alone, the first row is still the one chosen, and outside a repository the project
    rows are still offered disabled."""
    from aisquare.cli.ui.persona_dialogs import ExportPersonaScreen
    from aisquare.core import agents as agent_core

    claude, repo = tmp_path / "claude", tmp_path / "repo"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    where = {
        "claude_user": str(claude / "skills" / "pair"),
        "codex_user": str(agent_core.home() / ".agents" / "skills" / "pair"),
        "claude_project": str(repo / ".claude" / "skills" / "pair"),
        "codex_project": str(repo / ".agents" / "skills" / "pair"),
    }
    if not in_a_repo:  # offered, but disabled
        where.update({key: "needs a git repository" for key in where if key.endswith("project")})
    head = [(ident, label.format(**where)) for ident, label in HEAD_EXPORT]

    buttons = ExportPersonaScreen("pair", repo if in_a_repo else None)._destinations()
    shown = [(str(button.id), str(button.label)) for button in buttons]

    assert shown[0::2] == head[0::2]  # Claude Code's own rows, byte for byte
    assert shown == [(ident, label.replace("Codex", f"Codex{MARK}", 1)) for ident, label in head]
    assert [button.value for button in buttons] == [True, False, False, False]
    assert [button.disabled for button in buttons] == [False, False, not in_a_repo, not in_a_repo]
