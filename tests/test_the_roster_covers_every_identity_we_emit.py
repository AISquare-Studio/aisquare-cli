"""We register three identities and can ship under a fourth.

Measured on the train, three lines from one interpreter::

    registered roster : ('aisquare-planner', 'aisquare-coder', 'aisquare-runner')
    unattributed run  : aisquare-cli
    unknown session   : aisquare-cli

``_agent_name_for`` opens with ``role = "cli"`` and keeps it whenever the run is
``UNATTRIBUTED_RUN``, the session is absent from the store, the session has no
role, or the store read throws — "an unknown role still ships, as cli", in its
own words. ``ship_once`` passes that straight to
``AgentRunTracer(agent_name=...)``, so it becomes ``X-Agent-Name``. But
``register`` builds its roster from ``settings.roles``, and ``cli`` is not a
role, so that name is never registered.

THE CONSEQUENCE IS A BACKLOG, NOT A DELETION — AND THE FIRST VERSION OF THIS
DOCSTRING SAID OTHERWISE. I wrote that these spans were dead-lettered and lost
permanently; ``dfd9a883`` corrected it, and the correction is worth keeping here
because a wrong severity shapes a wrong fix. There are THREE ``409``s and they
diverge exactly on this case. From ``sweeper.py`` at
``/home/work/work/AISquare-Explainability-SDK`` @ ``bb88bb5`` (``aisquare``
1.0.6), quoting its own comments:

* ``agent_not_registered`` — "the agent is named but IAM has no mapping … a
  routine onboarding race, transient — retried forever";
* ``awaiting_trace_route`` — child-only batch, no route pin yet: transient;
* ``no_agent_identity`` — the batch holds the trace's TRUE root span and still
  has no agent name ANYWHERE, "deterministic POISON", dead-lettered after
  ``_NO_AGENT_IDENTITY_GRACE``.

``aisquare-cli`` IS a name, so it takes the first branch — ``gateway/main.py``
raises ``409 agent_not_registered`` with ``routing.agent_name`` attached. So
the real cost is a silent, indefinitely growing backlog that nothing reports,
and registering the name later still drains it.

That makes the fix MORE obviously right, not less: this is an onboarding gap,
and declaring the identity is exactly what closes an onboarding gap. It also
means there is nothing here to build — no dead-letter handling, no loss alarm,
no retry policy. The SDK already retries correctly and adding to it would
encode a failure mode that does not occur.

It lands on the sessions nobody is watching: one started outside ``team spawn``,
one whose board row is roleless, one whose store read blipped. Clause 2 of the
north star loses exactly those.

WHY THE FIX IS TO REGISTER THE NAME AND NOT TO STOP EMITTING IT. Resolving a
roleless run to a registered role instead would mis-file it: ``_agent_name_for``
exists precisely "so a planner's Run is not a coder's — the gateway collapses
runs and costs under a shared name". A run of unknown provenance is honestly
``cli``; the roster is what was wrong.

The first test below is the one that matters, because it is the only one that
fails when the two modules drift apart — it asks the emitter what it can produce
and the roster whether it covers it, rather than comparing two hand-written
lists that a later edit would update in one place.
"""

from __future__ import annotations

import pytest

from aisquare.core.config import ExplainabilitySettings
from aisquare.services.explainability import UNATTRIBUTED_RUN, _agent_name_for
from aisquare.services.explainability_ops import resolve_target


def _roster(settings: ExplainabilitySettings) -> tuple[str, ...]:
    return resolve_target(settings, env={}).agent_names


@pytest.mark.parametrize("session_id", [UNATTRIBUTED_RUN, "a-session-not-in-the-store"])
def test_every_name_the_ship_path_can_emit_is_registered(session_id: str) -> None:
    """The whole claim, asked of the two modules rather than of a literal.

    A hand-written expected list would pass forever if someone changed the
    fallback, which is exactly the drift this exists to catch.
    """
    settings = ExplainabilitySettings()

    emitted = _agent_name_for(settings, session_id)

    assert emitted in _roster(settings), (emitted, _roster(settings))


def test_the_three_role_identities_are_still_there_and_still_first() -> None:
    """The control. Registering a fourth name must not disturb the three that
    §1a already registers, nor their order — the runbook quotes them as a list.
    """
    assert _roster(ExplainabilitySettings())[:3] == (
        "aisquare-planner",
        "aisquare-coder",
        "aisquare-runner",
    )


def test_a_custom_template_renders_the_fallback_too() -> None:
    """The fallback is a ROLE, not a name — so it goes through the template.

    Hardcoding "aisquare-cli" into the roster would register a name a machine
    with its own template never emits, and miss the one it does.
    """
    settings = ExplainabilitySettings(agent_name_template="acme-{role}")

    assert _agent_name_for(settings, UNATTRIBUTED_RUN) in _roster(settings)
    assert "acme-cli" in _roster(settings)


def test_custom_roles_are_unaffected() -> None:
    """Somebody else's roster must keep working, with the fallback added once."""
    settings = ExplainabilitySettings(roles=["reviewer"])

    assert _roster(settings) == ("aisquare-reviewer", "aisquare-cli")


def test_a_machine_that_already_calls_a_role_cli_gets_no_duplicate() -> None:
    """Registering the same name twice is not an error, but a roster that
    repeats itself is a roster somebody will read as a bug."""
    settings = ExplainabilitySettings(roles=["cli", "coder"])

    assert _roster(settings) == ("aisquare-cli", "aisquare-coder")


def test_the_roster_never_grows_a_name_nothing_can_emit() -> None:
    """The promise runs both ways: registering identities the CLI cannot
    produce trains an operator to ignore a list that is partly fiction."""
    settings = ExplainabilitySettings()
    emittable = {_agent_name_for(settings, UNATTRIBUTED_RUN)} | {
        settings.agent_name_template.format(role=r) for r in settings.roles
    }

    assert set(_roster(settings)) == emittable


def test_doctor_shows_the_identity_it_will_actually_use() -> None:
    """The surface built to show identities was hiding the fourth one.

    An operator registers exactly what this row lists, so a name missing here
    is a name that will 409 in production.
    """
    from aisquare.core.config import ExplainabilityTarget
    from aisquare.services import explainability_ops as ops

    settings = ExplainabilitySettings(
        enabled=True,
        targets={"stg": ExplainabilityTarget(gateway_url="https://gw.invalid")},
    )
    rows = ops.checks(settings, live=False, env={"EXPLAINABILITY_API_KEY": "k"})
    detail = next(r for r in rows if r.name == "explainability config").detail

    assert "aisquare-cli" in detail, detail
