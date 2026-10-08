"""Setup, upgrade and removal of aisquare itself.

Everything the upgrade path calls is imported at module top, and that is
load-bearing rather than tidy: ``uv tool install --force`` deletes and
recreates the environment this process was loaded from while it is still
running (measured: the directory's inode changes). Any import made after the
installer returns would load the NEW version's module into the OLD process —
or fail. So the code after the install reaches only what is already in
memory, and anything that needs the new version runs AS the new version, in a
subprocess.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath

from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import credentials as credentials_store
from aisquare.core import paths
from aisquare.core import snapshot as snapshot_core
from aisquare.core.config import (
    AppConfig,
    ExplainabilitySettings,
    load_config,
    save_config,
)
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxServer, TmuxUnavailable
from aisquare.core.version import __version__
from aisquare.core.workspace import current_project
from aisquare.models import SetupReport
from aisquare.services import agents as agents_service
from aisquare.services import explainability as explainability_service
from aisquare.services import install_route, onboarding
from aisquare.services import project as project_service


class ExplainabilityResetRefused(RuntimeError):
    """``--reinit`` would discard a configured explainability section.

    Raised rather than warned because the loss is not recoverable from anything
    on the machine: ``[explainability.targets]`` holds a gateway URL and the
    NAME of the environment variable holding the key, both configured out of
    band. Afterwards ``status`` reads as a plausible *unconfigured* machine
    rather than a broken one, so nothing downstream reports it.

    A config that cannot be read is refused too, as
    :class:`UnreadableConfigResetRefused`: what it configures cannot be seen,
    so neither can what a reset would take.
    """

    def __init__(self, summary: str) -> None:
        super().__init__(summary)
        self.summary = summary


class UnreadableConfigResetRefused(ExplainabilityResetRefused):
    """``--reinit`` would replace a ``config.toml`` it cannot read; ``summary`` is why not.

    The reset used to go ahead on such a file, on the grounds that a section
    nobody can read has nothing to protect. That held only while "cannot read"
    meant "holds nothing". A config.toml PowerShell 5.1 saved as UTF-16 holds
    everything its operator configured, and it was replaced with the defaults
    by a plain ``--reinit`` (review of the #203 final-review fixes). A file in
    an encoding this build now reads is refused like any other. One it still
    cannot read needs ``--yes``, which says the operator means to lose what
    is in it. ``doctor``'s fix for an invalid config says so, so the recovery
    it prescribes is still one command.
    """


def _configured_explainability(settings: ExplainabilitySettings) -> str | None:
    """What a reset would take, or None if there is nothing configured.

    Keys on three fields rather than one: a machine mid-cutover may have any of
    them set, and checking only ``targets`` would let a half-configured machine
    be reset in silence.
    """
    parts: list[str] = []
    if settings.targets:
        parts.append("targets " + ", ".join(sorted(settings.targets)))
    if settings.enabled:
        parts.append("tracing enabled")
    if settings.gateway_url:
        parts.append(f"gateway {settings.gateway_url}")
    return "; ".join(parts) or None


def initialize(
    path: Path | None,
    *,
    api_key: str | None,
    local: bool,
    agents: list[str],
    onboard: bool,
    reinit: bool,
    assume_yes: bool,
    explainability: bool | None = None,
) -> SetupReport:
    """Set up ``~/.aisquare``, register & snapshot the project, and connect agents.

    Idempotent and non-interactive: safe to re-run (``assume_yes`` is therefore
    moot for now). ``reinit`` resets ``config.toml`` to defaults. Agents named via
    ``--agent`` are connected (hooks installed + context ingested); cloud auth is
    not wired yet.
    """
    home = paths.aisquare_home()
    already_initialized = paths.config_path().exists() or paths.db_path().exists()
    paths.ensure_home()

    discarded: str | None = None
    unreadable: str | None = None
    if reinit and paths.config_path().exists():
        try:
            existing: ExplainabilitySettings | None = load_config().explainability
        except Exception as exc:
            # Nothing can say what it configures, so the reset needs consent
            # (UnreadableConfigResetRefused).
            existing, unreadable = None, str(exc)
            if not assume_yes:
                raise UnreadableConfigResetRefused(unreadable) from exc
        if existing is not None:
            discarded = _configured_explainability(existing)
            if discarded and not assume_yes:
                raise ExplainabilityResetRefused(discarded)

    if reinit or not paths.config_path().exists():
        save_config(AppConfig(), discard_unreadable=unreadable is not None)

    project = current_project(path)
    with store_session() as store:
        store.onboard_project(project)  # init is the deliberate add (#139)

    notes: list[str] = []
    if unreadable is not None:
        notes.append(f"reset replaced a config.toml it could not read ({unreadable})")
    if discarded:
        # Consent was given, so the reset happened — but say what went, because
        # nothing downstream reports a missing targets table.
        notes.append(f"reset discarded the configured explainability section ({discarded})")
    if api_key:
        # Merged rather than replaced: `serve` keeps its bearer token in the same
        # file, and a whole-file write erased it (and, in the other order, this
        # key). One helper owns the format so the two cannot diverge again --
        # and reports whether the file could really be locked to this account,
        # because on NTFS the 0600 that guarded it is a no-op.
        _, restricted = credentials_store.store(**{credentials_store.API_KEY: api_key})
        # One string with one branch, rather than two near-copies sharing a
        # prefix: the two paths cannot drift into saying different things about
        # where the key landed.
        notes.append(
            "Stored API key in ~/.aisquare/credentials"
            + (
                "."
                if restricted
                else " — but could NOT restrict it to your account; other users on this "
                "machine may be able to read it."
            )
        )
    elif not local:
        notes.append(
            "No API key given — running local-only; re-run with --api-key to connect later."
        )

    notes.extend(_explainability_step(explainability))

    onboarded = 0
    if onboard:
        report = project_service.onboard(path, refresh=False)
        onboarded = len(report.seeded)
        if report.snapshot is not None and report.snapshot.status == "ready":
            notes.append(
                f"Snapshot: {report.snapshot.file_count} files, "
                f"{report.snapshot.token_count} tokens packed for fast agent context."
            )
        elif report.snapshot is None:
            # Off (no Node, or no packer: optional, not a fault) or a pack that
            # failed and why -- the same sentence `project onboard` prints, from one place.
            notes.append(f"Snapshot: {report.snapshot_note or snapshot_core.skipped_detail()}.")

    for agent in agents:
        try:
            connection = agents_service.connect(agent)
        except (KeyError, ValueError) as exc:
            notes.append(f"Could not connect {agent}: {exc}")
            continue
        # Always installed: `connect` refuses rather than return a connection without hooks.
        if connection.hooks_off is not None:
            notes.append(
                f"Installed {agent}'s hooks and imported {connection.imported} entries, but "
                f'{connection.hooks_off} sets "disableAllHooks": true, so they will not run '
                "until you remove that key."
            )
            continue
        notes.append(f"Connected {agent}: hooks installed, imported {connection.imported} entries.")

    return SetupReport(
        home=home,
        already_initialized=already_initialized,
        project=project,
        onboarded=onboarded,
        notes=notes,
    )


def _explainability_step(decision: bool | None) -> list[str]:
    """The optional explainability step: offer it, take it, or leave no trace.

    Three outcomes, and the middle one is the important one:

    * ``True``  — the user opted in; configure and say what will be captured.
    * ``None``  — not asked or not answered. Mention the step exists, if and
      only if it could actually be accepted here, and change nothing.
    * ``False`` — declined. Say nothing, do nothing. #50's first acceptance
      clause is that declining leaves ZERO behavioural change, and a decline
      that still wrote a config key or printed a nag would not be zero.

    Never raises: a machine with a broken gateway config must still finish
    ``init``.
    """
    if decision is False:
        return []
    try:
        offer = explainability_service.shipping_offer()
    except Exception:  # setup must not die of an optional step
        return []
    if decision is None:
        if not offer.available:
            return []
        return [
            "Explainability: this machine can ship "
            f"{explainability_service.ShippingOffer.CAPTURES} to {offer.gateway_url}. "
            "Off until you ask for it: aisquare init --explainability"
        ]
    if not offer.available:
        return [f"Explainability not configured — {offer.reason}"]
    state = explainability_service.configure_shipping()
    if not state.configured:
        return [f"Explainability not configured — {state.reason}"]
    return [
        f"Explainability on: shipping {explainability_service.ShippingOffer.CAPTURES} "
        f"to {state.gateway_url}. Drain with: aisquare explainability ship"
    ]


# --- upgrade -----------------------------------------------------------------------------

HOOK_AGENT = "claude-code"
"""The one agent whose hooks aisquare writes (``core.agents._HOOKS``)."""

VERSION_CHECK_TIMEOUT_SECONDS = 60.0
HOOK_REFRESH_TIMEOUT_SECONDS = 120.0


class InvalidVersion(ValueError):
    """``--version`` named something that is not a version."""


class UpgradeRefused(RuntimeError):
    """This install is not one ``aisquare upgrade`` runs; ``command`` is the one that does."""

    def __init__(self, reason: str, command: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.command = command


@dataclass(frozen=True)
class HookSite:
    """One Claude Code config directory, and what its aisquare hooks run.

    ``reason`` is set when the directory is left alone, and says why.
    """

    config_dir: Path
    programs: tuple[str, ...] = ()
    reason: str | None = None
    hooks_off: Path | None = None
    """For a site the upgrade rewrites: its settings file, when that switches every hook off
    (``"disableAllHooks": true``, :func:`agent_core.hooks_off`). The hooks are rewritten so
    they run once the key goes; until then Claude Code runs none, so it is not "connected"."""


@dataclass(frozen=True)
class UpgradePlan:
    """What ``aisquare upgrade`` would do, decided before anything runs."""

    route: install_route.InstallRoute
    current: str
    target: str | None
    """The version asked for with ``--version``; ``None`` means the latest release."""
    latest: install_route.LatestRelease | None
    """What PyPI said, or ``None`` when it was not asked (a pinned or refused run)."""
    argv: tuple[str, ...]
    env: dict[str, str]
    reason: str | None
    """Why this route is reported rather than run; ``None`` when it runs."""
    refresh: tuple[HookSite, ...] = ()
    """Recorded hook sites the new install re-connects afterwards (issue #58)."""
    left: tuple[HookSite, ...] = ()
    """Recorded hook sites left as they are, each with its reason."""
    live_agents: tuple[str, ...] = ()
    """Live fleet agents, counted as uninstall counts them (:func:`running_fleet`)."""
    fleet_error: str | None = None
    """Why the fleet's live agents could not be counted, when they could not."""

    @property
    def runnable(self) -> bool:
        return self.reason is None

    @property
    def backwards(self) -> bool:
        """Whether this run moves to a release older than the one running (``--version``)."""
        return self.target is not None and install_route.is_newer(self.current, self.target) is True

    @property
    def fleet_warning(self) -> str | None:
        """What upgrading now costs the live fleet agents, or ``None`` when none runs.

        Uninstall refuses while they run, because the program their hooks call goes
        for good. Here it goes only while ``uv tool install --force`` recreates the
        environment, so it is said, in the plan and in the question, rather than
        refused: stopping the fleet costs every session in it, and the window is
        seconds (review of #257).
        """
        if not self.live_agents:
            return None
        count = len(self.live_agents)
        return (
            f"{count} fleet agent{'s are' if count != 1 else ' is'} running "
            f"({', '.join(self.live_agents)}): a hook {'they fire' if count != 1 else 'it fires'} "
            "while the install is being replaced fails, and that turn's board update is lost. "
            f"To be safe, stop {'them' if count != 1 else 'it'} first: {FLEET_SHUTDOWN}"
        )

    @property
    def command(self) -> str:
        return install_route.command_line(self.argv)

    @property
    def latest_version(self) -> str | None:
        return self.latest.version if self.latest is not None else None

    @property
    def destination(self) -> str:
        """Where the upgrade goes, for a person: a version, or what ``@latest`` means."""
        return self.target or self.latest_version or "the latest release"

    @property
    def update_available(self) -> bool | None:
        """Whether PyPI has something newer; ``None`` when PyPI could not say."""
        latest = self.latest_version
        if latest is None:
            return None
        newer = install_route.is_newer(latest, self.current)
        return (latest.strip() != self.current.strip()) if newer is None else newer

    @property
    def up_to_date(self) -> bool:
        """Nothing to install: the pin is what runs, or nothing newer is published.

        A running version AHEAD of PyPI's (a pre-release, a mirror) is up to
        date here — moving it would be a downgrade, and a downgrade is only ever
        done by asking for one with ``--version``.
        """
        if self.target is not None:
            return install_route.same_version(self.target, self.current)
        return self.update_available is False


@dataclass(frozen=True)
class HookRefresh:
    """One recorded hook site, re-connected by the new install — or why not."""

    config_dir: Path
    ok: bool
    error: str | None = None
    hooks_off: Path | None = None
    """:attr:`HookSite.hooks_off`: rewritten, but switched off there."""


@dataclass(frozen=True)
class UpgradeReport:
    """What an upgrade did. ``problem`` is set when the new version could not be confirmed."""

    plan: UpgradePlan
    exit_code: int
    version: str | None = None
    """What the install reports afterwards, asked in a new process, also after a failed
    install; ``None`` when it could not say."""
    problem: str | None = None
    hooks: tuple[HookRefresh, ...] = ()
    notes: tuple[str, ...] = ()
    cutoff: str | None = None
    """The uv cutoff the install ran under (``--exclude-newer P14D``), from the receipt uv
    wrote for it, or ``None``."""

    @property
    def installed(self) -> bool:
        """Whether the installer itself reported success."""
        return self.exit_code == 0

    @property
    def upgraded(self) -> bool:
        """Whether a NEW process reports the version this upgrade was for."""
        return self.installed and self.problem is None and self.version is not None


def upgrade_plan(target: str | None = None, *, check: bool = False) -> UpgradePlan:
    """Decide what ``aisquare upgrade`` would do. Reads only; starts nothing.

    PyPI is asked only when its answer is used: by ``--check``, and by a run
    that targets the latest release on a route that runs. A refused route is
    refused offline, and a pin needs no lookup.
    """
    if target is not None:
        pinned = install_route.version_argument(target)
        if pinned is None:
            raise InvalidVersion(target)
        target = pinned
    route = install_route.detect()
    reason = install_route.not_automated(route)
    latest: install_route.LatestRelease | None = None
    if check or (reason is None and target is None):
        latest = _latest_for(route)
    refresh: tuple[HookSite, ...] = ()
    left: tuple[HookSite, ...] = ()
    live: tuple[str, ...] = ()
    fleet_error: str | None = None
    if reason is None:
        refresh, left = refresh_sites(route.facts)
        if target is not None and install_route.is_newer(__version__, target):
            # upgrade() leaves the hooks alone on a move back, to a release that may
            # predate `agents refresh-hooks`, so the plan must not promise a re-connect:
            # the plan, its --json, the question and the report agree (review of #257).
            why = f"{target} is older than {__version__}: a move back leaves the hooks as they are"
            left = (*left, *(HookSite(site.config_dir, site.programs, why) for site in refresh))
            refresh = ()
        live, _unlistened, fleet_error = running_fleet()
    return UpgradePlan(
        route=route,
        current=__version__,
        target=target,
        latest=latest,
        argv=tuple(install_route.upgrade_argv(route, target, current=__version__)),
        env=install_route.installer_env(route),
        reason=reason,
        refresh=refresh,
        left=left,
        live_agents=live,
        fleet_error=fleet_error,
    )


def _beyond_pypi(route: install_route.InstallRoute) -> str | None:
    """Why PyPI's newest says nothing about what this install's upgrade gets, or ``None``:
    it resolves from its own index, or takes nothing uploaded after a uv cutoff."""
    own = install_route.own_index(route)
    if own is not None:
        return f"this install resolves from its own index ({own})"
    cutoff = install_route.cutoff(route)
    if cutoff is not None:
        return f"this install takes no release uploaded after its uv cutoff ({cutoff})"
    return None


def _latest_for(route: install_route.InstallRoute) -> install_route.LatestRelease:
    """PyPI's newest release, pre-releases counted when this install's upgrade takes them
    (``install_route.takes_prereleases``), unless PyPI says nothing about what it gets
    (:func:`_beyond_pypi`). With a cutoff, PyPI's newest was taken as the target, and the
    unchanged version uv correctly left was reported as §3.9.1's silent no-op on every run
    (sweep of #257)."""
    why = _beyond_pypi(route)
    if why is not None:
        return install_route.LatestRelease(None, f"PyPI was not asked: {why}")
    if install_route.takes_prereleases(route, __version__):
        return install_route.fetch_latest(prereleases=True)
    return install_route.fetch_latest()


def runs_this_install(binary: agent_core.HookBinary, found: install_route.Facts) -> bool:
    """Whether a hook's program is THIS install — the one being upgraded.

    A console script counts when it IS one of this environment's scripts or a
    link into them: the installer's hooks name ``~/.local/bin/aisquare``'s
    target, and a hand-written hook may name the link itself. ``python -m
    aisquare`` counts when its interpreter sits in this environment's ``bin``,
    compared by directory because every venv's ``python`` resolves to one base
    interpreter.
    """
    if binary.module_form:
        return agent_core.dir_identity(binary.program.parent) == agent_core.dir_identity(
            found.executable.parent
        )
    program = agent_core.dir_identity(binary.program)
    prefix = agent_core.dir_identity(found.prefix)
    return program == prefix or prefix in program.parents


def refresh_sites(found: install_route.Facts) -> tuple[tuple[HookSite, ...], tuple[HookSite, ...]]:
    """Which recorded hook sites the upgraded install re-connects, and which it leaves.

    Only directories THIS home connected (``agents.json``) — a directory found on
    disk was never this home's to rewrite — and only those that still carry
    aisquare hooks: an operator who removed them by hand did not ask for them
    back. A site whose hooks run ANOTHER install of aisquare that is still on
    disk is left alone and named, because re-connecting it would quietly move a
    developer's hooks off the checkout they chose and onto this release. A
    program that is gone is not another install: those hooks fail every
    session, and re-connecting is the fix.
    """
    refresh: list[HookSite] = []
    left: list[HookSite] = []
    seen: set[Path] = set()
    for directory in agent_core.connected_dirs(HOOK_AGENT):
        key = agent_core.dir_identity(directory)
        if key in seen:
            continue
        seen.add(key)
        binaries, error = hook_binaries(directory)
        if error is not None:
            left.append(HookSite(directory, reason=error))
            continue
        if not binaries:
            continue
        programs = tuple(str(binary.program) for binary in binaries)
        # os.path.exists, not Path.exists: on 3.11 to 3.13 the latter raises PermissionError
        # (3.14 answers False) for a program in a directory this user cannot enter, and
        # upgrade crashed. Such a program counts as gone, whose hooks fail every session
        # (review of #257).
        foreign = next(
            (b for b in binaries if os.path.exists(b.program) and not runs_this_install(b, found)),
            None,
        )
        unwritable = agents_service.settings_unwritable(directory / "settings.json")
        if foreign is not None:
            left.append(
                HookSite(
                    directory,
                    programs,
                    f"its hooks run {foreign.program}, another install of aisquare",
                )
            )
        elif unwritable is not None:
            # Planned, it failed in the new install after a good upgrade, which then
            # exited 1 and kept asq closed; the remedy it printed failed the same way
            # (review of #257). The hooks name the tool path the reinstall keeps.
            left.append(HookSite(directory, programs, _CANNOT_REWRITE.format(unwritable)))
        else:
            # Rewritten all the same, as `agents connect` does: they run once the key goes.
            # Said, because the plan and the report called such a site re-connected, where
            # connect and init now say it is not (sweep of #257).
            off = agent_core.hooks_off(HOOK_AGENT, directory)
            refresh.append(HookSite(directory, programs, hooks_off=off))
    return tuple(refresh), tuple(left)


#: Why a site whose hooks are ours is left: aisquare may not write its settings.json.
_CANNOT_REWRITE = "its settings.json cannot be rewritten: {}"


#: A ``"command": "…"`` pair in settings.json text, found without parsing the JSON.
_COMMAND_FIELD = re.compile(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"')


def lenient_hook_commands(text: str) -> list[str]:
    """The aisquare hook commands in settings.json ``text``, valid JSON or not.

    Claude Code reads its settings more forgivingly than ``json.loads``: a
    Latin-1 byte or a trailing comma need not stop it running the hooks inside.
    So whether a file holds OUR hooks is decided from every ``"command"`` value
    in it, matched as text, each judged by the same matcher ``connect`` uses.
    """
    found: list[str] = []
    for match in _COMMAND_FIELD.finditer(text):
        try:
            command = json.loads(f'"{match.group(1)}"')
        except ValueError:
            command = match.group(1)
        if isinstance(command, str) and agent_core.hook_binary(command) is not None:
            found.append(command)
    return found


#: Byte-order marks a hand-saved settings.json may start with: PowerShell 5.1 writes
#: UTF-16, Notepad has written UTF-8 with a BOM.
_BOMS = ((b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"), (b"\xef\xbb\xbf", "utf-8-sig"))


def lenient_text(raw: bytes) -> str:
    """``raw`` as text for :func:`lenient_hook_commands`: by its BOM, else UTF-8 with
    replacement characters, so no byte stops the search for our commands."""
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            return raw.decode(codec, errors="replace")
    return raw.decode("utf-8", errors="replace")


def settings_unreadable(directory: Path) -> str | None:
    """Why the aisquare hooks in ``directory`` cannot be read or rewritten, or ``None``.

    ``hook_commands`` reads a file it cannot parse as one with no hooks (#247),
    which is right for doctor and wrong for a step that has to act on the hooks.
    A site is named, with its reason, when:
    - its settings.json cannot be read at all, which may hide hooks; or
    - it holds aisquare hook commands (:func:`lenient_hook_commands`) in a file
      that is not UTF-8, not valid JSON, or not an object with a ``hooks``
      object. Claude Code may still run those hooks, and this CLI cannot rewrite
      that file safely.
    A file that cannot be parsed but holds nothing of ours is not this
    command's business (review of #254).
    """
    settings = directory / "settings.json"
    # One read, "missing" split out. An exists() first raised PermissionError on 3.11 to
    # 3.13 for a directory this user cannot enter, and answers False on 3.14, passing it
    # as one with no hooks; either broke the promise above (review of #257).
    try:
        raw = settings.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        return f"its settings.json could not be read ({exc})"
    problem: str | None = None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # UnicodeDecodeError is one
        problem = f"cannot be read as UTF-8 JSON ({exc})"
    else:
        if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
            problem = "is not a JSON object with a hooks object"
    if problem is None or not lenient_hook_commands(lenient_text(raw)):
        return None
    # Said by upgrade's plan and by uninstall's: both act on the hooks by rewriting the
    # file, so the ending names that, not either command's verb (review of #257).
    return (
        f"its settings.json holds aisquare hooks but {problem}, "
        "so aisquare cannot rewrite it safely"
    )


def hook_binaries(directory: Path) -> tuple[list[agent_core.HookBinary], str | None]:
    """The distinct programs ``directory``'s aisquare hooks run — or why it could not be read.

    The one reader for upgrade's refresh and uninstall's plan. A settings.json
    this user cannot read, or that is not UTF-8, is a reason (:func:`settings_unreadable`),
    and so is anything else the read raises: one bad file must not stop the work
    on every other directory, and must never pass for a directory with no hooks.
    """
    unreadable = settings_unreadable(directory)
    if unreadable is not None:
        return [], unreadable
    try:
        commands = agent_core.hook_commands(HOOK_AGENT, directory)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        # RuntimeError: pathlib's, for a recorded `~olduser/.claude` (sweep of #257).
        return [], f"its settings.json could not be read ({exc})"
    binaries: list[agent_core.HookBinary] = []
    for command in commands:
        binary = agent_core.hook_binary(command)
        if binary is not None and binary not in binaries:
            binaries.append(binary)
    return binaries, None


def upgrade(plan: UpgradePlan, *, to_stderr: bool = False) -> UpgradeReport:
    """Run the plan: install, confirm the version in a NEW process, refresh the hooks.

    ``to_stderr`` sends the installer's output to stderr, for ``--json``.
    Nothing after the installer imports anything (see the module docstring);
    the version check and the hook refresh are the new install's own
    processes. A hook site that fails is reported and the rest still run.
    """
    if plan.reason is not None:
        raise UpgradeRefused(plan.reason, plan.command)
    code = install_route.run_installer(plan.argv, env=plan.env, to_stderr=to_stderr)
    if code != 0:
        # Asked too: uv resolves before it replaces anything (measured, uv 0.12.19), so a
        # run it could not resolve, as under a cutoff that excludes even the release that
        # runs, leaves that release installed, and no reinstall from nothing is needed.
        kept, _ = _installed_version(plan)
        return UpgradeReport(
            plan, exit_code=code, version=kept, problem=f"{plan.argv[0]} exited {code}"
        )
    ran = _as_recorded(plan.route)
    cutoff = install_route.cutoff(ran) or install_route.cutoff(plan.route)
    version, problem = _verify(plan, ran)
    if problem is not None:
        return UpgradeReport(plan, exit_code=code, version=version, problem=problem, cutoff=cutoff)
    notes: list[str] = []
    latest = plan.latest_version
    moved_elsewhere = latest is not None and not install_route.same_version(version or "", latest)
    if plan.target is None and version is not None and moved_elsewhere:
        held = "your uv cutoff allows" if cutoff is not None else "your package index served"
        notes.append(f"PyPI's latest is {latest}; {held} {version}")
    notes.append(
        f"asq and `aisquare serve` processes that were already running keep {plan.current} "
        "until they are restarted"
    )
    if version is not None and install_route.is_newer(plan.current, version):
        # A move BACK lands on a release that may predate `agents refresh-hooks`
        # (0.7 and earlier do), and the hooks the newer version wrote still run it.
        notes.append(
            f"{version} is older than {plan.current}, so the hooks were left as they were; "
            f"`aisquare agents connect {HOOK_AGENT}` rewrites them for {version}"
        )
        return UpgradeReport(
            plan, exit_code=code, version=version, notes=tuple(notes), cutoff=cutoff
        )
    hooks = tuple(_refresh(site, plan.route.facts) for site in plan.refresh)
    return UpgradeReport(
        plan, exit_code=code, version=version, hooks=hooks, notes=tuple(notes), cutoff=cutoff
    )


def _as_recorded(route: install_route.InstallRoute) -> install_route.InstallRoute:
    """``route`` with the receipt uv wrote for the install that just ran.

    A cutoff or an index set in uv's own settings (uv.toml, ``UV_EXCLUDE_NEWER``,
    ``UV_INDEX_URL``) applies to that install and is recorded in its receipt, never in
    the one the plan read (measured, uv 0.12.19). Only reads: tomllib and the receipt
    parser are already in memory (see the module docstring).
    """
    try:
        receipt = install_route.read_receipt(route.facts.prefix)
    except OSError:
        receipt = None
    return route if receipt is None else replace(route, receipt=receipt)


def _reason_line(*texts: str) -> str:
    """Why a command failed, whole: from the first text that says anything, the line that
    names the failure, read past Rich's wrapping and box borders. The child writes to a
    pipe, so Rich lays it out at 80 columns, and the last line was a wrapped path's tail
    (``n'``) or a usage box's bottom border (review of #257). A traceback's exception
    line comes with what follows it; this CLI's own ``✗ …`` loses its mark."""
    for text in texts:
        verdict = onboarding.stderr_verdict(text)
        if verdict:
            return verdict.removeprefix("✗ ")
    return ""


def _installed_version(plan: UpgradePlan) -> tuple[str | None, str | None]:
    """``(version, problem)``: what the install in ``plan``'s prefix reports, asked in a NEW
    process, or why it could not say."""
    probe = agent_core.HookBinary(plan.route.facts.executable, module_form=True)
    answer = install_route.run_captured(probe.version_argv(), timeout=VERSION_CHECK_TIMEOUT_SECONDS)
    if answer.error is not None:
        return None, f"the new install could not be started ({answer.error})"
    if answer.returncode != 0:
        said = _reason_line(answer.stderr, answer.stdout) or f"exit {answer.returncode}"
        return None, f"the new install could not be started ({said})"
    found = agent_core.version_in(answer.stdout)
    if found is None:
        return None, "the new install did not report a version"
    return found, None


def _verify(plan: UpgradePlan, ran: install_route.InstallRoute) -> tuple[str | None, str | None]:
    """``(version, problem)`` from asking the NEW install its version in a new process.

    The exit code of the installer is not the evidence: §3.9.1's failure was a
    success code over an unchanged version. So success is a version the new
    process reports — the pin when one was asked for, otherwise any move that is
    not BACK: a downgrade is only done by asking for one with ``--version``. An
    unchanged version is a failure exactly when PyPI said there is something
    newer; when PyPI was not asked or could not answer, or the install ``ran``
    under (:func:`_as_recorded`) has its own index or a uv cutoff, it is the newest
    release the index serves, or the cutoff allows (:func:`_beyond_pypi`).
    """
    found, problem = _installed_version(plan)
    if found is None:
        return None, problem
    if plan.target is not None:
        if install_route.same_version(found, plan.target):
            return found, None
        return found, f"{plan.target} was asked for, but the new install reports {found}"
    if install_route.is_newer(plan.current, found):
        # The command asks for this release or newer (install_route._uv_spec), so uv fails
        # rather than resolve below it. Still a failure, never ✓ and asq reopened on a
        # release that may have no `upgrade` of its own (sweep of #257).
        return found, f"the new install reports {found}, which is older than {plan.current}"
    latest = plan.latest_version
    if not install_route.same_version(found, plan.current) or latest is None:
        return found, None
    if _beyond_pypi(ran) is not None:
        # A cutoff or an index in uv's own settings, which only the receipt uv just wrote
        # shows: blamed on §3.9.1, the newest release it allows failed (sweep of #257).
        return found, None
    return found, (
        f"uv reported success but aisquare still reports {found}, not {latest} — the "
        "silent no-op docs/plans/one-line-install.md §3.9.1 describes"
    )


#: What the new install runs per site: ``agents refresh-hooks``, never ``agents
#: connect``, which also re-imports the agent's ``CLAUDE.md`` into memory — on every
#: upgrade it brought back sections the user had removed (review of #251).
REFRESH_HOOKS = ("agents", "refresh-hooks", HOOK_AGENT)


def _refresh(site: HookSite, found: install_route.Facts) -> HookRefresh:
    """Rewrite one site's hooks BY the new install, so they name the new install.

    The console script beside the interpreter, not ``python -m aisquare``: hooks
    are written for whatever program the refresh ran as, and the module form
    would fall back to whichever ``aisquare`` comes first on PATH.
    """
    script = found.executable.with_name("aisquare.exe" if found.platform == "win32" else "aisquare")
    if not script.exists():
        return HookRefresh(site.config_dir, False, f"the new install has no {script}")
    argv = [str(script), *REFRESH_HOOKS, "--config-dir", str(site.config_dir)]
    answer = install_route.run_captured(argv, timeout=HOOK_REFRESH_TIMEOUT_SECONDS)
    if answer.error is not None:
        return HookRefresh(site.config_dir, False, answer.error)
    if answer.returncode != 0:
        said = _reason_line(answer.stderr, answer.stdout) or f"exit {answer.returncode}"
        return HookRefresh(site.config_dir, False, said)
    return HookRefresh(site.config_dir, True, hooks_off=site.hooks_off)


# --- uninstall ---------------------------------------------------------------------------
#
# The ORDER is the design, and every step that can go wrong stops the ones after
# it from removing the means to retry:
#
#   1. refuse while the fleet has live agents — their sessions would keep firing
#      hooks at a program that is about to vanish;
#   2. remove aisquare's hook groups from every Claude Code directory, one
#      directory at a time, each failure recorded and the rest still attempted;
#   3. only when the home STAYS, record in agents.json that nothing is connected;
#   4. with --purge only, delete the home — behind a guard, never through a link;
#   5. LAST, and only when 2-4 all succeeded, hand the process to the package
#      manager. A failure anywhere above keeps the package, so `aisquare
#      uninstall` is still there to run again.
#
# Nothing here goes through `agents_service.disconnect`: it calls set_connected,
# which calls `paths.ensure_home()`, and an uninstall that recreated the home it
# was keeping out of — or had just purged — would be the one place that broke the
# promise that the plan never creates ~/.aisquare.

#: Files that make a directory an aisquare home: at least one must be there
#: before --purge deletes anything.
HOME_MARKERS = ("config.toml", "context.db", "agents.json")

FLEET_SHUTDOWN = "aisquare fleet shutdown --all --yes"


class UninstallRefused(RuntimeError):
    """Uninstall will not start; ``error`` is the machine-readable reason."""

    def __init__(self, message: str, error: str) -> None:
        super().__init__(message)
        self.error = error


@dataclass(frozen=True)
class McpRegistration:
    """An MCP server entry in a ``.claude.json`` that runs aisquare."""

    name: str
    file: Path
    project: str | None = None
    """The project a local-scope entry belongs to; ``None`` for user scope."""


@dataclass(frozen=True)
class UninstallPlan:
    """What ``aisquare uninstall`` would do, read before anything is touched.

    Read-only, and it never creates ``~/.aisquare``: the store is opened only when
    ``context.db`` already exists, and every other read is a file that is there
    or is not.
    """

    route: install_route.InstallRoute
    hooks: tuple[HookSite, ...]
    """Every Claude Code directory holding aisquare hooks, with what they run."""
    unreadable: tuple[HookSite, ...]
    """Directories whose hooks cannot be taken out: a settings.json that could not be
    read, which may hold some, or one holding ours that this user may not write."""
    mcp: tuple[McpRegistration, ...]
    package_argv: tuple[str, ...]
    package_env: dict[str, str]
    package_reason: str | None
    """Why the package step is printed rather than run; ``None`` when it runs."""
    home: Path
    home_exists: bool
    home_entries: tuple[str, ...]
    accounts: tuple[str, ...]
    """The Claude Code logins kept in the home's account slots, described."""
    keychain: bool
    """Whether those logins' tokens live in the macOS Keychain, which a purge leaves."""
    purge: bool
    purge_refusal: str | None
    """Why --purge may not delete the home: it refuses a run with ``purge`` set, and a
    plan without it does not offer --purge for this home."""
    live_agents: tuple[str, ...]
    """Live fleet rows that may still be running: with tmux on PATH every live row,
    without it those whose socket still has a server listening."""
    unlistened: int
    """Live rows that do not block: no tmux here and no server on their socket."""
    fleet_error: str | None
    """Why the fleet's live agents could not be counted, when they could not."""
    plugins: tuple[agent_core.ClaudePlugin, ...] = ()
    """Where the aisquare Claude Code plugin is enabled: in a config dir (user scope) or
    in a repository (project and local scope). Uninstall leaves it, as Claude Code owns
    it, and it keeps running aisquare there: through uvx once the package is gone,
    which makes the home again (review of #257)."""

    @property
    def package_command(self) -> str:
        return install_route.command_line(self.package_argv)

    @property
    def blocking(self) -> tuple[HookSite, ...]:
        """The ``unreadable`` sites whose hooks keep the package and the home: every one, unless
        this run purges (:attr:`purges`). Every one is then inside the home (an account slot, a
        retired one too), and the purge deletes their hooks with it, as it does a plugin there
        (:attr:`lasting_plugins`) (sweep of #257)."""
        return () if self.purges else self.unreadable

    @property
    def blocked(self) -> bool:
        """Whether some directory's hooks cannot be taken out (:attr:`blocking`). The run then
        keeps the package and the home, which those hooks still call, so the plan, its
        --json and the question say so too (review of #257)."""
        return bool(self.blocking)

    @property
    def purges(self) -> bool:
        """Whether this run deletes the home: --purge, a home the guard lets it delete
        (:attr:`purge_refusal`), and no site outside the home whose hooks cannot be taken
        out, as such a site stops the run before the purge. The plan, its --json, the
        question and :func:`uninstall` all follow this one rule (sweep of #257)."""
        return (
            self.purge
            and self.home_exists
            and self.purge_refusal is None
            and all(self.in_home(site.config_dir) for site in self.unreadable)
        )

    def in_home(self, path: Path) -> bool:
        """Whether ``path`` is inside the home, so a purge deletes it with everything in it."""
        return agent_core.dir_identity(self.home) in agent_core.dir_identity(path).parents

    @property
    def lasting_plugins(self) -> tuple[agent_core.ClaudePlugin, ...]:
        """The plugins still enabled after this run: when it purges, not those whose config
        dir is inside the home it deletes (the fleet's account slots, retired ones too),
        which go with it and can run nothing afterwards (review of #257)."""
        if not self.purges:
            return self.plugins
        return tuple(plugin for plugin in self.plugins if not self.in_home(plugin.config_dir))

    @property
    def lasting_mcp(self) -> tuple[McpRegistration, ...]:
        """The MCP registrations still there after this run: when it purges, not those in a
        ``.claude.json`` inside the home (an account slot's), which the purge deletes, as it
        does a plugin there (sweep 2 of #257). The run asks this before the purge: a link
        inside the home may lead to a file the purge leaves, and once the link is gone, its
        path no longer resolves to that file."""
        if not self.purges:
            return self.mcp
        return tuple(entry for entry in self.mcp if not self.in_home(entry.file))

    @property
    def refusal(self) -> UninstallRefused | None:
        """The reason nothing may start, or ``None``."""
        if self.live_agents:
            count = len(self.live_agents)
            return UninstallRefused(
                f"{count} fleet agent{'s are' if count != 1 else ' is'} running "
                f"({', '.join(self.live_agents)}) — their sessions would keep calling hooks "
                f"that no longer exist. Stop them first: {FLEET_SHUTDOWN}",
                error="fleet_running",
            )
        if self.purge and self.purge_refusal is not None:
            return UninstallRefused(
                f"--purge will not delete {self.home}: {self.purge_refusal}",
                error="purge_refused",
            )
        lasting = self.lasting_plugins
        if self.purges and lasting:
            # A purge the next session undoes is not one: refused like the fleet, with
            # the command that clears the way. A plain uninstall only says so.
            where = ", ".join(plugin_place(plugin) for plugin in lasting)
            return UninstallRefused(
                f"--purge would not last: the aisquare plugin is enabled in {where}, so "
                f"Claude Code's next session there runs aisquare and makes {self.home} again. "
                f"Remove the plugin first: "
                f"{'; '.join(plugin_removal(plugin) for plugin in lasting)}",
                error="plugin_enabled",
            )
        return None


def plugin_removal(plugin: agent_core.ClaudePlugin) -> str:
    """The command that removes the aisquare plugin from its config dir — at its scope,
    and for a project- or local-scope install from inside its repository."""
    return agent_core.claude_plugin_command(
        "uninstall", plugin.config_dir, scope=plugin.scope, project=plugin.project
    )


def plugin_place(plugin: agent_core.ClaudePlugin) -> str:
    """Where the aisquare plugin runs: its config dir, or the repository that enables it."""
    if plugin.project is None:
        return str(plugin.config_dir)
    return f"{plugin.project} ({plugin.scope} scope)"


@dataclass(frozen=True)
class HookRemoval:
    """One directory's hooks, removed — or why not."""

    config_dir: Path
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class UninstallReport:
    """What uninstall did. ``package_runs`` means the package step comes next, by exec."""

    plan: UninstallPlan
    hooks: tuple[HookRemoval, ...] = ()
    record_error: str | None = None
    """Why agents.json could not be updated, when it could not."""
    purged: bool = False
    purge_error: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return any(not hook.ok for hook in self.hooks) or self.purge_error is not None

    @property
    def package_runs(self) -> bool:
        return not self.failed and self.plan.package_reason is None


def _user_home() -> Path:
    """The user's home directory (an indirection so tests can name one)."""
    return Path.home()


def _tmux_on_path() -> bool:
    """Whether tmux exists here at all (an indirection so tests can decide)."""
    return shutil.which("tmux") is not None


def _is_link(path: Path) -> bool:
    """A symlink, or on Windows a junction — anything a delete could be led through."""
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)  # Python 3.12+
    return bool(is_junction(path)) if is_junction is not None else False


def custom_home(home: Path) -> bool:
    """Whether AISQUARE_HOME moved the home away from ``~/.aisquare``."""
    if not os.environ.get(paths.HOME_ENV_VAR):
        return False
    return agent_core.dir_identity(home) != agent_core.dir_identity(_user_home() / ".aisquare")


def purge_refusal(home: Path, *, custom: bool) -> str | None:
    """Why ``home`` must not be deleted, or ``None`` when ``--purge`` may delete it.

    Every check is about the one mistake that cannot be undone, a recursive
    delete of the wrong directory:
    - a link (#198 plans links between account slots and ~/.claude, and a
      delete led through one would take the target);
    - the user's home, or anything above it, or a filesystem root;
    - a directory with none of aisquare's markers;
    - ANY home AISQUARE_HOME moved elsewhere. A directory someone chose can hold
      their own things under names aisquare also uses (``projects/``,
      ``screenshots/``), and no name tells the two apart (review of #253), so
      only ``~/.aisquare`` — aisquare's by name — is ever deleted.
    """
    if _is_link(home):
        return f"{home} is a link — delete what it points at by hand if that is what you mean"
    if not home.exists():
        return None
    if not home.is_dir():
        return f"{home} is not a directory"
    resolved = agent_core.dir_identity(home)
    user_home = agent_core.dir_identity(_user_home())
    if resolved == user_home:
        return f"{home} is your home directory"
    if resolved == Path(resolved.anchor):
        return f"{home} is the root of a filesystem"
    if resolved in user_home.parents:
        return f"{home} contains your home directory"
    if not any((home / marker).is_file() for marker in HOME_MARKERS):
        return f"{home} holds none of {', '.join(HOME_MARKERS)}, so it is not an aisquare home"
    if custom:
        return (
            f"AISQUARE_HOME moved the home to {home}, and --purge deletes only ~/.aisquare: a "
            "directory you chose may hold your own files under names aisquare also uses — "
            "delete it by hand"
        )
    return None


def _account_dirs() -> list[Path]:
    """Every directory under the managed account root: the slots and the removed ones."""
    root = paths.claude_accounts_dir()
    try:
        return sorted(child for child in root.iterdir() if child.is_dir()) if root.is_dir() else []
    except OSError:
        return []


def _accounts_kept() -> tuple[str, ...]:
    """The logins the home keeps in its account slots, as a person reads them."""
    described: list[str] = []
    for account in accounts_core.managed_accounts():
        identity = accounts_core.identity(account)
        who = identity.email if identity is not None else "not signed in"
        described.append(f"slot {account.slot}: {who}")
    retired = [d for d in _account_dirs() if ".removed-" in d.name]
    if retired:
        described.append(f"{len(retired)} removed slot{'s' if len(retired) != 1 else ''}")
    return tuple(described)


#: The executable names that ARE aisquare, as either platform writes them.
_PROGRAM_NAMES = frozenset({"aisquare", "asq", "aisquare.exe", "asq.exe"})


def _runs_aisquare(spec: object) -> bool:
    """Whether an ``mcpServers`` entry starts aisquare — by program, never by mention.

    ``command`` counts when its file name IS the program (read as a Windows path
    whatever this machine is, since the file may come from either; ``/`` splits
    there too). An argument counts only as a BARE program name — ``uvx --from
    aisquare-cli aisquare serve`` — or as the ``-m aisquare`` pair. A path or a
    file that merely ends in ``aisquare`` (``~/Code/AISquare``, ``aisquare.json``)
    is another server's argument, and naming that server for removal would be
    wrong (review of #253).
    """
    if not isinstance(spec, dict):
        return False
    command = spec.get("command")
    if isinstance(command, str) and PureWindowsPath(command).name.lower() in _PROGRAM_NAMES:
        return True
    args = spec.get("args")
    tokens = [str(arg) for arg in args] if isinstance(args, list) else []
    for index, token in enumerate(tokens):
        if token.lower() in _PROGRAM_NAMES:
            return True
        if token == "-m" and tokens[index + 1 : index + 2] == ["aisquare"]:
            return True
    return False


def _mcp_registrations(directories: Iterable[Path]) -> tuple[McpRegistration, ...]:
    """aisquare's MCP servers in every ``.claude.json`` these directories use — read only.

    Listed, never edited: ``.claude.json`` is Claude Code's own file, rewritten by
    every running session, and a second writer racing them is how a login gets
    lost. ``claude mcp remove`` is the tool that owns it.
    """
    found: list[McpRegistration] = []
    read: set[Path] = set()
    for directory in directories:
        for path in agent_core.claude_json_paths(directory):
            if path in read:
                continue
            read.add(path)
            data = agent_core.read_json(path)
            servers = data.get("mcpServers")
            if isinstance(servers, dict):
                found.extend(
                    McpRegistration(str(name), path)
                    for name, spec in servers.items()
                    if _runs_aisquare(spec)
                )
            projects = data.get("projects")
            if not isinstance(projects, dict):
                continue
            for project, block in projects.items():
                servers = block.get("mcpServers") if isinstance(block, dict) else None
                if isinstance(servers, dict):
                    found.extend(
                        McpRegistration(str(name), path, str(project))
                        for name, spec in servers.items()
                        if _runs_aisquare(spec)
                    )
    return tuple(found)


def _plugins(directories: Iterable[Path]) -> tuple[agent_core.ClaudePlugin, ...]:
    """The aisquare plugin wherever these config dirs enable it: in the dir itself (user
    scope), and in each repository a project- or local-scope install of theirs enables
    (sweep of #257: those were never read, so --purge went ahead and the next session
    in the repository made the home again). Read only; empty where the plugin route
    does not run (:func:`agent_core.plugin_route_supported`)."""
    if not agent_core.plugin_route_supported():
        return ()
    found: list[agent_core.ClaudePlugin] = []
    seen: set[Path] = set()
    for directory in directories:
        key = agent_core.dir_identity(directory)
        if key in seen:
            continue
        seen.add(key)
        user = agent_core.claude_plugin(directory)
        if user is not None:
            found.append(user)
        found.extend(agent_core.claude_repo_plugins(directory))
    return tuple(found)


def _claude_dirs_for_mcp() -> list[Path]:
    """``~/.claude`` and every ``~/.claude*`` directory, hooked or not, for the MCP scan.

    ``claude mcp add`` on a plain install writes ``~/.claude.json``, beside
    ``~/.claude``, whether or not that directory carries hooks; reading a few
    small files is cheap next to telling nobody about a server that will fail to
    start once the package is gone.
    """
    default = accounts_core.home_config_dir()
    found = [default]
    with contextlib.suppress(OSError):
        found.extend(
            sorted(p for p in default.parent.glob(".claude*") if p.is_dir() and p != default)
        )
    return found


_LIVE_ROWS = (
    "SELECT f.label, f.tmux_socket, p.root FROM fleet_agent AS f "
    "LEFT JOIN project AS p ON p.id = f.project_id WHERE f.ended_at IS NULL"
)


def _siblings_hiding_hooks() -> list[Path]:
    """``~/.claude*`` directories whose aisquare hooks only show without strict parsing.

    The on-disk scan behind :func:`agent_core.hook_dirs` reads leniently since
    #247, so a sibling whose settings.json is Latin-1 or not valid JSON reads as
    having no hooks there, though Claude Code may still run them. Those are found
    here, and the plan then names them as sites it cannot clean, which keeps the
    package (review of #254). A sibling this user cannot read at all stays
    skipped, as the scan's own docstring rules: it cannot be shown to carry our
    hooks, and another account's backup must not block an uninstall forever.
    """
    found: list[Path] = []
    for directory in _claude_dirs_for_mcp():
        try:
            raw = (directory / "settings.json").read_bytes()
        except OSError:
            continue
        if lenient_hook_commands(lenient_text(raw)):
            found.append(directory)
    return found


def _live_fleet_agents() -> tuple[tuple[tuple[str, str], ...], str | None]:
    """The fleet's live rows as ``(label (project), socket)``, or why they could not be read.

    Read with a plain ``query_only`` connection, never through ``store_session``:
    that runs ``ensure_home`` (recreating ``cache/`` and ``log/``) and migrates
    the schema, and the plan writes nothing (review of #253). As the last
    connection, its close leaves no ``-wal``/``-shm`` behind; a ``mode=ro`` one
    would (measured). Opened only when ``context.db`` exists and is not empty. A
    store from before the fleet has no rows to count; any other store that
    cannot answer is reported, never raised — a damaged board is no reason to
    keep a user from removing the tool.
    """
    database = paths.db_path()
    try:
        if not database.is_file() or database.stat().st_size == 0:
            return (), None
    except OSError as exc:
        return (), str(exc)
    try:
        connection = sqlite3.connect(str(database), timeout=2.0)
    except sqlite3.Error as exc:
        return (), str(exc) or type(exc).__name__
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(_LIVE_ROWS).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return (), None  # a store from before the fleet: nothing can be live
        return (), str(exc) or type(exc).__name__
    except sqlite3.Error as exc:
        return (), str(exc) or type(exc).__name__
    finally:
        connection.close()
    return (
        tuple(
            (f"{label} ({Path(root).name if root else 'unknown project'})", socket_name or "asq")
            for label, socket_name, root in rows
        ),
        None,
    )


def _server_listening(socket_name: str) -> bool:
    """Whether a tmux server answers on fleet socket ``socket_name``, asked WITHOUT tmux.

    "tmux is not on PATH" is no evidence that no agent runs — the agents may have
    been started from a shell whose PATH had it (``core.tmux``: "a missing
    binary is not evidence either way"). The socket file is: a plain AF_UNIX
    connect at tmux's own path (``TmuxServer.socket_path``). Only "no file" and
    "nobody listening" count as no server; any other answer is treated as a live
    one, because the cost of being wrong is removing hooks under running agents.
    """
    try:
        path = TmuxServer(socket_name).socket_path()
    except TmuxUnavailable:
        return False  # no POSIX uid, so no tmux server can exist here at all
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        return False
    probe = socket.socket(family, socket.SOCK_STREAM)
    probe.settimeout(2.0)
    try:
        probe.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError:
        return True
    finally:
        probe.close()
    return True


def running_fleet() -> tuple[tuple[str, ...], int, str | None]:
    """``(live, unlistened, error)``: the fleet agents that may be running, the live rows
    that cannot be (no tmux here and no server on their socket), and why the fleet could
    not be read. The one count upgrade and uninstall both act on."""
    rows, fleet_error = _live_fleet_agents()
    tmux_found = _tmux_on_path()
    live = tuple(
        label for label, socket_name in rows if tmux_found or _server_listening(socket_name)
    )
    return live, len(rows) - len(live), fleet_error


def uninstall_plan(*, purge: bool = False) -> UninstallPlan:
    """Decide what ``aisquare uninstall`` would do. Reads only; never creates the home."""
    route = install_route.detect()
    candidates = [*agent_core.hook_dirs(HOOK_AGENT), *_account_dirs(), *_siblings_hiding_hooks()]
    hooks: list[HookSite] = []
    unreadable: list[HookSite] = []
    seen: set[Path] = set()
    for directory in candidates:
        key = agent_core.dir_identity(directory)
        if key in seen:
            continue
        seen.add(key)
        binaries, error = hook_binaries(directory)
        unwritable = (
            agents_service.settings_unwritable(directory / "settings.json") if binaries else None
        )
        if error is not None:
            unreadable.append(HookSite(directory, reason=error))
        elif unwritable is not None:
            # Hooks of ours that cannot be taken out: the run would fail on the write,
            # so the plan says so first and keeps the package (review of #257).
            unreadable.append(HookSite(directory, reason=_CANNOT_REWRITE.format(unwritable)))
        elif binaries:
            hooks.append(HookSite(directory, tuple(str(b.program) for b in binaries)))
    home = paths.aisquare_home()
    try:
        entries = tuple(sorted(c.name for c in home.iterdir())) if home.is_dir() else ()
    except OSError:
        entries = ()
    live, unlistened, fleet_error = running_fleet()
    custom = custom_home(home)
    claude_dirs = [*candidates, *_claude_dirs_for_mcp()]
    return UninstallPlan(
        route=route,
        hooks=tuple(hooks),
        unreadable=tuple(unreadable),
        mcp=_mcp_registrations(claude_dirs),
        package_argv=tuple(install_route.remove_argv(route)),
        package_env=install_route.installer_env(route),
        package_reason=install_route.not_removable(route),
        home=home,
        home_exists=home.exists() or home.is_symlink(),
        home_entries=entries,
        accounts=_accounts_kept(),
        keychain=accounts_core.keychain_platform(),
        purge=purge,
        purge_refusal=purge_refusal(home, custom=custom),
        live_agents=live,
        unlistened=unlistened,
        fleet_error=fleet_error,
        plugins=_plugins(claude_dirs),
    )


def uninstall(plan: UninstallPlan) -> UninstallReport:
    """Carry out the plan up to — not including — removing the package.

    Raises :class:`UninstallRefused` before touching anything when the plan
    refuses. Each directory's hooks are removed on their own: one that fails is
    recorded and the rest still go (fail open per directory). The package step
    is the caller's (:func:`remove_package`), and only when
    :attr:`UninstallReport.package_runs` says nothing failed.
    """
    refusal = plan.refusal
    if refusal is not None:
        raise refusal
    removals: list[HookRemoval] = []
    for site in plan.hooks:
        try:
            agent_core.remove_hooks(HOOK_AGENT, site.config_dir)
            left = agent_core.hook_commands(HOOK_AGENT, site.config_dir)
        except Exception as exc:  # fail open per directory; the report names it
            removals.append(HookRemoval(site.config_dir, False, str(exc) or type(exc).__name__))
            continue
        if left:
            removals.append(
                HookRemoval(site.config_dir, False, f"{len(left)} aisquare hook(s) still there")
            )
        else:
            removals.append(HookRemoval(site.config_dir, True))
    # A site that could not be read may still hold our hooks, and nothing here can
    # take them out: a failure like any other, so the package stays and the
    # command is still there to run again (review of #253). One inside the home
    # waits for the purge, which deletes it, hooks and all (sweep of #257).
    blocking = plan.blocking
    removals.extend(HookRemoval(site.config_dir, False, site.reason) for site in blocking)
    purged, purge_error = False, None
    hooks_failed = any(not removal.ok for removal in removals)
    lasting_mcp = plan.lasting_mcp  # read before the purge: see its docstring
    if plan.purges and not hooks_failed:
        purged, purge_error = _purge(plan.home)
    elif plan.purge and plan.home_exists and hooks_failed:
        purge_error = "not attempted: hooks were left in a directory above"
    if not purged:
        # The purge that would have taken them did not happen: they are still there.
        removals.extend(
            HookRemoval(site.config_dir, False, site.reason)
            for site in plan.unreadable
            if site not in blocking
        )
    record_error: str | None = None
    if not purged and paths.agents_registry_path().is_file():
        # The home survives — no purge, or one not attempted or not finished — so
        # it must not claim a connection that is gone: any recorded site with no
        # aisquare hook left, whether this run removed them or they were already
        # gone. Only when agents.json exists: set_connected creates the home.
        try:
            _unrecord_hookless()
        except Exception as exc:  # a record, not the hooks: reported, never a stop
            record_error = str(exc) or type(exc).__name__
    return UninstallReport(
        plan,
        hooks=tuple(removals),
        record_error=record_error,
        purged=purged,
        purge_error=purge_error,
        notes=tuple(
            _uninstall_notes(plan, removals, mcp=lasting_mcp if purged else plan.mcp, purged=purged)
        ),
    )


def _unrecord_hookless() -> None:
    """Drop every recorded agent site that carries no aisquare hook from agents.json.

    Every agent, not only Claude Code: ``agents connect codex`` records a site
    that never had hooks, and an uninstalled aisquare connects nothing. A site
    whose settings.json cannot be read keeps its record — it may still hold ours.
    """
    registry = agent_core.read_json(paths.agents_registry_path())
    names = set(registry.get("connected") or []) | set(registry.get("connections") or {})
    for name in sorted(str(n) for n in names):
        for directory in agent_core.connected_dirs(name, registry):
            binaries, error = hook_binaries(directory) if name == HOOK_AGENT else ([], None)
            if error is None and not binaries:
                agent_core.set_connected(name, False, directory)


def _purge(home: Path) -> tuple[bool, str | None]:
    """Delete the home, after asking the guard again right before the delete.

    ``shutil.rmtree`` never follows a link inside the tree — it removes the link —
    and refuses a link at the top; the guard has already refused that one.
    """
    refusal = purge_refusal(home, custom=custom_home(home))
    if refusal is not None:
        return False, refusal
    try:
        # The markers LAST: a delete that stops partway leaves them, so the retry
        # the report promises is still recognised as an aisquare home (review of
        # #253). A link is unlinked, never followed; rmtree inside does the same.
        for child in sorted(home.iterdir(), key=lambda entry: entry.name in HOME_MARKERS):
            if _is_link(child) or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)
        home.rmdir()
    except OSError as exc:
        return False, f"{home} was only partly deleted ({exc})"
    return True, None


def _uninstall_notes(
    plan: UninstallPlan,
    removals: Iterable[HookRemoval],
    *,
    mcp: tuple[McpRegistration, ...],
    purged: bool = False,
) -> list[str]:
    """What the run leaves for the user to do. ``mcp`` is the registrations still there."""
    notes: list[str] = []
    if any(removal.ok for removal in removals):
        notes.append("open Claude Code sessions keep the hooks they started with — restart them")
    if mcp:
        names = ", ".join(sorted({entry.name for entry in mcp}))
        notes.append(
            f"MCP servers that run aisquare are still registered ({names}); Claude Code owns "
            "that file — remove each with: claude mcp remove <name>"
        )
    for plugin in plan.lasting_plugins if purged else plan.plugins:
        notes.append(plugin_note(plugin))
    notes.append(
        "a running `aisquare serve` keeps running until you stop it; uv, tmux, Node, gh "
        "and Claude Code stay installed"
    )
    return notes


def plugin_note(plugin: agent_core.ClaudePlugin) -> str:
    """What an uninstall leaves running in a directory that enables the aisquare plugin."""
    return (
        f"the aisquare plugin is still enabled in {plugin_place(plugin)}, so Claude Code keeps "
        f"running aisquare there (through uvx once the package is gone) — remove it: "
        f"{plugin_removal(plugin)}"
    )


def remove_package(plan: UninstallPlan, *, stdout_to_stderr: bool) -> None:
    """Hand this process to the package manager. Returns only if it could not start.

    The LAST step on purpose: the hooks are already gone, so no session can fire
    one at the program while it disappears (and if any were left, uninstall
    stopped before this).
    """
    if plan.package_reason is not None:
        return
    install_route.exec_replace(
        plan.package_argv, env=plan.package_env, stdout_to_stderr=stdout_to_stderr
    )
