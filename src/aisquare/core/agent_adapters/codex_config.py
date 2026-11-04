"""Codex's config layers, as aisquare reads them for a launch, and its OpenTelemetry.

The one reader of the layers a Codex launch may read (:func:`layers`: the system's and the
home's ``config.toml``, the ``-p`` profile's file, a trusted project's ``.codex``, the
administrator's managed config over them; :func:`read_layer`, lenient or strict), which the
adapter (``codex.py``) asks too. Over them and its ``-c`` overrides: whether the operator
set a key (:func:`operator_sets`: the fleet's ``-c`` settings stand down before one), whether
they export OpenTelemetry already (:func:`operator_configured`) and what they keep out of
the shells Codex's tools run in (:func:`tool_shell_excludes`). And :class:`CodexTelemetry`,
the adapter's ``telemetry_reader``: those, and the attributes of its OTLP log records the
receiver keeps (``services.native_telemetry``).
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import tomllib
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aisquare.core.agent_adapters.types import HookTrustError, option_values

#: The system layer of Codex's config, under every user's: 0.162.1 reads it (in its binary,
#: review r14 of #113, finding 9).
SYSTEM_CONFIG = Path("/etc/codex/config.toml")
#: The managed layer an administrator sets over every user's config, also in 0.162.1's
#: binary (review r15 of #113, finding 4), under the launch's ``-c`` overrides alone. Its
#: ``requirements.toml`` beside it only narrows what may be chosen, and its macOS MDM and
#: cloud-managed layers are not files.
MANAGED_CONFIG = Path("/etc/codex/managed_config.toml")


class NativeConfigError(ValueError):
    """A config layer could contain operator settings we cannot safely inspect."""


def read_layer(path: Path, layer: str = "", *, strict: bool = False) -> dict[str, Any]:
    """``path``'s TOML table, ``{}`` for one that is absent. One that cannot be read (not a
    regular file, which a FIFO would block the read of; unreadable; not TOML; under a home
    that is a file) is ``{}`` too, or, ``strict``, :class:`NativeConfigError` naming it as
    Codex's ``layer`` config: what a check that must stand down before an operator's
    setting it cannot see asks (:func:`operator_sets`, :func:`operator_configured`)."""
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
    if not strict:
        return {}
    raise NativeConfigError(
        f"Cannot inspect Codex {layer} config {path}: {problem}. "
        "Native telemetry configuration left unchanged."
    ) from cause


def _plain_profile(profile: object) -> str | None:
    """``profile`` where it can name a profile file beside the home (``<name>.config.toml``):
    a plain file name, no NUL in it; else ``None``."""
    if (
        not isinstance(profile, str)
        or not profile
        or "\x00" in profile
        or Path(profile).name != profile
        or profile in {".", ".."}
    ):
        return None
    return profile


def selected_profile(args: Sequence[str]) -> str | None:
    """The profile ``-p``/``--profile`` in ``args`` selects (the last), a plain name; else
    ``None``. Measured on 0.162.1, only ``-p`` selects a profile file."""
    return _plain_profile(next(reversed(option_values(args, "-p", "--profile")), None))


def layers(
    home: Path, args: Sequence[str], cwd: Path | None = None, *, strict: bool = False
) -> list[dict[str, Any]]:
    """The config tables Codex layers for a session launched with ``args`` from ``home``,
    lowest first: :data:`SYSTEM_CONFIG`, ``config.toml`` in ``home``, the profile's file
    beside it (:func:`selected_profile`), with ``cwd`` a trusted
    project's own (:func:`project_files`) over them, and the administrator's
    :data:`MANAGED_CONFIG` over every file. The ``-c`` overrides in ``args`` are over
    them all and are not read here. Each read as :func:`read_layer` reads it, ``strict``
    or not. Measured on 0.162.1 and 0.164.0: a trusted project's
    ``developer_instructions`` won over ``config.toml``'s and the ``-p`` profile's, and the
    folder nearest ``cwd`` over its root's."""
    profile = selected_profile(args)
    files = [("system", SYSTEM_CONFIG), ("user", home / "config.toml")]
    if profile:
        files.append((f"profile {profile!r}", home / f"{profile}.config.toml"))
    files.append(("managed", MANAGED_CONFIG))
    *below, managed = [read_layer(path, layer, strict=strict) for layer, path in files]
    if cwd is None:
        return [*below, managed]
    project = [
        read_layer(path, "project", strict=strict)
        for path in project_files([*below, managed], args, cwd)
    ]
    return [*below, *project, managed]


