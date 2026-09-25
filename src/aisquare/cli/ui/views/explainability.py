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
from functools import partial
from typing import Literal

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Label, Static
from textual.worker import Worker, WorkerState

from aisquare.core import orchestrator, outbox, paths
from aisquare.core.config import AppConfig, ExplainabilityTarget, load_config, save_config
from aisquare.core.store import store_session
from aisquare.models import CheckStatus, ProjectInfo
from aisquare.services import credits as credits_service
from aisquare.services import destinations, iam
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops as ops
from aisquare.services.explainability import KEY_ENV_VAR, RESERVED_ENV_VARS

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
    severity: CheckStatus = CheckStatus.ok
    """The probe row's verdict while tracing is on — ``ProxyState.severity``, the
    one vocabulary every surface speaks — and ``ok`` while it is off. This
    carried ``problem`` and ``caution`` as two booleans derived from it, the
    encoding ``ProxyState`` itself had just shed for being able to say both at
    once (review of #132). Nothing derives a boolean from it: ``render_status``
    reads the severity, and a derived ``problem`` re-offered the encoding just
    removed (round 5)."""


def key_project(page: ProjectInfo | None) -> ProjectInfo | None:
    """The project whose key a launch from ``page`` authenticates with (#141).

    A fleet window runs ``launch`` in the page's root, and ``launch`` joins
    ``orchestrator.team_project`` from there — ``AISQUARE_TEAM_HUB`` when it is
    set, else this checkout — the one answer the CLI's ``key``, ``env``,
    ``status`` and ``register`` give. This tab answered with the page itself,
    so under a hub it showed and attached the page's key while the page's
    agents launched with the hub's (review of #170). The row names whichever
    project it is, so a hub is visible as the hub.
    """
    return orchestrator.team_project(page.root) if page is not None else None


def status_report(page: ProjectInfo | None = None) -> StatusReport:
    """Gather what ``status`` shows: the proxy lane, the client lane, the spool.

    The probe dials only when tracing is on (``ops.proxy_state`` decides, as it
    does for the CLI), so a machine that never asked for tracing costs nothing.
    The key is resolved for the project ``page``'s launches join (:func:`key_project`).
    """
    project = key_project(page)
    config = load_config()
    settings = config.explainability
    target = ops.resolve_target(settings, None, project_id=project.id if project else None)
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
        # "lands in" rather than the CLI's "destination": the view's label column is
        # as wide as its longest label plus one, and a longer word re-pads every
        # row (tests pin "enabled:   on"). Same renderer, same sentence.
        ("lands in", destinations.describe(target.destination, key_source=target.key_source)),
        ("credits", _credits_row(target)),
        ("key", f"{target.key_origin} {'is set' if target.api_key else 'is NOT set'}"),
        ("project", _project_key_row(project, target)),
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
    return StatusReport(rows=rows, severity=proxy.severity if settings.enabled else CheckStatus.ok)


def _credits_row(target: ops.ResolvedTarget) -> str:
    """The destination workspace's credits (#143) — cached a minute, off the UI thread.

    The row is always drawn, so when a destination IS chosen but there is
    nothing to ask with, it says which is missing. ``describe(None)`` used to
    answer "(no destination chosen)" there, directly under the ``lands in``
    row naming that destination (review of #173, round 1). The CLI, whose
    line is optional, leaves it out instead.
    """
    destination = target.destination
    if destination is None:
        return "(no destination chosen)"
    try:
        session = iam.current_session()
    except iam.IamError:
        session = None
    if session is None:
        return "(sign in to read them — aisquare login)"
    reading = credits_service.for_destination(session, destination)
    if reading is None and session.source == "env":
        # `aisquare login` refuses while the variable is set (`env_token_set`):
        # the token goes to the API the environment names, so that is the fix,
        # with a token that API issued. The one set now most likely came from
        # the other server (docs/signing-in.md: set it only where every command
        # talks to the server that issued it); pointed at this API alone, the
        # row read a 401 instead (review of #173, round 2).
        return (
            f"({iam.TOKEN_ENV_VAR} is used with {session.api_url}, not this workspace's API — "
            f"set {iam.API_URL_ENV_VAR}={destination.api_url} and a token that API issued "
            "to read them)"
        )
    if reading is None:  # the session belongs to another API than the workspace's
        return (
            f"(signed in to {session.api_url}, not this workspace's API — "
            f"aisquare login --api-url {destination.api_url} to read them)"
        )
    return credits_service.describe(reading)


