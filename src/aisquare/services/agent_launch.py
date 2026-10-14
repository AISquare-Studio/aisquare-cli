"""Shared agent/profile selection, before native command or model resolution."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from aisquare.core import agents, harness, orchestrator, paths, selfcli
from aisquare.core.agent_adapters import DEFAULT_AGENT, adapter_for_binary, get_adapter
from aisquare.core.agent_adapters.types import AgentAdapter, BadEffortError
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
            ["aisquare", "team", "bind", role, "--agent", DEFAULT_AGENT, "--bin", binary]
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
    # The agent started here is its own process, not one a tool started: the variable
    # its kind marks its tools' shells with (Codex's CODEX_THREAD_ID), inherited from
    # wherever this runs — a window of a tmux server a Codex tool started — would make
    # its own hooks read as a nested child's (`agent_sessions.own_process`).
    if selected.adapter.capabilities.tool_shell_env is not None:
        env.pop(selected.adapter.capabilities.tool_shell_env, None)
    # Claude Code's CLAUDE_CONFIG_DIR stays as the shell or the binding wrote it.
    if selected.adapter.capabilities.exports_home_env:
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
        (DEFAULT_AGENT, "default"),
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
        and source == "flag"
        and chosen_binary.source != "flag"
        and adapter.id != DEFAULT_AGENT
        and not (bound and bound.agent == adapter.id)
    ):
        # --agent declares the family of a --bin on the same command only; a binding's
        # unnamed executable is what it always was, the default agent's (claude2).
        raise _undeclared_binding(chosen_binary, adapter, role)
    elif (
        inferred is None
        and source not in {"flag", "role", "default"}
        # A user default of the default agent names what an unnamed binary always was.
        and not (source == "user" and adapter.id == DEFAULT_AGENT)
    ):
        raise UnknownWrapperError(chosen_binary.binary, role)
    effective_env = {**os.environ, **profile.env}
    return ResolvedAgent(
        adapter,
        chosen_binary,
        source,
        profile,
        # The binding's or this shell's, as the agent itself reads it (Claude Code's as
        # written, as every reader names it: core.agents._claude_home).
        adapter.config_dir(agents._home(), effective_env),
    )


def _undeclared_binding(
    chosen: harness.BinaryResolution, adapter: AgentAdapter, role: str
) -> ValueError:
    import shlex

    where = {
        "env": harness._bin_env_var(role),
        "env:global": harness._BIN_ENV_GLOBAL,
    }.get(chosen.source, f"team.profiles.{role}.bin")
    declare = shlex.join(
        ["aisquare", "team", "bind", role, "--agent", adapter.id, "--bin", chosen.binary]
    )
    return ValueError(
        f"--agent {adapter.id} cannot vouch for {chosen.binary!r}: it comes from {where}, "
        f"not from this command, and its agent family is unknown. To run {adapter.label} "
        f"itself, pass its executable ({adapter.binary}) on this command; to run "
        f"{chosen.binary!r} as {adapter.label}, declare it: {declare}."
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
    # Saved flags in AISquare's own vocabulary (Claude Code's --effort) are part of
    # AISquare's configuration contract. The final native override wins, and one-off
    # passthrough belongs to the CLI (including values introduced by newer versions or
    # wrappers), as does a native config value (NativeChoice.effort_index).
    choice = selected.adapter.native_choice(raw)
    if choice.effort_index is not None and choice.effort_index < len(owned):
        selected.adapter.validate(None, choice.effort)
    model, original_effort = choice.model, choice.effort
    argv = selected.adapter.native_args(raw)
    effort = (
        selected.adapter.effort_alias(original_effort)
        if original_effort is not None
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
    selected: ResolvedAgent,
    role: str,
    raw_args: list[str] | NativeArguments,
    *,
    ladder: bool = False,
) -> harness.ModelResolution | None:
    """The model and effort a launch passes, never probing (no paid call at a launch).

    ``ladder`` is ``fleet spawn``'s, which resolves the role as ``team spawn`` does
    (``team harness`` reports it) for every agent: one with AISquare's ladders (Claude
    Code) walks its role's ladder on the cached probe verdicts, else takes its first
    rung (Claude Code runs its own default in place of a model the account lacks), under
    its pins (``AISQUARE_MODEL_<ROLE>``, ``[agents.models.<agent>]``). A hand ``aisquare
    launch`` of such an agent passes nothing, its argv as it always was; another agent
    takes its configured pins there too. What the native arguments already choose wins
    (:func:`resolved_model_args`)."""
    arguments = _arguments(selected, raw_args)
    if selected.adapter.capabilities.model_ladders and not ladder:
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
    from aisquare.services import native_telemetry

    native_telemetry.disown_headers(env)
    adapter = selected.adapter
    if not adapter.capabilities.native_telemetry:
        return [], ""
    # The home this launch's environment names: an account's (`apply_launch_env`) over
    # the binding's or the shell's, which `selected` was resolved with.
    home = (
        adapter.config_dir(agents._home(), env)
        if env.get(adapter.home_env, "").strip()
        else selected.config_dir
    )
    try:
        settings = load_config().explainability
        if not settings.enabled:
            return [], ""
        if not settings.ship:
            return (
                [],
                f"{adapter.label} model tracing requires configured insight shipping; "
                "native auth unchanged",
            )
        if native_telemetry.operator_configured(home, raw_args or []):
            return (
                [],
                f"{adapter.label} keeps your configured exporter; AISquare native tracing is off",
            )
        return native_telemetry.start_receiver(env, adapter)
    except Exception as exc:
        return [], f"{adapter.label} launched without native telemetry: {exc}"
