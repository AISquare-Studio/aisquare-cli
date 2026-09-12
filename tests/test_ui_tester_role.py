"""The ui-tester: a first-class role that verifies user-facing work in a real browser.

Why a role and not a line in the runner's briefing: frontend tickets are most of
what PlatformQA produces, "opened the page and measured" is a different skill and
a different toolset from "ran the suite", and a name lets the planner route to
it, the manager spawn it only when UI is involved, and the runner stay the plain
code-and-tests checker.

Why the role OWNS ``--chrome``: the operator who set this up passed ``--chrome``
in a personal alias. The next operator will not. A tool the role needs is the
role's business, so ``RoleProfile.default_args`` carries it and ``launch`` adds it
wherever the role starts — a hand-started window, ``team spawn``, a fleet window —
only for the default binary, never twice, and never over an explicit
``--no-chrome``.

What the CLI can and cannot know about the browser: MCP servers and plugins are
declared in files on disk, so ``doctor`` reads them. The Claude in Chrome
extension lives in the browser and is invisible from a terminal; the row says so
instead of guessing, and the briefing tells the role to check what answers before
it starts and to reopen — never pass — a UI task it could not open in a browser.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.cli.ui.sidebar import ROLE_ICON
from aisquare.core import agents as agent_core
from aisquare.core import harness
from aisquare.core.config import (
    AppConfig,
    ExplainabilitySettings,
    ExplainabilityTarget,
    _default_fleet_roles,
    save_config,
)
from aisquare.models import CheckStatus
from aisquare.services import diagnostics
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops as ops
from aisquare.services.fleet import FLEET_ROLES

ROLE = "ui-tester"


# ── the role exists everywhere a role must ────────────────────────────────────


def test_the_role_is_wired_into_every_list_that_enumerates_roles() -> None:
    assert ROLE in launch_cli.ROLES, "launchable by name"
    assert ROLE in harness.ROLE_PROFILES, "has a model ladder"
    assert ROLE in FLEET_ROLES, "the manager can spawn it"
    assert ROLE in _default_fleet_roles(), "the fleet has a launch shape for it"
    assert ROLE in ExplainabilitySettings().roles, "its identity is registered by default"
    assert ROLE in ROLE_ICON, "the fleet UI can draw it"
    assert ROLE in harness._LANE, "the lane rule knows what it does instead"


def test_the_ladder_is_the_verifiers_ladder() -> None:
    profile = harness.ROLE_PROFILES[ROLE]
    assert profile.ladder == ["sonnet", "opus"], "same as runner and reviewer"
    assert profile.effort_offset == 0
    assert profile.default_args == ["--chrome"]


def test_a_seat_is_briefed_as_the_role() -> None:
    assert harness.base_role("ui-tester3") == ROLE
    text = " ".join(harness.role_cycle("ui-tester3", "abcd1234"))
    assert text.startswith("Your standing cycle (ui-tester)")
    assert "Stay in your lane (ui-tester)" in text


# ── the briefing ──────────────────────────────────────────────────────────────


def test_the_briefing_verifies_in_a_real_browser_and_measures() -> None:
    text = " ".join(harness.role_cycle(ROLE, "abcd1234"))
    assert "task next --status review" in text
    assert '"UI: …"' in text, "it takes the tasks the planner marked for it"
    assert "REAL browser" in text
    for tool in ("Claude in Chrome", "Chrome DevTools MCP", "Playwright MCP"):
        assert tool in text, tool
    assert "BEFORE you start" in text, "it checks which tools answer first"
    assert "MEASURE" in text and "screenshots" in text
    assert "never pass a visual requirement by reading code" in text


def test_the_briefing_degrades_honestly_without_a_browser() -> None:
    text = " ".join(harness.role_cycle(ROLE, "abcd1234"))
    assert "No browser tool answers?" in text
    assert "UI not browser-verified in this window" in text
    assert "never done" in text, "a UI task without a browser is reopened, not passed"


def test_the_briefing_is_read_only_and_pre_fills_the_session_id() -> None:
    text = " ".join(harness.role_cycle(ROLE, "abcd1234"))
    assert "never edit" in text and "never push" in text
    assert "--as abcd1234" in text


def test_the_briefing_says_which_build_it_verified() -> None:
    """The load-bearing gap in a role whose whole premise is trustworthy
    evidence: it runs with `worktree=False` in the project root, nothing moves
    it into the coder's tree, and it opens a URL, sees a working page, measures
    it honestly and `task done`s with genuine evidence of the PRE-change build.
    """
    text = " ".join(harness.role_cycle(ROLE, "abcd1234"))
    assert "no worktree" in text
    assert "SAY WHICH BUILD" in text and "branch or commit" in text
    assert "<url>" in text, "the note carries the URL it verified"
    assert "reopen it as underspecified" in text, "a task naming neither is not verifiable"


def test_the_briefing_can_reach_past_the_head_of_the_review_pool() -> None:
    """`task next --status review` returns exactly one task (`ORDER BY id`, no
    offset) and `--claim` is refused for review tasks, so a non-UI task at the
    head made this cycle stop — with the `UI:` task it owns sitting behind it.
    """
    text = " ".join(harness.role_cycle(ROLE, "abcd1234"))
    assert "HEAD of the review pool" in text
    assert "aisquare board" in text and "by id" in text


def test_the_briefing_does_not_claim_an_enforcement_it_does_not_have() -> None:
    """There is no PreToolUse hook and nothing writes an allowed-tools list, so
    "Read-only" was claimed and enforced nowhere. `--restricted` is not the fix:
    it removes the Bash this role's own verdict commands need.
    """
    text = " ".join(harness.role_cycle(ROLE, "abcd1234"))
    assert "ASKED to be read-only" in text
    assert "nothing here enforces it" in text
    assert "--restricted" not in text


def test_the_runner_names_a_verdict_for_a_ui_task_it_cannot_verify() -> None:
    """The replaced line weakened the invariant its own first clause states: an
    "otherwise" branch with no verdict reads as `task done` with a note, inside
    the sentence that forbids rubber-stamping. The ui-tester's identical
    situation is explicit ("never done"), and one fact must not resolve two ways.
    """
    text = " ".join(harness.role_cycle("runner", "abcd1234"))
    assert 'task reopen <id> --reason "UI not browser-verified"' in text
    assert "never done" in text
    tester = " ".join(harness.role_cycle("tester", "abcd1234"))
    assert 'task reopen <id> --reason "UI not browser-verified"' in tester, (
        "`tester` is the fleet's name for `runner` and shares the rule"
    )


def test_the_runner_does_not_defer_ui_tasks_to_a_stale_ui_tester() -> None:
    """Review of #112, round 2: `_render_board` keeps listing a ui-tester that
    crashed without SessionEnd (marked `(stale)`), so "when one is on the board"
    routed `UI:` work to a verifier that no longer exists. Presence is not
    availability: a stale row means the runner reopens the task itself."""
    for role in ("runner", "tester"):
        text = " ".join(harness.role_cycle(role, "abcd1234"))
        assert "NOT marked (stale)" in text, role
        assert "not an available one" in text, role
        # the board is where staleness is visible — the briefing names the marker it prints
        assert "(stale)" in text


def test_the_manager_names_the_build_when_it_spawns_a_ui_tester() -> None:
    """The tester's documented workaround, which the ui-tester needs more: it
    measures a page instead of running a suite, so it fails SILENTLY against the
    wrong tree."""
    text = " ".join(harness.role_cycle("manager", "abcd1234"))
    assert "fleet spawn ui-tester --prompt" in text
    assert "branch or worktree" in text and "URL" in text


def test_the_other_roles_know_the_ui_tester_exists() -> None:
    planner = " ".join(harness.role_cycle("planner", "abcd1234"))
    assert '"UI: …"' in planner and "browser steps" in planner, "the planner marks UI work"
    runner = " ".join(harness.role_cycle("runner", "abcd1234"))
    assert "belongs to the ui-tester" in runner
    assert "not browser-verified" in runner, "a runner alone says what it did not check"
    manager = " ".join(harness.role_cycle("manager", "abcd1234"))
    assert "fleet spawn ui-tester" in manager
    reviewer = " ".join(harness.role_cycle("reviewer", "abcd1234"))
    assert "no ui-tester evidence" in reviewer and "request-changes" in reviewer


# ── --chrome is the role's, applied exactly once, opt-out wins ───────────────


def _args(binary: str, args: list[str], role: str = ROLE) -> list[str]:
    return harness.role_defaults(role, binary=binary, args=args).args


def test_default_args_apply_to_the_default_binary_only() -> None:
    assert _args("claude", []) == ["--chrome"]
    assert _args("/usr/local/bin/claude", []) == ["--chrome"]
    assert _args("claude-next", []) == []
    assert _args("/opt/wrap/agent", []) == []


def test_default_args_reach_the_shims_and_survive_a_trailing_slash() -> None:
    """The gate was a bare-basename test, so it was too strict in three ways.

    `claude.cmd`/`claude.ps1` are what npm writes on Windows — where this CLI
    installs too (`install.ps1`) — and `os.path.basename("claude/")` is `""`,
    so a binding typed with a slash lost the flag with nothing said.
    """
    for binary in ("claude.exe", "claude.cmd", "claude.ps1", "/opt/npm/bin/claude.CMD"):
        assert _args(binary, []) == ["--chrome"], binary
    assert _args("/usr/local/bin/claude/", []) == ["--chrome"], "a trailing slash is not a name"


def test_default_args_accept_a_wrapper_named_claude() -> None:
    """Deliberate, and the docstring says so: `~/bin/claude` is the shape
    operators use to add an account or a flag to Claude Code, and from here it
    is indistinguishable from the real thing."""
    assert _args("/opt/wrap/claude", []) == ["--chrome"]
    assert harness.is_default_agent("~/bin/claude") is True
    assert harness.is_default_agent("claude2") is False


def test_default_args_never_duplicate_and_never_override_an_opt_out() -> None:
    assert _args("claude", ["--chrome"]) == []
    assert _args("claude", ["--no-chrome"]) == []
    assert _args("claude", ["--resume"]) == ["--chrome"]


def test_an_opt_out_written_with_a_value_still_opts_out() -> None:
    """`present = set(args)` was exact-token, so `--no-chrome=1` read as an
    unrelated word and `--chrome` was appended beside it."""
    assert _args("claude", ["--no-chrome=1"]) == [], "the documented opt-out wins"
    assert _args("claude", ["--chrome=1"]) == [], "already said, in the other spelling"


def test_the_opt_out_is_the_conventions_and_not_a_table() -> None:
    """`--no-<flag>` is the convention this CLI already relies on, so the next
    `default_args` entry needs no table entry."""
    assert harness._opt_out_of("--chrome") == "--no-chrome"
    assert harness._opt_out_of("--dangerously-skip-permissions") == (
        "--no-dangerously-skip-permissions"
    )


def test_a_withheld_flag_is_never_silent() -> None:
    """The sibling gate (`explainability.accepts_session_id`) returns a note that
    `launch` prints; this one printed nothing, so the operator saw a clean launch
    and a ui-tester that reopened every UI task as not browser-verified."""
    withheld = harness.role_defaults(ROLE, binary="claude-next", args=[])
    assert withheld.args == []
    assert withheld.notes and "--chrome withheld" in withheld.notes[0]
    assert "'claude-next' is not claude" in withheld.notes[0]
    quiet = harness.role_defaults(ROLE, binary="claude", args=["--no-chrome"])
    assert quiet.notes == [], "the operator's own opt-out is already on their screen"


def test_one_predicate_answers_the_binary_question_for_both_gates() -> None:
    """`role_defaults` and `--session-id` pinning ran 55 lines apart in the same
    launch, each with its own `"claude"` literal and each missing the shims."""
    for binary in ("claude", "/usr/local/bin/claude", "claude.cmd", "/opt/wrap/claude"):
        assert explainability_service.accepts_session_id(binary) is True, binary
        assert harness.is_default_agent(binary) is True, binary
    for binary in ("claude2", "claude-next", "aider"):
        assert explainability_service.accepts_session_id(binary) is False, binary
        assert harness.is_default_agent(binary) is False, binary


def test_roles_without_defaults_get_nothing() -> None:
    for role in ("coder", "planner", "runner", "reviewer", "validator", "manager", "tester"):
        assert _args("claude", [], role) == [], role
    assert _args("claude", [], "stenographer") == []


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    return captured


def test_launch_adds_chrome_for_the_ui_tester_and_for_nobody_else(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    assert runner.invoke(app, ["launch", ROLE]).exit_code == 0
    assert spy["argv"] == ["claude", "--chrome"]
    assert runner.invoke(app, ["launch", "coder"]).exit_code == 0
    assert spy["argv"] == ["claude"]


def test_launch_keeps_the_operators_word_on_chrome(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    assert runner.invoke(app, ["launch", ROLE, "--no-chrome"]).exit_code == 0
    assert spy["argv"] == ["claude", "--no-chrome"], "an opt-out is never overridden"
    assert runner.invoke(app, ["launch", ROLE, "--chrome", "--resume"]).exit_code == 0
    assert spy["argv"] == ["claude", "--chrome", "--resume"], "never twice"


def test_launch_withholds_browser_flags_from_a_declared_wrapper_and_says_so(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(
        app, ["launch", ROLE, "--agent", "claude-code", "--command", "claude-next"]
    )
    assert result.exit_code == 0, result.output
    assert spy["argv"] == ["claude-next"], "Claude Code's flag is not another agent's"
    assert "--chrome withheld" in result.output, (
        "a silent withholding is a ui-tester that reopens every UI task with nothing said"
    )


def test_spawn_prints_the_role_flag_in_the_pasteable_command(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`team spawn` prints a command meant to be pasted; a printed command that
    silently launches with different flags than the fleet's window would is the
    failure its own comment warns about."""
    monkeypatch.setenv("AISQUARE_HARNESS_PROBE", "0")
    result = runner.invoke(app, ["team", "spawn", ROLE])
    assert result.exit_code == 0, result.output
    assert "--chrome" in result.output
    assert "--model sonnet" in result.output

    bound = runner.invoke(
        app, ["--json", "team", "spawn", ROLE, "--agent", "claude-code", "--bin", "claude-next"]
    )
    assert bound.exit_code == 0, bound.output
    pasted = json.loads(bound.stdout)["command"]
    assert "--chrome" not in pasted, "Claude Code's flag is not another agent's"
    assert "--chrome withheld" in bound.output, "and the paste says what it is missing"