def _project_key_row(project: ProjectInfo | None, target: ops.ResolvedTarget) -> str:
    """``<name>: its own key for stg`` / ``<name>: the machine key`` — the origin per project."""
    if project is None:
        return "(no project)"
    name = project.root.name or project.id
    binding = ops.project_key_binding(project.id)
    if binding is None:
        return (
            f"{name}: no key of its own — the machine's applies (attach one below: the "
            "workspace key, 'this project only' ticked)"
        )
    if binding.target != target.name:
        state = f"not used for target {target.name}"
    elif target.key_source == "project":
        state = "in use"
    else:
        # Bound to THIS target and still not the answer: the file is gone or
        # unreadable. `key show`'s words for it — this row used to say "not used
        # for target stg" about the very target it is bound to (review of #170).
        state = f"file MISSING or unreadable at {binding.key_path} — attach it again below"
    return f"{name}: its own key for target {binding.target} ({state})"


_MINTED_KEY_REFUSAL = Notice(
    "this project's key was minted by the CLI — replace it with "
    "aisquare explainability key set, which revokes the minted one",
    "warning",
)


def minted_key_refusal(project: ProjectInfo) -> Notice | None:
    """The refusal when ``project``'s key file holds a key the CLI minted (#142), else ``None``.

    Replacing it here would owe that key a revocation — a network call, and
    this tab's handlers run on the UI thread — so the CLI does it instead:
    ``key set`` revokes the minted key once its own is written. The form asks
    this BEFORE any write of its own, so a refusal keeps what was typed; the
    writer asks again inside its own session (:func:`attach_project_key`).

    No ``context.db`` means nothing was ever minted here, and the answer is
    ``None`` without opening one: a store session creates the database, and on
    a fresh machine this read ran before anything was saved (review of #172).
    """
    if not paths.db_path().exists():
        return None
    with store_session() as store:
        minted = store.project_destination(project.id)
    if minted is None or not minted.key_uid:
        return None
    return _MINTED_KEY_REFUSAL


def attach_project_key(value: str, project: ProjectInfo, target: str) -> Notice:
    """What *Save setup* does with a key and *this project only* ticked (#141).

    ``project`` is the one the page's agents launch into (:func:`key_project`),
    never the ``project switch`` pin (review of #170), and ``target`` is the
    deployment the form resolved for the key. It was a separate *Attach key*
    field, bound to the active target, beside #131's Setup form and its own key
    field: two key inputs on one tab, writing to two places under two rules for
    the deployment. One field now, and the box says whose key it is.

    Never over a key the CLI minted (#142): the writer refuses it in its own
    session (``refuse_minted``), nothing is written, and the refusal
    :func:`minted_key_refusal` gives is returned instead — so no caller of this
    writer can skip the check, and it costs no store session of its own.
    """
    try:
        binding = ops.attach_project_key(project, value.strip(), target=target, refuse_minted=True)
    except ops.MintedKeyInPlace:
        return _MINTED_KEY_REFUSAL
    name = project.root.name or project.id
    return Notice(
        f"✓ key attached to {name} for target {target} — {binding.key_path} (mode 600); "
        "launches in this project authenticate the proxy with it. If that workspace has not "
        "registered this machine's agents yet, press Register roster",
        "information",
    )


#: The probe row's style per verdict — one mapping, so a new severity is one
#: entry here and not a third branch of a nested conditional.
_PROBE_STYLES: dict[CheckStatus, str] = {CheckStatus.fail: "bold red", CheckStatus.warn: "yellow"}