def project_files(trust: Sequence[Mapping[str, Any]], args: Sequence[str], cwd: Path) -> list[Path]:
    """The project's own config files Codex layers for a session in ``cwd`` (``-C``/``--cd``
    in ``args`` moved from it), outermost first, as measured on 0.162.1: only for a project
    ``trust`` (its other layers) says ``trust_level = "trusted"`` of, by the real path of its
    root; in a git checkout ``.codex/config.toml`` in its root and in every folder from
    there down to ``cwd`` (none above the root, none for a trusted ``cwd`` under an
    untrusted root), outside one in ``cwd`` alone (none in its parents)."""
    for value in option_values(args, "-C", "--cd"):
        cwd = cwd / value
    here = Path(os.path.realpath(cwd))
    walk = [here, *here.parents]
    root = next((folder for folder in walk if (folder / ".git").exists()), here)
    trusted = False
    for table in trust:
        projects = table.get("projects")
        entry = projects.get(str(root)) if isinstance(projects, dict) else None
        level = entry.get("trust_level") if isinstance(entry, dict) else None
        if isinstance(level, str):
            trusted = level == "trusted"
    if not trusted:
        return []
    return [folder / ".codex" / "config.toml" for folder in reversed(walk[: walk.index(root) + 1])]


def sets(table: object, key: str) -> bool:
    """Whether ``table`` sets the dotted ``key``, whatever the value."""
    for part in key.split("."):
        if not isinstance(table, dict) or part not in table:
            return False
        table = table[part]
    return True


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
    """The config layers ``config_dir``'s launch may read (:func:`layers`, read strictly:
    the system, user and selected profile's config, the administrator's managed config,
    and ``overrides``), with the profile that selects; a reader asks them all, whatever
    their order. Measured on 0.162.1, only ``-p`` selects a profile file: a ``profile``
    key in config.toml loads none, ``-c profile=`` and a legacy ``[profiles.<p>]`` table
    beside ``-p`` refuse the launch, so reading them too only ever reads more."""
    configs = [*layers(config_dir, args, strict=True), *overrides]
    named = next((config["profile"] for config in reversed(configs) if "profile" in config), None)
    profile = selected_profile(args) or _plain_profile(named)
    if profile and profile != selected_profile(args):
        configs.append(
            read_layer(config_dir / f"{profile}.config.toml", f"profile {profile!r}", strict=True)
        )
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
    """Preserve exporters in native user/system config, the selected profile and the
    administrator's managed config (:func:`_layers`).

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
    ``args``, the system or user config, the selected profile's file or legacy table, or
    the administrator's managed config (:func:`_layers`), whatever the value. Raises
    :class:`NativeConfigError` for a layer that cannot be read."""
    overrides = [parsed for _, parsed in _overrides(args)]
    configs, profile = _layers(config_dir, args, overrides)
    return any(sets(table, key) for config in configs for table in _selected(config, profile))


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
            configs.append(read_layer(directory / ".codex" / "config.toml", "project", strict=True))
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