def test_the_identity_planner_sees_the_roles_own_args(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third argv source has to reach `plan_session_identity` too.

    Its comment records a MEASURED incident as the reason it must be handed the
    effective list: a bound `--session-id X` got a second one appended after it,
    and a bound `--continue`/`--resume` defeated the deliberate refusal to pin.
    `default_args` reopened that seam — harmless for `--chrome`, but a
    session-shaped flag is a natural fit for a role meant to reattach to a
    long-running browser window, so the invariant is tested with one.
    """
    from tests.proxy_stub import healthy_proxy

    monkeypatch.setitem(
        harness.ROLE_PROFILES,
        ROLE,
        harness.ROLE_PROFILES[ROLE].model_copy(update={"default_args": ["--continue"]}),
    )
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    with healthy_proxy() as proxy_url:
        config = AppConfig()
        config.explainability = ExplainabilitySettings(enabled=True, proxy_url=proxy_url)
        save_config(config)
        result = runner.invoke(app, ["launch", ROLE])

    assert result.exit_code == 0, result.output
    assert "X-Pipeline-Id" in spy["env"]["ANTHROPIC_CUSTOM_HEADERS"], "still traced"
    assert "--session-id" not in spy["argv"], (
        f"pinned an id onto a role-supplied --continue: {spy['argv']}"
    )
    assert spy["argv"] == ["claude", "--continue"], spy["argv"]


def test_the_harness_matrix_shows_the_roles_own_flags(
    runner: CliRunner, isolated_home: Path
) -> None:
    """`aisquare team harness` is the documented answer to "what will this role
    launch with" (docs/fleet.md), and it reported `extra_args: []` for a
    ui-tester that execs `claude --chrome`."""
    result = runner.invoke(app, ["--json", "team", "harness"])
    assert result.exit_code == 0, result.output
    rows = {row["role"]: row for row in json.loads(result.output)["roles"]}
    assert rows[ROLE]["default_args"] == ["--chrome"]
    assert rows["coder"]["default_args"] == []

    text = runner.invoke(app, ["team", "harness"])
    assert text.exit_code == 0, text.output
    assert "role_args=--chrome" in text.output


# ── doctor: what the machine declares, and what it cannot see ─────────────────


@pytest.fixture
def config_dirs(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Point the row at the given config dirs, as this home's connected ones.

    `connected_dirs` + the ambient dir, NOT `hook_sites`: the row must not grade
    anything (a second `hook_sites` scan re-ran a `--version` subprocess per
    site) and must not count directories this home never connected.
    """

    def use(*dirs: Path, ambient: Path | None = None) -> None:
        monkeypatch.setattr(agent_core, "connected_dirs", lambda name, registry=None: list(dirs))
        monkeypatch.setattr(
            agent_core,
            "_claude_home",
            lambda config_dir=None: ambient or (dirs[0] if dirs else Path("/nonexistent")),
        )

    return use


def test_doctor_reads_declared_browser_tools_from_every_config_dir(
    tmp_path: Path, config_dirs: Callable[..., None]
) -> None:
    a, b = tmp_path / "claude-a", tmp_path / "claude-b"
    a.mkdir()
    b.mkdir()
    (a / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"chrome-devtools-mcp@claude-plugins-official": True}})
    )
    (b / ".claude.json").write_text(
        json.dumps({"projects": {"/x": {"mcpServers": {"playwright": {"command": "npx"}}}}})
    )
    config_dirs(a, b)
    check = diagnostics._check_browser_tools(tmp_path)
    assert check.status is CheckStatus.ok
    assert "plugin chrome-devtools-mcp" in check.detail
    assert "mcp playwright" in check.detail
    assert "Claude in Chrome cannot be detected" in check.detail, (
        "honest about the one it cannot see"
    )


