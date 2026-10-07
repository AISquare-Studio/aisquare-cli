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

import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import credentials as credentials_store
from aisquare.core import paths
from aisquare.core.config import (
    AppConfig,
    ExplainabilitySettings,
    load_config,
    save_config,
)
from aisquare.core.store import store_session
from aisquare.core.tmux import CONF_NAME as FLEET_TMUX_CONF
from aisquare.core.version import __version__
from aisquare.core.workspace import current_project
from aisquare.models import SetupReport
from aisquare.services import agents as agents_service
from aisquare.services import explainability as explainability_service
from aisquare.services import install_route
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
            notes.append("Codebase snapshot skipped (repomix/Node not available).")

    for agent in agents:
        try:
            connection = agents_service.connect(agent)
        except (KeyError, ValueError) as exc:
            notes.append(f"Could not connect {agent}: {exc}")
            continue
        hook_note = "hooks installed" if connection.hooks_installed else "no hooks for this agent"
        notes.append(f"Connected {agent}: {hook_note}, imported {connection.imported} entries.")

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

    @property
    def runnable(self) -> bool:
        return self.reason is None

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


@dataclass(frozen=True)
class UpgradeReport:
    """What an upgrade did. ``problem`` is set when the new version could not be confirmed."""

    plan: UpgradePlan
    exit_code: int
    version: str | None = None
    problem: str | None = None
    hooks: tuple[HookRefresh, ...] = ()
    notes: tuple[str, ...] = ()

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
        own = install_route.own_index(route)
        latest = (
            install_route.LatestRelease(
                None, f"PyPI was not asked: this install resolves from its own index ({own})"
            )
            if own is not None
            else install_route.fetch_latest()
        )
    refresh: tuple[HookSite, ...] = ()
    left: tuple[HookSite, ...] = ()
    if reason is None:
        refresh, left = refresh_sites(route.facts)
    return UpgradePlan(
        route=route,
        current=__version__,
        target=target,
        latest=latest,
        argv=tuple(install_route.upgrade_argv(route, target)),
        env=install_route.installer_env(route),
        reason=reason,
        refresh=refresh,
        left=left,
    )


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
        try:
            commands = agent_core.hook_commands(HOOK_AGENT, directory)
        except (OSError, ValueError, TypeError) as exc:
            # Unreadable, not UTF-8 (a ValueError), or a hooks table that is not
            # the shape Claude Code writes (`{"Stop": 1}` is a TypeError): left and
            # named, so one bad file cannot stop the upgrade of everything else.
            left.append(HookSite(directory, reason=f"its settings.json could not be read ({exc})"))
            continue
        binaries: list[agent_core.HookBinary] = []
        for command in commands:
            binary = agent_core.hook_binary(command)
            if binary is not None and binary not in binaries:
                binaries.append(binary)
        if not binaries:
            continue
        programs = tuple(str(binary.program) for binary in binaries)
        foreign = next(
            (b for b in binaries if b.program.exists() and not runs_this_install(b, found)),
            None,
        )
        if foreign is not None:
            left.append(
                HookSite(
                    directory,
                    programs,
                    f"its hooks run {foreign.program}, another install of aisquare",
                )
            )
        else:
            refresh.append(HookSite(directory, programs))
    return tuple(refresh), tuple(left)


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
        return UpgradeReport(plan, exit_code=code, problem=f"{plan.argv[0]} exited {code}")
    version, problem = _verify(plan)
    if problem is not None:
        return UpgradeReport(plan, exit_code=code, version=version, problem=problem)
    notes: list[str] = []
    latest = plan.latest_version
    moved_elsewhere = latest is not None and not install_route.same_version(version or "", latest)
    if plan.target is None and version is not None and moved_elsewhere:
        notes.append(f"PyPI's latest is {latest}; your package index served {version}")
    notes.append(
        f"asq and `aisquare serve` processes that were already running keep {plan.current} "
        "until they are restarted"
    )
    if version is not None and install_route.is_newer(plan.current, version):
        # A move BACK lands on a release that may predate `agents refresh-hooks`
        # (0.8 and earlier do), and the hooks the newer version wrote still run it.
        notes.append(
            f"{version} is older than {plan.current}, so the hooks were left as they were; "
            f"`aisquare agents connect {HOOK_AGENT}` rewrites them for {version}"
        )
        return UpgradeReport(plan, exit_code=code, version=version, notes=tuple(notes))
    hooks = tuple(_refresh(site, plan.route.facts) for site in plan.refresh)
    return UpgradeReport(plan, exit_code=code, version=version, hooks=hooks, notes=tuple(notes))


def _reason_line(*texts: str) -> str:
    """Why a command failed: the LAST non-empty line of the first text that has one,
    where this CLI's ``✗ …`` and a traceback's exception both land."""
    for text in texts:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[-1].removeprefix("✗ ")
    return ""


