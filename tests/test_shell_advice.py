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
from collections.abc import Iterator
from pathlib import Path

import pytest

from aisquare.cli.ui.views.doctor import COMMAND
from aisquare.core import program

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
        "doctor-fix: the database row; its detail names the commands that fail there, a fact"
    ),
    "services/diagnostics.py::_check_dead_managers": "doctor-fix: the restarts its row's fix names",
    "services/diagnostics.py::_check_fleet": "doctor-fix: the fleet row's fix",
    "services/diagnostics.py::_check_instances": "data: 'asq instances' is the row's name",
    "services/diagnostics.py::_check_other_agents": "doctor-fix: the coding-agent rows' fixes",
    "services/diagnostics.py::_check_remote_control": "doctor-fix: the Remote Control row's fix",
    "services/diagnostics.py::_check_resumable_agents": (
        "doctor-fix: the restarts its row's fix names"
    ),
    "services/diagnostics.py::_check_role_bindings.unset": "doctor-fix: returns the fix line",
    "services/diagnostics.py::_coders_missed": "doctor-fix: the connects its row's fix names",
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
    "services/install_route.py::not_automated": (
        "data: the upgrade plan's reason; asq's Update dialog shows none that names a command "
        "(R13a)"
    ),
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
    "services/remote_server.py::read_only_reason": (
        "phone: every 403 read_only the Remote page and its modal show (B.6)"
    ),
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
    through unseen."""
    spelt = {where for where, _, _, _ in _sites()}
    assert sorted(set(ALLOWED) - spelt) == []


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