def test_doctor_names_the_directory_beside_each_tool(
    tmp_path: Path, config_dirs: Callable[..., None]
) -> None:
    """`per_dir` was built and then thrown away by `sorted({...})`, so the row
    could not answer the question an operator with four Claude dirs asks."""
    a, b = tmp_path / "claude-a", tmp_path / "claude-b"
    a.mkdir()
    b.mkdir()
    (a / ".claude.json").write_text(json.dumps({"mcpServers": {"pw": {"command": "playwright"}}}))
    (b / ".claude.json").write_text(json.dumps({"mcpServers": {"cdp": {"args": ["puppeteer"]}}}))
    config_dirs(a, b)
    check = diagnostics._check_browser_tools(tmp_path)
    assert f"mcp pw ({a})" in check.detail
    assert f"mcp cdp ({b})" in check.detail


def test_doctor_reads_the_default_installs_claude_json_beside_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dirs: Callable[..., None]
) -> None:
    """The default install keeps `.claude.json` BESIDE the config dir, at
    `~/.claude.json` — which is exactly where `claude mcp add` writes. Probing
    only `<dir>/.claude.json` left the whole layer (and the `projects` fan-out)
    dead on the common layout and told an operator to install what they had.
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text(
        json.dumps(
            {"mcpServers": {"chrome-devtools": {"command": "npx", "args": ["chrome-devtools-mcp"]}}}
        )
    )
    monkeypatch.setattr(agent_core, "_home", lambda: home)
    config_dirs(home / ".claude")
    check = diagnostics._check_browser_tools(tmp_path)
    assert "mcp chrome-devtools" in check.detail, check.detail


def test_doctor_reads_a_redirected_dirs_claude_json_from_inside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dirs: Callable[..., None]
) -> None:
    """A directory the environment redirects to keeps it INSIDE — both halves,
    from the directory itself and never from this process's environment."""
    home = tmp_path / "home"
    slot = home / ".claude-c2"
    slot.mkdir(parents=True)
    (slot / ".claude.json").write_text(json.dumps({"mcpServers": {"playwright": {}}}))
    monkeypatch.setattr(agent_core, "_home", lambda: home)
    config_dirs(slot)
    assert "mcp playwright" in diagnostics._check_browser_tools(tmp_path).detail