def _verify(plan: UpgradePlan) -> tuple[str | None, str | None]:
    """``(version, problem)`` from asking the NEW install its version in a new process.

    The exit code of the installer is not the evidence: §3.9.1's failure was a
    success code over an unchanged version. So success is a version the new
    process reports — the pin when one was asked for, otherwise any MOVE. An
    unchanged version is a failure exactly when PyPI said there is something
    newer; when PyPI could not be asked it means the index had nothing newer.
    """
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
    if plan.target is not None:
        if install_route.same_version(found, plan.target):
            return found, None
        return found, f"{plan.target} was asked for, but the new install reports {found}"
    latest = plan.latest_version
    if not install_route.same_version(found, plan.current) or latest is None:
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
    return HookRefresh(site.config_dir, True)


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

#: What aisquare itself writes at the top of its home, by name. A home that
#: AISQUARE_HOME moved somewhere custom is deleted by --purge only when it holds
#: nothing else: pointed at a directory people keep other things in, a
#: recursive delete would take those too. Names come from the helpers that
#: create them wherever one exists, so a rename there cannot strand this list.
_HOME_NAMES = frozenset(
    {
        paths.config_path().name,
        paths.credentials_path().name,
        f"{paths.credentials_path().name}.lock",
        paths.db_path().name,
        f"{paths.db_path().name}-wal",
        f"{paths.db_path().name}-shm",
        f"{paths.db_path().name}-journal",
        paths.agents_registry_path().name,
        paths.state_path().name,
        f"{paths.state_path().name}.lock",
        paths.claude_accounts_dir().name,
        paths.cache_dir().name,
        paths.log_dir().name,
        paths.project_data_dir("x").parent.name,
        paths.explainability_dir().name,
        paths.truncation_marker_path().name,
        explainability_service.key_path().name,
        FLEET_TMUX_CONF,
        "screenshots",
    }
)
#: ``core.atomic``'s sibling temp files: ``.<name>.<pid>.<8 hex>.tmp``.
_ATOMIC_TEMP = re.compile(r"^\..+\.\d+\.[0-9a-f]{8}\.tmp$")

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
    """Directories that may hold hooks but whose settings.json could not be read."""
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
    purge: bool
    purge_refusal: str | None
    """Why the home may not be deleted — consulted only when ``purge`` is set."""
    live_agents: tuple[str, ...]
    fleet_error: str | None
    """Why the fleet's live agents could not be counted, when they could not."""
    tmux_found: bool

    @property
    def package_command(self) -> str:
        return install_route.command_line(self.package_argv)

    @property
    def refusal(self) -> UninstallRefused | None:
        """The reason nothing may start, or ``None``."""
        if self.live_agents and self.tmux_found:
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
        return None


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
    unrecorded: bool = False
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


