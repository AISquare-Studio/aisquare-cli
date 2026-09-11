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
from textual.widgets import Button, Input, Label, Static
from textual.worker import Worker, WorkerState

from aisquare.core import outbox
from aisquare.core.config import AppConfig, load_config, save_config
from aisquare.models import CheckStatus
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
    caution: bool = False
    """Tracing is on, sessions ARE traced, and something about where they land
    could not be checked from here — amber, which this tab used to render as
    green because it read ``healthy`` alone."""


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
        (
            "probe",
            proxy.summary
            + (
                f"\n  → {proxy.remediation}"
                if proxy.remediation and proxy.severity is not CheckStatus.ok
                else ""
            ),
        ),
        ("shipping", shipping.reason),
        (
            "spool",
            f"{shipping.queued} queued, {shipping.sent} sent, {shipping.dead} dead-letter{located}",
        ),
        ("redaction", ops.redaction_summary(config.redaction.level)),
    )
    return StatusReport(
        rows=rows,
        problem=settings.enabled and not proxy.healthy,
        caution=settings.enabled and proxy.severity is CheckStatus.warn,
    )


def render_status(report: StatusReport) -> Text:
    text = Text()
    width = max(len(label) for label, _ in report.rows) + 1
    for label, value in report.rows:
        text.append(f"{label + ':':<{width}} ", style="bold")
        style = ""
        if label == "probe":
            style = "bold red" if report.problem else ("yellow" if report.caution else "")
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
    # The TUI is the product's primary path: without this the operator presses
    # register, reads "✓ registered 7 identities" and is told nothing about the
    # eighth role their config.toml never gained.
    unlisted = ops.unregistered_roles(target)
    if unlisted:
        lines.append(f"note: {ops.unregistered_roles_note(unlisted, target)}")
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


def _has_scheme(url: str) -> bool:
    """Whether ``url`` names a scheme this CLI can hand to an agent.

    A bare host is the mistake the form has to catch rather than store: it
    parses as a PATH, so every later reader sees no host at all.
    """
    split = explainability_service.split_url(url)
    return split is not None and split.scheme in ("http", "https")


def _configured_proxy(config: AppConfig, target_name: str) -> str:
    """The proxy ALREADY stored for the target the form is about to write.

    Read from the target the save will land in -- which is the typed name when
    one was given, and the active target otherwise -- so a correction to one
    deployment cannot be judged against another's settings.
    """
    settings = config.explainability
    name = target_name or settings.target
    existing = settings.targets.get(name)
    return (existing.proxy_url if existing else "") or ""


