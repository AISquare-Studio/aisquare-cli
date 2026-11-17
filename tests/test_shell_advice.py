"""The shared messages say no command in asq, and the shell's text is unchanged (R13b, #146).

The owner: "the whole purpose of TUI is to make it for users to never use the command line
since their manager is aware of it, so hide all of that". R13a reworded asq's own strings
(``tests/test_asq_names_no_command.py``). The messages asq shares with the CLI — a
FleetError, a receipt's notes, an account's refusal — are built once, for both: each
clause that tells a person a command to type goes through ``program.shell_advice``, which
keeps it for a shell and drops it (or says asq's way) while asq is up. The CLI's text is
byte-for-byte what it was.

The guard: every call in ``services/**`` and ``core/**`` that spells a command sits inside
``shell_advice(...)``, or is one of what is never a person's to type in asq:

- ``agent``: text for an agent, which acts on the command wherever it is shown;
- ``data``: a command returned or run as data (argv, a hook's command line), not shown as advice;
- ``doctor-fix``: the doctor's ``fix``, the shell's; asq's Doctor says ``ui_way`` instead;
- ``cli``: what only the shell runs, proven by the call graph: nothing asq runs reaches it;
- ``phone``: the Remote page's own text, a web UI of its own (R13 design, B.6).
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from aisquare.cli.ui.views.doctor import COMMAND
from aisquare.core import program
from aisquare.core.selfcli import CliResult
from aisquare.models import TeamEvent

SRC = Path(__file__).resolve().parents[1] / "src" / "aisquare"

SPELLERS = frozenset(
    {
        "word",
        "words",
        "command_line",
        "fleet_command",
        "onboard_command",
        "forget_command",
        "config_dir_command",
        "trust_command",
        "claude_plugin_command",
    }
)
"""The calls that spell a command: ``prog.word()`` starts every one a hint prints (R3)."""

ADVICE = frozenset({"shell_advice", "detail_advice"})
"""What keeps a clause for a shell and drops it in asq (``core.program``)."""

DOCTORS = frozenset({"services/diagnostics.py", "services/explainability_ops.py"})
"""The doctors: ``doctor``'s rows, and ``explainability doctor``'s."""

DOCTOR_ROWS = frozenset({"_warn", "_fail", "degrade", "DoctorCheck"})
"""Their row builders: the ``fix`` (third, or ``fix=``) is the doctor's fix, the shell's."""

ALLOWED: dict[str, str] = {
    "core/sandbox_roots.py::forget_argv": "data: returns the argv; its callers wrap it",
    "services/adopt.py::adopt": (
        "data: the argv that runs an agent in its account, which server_note wraps"
    ),
    "services/diagnostics.py::_check_role_bindings": "doctor-fix: the connect its fix names",
    "core/agent_adapters/claude_code.py::<module>": (
        "data: a third party's install line; asq links the install page instead (R13a)"
    ),
    "core/agent_adapters/claude_code.py::ClaudeCodePlugin.command": (
        "data: returns the command line; the doctor's fix says it"
    ),
    "core/agent_adapters/claude_installs.py::_plugins": (
        "data: an install's removal command, carried in its record"
    ),
    "core/agent_adapters/claude_installs.py::mcp_removal": (
        "data: returns the command line that removes the server"
    ),
    "core/agent_adapters/claude_usage.py::_attachment": (
        "data: a trace line naming the hook that ran"
    ),
    "core/agent_adapters/codex.py::<module>": (
        "data: a third party's install line; asq links the install page instead (R13a)"
    ),
    "core/agent_adapters/codex.py::CodexAdapter": (
        "data: a third party's install line; asq links the install page instead (R13a)"
    ),
    "core/agent_adapters/codex.py::_aimed": (
        "data: returns a command line aimed at an account's home"
    ),
    "core/claude_accounts.py::<module>": (
        "data: a third party's install line; asq links the install page instead (R13a)"
    ),
    "core/codex_accounts.py::<module>": (
        "data: a third party's install line; asq links the install page instead (R13a)"
    ),
    "core/experimental.py::captain_off": (
        "agent: what asq's ui receiver answers the captain with; asq's own spawn of the captain"
        " wraps it"
    ),
    "core/harness.py::<module>": "agent: the role cycle and start pack an agent reads",
    "core/harness.py::_role_cycle_core": "agent: the role cycle an agent reads",
    "core/harness.py::_ui_start": "agent: the start pack an agent reads",
    "core/harness.py::_verify_start": "agent: the start pack an agent reads",
    "core/launchers.py::Launcher.label": (
        "data: names a running launch process, not a command to type"
    ),
    "core/spawn.py::<module>": (
        "data: the seam register's reasons, read by developers and its own tests"
    ),
    "core/store.py::damaged_store_recovery": (
        "doctor-fix: the database row's fix and the CLI's hint; the store's own messages wrap it"
    ),
    "core/store.py::store_recovery": (
        "doctor-fix: the CLI's hint for a store that will not open; the store's own message "
        "wraps it"
    ),
    "core/tmux.py::<module>": "data: the private tmux server's configuration file",
    "services/agent_launch.py::UnknownWrapperError.__init__": (
        "data: its fix, the line `team` and the doctor give as the fix; its message wraps it"
    ),
    "services/agents.py::claude_plugin_command": "data: returns the command line",
    "services/agents.py::config_dir_command": "data: returns the command line; remedies wrap it",
    "services/auto_mode.py::_mode_step": (
        "data: returns the step's command; spawn_note wraps it, the doctor's fix says it"
    ),
    "services/auto_mode.py::_remedy": "doctor-fix: the explainability doctor's fix",
    "services/auto_mode.py::_restart_off_auto": (
        "data: returns the restart's command line; spawn_note wraps it"
    ),
    "services/briefs.py::_command_of": "data: the command a report ran, shown as what ran",
    "services/captain/__main__.py::main": (
        "cli: `python -m aisquare.services.captain`'s own help and its stderr"
    ),
    "services/captain/actions.py::<module>": (
        "agent: how the manager answers the captain, appended to its question"
    ),
    "services/captain/actions.py::_no_owners_dialog": (
        "agent: an action's refusal, read by the captain agent"
    ),
    "services/captain/actions.py::_wololo": "agent: typed to the agent the captain reassigned",
    "services/captain/actions.py::run_stdio": (
        "cli: the MCP server's own command line, as it names itself"
    ),
    "services/captain/gate.py::<module>": (
        "data: the authenticator's issuer name, and the extra's install line"
    ),
    "services/captain/gate.py::enroll_command": "data: returns the command line",
    "services/captain/voice.py::<module>": "data: the voice extra's install line",
    "services/claude_accounts.py::trust_command": (
        "data: returns the command line; its callers wrap it"
    ),
    "services/code.py::<module>": "data: the extra's install line, which `code` prints at a shell",
    "services/code.py::index_hint": (
        "data: returns the command line; `code` prints it at a shell, and its refusal is the "
        "agents'"
    ),
    "services/destinations.py::<module>": (
        "doctor-fix: REVOKE_RETRY, the revocation row's fix and the CLI's hint; revoke_retry() "
        "says it where asq may show it"
    ),
    "services/diagnostics.py::<module>": "doctor-fix: the global install line the fixes name",
    "services/diagnostics.py::_account_limit_checks": (
        "doctor-fix: the switches its row's fix names"
    ),
    "services/diagnostics.py::_check_claude_code": "doctor-fix: the connects its row's fix names",
    "services/diagnostics.py::_check_database": (
        "doctor-fix: the copy (cp) its fix says to keep; the search its detail names is "
        "detail_advice"
    ),
    "services/diagnostics.py::_check_dead_managers": "doctor-fix: the restarts its row's fix names",
    "services/diagnostics.py::_check_fleet": "doctor-fix: the fleet row's fix",
    "services/diagnostics.py::_check_instances": "data: 'asq instances' is the row's name",
    "services/diagnostics.py::_check_other_agents": "doctor-fix: the coding-agent rows' fixes",
    "services/diagnostics.py::_check_remote_control": "doctor-fix: the Remote Control row's fix",
    "services/diagnostics.py::_check_role_bindings.unset": "doctor-fix: returns the fix line",
    "services/diagnostics.py::_coders_missed": (
        "doctor-fix: the connects its rows' fixes name; its detail's clause says them in "
        "detail_advice"
    ),
    "services/diagnostics.py::_disconnect_fix": "doctor-fix: returns the fix",
    "services/diagnostics.py::_left_from_before_capture": (
        "data: the shell's account of the captured projects; its row says it as detail_advice, "
        "with asq's summary"
    ),
    "services/diagnostics.py::_minted_keys_check": "doctor-fix: the minted-keys row's fix",
    "services/diagnostics.py::_plugin_runner": "doctor-fix: the plugin runner row's fix",
    "services/diagnostics.py::_plugin_uninstall": "doctor-fix: returns the fix",
    "services/diagnostics.py::_reconnect": (
        "doctor-fix: Claude Code's fix, a button on the Doctor page"
    ),
    "services/diagnostics.py::_shared_number_checks": "doctor-fix: the shared-number row's fix",
    "services/diagnostics.py::_short_timeouts": "doctor-fix: the connects its row's fix names",
    "services/diagnostics.py::install_hint": (
        "data: a third party's install line; asq links the install page instead (R13a)"
    ),
    "services/explainability.py::<module>": (
        "data: the extra's install line (INSTALL_HINT and its kin); where shown, its users wrap it"
    ),
    "services/explainability_ops.py::<module>": (
        "doctor-fix: the explainability doctor's shared fixes (_PROXY_FIX, RESET_COMMAND); asq "
        "shows a fix only when it names no command"
    ),
    "services/explainability_ops.py::_check_config": "doctor-fix: the config row's fix",
    "services/explainability_ops.py::_check_outbox": "doctor-fix: the spool row's fix",
    "services/explainability_ops.py::_rerun_live": "data: returns the command line",
    "services/explainability_ops.py::apply_fixes": (
        "doctor-fix: what `doctor --fix` reports it did, at a shell"
    ),
    "services/explainability_ops.py::deployment_fix": "doctor-fix: returns the fix",
    "services/explainability_ops.py::probe_ingest": (
        "data: the probe's attribute that names what sent it"
    ),
    "services/explainability_ops.py::routing_sources": (
        "doctor-fix: the routing row's fix; asq shows only where"
    ),
    "services/fleet.py::_dead_team_line": "agent: what a restarted manager is told of its team",
    "services/fleet.py::_exclude_worktrees": "data: the comment it writes into .git/info/exclude",
    "services/fleet.py::_exited_ways": "data: the ways out _exited_note says as shell advice",
    "services/fleet.py::_fork_prompt": "agent: the fork's first prompt",
    "services/fleet.py::_forkable": "data: names the command the fork ran",
    "services/fleet.py::_handoff_prompt": "agent: the replacement's first prompt",
    "services/fleet.py::_launch_record": (
        "agent: the way out its board note names (_say_refused); its refusal wraps it"
    ),
    "services/fleet.py::_paused_by_the_owner": (
        "agent: the refusal to one who is not the owner, who asks the owner"
    ),
    "services/fleet.py::_refuse_an_agents_opening_command": (
        "agent: the refusal of a spawn an agent makes"
    ),
    "services/fleet.py::_refuse_an_agents_setting_command": (
        "agent: the refusal of what an agent types"
    ),
    "services/fleet.py::_refuse_by_group": (
        "agent: the refusal of a spawn an agent makes in a paused group"
    ),
    "services/fleet.py::_restart_prompt": "agent: the restarted agent's first prompt",
    "services/fleet.py::_resume_prompt": "agent: the moved agent's first prompt",
    "services/fleet.py::_tell": "data: returns the command line; its callers wrap it",
    "services/fleet.py::pause_agent": (
        "agent: a stored board event (agent_paused); asq's board drops its tail (watch.shown_text)"
    ),
    "services/fleet.py::switch": "cli: `fleet switch`'s refusals, printed at a shell",
    "services/handoff.py::HandoffBrief._tail": "agent: the brief the agent taking over reads",
    "services/hooks.py::_warn": (
        "agent: a stored board event agents act on; asq's board drops its tail "
        "(watch.drop_switch_tail)"
    ),
    "services/hooks.py::capture_prompt": "agent: a hook's stderr, in the agent's own pane",
    "services/hooks.py::check_before_limit": "agent: a hook's stderr, in the agent's own pane",
    "services/hooks.py::fresh_replacement_refusal": (
        "cli: `fleet switch`'s refusal, printed at a shell"
    ),
    "services/hooks.py::hand_over": (
        "agent: a stored board event agents act on; asq's board drops its tail "
        "(watch.drop_switch_tail)"
    ),
    "services/hooks.py::remember_the_model": "agent: a hook's stderr, in the agent's own pane",
    "services/hooks.py::remember_the_start": "agent: a hook's stderr, in the agent's own pane",
    "services/install_route.py::<module>": (
        "data: the installer's one-liner, the last resort a shell's failure names"
    ),
    "services/install_route.py::InstallRoute.describe": (
        "data: 'a uv tool at …' names the install, not a command"
    ),
    "services/install_route.py::InstallRoute.manager": (
        "data: 'uv tool' names the manager, not a command"
    ),
    "services/install_route.py::cutoff": "data: returns the flag's command-line form",
    "services/lifecycle.py::UninstallPlan.package_command": "data: returns the command line",
    "services/lifecycle.py::UpgradePlan.command": "data: returns the command line",
    "services/lifecycle.py::UpgradePlan.config_warning": (
        "cli: the upgrade plan's warning, printed at a shell"
    ),
    "services/lifecycle.py::UpgradePlan.tools_warning": (
        "cli: the upgrade plan's warning, printed at a shell"
    ),
    "services/lifecycle.py::_left_by_move_back": (
        "data: the command a site left by a move back carries, for the upgrade plan"
    ),
    "services/lifecycle.py::_left_without_agent_hooks": (
        "data: the command a site left by a move back carries, for the upgrade plan"
    ),
    "services/lifecycle.py::_uninstall_notes": "cli: the uninstall's notes, printed at a shell",
    "services/lifecycle.py::fleet_shutdown": "data: returns the command line",
    "services/lifecycle.py::uninstall": "data: 'aisquare hook(s)' names the hooks, not a command",
    "services/lifecycle.py::upgrade": "cli: the upgrade's notes, printed at a shell",
    "services/mcp_server.py::_serve_stdio_until_idle.watchdog": (
        "cli: the MCP server's stderr, read by the agent that runs it"
    ),
    "services/model_memory.py::_say_unread": "agent: a launch's stderr, in the agent's own pane",
    "services/onboarding.py::FixCommand.label": (
        "data: the fix's shell form; asq says its title (R13a)"
    ),
    "services/onboarding.py::_shown_command": "data: the line that ran; its message wraps it",
    "services/parent.py::briefing": "agent: the parent manager's briefing",
    "services/parent.py::routed_text": "agent: a routed line the parent manager reads",
    "services/project.py::BoardReader.frees": (
        "data: the commands that free a board; their callers wrap them"
    ),
    "services/project.py::fleet_command": "data: returns the command line",
    "services/project.py::forget_command": "data: returns the command line",
    "services/project.py::onboard_command": "data: returns the command line",
    "services/remote_push.py::<module>": (
        "data: a phone notification's text, 'from rlx remote' names where it was asked"
    ),
    "services/remote_server.py::_remote_serve_off": (
        "cli: the auto-off's failure, said by `remote serve` at its shell"
    ),
    "services/remote_server.py::_remote_turn_pinned": (
        "phone: a 404 the Remote page shows; the phone page is its own web UI (R13 design, B.6)"
    ),
    "services/remote_server.py::_remote_uv_tool_hint": (
        "data: the install line remote_install_hint returns; its callers wrap it"
    ),
    "services/remote_server.py::_unlock_lockout_alert": "data: a log line",
    "services/remote_server.py::locked_out": "phone: a 403 the Remote page shows (B.6)",
    "services/remote_server.py::lockout_alert": "phone: the alert pushed to the phones (B.6)",
    "services/remote_server.py::remote_install_hint": (
        "data: returns the install line; its callers wrap it"
    ),
    "services/remote_server.py::revoke_later": "phone: a 503 the Remote page shows (B.6)",
    "services/remote_server.py::revoke_unsaved": "phone: a 503 the Remote page shows (B.6)",
    "services/remote_server.py::too_many_devices": "phone: a 409 the Remote page shows (B.6)",
    "services/store_broker.py::_remedy": "agent: the broker's refusal, read by the sandboxed agent",
    "services/store_broker.py::ask": "agent: the broker's refusal, read by the sandboxed agent",
    "services/store_broker.py::sandbox_environment": (
        "agent: the broker's refusal, read by the sandboxed agent"
    ),
    "services/store_broker.py::vet": "agent: the broker's refusal, read by the sandboxed agent",
    "services/team.py::_assignment_lines": "agent: the assignment text an agent reads",
    "services/team.py::_delegated_board": "agent: the board text an agent reads",
    "services/team.py::_limited_text": (
        "agent: a stored board event agents act on; asq's board drops its tail "
        "(watch.drop_switch_tail)"
    ),
    "services/team.py::_protocol_lines": "agent: the protocol an agent reads",
    "services/team.py::_render_board": "agent: the board text an agent reads",
    "services/team.py::_render_delta": "agent: the board delta an agent reads",
    "services/team.py::_render_wakeup": "agent: the wake-up an agent reads",
    "services/team.py::_role_cycle": "agent: the role cycle an agent reads",
    "services/team.py::_start_pack": "agent: the start pack an agent reads",
    "services/team.py::fleet_switches": (
        "data: each limited session's switch command; the board's chip says it only at a shell "
        "(cli/watch.py)"
    ),
    "services/team.py::short_line": "agent: an agent's one-line view of an event",
}
"""``path::qualname`` → why a command spelt there is not a person's to type in asq."""