def purge_refusal(home: Path, *, custom: bool) -> str | None:
    """Why ``home`` must not be deleted, or ``None`` when ``--purge`` may delete it.

    Every check is about the one mistake that cannot be undone — a recursive
    delete of the wrong directory: a link (#198 plans links between account slots
    and ~/.claude, and a delete led through one would take the target), the
    user's home or anything above it, a directory with none of aisquare's
    markers, and — when AISQUARE_HOME moved the home somewhere custom — any
    entry aisquare did not create.
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
        try:
            names = sorted(child.name for child in home.iterdir())
        except OSError as exc:
            return f"{home} could not be listed ({exc})"
        foreign = [n for n in names if n not in _HOME_NAMES and not _ATOMIC_TEMP.match(n)]
        if foreign:
            shown = ", ".join(foreign[:5]) + (
                f" and {len(foreign) - 5} more" if len(foreign) > 5 else ""
            )
            return (
                f"AISQUARE_HOME points at a directory that also holds {shown}, which aisquare "
                "did not create — move them out, or delete the directory by hand"
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


def _runs_aisquare(spec: object) -> bool:
    """Whether an ``mcpServers`` entry starts aisquare: by name, or ``python -m aisquare``.

    Any token counts, not only ``command``: ``uvx --from aisquare-cli aisquare
    serve`` names the program in its arguments. Windows names (``asq.exe``) are
    read as Windows paths whatever this machine is, because the file may have
    been written on either.
    """
    if not isinstance(spec, dict):
        return False
    tokens = [str(spec.get("command") or "")]
    args = spec.get("args")
    if isinstance(args, list):
        tokens.extend(str(arg) for arg in args)
    for index, token in enumerate(tokens):
        if PureWindowsPath(token).stem.lower() in ("aisquare", "asq"):
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


def _live_fleet_agents() -> tuple[tuple[str, ...], str | None]:
    """The fleet's live rows as ``label (project)``, or why they could not be read.

    The store is opened only when ``context.db`` exists and is not empty: opening
    creates a missing home, and an empty file is rebuilt by ``open_store`` (its
    truncation path writes a marker). A store that cannot answer is reported,
    never raised — the plan reads, and a damaged board is no reason to keep a
    user from removing the tool.
    """
    database = paths.db_path()
    try:
        if not database.is_file() or database.stat().st_size == 0:
            return (), None
    except OSError as exc:
        return (), str(exc)
    try:
        with store_session() as store:
            live = [
                f"{agent.label} ({project.codename or project.root.name})"
                for project in store.list_projects(all=True, include_forgotten=True)
                for agent in store.fleet_agents(project.id, live_only=True)
            ]
    except Exception as exc:  # StoreUnopenable, a corrupt page, a lock: report it
        return (), str(exc) or type(exc).__name__
    return tuple(live), None


def uninstall_plan(*, purge: bool = False) -> UninstallPlan:
    """Decide what ``aisquare uninstall`` would do. Reads only; never creates the home."""
    route = install_route.detect()
    candidates = [*agent_core.hook_dirs(HOOK_AGENT), *_account_dirs()]
    hooks: list[HookSite] = []
    unreadable: list[HookSite] = []
    seen: set[Path] = set()
    for directory in candidates:
        key = agent_core.dir_identity(directory)
        if key in seen:
            continue
        seen.add(key)
        try:
            commands = agent_core.hook_commands(HOOK_AGENT, directory)
        except OSError as exc:
            unreadable.append(
                HookSite(directory, reason=f"its settings.json could not be read ({exc})")
            )
            continue
        if commands:
            programs: list[str] = []
            for command in commands:
                binary = agent_core.hook_binary(command)
                if binary is not None and str(binary.program) not in programs:
                    programs.append(str(binary.program))
            hooks.append(HookSite(directory, tuple(programs)))
    home = paths.aisquare_home()
    try:
        entries = tuple(sorted(c.name for c in home.iterdir())) if home.is_dir() else ()
    except OSError:
        entries = ()
    live, fleet_error = _live_fleet_agents()
    custom = bool(os.environ.get(paths.HOME_ENV_VAR))
    return UninstallPlan(
        route=route,
        hooks=tuple(hooks),
        unreadable=tuple(unreadable),
        mcp=_mcp_registrations(candidates),
        package_argv=tuple(install_route.remove_argv(route)),
        package_env=install_route.installer_env(route),
        package_reason=install_route.not_removable(route),
        home=home,
        home_exists=home.exists() or home.is_symlink(),
        home_entries=entries,
        accounts=_accounts_kept(),
        purge=purge,
        purge_refusal=purge_refusal(home, custom=custom),
        live_agents=live,
        fleet_error=fleet_error,
        tmux_found=_tmux_on_path(),
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
    unrecorded = False
    if not plan.purge and paths.agents_registry_path().is_file():
        # The home stays, so it should not claim a connection that is gone. Only
        # because agents.json already exists: set_connected creates the home.
        try:
            for removal in removals:
                if removal.ok:
                    agent_core.set_connected(HOOK_AGENT, False, removal.config_dir)
            unrecorded = True
        except Exception:  # agents.json is a record, not the hooks: never a reason to stop
            unrecorded = False
    purged, purge_error = False, None
    hooks_failed = any(not removal.ok for removal in removals)
    if plan.purge and plan.home_exists and not hooks_failed:
        purged, purge_error = _purge(plan.home)
    elif plan.purge and hooks_failed:
        purge_error = "not attempted: hooks were left in a directory above"
    return UninstallReport(
        plan,
        hooks=tuple(removals),
        unrecorded=unrecorded,
        purged=purged,
        purge_error=purge_error,
        notes=tuple(_uninstall_notes(plan, removals)),
    )


def _purge(home: Path) -> tuple[bool, str | None]:
    """Delete the home, after asking the guard again right before the delete.

    ``shutil.rmtree`` never follows a link inside the tree — it removes the link —
    and refuses a link at the top; the guard has already refused that one.
    """
    refusal = purge_refusal(home, custom=bool(os.environ.get(paths.HOME_ENV_VAR)))
    if refusal is not None:
        return False, refusal
    try:
        shutil.rmtree(home)
    except OSError as exc:
        return False, f"{home} was only partly deleted ({exc})"
    return True, None


def _uninstall_notes(plan: UninstallPlan, removals: Iterable[HookRemoval]) -> list[str]:
    notes: list[str] = []
    if any(removal.ok for removal in removals):
        notes.append("open Claude Code sessions keep the hooks they started with — restart them")
    if plan.mcp:
        names = ", ".join(sorted({entry.name for entry in plan.mcp}))
        notes.append(
            f"MCP servers that run aisquare are still registered ({names}); Claude Code owns "
            "that file — remove each with: claude mcp remove <name>"
        )
    notes.append(
        "a running `aisquare serve` keeps running until you stop it; uv, tmux, Node, gh "
        "and Claude Code stay installed"
    )
    return notes


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
