"""Shared agent/profile selection, before native command or model resolution."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from aisquare.core import harness, orchestrator, paths
from aisquare.core.agent_adapters import adapter_for_binary, get_adapter
from aisquare.core.agent_adapters.types import AgentAdapter, config_home
from aisquare.core.config import AppConfig, load_config, save_config
from aisquare.core.store import store_session

ACTIVE_AGENT_ENV = "AISQUARE_CODING_AGENT"


@dataclass(frozen=True)
class ResolvedAgent:
    adapter: AgentAdapter
    binary: harness.BinaryResolution
    source: str
    profile: harness.LaunchProfile
    config_dir: Path


def executable(selected: ResolvedAgent) -> str | None:
    effective_path = selected.profile.env.get("PATH", os.environ.get("PATH"))
    if effective_path == os.environ.get("PATH"):
        return shutil.which(selected.binary.binary)
    return shutil.which(selected.binary.binary, path=effective_path)


def project_default(cwd: Path | None = None) -> str | None:
    # Do not create a store just to display defaults/doctor on a fresh machine.
    if not paths.db_path().exists():
        return None
    project = orchestrator.team_project(cwd)
    with store_session() as store:
        return store.get_meta(f"coding-agent:{project.id}")


def resolve(
    role: str = "coder",
    *,
    agent: str | None = None,
    binary: str | None = None,
    cwd: Path | None = None,
    env_overrides: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> ResolvedAgent:
    profile = harness.resolve_profile(role, env_overrides=env_overrides, extra_args=extra_args)
    chosen_binary = harness.resolve_binary(role, override=binary)
    try:
        config = load_config()
    except Exception:
        config = AppConfig()  # profile.notice carries the existing fail-open explanation
    bound = config.team.profiles.get(role)
    inferred = (
        adapter_for_binary(chosen_binary.binary) if chosen_binary.source != "default" else None
    )
    try:
        preferred = project_default(cwd)
    except Exception:
        preferred = None  # a damaged board must not prevent launching
    choices = (
        (agent, "flag"),
        (bound.agent if bound else None, "role"),
        (inferred.id if inferred else None, chosen_binary.source),
        (preferred, "project"),
        (os.environ.get(ACTIVE_AGENT_ENV), "inherited"),
        (config.agents.default, "user"),
        ("claude-code", "default"),
    )
    name, source = next((value, origin) for value, origin in choices if value)
    adapter = get_adapter(name)
    if chosen_binary.source == "default":
        chosen_binary = harness.BinaryResolution(binary=adapter.binary, source=source)
    elif inferred is not None and inferred.id != adapter.id:
        raise ValueError(
            f"{chosen_binary.binary!r} runs {inferred.id}, but {adapter.id} was selected"
        )
    effective_env = {**os.environ, **profile.env}
    return ResolvedAgent(
        adapter,
        chosen_binary,
        source,
        profile,
        config_home(adapter, Path.home(), effective_env),
    )


def use(name: str, *, project: bool = False, cwd: Path | None = None) -> str:
    get_adapter(name)
    if project:
        target = orchestrator.team_project(cwd)
        with store_session() as store:
            store.ensure_project(target)
            store.set_meta(f"coding-agent:{target.id}", name)
        return target.id
    config = load_config()
    config.agents.default = name
    save_config(config)
    return "user"


def model_for(
    selected: ResolvedAgent,
    role: str,
    *,
    probe: bool | None = None,
    refresh: bool = False,
    effort: str | None = None,
) -> harness.ModelResolution | None:
    return selected.adapter.resolve_model(
        role,
        binary=selected.binary.binary,
        env={**os.environ, **selected.profile.env},
        probe=probe,
        refresh=refresh,
        effort=effort,
    )


def mcp_args(selected: ResolvedAgent) -> list[str]:
    import sys

    try:
        enabled = load_config().agents.mcp
    except Exception:
        return []
    if not enabled:
        return []
    return selected.adapter.mcp_args(
        sys.executable,
        ["-m", "aisquare", "serve", "--stdio", "--close-after", "0"],
        sorted(
            {
                "AISQUARE_HOME",
                "AISQUARE_TEAM_HUB",
                "AISQUARE_ROLE",
                ACTIVE_AGENT_ENV,
                "AISQUARE_LAUNCH_ID",
                "AISQUARE_FLEET_AGENT",
                "AISQUARE_PIPELINE_ID",
                "AISQUARE_TRACE_AGENT_NAME",
                *(
                    key
                    for key in {**os.environ, **selected.profile.env}
                    if key.startswith("AISQUARE_")
                ),
            }
        ),
    )


def native_model_args(selected: ResolvedAgent, role: str, raw_args: list[str]) -> list[str]:
    from aisquare.core.agent_adapters.types import has_option

    if selected.adapter.capabilities.model_ladders:
        return []  # plain launch has always left Claude model choice to its CLI
    resolution = model_for(selected, role, probe=False)
    if resolution is None:
        return []
    model = None if has_option(raw_args, "--model", "-m") else resolution.model or None
    return selected.adapter.model_args(model, resolution.effort or None)


def telemetry_args(
    selected: ResolvedAgent,
    env: dict[str, str],
    raw_args: list[str] | None = None,
) -> tuple[list[str], str]:
    from aisquare.services import explainability, native_telemetry

    try:
        settings = load_config().explainability
        if not selected.adapter.capabilities.model_proxy:
            explainability.disown_inherited_trace(env)
        if selected.adapter.id != "codex":
            return [], ""
        # A child must never keep a Claude parent's model/run identity, even
        # when the child's own telemetry is disabled.
        if not settings.enabled:
            return [], ""
        if not settings.ship:
            return (
                [],
                "Codex model tracing requires configured insight shipping; native auth unchanged",
            )
        if native_telemetry.operator_configured(selected.config_dir, raw_args or []):
            return (
                [],
                "Codex keeps your configured exporter; AISquare native tracing is off",
            )
        return native_telemetry.start(env)
    except Exception as exc:
        return [], f"Codex launched without native telemetry: {exc}"
