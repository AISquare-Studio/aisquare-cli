"""The Explainability tab: both tracing lanes at a glance, and the switches as buttons.

Everything here goes through the services behind ``cli/explainability.py`` —
what ``status``, ``enable``, ``disable``, ``register`` and ``ship`` call — and
nothing prints or prompts. Consent is a button: tracing turns on because the
user pressed **Enable**, never because a dialog asked (#50's boundary — nothing
ships before the user configured it). Work that reaches the network — the roster
registration, a drain of the spool, the proxy probe — runs in a thread worker
so the UI never blocks on a gateway; results arrive as notifications, the lines
the CLI would have printed.

The ``env`` block is deliberately NOT rendered. Its ``ANTHROPIC_CUSTOM_HEADERS``
carries the workspace key for a hosted proxy and ``cli/explainability.py`` says
it is not safe for scrollback or a screen-share; a full-screen UI is both.
``aisquare explainability env <role>`` stays the way to get it, into ``eval``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.core import outbox
from aisquare.core.config import AppConfig, load_config, save_config
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops as ops
from aisquare.services.explainability import RESERVED_ENV_VARS

STATUS_WORKER = "explainability-status"
REGISTER_WORKER = "explainability-register"
SHIP_WORKER = "explainability-ship"
SHIP_LIMIT = 500
"""Most records one press of Ship drains — the CLI's default."""

Severity = Literal["information", "warning", "error"]


@dataclass(frozen=True)
class Notice:
    """What a finished piece of work has to say, and how loudly."""

    message: str
    severity: Severity = "information"


@dataclass(frozen=True)
class StatusReport:
    """The facts ``aisquare explainability status`` prints, gathered off the UI thread."""

    rows: tuple[tuple[str, str], ...]
    problem: bool
    """Tracing is on and the proxy would not take a session — the red state."""


def status_report() -> StatusReport:
    """Gather what ``status`` shows: the proxy lane, the client lane, the spool.

    The probe dials only when tracing is on (``ops.proxy_state`` decides, as it
    does for the CLI), so a machine that never asked for tracing costs nothing.
    """
    config = load_config()
    settings = config.explainability
    target = ops.resolve_target(settings, None)
    proxy = ops.proxy_state(target, on=settings.enabled)
    shipping = explainability_service.shipping_state()
    try:
        queue_dir: str | None = str(outbox.queue_dir())
    except Exception:  # decoration on a status line; a bad home costs the path, not the tab
        queue_dir = None
    located = f" — {queue_dir}" if queue_dir else ""
    rows = (
        ("enabled", "on" if settings.enabled else "off"),
        ("target", target.name),
        ("gateway", f"{target.gateway_url or '(unset)'} [{target.gateway_source}]"),
        ("key", f"{target.key_origin} {'is set' if target.api_key else 'is NOT set'}"),
        ("proxy", target.proxy_url),
        ("identity", target.agent_name_template),
        ("agents", ", ".join(target.agent_names) or "(none)"),
        ("probe", proxy.summary),
        ("shipping", shipping.reason),
        (
            "spool",
            f"{shipping.queued} queued, {shipping.sent} sent, {shipping.dead} dead-letter{located}",
        ),
        ("redaction", ops.redaction_summary(config.redaction.level)),
    )
    return StatusReport(rows=rows, problem=settings.enabled and not proxy.healthy)


def render_status(report: StatusReport) -> Text:
    text = Text()
    width = max(len(label) for label, _ in report.rows) + 1
    for label, value in report.rows:
        text.append(f"{label + ':':<{width}} ", style="bold")
        style = "bold red" if report.problem and label == "probe" else ""
        text.append(f"{value}\n", style=style)
    return text


