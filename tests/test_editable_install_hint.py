"""Our own advice must not brick the machine it is printed on.

`aisquare-cli[explainability]` is safe on a normal install: both distributions
land in one site-packages directory, their subpackages merge, and only the
top-level `__init__.py` collides — which the CLI survives because nothing reads
a name out of it (see test_sdk_coexistence).

An EDITABLE install is different in kind, not in degree. The editable hook is a
`.pth` line appending the checkout's `src/` to `sys.path`, and site-packages
comes FIRST. So the SDK's real `aisquare/` package does not merge with the
checkout — it SHADOWS it wholesale, and every command dies with:

    ModuleNotFoundError: No module named 'aisquare.cli'

Measured on BOTH editable shapes pip produces — the `.pth` path line and the
import-hook form (`_editable_impl_*.py`, hatchling's `dev-mode-exact`) — and
both are bricked identically, so this is a property of editable installs rather
than of one packaging style. Also measured: reinstalling editable does NOT
recover it; only `pip uninstall aisquare` does; and a non-editable install with
the extra is still fine.

The CLI is dead at that point, so no doctor check can report it — the only
useful moment is BEFORE, while we are still the ones printing the advice.
"""

from __future__ import annotations

import pytest

from aisquare.services import explainability as service


def test_a_normal_install_is_advised_to_use_the_extra() -> None:
    """The safe path stays the recommendation."""
    hint = service.install_hint(editable=False)

    assert "aisquare-cli[explainability]" in hint
    assert "editable" not in hint.lower()


def test_an_editable_install_is_warned_off_it() -> None:
    """Printing the install line here would break the machine reading it."""
    hint = service.install_hint(editable=True)

    assert "editable" in hint.lower()
    assert 'pip install --upgrade "aisquare-cli[explainability]"' not in hint, (
        "an editable checkout must not be handed the command that shadows it"
    )


def test_the_editable_warning_names_the_symptom_and_the_recovery() -> None:
    """Someone hits this with a dead CLI and a search engine. Give them both."""
    hint = service.install_hint(editable=True)

    assert "No module named 'aisquare.cli'" in hint, "the symptom they will search for"
    assert "pip uninstall aisquare" in hint, (
        "the ONLY thing measured to recover it — reinstalling editable does not"
    )


def test_running_from_site_packages_is_not_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_package_root", lambda: "/usr/lib/python3/site-packages/aisquare")

    assert service.running_editable() is False


def test_running_from_a_checkout_is_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_package_root", lambda: "/home/me/work/aisquare-cli/src/aisquare")

    assert service.running_editable() is True


def test_detection_does_not_depend_on_the_editable_packaging_style() -> None:
    """Both shapes pip emits resolve the package from the CHECKOUT, not site-packages.

    Verified against a real import-hook install (hatchling `dev-mode-exact`,
    which writes `_editable_impl_*.py` plus a `.pth` that imports it) as well as
    the default `.pth` path form: both report editable, and both are shadowed by
    the extra identically. Asking WHERE the package resolves from — rather than
    HOW the install is wired — is what makes the detector indifferent to that.
    """
    assert "site-packages" not in service._package_root()
    assert service.running_editable() is True


def test_the_shipping_reason_carries_the_right_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason line is where an operator actually meets this."""
    from aisquare.core import insights
    from aisquare.core.config import AppConfig, save_config

    config = AppConfig()
    config.explainability.ship = True
    config.explainability.gateway_url = "https://gateway.example"
    save_config(config)
    insights.reset_cache()
    monkeypatch.setenv("EXPLAINABILITY_API_KEY", "sk-test")
    monkeypatch.setattr(service, "sdk_available", lambda: False)
    monkeypatch.setattr(service, "running_editable", lambda: True)

    reason = service.shipping_state().reason

    assert "editable" in reason.lower(), reason


def test_this_very_checkout_is_detected_as_editable() -> None:
    """The suite runs from an editable install, so the detector must say so.

    A detector that returned False here would be silently useless in exactly
    the environment that needs it.
    """
    assert service.running_editable() is True