# --- the trust of a hooks.json, as Codex records a human's yes -------------------------------
#
# Codex runs a hook in a ``hooks.json`` of its home only once that hook is trusted, and 0.164.0
# and 0.166.0 ask at their start, in a "Hooks need review · N hooks are new or changed" dialog
# that holds a fleet window until a human answers it (SMOKE12 O3 of #113). The answer "Trust
# all and continue" writes one table per hook into the home's ``config.toml``:
#
#     [hooks.state."<the hooks.json's path>:<event in snake case>:<group>:<handler>"]
#     trusted_hash = "sha256:<hex>"
#
# where the hash is Codex's of the hook as it normalizes it (``hooks/src/engine/discovery.rs``
# ``hook_hash``, through ``config/src/fingerprint.rs`` ``version_for_toml``, the same code in
# 0.164.0 and 0.166.0): sha256 over the compact JSON, keys sorted, of ``{"event_name": <event in
# snake case>, "matcher": <the group's, for an event that takes one>, "hooks": [<the handler,
# its timeout defaulted and clamped as Codex runs it, its ``async`` given>]}``. Codex reads
# that state from the user's ``config.toml`` and the launch's ``-c`` alone (``hook_states_from_
# stack``: no project, plugin or managed layer may write it), and a hook whose hash differs is
# "changed" and asked about again. :func:`hook_hash` is that hash, held to the hashes 0.164.0
# and 0.166.0 wrote (``tests/fixtures/codex_hook_trust-<version>.json``), and
# :func:`sync_hook_trust` writes the tables, each where Codex's own write puts it, for the
# handlers ``core.agents.install_hooks`` wrote and no other. It keeps a record of the tables it
# wrote (:data:`TRUST_RECORD`), so it takes back exactly those and never writes over, nor
# takes back, a table of the human's (SMOKE12 O3 reopen, seq 10437).

#: Codex's hook events (``HookEventName``) that read a group's ``matcher``; the rest run every
#: group's hooks whatever it says, and hash none (``events/common.rs``
#: ``matcher_pattern_for_event``).
_MATCHED_EVENTS = frozenset(
    {
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "SessionStart",
        "SessionEnd",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "PostCompact",
    }
)
_UNMATCHED_EVENTS = frozenset(
    {"PreModelRequest", "PostModelRequest", "UserPromptSubmit", "Stop", "Interrupt"}
)
#: The events whose timeout defaults to one second and is clamped to three
#: (``normalize_command_hook``); every other one defaults to 600 seconds.
_SHORT_EVENTS = frozenset({"SessionEnd", "Interrupt"})
#: The keys of a command handler :func:`hook_hash` knows Codex's normalization of; a handler
#: with another (``commandWindows``, ``additionalContextLimit``) is hashed by none here.
_HASHED_KEYS = frozenset({"type", "command", "timeout", "async", "statusMessage"})


def hook_event_key(event: str) -> str:
    """``PreToolUse`` as a ``[hooks.state]`` key spells it: ``pre_tool_use``."""
    return "".join(
        f"_{c.lower()}" if c.isupper() and at else c.lower() for at, c in enumerate(event)
    )


def hook_key(hooks_file: Path | str, event: str, group: int, handler: int) -> str:
    """The ``[hooks.state]`` key of ``hooks_file``'s ``handler``-th hook in its ``group``-th
    group for ``event`` (``hook_key``): the file by the path Codex found it at."""
    return f"{hooks_file}:{hook_event_key(event)}:{group}:{handler}"


def hook_hash(event: str, group: Mapping[str, Any], handler: Mapping[str, Any]) -> str | None:
    """The ``trusted_hash`` Codex records for ``handler`` of ``group`` under ``event``, else
    ``None`` for a hook this does not know how Codex hashes (not a command, a key past
    :data:`_HASHED_KEYS`, a value of another type, an event Codex does not run), which is left
    to Codex to ask about."""
    if event not in _MATCHED_EVENTS | _UNMATCHED_EVENTS or handler.get("type") != "command":
        return None
    if not set(handler) <= _HASHED_KEYS:
        return None
    command, timeout = handler.get("command"), handler.get("timeout")
    runs_async, status = handler.get("async", False), handler.get("statusMessage")
    if not isinstance(command, str) or not command.strip() or not isinstance(runs_async, bool):
        return None
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int)):
        return None
    if (timeout is not None and timeout < 0) or (
        status is not None and not isinstance(status, str)
    ):
        return None
    if event in _SHORT_EVENTS:
        seconds = min(max(1 if timeout is None else timeout, 1), 3)
    else:
        seconds = max(600 if timeout is None else timeout, 1)
    normalized: dict[str, Any] = {
        "type": "command",
        "command": command,
        "timeout": seconds,
        "async": runs_async,
    }
    if status is not None:
        normalized["statusMessage"] = status
    identity: dict[str, Any] = {"event_name": hook_event_key(event), "hooks": [normalized]}
    matcher = group.get("matcher") if event in _MATCHED_EVENTS else None
    if matcher is not None:
        if not isinstance(matcher, str):
            return None
        identity["matcher"] = matcher
    import hashlib

    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def hook_states(home: Path) -> dict[str, Any]:
    """``[hooks.state]`` of ``home``'s ``config.toml``: ``{}`` with none, or one that cannot be
    read (:func:`read_layer`)."""
    hooks = read_layer(home / "config.toml").get("hooks")
    states = hooks.get("state") if isinstance(hooks, dict) else None
    return states if isinstance(states, dict) else {}