def register_roster() -> Notice:
    """What ``aisquare explainability register`` does, as a notice instead of an exit code."""
    settings = load_config().explainability
    target = ops.resolve_target(settings, None)
    if not target.gateway_url:
        return Notice(
            f"target '{target.name}' has no gateway URL — set one with: "
            f"aisquare explainability enable --target {target.name} --gateway-url <url>",
            "error",
        )
    if not target.api_key:
        return Notice(
            f"${target.api_key_env} is not set in this environment — export the workspace key "
            "there (it is never stored by the CLI) and start the UI again",
            "error",
        )
    names = target.agent_names
    if not names:
        return Notice("no agent identities to register — check explainability.roles", "error")
    verdict = ops.register_roster(target, names)
    if not verdict.ok:
        return Notice(
            f"registration refused by {target.gateway_url}: {verdict.detail} — a workspace key "
            "is required here; a studio-scoped key cannot declare a roster",
            "error",
        )
    published = ops.publication_ids(verdict.payload)
    lines = [f"✓ registered {len(names)} identities with target '{target.name}'"]
    for agent_name in names:
        publication = published.get(agent_name)
        lines.append(
            f"{agent_name}: {f'publication_id {publication}' if publication else 'registered'}"
        )
    if not published:
        lines.append("(the workspace returned no publication ids; re-run after it syncs)")
    return Notice("\n".join(lines))


def ship_spool() -> Notice:
    """One drain of the spool, as ``aisquare explainability ship`` would report it."""
    report = explainability_service.ship_once(limit=SHIP_LIMIT)
    message = report.reason
    if report.runs:
        message += f"\nruns: {', '.join(report.runs)}"
    if report.dead:
        return Notice(message, "error")
    return Notice(message, "warning" if report.blocked else "information")


def stale_shell_export(config: AppConfig) -> str | None:
    """The ``ANTHROPIC_BASE_URL`` this process still carries for the proxy just switched off.

    The same rule ``disable`` applies: only when the value IS the proxy this
    machine was configured to use, and only when that proxy was chosen — the
    shipped loopback default is someone else's long-running proxy and not ours
    to warn about. ``None`` when nothing is stale.
    """
    target = ops.resolve_target(config.explainability, None)
    ambient = os.environ.get(RESERVED_ENV_VARS[0])
    if ambient and ambient == target.proxy_url and target.proxy_source != "default":
        return ambient
    return None


