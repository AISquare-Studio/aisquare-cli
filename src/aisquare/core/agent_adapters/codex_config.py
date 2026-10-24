"""Codex's config layers, as aisquare reads them for a launch, and its OpenTelemetry.

The layers a Codex launch may read (the system's and the home's ``config.toml``, its ``-c``
overrides, the ``-p`` profile's file and legacy table, a trusted project's ``.codex``), asked
whether the operator set a key (:func:`operator_sets`: the fleet's ``-c`` settings stand down
before one), whether they export OpenTelemetry already (:func:`operator_configured`) and what
they keep out of the shells Codex's tools run in (:func:`tool_shell_excludes`). And
:class:`CodexTelemetry`, the adapter's ``telemetry_reader``: those, and the attributes of its
OTLP log records the receiver keeps (``services.native_telemetry``).
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from aisquare.core.agent_adapters.types import option_values

SYSTEM_CONFIG = Path("/etc/codex/config.toml")


class NativeConfigError(ValueError):
    """A config layer could contain operator settings we cannot safely inspect."""


def _read_config(path: Path, layer: str) -> dict[str, Any]:
    problem: Exception
    cause: BaseException
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("not a regular file")
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        # Windows says "not found" under a home that is a file, where POSIX says ENOTDIR.
        if not os.path.lexists(path.parent) or os.path.isdir(path.parent):
            return {}
        problem, cause = NotADirectoryError(f"{path.parent} is not a directory"), exc
    except (OSError, ValueError, RecursionError) as exc:
        problem = cause = exc
    raise NativeConfigError(
        f"Cannot inspect Codex {layer} config {path}: {problem}. "
        "Native telemetry configuration left unchanged."
    ) from cause


def _overrides(args: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Each ``-c``/``--config`` override in ``args``: its key as written, and its value parsed
    as Codex parses it; one that does not parse is Codex's to refuse, and skipped."""
    overrides: list[tuple[str, dict[str, Any]]] = []
    for value in option_values(args, "-c", "--config"):
        key, separator, raw = value.partition("=")
        if not separator:
            continue
        try:
            try:
                parsed = tomllib.loads(value)
            except (tomllib.TOMLDecodeError, RecursionError):
                # Codex accepts an unquoted string as a config override value.
                parsed = tomllib.loads(f"{key}={json.dumps(raw)}")
        except (tomllib.TOMLDecodeError, RecursionError):
            continue  # Native argument validation belongs to Codex.
        # Keep each dotted override: a shallow update would erase sibling keys
        # under profiles when a later -c configures something else there.
        overrides.append((key.strip(), parsed))
    return overrides


def _layers(
    config_dir: Path, args: list[str], overrides: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str | None]:
    """The config layers ``config_dir``'s launch may read (its system and user config,
    ``overrides`` and the selected profile's file), with the profile that selects; a
    reader asks them all, whatever their order. Measured on 0.162.1, only ``-p`` selects
    a profile file: a ``profile`` key in config.toml loads none, ``-c profile=`` and a
    legacy ``[profiles.<p>]`` table beside ``-p`` refuse the launch, so reading them
    too only ever reads more."""
    configs = [
        _read_config(SYSTEM_CONFIG, "system"),
        _read_config(config_dir / "config.toml", "user"),
        *overrides,
    ]
    profile = next((config["profile"] for config in reversed(configs) if "profile" in config), None)
    for value in option_values(args, "-p", "--profile"):
        profile = value
    if (
        not isinstance(profile, str)
        or "\x00" in profile
        or Path(profile).name != profile
        or profile in {".", ".."}
    ):
        profile = None
    if profile:
        configs.append(_read_config(config_dir / f"{profile}.config.toml", f"profile {profile!r}"))
    return configs, profile


def _selected(config: dict[str, Any], profile: str | None) -> list[dict[str, Any]]:
    """``config``, and the legacy ``[profiles.<profile>]`` table in it when one is selected."""
    legacy_profiles = config.get("profiles", {})
    if profile and isinstance(legacy_profiles, dict):
        selected = legacy_profiles.get(profile, {})
        if isinstance(selected, dict):
            return [config, selected]
    return [config]


def operator_configured(config_dir: Path, args: list[str]) -> bool:
    """Preserve exporters in native user/system config and the selected profile.

    Codex ignores `otel` in project-local config. Other account homes and
    unselected profile files are not layers of this launch either.
    """
    overrides = [parsed for _, parsed in _overrides(args)]
    if any("otel" in parsed for parsed in overrides):
        return True
    configs, profile = _layers(config_dir, args, overrides)
    return any("otel" in table for config in configs for table in _selected(config, profile))


