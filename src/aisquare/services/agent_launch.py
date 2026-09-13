"""Shared agent/profile selection, before native command or model resolution."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from aisquare.core import agents, harness, orchestrator, paths, selfcli
from aisquare.core.agent_adapters import adapter_for_binary, get_adapter
from aisquare.core.agent_adapters.types import (
    AgentAdapter,
    BadEffortError,
    config_home,
    model_overrides,
    option_occurrences,
)
from aisquare.core.config import AppConfig, config_snapshot, load_config, save_config
from aisquare.core.spawn import LAUNCH_AGENT_ENV as LAUNCH_AGENT_ENV
from aisquare.core.store import store_session

# Operator preference; launches export their own family under LAUNCH_AGENT_ENV.
ACTIVE_AGENT_ENV = "AISQUARE_CODING_AGENT"
_PROJECT_CHOICE: ContextVar[tuple[Path, str | None] | None] = ContextVar(
    "project_agent_choice", default=None
)


@contextmanager
def selection_snapshot(cwd: Path | None = None) -> Iterator[None]:
    """Resolve a diagnostic's roles against one config and project preference."""
    root = (cwd or Path.cwd()).absolute()
    try:
        choice = project_default(root)
    except Exception:
        choice = None
    token = _PROJECT_CHOICE.set((root, choice))
    try:
        with config_snapshot(), harness.probe_snapshot():
            yield
    finally:
        _PROJECT_CHOICE.reset(token)


class UnknownWrapperError(ValueError):
    """A default names an agent, but does not identify an arbitrary executable."""

    def __init__(self, binary: str, role: str) -> None:
        import shlex

        self.fix = shlex.join(
            ["aisquare", "team", "bind", role, "--agent", "claude-code", "--bin", binary]
        )
        super().__init__(
            f"The agent family of {binary!r} is unknown. For a Claude wrapper, run: {self.fix}. "
            "For a Codex wrapper use --agent codex instead; for a Claude wrapper use "
            "--agent claude-code. "
            "Non-Claude user defaults, project defaults and operator preferences "
            "do not identify a wrapper's family."
        )


@dataclass(frozen=True)
class ResolvedAgent:
    adapter: AgentAdapter
    binary: harness.BinaryResolution
    source: str
    profile: harness.LaunchProfile
    config_dir: Path


def executable(selected: ResolvedAgent) -> str | None:
    return harness.executable_path(selected.binary.binary, {**os.environ, **selected.profile.env})


def launch_identity(env: dict[str, str], selected: ResolvedAgent, hub: Path | None) -> str | None:
    """Disown an inherited pane before giving this launch its own identity."""
    import uuid

    from aisquare.services import explainability

    fleet = env.get("AISQUARE_FLEET_AGENT") if not env.get("AISQUARE_LAUNCH_ID") else None
    parent_run = explainability.disown_inherited_trace(env)
    if fleet:
        env["AISQUARE_FLEET_AGENT"] = fleet
    env["AISQUARE_LAUNCH_ID"] = str(uuid.uuid4())
    env[LAUNCH_AGENT_ENV] = selected.adapter.id
    if selected.adapter.id != "claude-code" or selected.adapter.home_env in env:
        env[selected.adapter.home_env] = str(selected.config_dir)
    if hub is not None:
        env.setdefault("AISQUARE_TEAM_HUB", str(hub))
    return parent_run