class ExplainabilityView(VerticalScroll):
    """Status of the tracing lanes and the buttons that change them."""

    DEFAULT_CSS = """
    ExplainabilityView { padding: 1 2; }
    ExplainabilityView #explainability-status { height: auto; margin-bottom: 1; }
    ExplainabilityView #explainability-actions { height: auto; }
    ExplainabilityView #explainability-actions Button { margin-right: 1; }
    ExplainabilityView #explainability-note { height: auto; margin-top: 1; color: $text-muted; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.status_text = ""
        """The plain text of the status block (what a test reads)."""

    def compose(self) -> ComposeResult:
        yield Static(Text("reading the tracing state…", style="dim"), id="explainability-status")
        with Horizontal(id="explainability-actions"):
            yield Button("Enable tracing", id="explainability-enable", variant="primary")
            yield Button("Disable", id="explainability-disable")
            yield Button("Register roster", id="explainability-register")
            yield Button("Ship spool", id="explainability-ship")
            yield Button("Refresh", id="explainability-refresh")
        yield Static(
            Text(
                "Enable is the consent: sessions launched after it are traced through the "
                "proxy. Ship drains the insights this CLI buffered (prompts, board notes, task "
                "events — no file contents, no model traffic). The shell exports for a terminal "
                "stay in `aisquare explainability env <role>`: they carry a key.",
            ),
            id="explainability-note",
        )

    def on_mount(self) -> None:
        self.refresh_status()

    # --- reading ---------------------------------------------------------------------

    def refresh_status(self) -> None:
        """Re-read both lanes off the UI thread (the probe may dial the proxy)."""
        self.run_worker(
            status_report, name=STATUS_WORKER, group=STATUS_WORKER, exclusive=True, thread=True,
            exit_on_error=False,
        )  # fmt: skip

    def _show_status(self, report: StatusReport) -> None:
        rendered = render_status(report)
        self.status_text = rendered.plain
        self.query_one("#explainability-status", Static).update(rendered)

    # --- the switches (config writes, on the UI thread: local and immediate) -------------

    def _read_config(self) -> AppConfig | None:
        try:
            return load_config()
        except Exception as exc:  # a broken config.toml: say so, change nothing
            self.notify(f"config unreadable — nothing changed: {exc}", severity="error", timeout=8)
            return None

    def _write_config(self, config: AppConfig) -> bool:
        """Persist through the one writer; a refused write is a notice, not a crash."""
        try:
            save_config(config)
        except OSError as exc:  # the operator's filesystem saying no — the foreseeable failure
            self.notify(f"could not write the config: {exc}", severity="error", timeout=8)
            return False
        return True

    @on(Button.Pressed, "#explainability-enable")
    def _turn_tracing_on(self) -> None:
        """What ``aisquare explainability enable`` does with no options."""
        config = self._read_config()
        if config is None:
            return
        config.explainability.enabled = True
        if not self._write_config(config):
            return
        resolved = ops.resolve_target(config.explainability, None)
        self.notify(
            f"✓ tracing enabled for target '{resolved.name}' — next: aisquare doctor --live",
            timeout=6,
        )
        self.refresh_status()

    @on(Button.Pressed, "#explainability-disable")
    def _turn_tracing_off(self) -> None:
        """What ``aisquare explainability disable`` does: off, targets kept."""
        config = self._read_config()
        if config is None:
            return
        config.explainability.enabled = False
        if not self._write_config(config):
            return
        self.notify(
            "✓ tracing disabled — sessions launch untraced, targets left in place", timeout=6
        )
        stale = stale_shell_export(config)
        if stale is not None:
            self.notify(
                f"this process still exports {RESERVED_ENV_VARS[0]}={stale} — launches from "
                f"here keep using the proxy and will fail once it stops; unset "
                f"{' '.join(RESERVED_ENV_VARS)} in the shell that started the UI",
                severity="warning",
                timeout=10,
            )
        self.refresh_status()

    # --- the network work (thread workers; results are notices) -------------------------

    @on(Button.Pressed, "#explainability-register")
    def _register(self) -> None:
        self._start_network_work(REGISTER_WORKER, register_roster)

    @on(Button.Pressed, "#explainability-ship")
    def _ship(self) -> None:
        self._start_network_work(SHIP_WORKER, ship_spool)

    @on(Button.Pressed, "#explainability-refresh")
    def _refresh(self) -> None:
        self.refresh_status()

    def _start_network_work(self, name: str, work: Callable[[], Notice]) -> None:
        self._set_buttons(disabled=True)
        self.run_worker(
            work, name=name, group=name, exclusive=True, thread=True, exit_on_error=False
        )

    def _set_buttons(self, *, disabled: bool) -> None:
        for button_id in ("#explainability-register", "#explainability-ship"):
            self.query_one(button_id, Button).disabled = disabled

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        name = event.worker.name
        if name == STATUS_WORKER:
            if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, StatusReport):
                self._show_status(event.worker.result)
            elif event.state is WorkerState.ERROR:
                self.query_one("#explainability-status", Static).update(
                    Text(f"could not read the tracing state: {event.worker.error}", style="red")
                )
            return
        if name not in (REGISTER_WORKER, SHIP_WORKER):
            return
        if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, Notice):
            notice = event.worker.result
            self.notify(notice.message, severity=notice.severity, timeout=10)
            self.refresh_status()
        elif event.state is WorkerState.ERROR:
            self.notify(f"{name.removeprefix('explainability-')} failed: {event.worker.error}",
                        severity="error", timeout=10)  # fmt: skip
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self._set_buttons(disabled=False)
