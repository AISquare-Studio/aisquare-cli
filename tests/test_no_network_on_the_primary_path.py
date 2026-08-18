"""Tracing may cost a trace. It may not cost a millisecond on the primary path.

That clause of the fail-open doctrine is the one with no test behind it, while
the handoff's doctrine section says each of them has one. The primary path in
the most literal sense is the hook: it runs on every prompt of every session.

MEASURED AT 9dce497 — measurements at a commit, recorded rather than asserted,
because a wall-clock bound in CI is flaky by construction and a muted test is
worse than none.

WHAT AN OPERATOR PAYS per invocation, subprocess, medians of 9 runs:

    aisquare hook user-prompt-submit
      tracing off                   353 ms   (min 339, max 362)
      tracing on, proxy UNREACHABLE 363 ms   (min 340, max 415)
      tracing on + shipping on      360 ms   (min 341, max 372)

Decomposed so nobody reads 353 ms as a tracing cost: ``python3 -c pass`` 49 ms,
``aisquare --version`` 326 ms, hook 353 ms. The CLI's import dominates. (An
earlier revision also derived "the hook's own work is ~27 ms" from that
subtraction; it is dropped, because the 36 ms floor cited below is larger than
the 27 ms difference it came from.)

THOSE OVERLAPPING RANGES DO NOT BY THEMSELVES RULE ANYTHING OUT. The first
version of this docstring said only "the difference is noise, and tracing adds
~0", which lets a reader conclude more than the data supports: it never says
what effect size the measurement could have DETECTED. @8dd460fb re-measured this
box at 25 samples and found the BASE measurement alone spreads 36 ms — three of
us run gates here — so against nine samples a genuine 15 ms cost would have been
invisible and would have read exactly like zero.

SO THE BOUND IS STATED, WITH A CONTROL. In-process, which drops process startup
out of the sample, 60 samples a side:

      tracing OFF              median 87.70 ms   p10 77.09  p90 102.36
      tracing ON + shipping    median 85.78 ms   p10 75.05  p90 108.92
      ON + a deliberate 5 ms   median 89.62 ms
      tracing delta            -1.93 ms   (negative: what noise looks like)
      injected-5 ms delta      +3.84 ms   <- the control

A real five milliseconds moves the median by about four, so this harness
resolves 5 ms even with that spread. The supportable claim is NO EFFECT THE SIZE
OF 5 MS — not "tracing adds ~0". A configured-but-dead proxy costs the hook
nothing detectable at that resolution.

WHAT IS ASSERTED HERE IS THE MECHANISM THOSE NUMBERS MEASURED: with tracing
fully configured, the hook path opens no socket. That is deterministic where the
timing is not. If someone later puts a probe on this path, the timing might
drift only on a loaded machine and pass; the socket assertion fails at once and
names the hook.

The control is not optional. A tripwire that never fires is indistinguishable
from a path that never reaches the network, and this file would then assert
nothing at all — the vacuous shape this shift has found in four separate
instruments.
"""

from __future__ import annotations

import json
import socket

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.config import AppConfig, ExplainabilityTarget, save_config

_HOOKS = ["session-start", "user-prompt-submit", "stop", "session-end", "notification"]
_KEY_VAR = "PRIMARY_PATH_KEY_VAR"


class _SocketOpened(AssertionError):
    """Raised by the tripwire, distinct from any assertion the tests make."""


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``socket.socket`` so any connection attempt is RECORDED.

    Recorded, not merely raised, and that distinction is the whole test. My
    first version asserted on ``result.exception`` — and the bite check found
    it PASSED with a real proxy probe added to the hook path, because the
    hook's ``except Exception`` swallows everything, including the tripwire.
    The exception never reaches the caller, so an assertion on it can never
    fail. A list the fixture owns survives the swallow.

    Replaces rather than listens: this file binds nothing. Port 9090 on this
    machine holds a proxy that is not ours, and a test that needs a real
    listener is a test that will one day be pointed at it.
    """
    attempts: list[str] = []

    class Tripwire(socket.socket):
        def __init__(self, *args: object, **kwargs: object) -> None:
            attempts.append("socket")
            raise _SocketOpened("the primary path opened a socket")

    monkeypatch.setattr(socket, "socket", Tripwire)
    return attempts


@pytest.fixture
def traced(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine mid-cutover: enabled, shipping, and a proxy URL configured.

    The proxy URL matters — an unconfigured machine has nothing to connect to,
    so the assertion would hold for the wrong reason.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=True)
    monkeypatch.setenv(_KEY_VAR, "-".join(["not", "a", "real", "key"]))
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.ship = True
    config.explainability.target = "stg"
    config.explainability.targets["stg"] = ExplainabilityTarget(
        gateway_url="https://gateway.invalid",
        api_key_env=_KEY_VAR,
        proxy_url="http://127.0.0.1:9099",
    )
    save_config(config)


def _payload(tmp_path: object) -> str:
    return json.dumps({"session_id": "primary-path", "cwd": str(tmp_path), "prompt": "hello"})


def test_the_tripwire_actually_fires(no_network: list[str]) -> None:
    """The control, and the reason anything below means something.

    Without this, "no hook opened a socket" reads identically whether the path
    is clean or the instrument is broken.
    """
    with pytest.raises(_SocketOpened):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    assert no_network == ["socket"], "the tripwire fired without recording"


def test_a_command_that_should_reach_the_network_still_does(
    runner: CliRunner, traced: None, no_network: list[str]
) -> None:
    """Second half of the control: the tripwire is reachable from the CLI too.

    ``explainability env`` probes the proxy by design. If this passed quietly
    the tripwire might simply be unreachable through a CliRunner invocation,
    and every assertion below would be vacuous for that reason instead.
    """
    result = runner.invoke(app, ["explainability", "env", "coder"])

    assert result.exit_code == 1, result.output
    assert result.stdout == "", "a session was routed despite an unreachable proxy"
    assert no_network, "the tripwire is not reachable through a CliRunner invocation"


@pytest.mark.parametrize("hook", _HOOKS)
def test_no_hook_touches_the_network(
    runner: CliRunner, traced: None, no_network: list[str], tmp_path: object, hook: str
) -> None:
    """The clause itself: tracing configured, and the primary path stays local."""
    result = runner.invoke(app, ["hook", hook], input=_payload(tmp_path))

    assert no_network == [], f"hook {hook} opened a socket on the primary path"
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("hook", _HOOKS)
def test_the_neighbouring_clause_still_holds(
    runner: CliRunner, traced: None, no_network: list[str], tmp_path: object, hook: str
) -> None:
    """ "May never cost an exit code" — the clause next to this one.

    Asserted here because a careless fix for the network clause (say, catching
    the tripwire and re-raising) would satisfy the assertion above while
    breaking the one that matters more.
    """
    result = runner.invoke(app, ["hook", hook], input=_payload(tmp_path))

    assert result.exit_code == 0
    assert "Traceback" not in result.output
