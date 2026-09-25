"""Where a project's traces land, chosen while signed in (#142).

``aisquare login`` already knows WHO you are. This module lets a project say
WHERE its traces go — a workspace and a studio — by listing what the signed-in
user can see and recording the choice per project, with no key pasted anywhere
the CLI can help it.

The facts this is built on, measured against the backend and gateway sources
on 2026-09-13 (AISquare-Studio-BE ``46944a0``, AISquare-Explainability-SDK
``9d060fc``), because the issue's design hinged on what the platform accepts
from a sign-in token:

* ``GET /api/v2/workspaces/`` and ``GET /api/v2/publications/`` (studios,
  scoped by the ``X-Workspace-Id`` header) accept the CLI's ``aisq_`` Bearer
  token. So listing and choosing work TODAY.
* Ingest does not take a Bearer token at all: the gateway's
  ``/v1/traces/ingest`` and the hosted proxy both authenticate on a workspace
  ingest key (``AIS_…``, scope ``ingest:write``). "No key handling" therefore
  means the CLI obtains the key on the user's behalf — and the endpoint that
  mints one, ``POST /api/v2/iam/workspace-api-key/``, still authenticates with
  the web app's JWT class, so it answers a sign-in token with
  ``401 token_not_valid``. That gap is filed as the backend counterpart; the
  client side of the exchange is here, coded to the endpoint's contract, and
  :func:`mint_key` says exactly that when it is refused.
* A studio is not a header or a body field on a span. The gateway lands a span
  in the studio its ``agent.name`` is ROUTED to inside the key's workspace, a
  binding held by the backend (``PUT /api/v2/iam/workspaces/<id>/agents/<name>/``,
  takes a workspace key). So choosing a studio means binding this machine's
  agent identities to it, which :func:`bind_roster` does once a key is at hand.

Design rules, each with its reason in the function that enforces it: the
environment the session belongs to decides the deployment (no URL typed, no
staging key near a prod gateway); the choice lives in the store per project
(one machine, many workspaces); a minted key is the CLI's and ``logout`` clears
it, a key attached by hand (#141) is the operator's and is left alone; and a
minted key's uid is never forgotten until the server has confirmed its
revocation (:func:`revoke_owed`).
"""

from __future__ import annotations

import contextlib
import re
import socket
import sqlite3
import time
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from aisquare.core import paths
from aisquare.core.config import ExplainabilitySettings, ExplainabilityTarget
from aisquare.core.store import ContextStore, store_session
from aisquare.models import UNKNOWN_KEY_UID, PendingRevocation, ProjectInfo, TraceDestination
from aisquare.services import iam
from aisquare.services.explainability import (
    KEY_ENV_VAR,
    clear_project_api_key,
    project_key_path,
    store_project_api_key,
)
from aisquare.services.explainability_ops import (
    HttpVerdict,
    ResolvedTarget,
    _request,
    put_back_project_key,
)

#: The backend issue that makes the key exchange work for a sign-in token.
BACKEND_ISSUE = "AISquare-Studio-BE#3493"

#: The scope an ingest key needs; the create endpoint defaults to ``["*"]``
#: when none is sent, which is more than a tracing credential should hold.
INGEST_SCOPE = "ingest:write"

PAGE_SIZE = 100  # the API's maximum


