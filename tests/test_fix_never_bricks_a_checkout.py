"""``doctor --fix`` must not pip-install the SDK into an editable checkout.

The SDK's distribution is ``aisquare`` and so is this package's import name. In
a NON-editable install the two merge — different subdirectories under one
``site-packages/aisquare/`` — and installing the extra is the reversible, local
repair ``--fix`` advertises. In an EDITABLE install they do not merge: the SDK
ships a real ``aisquare/__init__.py`` into ``site-packages``, which precedes the
``.pth``-appended ``src`` on ``sys.path``, so the SDK wins the name wholesale
and every command dies with ``No module named 'aisquare.cli'``. Measured on
both editable shapes this project can produce; ``pip install -e`` does not
recover it, only uninstalling the SDK does.

WHY THIS IS A TEST AND NOT A DOC. ``services/explainability.py`` already
carried both halves of the answer — ``running_editable()`` and
``EDITABLE_INSTALL_HINT`` — and the hint was printed on the paths that only
*advise* an install. The one path that actually *performs* one did not consult
it, and the suite itself is a caller: ``tests/test_doctor_does_not_create_state``
invokes ``doctor --fix --yes`` three times against a checkout. So pytest
pip-installed the SDK into its own interpreter, mid-run, over the network, and
every subprocess-spawning test collected after it failed against a shadowed
CLI — 15 of them — while the test that caused it passed. The damage outlived
the run: the developer's venv stayed bricked afterwards.

That is the shape this file pins. A guard that lives only in a comment gets
re-derived by the next person to read the traceback.
"""

from __future__ import annotations

import pytest

from aisquare.services import explainability_ops as ops
from aisquare.services.explainability import running_editable

_ABSENT = ops.SdkPresence(importable=False, script=None, version=None, shadowing=False)


def test_a_checkout_refuses_the_install_even_when_told_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--yes`` is consent to a repair, and this would not be one.

    Consent cannot make it safe: what the operator agreed to was a fix, and the
    outcome is a CLI that cannot start. So the refusal outranks the flag rather
    than being one more thing the flag overrides.
    """
    monkeypatch.setattr(ops, "sdk_presence", lambda: _ABSENT)
    monkeypatch.setattr(ops, "running_editable", lambda: True)
    monkeypatch.setattr(
        ops, "install_sdk", lambda: pytest.fail("pip-installed the SDK into a checkout")
    )

    actions = ops.apply_fixes(assume_yes=True)

    assert any("refused" in action for action in actions), actions


def test_the_refusal_says_how_to_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal that does not name the way forward just moves the confusion.

    Both halves matter: a separate environment is the fix, and
    ``pip uninstall aisquare`` is the recovery for someone who already did it
    the other way and is reading this from a shell where nothing works.
    """
    monkeypatch.setattr(ops, "sdk_presence", lambda: _ABSENT)
    monkeypatch.setattr(ops, "running_editable", lambda: True)
    monkeypatch.setattr(ops, "install_sdk", lambda: (True, "installed"))

    reported = " ".join(ops.apply_fixes(assume_yes=True))

    assert "separate" in reported
    assert "pip uninstall aisquare" in reported


def test_a_real_install_is_still_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    """The POSITIVE control, without which the guard could be `return []`.

    A refusal that fired everywhere would pass both tests above and quietly
    remove the one repair ``--fix`` exists to perform for the operators who are
    not running from a checkout — which is everyone the runbook addresses.
    """
    monkeypatch.setattr(ops, "sdk_presence", lambda: _ABSENT)
    monkeypatch.setattr(ops, "running_editable", lambda: False)
    monkeypatch.setattr(ops, "install_sdk", lambda: (True, "installed aisquare[explainability]"))

    actions = ops.apply_fixes(assume_yes=True)

    assert any("installed" in action for action in actions), actions
    assert not any("refused" in action for action in actions), actions


def test_the_suite_itself_runs_from_a_checkout() -> None:
    """Guard the guard: the refusal only protects the suite if this is true here.

    ``running_editable`` is patched in the tests above precisely because it is
    the thing under test; this one asks the unpatched question, so the premise
    those tests rest on cannot rot silently. If this ever fails, the suite is
    grading an installed copy and ``tests/conftest.py``'s import guard has more
    to say about it than this file does.

    Imported from its own module rather than read off ``ops``: the name is not
    part of that module's interface, it is just visible there, and mypy strict
    is right to say so.
    """
    assert running_editable() is True
