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

#: The four places the one id must reach, named as DATA.
#:
#: This started as four blocks of inline assertions, and @8dd460fb's third
#: sabotage says that shape can be quietly narrowed: measured at 5fd9f3b,
#: deleting the join-log block left the file GREEN, and so did deleting the
#: header block — five passed either way, while the name, the docstring and the
#: handoff's evidence table all still said FOUR. Not a wrong assertion; an
#: assertion that stopped being made.
#:
#: As a set it cannot go quiet: removing a place changes something this file
#: compares, and the failure names what went missing.
SPINE_PLACES = frozenset(
    {
        "X-Pipeline-Id header",
        "AISQUARE_PIPELINE_ID marker",
        "board row",
        "join log",
    }
)

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


def _places_carrying_another_id(
    observed: dict[str, tuple[str, str]], minted: str
) -> dict[str, str]:
    """Places whose value is not the one that was minted.

    A CALLABLE rule rather than a comprehension inside the test, because this
    guard's ratchet is EMPTY: "nothing disagrees" is what success looks like,
    so a rule that has gone blind produces the correct-looking answer for free.
    Measured before this existed — ``if value != minted`` replaced by
    ``if False`` left SIX TESTS PASSING, and the guard reported that one minted
    id reached all four places while comparing none of them.

    @8dd460fb named the wrinkle: in an empty-ratchet guard, emptiness is both
    the goal and the symptom, and the only thing that can tell them apart is an
    input the rule must still SEE. Hence the positive control below, driven
    through this same function rather than around it — proving a helper works
    says nothing about whether the assertion still calls it.
    """
    return {place: value for place, (value, _origin) in observed.items() if value != minted}


def test_the_disagreement_rule_reports_a_place_that_carries_another_id() -> None:
    """Positive control: the input this rule must still see.

    Synthetic rather than the real spine walk, so it keeps controlling when the
    walk changes — and it names the place, because "something disagreed" would
    not tell whoever reads the failure which hop broke.
    """
    observed = {
        "X-Pipeline-Id header": ("the-minted-id", "wiring.env"),
        "board row": ("a-different-id", "team status --json"),
    }

    reported = _places_carrying_another_id(observed, "the-minted-id")

    assert reported == {"board row": "a-different-id"}