def test_doctor_reads_the_projects_own_mcp_json(
    tmp_path: Path, config_dirs: Callable[..., None]
) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"browser-use": {}}}))
    config_dirs(home)
    check = diagnostics._check_browser_tools(project)
    assert check.status is CheckStatus.ok
    assert "mcp browser-use" in check.detail


def test_doctor_reads_the_process_cwd_through_the_cli(
    runner: CliRunner, work_dir: Path, config_dirs: Callable[..., None]
) -> None:
    """`cli/root.py` calls `doctor()` with no cwd, and `None` means the process
    cwd — every neighbour resolves it that way. Treating it as "no project" made
    the row disagree with itself between the CLI and the fleet UI, and the CLI
    was the surface sending operators to install what their repo declares.
    """
    (work_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"playwright": {}}}))
    config_dirs(work_dir / "empty-claude")
    result = runner.invoke(app, ["--json", "doctor"])
    assert result.exit_code in (0, 1), result.output
    row = next(c for c in json.loads(result.output) if c["name"] == "browser tools")
    assert "mcp playwright" in row["detail"], row["detail"]


def test_doctor_declines_to_guess_from_a_name_alone(
    tmp_path: Path, config_dirs: Callable[..., None]
) -> None:
    """Verified false positives of the old `playwright|chrome|devtools|browser|
    puppeteer` name pattern: none of these can drive a browser, and each one
    flipped the row to "the ui-tester can measure"."""
    home = tmp_path / "claude"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "react-devtools": {"command": "react-devtools-mcp"},
                    "file-browser": {},
                    "s3-browser": {},
                    "db-browser": {},
                    "browserslist-mcp": {},
                    "chrome-history-reader": {},
                }
            }
        )
    )
    config_dirs(home)
    check = diagnostics._check_browser_tools(tmp_path)
    for guess in (
        "react-devtools",
        "file-browser",
        "s3-browser",
        "db-browser",
        "browserslist-mcp",
        "chrome-history-reader",
    ):
        assert guess not in check.detail, guess
    assert "no browser MCP/plugin declared" in check.detail


