"""``aisquare launch <role>`` — the ergonomic replacement for ``AISQUARE_ROLE=… claude``."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.services.explainability import join_records


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the exec so the agent is never really launched."""
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    return captured


def test_launch_execs_the_agent_with_the_role_in_env(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["binary"] == "/usr/local/bin/claude"
    assert spy["argv"] == ["claude"]
    assert spy["env"]["AISQUARE_ROLE"] == "coder"


def test_launch_forwards_extra_arguments_to_the_agent(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["launch", "runner", "--model", "opus", "-p", "go"])

    assert result.exit_code == 0, result.output
    # The role is consumed; everything after it reaches the agent untouched.
    assert spy["argv"] == ["claude", "--model", "opus", "-p", "go"]
    assert spy["env"]["AISQUARE_ROLE"] == "runner"


def test_launch_activates_the_project(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    # A role launch is the opt-in for the repo — the board must exist afterwards.
    with store_session() as store:
        assert not store.team_active(team_project(work_dir).id)

    assert runner.invoke(app, ["launch", "planner"]).exit_code == 0

    with store_session() as store:
        assert store.team_active(team_project(work_dir).id)


def test_launch_rejects_an_unknown_role(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    # `reviewer` became a real (fleet) role; `codr` is the typo the whitelist exists for.
    result = runner.invoke(app, ["launch", "codr"])

    assert result.exit_code == 1
    assert "unknown role" in result.output
    assert not spy, "nothing should be exec'd for an invalid role"


def test_launch_reports_a_missing_agent_binary(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 1
    assert "not on your PATH" in result.output


def test_launch_honours_a_custom_agent_command(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["launch", "coder", "--command", "claude-next"])

    assert result.exit_code == 0, result.output
    assert spy["argv"] == ["claude-next"]


def test_launch_env_flag_sets_a_variable_for_the_agent(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], tmp_path: Path
) -> None:
    # Parallel installs are reached by setting the variables the shell alias
    # sets, which is why this takes a variable rather than an "account": the
    # CLI has no business knowing what any of these keys mean.
    account = tmp_path / ".claude-account1"
    account.mkdir()

    result = runner.invoke(app, ["launch", "coder", "--env", f"CLAUDE_CONFIG_DIR={account}"])

    assert result.exit_code == 0, result.output
    assert spy["env"]["CLAUDE_CONFIG_DIR"] == str(account)
    assert spy["env"]["AISQUARE_ROLE"] == "coder"


def test_launch_does_not_validate_the_values_it_carries(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], tmp_path: Path
) -> None:
    # Deliberately permissive. Rejecting a non-existent directory would require
    # knowing WHICH keys name directories -- exactly the coupling this design
    # removes. The operator owns the values; we carry them.
    missing = tmp_path / "not-created"

    result = runner.invoke(app, ["launch", "coder", "--env", f"CLAUDE_CONFIG_DIR={missing}"])

    assert result.exit_code == 0, result.output
    assert spy["env"]["CLAUDE_CONFIG_DIR"] == str(missing)


def test_launch_without_a_profile_leaves_the_ambient_config_dir_alone(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/from/the/shell")

    assert runner.invoke(app, ["launch", "coder"]).exit_code == 0
    assert spy["env"]["CLAUDE_CONFIG_DIR"] == "/from/the/shell"


def test_launch_forwards_the_global_output_flags_to_the_agent(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    """Anything after the role belongs to the AGENT — the five global flags too.

    The root group injects --json/--verbose/-v/--quiet/-q/--no-color/--profile
    into every command (issue #24). On an arg-forwarding command that would let
    click parse them OUT of the forwarded argv (--profile even eats its value),
    so the agent silently never sees flags the user typed for it. launch
    declares ignore_unknown_options — injection must respect that and skip it.
    """
    result = runner.invoke(app, ["launch", "coder", "--verbose", "--profile", "p1", "--json", "-q"])

    assert result.exit_code == 0, result.output
    assert spy["argv"] == ["claude", "--verbose", "--profile", "p1", "--json", "-q"]


def test_launch_respects_the_master_off_switch(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_TEAM", "0")

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 1
    assert not spy, "the orchestrator is off — nothing should be launched"


# ── explainability wiring at the env seam ────────────────────────────────────


def _tracing_on(proxy_url: str) -> None:
    from aisquare.core.config import AppConfig, ExplainabilitySettings, save_config

    save_config(AppConfig(explainability=ExplainabilitySettings(enabled=True, proxy_url=proxy_url)))


def test_launch_default_config_adds_no_tracing(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tracing off (the default) must leave the launch byte-identical: no env
    delta, no extra output — zero cost to every existing user."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_BASE_URL" not in spy["env"]
    assert "ANTHROPIC_CUSTOM_HEADERS" not in spy["env"]
    assert "explainability" not in result.output


def test_launch_enabled_wires_the_traced_env(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import explainability as explainability_service
    from aisquare.services.explainability import SessionWiring

    _tracing_on("http://127.0.0.1:9")

    def fake_wire(settings: Any, role: str, **kwargs: Any) -> SessionWiring:
        return SessionWiring(
            traced=True,
            reason=f"traced as aisquare-{role} (pipeline p-1)",
            env={
                "ANTHROPIC_BASE_URL": settings.proxy_url,
                "ANTHROPIC_CUSTOM_HEADERS": f"X-Agent-Name: aisquare-{role}\nX-Pipeline-Id: p-1",
            },
        )

    monkeypatch.setattr(explainability_service, "wire_session", fake_wire)

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    assert "X-Pipeline-Id" in spy["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert "traced as aisquare-coder" in result.output


def test_launch_with_dead_proxy_still_launches(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open through the REAL probe: tracing enabled, nothing listening —
    the launch must proceed untraced instead of breaking or misrouting."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    _tracing_on("http://127.0.0.1:9")  # discard port — connection refused

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["binary"].endswith("/claude"), "the launch must happen regardless"
    assert "ANTHROPIC_BASE_URL" not in spy["env"]
    assert "untraced" in result.output


def test_launch_survives_a_base_url_the_agent_could_not_parse(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch-blocking case, through the real launch path.

    A corrupt ``proxy_url`` used to reach the agent's environment untouched,
    and the agent — which parses that variable before it can report anything —
    died with "API Error: Invalid URL" and exit 1. Tracing cost a LAUNCH,
    which the doctrine forbids outright. The value now has the same fail-open
    the ``/health`` probe has always had: refuse it, say why, launch untraced.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    _tracing_on("$http://127.0.0.1:9190")  # the exact shape dash produced

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["argv"] == ["claude"], "the launch must be the one you would have got anyway"
    assert "ANTHROPIC_BASE_URL" not in spy["env"], "a value we refused may not reach the agent"
    assert "ANTHROPIC_CUSTOM_HEADERS" not in spy["env"]
    assert "proxy_url" in result.output and "untraced" in result.output


def test_spawn_exec_survives_a_base_url_the_agent_could_not_parse(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard lives in the wiring, not in one command — so the other seam
    that sets the variable inherits it without needing its own check."""
    import os

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    _tracing_on("$http://127.0.0.1:9190")
    seen: dict[str, Any] = {}

    monkeypatch.setattr("aisquare.cli.team.shutil.which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(
        "aisquare.cli.team.os.execvpe",
        lambda file, argv, env: seen.update(argv=argv, env=env),
    )

    result = runner.invoke(app, ["team", "spawn", "coder", "--exec", "--no-probe"])

    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_BASE_URL" not in seen["env"]
    assert os.environ.get("ANTHROPIC_BASE_URL") is None
    assert "proxy_url" in result.output


def _join_seen_by_the_agent(
    monkeypatch: pytest.MonkeyPatch, agent_env: dict[str, str], session_id: str
) -> list[dict[str, object]]:
    """Run the join hook the way the launched agent would, and read the log.

    The launcher hands the agent an environment and is then replaced by it, so
    the join is closed one process later — by the hook running INSIDE the
    agent, which is the only place the board session id exists. Reproducing
    that here means adopting the env the launcher built.
    """
    from aisquare.services import explainability as explainability_service
    from aisquare.services import hooks

    for name in (
        explainability_service.PIPELINE_ID_ENV_VAR,
        explainability_service.TRACE_AGENT_NAME_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
        if name in agent_env:
            monkeypatch.setenv(name, agent_env[name])
    assert hooks.record_trace_join(session_id) is None
    return join_records()


def test_a_child_of_a_traced_session_gets_its_OWN_identity(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE case for the morning: agents spawning agents.

    A traced session's environment carries our wiring, so a launch from inside
    one used to hit the "not overriding your routing" guard and start the child
    UNTRACED — every agent below the first silently dropping off the trace. And
    simply proceeding would be worse: the child would inherit the PARENT's
    X-Pipeline-Id and merge into its Run.

    So the parent's identity is disowned first and the child wires a fresh one.
    """
    from tests.proxy_stub import healthy_proxy

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9190")
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS", "X-Agent-Name: aisquare-planner\nX-Pipeline-Id: parent-run"
    )
    monkeypatch.setenv("AISQUARE_PIPELINE_ID", "parent-run")  # the marker: this trace is OURS

    with healthy_proxy() as proxy_url:
        _tracing_on(proxy_url)
        result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    headers = spy["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert "X-Pipeline-Id: parent-run" not in headers, "the child must not join the parent's Run"
    assert "X-Agent-Name: aisquare-coder" in headers, "and it wears its own role"
    assert spy["env"]["ANTHROPIC_BASE_URL"] == proxy_url
    assert spy["env"]["AISQUARE_PIPELINE_ID"] != "parent-run"
    assert "--session-id" in spy["argv"], "a traced child is pinned like any other"


def test_a_real_gateway_of_the_operators_is_still_never_overridden(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same variables, without our marker beside them, belong to the human.

    This is the line the disowning must not cross: we clear what WE set, and a
    gateway the operator exported themselves still makes us stand down.
    """
    from tests.proxy_stub import healthy_proxy

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://my-own-gateway.example")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("AISQUARE_PIPELINE_ID", raising=False)  # no marker — not ours

    with healthy_proxy() as proxy_url:
        _tracing_on(proxy_url)
        result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["env"]["ANTHROPIC_BASE_URL"] == "https://my-own-gateway.example"
    assert "already set" in result.output and "untraced" in result.output


def test_an_untraceable_child_does_not_keep_the_parents_identity(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disowning has to survive the wiring FAILING, which is the subtle half.

    If the child's own wiring falls open (dead proxy) after we cleared the
    parent's identity, it must launch clean-untraced — not fall back to
    wearing the parent's headers, which is exactly the silent merge.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9190")
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS", "X-Agent-Name: aisquare-planner\nX-Pipeline-Id: parent-run"
    )
    monkeypatch.setenv("AISQUARE_PIPELINE_ID", "parent-run")
    _tracing_on("http://127.0.0.1:9")  # discard port — the child's wiring fails open

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_BASE_URL" not in spy["env"], "an untraced child wears nobody's identity"
    assert "ANTHROPIC_CUSTOM_HEADERS" not in spy["env"]
    assert "AISQUARE_PIPELINE_ID" not in spy["env"]
    assert "untraced" in result.output


def test_a_role_bound_to_a_wrapper_traces_and_still_joins(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the flag alone can never cover, and the reason the join lives
    in the environment.

    Since #57 a role can be bound to any executable. Handing that executable
    ``--session-id`` would kill the launch, so nothing is added to its argv —
    and it still joins, because the pipeline id travels in the ENVIRONMENT and
    the hook that closes the join runs inside the agent, not out here.
    """
    from aisquare.core.config import (
        AppConfig,
        ExplainabilitySettings,
        RoleLaunchProfile,
        TeamSettings,
        save_config,
    )
    from tests.proxy_stub import healthy_proxy

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    with healthy_proxy() as proxy_url:
        save_config(
            AppConfig(
                explainability=ExplainabilitySettings(enabled=True, proxy_url=proxy_url),
                team=TeamSettings(profiles={"coder": RoleLaunchProfile(bin="my-wrapper")}),
            )
        )
        result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["argv"] == ["my-wrapper"], "no flag may reach a binary we did not resolve"
    assert "X-Pipeline-Id" in spy["env"]["ANTHROPIC_CUSTOM_HEADERS"], "still traced"

    keyed_by = spy["env"]["AISQUARE_PIPELINE_ID"]
    (record,) = _join_seen_by_the_agent(monkeypatch, spy["env"], "board-session-42")
    assert record["session_id"] == "board-session-42"
    assert record["pipeline_id"] == keyed_by
    assert record["agent_name"] == "aisquare-coder"


def test_launch_starts_the_agent_on_the_id_it_traces_under(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE correlation spine, through the real wiring: the id the agent is
    STARTED on is the id in ``X-Pipeline-Id``.

    The board keys a session by the id the agent reports to the SessionStart
    hook, so those two being equal is the only thing that lets a board row and
    a gateway Run be joined. Asserted end to end (real probe, real wiring)
    because it is a property of the chain, not of any one function in it.
    """
    from aisquare.services.explainability import join_records
    from tests.proxy_stub import healthy_proxy

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    with healthy_proxy() as proxy_url:
        _tracing_on(proxy_url)
        result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    argv = spy["argv"]
    assert "--session-id" in argv, "a traced launch must name the session it traces"
    started_on = argv[argv.index("--session-id") + 1]
    uuid.UUID(started_on)  # claude accepts nothing else
    assert f"X-Pipeline-Id: {started_on}" in spy["env"]["ANTHROPIC_CUSTOM_HEADERS"]

    # The marker travels too, so the agent's own hook can close the join —
    # the strict path and the general path agree on the same id.
    assert spy["env"]["AISQUARE_PIPELINE_ID"] == started_on
    assert spy["env"]["AISQUARE_TRACE_AGENT_NAME"] == "aisquare-coder"

    # The launcher writes nothing; the hook inside that agent does.
    assert join_records() == [], "the launcher records nothing; the hook does"
    (record,) = _join_seen_by_the_agent(monkeypatch, spy["env"], started_on)
    assert record["session_id"] == started_on
    assert record["pipeline_id"] == started_on
    assert record["agent_name"] == "aisquare-coder"


def test_two_launches_are_started_on_different_ids(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two agents on one session id merge into a single board row AND a single
    Run — the failure both halves of this design exist to prevent."""
    from tests.proxy_stub import healthy_proxy

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    ids = []
    with healthy_proxy() as proxy_url:
        _tracing_on(proxy_url)
        for _ in range(2):
            assert runner.invoke(app, ["launch", "coder"]).exit_code == 0
            argv = spy["argv"]
            ids.append(argv[argv.index("--session-id") + 1])

    assert ids[0] != ids[1]


def test_launch_does_not_pin_an_id_it_cannot_own(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--continue resumes a session chosen at run time, and a custom agent may
    not speak the flag at all. Neither is pinned — and BOTH still join, because
    the join rides in the environment and is closed by the agent's own hook."""
    from tests.proxy_stub import healthy_proxy

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    with healthy_proxy() as proxy_url:
        _tracing_on(proxy_url)

        assert runner.invoke(app, ["launch", "coder", "--continue"]).exit_code == 0
        assert spy["argv"] == ["claude", "--continue"]
        assert "X-Pipeline-Id" in spy["env"]["ANTHROPIC_CUSTOM_HEADERS"]
        (record,) = _join_seen_by_the_agent(monkeypatch, spy["env"], "the-resumed-session")
        assert record["pipeline_id"] == spy["env"]["AISQUARE_PIPELINE_ID"]

        result = runner.invoke(app, ["launch", "coder", "--command", "aider"])
        assert result.exit_code == 0, result.output
        assert spy["argv"] == ["aider"], "no flag reaches a binary we did not resolve"
        assert "X-Pipeline-Id" in spy["env"]["ANTHROPIC_CUSTOM_HEADERS"], "still traced"


def test_untraced_launch_is_never_handed_a_session_id(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open means the fallback is the launch you would have got anyway —
    argv included. An id pinned onto an untraced launch buys no correlation
    and spends real risk."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    _tracing_on("http://127.0.0.1:9")  # discard port — connection refused

    result = runner.invoke(app, ["launch", "coder", "--model", "opus"])

    assert result.exit_code == 0, result.output
    assert spy["argv"] == ["claude", "--model", "opus"]
    assert "untraced" in result.output


def test_a_session_starts_normally_when_the_join_cannot_be_written(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join log is a convenience; the session it annotates is the product.

    Asserted at the hook seam, because that is where the write happens now and
    a raising observer THERE would break every session start, not just a
    launch. The failure is reported to the caller and swallowed by it.
    """
    from aisquare.core import paths
    from aisquare.services import hooks

    monkeypatch.setenv("AISQUARE_PIPELINE_ID", "run-9")
    monkeypatch.setenv("AISQUARE_TRACE_AGENT_NAME", "aisquare-coder")
    paths.explainability_dir().parent.mkdir(parents=True, exist_ok=True)
    paths.explainability_dir().write_text("in the way", encoding="utf-8")

    reason = hooks.record_trace_join("board-9")
    assert reason is not None and "join record not written" in reason

    # And the context the hook exists to emit is produced regardless.
    hooks.session_start_context(work_dir, session_id="board-9", source="startup")


def test_an_untraced_session_writes_no_join_at_all(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ordinary session takes this path: one lookup, nothing written, no
    evidence left of a Run that does not exist."""
    from aisquare.core import paths
    from aisquare.services import hooks

    monkeypatch.delenv("AISQUARE_PIPELINE_ID", raising=False)

    assert hooks.record_trace_join("board-10") is None
    assert not paths.explainability_joins_path().exists()


def test_launch_reuses_the_forwarded_session_id(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --session-id the caller passes to the agent becomes the pipeline id —
    read from the forwarded args, never injected into them."""
    from aisquare.services import explainability as explainability_service
    from aisquare.services.explainability import SessionWiring

    _tracing_on("http://127.0.0.1:9")
    seen: dict[str, Any] = {}

    def fake_wire(settings: Any, role: str, **kwargs: Any) -> SessionWiring:
        seen.update(kwargs)
        return SessionWiring(traced=False, reason="stubbed")

    monkeypatch.setattr(explainability_service, "wire_session", fake_wire)

    runner.invoke(app, ["launch", "coder", "--session-id", "sess-7"])
    assert seen["session_id"] == "sess-7"

    runner.invoke(app, ["launch", "coder", "--session-id=sess-8"])
    assert seen["session_id"] == "sess-8"


def test_launch_survives_a_corrupt_config(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    """launch never read the config before tracing arrived; a corrupt
    config.toml must not become a new way for a launch to die (the zero-
    breakage bar). Worst allowed outcome: untraced, with a reason."""
    from aisquare.core import paths

    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text("explainability = [unclosed", encoding="utf-8")

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["env"]["AISQUARE_ROLE"] == "coder", "the launch must happen regardless"
    assert "config unreadable" in result.output


def test_a_role_bound_to_a_session_id_is_not_given_a_second_one(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session planning has to read the EFFECTIVE argv, not just what was typed.

    `argv` is `[binary, *profile.args, *ctx.args, *pinned_id]`, so a role bound
    with `--session-id` via `team bind --arg` carries it without it ever
    appearing in `ctx.args`. Planning on `ctx.args` alone read this launch as
    fresh and appended a SECOND `--session-id`, silently overriding the id the
    operator bound — and the board/Run join then keys on the minted id rather
    than theirs. `team spawn` already passed its profile args; this path did not.
    """
    from aisquare.core.config import (
        AppConfig,
        ExplainabilitySettings,
        RoleLaunchProfile,
        TeamSettings,
        save_config,
    )
    from tests.proxy_stub import healthy_proxy

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    with healthy_proxy() as proxy_url:
        save_config(
            AppConfig(
                explainability=ExplainabilitySettings(enabled=True, proxy_url=proxy_url),
                team=TeamSettings(
                    profiles={"coder": RoleLaunchProfile(args=["--session-id", "bound-id-1234"])}
                ),
            )
        )
        result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["argv"].count("--session-id") == 1, (
        f"two --session-id flags reached the agent: {spy['argv']}"
    )
    assert spy["argv"] == ["claude", "--session-id", "bound-id-1234"], spy["argv"]
    assert "X-Pipeline-Id" in spy["env"]["ANTHROPIC_CUSTOM_HEADERS"], "still traced"


@pytest.mark.parametrize("bound", [["--continue"], ["--resume"]])
def test_a_role_bound_to_a_resume_is_not_pinned(
    bound: list[str],
    runner: CliRunner,
    work_dir: Path,
    spy: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal to pin has to survive the flag arriving from a binding.

    `--continue` and a bare `--resume` name a session chosen at run time, so
    planning deliberately injects nothing: guessing an id merges two agents onto
    one board row and one Run, which is worse than no join. Read off `ctx.args`
    only, that refusal never fired for a bound flag and the launcher appended a
    `--session-id` to a resume.
    """
    from aisquare.core.config import (
        AppConfig,
        ExplainabilitySettings,
        RoleLaunchProfile,
        TeamSettings,
        save_config,
    )
    from tests.proxy_stub import healthy_proxy

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    with healthy_proxy() as proxy_url:
        save_config(
            AppConfig(
                explainability=ExplainabilitySettings(enabled=True, proxy_url=proxy_url),
                team=TeamSettings(profiles={"coder": RoleLaunchProfile(args=list(bound))}),
            )
        )
        result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert "--session-id" not in spy["argv"], (
        f"pinned an id onto {bound[0]}, which names a session that does not exist "
        f"yet: {spy['argv']}"
    )
    assert spy["argv"] == ["claude", *bound], spy["argv"]


def test_every_role_that_can_take_a_seat_has_a_harness_profile() -> None:
    """The exclusion that makes ``services.team.base_role`` complete.

    ``_SEAT`` is built from ``launch.ROLES``; ``base_role`` strips the digits and
    keeps the result only when ``harness.ROLE_PROFILES`` has it. So a role added to
    ``ROLES`` without a profile would be a seat ``launch`` accepts and
    ``base_role`` refuses — a session with no work cycle and no ladder, which is
    the defect this pins the fix against. Asserted of the two modules rather than
    of a literal list, so neither can drift alone.

    One direction on purpose: a profiled role that ``launch`` does not whitelist is
    reachable through ``team bind`` and is not this hazard.
    """
    from aisquare.core import harness

    # An empty set on either side would satisfy the claim for free, which is the
    # shape where blindness and success look identical — so read both first.
    assert "coder" in launch_cli.ROLES and "coder" in harness.ROLE_PROFILES
    assert set(launch_cli.ROLES) - {"nosuchrole"} == set(launch_cli.ROLES)
    assert {"nosuchrole", *launch_cli.ROLES} - set(harness.ROLE_PROFILES) == {"nosuchrole"}, (
        "the positive control: an unprofiled role IS reported by this comparison"
    )

    missing = set(launch_cli.ROLES) - set(harness.ROLE_PROFILES)
    assert not missing, f"{sorted(missing)} accept a numbered seat but have no harness profile"


def test_a_numbered_seat_exports_the_seat_and_resolves_to_its_base_role() -> None:
    """Both halves of the seat contract, from the one place that defines its shape.

    The regex must accept the seat (the board keeps ``coder1`` as an identity) and
    ``base_role`` must map it home. The negative controls are the shapes neither
    may claim: a typo, and a bare number.
    """
    from aisquare.services import team as team_service

    assert launch_cli._SEAT.match("coder1") is not None
    assert team_service.base_role("coder1") == "coder"
    assert launch_cli._SEAT.match("codr1") is None
    assert team_service.base_role("codr1") == "codr1"
    assert launch_cli._SEAT.match("1") is None