def test_the_disagreement_rule_stays_quiet_when_every_place_agrees() -> None:
    """Negative control, so "report everything" is not a fix.

    Without it the cheapest way to satisfy the positive control is a rule that
    always fires, and a guard that fails on a healthy spine gets deleted rather
    than repaired.
    """
    observed = {
        "X-Pipeline-Id header": ("the-minted-id", "wiring.env"),
        "board row": ("the-minted-id", "team status --json"),
    }

    assert _places_carrying_another_id(observed, "the-minted-id") == {}


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

    # Each place records the value AND where it was read from. The origins are
    # asserted distinct below, because "four places" satisfied four times out of
    # one object is the same narrowing one level down.
    observed: dict[str, tuple[str, str]] = {}

    headers = wiring.env["ANTHROPIC_CUSTOM_HEADERS"]
    header_id = next(
        line.split(":", 1)[1].strip()
        for line in headers.splitlines()
        if line.strip().startswith("X-Pipeline-Id:")
    )
    observed["X-Pipeline-Id header"] = (header_id, "wiring.env")

    marker = trace_marker(wiring)
    observed["AISQUARE_PIPELINE_ID marker"] = (marker[PIPELINE_ID_ENV_VAR], "trace_marker()")

    # The board row, created by the SessionStart hook the way Claude Code
    # invokes it — the agent was started ON the minted id.
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
    assert len(sessions) == 1, f"expected one board row, got {len(sessions)}"
    observed["board row"] = (sessions[0]["id"], "team status --json")

    joins = paths.explainability_joins_path()
    assert joins.exists(), "no join was recorded for a traced session"
    records = [json.loads(line) for line in joins.read_text().splitlines() if line.strip()]
    assert records, "join log is empty"
    assert records[-1]["session_id"] == records[-1]["pipeline_id"], (
        "the join log's two halves disagree, so it joins nothing"
    )
    observed["join log"] = (records[-1]["pipeline_id"], "joins.jsonl on disk")

    missing = SPINE_PLACES - set(observed)
    assert not missing, f"the spine control stopped checking: {sorted(missing)}"
    assert set(observed) == SPINE_PLACES, (
        f"unexpected place: {sorted(set(observed) - SPINE_PLACES)}"
    )

    origins = {origin for _value, origin in observed.values()}
    assert len(origins) == len(SPINE_PLACES), (
        f"four places must come from four sources, got {sorted(origins)} — "
        "the same object satisfying several places is the narrowing one level down"
    )

    # No intermediate variable on purpose: `disagreeing = {}` was a one-word
    # bypass that left every assertion here passing. Calling the rule inside
    # the assert means a bypass has to edit the assert itself, which is a
    # conspicuous edit rather than a silent one.
    assert not _places_carrying_another_id(observed, minted), (
        "these places carry a different id than the mint: "
        f"{_places_carrying_another_id(observed, minted)}"
    )
    # The rule, demonstrated on THIS data rather than on a synthetic mapping:
    # seed one place with a wrong id and require it to be reported. Without
    # this, a rule blinded to disagreements produces the empty answer that
    # success also produces, which is @8dd460fb's empty-ratchet wrinkle.
    #
    # It does NOT close the regress: `disagreeing = {}` above still passes,
    # because an assertion's input can always be replaced with a literal and
    # nothing in this file can see that. What it buys is that the rule cannot
    # go blind unnoticed — only that this one assertion can be bypassed
    # deliberately, which is a conspicuous edit rather than a silent one.
    seeded = {**observed, "board row": (minted + "-not-the-mint", "team status --json")}
    assert _places_carrying_another_id(seeded, minted) == {"board row": minted + "-not-the-mint"}, (
        "the rule cannot see a disagreement in the very data this test just checked"
    )


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


#: The claims this file exists to make. Deleting a PLACE now fails, because the
#: places are a set — but deleting a whole TEST still passed, which is the same
#: narrowing one level up: sabotage D, measured, 4 green with the distinctness
#: control gone, and that control is the one thing standing between us and a
#: mint that returns a constant.
#:
#: This does not reach a fixed point — the guard itself can be deleted. What it
#: buys is that narrowing stops being SILENT: you can no longer quietly drop a
#: claim, only conspicuously remove a claim and the record that it was made,
#: which is two edits in one diff and reads as a decision rather than an
#: oversight.
REQUIRED_CLAIMS = frozenset(
    {
        "test_one_minted_id_reaches_all_four_places",
        "test_two_sessions_do_not_share_an_id",
        "test_the_agent_name_follows_the_role",
        "test_the_spawn_template_passes_the_flag_the_parser_looks_for",
        "test_an_untraced_session_passes_no_session_id_at_all",
        "test_the_disagreement_rule_reports_a_place_that_carries_another_id",
        "test_the_disagreement_rule_stays_quiet_when_every_place_agrees",
    }
)


_THIS_GUARD = "test_this_file_still_makes_every_claim_it_says_it_makes"


def test_this_file_still_makes_every_claim_it_says_it_makes() -> None:
    """Guard the guard, at the level a deleted test lives on.

    Named from the module rather than counted, so the failure says WHICH claim
    stopped being made — a count would only say that one did.
    """
    present = {name for name in globals() if name.startswith("test_")} - {_THIS_GUARD}

    missing = REQUIRED_CLAIMS - present
    unregistered = present - REQUIRED_CLAIMS

    assert not missing, f"this file no longer makes these claims: {sorted(missing)}"
    # The other direction, so the list cannot rot into a stale subset: a new
    # claim that is never registered would leave REQUIRED_CLAIMS describing an
    # older, smaller file while passing. Same reason @8dd460fb's ratchets fail
    # both ways.
    assert not unregistered, (
        f"new claims are not registered in REQUIRED_CLAIMS: {sorted(unregistered)}"
    )
    assert REQUIRED_CLAIMS, "an empty REQUIRED_CLAIMS would satisfy both assertions above"