class DestinationError(Exception):
    """A destination step that could not be taken, with a code the CLI can key copy on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── environments ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Environment:
    """One deployment: the API host a session belongs to and the tracing endpoints beside it.

    ``proxy_url`` is the hosted ``claude_code`` proxy as the cutover runbook
    records it (the public ``:9443`` listener in front of the proxy's 9090);
    ``None`` where none is recorded, in which case the target keeps whatever
    proxy it has and ``doctor --live`` says so.
    """

    name: str
    api_hosts: tuple[str, ...]
    gateway_url: str
    proxy_url: str | None
    dashboard_url: str


#: Measured 2026-09-13 from AISquare-Explainability-SDK ``docs/DEPLOYMENT.md``
#: (gateway hosts: "those three URLs are the complete list"), this repo's
#: ``docs/runbooks/explainability-prod-cutover.md`` (proxy listeners) and
#: ``docs/plans/aisquare-login.md`` §2 (API hosts per environment). ``dev``
#: keeps the legacy dotted staging gateway name on purpose — that IS its host.
#: So is its proxy: a proxy ships to the one gateway it was started with, and
#: each deployment's is the one beside its gateway. The runbook recorded the
#: dotted ``:9443`` as staging's, from before that box became dev (the SDK's
#: ``deploy-dev.yml``), and ``stg`` sent staging's model traffic and key to
#: dev's gateway; staging's own answered at its gateway's host on 2026-09-25
#: (final review of #203, EX4).
ENVIRONMENTS: tuple[Environment, ...] = (
    Environment(
        name="prod",
        api_hosts=("api.aisquare.studio",),
        gateway_url="https://explainability-api.aisquare.studio",
        proxy_url="https://explainability-api.aisquare.studio:9443",
        dashboard_url="https://x.aisquare.studio",
    ),
    Environment(
        name="stg",
        api_hosts=("stg-api.aisquare.studio",),
        gateway_url="https://stg-explainability-api.aisquare.studio",
        proxy_url="https://stg-explainability-api.aisquare.studio:9443",
        dashboard_url="https://stg-x.aisquare.studio",
    ),
    Environment(
        name="dev",
        api_hosts=("studio-api-dev.aisquare.com",),
        gateway_url="https://stg-explainability.api.aisquare.studio",
        proxy_url="https://stg-explainability.api.aisquare.studio:9443",
        dashboard_url="https://dev-x.aisquare.com",
    ),
    Environment(
        name="local",
        api_hosts=("localhost", "127.0.0.1"),
        gateway_url="http://localhost:8000",
        proxy_url="http://127.0.0.1:9090",
        dashboard_url="http://localhost:3005",
    ),
)


def environment_for(api_url: str) -> Environment | None:
    """The deployment an API URL belongs to, or ``None`` for a host this table does not know.

    Matched on the host alone: the scheme and a port are how a local stack is
    reached, not which deployment it is. An unknown host is not an error here —
    :func:`environment_name` still yields a target name for it — it only means
    no gateway or proxy can be filled in, and ``use`` says so.
    """
    host = (urlsplit(api_url).hostname or "").lower()
    for environment in ENVIRONMENTS:
        if host in environment.api_hosts:
            return environment
    return None


def environment_name(api_url: str) -> str:
    """The target name for a session's API: the deployment's, else its bare host."""
    known = environment_for(api_url)
    if known is not None:
        return known.name
    return (urlsplit(api_url).hostname or api_url).lower()


def key_env_for(name: str) -> str:
    """``EXPLAINABILITY_<NAME>_API_KEY``: the variable a target this module creates names.

    The shape the settings docstring already shows for a hand-written prod
    target, derived from the target name so an unknown host still yields a
    legal variable name.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "TARGET"
    return f"EXPLAINABILITY_{slug}_API_KEY"


def _machine_key_serves(settings: ExplainabilitySettings, gateway_url: str) -> bool:
    """Whether the machine's unlabelled key is already the key of the deployment at ``gateway_url``.

    ``init --explainability`` writes the top-level gateway and the key file
    together, so a top-level gateway equal to the deployment's IS the
    single-deployment machine pointing at it. The machine's own target's
    gateway counts as well while that target reads the key, which it does by
    naming the default variable: the machine already sends the key there. One
    that names a variable of its own never sent the key file to its gateway,
    and counting it anyway gave a destination there the default variable: on a
    prod machine moved onto a staging target with a variable of its own, the
    prod key file went to the staging gateway and proxy (review of #203,
    round 2). Anything else is a key issued for somewhere this function cannot
    see, and so is a deployment with no gateway known.
    """
    if not gateway_url:
        return False
    served = {settings.gateway_url}
    own = settings.targets.get(settings.target)
    if own is not None and own.api_key_env == KEY_ENV_VAR:
        served.add(own.gateway_url)
    return gateway_url.rstrip("/") in {url.rstrip("/") for url in served if url}


def deployment_target(
    settings: ExplainabilitySettings, destination: TraceDestination
) -> ExplainabilityTarget:
    """The deployment a project's destination names, as the target its resolution reads.

    The operator's ``[explainability.targets.<name>]`` for that deployment when
    there is one, with only what is empty filled from the table: a gateway or
    proxy set by hand stays. Otherwise a target of its own. Either way it names
    a key variable of its own (:func:`key_env_for`) unless it already names
    one other than the default. With the default one, the unlabelled machine
    key — ``~/.aisquare/explainability-key`` or ``$EXPLAINABILITY_API_KEY`` —
    would answer for every deployment anyone signs in to, and ``use`` would
    bind the roster and every launch would authenticate with a key issued for
    somewhere else: the hazard ``tests/test_key_never_crosses_deployments.py``
    pins. An entry the operator wrote without an ``api_key_env`` is no
    exception: it is the entry ``use`` tells them to write for a host outside
    the table (its gateway and proxy), and the machine's prod key went to the
    self-hosted gateway and proxy with it (review of #203). Nor is one that
    writes the default out: ``save_config`` writes every field, so every entry
    the CLI saved names it, and the two cannot be told apart. The one exception
    is the deployment the machine key already serves
    (:func:`_machine_key_serves`), where that key is exactly the right one and a
    new variable would only take it away.

    BUILT FOR THE ONE RESOLUTION, NEVER WRITTEN TO THE CONFIG. ``use`` used to
    write it into the ``targets`` map the machine's own target is read from, so
    one project's choice re-pointed every project without a destination. On
    the machine ``init --explainability`` writes — a top-level gateway, the key
    file, no target, and ``target = "stg"`` by default — ``use`` for one
    project while signed in to staging created ``targets.stg`` with the staging
    gateway and a key variable nothing sets: every other project, the doctor
    and the shipper moved to staging with no key, so untraced. Filling an
    existing target's empty gateway did the same (review of #203). Only
    :func:`~aisquare.services.explainability_ops.resolve_target` calls this,
    for the project whose destination names the target.

    An API host outside the table gets no gateway and no proxy here, and the
    resolver answers "no gateway known" rather than reaching for the machine's
    top level: that is another deployment's, and the project's key went to it.
    """
    environment = environment_for(destination.api_url)
    configured = settings.targets.get(destination.environment)
    target = configured.model_copy() if configured is not None else ExplainabilityTarget()
    if environment is not None:
        if not target.gateway_url:
            target.gateway_url = environment.gateway_url
        if not target.proxy_url and environment.proxy_url:
            target.proxy_url = environment.proxy_url
    # Judged on the gateway this resolution uses, filled in or set by hand.
    if target.api_key_env == KEY_ENV_VAR and not _machine_key_serves(settings, target.gateway_url):
        target.api_key_env = key_env_for(destination.environment)
    return target


# ── what the signed-in user can see ────────────────────────────────────────────


@dataclass(frozen=True)
class Workspace:
    id: int
    uid: str | None
    name: str
    type: str | None = None
    role: str | None = None
    invite_status: str | None = None

    @property
    def member(self) -> bool:
        """A workspace the user belongs to — the list also carries pending invites."""
        return self.role is not None


@dataclass(frozen=True)
class Studio:
    id: int
    uid: str | None
    name: str
    workspace_id: int | None = None
    is_default: bool = False
    is_inbox: bool = False
    visibility: str | None = None


def _pages(session: iam.Session, path: str, *, workspace: str | None) -> Iterator[dict[str, Any]]:
    """Every row of a paginated listing, page by page, in the API's own envelope.

    The envelope is ``{count, next, previous, page, page_size, total, results}``;
    the ``next`` link is followed by page NUMBER rather than by URL so the
    request always goes through :func:`iam.request`, which is the only place
    allowed to put the token on the wire. Always against the SESSION's API URL:
    a session belongs to one host, and ``iam.request`` refuses to send its token
    anywhere else — so the default resolution (flag, variable, config) is not
    consulted here at all.
    """
    page = 1
    while True:
        joiner = "&" if "?" in path else "?"
        result = iam.request(
            f"{path}{joiner}page={page}&page_size={PAGE_SIZE}",
            workspace=workspace,
            api_url=session.api_url,
        )
        if result.status != 200 or not isinstance(result.body, dict):
            raise DestinationError(
                "api_error",
                f"the API answered HTTP {result.status} for {path.split('?')[0]}: "
                f"{_detail(result.body)}",
            )
        rows = result.body.get("results")
        if not isinstance(rows, list):
            raise DestinationError("api_error", f"unexpected listing shape from {path}")
        yield from (row for row in rows if isinstance(row, dict))
        if not result.body.get("next"):
            return
        page += 1


def _detail(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("detail", "error", "message"):
            if body.get(key):
                return str(body[key])
    return str(body)[:160] if body else "(no body)"


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def list_workspaces(session: iam.Session) -> list[Workspace]:
    """The workspaces the signed-in user belongs to, members first, then pending invites."""
    rows = _pages(session, "api/v2/workspaces/", workspace=None)
    found: list[Workspace] = []
    for row in rows:
        ident = _int(row.get("id"))
        if ident is None:
            continue
        found.append(
            Workspace(
                id=ident,
                uid=str(row["uid"]) if row.get("uid") else None,
                name=str(row.get("name") or ident),
                type=str(row["type"]) if row.get("type") else None,
                role=str(row["effective_role"]) if row.get("effective_role") else None,
                invite_status=str(row["invite_status"]) if row.get("invite_status") else None,
            )
        )
    return sorted(found, key=lambda w: (not w.member, w.name.lower()))


def list_studios(session: iam.Session, workspace: Workspace) -> list[Studio]:
    """The studios of ``workspace`` the signed-in user can see.

    ``scope=workspace`` plus the ``X-Workspace-Id`` header: the API resolves
    the workspace from the header and falls back to the user's personal one
    without it, so the header is never optional here. The workspace is named
    by its uid when it has one (the header's documented shape), else by id.

    A row that names ANOTHER workspace is dropped: that fallback also answers
    a header the API does not honour for this user (an invitation not yet
    accepted), and a personal studio recorded under the team workspace would
    route nothing where the destination says.
    """
    rows = _pages(
        session,
        "api/v2/publications/?scope=workspace",
        workspace=workspace.uid or str(workspace.id),
    )
    found: list[Studio] = []
    for row in rows:
        ident = _int(row.get("id"))
        if ident is None:
            continue
        owner = _int(row.get("workspace_id") or row.get("workspace"))
        if owner is not None and owner != workspace.id:
            continue
        found.append(
            Studio(
                id=ident,
                uid=str(row["uid"]) if row.get("uid") else None,
                name=str(row.get("name") or ident),
                workspace_id=owner,
                is_default=bool(row.get("is_default")),
                is_inbox=bool(row.get("is_inbox")),
                visibility=str(row["visibility"]) if row.get("visibility") else None,
            )
        )
    return sorted(found, key=lambda s: (s.is_inbox, not s.is_default, s.name.lower()))


def pick_workspace(
    ref: str, workspaces: list[Workspace], *, members_only: bool = False
) -> Workspace:
    """A workspace by name (case-insensitive), uid or id; ambiguity and absence are errors.

    ``members_only`` is for choosing a destination: the listing carries pending
    invitations so the reason one cannot be picked is on screen, and a
    workspace the user has not joined is not somewhere their traces can land.
    The ref is matched among the workspaces the user belongs to FIRST: matched
    across the invitations too, a member workspace named like a pending one
    read as ``ambiguous`` and could not be chosen by name at all (review of
    #172). Only a ref no member workspace answers is looked for among the
    invitations, to say why it cannot be picked.
    """
    if not members_only:
        return _pick_workspace(ref, workspaces)
    try:
        return _pick_workspace(ref, [w for w in workspaces if w.member])
    except DestinationError as exc:
        if exc.code != "not_found":
            raise
    found = _pick_workspace(ref, workspaces)  # not a member's: an invitation, or nothing
    raise DestinationError(
        "not_a_member",
        f"you are invited to {found.name} ({found.invite_status or 'pending'}) but not a "
        "member yet — accept the invitation in the web app, then choose it",
    )


def _pick_workspace(ref: str, workspaces: list[Workspace]) -> Workspace:
    wanted = ref.strip()
    exact = [w for w in workspaces if wanted in (str(w.id), w.uid)]
    if len(exact) == 1:
        return exact[0]
    named = [w for w in workspaces if w.name.lower() == wanted.lower()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        ids = ", ".join(f"{w.name} ({w.id})" for w in named)
        raise DestinationError("ambiguous", f"several workspaces are named '{ref}': {ids}")
    have = ", ".join(w.name for w in workspaces) or "(none)"
    raise DestinationError("not_found", f"no workspace matches '{ref}' — you can see: {have}")


def pick_studio(ref: str | None, studios: list[Studio], workspace: Workspace) -> Studio:
    """A studio by name, uid or id; with no ref, the workspace's default studio if it has one."""
    if ref is None:
        defaults = [s for s in studios if s.is_default and not s.is_inbox]
        if len(defaults) == 1:
            return defaults[0]
        raise DestinationError(
            "studio_required",
            f"name the studio: {workspace.name} has no single default — "
            f"{', '.join(s.name for s in studios if not s.is_inbox) or '(no studios)'}",
        )
    wanted = ref.strip()
    exact = [s for s in studios if wanted in (str(s.id), s.uid)]
    if len(exact) == 1:
        return exact[0]
    named = [s for s in studios if s.name.lower() == wanted.lower()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        ids = ", ".join(f"{s.name} ({s.id})" for s in named)
        raise DestinationError("ambiguous", f"several studios are named '{ref}': {ids}")
    have = ", ".join(s.name for s in studios if not s.is_inbox) or "(none)"
    raise DestinationError(
        "not_found", f"no studio matches '{ref}' in {workspace.name} — you can see: {have}"
    )


# ── the choice ─────────────────────────────────────────────────────────────────


def choose(
    store: ContextStore,
    project: ProjectInfo,
    workspace: Workspace,
    studio: Studio,
    session: iam.Session,
    *,
    previous: TraceDestination | None = None,
) -> TraceDestination:
    """Record where ``project``'s traces land.

    A re-point into ANOTHER workspace detaches the key the CLI minted: it was
    that workspace's credential and cannot serve this one. Its file and binding
    go and its revocation is owed (:func:`detach`); the caller revokes it once
    the store session is closed (:func:`revoke_owed`) — which a move onto
    another deployment cannot do with the new one's session, so the key stays
    owed, and said, until a sign-in on the host that minted it can (review of
    #172). A re-point within the same workspace (another studio) keeps it.
    "The same workspace" is the id on the same API: workspace ids are per
    deployment, so a staging 7 and a production 7 are two workspaces.

    The project row is made sure of first — a destination references it, and
    ``use`` may run in a directory nothing has registered yet. Captured, not
    onboarded (#139): choosing where traces land is not adding it to the list.
    """
    store.ensure_project(project)
    key_uid = None
    if (
        previous is not None
        and previous.workspace_id == workspace.id
        and _same_api(previous.api_url, session.api_url)
    ):
        key_uid = previous.key_uid
    if previous is not None and previous.key_uid and key_uid is None:
        # The minted key was the old workspace's; it cannot serve the new one.
        # The key FILE is dropped too: a binding to the old deployment would
        # otherwise keep answering for a project that moved.
        detach(store, project.id)
    return store.set_project_destination(
        TraceDestination(
            project_id=project.id,
            api_url=session.api_url,
            environment=environment_name(session.api_url),
            workspace_id=workspace.id,
            workspace_uid=workspace.uid,
            workspace_name=workspace.name,
            studio_id=studio.id,
            studio_uid=studio.uid,
            studio_name=studio.name,
            key_uid=key_uid,
            set_at=datetime.now(tz=UTC),
            set_by=session.email or session.sub or None,
        )
    )


def forget(store: ContextStore, project: ProjectInfo) -> TraceDestination | None:
    """Drop the project's destination and detach the key the CLI minted for it; the old row.

    ``None`` when there was none. The key's revocation is owed from the commit
    that detaches it; the caller revokes it once the store session is closed
    (:func:`revoke_owed`).
    """
    previous = store.project_destination(project.id)
    if previous is None:
        return None
    detach(store, project.id)
    store.clear_project_destination(project.id)
    return previous


def _same_api(one: str, other: str) -> bool:
    return one.rstrip("/") == other.rstrip("/")


def detach(store: ContextStore, project_id: str) -> bool:
    """Take the project's MINTED key off it — its revocation owed — and delete its file.

    The store does the part that must not come apart in one transaction
    (``detach_minted_key``: the uid off the row and into ``pending_revocation``,
    the binding deleted); the file goes after, and only when that binding named
    the project's own key file. A file that will not delete is left: nothing
    binds it any more, so nothing reads it, and the key in it is owed a
    revocation all the same. Nothing here revokes — that is a network call, made
    outside the store session (:func:`revoke_owed`). Returns whether a key was
    detached.

    The uid is set only while the project's key file holds the key the CLI
    minted — every writer of the binding says which kind it wrote
    (``set_project_explainability(minted=)``) — and that invariant is what keeps
    a hand-attached key out of here: the file and the binding are the same for
    both kinds.
    """
    detached, binding = store.detach_minted_key(project_id)
    if binding is not None and binding.key_path == project_key_path(project_id):
        with contextlib.suppress(OSError):
            clear_project_api_key(project_id)
    return detached


# ── revocations owed ──────────────────────────────────────────────────────────


REVOKE_BUDGET_SECONDS = 20.0
"""What one pass of :func:`revoke_owed` may spend on requests, whatever the count.

A ``prune --purge`` over many keyed projects made one blocking revoke (10 s
timeout each) per project, inside the loop and the store session (review of
#172); now they share this, and what it does not reach stays owed.
"""

REVOKE_RETRY = (
    "`aisquare explainability use`, `aisquare doctor --live` and `aisquare logout` try "
    "again while signed in to the API that minted it; or revoke it in the dashboard's key "
    "list (aisquare-cli <host> <project>)"
)
"""Where a key still owed is tried again — the one remedy every surface names."""


@dataclass
class Revocations:
    """One pass over the revocations owed (:func:`revoke_owed`): what went, what is still live."""

    revoked: list[PendingRevocation] = field(default_factory=list)
    owed: list[PendingRevocation] = field(default_factory=list)
    """Still live on the server, each with ``last_error`` saying why."""

    def as_json(self) -> dict[str, Any]:
        return {
            "revoked": [record.key_uid for record in self.revoked],
            "still_live": [
                {
                    "key_uid": record.key_uid,
                    "workspace": record.workspace_name,
                    "project": record.project_name,
                    "api_url": record.api_url,
                    "reason": record.last_error,
                }
                for record in self.owed
            ],
        }


def revoke_owed(
    session: iam.Session | None,
    *,
    project_ids: Collection[str] | None = None,
    budget: float = REVOKE_BUDGET_SECONDS,
) -> Revocations:
    """Revoke the minted keys that were detached, and forget each once the server confirms it.

    Runs with NO store session open: the owed records are read, the store is
    closed, one request goes out per key, and the outcome is written in a second
    session — a revoke is a network call (10 s timeout), and made inside a
    session it held the store for every key. A record is deleted only on the
    server's confirmation (:func:`_revoke`); anything else — signed out, signed
    in to another host, offline, refused, out of ``budget`` — keeps it for the
    next pass, and the report says why. The record keeps only what an ATTEMPT
    learned: a pass that could not ask (signed out, another host, out of time)
    leaves the reason the server last gave, which is what the ``minted-keys``
    row goes on saying. ``project_ids`` limits the pass to what a
    command just detached; ``None`` is every key owed, which is what ``use``,
    ``doctor --live`` and ``logout`` retry. A store that cannot be read or
    cannot record the outcome costs the bookkeeping, not the command, whose
    own write has already committed: what could not be read stays owed for the
    next pass, and a key the server revoked and this could not forget answers
    404 on the next pass, which settles it.
    """
    report = Revocations()
    if not paths.db_path().exists():
        return report  # nothing was ever detached; a read must not create the store
    owed: list[PendingRevocation] = []
    # Guarded like the write below: unguarded, a locked store turned a `key clear`
    # or a purge that had already committed into a traceback (review of #172's
    # follow-ups, round 1, F3).
    with contextlib.suppress(sqlite3.Error, OSError), store_session() as store:
        owed = [
            record
            for record in store.pending_revocations()
            if project_ids is None or record.project_id in project_ids
        ]
    if not owed:
        return report
    deadline = time.monotonic() + budget
    attempted: list[PendingRevocation] = []
    for record in owed:
        reason, asked = _revoke(record, session, deadline)
        if reason is None:
            report.revoked.append(record)
            continue
        kept = record.model_copy(update={"last_error": reason})
        report.owed.append(kept)
        if asked:
            attempted.append(kept)
    with contextlib.suppress(sqlite3.Error, OSError), store_session() as store:
        for record in report.revoked:
            store.settle_revocation(record.key_uid)
        # Every pass wrote its reason: a signed-out one replaced the 403 a caller
        # who is not OWNER or ADMIN had been given, and the `minted-keys` row sent
        # the operator to sign in when the blocker was the role (review of #172's
        # follow-ups, round 1, F6).
        for record in attempted:
            store.note_revocation_failure(record.key_uid, record.last_error or "")
    return report


def _revoke(
    record: PendingRevocation, session: iam.Session | None, deadline: float
) -> tuple[str | None, bool]:
    """Revoke one owed key where it was minted: why not (``None`` once confirmed), and if it asked.

    Confirmed is a 2xx, or a 404 — the server has no such key any more (revoked
    from the dashboard, or by an earlier attempt whose answer was lost), so
    nothing is left to revoke. Only against the API the key was minted on: a
    session belongs to one host, and a uid sent to another gets a 404 that
    would read as that confirmation. A refusal (the endpoint shares the mint's
    authentication gap; only a workspace OWNER or ADMIN may revoke), a server
    error or an unreachable server leaves it owed. No request goes out while
    signed out, signed in to another host or out of time, and the second value
    says so: that reason is this pass's, not the key's (:func:`revoke_owed`).
    Never raises.

    On that host the 404 does not depend on who asks, or from which workspace:
    measured against AISquare-Studio-BE ``aab6d7f5``, the endpoint finds the
    key by its uid alone among the host's active keys and checks the caller's
    role in the workspace the KEY belongs to, so a caller who may not revoke it
    is answered 403, never 404. The request names that workspace in
    ``X-Workspace-Id`` all the same, as every call meaning a workspace must
    (``iam.request``): an endpoint that took its context from the header
    would otherwise answer from the caller's personal workspace, where a 404
    settles nothing (review of #172's follow-ups, round 1, F1).
    """
    if session is None:
        return "signed out", False
    if not _same_api(record.api_url, session.api_url):
        return f"signed in to {session.api_url}, not {record.api_url}", False
    left = deadline - time.monotonic()
    if left <= 0:
        return "not tried — this pass ran out of time", False
    try:
        result = iam.request(
            f"api/v2/iam/workspace-api-key/{record.key_uid}/revoke/",
            method="POST",
            workspace=str(record.workspace_id),
            api_url=session.api_url,
            tolerate=(400, 401, 403, 404),
            timeout=min(iam.HTTP_TIMEOUT_SECONDS, left),
        )
    except iam.IamError as exc:
        return exc.message, True
    if 200 <= result.status < 300 or result.status == 404:
        return None, True
    return f"the API answered HTTP {result.status}: {_detail(result.body)}", True


def describe_owed(owed: list[PendingRevocation]) -> str:
    """``1 key the CLI minted is still live on the server — acme for web (signed out)``."""
    noun = "key the CLI minted is" if len(owed) == 1 else "keys the CLI minted are"
    which = "; ".join(
        f"{record.workspace_name} for {record.project_name} "
        f"({record.last_error or 'not tried yet'})"
        for record in owed
    )
    return f"{len(owed)} {noun} still live on the server — {which}"


def describe_revocations(report: Revocations) -> str | None:
    """The line a command adds for the revocations it made; ``None`` when it made none."""
    parts: list[str] = []
    if report.revoked:
        which = ", ".join(
            f"{record.workspace_name} for {record.project_name}" for record in report.revoked
        )
        parts.append(f"revoked {len(report.revoked)} key(s) the CLI minted ({which})")
    if report.owed:
        parts.append(f"{describe_owed(report.owed)} — {REVOKE_RETRY}")
    return "; ".join(parts) or None


# ── the credential, on the user's behalf ──────────────────────────────────────


@dataclass(frozen=True)
class MintedKey:
    uid: str
    name: str
    path: str


def key_name(project: ProjectInfo) -> str:
    """``aisquare-cli <host> <project>``: how the minted key reads in the dashboard's key list."""
    host = socket.gethostname().split(".")[0][:40] or "machine"
    return f"aisquare-cli {host} {project.root.name or project.id}"[:255]


def mint_key(
    store: ContextStore, project: ProjectInfo, destination: TraceDestination, session: iam.Session
) -> MintedKey:
    """Exchange the session for a workspace ingest key and attach it to the project (#141 shape).

    ``scopes`` is sent explicitly: the endpoint defaults to ``["*"]`` and a
    tracing credential must be able to send spans and read nothing. The key
    value goes to the project's mode-600 file and nowhere else; the store keeps
    the uid so ``logout`` (and a re-point) can revoke exactly this key.

    TODAY THE ENDPOINT REFUSES A SIGN-IN TOKEN — its authentication class is
    the web app's JWT one — so a 401 here, from a session that listed the
    workspaces a moment ago, is the endpoint's gap and not the session's, and
    the error says so with the backend issue to watch. Any other 4xx is the
    user's standing: only a workspace OWNER or ADMIN may mint keys.

    NEVER OVER A KEY ATTACHED BY HAND. The project has one key file, so a mint
    for this deployment while the operator's key is bound to another would
    overwrite that key and rebind it — the operator's credential destroyed by
    a command that promised to leave it alone. Refused before the request, so
    no key is created only to be thrown away.

    A KEY THE CLI MINTED BEFORE IS OWED A REVOCATION: the row's uid now names
    the new one, so the old one would stay a live ``ingest:write`` key that
    nothing on this machine remembers (``use`` mints over one when its file is
    gone). The new binding and uid and the old uid's pending revocation are one
    commit (``set_project_explainability(minted=)``), made once the new key is
    in its file, so a key is never owed before its replacement is in place; an
    idempotent mint that answers with the same uid owes nothing. The caller
    revokes it once the store session is closed (:func:`revoke_owed`).

    A new key that cannot be recorded (the store refuses the commit) is put
    back out of the file and revoked on the spot, best effort: recorded
    nowhere, it would be a live key this machine never knew it had. The file
    is put back as ``key set``'s is (``put_back_project_key``): when the
    earlier key cannot be written back, the file is removed and the error says
    so, rather than keeping the new key, revoked a moment later, under the
    earlier binding.
    """
    binding = store.project_explainability(project.id)
    if binding is not None and not destination.key_uid and binding.key_path.is_file():
        raise DestinationError(
            "hand_key_attached",
            f"the project has a key attached by hand for target {binding.target}, and the CLI "
            "does not overwrite it — `aisquare explainability key set --target "
            f"{destination.environment}` replaces it, `key clear` removes it",
        )
    result = iam.request(
        "api/v2/iam/workspace-api-key/",
        method="POST",
        json_body={
            "workspace_id": destination.workspace_id,
            "name": key_name(project),
            "scopes": [INGEST_SCOPE],
        },
        api_url=session.api_url,
        tolerate=(401,),
    )
    if result.status == 401:
        raise DestinationError(
            "key_exchange_unsupported",
            "the API does not accept a sign-in token on its key endpoint yet "
            f"({BACKEND_ISSUE}); until it does, attach a key from the dashboard with "
            "`aisquare explainability key set --from-env VAR`",
        )
    if result.status == 403:
        raise DestinationError(
            "not_allowed",
            f"only a workspace OWNER or ADMIN can mint a key for {destination.workspace_name}"
            f" — {_detail(result.body)}",
        )
    body = result.body if isinstance(result.body, dict) else {}
    value = body.get("api_key")
    uid = str(body.get("uid") or UNKNOWN_KEY_UID)
    if result.status not in (200, 201) or not isinstance(value, str) or not value:
        raise DestinationError(
            "api_error", f"key creation answered HTTP {result.status}: {_detail(result.body)}"
        )
    earlier = _key_file_contents(project.id)
    path = store_project_api_key(project.id, value)
    try:
        store.set_project_explainability(
            project.id,
            target=destination.environment,
            key_path=path,
            set_by=destination.set_by,
            minted=uid,
            api_url=destination.api_url,
        )
    except BaseException as refused:
        put_back_project_key(project.id, earlier, refused)
        if uid != UNKNOWN_KEY_UID:
            unrecorded = PendingRevocation(
                key_uid=uid,
                api_url=destination.api_url,
                workspace_id=destination.workspace_id,
                workspace_name=destination.workspace_name,
                project_id=project.id,
                project_name=project.root.name or project.id,
                detached_at=datetime.now(tz=UTC),
            )
            _revoke(unrecorded, session, time.monotonic() + iam.HTTP_TIMEOUT_SECONDS)
        raise
    return MintedKey(uid=uid, name=str(body.get("name") or ""), path=str(path))


def _key_file_contents(project_id: str) -> str | None:
    """What the project's key file holds now, to put back; ``None`` when there is none to read."""
    try:
        return project_key_path(project_id).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def detach_minted_keys(store: ContextStore) -> list[str]:
    """Sign-out: take every key the CLI minted off its project, each owed a revocation.

    A credential the CLI obtained on the user's behalf must not outlive the
    sign-in that obtained it, so every one is detached — file, binding, uid —
    and its revocation owed from the same commit; the caller revokes them with
    the session BEFORE revoking the session itself, whose Bearer the revoke
    call takes (:func:`forget_minted_keys`). One project's file that will not
    delete does not keep the others' (:func:`detach`). Returns the project ids.
    """
    return [
        destination.project_id
        for destination in store.project_destinations()
        if destination.key_uid and detach(store, destination.project_id)
    ]


@dataclass(frozen=True)
class MintedKeysForgotten:
    """What a sign-out did with the keys the CLI minted: how many it detached, what it revoked."""

    detached: int
    revocations: Revocations


def forget_minted_keys(session: iam.Session) -> MintedKeysForgotten:
    """``logout`` and the Accounts page's *Sign out*: the keys the CLI minted go with the session.

    Every minted key is detached (:func:`detach_minted_keys`) and every key
    owed — these and any an earlier command could not revoke — is revoked with
    ``session``, which must still be live: call this BEFORE the session is
    revoked. What cannot be revoked stays owed and is in the result, for the
    sign-out to say; the next sign-in's ``use`` or ``doctor --live`` tries
    again. Never raises: a store that cannot be read costs these keys' cleanup,
    never the sign-out.
    """
    if not derived_credentials_exist():
        return MintedKeysForgotten(detached=0, revocations=Revocations())
    detached: list[str] = []
    with contextlib.suppress(Exception), store_session() as store:
        detached = detach_minted_keys(store)
    try:
        revocations = revoke_owed(session)
    except Exception:
        revocations = Revocations()
    return MintedKeysForgotten(detached=len(detached), revocations=revocations)


def derived_credentials_exist() -> bool:
    """Cheap gate for ``logout``: is there a store at all to clear anything from."""
    return paths.db_path().exists()


# ── routing: which studio the agents land in ─────────────────────────────────


@dataclass(frozen=True)
class Binding:
    agent: str
    ok: bool
    detail: str
    studio_id: int | None = None


@dataclass
class RosterReport:
    bound: list[Binding] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return bool(self.bound) and all(b.ok for b in self.bound)


def bind_roster(
    destination: TraceDestination, target: ResolvedTarget, *, timeout: float = 6.0
) -> RosterReport:
    """Route this machine's agent identities to the chosen studio, with the workspace key.

    The gateway files a span under the studio its ``agent.name`` is bound to in
    the key's workspace; an unbound name lands in the workspace's Unassigned
    inbox (or is refused, in strict workspaces). So a destination is only
    honoured once the names this machine registers are bound, and this is that
    write: ``PUT /api/v2/iam/workspaces/<id>/agents/<name>/`` per identity.

    With the workspace KEY, not the session: the endpoint takes an API key,
    and the key's owner is who is acting — a workspace OWNER or ADMIN, or the
    studio's owner, may bind; anyone else gets 403 per name, reported and not
    raised, because the destination is still recorded and the next person to
    hold the right role can run it again.
    """
    report = RosterReport()
    if not target.api_key:
        return report
    if destination.studio_id is None:
        return report
    base = destination.api_url.rstrip("/")
    for agent in target.agent_names:
        verdict: HttpVerdict = _request(
            f"{base}/api/v2/iam/workspaces/{destination.workspace_id}/agents/{agent}/",
            api_key=target.api_key,
            body={
                "publication_id": destination.studio_id,
                "notes": f"aisquare-cli: {destination.set_by or 'operator'} chose "
                f"{destination.label} for a project",
            },
            method="PUT",
            timeout=timeout,
        )
        studio = None
        if isinstance(verdict.payload, dict):
            studio = _int(verdict.payload.get("publication_id"))
        detail = "bound" if verdict.ok else _binding_detail(verdict)
        report.bound.append(Binding(agent=agent, ok=verdict.ok, detail=detail, studio_id=studio))
    return report


def _binding_detail(verdict: HttpVerdict) -> str:
    if verdict.status == 401:
        return "the key was not accepted (is it this workspace's ingest key?)"
    if verdict.status == 403:
        return "needs a workspace OWNER/ADMIN key, or the studio owner's"
    if verdict.status == 404:
        return "the studio is not in this workspace any more"
    return verdict.detail


# ── describing it ──────────────────────────────────────────────────────────────


def describe(destination: TraceDestination | None, *, key_source: str | None = None) -> str:
    """``acme / Frontend · stg · chosen by a@b.c`` plus the credential's standing — one renderer."""
    if destination is None:
        return "(none chosen — aisquare explainability use <workspace>/<studio>)"
    parts = [destination.label, destination.environment]
    if destination.set_by:
        parts.append(f"chosen by {destination.set_by}")
    text = " · ".join(parts)
    if destination.key_uid:
        text += " — key minted by the CLI"
    elif key_source == "project":
        text += " — key attached by hand"
    elif key_source in ("env", "file"):
        text += " — using the machine key"
    elif key_source is not None:
        text += " — no key yet"
    return text


def as_json(destination: TraceDestination | None) -> dict[str, Any] | None:
    if destination is None:
        return None
    return {
        "workspace": {
            "id": destination.workspace_id,
            "uid": destination.workspace_uid,
            "name": destination.workspace_name,
        },
        "studio": {
            "id": destination.studio_id,
            "uid": destination.studio_uid,
            "name": destination.studio_name,
        },
        "environment": destination.environment,
        "api_url": destination.api_url,
        "key_minted": bool(destination.key_uid),
        "set_at": destination.set_at.isoformat(),
        "set_by": destination.set_by,
    }
