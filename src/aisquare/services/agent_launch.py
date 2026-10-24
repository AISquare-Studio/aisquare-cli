"""Shared agent/profile selection, before native command or model resolution."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aisquare.core import agents, harness, orchestrator, paths, selfcli
from aisquare.core.agent_adapters import (
    DEFAULT_AGENT,
    adapter_for_binary,
    default_adapter,
    get_adapter,
)
from aisquare.core.agent_adapters.types import (
    AgentAdapter,
    BadEffortError,
    LaunchPart,
    merged_settings,
)
from aisquare.core.config import AppConfig, config_snapshot, load_config, save_config
from aisquare.core.spawn import LAUNCH_AGENT_ENV as LAUNCH_AGENT_ENV
from aisquare.core.spawn import LAUNCH_PID_ENV
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
        from aisquare.services.install_route import command_line

        # A line to paste, quoted for this shell (cmd.exe or PowerShell on Windows).
        self.fix = command_line(
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
    from aisquare.services import explainability

    fleet = env.get("AISQUARE_FLEET_AGENT") if not env.get("AISQUARE_LAUNCH_ID") else None
    parent_run = explainability.disown_inherited_trace(env)
    if fleet:
        env["AISQUARE_FLEET_AGENT"] = fleet
    env["AISQUARE_LAUNCH_ID"] = str(uuid.uuid4())
    env[LAUNCH_AGENT_ENV] = selected.adapter.id
    # The agent is exec'd in this process, so its pid is this one: what tells an agent
    # that hands its hooks its own pid (Claude Code's CLAUDE_PID) from a child of it.
    env[LAUNCH_PID_ENV] = str(os.getpid())
    # The agent started here is its own process, not one a tool started: the variable
    # its kind marks its tools' shells with (Codex's CODEX_THREAD_ID), inherited from
    # wherever this runs — a window of a tmux server a Codex tool started — would make
    # its own hooks read as a nested child's (`agent_sessions.own_process`).
    # The rest those shells get go too, the spawner's session's and sandbox's, never this
    # agent's (`tool_shell_vars`: Codex's CODEX_SESSION_ID, CODEX_CI, …).
    capabilities = selected.adapter.capabilities
    for name in (capabilities.tool_shell_env, *capabilities.tool_shell_vars):
        if name is not None:
            env.pop(name, None)
    # Claude Code's CLAUDE_CONFIG_DIR stays as the shell or the binding wrote it.
    if selected.adapter.capabilities.exports_home_env:
        env[selected.adapter.home_env] = str(selected.config_dir)
    if hub is not None:
        env.setdefault("AISQUARE_TEAM_HUB", str(hub))
    return parent_run


def start_broker(
    selected: ResolvedAgent, env: dict[str, str], argv: list[str]
) -> tuple[list[str], str]:
    """``argv`` with this launch's broker inbox in its sandbox's profile and the broker
    started outside it, for an agent that sandboxes its tools (``services.store_broker.
    start``); a line to print when it could not be. A broker that does not start costs
    the agent its board calls from the sandbox, which then say so, never the launch. Raised
    as ``store_broker.LaunchRefused``: the aisquare its hooks and broker run lives where its
    sandbox may write (review r11 of #113, finding 2), and any other failure to work out
    the launch's store profile and what its sandbox may write, which launched the agent
    with none of it, no store read-only, no launch inputs held, no install check (r14,
    finding 7): refused, as an agent that sandboxes its tools never starts unprotected."""
    from aisquare.services import store_broker

    try:
        return store_broker.start_for_launch(env, selected.adapter, argv)
    except store_broker.LaunchRefused:
        raise
    except Exception as exc:
        raise store_broker.LaunchRefused(
            f"{selected.adapter.label}: its launch could not work out what its sandbox may "
            f"write ({type(exc).__name__}: {exc}), so it is not started without the store's "
            "protection; run `aisquare doctor`"
        ) from exc


def prepare_sandbox(selected: ResolvedAgent) -> None:
    """Before an agent that sandboxes its tools is exec'd: make the store its profile
    names (``types.prepare_store``), which its sandbox would otherwise leave an empty
    file in place of, and take the fleet's census of the rows spawned before launch
    records were kept (``services.fleet.take_census``) if it was not. Nothing in a
    sandbox can write the store (``types.store_read_only``), so the census is taken from
    rows no sandbox wrote whenever it is taken."""
    from aisquare.core.agent_adapters.types import prepare_store, store_dir

    if selected.adapter.capabilities.sandboxes_tools:
        prepare_store(store_dir())
        from aisquare.services import fleet

        fleet.take_census()


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
        and adapter.id != default_adapter().id
        and not (bound and bound.agent == adapter.id)
    ):
        # --agent declares the family of a --bin on the same command only; a binding's
        # unnamed executable is what it always was, the default agent's (claude2).
        raise _undeclared_binding(chosen_binary, adapter, role)
    elif (
        inferred is None
        and source not in {"flag", "role", "default"}
        # A user default of the default agent names what an unnamed binary always was.
        and not (source == "user" and adapter.id == default_adapter().id)
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
    from aisquare.services.install_route import command_line

    where = {
        "env": harness._bin_env_var(role),
        "env:global": harness._BIN_ENV_GLOBAL,
    }.get(chosen.source, f"team.profiles.{role}.bin")
    declare = command_line(
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


def mcp_args(selected: ResolvedAgent) -> LaunchPart:
    """AISquare's MCP server for this launch (``[agents] mcp``), as the agent takes it."""
    try:
        enabled = load_config().agents.mcp
    except Exception:
        return LaunchPart()
    if not enabled:
        return LaunchPart()
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
                    if key.startswith("AISQUARE_") and key != LAUNCH_SETTINGS_ENV
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


def _launch_home(selected: ResolvedAgent, env: Mapping[str, str]) -> Path:
    """The home this launch's environment names: an account's (``apply_launch_env``) over
    the binding's or the shell's, which ``selected`` was resolved with."""
    adapter = selected.adapter
    if env.get(adapter.home_env, "").strip():
        return adapter.config_dir(agents._home(), env)
    return selected.config_dir


def fleet_settings(
    selected: ResolvedAgent, env: Mapping[str, str], raw_args: list[str]
) -> LaunchPart:
    """What the agent of a fleet window launches with to keep a dialog of its own out of the
    pane the fleet types into (``AgentAdapter.fleet_settings``, from this launch's home);
    nothing outside a fleet window (``AISQUARE_FLEET_AGENT``), where the human answers the
    agent's dialogs."""
    if not env.get(orchestrator.FLEET_AGENT_ENV_VAR, "").strip():
        return LaunchPart()
    return selected.adapter.fleet_settings(_launch_home(selected, env), raw_args)


LAUNCH_SETTINGS_ENV = "AISQUARE_LAUNCH_SETTINGS"
"""Where a fleet spawn hands its window's ``aisquare launch`` the settings its slots answered
(``LaunchPart.settings``: the label's, the teams'), as JSON, for the one settings layer that
launch writes (:func:`compose`); never in the agent's environment."""


def carry_settings(env: dict[str, str], part: LaunchPart) -> None:
    """Hand ``part``'s environment, and its settings (:data:`LAUNCH_SETTINGS_ENV`), to the
    window ``env`` starts: what a fleet spawn's own slots answer beside their arguments."""
    env.update(part.env)
    if part.settings:
        env[LAUNCH_SETTINGS_ENV] = json.dumps(
            merged_settings(_carried(env.get(LAUNCH_SETTINGS_ENV)), part.settings)
        )


def _carried(raw: str | None) -> dict[str, Any]:
    try:
        carried = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return carried if isinstance(carried, dict) else {}


def compose(selected: ResolvedAgent, env: dict[str, str], *parts: LaunchPart) -> list[str]:
    """The arguments ``parts`` add to this launch, in order, with their environment applied to
    ``env``, and their settings and the spawn's (:data:`LAUNCH_SETTINGS_ENV`, taken out of
    ``env``) given as the agent's one settings layer of this launch
    (``AgentAdapter.settings_layer``: a file under the cache named for the launch, for an
    agent whose layer is a file). ``ValueError`` for settings answered for an agent with no
    layer, which the conformance suite refuses an adapter."""
    args: list[str] = []
    for part in parts:
        args += part.args
        env.update(part.env)
    settings = merged_settings(
        _carried(env.pop(LAUNCH_SETTINGS_ENV, None)), *(part.settings for part in parts)
    )
    if settings:
        launch = env.get("AISQUARE_LAUNCH_ID") or uuid.uuid4().hex
        file = paths.cache_dir() / "launch-settings" / f"{launch}.json"
        file.parent.mkdir(parents=True, exist_ok=True)
        _prune_settings(file.parent)
        layer = selected.adapter.settings_layer(settings, file)
        if layer is None:
            raise ValueError(f"{selected.adapter.label} has no settings layer for {settings}")
        args += layer.args
        env.update(layer.env)
    return args


_SETTINGS_KEPT_SECONDS = 7 * 24 * 3600


def _prune_settings(directory: Path) -> None:
    """Drop the settings layers of launches a week old: a running agent read its own at its
    start, and one restarted later writes it again."""
    import time

    old = time.time() - _SETTINGS_KEPT_SECONDS
    for stale in directory.glob("*.json"):
        with suppress(OSError):
            if stale.stat().st_mtime < old:
                stale.unlink()


def telemetry_args(
    selected: ResolvedAgent,
    env: dict[str, str],
    raw_args: list[str] | None = None,
) -> tuple[LaunchPart, str]:
    """The agent's native telemetry for this launch (``AgentAdapter.telemetry_args``, its
    per-launch receiver started), and the note that says what came of it."""
    from aisquare.services import native_telemetry

    native_telemetry.disown_headers(env)
    adapter = selected.adapter
    reader = adapter.telemetry_reader
    if not adapter.capabilities.native_telemetry or reader is None:
        return LaunchPart(), ""
    home = _launch_home(selected, env)
    cwd = Path.cwd()
    try:
        settings = load_config().explainability
        if not settings.enabled:
            return LaunchPart(), ""
        if not settings.ship:
            return (
                LaunchPart(),
                f"{adapter.label} model tracing requires configured insight shipping; "
                "native auth unchanged",
            )
        if reader.operator_configured(home, raw_args or [], cwd):
            return (
                LaunchPart(),
                f"{adapter.label} keeps your configured exporter; AISquare native tracing is off",
            )
        if env.get(native_telemetry.HEADERS_ENV):
            # The operator's own (ours was disowned above): the receiver's would replace
            # it in the agent's environment, and so in every process its tools start.
            return (
                LaunchPart(),
                f"{adapter.label} keeps your {native_telemetry.HEADERS_ENV}; "
                "AISquare native tracing is off",
            )
        excluded = reader.tool_shell_excludes(home, raw_args or [], cwd)
        if excluded is None:
            return (
                LaunchPart(),
                f"{adapter.label} keeps your {reader.tool_shell_policy}, which "
                "would hand AISquare's tracing token to every tool; native tracing is off",
            )
        return native_telemetry.start_receiver(env, adapter, excluded)
    except Exception as exc:
        return LaunchPart(), f"{adapter.label} launched without native telemetry: {exc}"
