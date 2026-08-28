"""A role profile can reach the tracing decision, and one binding switches it off.

``launch`` merges the role's bound profile into the environment (``launch.py``
``env.update(profile.env)``) BEFORE handing that environment to
``wire_session`` as ``base_env``. The wiring then stands down from any session
whose routing the user already owns — which is right, and which a profile can
now trigger.

That stand-down was designed for an AMBIENT variable: something exported in a
shell, transient, deliberate, and visible in the terminal that set it. A
profile binding is none of those. It is written to ``config.toml`` by
``aisquare team bind``, it survives every shell, and it applies to EVERY launch
of that role until somebody unbinds it. Same mechanism, very different
lifetime, and the only signal is one dim line on stderr per launch.

Both directions are pinned here because the interesting one is not the failure:
- binding ``CLAUDE_CONFIG_DIR``/``CLAUDE_CODE_TMPDIR`` — the per-seat idiom the
  README prescribes and the one actually in use — must leave tracing alone;
- binding a RESERVED var must stand down, and say so.

The reserved names are read from the code rather than retyped, so this cannot
drift from what the wiring actually reserves.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.core import harness
from aisquare.core.config import load_config
from aisquare.services import explainability as svc
from aisquare.services import explainability_ops as ops
from aisquare.services import settings as settings_service

_BOUNDARY = Path(__file__).resolve().parents[1] / "docs" / "explainability-tracing-boundary.md"


def _configure_tracing() -> None:
    config = load_config()
    config.explainability.enabled = True
    config.explainability.proxy_url = "http://127.0.0.1:9190"
    from aisquare.core.config import save_config

    save_config(config)


def _wire_through_the_launch_path(role: str, prober: object) -> svc.SessionWiring:
    """launch.py:157-224, in order: resolve profile, merge env, then wire."""
    env: dict[str, str] = {}
    profile = harness.resolve_profile(role, env_overrides={})
    env.update(profile.env)
    identity = svc.plan_session_identity("/usr/bin/claude", [])
    effective = ops.effective_settings(load_config().explainability)
    return svc.wire_session(
        effective,
        role,
        session_id=identity.session_id,
        base_env=env,
        prober=prober,  # type: ignore[arg-type]
    )


def _healthy(_url: str) -> svc.ProxyProbe:
    return svc.ProxyProbe(True, "proxy healthy")


def test_the_per_seat_binding_in_use_does_not_disturb_tracing() -> None:
    """The configuration five seats are actually bound with, pinned.

    ``CLAUDE_CONFIG_DIR`` and ``CLAUDE_CODE_TMPDIR`` are what the README's
    parallel-installs section prescribes. Every proof this project has of the
    tracing wiring was taken before any profile existed, so "profiles do not
    interfere" was an assumption until it was measured.
    """
    _configure_tracing()
    settings_service.bind_role(
        "coder1",
        env={"CLAUDE_CONFIG_DIR": "$HOME/.claude3", "CLAUDE_CODE_TMPDIR": "$HOME/.cache/claude3"},
    )

    wiring = _wire_through_the_launch_path("coder1", _healthy)

    assert wiring.traced, f"a seat binding must not cost a trace: {wiring.reason}"
    assert wiring.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9190"
    assert "aisquare-coder1" in wiring.reason


def test_binding_a_reserved_var_in_a_profile_switches_tracing_off_for_that_role() -> None:
    """The durable form of the stand-down, and the reason it needs documenting.

    Standing down is correct — we do not seize routing the operator owns. What
    is new is that ``team bind`` makes that choice PERSISTENT and silent after
    the fact, so a role can be permanently untraced by a line in config.toml
    that nothing in the tracing docs mentions.
    """
    _configure_tracing()
    reserved = svc.RESERVED_ENV_VARS[0]
    settings_service.bind_role("coder1", env={reserved: "http://127.0.0.1:9190"})

    wiring = _wire_through_the_launch_path("coder1", _healthy)

    assert not wiring.traced
    assert reserved in wiring.reason and "not overriding your routing" in wiring.reason
    assert wiring.env == {}, "a stood-down launch must carry no routing of ours"


def test_the_boundary_page_warns_that_a_profile_can_switch_tracing_off() -> None:
    """The hazard is only discoverable from a dim stderr line otherwise.

    Named from the code so the page cannot drift from what is reserved.
    """
    page = _BOUNDARY.read_text(encoding="utf-8")

    assert "team bind" in page, "the page must name the command that can do this"
    for reserved in svc.RESERVED_ENV_VARS:
        assert reserved in page, f"the page must name {reserved} as reserved"