def test_doctor_credits_the_plugin_and_not_its_marketplace(
    tmp_path: Path, config_dirs: Callable[..., None]
) -> None:
    """Proven end to end on the old pattern: this exact settings.json reported
    `ok — plugin my-linter declared — the ui-tester can measure`, naming a linter
    as browser tooling because the MARKETPLACE half of the key matched, while
    the one browser-shaped plugin present was correctly disabled."""
    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps(
            {
                "enabledPlugins": {
                    "my-linter@chrome-plugins-market": True,
                    "playwright-mcp@official": False,
                }
            }
        )
    )
    config_dirs(home)
    check = diagnostics._check_browser_tools(tmp_path)
    assert "my-linter" not in check.detail
    assert "no browser MCP/plugin declared" in check.detail


def test_doctor_finds_the_provider_in_the_args(
    tmp_path: Path, config_dirs: Callable[..., None]
) -> None:
    """The canonical declaration names the provider in `args` and leaves the
    server name to the operator, so a name-only match missed the real shape."""
    home = tmp_path / "claude"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"e2e": {"command": "npx", "args": ["@playwright/mcp@latest"]}}})
    )
    config_dirs(home)
    check = diagnostics._check_browser_tools(tmp_path)
    assert "mcp e2e" in check.detail, check.detail


