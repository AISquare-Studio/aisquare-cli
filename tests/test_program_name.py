"""``core.program``: the word every printed command starts with (#135, RLX R3; the manager's
ruling on Q2, brief 180), and the doctor's buttons read back from hints that print it.

The suite pins the word to ``rlx`` (conftest's ``hints_print_rlx``); the tests of the choice
itself put the real :func:`program.ours_on_path` back and build the PATH it reads.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as core
from aisquare.core import program
from aisquare.core.program import ours_on_path as real_ours_on_path
from aisquare.core.shell_line import command_line
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import diagnostics, onboarding

SUFFIX = ".exe" if sys.platform == "win32" else ""


def _script(directory: Path, name: str) -> Path:
    """A program ``shutil.which`` finds as ``name``: ``name.exe`` on Windows, as uv writes
    a console script there, and executable elsewhere."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}{SUFFIX}"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _path(monkeypatch: pytest.MonkeyPatch, *directories: Path) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(str(directory) for directory in directories))
    program.word.cache_clear()


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This install's scripts directory, as a uv tool's: ``aisquare`` and ``rlx`` side by
    side (``agents.current_install`` names the first), and the real PATH test back."""
    scripts = tmp_path / "tools" / "aisquare-cli" / "bin"
    _script(scripts, "aisquare")
    _script(scripts, "rlx")
    monkeypatch.setattr(agent_core, "current_install", lambda: scripts / f"aisquare{SUFFIX}")
    monkeypatch.setattr(program, "ours_on_path", real_ours_on_path)
    program.word.cache_clear()
    return scripts


# --------------------------------------------------------------------------- the word


def test_hints_say_rlx_while_the_rlx_on_path_is_this_installs_own(
    install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path(monkeypatch, install)

    assert program.word() == "rlx"
    assert program.words("accounts", "add") == "rlx accounts add"


@pytest.mark.skipif(sys.platform == "win32", reason="uv links ~/.local/bin on POSIX only")
def test_a_link_to_it_as_uv_makes_in_local_bin_is_ours(
    install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``uv tool install`` puts a link in ``~/.local/bin`` to the tool's own script: the
    file a link leads to is what is compared."""
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "rlx").symlink_to(install / "rlx")
    _path(monkeypatch, local)

    assert program.word() == "rlx"


