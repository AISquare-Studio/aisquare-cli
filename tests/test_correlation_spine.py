"""One minted id has to arrive in four places, and nothing re-checked that.

The north star's third clause is that board rows join to gateway Runs on a
shared key. The handoff states it as proven — "One id in four places | observed
in all four simultaneously" — which is true, and was established by a human
watching it happen once, before roughly a night of changes to the launcher, the
store and the hooks.

The pieces were tested; the SPINE was not. ``test_explainability.py`` asserts
``X-Pipeline-Id: {wiring.pipeline_id}`` reaches the headers — one hop, and real.
``record_join`` is exercised with ``session_id="board-1", pipeline_id="run-1"``
— literals typed into the test, which pin its dedup and say nothing about
whether the pipeline hands it what it minted. **A test whose inputs are
hand-written cannot detect the two ends drifting apart**, which is exactly the
failure this file exists for.

Walked by hand at 984a3b9 before writing any of this, and the spine held: mint,
header, argv expansion, board row and ``joins.jsonl`` all carried
``31f047ef-…``. So nothing here is a fix. It converts one manual observation
into something that fails the gate on the day it stops being true.

WHY THAT FAILURE WOULD OTHERWISE BE INVISIBLE. If the id stops flowing, every
surface still looks healthy: the launcher exits 0, the proxy still sends a
header, the board still gets a row, ``status`` is green. The only place the
break shows is a gateway Run that no board row points at — in the studio, which
by this repo's own standing residual nobody has read. There is no local symptom
to notice, so a test is the only thing that can notice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.config import AppConfig, ExplainabilityTarget, save_config
from aisquare.services.explainability import (
    PIPELINE_ID_ENV_VAR,
    TRACE_AGENT_NAME_ENV_VAR,
    ProxyProbe,
    SessionWiring,
    trace_marker,
    wire_session,
)
from aisquare.services.explainability_ops import effective_settings

_KEY_VAR = "SPINE_KEY_VAR"


def _healthy(_url: str) -> ProxyProbe:
    """Injected rather than bound: `wire_session` resolves `prober` at call time.

    Also the reason this file binds no socket. Port 9090 on this machine holds a
    long-lived proxy that is not ours, and a test that needs a real listener is
    a test that will one day be pointed at it.
    """
    return ProxyProbe(True, "proxy healthy")


def _configured() -> None:
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "stg"
    config.explainability.targets["stg"] = ExplainabilityTarget(
        gateway_url="https://gateway.invalid",
        api_key_env=_KEY_VAR,
        proxy_url="http://127.0.0.1:9099",
    )
    save_config(config)


def _mint(role: str = "coder") -> SessionWiring:
    from aisquare.core.config import load_config

    _configured()
    wiring = wire_session(
        effective_settings(load_config().explainability, None),
        role,
        base_env={},
        prober=_healthy,
    )
    assert wiring.traced, f"fixture premise broken: {wiring.reason}"
    return wiring


def test_one_minted_id_reaches_all_four_places(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole claim, end to end, with NO literal id anywhere in the test.

    Every value compared here descends from a single ``wire_session`` call. An
    implementation that put a different id in any one place fails, and there is
    no hand-written constant for it to accidentally agree with.
    """
    wiring = _mint()
    minted = wiring.pipeline_id
    assert minted, "nothing was minted"

    # 1. the header the proxy sends
    headers = wiring.env["ANTHROPIC_CUSTOM_HEADERS"]
    assert f"X-Pipeline-Id: {minted}" in headers

    # 2. the marker the shell exports, which is what the next command reads
    marker = trace_marker(wiring)
    assert marker[PIPELINE_ID_ENV_VAR] == minted

    # 3. the board row, created by the SessionStart hook the way Claude Code
    #    invokes it — the agent was started ON the minted id.
    monkeypatch.setenv(PIPELINE_ID_ENV_VAR, minted)
    monkeypatch.setenv(TRACE_AGENT_NAME_ENV_VAR, marker[TRACE_AGENT_NAME_ENV_VAR])
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    # The hook attaches a session to a REGISTERED project. My first run of this
    # used an unregistered tmp dir, got an empty board, and that reads exactly
    # like "the id did not arrive" — so the premise is asserted, not assumed.
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    projects = runner.invoke(app, ["--json", "project", "list"], catch_exceptions=False)
    assert json.loads(projects.stdout), "fixture premise: a project must be registered"

    payload = json.dumps({"session_id": minted, "cwd": str(tmp_path), "source": "startup"})
    started = runner.invoke(app, ["hook", "session-start"], input=payload)
    assert started.exit_code == 0, started.output

    listed = runner.invoke(app, ["--json", "team", "status"], catch_exceptions=False)
    sessions = json.loads(listed.stdout)["sessions"]
    assert [s["id"] for s in sessions] == [minted], "the board row is keyed on something else"

    # 4. the join log, which is what ties the two together for anyone reading later
    joins = paths.explainability_joins_path()
    assert joins.exists(), "no join was recorded for a traced session"
    records = [json.loads(line) for line in joins.read_text().splitlines() if line.strip()]
    assert records, "join log is empty"
    assert records[-1]["session_id"] == minted
    assert records[-1]["pipeline_id"] == minted