def test_doctor_honours_a_declined_project_server(
    tmp_path: Path, config_dirs: Callable[..., None]
) -> None:
    """An unapproved project server never starts, and the decision is recorded
    beside the config — so a committed `.mcp.json` entry the operator declined
    must not read as green."""
    home = tmp_path / "claude"
    home.mkdir()
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"playwright": {}}}))
    (home / ".claude.json").write_text(
        json.dumps({"projects": {str(project): {"disabledMcpjsonServers": ["playwright"]}}})
    )
    config_dirs(home)
    assert "mcp playwright" not in diagnostics._check_browser_tools(project).detail

    (home / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {
                    str(project): {
                        "disabledMcpjsonServers": ["playwright"],
                        "enabledMcpjsonServers": ["playwright"],
                    }
                }
            }
        )
    )
    assert "mcp playwright" in diagnostics._check_browser_tools(project).detail, (
        "a name in both lists is read as approved"
    )


def test_doctor_scans_the_hook_sites_once_per_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hook_sites` GRADES every site — `classify_hook_binary` runs a real
    `<that install's aisquare> --version` subprocess per directory, with a 10 s
    timeout and a cache built fresh per call — and `_check_claude_code` already
    calls it. A second scan re-ran every probe: 1 → 2 scans, 3 → 6 subprocesses,
    683 ms → 1246 ms on a four-dir machine, for grading this row never reads,
    on a path the fleet UI re-runs on every project switch and every `r`.
    """
    calls: list[str] = []

    def counting(name: str) -> list[Any]:
        calls.append(name)
        return []

    monkeypatch.setattr(agent_core, "hook_sites", counting)
    diagnostics.doctor(cwd=tmp_path, live=False)
    assert calls.count("claude-code") == 1
    assert len(calls) == len(set(calls)), f"duplicate hook_sites scans in one doctor run: {calls}"


def test_doctor_asks_only_about_this_homes_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hook_sites` deliberately includes directories this home never connected
    (`_claude_dirs_on_disk`, the #84 gap), which answers "does ANY Claude install
    on this box declare a browser tool" — not "will the ui-tester's window find
    one". A playwright MCP in `~/.claude4` turned the row green while the fleet
    spawned its ui-tester on `~/.claude`, where nothing answered.
    """
    home = tmp_path / "home"
    connected = home / ".claude"
    connected.mkdir(parents=True)
    stranger = home / ".claude4"
    stranger.mkdir()
    (stranger / ".claude.json").write_text(json.dumps({"mcpServers": {"playwright": {}}}))
    monkeypatch.setattr(agent_core, "_home", lambda: home)
    monkeypatch.setattr(agent_core, "connected_dirs", lambda name, registry=None: [connected])
    monkeypatch.setattr(agent_core, "_claude_home", lambda config_dir=None: connected)
    check = diagnostics._check_browser_tools(tmp_path)
    assert "playwright" not in check.detail, check.detail