def project_default(cwd: Path | None = None) -> str | None:
    snapshot = _PROJECT_CHOICE.get()
    if snapshot is not None and snapshot[0] == (cwd or Path.cwd()).absolute():
        return snapshot[1]
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
    inherited = os.environ.get(ACTIVE_AGENT_ENV)
    # Launch identity and operator preference have different lifetimes. A
    # parent's executable family cannot identify an arbitrary wrapper.
    owned_family = os.environ.get(LAUNCH_AGENT_ENV) if chosen_binary.source == "default" else None
    choices = (
        (agent, "flag"),
        (bound.agent if bound else None, "role"),
        (inferred.id if inferred else None, chosen_binary.source),
        (preferred, "project"),
        (owned_family, "inherited"),
        (inherited, "operator"),
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
    elif (
        inferred is None
        and source not in {"flag", "role", "default"}
        and not (source == "user" and adapter.id == "claude-code")
    ):
        raise UnknownWrapperError(chosen_binary.binary, role)
    effective_env = {**os.environ, **profile.env}
    return ResolvedAgent(
        adapter,
        chosen_binary,
        source,
        profile,
        config_home(adapter, agents._home(), effective_env),
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


@dataclass(frozen=True)
class NativeArguments:
    """One whole-argv normalization, with ownership retained for validation."""

    argv: list[str]
    model: str | None
    effort: str | None
    original_effort: str | None


def prepare_arguments(
    selected: ResolvedAgent,
    owned: list[str],
    forwarded: list[str] | None = None,
) -> NativeArguments:
    raw = [*owned, *(forwarded or [])]
    # Saved Claude flags are part of AISquare's configuration contract. The
    # final native override wins, and one-off passthrough belongs to the CLI
    # (including values introduced by newer versions or wrappers).
    if selected.adapter.id == "claude-code":
        efforts = list(option_occurrences(raw, "--effort"))
        if efforts and efforts[-1].index < len(owned):
            selected.adapter.validate(None, efforts[-1].value)
    model, original_effort = model_overrides(selected.adapter.id, raw)
    argv = selected.adapter.native_args(raw)
    effort = (
        selected.adapter.effort_alias(original_effort)
        if original_effort is not None
        and selected.adapter.id == "codex"
        and selected.adapter.effort_alias(original_effort) != original_effort.strip().lower()
        else original_effort
    )
    return NativeArguments(argv, model, effort, original_effort)


def _arguments(selected: ResolvedAgent, raw: list[str] | NativeArguments | None) -> NativeArguments:
    return raw if isinstance(raw, NativeArguments) else prepare_arguments(selected, raw or [])


def model_for(
    selected: ResolvedAgent,
    role: str,
    *,
    probe: bool | None = None,
    refresh: bool = False,
    effort: str | None = None,
    raw_args: list[str] | NativeArguments | None = None,
) -> harness.ModelResolution | None:
    if effort is not None:
        if not effort.strip():
            raise BadEffortError("--effort requires a reasoning level, not an empty value")
        selected.adapter.validate(None, effort)
    arguments = _arguments(selected, raw_args)
    model, native_effort = arguments.model, arguments.effort
    effective = {**os.environ, **selected.profile.env}
    if model is not None:
        effective[harness.role_env_key("MODEL", role)] = model
    result = selected.adapter.resolve_model(
        role,
        binary=selected.binary.binary,
        env=effective,
        probe=probe,
        refresh=refresh,
        effort=effort,
    )
    if result is None:
        return None
    if model is not None:
        # Report the actual native option value. Literal dash-prefixed prompts
        # need the native CLI's -- separator; guessing intent changes argv.
        result = result.model_copy(update={"model": model, "source": "native", "skipped": []})
    if native_effort is not None:
        notes = list(result.notes)
        original_effort = arguments.original_effort
        if original_effort is not None and native_effort != original_effort:
            notes.append(
                f"{selected.adapter.label} maps {original_effort!r} to native reasoning "
                f"effort {native_effort!r}."
            )
        if effort is not None and selected.adapter.effort_alias(effort) != native_effort:
            notes.append(
                f"Native effort {native_effort!r} takes precedence over --effort {effort!r}."
            )
        result = result.model_copy(
            update={
                "effort": native_effort.strip(),
                "effort_source": "native",
                "notes": list(dict.fromkeys(notes)),
            }
        )
    selected.adapter.validate(
        None if model is not None else result.model or None,
        None if native_effort is not None else result.effort or None,
    )
    return result


def mcp_args(selected: ResolvedAgent) -> list[str]:
    try:
        enabled = load_config().agents.mcp
    except Exception:
        return []
    if not enabled:
        return []
    command = selfcli.argv_for(["serve", "--stdio", "--close-after", "0"])
    return selected.adapter.mcp_args(
        command[0],
        command[1:],
        sorted(
            {
                "AISQUARE_HOME",
                "AISQUARE_TEAM_HUB",
                "AISQUARE_ROLE",
                ACTIVE_AGENT_ENV,
                LAUNCH_AGENT_ENV,
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


def launch_model_for(
    selected: ResolvedAgent, role: str, raw_args: list[str] | NativeArguments
) -> harness.ModelResolution | None:
    """Plain launch preserves Claude's native default; Codex honors saved pins."""
    arguments = _arguments(selected, raw_args)
    if selected.adapter.capabilities.model_ladders:
        return None
    return model_for(selected, role, probe=False, raw_args=arguments)


def resolved_model_args(
    selected: ResolvedAgent,
    resolution: harness.ModelResolution | None,
    raw_args: list[str] | NativeArguments,
) -> list[str]:
    if resolution is None:
        return []
    arguments = _arguments(selected, raw_args)
    model, effort = arguments.model, arguments.effort
    return selected.adapter.model_args(
        None if model is not None else resolution.model or None,
        None if effort is not None else resolution.effort or None,
    )


def telemetry_args(
    selected: ResolvedAgent,
    env: dict[str, str],
    raw_args: list[str] | None = None,
) -> tuple[list[str], str]:
    if selected.adapter.id != "codex":
        return [], ""
    from aisquare.services import native_telemetry

    try:
        settings = load_config().explainability
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