def test_two_sessions_do_not_share_an_id(runner: CliRunner) -> None:
    """The control, and the reason the test above cannot pass vacuously.

    An implementation that returned a constant — or reused the last id — would
    satisfy every equality above while merging two agents into one Run, which
    is the specific accident the mint exists to prevent.
    """
    first = _mint("coder").pipeline_id
    second = _mint("planner").pipeline_id

    assert first and second
    assert first != second


def test_the_agent_name_follows_the_role(runner: CliRunner) -> None:
    """Per-role identity: the other half of "traced under its own identity"."""
    assert trace_marker(_mint("coder"))[TRACE_AGENT_NAME_ENV_VAR] == "aisquare-coder"
    assert trace_marker(_mint("planner"))[TRACE_AGENT_NAME_ENV_VAR] == "aisquare-planner"


def test_the_spawn_template_passes_the_flag_the_parser_looks_for() -> None:
    """The hop that breaks silently, one level below where I first looked for it.

    The printed spawn line does not carry a literal id — it carries a shell
    expansion. The VARIABLE half is already safe: the template interpolates
    ``PIPELINE_ID_ENV_VAR``, so a rename propagates. The FLAG half is not: the
    template writes ``--session-id`` as a literal while the argv parser matches
    ``_SESSION_ID_FLAG``. If Claude Code ever renames that flag and only the
    constant is updated, the launcher keeps emitting the old spelling, the agent
    rejects or ignores it, and the board row and the Run stop sharing a key —
    with no error anywhere.

    Both sides are read from the source rather than retyped, because a hardcoded
    ``"--session-id"`` in this assertion would reproduce the very drift it is
    supposed to catch.
    """
    from aisquare.cli.team import _SESSION_ID_SUBSTITUTION
    from aisquare.services.explainability import _SESSION_ID_FLAG

    assert _SESSION_ID_FLAG in _SESSION_ID_SUBSTITUTION, (
        f"the spawn template does not pass {_SESSION_ID_FLAG!r}: {_SESSION_ID_SUBSTITUTION!r}"
    )
    assert f"${PIPELINE_ID_ENV_VAR}" in _SESSION_ID_SUBSTITUTION


def test_an_untraced_session_passes_no_session_id_at_all() -> None:
    """Fail-open, stated as a property of the template rather than of a run.

    The substitution collapses to NOTHING when nothing was minted. That matters
    more than it looks: an empty ``--session-id ''`` would be a broken launch,
    where no flag at all is a normal one. This is the doctrine's "may cost a
    trace, never a launch" written into a shell expansion.
    """
    from aisquare.cli.team import _SESSION_ID_SUBSTITUTION

    assert _SESSION_ID_SUBSTITUTION.startswith(f"${{{PIPELINE_ID_ENV_VAR}:+")
    assert _SESSION_ID_SUBSTITUTION.endswith("}")