def _callee(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _spells(node: ast.AST, unread: set[int]) -> bool:
    """A call that spells a command, or a literal that is one (asq's own pattern, R13a)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return id(node) not in unread and COMMAND.search(node.value) is not None
    if not isinstance(node, ast.Call) or _callee(node) not in SPELLERS:
        return False
    func = node.func
    if _callee(node) in ("word", "words"):  # only the program's: ``prog.word()``
        return isinstance(func, ast.Attribute) and getattr(func.value, "id", None) in (
            "prog",
            "program",
        )
    return True


def _in_a_doctor_fix(chain: list[ast.AST]) -> bool:
    for parent, child in zip(chain[1:], chain, strict=False):
        if isinstance(parent, ast.Call) and _callee(parent) in DOCTOR_ROWS:
            if any(kw.arg == "fix" and kw.value is child for kw in parent.keywords):
                return True
            if len(parent.args) > 2 and parent.args[2] is child:
                return True
    return False


def _sites() -> Iterator[tuple[str, str, int, bool]]:
    """(path::qualname, kind, line, passes) for every command spelt in services/core."""
    for base in ("services", "core"):
        for path in sorted((SRC / base).rglob("*.py")):
            if path == SRC / "core" / "program.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            rel = path.relative_to(SRC).as_posix()
            yield from _walk(tree, rel, [], [], _unread(tree))


def _unread(tree: ast.AST) -> set[int]:
    """The literals nobody is shown: docstrings, and patterns' sources (``re.compile``)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            out.add(id(node.value))
        if isinstance(node, ast.Call) and _callee(node) == "compile":
            out.update(id(inner) for inner in ast.walk(node) if isinstance(inner, ast.Constant))
    return out


def _walk(
    node: ast.AST, rel: str, scope: list[str], chain: list[ast.AST], unread: set[int]
) -> Iterator[tuple[str, str, int, bool]]:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        scope = [*scope, node.name]
    chain = [node, *chain]
    if _spells(node, unread):
        where = f"{rel}::{'.'.join(scope) or '<module>'}"
        wrapped = any(isinstance(up, ast.Call) and _callee(up) in ADVICE for up in chain[1:])
        fix = rel in DOCTORS and _in_a_doctor_fix(chain)
        kind = _callee(node) if isinstance(node, ast.Call) else "literal"
        yield where, kind or "", getattr(node, "lineno", 0), wrapped or fix
    for child in ast.iter_child_nodes(node):
        yield from _walk(child, rel, scope, chain, unread)


def test_every_command_a_shared_message_spells_is_shell_advice_or_never_a_person_s() -> None:
    unwrapped = sorted(
        {
            f"{where}:{line}"
            for where, _, line, passes in _sites()
            if not passes and where not in ALLOWED
        }
    )
    assert unwrapped == []


def test_every_allowed_place_still_spells_a_command() -> None:
    """No stale entry: an allowance outlives the command it was for and would let a new one
    through unseen. Nor one whose every command passes on its own, wrapped or in a doctor's
    fix: fleet-resume's, labelled a fix, let its row's DETAIL name `fleet restart` in asq's
    Doctor (tester 12783), and it is needed only where a command does not pass."""
    needed = {where for where, _, _, passes in _sites() if not passes}
    assert sorted(set(ALLOWED) - needed) == []


def test_every_allowance_names_its_kind() -> None:
    kinds = ("agent:", "data:", "doctor-fix:", "cli:", "phone:")
    assert [w for w, why in ALLOWED.items() if not why.startswith(kinds)] == []


def test_what_only_the_shell_runs_is_out_of_asq_s_reach() -> None:
    """A ``cli`` allowance is proven: no function asq defines reaches it, by the by-name call
    graph the config-write guard builds (an over-approximation: a name asq calls anywhere
    counts)."""
    from tests import test_config_writes_stay_in_the_cli as graph

    _, calls = graph._call_graph()
    starts = set()
    for module in (SRC / "cli" / "ui").rglob("*.py"):
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                starts.add(node.name)
    seen, todo = set(starts), list(starts)
    while todo:
        for callee in calls.get(todo.pop(), ()):
            if callee not in seen:
                seen.add(callee)
                todo.append(callee)
    reached = [
        where
        for where, why in ALLOWED.items()
        if why.startswith("cli:") and where.rsplit("::", 1)[1].split(".")[-1] in seen
    ]
    assert reached == []


# --- the switch ---------------------------------------------------------------------------------


def test_shell_advice_is_the_shell_s_outside_asq_and_asq_s_inside() -> None:
    assert program.shell_advice(" — `rlx fleet ls` shows them") == " — `rlx fleet ls` shows them"
    program.enter_app()
    try:
        assert program.shell_advice(" — `rlx fleet ls` shows them") == ""
        assert program.shell_advice("run `rlx login`", app="sign in on Accounts") == (
            "sign in on Accounts"
        )
    finally:
        program.leave_app()
    assert program.in_app() is False


def test_the_doctor_asq_runs_drops_its_details_commands_and_keeps_every_fix_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``doctor --json`` run to show its report in asq (``IN_APP_ENV``): a detail's clause
    goes; a shared message's stays, for it may be a fix, which asq hands whole to the
    manager."""
    monkeypatch.setenv(program.IN_APP_ENV, "1")
    assert program.detail_advice(" — `rlx init` sets it up") == ""
    assert program.shell_advice("; `rlx accounts` lists them") == "; `rlx accounts` lists them"
    assert program.in_app() is False


# --- the switch, up with asq ----------------------------------------------------------------


def test_the_switch_is_on_while_an_asq_app_is_up_and_off_after() -> None:
    from aisquare.cli.ui.clipboard import ClipboardApp

    seen: list[tuple[str, bool]] = []

    async def run() -> None:
        app: ClipboardApp[None] = ClipboardApp()
        seen.append(("before", program.in_app()))
        async with app.run_test() as pilot:
            await pilot.pause()
            seen.append(("up", program.in_app()))
        seen.append(("after", program.in_app()))

    asyncio.run(run())
    assert seen == [("before", False), ("up", True), ("after", False)]


def test_at_a_shell_keeps_the_shell_s_advice_and_still_drops_a_detail_s_in_asq() -> None:
    """The doctor asq runs in its own process builds every fix at a shell; its details'
    commands go all the same."""
    program.enter_app()
    try:
        with program.at_a_shell():
            assert program.shell_advice("x") == "x" and program.detail_advice("y") == ""
        assert program.shell_advice("x") == ""
    finally:
        program.leave_app()
    assert program.detail_advice("y") == "y"


# --- the shared messages, both ways ---------------------------------------------------------


def _builders() -> dict[str, Callable[[], str]]:
    from aisquare.models import UsageCause
    from aisquare.services import agents, destinations, fleet, lifecycle, remote_server
    from aisquare.services import claude_accounts as accounts
    from aisquare.services.captain import gate

    return {
        "a vanished account": lambda: accounts._vanished(None),
        "a sign-in's usage fix": lambda: accounts.usage_fix(UsageCause.not_signed_in, slot=2) or "",
        "a stale token's usage fix": lambda: (
            accounts.usage_fix(UsageCause.token_expired, slot=3) or ""
        ),
        "a key owed its revoke": destinations.revoke_retry,
        "another Remote on": remote_server.remote_already_on,
        "Remote on elsewhere": lifecycle.remote_off,
        "the captain's gate": gate.unlock_hint,
        "the captain's row in a pause": fleet.the_captain_reason,
        "the folder-trust question": lambda: fleet.trust_prompt_detail(Path("/srv/app")),
        "this shell's hook directory": agents._repoint,
    }


SHELL = {
    "a vanished account": "no Claude account in slot ? any more — it was removed meanwhile; "
    "see: rlx accounts",
    "a sign-in's usage fix": "press Log in (in a terminal: rlx accounts run 2, then /login)",
    "another Remote on": "another Remote is on for this ~/.aisquare (the rlx UI's R panel, or "
    "`rlx remote serve` in another shell) — turn it off first: two would share one link, one "
    "passphrase, one auto-off and one list of phones",
    "Remote on elsewhere": "press Ctrl-C where `rlx remote serve` runs, or turn Remote off in "
    "that rlx's R panel",
    "the captain's gate": "say your authenticator's code to the captain, or run: rlx captain "
    "gate unlock <code>",
    "the captain's row in a pause": "the captain — `rlx captain` runs it",
}
"""The shell's text, byte for byte as before R13b, for those whose words are fixed."""


@pytest.mark.parametrize("name", sorted(_builders()))
def test_a_shared_message_says_its_command_at_a_shell_and_none_in_asq(name: str) -> None:
    build = _builders()[name]
    shell = build()
    program.enter_app()
    try:
        shown = build()
    finally:
        program.leave_app()

    assert COMMAND.search(shell) or f"{program.word()} " in shell, "the shell is told it"
    assert COMMAND.search(shown) is None, shown
    assert shown and shown != shell
    if name in SHELL:
        assert shell == SHELL[name]


def test_in_asq_a_usage_fix_says_asq_s_buttons() -> None:
    from aisquare.models import UsageCause
    from aisquare.services import claude_accounts as accounts

    program.enter_app()
    try:
        assert accounts.usage_fix(UsageCause.not_signed_in, slot=2) == "press Log in"
        assert accounts.usage_fix(UsageCause.keychain_failed) == (
            "↻ Refresh usage asks the Keychain again"
        )
    finally:
        program.leave_app()


def test_the_agent_header_says_the_trust_question_s_way_in_asq() -> None:
    """asq's agent header showed ``approve it once, or `rlx accounts trust <folder>``` (the
    tester of #520, note 12700)."""
    from aisquare.services import fleet

    program.enter_app()
    try:
        said = fleet.trust_prompt_detail(Path("/srv/app"))
    finally:
        program.leave_app()
    assert said == "waiting for folder trust — approve it once in the agent's pane"


def test_an_agent_s_text_keeps_its_command_in_asq_s_process() -> None:
    """What asq's process answers an agent with (its ui receiver, the captain's refusal) is
    the agent's to act on: never shell advice."""
    from aisquare.core import experimental

    program.enter_app()
    try:
        assert "config set experimental.captain" in experimental.captain_off()
    finally:
        program.leave_app()


# --- the doctor asq runs --------------------------------------------------------------------


@pytest.mark.parametrize("how", ["in asq's own process", "run by asq as doctor --json"])
def test_the_doctor_asq_shows_keeps_every_fix_whole_and_names_no_command_in_a_detail(
    how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """asq's Doctor page runs the doctor in its own process, and its view again as
    ``doctor --json`` (``IN_APP_ENV``): either way every fix is the shell's, whole — asq hands
    it to the manager and reads its buttons from it — and no detail names a command."""
    from aisquare.services import diagnostics

    shell = diagnostics.doctor()
    if how.startswith("run"):
        monkeypatch.setenv(program.IN_APP_ENV, "1")
    else:
        program.enter_app()
    try:
        shown = diagnostics.doctor()
    finally:
        program.leave_app()

    assert [(row.name, row.fix) for row in shown] == [(row.name, row.fix) for row in shell]
    assert [row.detail for row in shown if COMMAND.search(row.detail)] == []
    assert any(COMMAND.search(row.detail) for row in shell), "a detail the switch changed"


def test_the_doctor_view_runs_the_report_it_shows_as_shown_in_asq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisquare.cli.ui.views import doctor as view
    from aisquare.core import selfcli

    told: list[str | None] = []

    def run(args: object, *, cwd: object = None, env: dict[str, str] | None = None) -> object:
        told.append((env or {}).get(program.IN_APP_ENV))
        raise OSError("not run")

    monkeypatch.setattr(selfcli, "run", run)
    with pytest.raises(OSError):
        view._shown_here(selfcli.run)(["--json", "doctor"])
    assert told == ["1"]

    def stand_in(args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
        raise AssertionError("not run")

    assert view._shown_here(stand_in) is stand_in


# --- the board -----------------------------------------------------------------------------


def _event(kind: str, text: str) -> TeamEvent:
    from datetime import UTC, datetime

    return TeamEvent(id="e", project_id="p", kind=kind, text=text, created_at=datetime.now(UTC))


STORED = {
    "limited": (
        "coder-1 hit its 5h limit · resets 14:00 — `rlx fleet switch --project api coder-1` "
        "moves it to the account with the most headroom (or wait for the reset)",
        "coder-1 hit its 5h limit · resets 14:00",
    ),
    "before_limit": (
        "coder-1: 91% used — over the 90%-used line; work has room — "
        '`rlx fleet switch --project api coder-1` moves it (`before_limit = "warn"`)',
        "coder-1: 91% used — over the 90%-used line; work has room",
    ),
    "handover_failed": (
        "coder-1: not switched — moved 3 min ago; waiting for the reset instead — "
        "`rlx fleet switch --project api coder-1 --force` moves it again; its own limit "
        "resets in 2h",
        "coder-1: not switched — moved 3 min ago; waiting for the reset instead; its own "
        "limit resets in 2h",
    ),
    "agent_paused": (
        "coder-1 paused — its claims, worktree and session are kept; "
        "`rlx fleet resume --project api` brings it back",
        "coder-1 paused — its claims, worktree and session are kept",
    ),
}


@pytest.mark.parametrize("kind", sorted(STORED))
def test_asq_s_board_leaves_out_the_command_a_stored_line_names(kind: str) -> None:
    """Agents act on the stored text, which keeps its command; asq's board and ``board -w``
    show the line without it (R13 design A.3), and a shell's board whole."""
    from aisquare.cli import watch

    stored, shown = STORED[kind]
    event = _event(kind, stored)
    assert watch.shown_text(event) == stored
    program.enter_app()
    try:
        assert watch.shown_text(event) == shown
        assert watch.shown_text(_event("note", stored)) == stored, "a note is the author's"
    finally:
        program.leave_app()


def test_asq_s_board_shows_a_limited_session_without_its_switch() -> None:
    from datetime import UTC, datetime

    from aisquare.cli import watch
    from aisquare.models import TeamSession

    now = datetime.now(UTC)
    session = TeamSession(
        id="s1", project_id="p", role="coder", state="limited", started_at=now, last_seen_at=now
    )
    switches = {"s1": "rlx fleet switch --project api coder-1"}
    shell = watch._session_lines([session], labels={}, switches=switches).plain
    program.enter_app()
    try:
        shown = watch._session_lines([session], labels={}, switches=switches).plain
    finally:
        program.leave_app()
    assert "`rlx fleet switch --project api coder-1`" in shell
    assert "fleet switch" not in shown and "limited" in shown


def test_a_session_told_apart_by_its_short_id_says_it_once_in_the_detail() -> None:
    """``note by login-fix (cd505d7a) (cd505d7a)``: the name carried the id already (the
    tester's nit on #140)."""
    from aisquare.cli import watch

    session = "cd505d7a-0000-4000-8000-000000000000"
    assert watch._named(session, {session: "login-fix (cd505d7a)"}) == "login-fix (cd505d7a)"
    assert watch._named(session, {session: "login-fix"}) == "login-fix (cd505d7a)"
    assert watch._named(session, None) == "cd505d7a"


def test_asq_s_explainability_page_shows_a_probe_fix_only_when_it_names_no_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe's remediation is the explainability doctor's fix, the shell's whole: asq's
    page shows it under the probe only when it names no command, as its Doctor does."""
    from aisquare.cli.ui.views import explainability as view
    from aisquare.models import CheckStatus
    from aisquare.services import explainability_ops as ops

    for remediation, shown in (
        ("Point it at a deployment: aisquare explainability enable --proxy-url <url>", False),
        ("Start the SDK's claude_code proxy", True),
    ):
        state = ops.ProxyState(summary="down", severity=CheckStatus.warn, remediation=remediation)
        monkeypatch.setattr(ops, "proxy_state", lambda target, on, state=state: state)
        probe = dict(view.status_report().rows)["probe"]
        assert (remediation in probe) is shown, probe


def test_the_doctor_builds_every_row_at_a_shell_in_asq_s_own_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asq's Doctor page runs the doctor in its own process: a fix built there from a shared
    message (the remedies a refused connect names) kept its command only because the whole
    report is built at a shell (``program.at_a_shell``); its details' commands still go."""
    from aisquare.services import diagnostics

    seen: list[tuple[str, str]] = []
    real = diagnostics._check_python

    def check_python() -> object:
        seen.append((program.shell_advice("a fix"), program.detail_advice("a detail")))
        return real()

    monkeypatch.setattr(diagnostics, "_check_python", check_python)
    program.enter_app()
    try:
        diagnostics.doctor()
    finally:
        program.leave_app()
    assert seen == [("a fix", "")]
