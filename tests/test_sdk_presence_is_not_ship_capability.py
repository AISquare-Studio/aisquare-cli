"""A console script on PATH is not an importable module, and only one of them ships.

Measured on this machine, one interpreter, three values::

    sdk_presence()  -> SdkPresence(importable=False,
                                   script='/home/work/.local/bin/explainability-doctor',
                                   version=None, shadowing=False)
    sdk_available() -> False
    doctor renders  -> ✓ explainability sdk: SDK present (console script)
    ship says       -> "explainability extra not installed, 0 buffered"

A green row and a refusing command, about the same SDK, in the same run.

THE OR IS THE WHOLE BUG. ``SdkPresence.present`` is ``importable or script is not
None``; ``sdk_available()`` is ``find_spec(SDK_MODULE) is not None`` — importable
alone. ``_check_sdk`` branches on ``present``, so the console script satisfies the
green while the property the client lane actually needs is missing.

AND THE SENTENCE FOR THIS STATE WAS ALREADY WRITTEN, SOMEWHERE IT CANNOT PRINT.
The not-present branch says "the proxy lane still traces model traffic, but the
CLI cannot ship its own insights as spans" — true of the script-only machine
too, and gated behind ``not presence.present``, so the one state where it is both
true and *surprising* is the one state that never says it.

WHY IT IS **NOT** GATED ON ``settings.ship``, WHICH IS WHERE THIS FIX STARTED.
``config.py`` calls ``ship`` "the single predicate on the primary path", which
reads like the right gate and is not: ``shipping_offer()`` refuses with "extra
not installed" whenever the SDK is not importable, and ``configure_shipping``
sits behind that refusal, so **ship=True and not-importable are mutually
exclusive**. Measured, not reasoned — ``init --explainability`` in a throwaway
home on this script-only machine answered "Explainability not configured —
explainability extra not installed" and left ``ship = False``.

A ``shipping and not importable`` gate is therefore UNREACHABLE in production,
and would have passed its unit tests forever because the tests build the state
by hand. The gate is ``target.configured`` — a gateway AND a key — which is
exactly the machine that WOULD ship and is silently not shipping. A stock
machine with nothing configured stays quiet, because a warning about a lane
nobody could use is how a section stops being read.

WHY IT IS A WARN AND NOT A FAIL. "No hard SDK dependency", and the proxy lane
really does keep tracing model traffic without the client lane. What is lost is
clause 2 of the north star — the insights the CLI itself holds — and losing it
silently is the defect, not losing it.
"""

from __future__ import annotations

from unittest import mock

import pytest

from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import explainability_ops as ops
from aisquare.services.explainability_ops import SdkPresence

_SCRIPT = "/home/work/.local/bin/explainability-doctor"

#: The measured state above, as a fixture value.
_SCRIPT_ONLY = SdkPresence(importable=False, script=_SCRIPT, version="1.0.6", shadowing=False)
_IMPORTABLE = SdkPresence(importable=True, script=_SCRIPT, version="1.0.6", shadowing=False)
_ABSENT = SdkPresence(importable=False, script=None, version=None, shadowing=False)
#: THE ACTUAL LIVE SHAPE on this machine: a script-only install exposes no
#: version, because `sdk_presence` reads dist metadata this environment cannot
#: see. The tests above used "1.0.6" and so never rendered the sentence a real
#: operator gets — which read "SDK present is reachable only as…".
_SCRIPT_ONLY_NO_VERSION = SdkPresence(
    importable=False, script=_SCRIPT, version=None, shadowing=False
)
_SHADOWING = SdkPresence(importable=True, script=None, version=None, shadowing=True)


def _row(presence: SdkPresence, *, deployable: bool, on: bool = True) -> DoctorCheck:
    """Patched rather than injected: `_check_sdk` should not grow a parameter
    that exists only so a test can reach it, and patching exercises the real
    lookup the production path uses."""
    with mock.patch.object(ops, "sdk_presence", lambda: presence):
        return ops._check_sdk(on=on, live=True, deployable=deployable)


def test_a_script_only_sdk_is_not_green_on_a_machine_that_would_ship() -> None:
    """The defect. `present` was True, so this row read ok and said nothing."""
    assert _row(_SCRIPT_ONLY, deployable=True).status is CheckStatus.warn


def test_it_never_fails_the_machine() -> None:
    """Doctrine: no hard SDK dependency, and the proxy lane is unaffected."""
    assert _row(_SCRIPT_ONLY, deployable=True).status is not CheckStatus.fail