def render_status(report: StatusReport) -> Text:
    text = Text()
    width = max(len(label) for label, _ in report.rows) + 1
    for label, value in report.rows:
        text.append(f"{label + ':':<{width}} ", style="bold")
        style = ""
        if label == "probe":
            style = _PROBE_STYLES.get(report.severity, "")
        text.append(f"{value}\n", style=style)
    return text


def register_roster(page: ProjectInfo | None = None) -> Notice:
    """What ``aisquare explainability register`` does, as a notice instead of an exit code.

    Under the key of the project ``page``'s launches join (:func:`key_project`)
    when that project has its own (#141), as the CLI's ``register`` does:
    registering at machine level left a project pointed at another workspace
    refused 409 on every span (review of #170).
    """
    project = key_project(page)
    settings = load_config().explainability
    target = ops.resolve_target(
        settings, None, project_id=project.id if project is not None else None
    )
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
    under = f" under {target.key_origin}" if target.key_source == "project" else ""
    lines = [f"✓ registered {len(names)} identities with target '{target.name}'{under}"]
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

    def __init__(self, project: ProjectInfo | None = None, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.project = project
        """The page this tab sits on; the key shown is the one its launches use (#141)."""
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
                "so this is also how one setting is changed later. The deployment field names "
                "the entry these settings belong to; this machine keeps using its current one "
                "unless 'make active' is ticked. The key is written to "
                "~/.aisquare/explainability-key at mode 600 and never shown back; with "
                "'this project only' ticked it is this page's project's own key instead (the "
                "hub's under a hub), for that deployment alone.",
            ),
            id="explainability-setup-note",
        )
        with Horizontal(classes="setup-row"):
            yield Label("deployment")
            yield Input(placeholder="stg", id="explainability-target")
            yield Checkbox("make active", id="explainability-switch")
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
            # A key per project (#141), in the one key field: no page, no project to own it.
            yield Checkbox(
                "this project only",
                id="explainability-key-project",
                disabled=self.project is None,
            )
        with Horizontal(classes="setup-row"):
            yield Button("Save setup", id="explainability-save", variant="success")

    def on_mount(self) -> None:
        self.refresh_status()

    # --- reading ---------------------------------------------------------------------

    def refresh_status(self) -> None:
        """Re-read both lanes off the UI thread (the probe may dial the proxy)."""
        self.run_worker(
            partial(status_report, self.project), name=STATUS_WORKER, group=STATUS_WORKER,
            exclusive=True, thread=True, exit_on_error=False,
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
        ``aisquare explainability enable`` calls -- and so is the VALIDATION:
        this handler checked URLs itself for one round, which guarded this door
        and left the CLI's open (``enable --gateway-url stg.example`` stored
        what the form refused). What stays here is what only a form can know:
        which fields were typed, and whether the box that moves the machine
        was ticked.

        The deployment field NAMES the entry these settings belong to. It does
        not move the machine unless "make active" is ticked: an operator on stg
        correcting prod's gateway was moving their machine to prod, silently --
        traffic to a deployment nobody chose, this tab's own headline failure
        arrived at from the other side. ``enable --target`` keeps switching,
        because a flag typed in a shell is the explicit act this box is.

        The key field is also where a project gets its own key (#141): with
        "this project only" ticked, the key is attached to the project this
        page's agents launch into, instead of written to the machine file —
        ``key set``'s write, the one :func:`attach_project_key` makes — and for
        ``key set``'s deployment: the one typed, else the one the project's
        launches resolve — its destination (#142), else an exported
        ``$AISQUARE_EXPLAINABILITY_TARGET``, else the machine's. When that is not
        the deployment the other typed settings go to, the save is refused before
        anything is written: one press wrote a gateway to one deployment and bound
        the key to another (review of #172). Never over a key the CLI minted
        (#142): that is refused before anything is written too.
        """
        target = self.query_one("#explainability-target", Input).value.strip()
        switch = self.query_one("#explainability-switch", Checkbox).value
        gateway = self.query_one("#explainability-gateway", Input).value.strip()
        proxy = self.query_one("#explainability-proxy", Input).value.strip()
        prefix = self.query_one("#explainability-prefix", Input).value.strip()
        key_env = self.query_one("#explainability-key-env", Input).value.strip()
        key_field = self.query_one("#explainability-key", Input)
        key = key_field.value.strip()
        # The project is looked up here, on the press, as every git lookup this tab
        # makes is kept off the page's build. A view with no page has the box
        # disabled, and so no project to own a key.
        own = self.query_one("#explainability-key-project", Checkbox).value
        owner = key_project(self.project) if key and own else None
        typed = any((gateway, proxy, prefix, key_env, key))
        if not typed and not (target and switch):
            message = (
                f"nothing to save for target '{target}' — every other field is blank; tick "
                "'make active' to switch this machine to it"
                if target
                else "nothing to save — every field is blank"
            )
            self.notify(message, severity="warning", timeout=8, markup=False)
            return

        # The writer's own question about the key variable, asked FIRST — it is
        # pure and needs no config. The `reads_from` guard below compares the
        # variable with the default, so a `$EXPLAINABILITY_API_KEY` paste or a
        # `MY VAR` used to be diagnosed as "export $$EXPLAINABILITY_API_KEY",
        # a sentence nobody can act on, while the exact problem
        # (`key_env_problem`) never ran (round 7 of #203).
        if key_env and (problem := explainability_service.key_env_problem(key_env)):
            self.notify(problem, severity="warning", timeout=8, markup=False)
            return

        # The field asks for a NAME, and a brace is refused with the reason
        # rather than repaired: the first cut stripped at the first brace and
        # stored what preceded it, so the tab kept a template the operator never
        # typed (`team-{env}-{role}` became `team-{role}`; a typo'd `nishil}`
        # became `nishil`) while the CLI's `--identity` refused the same input
        # through `identity_problem`. Two doors, two answers again (review of
        # #132). The writer's own check still runs on the composed template
        # below, so this is guidance for the one shape it cannot word: a name
        # with the template's braces in it.
        if prefix and ("{" in prefix or "}" in prefix):
            self.notify(
                f"prefix {prefix!r} is a name, not a template — the agent's role is added "
                "for you: try 'nishil', not 'nishil-{role}'",
                severity="warning",
                timeout=8,
                markup=False,
            )
            return

        config = self._read_config()
        if config is None:
            return
        settings = config.explainability
        name = target or settings.target
        # The key FILE answers for ONE variable, the default: it holds a single
        # unlabelled key, and a staging key must never satisfy a prod target
        # (`resolve_target`; tests/test_key_never_crosses_deployments.py). So a
        # key typed for a target that names its own variable would be written
        # where nothing reads it -- `✓ setup saved` over `$MY_VAR is NOT set`.
        # Judged against the variable the target will HAVE after this save (the
        # typed one, else the stored one), not against the field alone: a
        # target configured with `--key-env MY_VAR` last month fails the same way.
        # A project's own key is the resolver's FIRST rung, read whatever variable
        # the target names, so the rule is the file's alone.
        reads_from = key_env or settings.targets.get(name, ExplainabilityTarget()).api_key_env
        if key and owner is None and reads_from != KEY_ENV_VAR:
            self.notify(
                f"target '{name}' reads its key from ${reads_from}, and the key file is read "
                f"only for ${KEY_ENV_VAR} — a key typed here would never be used. Export "
                f"${reads_from} in the shell instead, or leave 'key variable' blank to use "
                "the file",
                severity="warning",
                timeout=10,
                markup=False,
            )
            return
        # Offered, not imposed -- and only where nothing was CHOSEN. `chosen_proxy`
        # is the resolver's own fold minus the shipped default: the target's
        # proxy, else a deliberate top-level one. Testing the per-target value
        # alone let a top-level `[explainability] proxy_url` be shadowed by a
        # suggestion; testing the blank FIELD, before that, replaced a stored
        # one. `hosted_proxy_for` is silent for a loopback gateway, whose own
        # port is the shipped 9090 default and not 9443.
        if gateway and not proxy and ops.chosen_proxy(settings, target or None) is None:
            suggested = explainability_service.hosted_proxy_for(gateway)
            if suggested is not None:
                proxy = suggested
        identity = f"{prefix}-{{role}}" if prefix else None
        try:
            name = explainability_service.configure_target(
                config,
                target_name=target or None,
                gateway_url=gateway or None,
                key_env=key_env or None,
                proxy_url=proxy or None,
                identity=identity,
                enable=False,
                make_active=switch,
            )
        except ValueError as exc:  # the writer refused a URL or template; nothing changed
            self.notify(str(exc), severity="warning", timeout=8, markup=False)
            return
        # The project key's deployment: the one typed, else the one a launch
        # resolves — `key set`'s default. A typed name that no target answers to
        # after this save is refused as `key set --target` refuses it: a binding
        # to a deployment nothing resolves traces nothing (review of #170).
        key_target = name
        if owner is not None:
            # With the project, as `key set` resolves it: its destination (#142)
            # names the deployment its traces go to when the field does not.
            key_target = ops.resolve_target(settings, target or None, project_id=owner.id).name
            known = sorted({settings.target, *settings.targets})
            if target and key_target not in known:
                self.notify(
                    f"no target '{key_target}' on this machine (known: {', '.join(known)}) — "
                    "give it a gateway URL here first, then attach the key",
                    severity="warning",
                    timeout=10,
                    markup=False,
                )
                return
            # The field blank, the settings go to the machine's target and the
            # key to the project's: two deployments from one press, and the
            # project's launches never read the gateway just typed (review of
            # #172). Refused before a write began, so the fields keep it all.
            if key_target != name and any((gateway, proxy, prefix, key_env)):
                self.notify(
                    f"this project's key belongs to target '{key_target}', where its traces "
                    f"go, and the other settings would be saved for '{name}' — type a "
                    "deployment to save both to it, or save the key on its own",
                    severity="warning",
                    timeout=10,
                    markup=False,
                )
                return
            # A key the CLI minted (#142) is the CLI's to replace, and refused
            # here, before a write began, the field keeps what was typed. A store
            # that cannot say is a refusal too: this runs on the UI thread, where
            # a raise ends the app, and guessing "not minted" could overwrite one.
            try:
                refused = minted_key_refusal(owner)
            except Exception as exc:
                refused = Notice(f"the store could not be read — nothing changed: {exc}", "error")
            if refused is not None:
                self.notify(refused.message, severity=refused.severity, timeout=10, markup=False)
                return
        # Cleared the moment a write begins, whatever happens after: a masked
        # Input still holds its value, and a failed key write used to return
        # before this line and leave the plaintext live in the widget for the
        # rest of the session -- against this view's own "never shown back".
        key_field.value = ""
        if not self._write_config(config):
            return
        # The key AFTER the config: a written key with no target to use it is
        # inert, while a target whose key failed to land is a red check that
        # names its own fix. The cheaper failure is the one left behind.
        if owner is not None:
            try:
                attached = attach_project_key(key, owner, key_target)
            except Exception as exc:  # the store or the filesystem said no: a notice
                self.notify(
                    f"settings saved, but the key could not be attached: {exc} — type it again",
                    severity="error",
                    timeout=8,
                    markup=False,
                )
                self.refresh_status()
                return
            self.notify(attached.message, severity=attached.severity, timeout=8, markup=False)
        elif key:
            try:
                explainability_service.store_api_key(key)
            except OSError as exc:
                self.notify(
                    f"settings saved, but the key could not be written: {exc} — type it again",
                    severity="error",
                    timeout=8,
                    markup=False,
                )
                self.refresh_status()
                return
        active = settings.target
        if not typed:
            done = f"✓ this machine now uses target '{name}' — press Enable tracing to trace to it"
        elif name != active:
            done = (
                f"✓ setup saved for target '{name}' — this machine stays on '{active}'; tick "
                "'make active' and save again to switch"
            )
        else:
            done = f"✓ setup saved for target '{name}' — press Enable tracing, then Register roster"
        self.notify(done, timeout=8, markup=False)
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
        self._start_network_work(REGISTER_WORKER, partial(register_roster, self.project))

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
