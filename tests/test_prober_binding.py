"""``wire_session`` must resolve its prober when it runs, not when it was defined.

A default argument binds at DEF time, so ``prober: Callable = probe_proxy``
captures the function object at import and monkeypatching
``services.explainability.probe_proxy`` afterwards cannot reach it. Nothing is
lying today — every existing test passes ``prober=`` explicitly — but all three
production callers (``cli/launch``, ``cli/team``, ``cli/explainability``) pass
nothing. So the first test that drives one of those through ``CliRunner`` and
patches ``probe_proxy`` gets the REAL network prober and passes by agreeing with
reality rather than by verifying anything.

It has already cost two people: the same shape in the sibling ``proxy_state``,
and a wrapper in ``test_role_profile.py`` that existed only to work around this
binding.
"""

from __future__ import annotations

import pytest

from aisquare.core.config import ExplainabilitySettings
from aisquare.services import explainability as service
from aisquare.services.explainability import ProxyProbe

TRACING_ON = ExplainabilitySettings(enabled=True, proxy_url="http://127.0.0.1:9")


def _never_called(url: str) -> ProxyProbe:
    raise AssertionError(f"the real prober ran against {url} — the patch did not take effect")


def _healthy(url: str) -> ProxyProbe:
    return ProxyProbe(True, "patched prober answered")


def test_patching_the_module_prober_reaches_wire_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: a module-level patch must redirect the default.

    This FAILS against the default-argument form — verified by running it
    against the unmodified tree, where the real probe_proxy dials 127.0.0.1:9
    and the wiring comes back untraced.
    """
    monkeypatch.setattr(service, "probe_proxy", _healthy)

    wiring = service.wire_session(TRACING_ON, "coder", session_id="s1")

    assert wiring.traced, wiring.reason
    assert (
        "patched prober answered" not in wiring.reason
    )  # reason is about the trace, not the probe


def test_an_explicit_prober_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving late must not take the argument away from callers that pass one."""
    monkeypatch.setattr(service, "probe_proxy", _never_called)

    wiring = service.wire_session(TRACING_ON, "coder", prober=_healthy)

    assert wiring.traced, wiring.reason


def test_an_unpatched_call_still_uses_the_real_prober() -> None:
    """No behaviour change for production: a real launch probes the real proxy.

    Nothing is patched here, so the real ``probe_proxy`` runs against a port
    with nothing on it and the session launches untraced, with the reason.
    """
    wiring = service.wire_session(TRACING_ON, "coder")

    assert not wiring.traced
    assert "unreachable" in wiring.reason or "untraced" in wiring.reason


def test_neither_prober_taker_binds_at_def_time() -> None:
    """proxy_state was fixed first; the pair must not drift back apart.

    Asserted on the SIGNATURE because that is where the trap lives: a callable
    default here is invisible at every call site and silently defeats a patch.
    """
    import inspect

    from aisquare.services import explainability_ops

    for function in (service.wire_session, explainability_ops.proxy_state):
        default = inspect.signature(function).parameters["prober"].default
        assert default is None, (
            f"{function.__name__} binds its prober at def time — patching "
            "services.explainability.probe_proxy will silently do nothing"
        )
