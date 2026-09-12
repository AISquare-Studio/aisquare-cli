"""Explicit model/effort preferences for agents with native model selection.

Native defaults stay native. Discovery and status never invoke a paid model.
"""

from aisquare.core import harness
from aisquare.core.agent_adapters.types import BadEffortError
from aisquare.core.config import AppConfig, load_config


def _pin(value: str | None) -> str | None:
    return value.strip() or None if value is not None else None


def resolve_model(
    agent: str,
    role: str,
    *,
    env: dict[str, str],
    effort: str | None,
    effort_is_native: bool = False,
) -> harness.ModelResolution:
    try:
        config = load_config()
    except Exception:
        # The launch profile reports the damaged config; native defaults and
        # explicit environment pins must still let the agent start.
        config = AppConfig()
    settings = config.agents.models.get(agent)
    seat = settings.roles.get(role) if settings else None
    base = settings.roles.get(harness.base_role(role)) if settings else None
    model = harness.role_model_override(role, env)
    source = "pinned" if model else "configured"
    model = model or _pin(seat.model if seat else None) or _pin(base.model if base else None)
    model = model or _pin(settings.model if settings else None)
    if not effort_is_native and effort is not None and not effort.strip():
        raise BadEffortError("--effort requires a reasoning level, not an empty value")
    pinned_effort = harness.role_effort_override(role, env)
    level = _pin(effort) or pinned_effort
    level = level or _pin(seat.effort if seat else None) or _pin(base.effort if base else None)
    level = level or _pin(settings.effort if settings else None)
    if effort_is_native:
        level = effort
    return harness.ModelResolution(
        role=role,
        model=model or "",
        effort=level or "",
        source=source if model else "native-default",
        effort_source="native"
        if effort_is_native
        else "explicit"
        if effort is not None
        else "pinned"
        if pinned_effort
        else "configured"
        if level
        else "native-default",
    )