#: aisquare's record, beside a Codex home's ``config.toml``, of the ``[hooks.state]`` tables it
#: wrote there and the handlers it wrote them for: a table is aisquare's to replace or take
#: back only while it holds the hash this record says aisquare wrote into it.
TRUST_RECORD = ".aisquare-hooks-trust.json"


@dataclass(frozen=True)
class TrustPlace:
    """One handler ``install_hooks`` wrote: its place (``<event>:<group>:<handler>``), the
    hash Codex gives it (:func:`hook_hash`; ``None`` when not known here, which leaves it to
    Codex's question) and its ``[hooks.state]`` key by each path Codex may find the file at."""

    place: str
    digest: str | None
    keys: tuple[str, ...]


def sync_hook_trust(home: Path, places: Sequence[TrustPlace]) -> list[str]:
    """Make aisquare's ``[hooks.state]`` tables in ``home``'s ``config.toml`` exactly those of
    ``places``; the keys written or taken back, ``[]`` when each was as it should be.

    A key with no table gets ``[hooks.state."<key>"]`` / ``trusted_hash = "<hash>"``, APPENDED
    after a blank line, as Codex appends one: every byte before it kept. A table that holds
    the hash already is aisquare's from then on (the human's "Trust all" for aisquare's own
    hook, before aisquare recorded any). A table that holds a hash aisquare wrote there
    (:data:`TRUST_RECORD`) has that line replaced in place, any comment after it and any
    other key of the table (``enabled``, the human's off switch) kept; one that holds any
    other hash, or none, is the human's and left alone, its hook to Codex's question. A table
    aisquare wrote for a handler no longer among ``places`` is taken back, the table where its
    ``trusted_hash`` was all it held, else that line; one the human has written over since is
    left. ``()`` takes back every one (``agents disconnect``, uninstall).

    The result is parsed before it is written and must be the file as it was with these
    changes alone. It is written as ``core.agents`` writes a hooks file (a file this user may
    not write refused, one with a second name written in place), and made owner-only where
    there is none. :class:`HookTrustError` says why the file was left as it was: not TOML, a
    ``hooks`` or ``state`` that is no table, an entry that is no table, a table of a key spelt
    some other way (inline, dotted), a file that could not be written."""
    config = Path(os.path.realpath(home / "config.toml"))
    try:
        raw = config.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise HookTrustError(f"its config.toml cannot be read ({exc.strerror or exc})") from exc
    try:
        text = raw.decode("utf-8")
        before = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError):
        raise HookTrustError("its config.toml is not valid TOML") from None
    hooks = before.get("hooks", {})
    states = hooks.get("state", {}) if isinstance(hooks, dict) else None
    if not isinstance(states, dict):
        raise HookTrustError("its config.toml has a 'hooks' or 'hooks.state' that is not a table")
    record = _read_record(home)
    owned = dict(record["tables"])
    wanted = {key: place.digest for place in places if place.digest for key in place.keys}
    expected = copy.deepcopy(before)
    changed: list[str] = []
    for key, digest in wanted.items():
        entry = states.get(key)
        if entry is not None and not isinstance(entry, dict):
            raise HookTrustError(
                f"its config.toml has a [hooks.state] entry for {key} that is not a table"
            )
        if entry is None:
            if text and not text.endswith("\n"):
                text += "\n"
            header = f"[hooks.state.{json.dumps(key, ensure_ascii=False)}]"
            text += ("\n" if text else "") + f"{header}\ntrusted_hash = {json.dumps(digest)}\n"
            expected.setdefault("hooks", {}).setdefault("state", {})[key] = {"trusted_hash": digest}
        elif entry.get("trusted_hash") == digest:
            owned[key] = digest
            continue
        elif key in owned and entry.get("trusted_hash") == owned[key]:
            text = _set_hash(text, ("hooks", "state", key), digest)
            expected["hooks"]["state"][key]["trusted_hash"] = digest
        else:
            owned.pop(key, None)  # the human's: their hook to review
            continue
        owned[key] = digest
        changed.append(key)
    for key in [key for key in owned if key not in wanted]:
        entry = states.get(key)
        if isinstance(entry, dict) and entry.get("trusted_hash") == owned[key]:
            whole = set(entry) == {"trusted_hash"}
            text = _take_back(text, ("hooks", "state", key), whole=whole)
            if whole:
                del expected["hooks"]["state"][key]
            else:
                del expected["hooks"]["state"][key]["trusted_hash"]
            changed.append(key)
        del owned[key]
    if changed:
        try:
            after = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, RecursionError):
            raise HookTrustError("its config.toml cannot take a [hooks.state] table") from None
        if _pruned(after) != _pruned(expected):
            raise HookTrustError("its config.toml would read otherwise with the trust recorded")
        _write_hook_state(config, text, made=not raw)
    _write_record(home, {place.place: place.digest for place in places}, owned)
    return changed