def operator_sets(config_dir: Path, args: list[str], key: str) -> bool:
    """Whether a config layer of this launch sets the dotted ``key``: an override in
    ``args``, the system or user config, or the selected profile's file or legacy table
    (:func:`_layers`), whatever the value. Raises :class:`NativeConfigError` for a layer
    that cannot be read."""
    path = key.split(".")

    def sets(table: Any) -> bool:
        for part in path:
            if not isinstance(table, dict) or part not in table:
                return False
            table = table[part]
        return True

    overrides = [parsed for _, parsed in _overrides(args)]
    configs, profile = _layers(config_dir, args, overrides)
    return any(sets(table) for config in configs for table in _selected(config, profile))


TOOL_SHELL_POLICY = "shell_environment_policy"
"""Codex's setting for the environment the shells its tools run in inherit, whose
``exclude`` keeps :data:`HEADERS_ENV` out of them (:func:`tool_shell_excludes`)."""


def tool_shell_excludes(config_dir: Path, args: list[str], cwd: Path) -> list[str] | None:
    """The patterns the operator's config keeps out of the shells Codex's tools run in
    (``shell_environment_policy.exclude``), which the receiver's must join.

    Ours goes in with ``-c``, which replaces the list every file layer sets (measured on
    0.162.1: the user config's, the ``-p`` profile file's and the project's
    ``.codex/config.toml``), so no file the operator keeps can drop the receiver's header
    from it (review r3 of #113, finding 7). Without the operator's patterns in it, every
    variable their config kept from the tools would reach them again, so every layer this
    launch may read is asked, the project's from ``cwd`` up included, and the patterns are
    their union: a superset of the list Codex applies, so a pattern a higher layer
    dropped stays out, the safe side. ``None`` when an override of the operator's own
    sets the list (or the whole table): it comes after ours on the command line and wins,
    which would hand the receiver's header to every tool.
    """
    overrides = _overrides(args)
    for key, parsed in overrides:
        policy = parsed.get(TOOL_SHELL_POLICY)
        if key == TOOL_SHELL_POLICY or (isinstance(policy, dict) and "exclude" in policy):
            return None
    configs, profile = _layers(config_dir, args, [parsed for _, parsed in overrides])
    for value in option_values(args, "-C", "--cd"):
        cwd = cwd / value
    for directory in (cwd, *cwd.parents):
        if (directory / ".codex").is_dir():
            configs.append(_read_config(directory / ".codex" / "config.toml", "project"))
    patterns: list[str] = []
    for config in configs:
        for table in _selected(config, profile):
            policy = table.get(TOOL_SHELL_POLICY)
            excluded = policy.get("exclude") if isinstance(policy, dict) else None
            if isinstance(excluded, list):
                patterns += [item for item in excluded if isinstance(item, str)]
    return list(dict.fromkeys(patterns))


_FIELDS = frozenset(
    {
        "event.name",
        "event",
        "model",
        "model_name",
        "provider_name",
        "conversation.id",
        "thread.id",
        "turn.id",
        "tool_name",
        "tool.name",
        "input_token_count",
        "output_token_count",
        "cached_token_count",
        "cached_input_token_count",
        "reasoning_output_token_count",
        "duration_ms",
        "success",
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "tool.execution_time_ms",
        "response_id",
        "call_id",
    }
)


class CodexTelemetry:
    """Codex's native OpenTelemetry (``AgentAdapter.telemetry_reader``): its exporter is
    ``-c otel.*`` (``CodexAdapter.telemetry_args``), the operator's own in any config layer
    of the launch is kept, its tools' shells are kept from the receiver's header by
    ``shell_environment_policy.exclude``, and of each log record the receiver keeps
    :data:`_FIELDS` (measured on 0.162.1)."""

    tool_shell_policy = f"-c {TOOL_SHELL_POLICY}"

    def operator_configured(self, home: Path, args: Sequence[str], cwd: Path) -> bool:
        return operator_configured(home, list(args))

    def tool_shell_excludes(self, home: Path, args: Sequence[str], cwd: Path) -> list[str] | None:
        return tool_shell_excludes(home, list(args), cwd)

    def decode(
        self, attributes: Mapping[str, object], event: str | None
    ) -> dict[str, object] | None:
        kept = {key: value for key, value in attributes.items() if key in _FIELDS}
        kept["event.name"] = kept.get("event.name") or event or "codex.event"
        return kept