def test_doctor_is_ok_not_amber_when_nothing_is_declared(
    tmp_path: Path, config_dirs: Callable[..., None]
) -> None:
    """Nothing here is a defect — the role runs and degrades honestly — and
    `install.sh` word-splits its amber list, so a row that was amber by design
    on a healthy machine split into phantom checks named `browser` and `tools`,
    exited the installer 2 and failed `tests/install/cell.sh`'s exact-set
    assertion in every matrix cell.
    """
    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"enabledPlugins": {"something-else": True}}))
    config_dirs(home)
    check = diagnostics._check_browser_tools(tmp_path)
    assert check.status is CheckStatus.ok, "advice, not a defect"
    assert check.fix is None, "an ok row carries no fix; the guidance is the detail"
    assert "not browser-verified" in check.detail
    assert "claude mcp add -s user chrome-devtools npx chrome-devtools-mcp" in check.detail, (
        "settings.json is not where Claude Code reads mcpServers"
    )
    assert "settings.json" not in check.detail, "the old fix line was a dead-end loop"


def test_doctor_survives_unreadable_or_malformed_files(
    tmp_path: Path, config_dirs: Callable[..., None]
) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text("{not json")
    (home / ".claude.json").write_text("[]")
    config_dirs(home)
    check = diagnostics._check_browser_tools(tmp_path)
    assert check.status is CheckStatus.ok


def test_doctor_lists_the_row_after_the_actionable_checks(
    runner: CliRunner, work_dir: Path
) -> None:
    result = runner.invoke(app, ["--json", "doctor"])
    assert result.exit_code in (0, 1), result.output
    names = [c["name"] for c in json.loads(result.output)]
    assert "browser tools" in names
    assert names.index("gh") < names.index("browser tools"), (
        "the fleet sidebar shows three not-ok rows; this one must not evict an actionable one"
    )
    assert names.index("fleet") < names.index("browser tools")


# ── register: an upgraded machine is told which launchable role its roster lacks ──