def test_another_programs_rlx_first_on_path_makes_hints_say_aisquare(
    install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PyPI's ``relax`` is another project, and a user's own ``rlx`` may come first on PATH:
    a hint that said ``rlx …`` would send them to it."""
    other = tmp_path / "other" / "bin"
    _script(other, "rlx")
    _path(monkeypatch, other, install)
    foreign = (program.word(), program.words("doctor"))
    _path(monkeypatch, install, other)
    ours_first = program.word()

    assert foreign == ("aisquare", "aisquare doctor")
    assert ours_first == "rlx", "control: ours first on PATH is ours"


def test_no_rlx_on_path_makes_hints_say_aisquare(
    install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout run as ``python -m aisquare``, or a PATH without ``~/.local/bin``: nothing
    answers to ``rlx``, so a hint names the command this install has always answered to."""
    _path(monkeypatch, tmp_path / "empty")

    assert program.word() == "aisquare"


def test_the_word_is_decided_once_per_process(
    install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path(monkeypatch, install)
    asked: list[str] = []
    which = shutil.which

    def counting(name: str, *args: object, **kwargs: object) -> str | None:
        asked.append(name)
        return which(name)

    monkeypatch.setattr(shutil, "which", counting)
    words = {program.word() for _ in range(5)}

    assert words == {"rlx"} and asked == ["rlx"]


def test_a_line_is_quoted_for_the_shell_it_is_pasted_into(
    install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path(monkeypatch, install)
    folder = str(tmp_path / "a folder")

    assert program.words("accounts", "trust", folder) == command_line(
        ["rlx", "accounts", "trust", folder]
    )


# ------------------------------------------------------------------ the doctor's buttons

#: One hint's command for each of ``onboarding.KNOWN_FIXES`` (after the program's word), and
#: the button it makes. A new entry in that table needs one here.
_SAMPLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "agents connect": (
        "agents connect claude-code --config-dir /tmp/c",
        ("agents", "connect", "claude-code", "--config-dir", "/tmp/c"),
    ),
    "project onboard": ("project onboard --refresh", ("project", "onboard", "--refresh")),
    "doctor --fix": ("doctor --fix --yes", ("doctor", "--fix", "--yes")),
    "accounts run": ("accounts run 3 login --device-auth", ("accounts", "run", "3")),
}


def _warn(fix: str) -> DoctorCheck:
    return DoctorCheck(name="row", status=CheckStatus.warn, detail="something", fix=fix)


def test_every_known_fix_makes_its_button_from_either_word_a_hint_prints() -> None:
    """A hint starts its command with ``rlx`` or, with another program's ``rlx`` first on
    PATH, ``aisquare`` (``program.PRINTED``): either makes the same button. Before #135 the
    parser read only ``aisquare``, so a hint reworded to ``rlx`` lost its button while the
    tests pinning the old words stayed green."""
    assert set(_SAMPLES) == {known.mention for known in onboarding.KNOWN_FIXES}
    for word in program.PRINTED:
        for mention, (command, argv) in _SAMPLES.items():
            fixes = onboarding.fix_commands([_warn(f"Fix it: {word} {command}")])
            assert [fix.argv for fix in fixes] == [argv], (word, mention)
    for word in ("asq", "relax", "uvx"):  # control: no hint prints these, and none is read
        assert onboarding.fix_commands([_warn(f"{word} doctor --fix --yes")]) == [], word


def test_a_buttons_label_names_the_command_with_the_word_hints_print(
    install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [fix] = onboarding.fix_commands([_warn("aisquare accounts run 3")])
    _path(monkeypatch, install)
    ours = fix.label
    other = tmp_path / "other"
    _script(other, "rlx")
    _path(monkeypatch, other, install)
    foreign = fix.label

    assert (ours, foreign) == ("rlx accounts run 3", "aisquare accounts run 3")


def _buttons_in_each_word(
    rows: Callable[[], list[DoctorCheck]], monkeypatch: pytest.MonkeyPatch
) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    """The buttons the doctor makes of ``rows()``, with the hints printing ``rlx`` and again
    printing ``aisquare`` (another program's ``rlx`` first on PATH), keyed by that word; each
    button's label starts with the word its hint printed."""
    made = {}
    for ours in (True, False):
        monkeypatch.setattr(program, "ours_on_path", lambda name, ours=ours: ours)
        program.word.cache_clear()
        word = program.word()
        fixes = onboarding.fix_commands(rows())
        assert all(fix.label.startswith(f"{word} ") for fix in fixes), (word, fixes)
        made[word] = sorted((fix.check, fix.argv) for fix in fixes)
    return made


def test_the_doctors_own_hints_make_the_same_buttons_whichever_word_they_print(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every button the doctor makes today, from its real rows in each state that makes one
    (#135's button proof, in a test's home), with the hints printing either word: the same
    commands. Claude Code on PATH and never connected, a Claude slot and a Codex slot that
    never signed in; then Codex chosen (`agents use codex`) and not connected."""
    agents = tmp_path / "agents-on-path"
    found = {"claude-code": str(agents / "claude"), "codex": str(agents / "codex")}
    monkeypatch.setattr(agent_core, "on_path", found.get)
    core.create_account("claude-code")
    core.create_account("codex")
    first = _buttons_in_each_word(
        lambda: [
            diagnostics._check_claude_code(),
            *diagnostics._claude_accounts_checks(),
            *diagnostics._agent_accounts_checks("codex"),
        ],
        monkeypatch,
    )
    used = runner.invoke(app, ["--json", "agents", "use", "codex"])
    assert used.exit_code == 0, used.output
    then = _buttons_in_each_word(diagnostics._check_other_agents, monkeypatch)

    assert first["rlx"] == first["aisquare"] and then["rlx"] == then["aisquare"]
    assert first["rlx"] == [
        ("claude-accounts", ("accounts", "run", "2")),
        ("claude-code", ("agents", "connect", "claude-code")),
        ("codex-accounts", ("accounts", "run", "3")),
    ]
    assert [(check, argv[:4]) for check, argv in then["rlx"]] == [
        ("codex", ("agents", "connect", "codex", "--config-dir"))
    ]


def test_the_window_title_is_the_products_name_and_the_top_left_is_its_logo() -> None:
    """asq's window title is the product's name, rlx (#133's PRODUCT_NAME; #135, Q11). The
    sidebar's header read the same constant until the owner's logo took the top-left corner
    (R23): r, a backslash, x, while the title stays rlx."""
    from aisquare.cli.ui.app import FleetApp
    from aisquare.cli.ui.sidebar import fleet_title
    from aisquare.core.version import PRODUCT_LOGO, PRODUCT_NAME

    assert FleetApp.TITLE == PRODUCT_NAME == "rlx"
    assert fleet_title(0).plain == PRODUCT_LOGO == r"r\x"
    assert list(PRODUCT_LOGO) == ["r", "\\", "x"]


def test_the_command_hints_print_is_one_the_install_writes() -> None:
    """``program.COMMAND`` is one of the commands this install writes beside ``aisquare`` and
    ``asq`` (#133's ``install_route.NEW_COMMANDS``), so ``ours_on_path`` can find it there."""
    from aisquare.services import install_route

    assert program.COMMAND in install_route.NEW_COMMANDS


def test_an_agents_board_and_the_watch_line_say_the_protocols_word() -> None:
    """What an agent reads and runs names ``program.PROTOCOL`` whichever word hints print
    (Q9, brief 180); only the human board's footer takes the program's word. An agent's board
    footer and the manager's watch line on ``word()`` (the tester's X1 and X3, seq 12524)
    fail here."""
    from datetime import UTC, datetime

    from aisquare.models import ProjectInfo, TeamSession, TeamTask
    from aisquare.services import team as team_service
    from aisquare.services import watch

    assert program.word() == "rlx" != program.PROTOCOL == "aisquare"  # conftest's pin
    now = datetime(2026, 11, 16, tzinfo=UTC)
    project = ProjectInfo(id="prj_words", root=Path("/work/words"))
    coder = TeamSession(
        id="sess-coder-words",
        project_id=project.id,
        role="coder",
        started_at=now,
        last_seen_at=now,
    )
    tasks = [
        TeamTask(
            id=f"tsk_{n:04d}",
            project_id=project.id,
            number=n,
            key=f"task-{n}",
            title=f"task {n}",
            status="todo",
            created_at=now,
            updated_at=now,
        )
        for n in range(1, 12)
    ]

    agents = team_service._render_board(project, [coder], tasks, [], me=coder)
    human = team_service._render_board(project, [coder], tasks, [], me=None)
    assert "… 3 more — `aisquare task list`" in agents, agents
    assert "… 3 more — `rlx task list`" in human, human

    shown = Path("/watch/p/20261116T000000Z.json")  # as this platform writes it
    line = watch.event_text(
        [watch.WatchItem("i1", project.id, "p", "permission", "a dialog")],
        shown,
    )
    assert line.endswith(f": aisquare watch show {shown}"), line


def test_what_the_mcp_server_tells_its_agent_says_the_protocols_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP server's solo instructions and its "orchestrator is not active" tool error are
    read by the agent, which runs what they name: the protocol's word (Q9; the tester's
    observation, seq 12524)."""
    from aisquare.services import mcp_server

    assert program.word() == "rlx"  # conftest's pin
    assert mcp_server.SOLO_INSTRUCTIONS.startswith("aisquare's shared task board")

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="orchestrator is not active") as refused:
        mcp_server._ensure_virtual_session()
    assert "start `aisquare serve` from the project" in str(refused.value)
    assert "or run `aisquare team on` there" in str(refused.value)


# ---------------------------------------------------------------------------- the guard

_SRC = Path(__file__).resolve().parent.parent / "src" / "aisquare"

#: Text R3 keeps as it is (the manager's rulings, brief 180; the owner's, seq 12065), whole files:
_KEEP_FILES: dict[str, str] = {
    **{
        name: "the AISquare integration, not rebranded (owner, seq 12065)"
        for name in (
            "cli/auth.py",
            "cli/explainability.py",
            "cli/ui/views/explainability.py",
            "services/credits.py",
            "services/destinations.py",
            "services/device_flow.py",
            "services/explainability.py",
            "services/explainability_ops.py",
            "services/iam.py",
            "services/ci_augment.py",
            "services/ci_doctor.py",
            "services/ci_client.py",
            "core/insights.py",
        )
    },
    **{
        name: "the agents' protocol, which names its commands with aisquare (Q9)"
        for name in (
            "core/injection.py",
            "core/prompt_origin.py",
            "core/ledger.py",
            "services/briefs.py",
            "services/command_reports.py",
            "services/parent.py",
            "cli/brief.py",
            "cli/command_reports.py",
            "cli/task.py",
        )
    },
    "core/program.py": "the one place the word is named",
    "core/spawn.py": "the inventory of process seams the tests walk; never printed",
    "core/agent_adapters/claude_usage.py": "usage traces, recorded as data",
    "core/agent_adapters/codex_usage.py": "usage traces, recorded as data",
}
#: ... and single functions (qualified names), the agents' protocol and the integration's rows:
_KEEP_FUNCS: dict[tuple[str, str], str] = {
    **{
        ("services/team.py", name): "the agents' protocol (Q9)"
        for name in (
            "_assignment_lines",
            "_captain_briefing",
            "_delegated_board",
            "_protocol_lines",
            "_render_delta",
            "_render_wakeup",
            "_role_cycle",
            "_start_pack",
            "short_line",
            "DeliveryUnconfirmedError.__init__",
        )
    },
    **{
        ("core/harness.py", name): "the agents' role cycles (Q9)"
        for name in ("_role_cycle_core", "_ui_start", "_verify_start", "<module>")
    },
    **{
        ("services/fleet.py", name): "an agent's fork, hand-off, restart or resume brief (Q9)"
        for name in ("_fork_prompt", "_handoff_prompt", "_restart_prompt", "_resume_prompt")
    },
    ("services/handoff.py", "HandoffBrief._tail"): "an agent's hand-off brief (Q9)",
    ("services/captain/actions.py", "_wololo"): "what the captain tells an agent (Q9)",
    ("services/watch.py", "event_text"): "the watch line a manager runs through its broker",
    **{
        ("services/store_broker.py", name): "the store broker's replies to a sandboxed agent"
        for name in ("vet", "_refused", "forward", "Broker._run", "ask", "_ring", "_runs")
    },
    ("services/diagnostics.py", "_workspace_credits_check"): "the AISquare integration",
    ("services/diagnostics.py", "_minted_keys_check"): "the AISquare integration",
    ("services/diagnostics.py", "_check_remote_control"): "explainability: the integration",
    ("services/lifecycle.py", "_explainability_step"): "explainability: the integration",
    ("cli/root.py", "init"): "init --explainability's help: the integration",
    ("cli/team.py", "show"): "its help describes the agents' one-line board update (Q9)",
}
#: ... and single literals, by the text they start with:
_KEEP_TEXTS: dict[tuple[str, str], str] = {
    ("services/captain/actions.py", "(from the captain — answer with: aisquare note"): (
        "signs a captain's note to an agent (Q9)"
    ),
    ("services/store_broker.py", "seal the report an `asq exec`"): "the broker's own table",
    ("services/diagnostics.py", "asq instances"): "the check's name, also its --json name",
    ("services/captain/gate.py", "aisquare captain"): "the TOTP issuer, in enrolled apps",
    ("core/tmux.py", "# aisquare fleet — private tmux server"): "a generated file's header",
    ("services/fleet.py", "# aisquare fleet worktrees"): "a generated file's header",
    ("services/context.py", "# aisquare context"): "an exported file's header",
    ("cli/hook.py", "this session starts with no rlx context"): "the product's name",
    ("cli/watch.py", "rlx board"): "the board window's title (Q11)",
    ("services/remote_push.py", "This is the test you asked for from rlx remote."): (
        "the product's name in a test notification"
    ),
}


#: ... and argv lists or tuples that start with the word aisquare (brief 215), by function. A
#: command shown to a person takes the word hints print (``prog.word()``); these are not that:
_KEEP_ARGVS: dict[tuple[str, str], str] = {
    # (c) data the program reads, not a command it shows:
    ("core/agent_adapters/lasting.py", "_names_aisquare"): "a process's argv, compared",
    ("core/instance_scan.py", "ui_command"): "a process's argv, compared",
    ("services/diagnostics.py", "_check_commands"): "the program's names, each one",
    # a command another program runs: the release a move back installs predates the rename
    # (it lacks refresh-hooks), and answers only to aisquare.
    ("services/lifecycle.py", "_left_by_move_back"): "run by the older release moved back to",
}


def _integration(why: str) -> bool:
    """Whether a kept entry is kept as the AISquare integration's (its reason says so): its
    commands keep their word, and it says rlx, never "the CLI" (R21, owner 2026-11-17)."""
    return "integration" in why


def _subcommands() -> list[str]:
    import typer.main

    return sorted(typer.main.get_command(app).commands)  # type: ignore[attr-defined]


def _scopes(tree: object) -> dict[int, str]:
    """id(node) -> the qualified name of the function or class around it."""
    import ast

    where: dict[int, str] = {}

    def visit(node: ast.AST, q: str) -> None:
        for child in ast.iter_child_nodes(node):
            cq = q
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                cq = f"{q}.{child.name}" if q else child.name
            where[id(child)] = cq or "<module>"
            visit(child, cq)

    assert isinstance(tree, ast.AST)
    visit(tree, "")
    return where


def _scan() -> set[tuple[str, int, str, str]]:
    """(file, line, kind, text) for every user-facing literal that names a command with a
    hard-coded word: an old one (aisquare, asq) anywhere, rlx outside static help, or calls
    the product "the CLI". What R3 keeps for the AISquare integration (:func:`_integration`)
    keeps its words, and is still read for "the CLI": the owner's R21 (2026-11-17) made that
    rlx there too."""
    import ast
    import re

    subs = "|".join(map(re.escape, _subcommands()))
    lead = r"(?:(?<=\\n)|(?<![\w./~@{$\\-]))"
    flags = r"(?:(?:--json|--no-color|-q|--quiet|-v|--verbose) )*"
    old = re.compile(lead + r"(?:aisquare|asq)(?= " + flags + r"(?:" + subs + r")\b)")
    new = re.compile(
        lead + r"rlx(?= " + flags + r"(?:" + subs + r")\b)"
        r"(?! instances (?:have|on)\b)(?! board — )"  # rlx instances (a noun); a heading
    )
    cli = re.compile(r"\b(?:[Tt]his|[Tt]he|an earlier|our own) CLI\b|\bCLI sign-in\b")
    shield = re.compile(r"`?aisquare hook\b[^`]*`?|-m aisquare\S*|aisquare-[\w-]+|aisquare@\S+")
    found: set[tuple[str, int, str, str]] = set()
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _KEEP_FILES and not _integration(_KEEP_FILES[rel]):
            continue
        kept_file = rel in _KEEP_FILES
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scope = _scopes(tree)
        bare = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
        helps = {
            id(sub)
            for n in ast.walk(tree)
            if isinstance(n, ast.keyword) and n.arg == "help"
            for sub in ast.walk(n.value)
        }
        commands = {
            id(fn.body[0].value)
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
            and fn.body
            and isinstance(fn.body[0], ast.Expr)
            and any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                for d in fn.decorator_list
            )
        }
        parts = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
        for node in ast.walk(tree):
            if id(node) in parts:
                continue
            if (
                isinstance(node, ast.List | ast.Tuple)
                and node.elts
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value == "aisquare"
            ):
                # An argv that hard-codes the word: the literal sweep below reads strings alone,
                # and missed every hint built from a list (brief 215).
                where = (rel, scope.get(id(node), "<module>"))
                if not kept_file and where not in _KEEP_FUNCS and where not in _KEEP_ARGVS:
                    words = [e.value if isinstance(e, ast.Constant) else "…" for e in node.elts]
                    found.add((rel, node.lineno, "argv", " ".join(map(str, words))[:120]))
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(
                    str(v.value) if isinstance(v, ast.Constant) else "{…}" for v in node.values
                )
            else:
                continue
            static = id(node) in helps or id(node) in commands
            if id(node) in bare and not static:
                continue  # developer prose
            func = _KEEP_FUNCS.get((rel, scope.get(id(node), "<module>")))
            if func is not None and not _integration(func):
                continue
            if any(rel == f and text.startswith(t) for f, t in _KEEP_TEXTS):
                continue
            kept = kept_file or func is not None
            plain = shield.sub(lambda m: "\x01" * len(m.group(0)), text)
            kinds = [
                kind
                for kind, hit in (
                    ("old word", None if kept else old.search(plain)),
                    ("hard-coded rlx", None if kept or static else new.search(plain)),
                    ("the CLI", cli.search(plain)),
                )
                if hit
            ]
            if kinds:
                found.add((rel, node.lineno, ",".join(kinds), text[:120]))
    return found


def test_no_user_facing_text_names_a_command_with_a_hard_coded_word() -> None:
    """Every printed command takes its word from core.program (the manager's Q2, brief 180):
    no literal `aisquare <sub>` or `asq <sub>`, and no literal `rlx <sub>` outside help text
    (static, read at import). What R3 keeps is listed above, each with why."""
    assert sorted(_scan()) == []


def test_every_kept_entry_is_still_in_the_source() -> None:
    """An entry whose file, function or text is gone is dropped, not left to cover another."""
    for rel in _KEEP_FILES:
        assert (_SRC / rel).is_file(), rel
    for rel, text in _KEEP_TEXTS:
        assert text in (_SRC / rel).read_text(encoding="utf-8"), (rel, text)
    for rel, name in _KEEP_ARGVS:
        assert f"def {name.split('.')[-1]}(" in (_SRC / rel).read_text(encoding="utf-8"), rel


def test_the_guard_finds_what_it_should_and_passes_what_it_should(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = (
        '"""aisquare doctor in a docstring is developer prose."""\n'
        'A = "run aisquare doctor to see"\n'
        'B = f"`{prog.word()} accounts add` makes one"\n'
        'C = "`rlx accounts add` makes one"\n'
        'D = "rlx is quitting"\n'
        'E = "keys the CLI minted"\n'
        'F = "each running `aisquare hook stop`"\n'
        'G = "pip install aisquare-cli[voice]"\n'
        'H = ["aisquare", "accounts", "add"]\n'
        'I = (prog.word(), "accounts", "add")\n'
        'J = ["aisquare-cli", "x"]\n'
    )
    root = tmp_path / "aisquare"
    (root / "cli").mkdir(parents=True)
    (root / "cli" / "fake.py").write_text(module, encoding="utf-8")
    # The AISquare integration keeps its words, and says rlx, never "the CLI" (R21):
    (root / "cli" / "auth.py").write_text(
        'K = "run aisquare login; it forgets the keys the CLI minted"\n'
        'L = "CLI sign-in is temporarily paused."\n'
        'M = "run aisquare login; it forgets the keys rlx minted"\n',
        encoding="utf-8",
    )
    # ... and what is kept for another reason is not read at all (the agents' protocol):
    (root / "core").mkdir()
    (root / "core" / "injection.py").write_text('N = "keys the CLI minted"\n', encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_SRC", root)
    found = sorted((rel, kind, text) for rel, _line, kind, text in _scan())
    assert found == [
        ("cli/auth.py", "the CLI", "CLI sign-in is temporarily paused."),
        ("cli/auth.py", "the CLI", "run aisquare login; it forgets the keys the CLI minted"),
        ("cli/fake.py", "argv", "aisquare accounts add"),
        ("cli/fake.py", "hard-coded rlx", "`rlx accounts add` makes one"),
        ("cli/fake.py", "old word", "run aisquare doctor to see"),
        ("cli/fake.py", "the CLI", "keys the CLI minted"),
    ]


def test_no_module_names_the_word_at_import(tmp_path: Path) -> None:
    """The word is the PATH's and the install's when a line is said, never frozen at import: a
    module constant that called program.word() kept the word its import found, and on every
    hook it would look up PATH and walk the install's RECORD for nothing."""
    import subprocess

    probe = (
        "import importlib, pkgutil\n"
        "from aisquare.core import program\n"
        "calls = []\n"
        "real = program.word\n"
        "program.word = lambda: calls.append(1) or real()\n"
        "import aisquare\n"
        "for info in pkgutil.walk_packages(aisquare.__path__, 'aisquare.'):\n"
        "    if info.name == 'aisquare.__main__':\n"
        "        continue\n"
        "    try:\n"
        "        importlib.import_module(info.name)\n"
        "    except ImportError:\n"
        "        pass  # an optional extra this environment lacks\n"
        "print(len(calls))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=300, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "0", done.stdout
