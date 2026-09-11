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
) -> harness.ModelResolution:
    try:
        config = load_config()
    except Exception:
        # The launch profile reports the damaged config; native defaults and
        # explicit environment pins must still let the agent start.
        config = AppConfig()
    settings = config.agents.models.get(agent)
    role_settings = settings.roles.get(harness.base_role(role)) if settings else None
    suffix = "".join(c if c.isalnum() else "_" for c in role.upper())
    model = _pin(env.get(f"AISQUARE_MODEL_{suffix}"))
    source = "pinned" if model else "configured"
    model = model or _pin(role_settings.model if role_settings else None)
    model = model or _pin(settings.model if settings else None)
    if effort is not None and not effort.strip():
        raise BadEffortError("--effort requires a reasoning level, not an empty value")
    pinned_effort = _pin(env.get(f"AISQUARE_EFFORT_{suffix}"))
    level = _pin(effort) or pinned_effort
    level = level or _pin(role_settings.effort if role_settings else None)
    level = level or _pin(settings.effort if settings else None)
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
