"""Explicit model/effort preferences for agents with native model selection.

Native defaults stay native. Discovery and status never invoke a paid model.
"""

from aisquare.core import harness
from aisquare.core.config import load_config


def resolve_model(
    agent: str,
    role: str,
    *,
    env: dict[str, str],
    effort: str | None,
) -> harness.ModelResolution:
    settings = load_config().agents.models.get(agent)
    role_settings = settings.roles.get(harness.base_role(role)) if settings else None
    suffix = "".join(c if c.isalnum() else "_" for c in role.upper())
    model = env.get(f"AISQUARE_MODEL_{suffix}")
    source = "pinned" if model else "configured"
    model = model or (role_settings.model if role_settings else None)
    model = model or (settings.model if settings else None)
    level = effort or env.get(f"AISQUARE_EFFORT_{suffix}")
    level = level or (role_settings.effort if role_settings else None)
    level = level or (settings.effort if settings else None)
    return harness.ModelResolution(
        role=role,
        model=model or "",
        effort=level or "",
        source=source if model or level else "native-default",
        effort_source="explicit" if effort else "configured" if level else "native-default",
    )