class ExplainabilityView(VerticalScroll):
    """Status of the tracing lanes and the buttons that change them."""

    DEFAULT_CSS = """
    ExplainabilityView { padding: 1 2; }
    ExplainabilityView #explainability-status { height: auto; margin-bottom: 1; }
    ExplainabilityView #explainability-actions { height: auto; }
    ExplainabilityView #explainability-actions Button { margin-right: 1; }
    ExplainabilityView #explainability-note { height: auto; margin-top: 1; color: $text-muted; }
    ExplainabilityView .setup-row { height: auto; margin-bottom: 1; }
    ExplainabilityView .setup-row Label { width: 18; padding-top: 1; }
    ExplainabilityView .setup-row Input { width: 1fr; }
    ExplainabilityView #explainability-setup-title { margin-top: 1; text-style: bold; }
    ExplainabilityView #explainability-setup-note { height: auto; color: $text-muted; }
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
        yield Static(Text("Setup"), id="explainability-setup-title")
        yield Static(
            Text(
                "Everything a machine needs to start tracing. Blank leaves a field as it is, "
                "so this is also how one setting is changed later. The key is written to "
                "~/.aisquare/explainability-key at mode 600 and never shown back.",
            ),
            id="explainability-setup-note",
        )
        with Horizontal(classes="setup-row"):
            yield Label("deployment")
            yield Input(placeholder="stg", id="explainability-target")
        with Horizontal(classes="setup-row"):
            yield Label("gateway URL")
            yield Input(placeholder="https://…", id="explainability-gateway")
        with Horizontal(classes="setup-row"):
            yield Label("proxy URL")
            yield Input(placeholder="blank = the hosted proxy", id="explainability-proxy")
        with Horizontal(classes="setup-row"):
            yield Label("your prefix")
            yield Input(placeholder="e.g. arbind", id="explainability-prefix")
        with Horizontal(classes="setup-row"):
            yield Label("key variable")
            yield Input(placeholder="EXPLAINABILITY_API_KEY", id="explainability-key-env")
        with Horizontal(classes="setup-row"):
            yield Label("workspace key")
            yield Input(placeholder="AIS_…", password=True, id="explainability-key")
        with Horizontal(classes="setup-row"):
            yield Button("Save setup", id="explainability-save", variant="success")

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
            self.notify(
                f"config unreadable — nothing changed: {exc}",
                severity="error",
                timeout=8,
                markup=False,
            )
            return None

    def _write_config(self, config: AppConfig) -> bool:
        """Persist through the one writer; a refused write is a notice, not a crash."""
        try:
            save_config(config)
        except OSError as exc:  # the operator's filesystem saying no — the foreseeable failure
            # markup=False, as everywhere in this view: the message carries a
            # path and an OS string, and a toast parses markup by default —
            # ``[Errno 30] Read-only file system: '/home/me/[work]/…'`` reached
            # the screen naming a directory that does not exist, and a stray
            # ``[/x]`` raises MarkupError inside Toast.render.
            self.notify(
                f"could not write the config: {exc}", severity="error", timeout=8, markup=False
            )
            return False
        return True

    @on(Button.Pressed, "#explainability-save")
    def _save_setup(self) -> None:
        """Everything a new machine needs, in one press — #131's second half.

        The four settings and the key were reachable only from a shell, while
        this tab could already SEE that they were missing: it rendered "key is
        NOT set" beside a red probe and offered no way to act on either. An
        external adopter's first contact with tracing was a runbook of flags,
        and the one they could not guess (the hosted proxy's port, beside the
        gateway) is the one that decides whether their Runs arrive.

        A blank field changes nothing, so this is equally how one setting is
        corrected later. The write is ``configure_target`` -- the same function
        ``aisquare explainability enable`` calls -- because two writers for one
        config file agree right up until they do not.
        """
        target = self.query_one("#explainability-target", Input).value.strip()
        gateway = self.query_one("#explainability-gateway", Input).value.strip()
        proxy = self.query_one("#explainability-proxy", Input).value.strip()
        prefix = self.query_one("#explainability-prefix", Input).value.strip()
        key_env = self.query_one("#explainability-key-env", Input).value.strip()
        key_field = self.query_one("#explainability-key", Input)
        key = key_field.value.strip()
        if not any((target, gateway, proxy, prefix, key_env, key)):
            self.notify("nothing to save — every field is blank", severity="warning", timeout=6)
            return

        # A schemeless host parses with the WHOLE string as the path: no host,
        # so no suggestion, and `is_loopback` reads the empty host as local and
        # suppresses the very caution that would have flagged it. The operator
        # ends configured, green and stranded -- this form's own failure mode,
        # reached by omitting four characters. Refuse instead.
        if gateway and not _has_scheme(gateway):
            self.notify(
                f"gateway needs a scheme — try https://{gateway}",
                severity="warning",
                timeout=8,
                markup=False,
            )
            return
        if proxy and not _has_scheme(proxy):
            self.notify(
                f"proxy needs a scheme — try https://{proxy}",
                severity="warning",
                timeout=8,
                markup=False,
            )
            return
        # The field asks for a NAME. An operator who has read the `--identity`
        # examples types `nishil-{role}` and would get `nishil-{role}-{role}`,
        # which renders as `nishil-coder-coder`; a stray `{` makes every
        # `.format(role=...)` raise, `agent_names` come back empty, and Register
        # point at the wrong setting. Take the name out of what they typed.
        if prefix and ("{" in prefix or "}" in prefix):
            cleaned = prefix.split("{")[0].rstrip("-_ ")
            if not cleaned:
                self.notify(
                    "prefix is a name, not a template — try 'nishil', not 'nishil-{role}'",
                    severity="warning",
                    timeout=8,
                    markup=False,
                )
                return
            self.notify(
                f"prefix is a name, not a template — using '{cleaned}'",
                severity="warning",
                timeout=8,
                markup=False,
            )
            prefix = cleaned

        config = self._read_config()
        if config is None:
            return
        # Offered, not imposed -- and offered only where there is nothing to
        # overwrite. The test was `not proxy`, the BLANK FIELD, so a target with
        # a deliberate proxy whose gateway the operator merely corrected had it
        # silently replaced: the opposite of the "a blank field changes nothing"
        # contract printed above this form, which the service-level test asserts
        # one layer down. `hosted_proxy_for` is silent for a loopback gateway,
        # whose own convention is the shipped 9090 default and not 9443.
        if gateway and not proxy and not _configured_proxy(config, target):
            suggested = explainability_service.hosted_proxy_for(gateway)
            if suggested is not None:
                proxy = suggested
        identity = f"{prefix}-{{role}}" if prefix else None
        name = explainability_service.configure_target(
            config,
            target_name=target or None,
            gateway_url=gateway or None,
            key_env=key_env or None,
            proxy_url=proxy or None,
            identity=identity,
            enable=False,
        )
        if not self._write_config(config):
            return
        # The key AFTER the config: a written key with no target to use it is
        # inert, while a target whose key failed to land is a red check that
        # names its own fix. The cheaper failure is the one left behind.
        if key:
            try:
                explainability_service.store_api_key(key)
            except OSError as exc:
                self.notify(
                    f"settings saved, but the key could not be written: {exc}",
                    severity="error",
                    timeout=8,
                    markup=False,
                )
                self.refresh_status()
                return
        key_field.value = ""  # never rendered back, not even masked
        self.notify(
            f"✓ setup saved for target '{name}' — press Enable tracing, then Register roster",
            timeout=8,
            markup=False,
        )
        self.refresh_status()

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
            markup=False,
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
            "✓ tracing disabled — sessions launch untraced, targets left in place",
            timeout=6,
            markup=False,
        )
        stale = stale_shell_export(config)
        if stale is not None:
            self.notify(
                f"this process still exports {RESERVED_ENV_VARS[0]}={stale} — launches from "
                f"here keep using the proxy and will fail once it stops; unset "
                f"{' '.join(RESERVED_ENV_VARS)} in the shell that started the UI",
                severity="warning",
                timeout=10,
                markup=False,
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
            # markup=False: a Notice carries gateway prose, a key path and env
            # names — ``ship_once``'s "set $KEY or write /home/me/[work]/…/key"
            # loses the bracketed directory when parsed as markup.
            self.notify(notice.message, severity=notice.severity, timeout=10, markup=False)
            self.refresh_status()
        elif event.state is WorkerState.ERROR:
            self.notify(f"{name.removeprefix('explainability-')} failed: {event.worker.error}",
                        severity="error", timeout=10, markup=False)  # fmt: skip
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self._set_buttons(disabled=False)