def test_register_names_first_class_roles_missing_from_the_configured_roster(
    runner: CliRunner, work_dir: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "prod"
    config.explainability.roles = ["planner", "coder", "runner"]  # a pre-ui-tester config.toml
    config.explainability.targets = {
        "prod": ExplainabilityTarget(gateway_url="https://gateway.example")
    }
    save_config(config)
    explainability_service.store_api_key("wk-test")
    monkeypatch.setattr(
        ops,
        "register_roster",
        lambda target, names: ops.HttpVerdict(
            ok=True, status=200, detail="HTTP 200", payload={"agents": []}
        ),
    )
    result = runner.invoke(app, ["explainability", "register"])
    assert result.exit_code == 0, result.output
    assert "not in explainability.roles" in result.output
    assert "ui-tester" in result.output
    assert "--role ui-tester" in result.output


@pytest.fixture
def registered_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gateway that accepts any roster, so the tests are about the hint."""
    monkeypatch.setattr(
        ops,
        "register_roster",
        lambda target, names: ops.HttpVerdict(
            ok=True, status=200, detail="HTTP 200", payload={"agents": []}
        ),
    )


def _configured(roles: list[str], target_roles: list[str] | None = None) -> None:
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "prod"
    config.explainability.roles = roles
    config.explainability.targets = {
        "prod": ExplainabilityTarget(gateway_url="https://gateway.example", roles=target_roles)
    }
    save_config(config)
    explainability_service.store_api_key("wk-test")


def test_register_reads_the_targets_roster_and_not_the_top_level(
    runner: CliRunner, work_dir: Path, isolated_home: Path, registered_ok: None
) -> None:
    """The MISS Anmol reproduced: the hint was silent in exactly the
    configuration it was written for.

    `resolve_target` resolves `roles = target.roles if target.roles is not None
    else settings.roles`, so a target listing two roles publishes two identities
    while the top level still lists eight — six launchable roles unregistered,
    which is the 409 `agent_not_registered` backlog, and the note printed
    nothing because `settings.roles` contained them.
    """
    _configured(list(ExplainabilitySettings().roles), target_roles=["planner", "coder"])
    result = runner.invoke(app, ["explainability", "register"])
    assert result.exit_code == 0, result.output
    assert "not in explainability.roles" in result.output
    for role in (ROLE, "tester", "reviewer", "validator", "manager"):
        assert role in result.output, role
    assert "register --target prod --role" in result.output, "the hint names its target"


def test_the_register_hint_keeps_the_selected_target(
    runner: CliRunner, work_dir: Path, isolated_home: Path, registered_ok: None
) -> None:
    """Review of #112, round 2: with staging active, `register --target prod`
    printed a follow-up that dropped `--target prod` — following it registered the
    missing roles in staging and left prod's roster gap unchanged."""
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "stg"
    config.explainability.roles = list(ExplainabilitySettings().roles)
    config.explainability.targets = {
        "stg": ExplainabilityTarget(gateway_url="https://stg.example"),
        "prod": ExplainabilityTarget(
            gateway_url="https://gateway.example", roles=["planner", "coder"]
        ),
    }
    save_config(config)
    explainability_service.store_api_key("wk-test")
    result = runner.invoke(app, ["explainability", "register", "--target", "prod"])
    assert result.exit_code == 0, result.output
    assert "aisquare explainability register --target prod --role" in result.output
    assert "--target stg" not in result.output


def test_the_register_hint_shell_quotes_the_target_name(
    runner: CliRunner, work_dir: Path, isolated_home: Path, registered_ok: None
) -> None:
    """Review of #112, round 3: `register --target "prod west"` is a valid
    selection, and the unquoted hint split the name into two arguments — following
    it exited 2 with an unexpected `west`."""
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "stg"
    config.explainability.roles = list(ExplainabilitySettings().roles)
    config.explainability.targets = {
        "stg": ExplainabilityTarget(gateway_url="https://stg.example"),
        "prod west": ExplainabilityTarget(
            gateway_url="https://west.example", roles=["planner", "coder"]
        ),
    }
    save_config(config)
    explainability_service.store_api_key("wk-test")
    result = runner.invoke(app, ["explainability", "register", "--target", "prod west"])
    assert result.exit_code == 0, result.output
    assert "register --target 'prod west' --role" in result.output
    # and the printed command actually selects that target when followed
    followed = runner.invoke(
        app, ["explainability", "register", "--target", "prod west", "--role", "tester"]
    )
    assert followed.exit_code == 0, followed.output


def test_register_stays_quiet_when_the_target_registers_everything(
    runner: CliRunner, work_dir: Path, isolated_home: Path, registered_ok: None
) -> None:
    """The FALSE ALARM, same cause: a pre-upgrade top level plus a target that
    lists every role registers every identity, and the note nagged anyway and
    handed over `--role` flags the operator does not need."""
    _configured(["planner", "coder", "runner"], target_roles=list(harness.ROLE_PROFILES))
    result = runner.invoke(app, ["explainability", "register"])
    assert result.exit_code == 0, result.output
    assert "not in explainability.roles" not in result.output


def test_register_carries_the_roster_gap_into_the_json_payload(
    runner: CliRunner, work_dir: Path, isolated_home: Path, registered_ok: None
) -> None:
    """The note is suppressed under `--json`, so without a field the gap was
    invisible to automation."""
    _configured(["planner", "coder", "runner"])
    result = runner.invoke(app, ["--json", "explainability", "register"])
    assert result.exit_code == 0, result.output
    assert ROLE in json.loads(result.output)["unregistered_roles"]


def test_the_tui_register_button_names_the_gap_too(
    work_dir: Path, isolated_home: Path, registered_ok: None
) -> None:
    """`register_roster()` is a second complete implementation of this command
    for the TUI — the product's primary path — and it mirrored every other
    branch verbatim without the hint, so the operator saw only
    "✓ registered N identities"."""
    from aisquare.cli.ui.views import explainability as view

    _configured(["planner", "coder", "runner"])
    notice = view.register_roster()
    assert "not in explainability.roles" in notice.message
    assert ROLE in notice.message
