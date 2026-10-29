"""Explicit model/effort preferences for agents with native model selection.

Native defaults stay native. Discovery and status never invoke a paid model.
"""

from aisquare.core import harness
from aisquare.core.agent_adapters.types import BadEffortError
from aisquare.core.config import AppConfig, load_config


def _pin(value: str | None) -> str | None:
    return value.strip() or None if value is not None else None


def configured(agent: str, role: str) -> tuple[str | None, str | None]:
    """The model and effort ``[agents.models.<agent>]`` names for ``role``: the seat's,
    then its base role's, then the agent's own; ``(None, None)`` for none, or a config
    that cannot be read (the launch profile reports that)."""
    try:
        config = load_config()
    except Exception:
        config = AppConfig()
    settings = config.agents.models.get(agent)
    seat = settings.roles.get(role) if settings else None
    base = settings.roles.get(harness.base_role(role)) if settings else None
    model = _pin(seat.model if seat else None) or _pin(base.model if base else None)
    effort = _pin(seat.effort if seat else None) or _pin(base.effort if base else None)
    return (
        model or _pin(settings.model if settings else None),
        effort or _pin(settings.effort if settings else None),
    )


def resolve_model(
    agent: str,
    role: str,
    *,
    env: dict[str, str],
    effort: str | None,
) -> harness.ModelResolution:
    configured_model, configured_effort = configured(agent, role)
    model = harness.role_model_override(role, env)
    source = "pinned" if model else "configured"
    model = model or configured_model
    if effort is not None and not effort.strip():
        raise BadEffortError("--effort requires a reasoning level, not an empty value")
    pinned_effort = harness.role_effort_override(role, env)
    level = _pin(effort) or pinned_effort or configured_effort
    return harness.ModelResolution(
        role=role,
        model=model or "",
        effort=level or "",
        source=source if model else "native-default",
        effort_source="explicit"
        if effort is not None
        else "pinned"
        if pinned_effort
        else "configured"
        if level
        else "native-default",
    )
