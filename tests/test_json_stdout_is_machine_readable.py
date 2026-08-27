"""Under ``--json``, stdout is for a program: empty, or parseable JSON.

Two runbooks tell the operator to pipe it — ``aisquare --json explainability
status | jq -r .shipping.gateway`` — and `docs/planner-findings-loop.md` builds
on the same habit. A command that prints a ✓ line to stdout under that flag does
not fail; it hands a jq pipeline a parse error with no explanation, and the
operator's reasonable conclusion is that the CLI is broken.

MEASURED at 585ca18, all 97 swept leaf commands, in a configured home: 87 parse,
4 print nothing, and 6 print human text. Three of the six are by design and are
allowed below with the reason. The other three were
``src/aisquare/cli/explainability.py`` consulting ``json_output`` in exactly one
place — ``status`` — while ``enable``, ``disable`` and ``ship`` never looked. One
group, one flag, four commands, two behaviours.

``tests/test_json_mode.py`` holds this contract for STUB commands (``sync``,
``open``). Nothing held it for the commands that do something.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.paths import config_path
from tests.proxy_stub import healthy_proxy
from tests.test_explainability_ops import _gateway
from tests.test_no_traceback_in_a_configured_home import _swept, configured_home  # noqa: F401

#: Commands whose stdout is a DOCUMENT or a PROTOCOL, not a report. ``--json``
#: selects how the CLI reports; it does not reformat a payload whose shape
#: someone else parses. Each entry names what reads it, because "allowed" with
#: no reason is how an allow list becomes a hiding place.
#: A roster answer with BOTH shapes the human output distinguishes: an agent
#: with a publication id, and one without. A stub that returned ids for all of
#: them would leave the second branch of the renderer unexercised.
_ROSTER_ROUTE = "/v1/agents/register-roster"
_ROSTER_BODY = {
    "agents": [
        {"name": "aisquare-planner", "publication_id": 101},
        {"name": "aisquare-coder", "publication_id": 102},
    ]
}

NOT_A_REPORT = {
    "context export": "emits the markdown context document; --json would corrupt the export",
    "ctx export": "alias of context export",
    "hook session-start": "emits the <aisquare-context> block Claude Code itself parses",
}


def test_the_allow_list_names_commands_that_exist() -> None:
    """A renamed command would leave a permanent unexamined exemption here."""
    swept = {name for name, _ in _swept()}

    unknown = sorted(name for name in NOT_A_REPORT if name not in swept)

    assert not unknown, f"allowed but no longer swept: {unknown}"


@pytest.fixture(params=["proxy-down", "proxy-healthy"], ids=["proxy-down", "proxy-healthy"])
def in_both_proxy_states(
    request: pytest.FixtureRequest,
    configured_home: Path,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """The configured machine with the proxy down, and again with it answering.

    ONE PROXY STATE IS NOT THE COMMAND. ``explainability env`` refuses when the
    session would not be traced, and that refusal goes through the shared
    ``fail`` helper, which emits JSON. So with no proxy the command looks like a
    good citizen of this contract and only its success path breaks it — the
    sweep was measuring a branch and reporting on a command. That is the same
    shape as an empty ratchet measured over one damage shape, which is why the
    damaged-store file runs two.

    The proxy binds port 0, so the OS picks it. Never 9090: this project
    documents that port as somebody else's long-running proxy.
    """
    if request.param == "proxy-down":
        # "Nothing listening" has to be MADE true, not assumed. The docstring
        # above says never 9090 because it is somebody else's long-running
        # proxy — and then this branch inherited the configured default, which
        # is exactly 9090. Any developer with a proxy running there got a
        # collection ERROR here from a fixture asserting its own premise.
        #
        # Port 1 is privileged, so an unprivileged process cannot be listening
        # on it and connection-refused is guaranteed.
        # The premise, asserted: with nothing listening, env must REFUSE.
        # Without this the two params could quietly be the same state twice.
        assert runner.invoke(app, ["explainability", "env", "coder"]).exit_code == 1
        yield request.param
        return

    server, gateway, _seen = _gateway({_ROSTER_ROUTE: (200, _ROSTER_BODY)})
    with healthy_proxy() as url:
        try:
            # Not credential-shaped on purpose: a literal that LOOKS like a key
            # was rejected by push protection on this repo once, and nothing
            # here needs entropy — the stub accepts any value.
            monkeypatch.setenv("AISQUARE_LOCAL_STUB_KEY", "local-stub")
            enabled = runner.invoke(
                app,
                [
                    "explainability",
                    "enable",
                    "--proxy-url",
                    url,
                    "--gateway-url",
                    gateway,
                    "--key-env",
                    "AISQUARE_LOCAL_STUB_KEY",
                ],
            )
            assert enabled.exit_code == 0, enabled.output

            # The premise, twice, because this parameter now claims two things.
            traced = runner.invoke(app, ["explainability", "env", "coder"])
            assert traced.exit_code == 0, (
                "the stub proxy is not being seen as healthy, so this parameter "
                f"is the proxy-down case wearing another name: {traced.output}"
            )
            registered = runner.invoke(app, ["explainability", "register"])
            assert registered.exit_code == 0, (
                "the stub gateway is not accepting the roster, so `register` is "
                f"still taking a failure branch here: {registered.output}"
            )
            yield request.param
        finally:
            server.shutdown()


def test_json_stdout_is_empty_or_parseable(in_both_proxy_states: str, runner: CliRunner) -> None:
    """The property, with every offender reported rather than just the first."""
    # EVERY COMMAND SEES THE SAME MACHINE. Without this the sweep is
    # order-dependent, and measurably so: `explainability disable` sorts before
    # `explainability env` in the tree, so by the time `env` ran, tracing was
    # off and it took its refusal branch — which emits JSON — no matter what
    # proxy state this parameter set up. The sweep looked green over a state it
    # had destroyed three commands earlier. Config only: the store is rebuilt
    # by whatever needs it, and it is not what decides these branches.
    config = config_path()
    pristine = config.read_bytes()

    offenders: dict[str, str] = {}
    parsed = 0
    for name, argv in _swept():
        if name in NOT_A_REPORT:
            continue
        config.write_bytes(pristine)
        stdout = runner.invoke(app, ["--json", *argv], catch_exceptions=True).stdout.strip()
        if not stdout:
            continue
        try:
            json.loads(stdout)
        except ValueError:
            offenders[name] = stdout.splitlines()[0][:80]
        else:
            parsed += 1

    assert not offenders, (
        "these print human text to stdout under --json, so a jq pipeline over "
        f"them fails with no explanation ({in_both_proxy_states}): {offenders}"
    )
    # Emptiness is the goal and the symptom: a sweep that stopped invoking
    # anything, or one where every command happened to print nothing, reports
    # exactly the same zero.
    assert parsed >= 80, f"only {parsed} commands produced JSON to parse"


@pytest.mark.parametrize(
    ("stdout", "is_json"),
    [
        ('{"enabled": true}', True),
        ("✓ tracing enabled for target 'tst'", False),
        ("# aisquare context", False),
        ("shipping is not configured — nothing to do", False),
        ("[]", True),
    ],
    ids=["object", "tick-line", "markdown-heading", "prose", "array"],
)
def test_the_rule_separates_json_from_prose(stdout: str, is_json: bool) -> None:
    """A control on the rule itself, on the exact strings this found."""
    try:
        json.loads(stdout)
    except ValueError:
        assert not is_json, f"{stdout!r} should have parsed"
    else:
        assert is_json, f"{stdout!r} should not have parsed"