def hook_trust_in_force(
    home: Path, settings: Mapping[str, Any], spelt: Sequence[Path | str]
) -> bool:
    """Whether the handlers aisquare last recorded (:data:`TRUST_RECORD`) are each still at
    its place in ``settings`` with the hash it recorded, every one known, and ``home``'s
    ``config.toml`` trusts each of them under every path in ``spelt``. Read-only."""
    record = _read_record(home)
    if not record["places"]:
        return False
    states = hook_states(home)
    hooks = settings.get("hooks")
    for place, digest in record["places"].items():
        event, _, rest = place.rpartition(":")
        event, _, group_at = event.rpartition(":")
        try:
            group = hooks[event][int(group_at)]  # type: ignore[index]
            handler = group["hooks"][int(rest)]
        except (KeyError, IndexError, TypeError, ValueError):
            return False
        if digest is None or not isinstance(group, dict) or not isinstance(handler, dict):
            return False
        if not set(group) <= {"matcher", "hooks"}:
            return False  # not the group aisquare wrote: a key past what Codex hashes
        if hook_hash(event, group, handler) != digest:
            return False
        for file in spelt:
            key = hook_key(file, event, int(group_at), int(rest))
            state = states.get(key)
            if record["tables"].get(key) != digest or not isinstance(state, dict):
                return False
            if state.get("trusted_hash") != digest:
                return False
    return True