def test_the_row_names_the_lane_that_is_actually_broken() -> None:
    """ "SDK problem" is not actionable; "your insights will not ship" is.

    The operator's next question is always "so what stopped working" — a row
    that does not answer it sends them to re-install something that is, by its
    own console script, already installed.
    """
    detail = _row(_SCRIPT_ONLY, deployable=True).detail

    assert "ship" in detail.lower()
    assert "import" in detail.lower(), detail


def test_the_row_still_says_where_the_sdk_is() -> None:
    """Softening the status must not cost the only clue that was already there.

    The script path is what tells an operator this is a two-environments
    problem rather than a missing package — which is the difference between
    the right fix and re-running an install that already succeeded.
    """
    detail = _row(_SCRIPT_ONLY, deployable=True).detail

    assert _SCRIPT in detail, detail
    assert "1.0.6" in detail, detail


def test_an_unconfigured_machine_stays_quiet() -> None:
    """No gateway and no key: there is no lane to be off, so this is not news."""
    assert _row(_SCRIPT_ONLY, deployable=False).status is CheckStatus.ok


def test_an_importable_sdk_is_green_even_when_deployable() -> None:
    """The control: this must not fire on a correctly installed machine."""
    assert _row(_IMPORTABLE, deployable=True).status is CheckStatus.ok


def test_an_absent_sdk_keeps_its_old_verdict() -> None:
    """Unchanged branch, pinned because the new one sits right beside it."""
    assert _row(_ABSENT, deployable=True).status is CheckStatus.warn
    assert "not installed" in _row(_ABSENT, deployable=True).detail


def test_shadowing_keeps_its_own_message() -> None:
    """The other pre-existing branch, which outranks everything below it."""
    row = _row(_SHADOWING, deployable=True)

    assert row.status is CheckStatus.warn
    assert "overwrote" in row.detail


def test_the_remedy_does_not_tell_an_editable_checkout_to_install_the_extra() -> None:
    """`install_hint()` knows what the bare constant does not.

    In an editable checkout the extra SHADOWS the CLI — install_hint's own
    words: "every command dies with No module named 'aisquare.cli'". A row
    that printed the raw INSTALL_HINT would hand a developer the one command
    that breaks their machine, while telling them it is the fix.
    """
    from aisquare.services.explainability import install_hint

    remediation = _row(_SCRIPT_ONLY, deployable=True).fix or ""

    assert remediation.endswith(install_hint()), remediation


def test_the_deployable_flag_actually_reaches_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring, which every test above would pass without.

    `_check_sdk` can be perfectly correct and still never see the real value.
    This is the only test here that goes through `checks()`, and it is the one
    that fails if the argument is not threaded.
    """
    from aisquare.core.config import ExplainabilitySettings, ExplainabilityTarget

    monkeypatch.setattr(ops, "sdk_presence", lambda: _SCRIPT_ONLY)
    # NOT ship=True. That state cannot exist beside a non-importable SDK, and
    # writing it here would rebuild by hand the impossibility this file exists
    # to document. A gateway and a key is the whole precondition.
    settings = ExplainabilitySettings(
        enabled=True,
        targets={"stg": ExplainabilityTarget(gateway_url="https://gw.invalid")},
    )

    rows = ops.checks(settings, live=False, env={"EXPLAINABILITY_API_KEY": "k"})
    row = next(r for r in rows if r.name == "explainability sdk")

    assert row.status is CheckStatus.warn, row.detail


def test_shipping_cannot_be_on_while_the_sdk_is_unimportable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The premise correction, pinned so nobody re-derives it at 03:00.

    This is the fact that moved the gate off ``settings.ship``. If a future
    change lets shipping turn on without an importable SDK, the gate below
    becomes wrong and this test is the one that says so — the row would then
    need to fire on the ship flag as well.
    """
    from aisquare.services import explainability as svc

    monkeypatch.setattr(svc, "sdk_available", lambda: False)
    offer = svc.shipping_offer()

    assert not offer.available
    assert "extra not installed" in offer.reason


def test_the_versionless_shape_reads_as_english() -> None:
    """The real machine has version=None; the fixtures above did not.

    `f"SDK {version or 'present'}"` renders "SDK present is reachable only as a
    console script", which is the sort of line a reader trusts slightly less
    for the rest of the section. Pinned on the live shape, not the tidy one.
    """
    detail = _row(_SCRIPT_ONLY_NO_VERSION, deployable=True).detail

    assert "SDK present is" not in detail, detail
    assert detail.startswith("the SDK is reachable only as a console script"), detail
    assert _SCRIPT in detail
