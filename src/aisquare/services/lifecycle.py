"""Setup, upgrade and removal of aisquare itself."""

from __future__ import annotations

from pathlib import Path

from aisquare.core import credentials as credentials_store
from aisquare.core import paths
from aisquare.core.config import (
    AppConfig,
    ExplainabilitySettings,
    load_config,
    save_config,
)
from aisquare.core.store import store_session
from aisquare.core.stubs import stub
from aisquare.core.workspace import current_project
from aisquare.models import SetupReport
from aisquare.services import agents as agents_service
from aisquare.services import explainability as explainability_service
from aisquare.services import project as project_service


class ExplainabilityResetRefused(RuntimeError):
    """``--reinit`` would discard a configured explainability section.

    Raised rather than warned because the loss is not recoverable from anything
    on the machine: ``[explainability.targets]`` holds a gateway URL and the
    NAME of the environment variable holding the key, both configured out of
    band. Afterwards ``status`` reads as a plausible *unconfigured* machine
    rather than a broken one, so nothing downstream reports it.

    Not raised when the config cannot be parsed: ``doctor`` sends an operator
    here to reset an invalid file, and a refusal built on a section we cannot
    read would strand exactly that person.
    """

    def __init__(self, summary: str) -> None:
        super().__init__(summary)
        self.summary = summary


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
    if reinit and paths.config_path().exists():
        try:
            existing = load_config().explainability
        except Exception:
            existing = None  # unreadable: --reinit is the documented recovery
        if existing is not None:
            discarded = _configured_explainability(existing)
            if discarded and not assume_yes:
                raise ExplainabilityResetRefused(discarded)

    if reinit or not paths.config_path().exists():
        save_config(AppConfig())

    project = current_project(path)
    with store_session() as store:
        store.ensure_project(project)

    notes: list[str] = []
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
        if restricted:
            notes.append("Stored API key in ~/.aisquare/credentials.")
        else:
            notes.append(
                "Stored API key in ~/.aisquare/credentials — but could NOT restrict it to "
                "your account; other users on this machine may be able to read it."
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


def upgrade() -> None:
    """Upgrade aisquare and refresh its agent hooks."""
    stub("upgrade")


def uninstall() -> None:
    """Remove agent hooks and optionally wipe local data."""
    stub("uninstall")