def _read_record(home: Path) -> dict[str, dict[str, Any]]:
    """:data:`TRUST_RECORD` as ``{"places": {place: hash}, "tables": {key: hash}}``; empty
    where there is none or it cannot be read, which owns no table."""
    found: dict[str, dict[str, Any]] = {"places": {}, "tables": {}}
    try:
        data = json.loads((home / TRUST_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return found
    if not isinstance(data, dict):
        return found
    for name in ("places", "tables"):
        value = data.get(name)
        if isinstance(value, dict):
            found[name] = {
                str(key): digest
                for key, digest in value.items()
                if digest is None or isinstance(digest, str)
            }
    found["tables"] = {key: digest for key, digest in found["tables"].items() if digest}
    return found


def _write_record(home: Path, places: Mapping[str, str | None], tables: Mapping[str, str]) -> None:
    """Keep :data:`TRUST_RECORD`, or remove it once it names nothing."""
    from aisquare.core import atomic

    path = home / TRUST_RECORD
    if not places and not tables:
        with suppress(FileNotFoundError):
            path.unlink()
        return
    body = json.dumps({"places": dict(places), "tables": dict(tables)}, indent=2) + "\n"
    with suppress(OSError):
        if path.read_text(encoding="utf-8") == body:
            return
    try:
        atomic.write_replacing(path, body)
    except OSError as exc:
        raise HookTrustError(f"its {TRUST_RECORD} could not be written ({exc})") from exc


def _write_hook_state(config: Path, text: str, *, made: bool) -> None:
    """``text`` over ``config`` as ``core.agents`` writes a hooks file; owner-only if made."""
    from aisquare.core import agents, atomic

    try:
        if made and not config.exists():
            atomic.write_replacing(config, text.encode("utf-8"), owner_only=True)
        else:
            agents.write_settings_text(config, text)
    except (OSError, ValueError) as exc:  # AgentSettingsError (not a regular file) is a ValueError
        raise HookTrustError(f"its config.toml could not be written ({exc})") from exc


def _pruned(table: Mapping[str, Any]) -> dict[str, Any]:
    """``table`` without an empty ``hooks.state``, or a ``hooks`` emptied by that."""
    out = copy.deepcopy(dict(table))
    hooks = out.get("hooks")
    if isinstance(hooks, dict):
        if hooks.get("state") == {}:
            del hooks["state"]
        if not hooks:
            del out["hooks"]
    return out


def _header(line: str) -> tuple[str, ...] | None:
    """The table path a ``[a."b"]`` header line opens, else ``None`` (an array of tables, a
    key, a value's line)."""
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    try:
        parsed = tomllib.loads(stripped)
    except (tomllib.TOMLDecodeError, RecursionError):
        return None
    path: list[str] = []
    node: Any = parsed
    while isinstance(node, dict) and len(node) == 1:
        ((name, node),) = node.items()
        path.append(name)
    return tuple(path) if node == {} else None


def _table(lines: list[str], path: tuple[str, ...]) -> tuple[int, int]:
    """The header line of table ``path`` and the line after its last key: its own lines,
    the comments and blank lines in front of the next header not among them.
    :class:`HookTrustError` when no header opens it (the table spelt inline or dotted)."""
    start = next((at for at, each in enumerate(lines) if _header(each) == path), None)
    if start is None:
        raise HookTrustError(
            f"its config.toml spells [hooks.state] for {path[-1]} in a way not edited here"
        )
    end = next(
        (at for at in range(start + 1, len(lines)) if _header(lines[at]) is not None), len(lines)
    )
    while end > start + 1 and (
        not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1
    return start, end


#: A ``trusted_hash`` line: its key and ``=``, its value (a basic or literal string), and what
#: follows it (spaces, a comment), which a replaced line keeps.
_HASH_LINE = re.compile(
    r"""^(?P<key>\s*trusted_hash\s*=\s*)(?P<value>"(?:[^"\\]|\\.)*"|'[^']*')(?P<rest>[^\n]*)(?P<end>\n?)$"""
)


def _hash_line(lines: list[str], start: int, end: int) -> int | None:
    """The index of the ``trusted_hash`` line in ``lines[start + 1:end]``, else ``None``."""
    for at in range(start + 1, end):
        with suppress(tomllib.TOMLDecodeError, RecursionError):
            if set(tomllib.loads(lines[at].strip())) == {"trusted_hash"}:
                return at
    return None


def _set_hash(text: str, path: tuple[str, ...], digest: str) -> str:
    """``text`` with the ``trusted_hash`` of table ``path`` set to ``digest``: its line's value
    replaced, what follows it kept, else a line put under the header."""
    lines = text.splitlines(keepends=True)
    start, end = _table(lines, path)
    at = _hash_line(lines, start, end)
    if at is None:
        if not lines[start].endswith("\n"):
            lines[start] += "\n"
        lines.insert(start + 1, f"trusted_hash = {json.dumps(digest)}\n")
        return "".join(lines)
    match = _HASH_LINE.match(lines[at])
    if match is None:
        raise HookTrustError(f"its config.toml spells the trusted_hash of {path[-1]} otherwise")
    lines[at] = f"{match['key']}{json.dumps(digest)}{match['rest']}{match['end']}"
    return "".join(lines)


def _take_back(text: str, path: tuple[str, ...], *, whole: bool) -> str:
    """``text`` without table ``path`` (``whole``: its header and its own lines, and the blank
    line before it that its append added), else without its ``trusted_hash`` line."""
    lines = text.splitlines(keepends=True)
    start, end = _table(lines, path)
    if not whole:
        at = _hash_line(lines, start, end)
        if at is not None:
            del lines[at]
        return "".join(lines)
    if start > 0 and not lines[start - 1].strip():
        start -= 1
    del lines[start:end]
    return "".join(lines)
