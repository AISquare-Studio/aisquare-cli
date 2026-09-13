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
it, a key attached by hand (#141) is the operator's and is left alone.
"""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from aisquare.core import paths
from aisquare.core.config import AppConfig, ExplainabilityTarget
from aisquare.core.store import ContextStore
from aisquare.models import ProjectInfo, TraceDestination
from aisquare.services import iam
from aisquare.services.explainability import (
    clear_project_api_key,
    project_key_path,
    store_project_api_key,
)
from aisquare.services.explainability_ops import (
    HttpVerdict,
    ResolvedTarget,
    _request,
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
        proxy_url="https://stg-explainability.api.aisquare.studio:9443",
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


def ensure_target(config: AppConfig, api_url: str) -> tuple[str, bool]:
    """Make sure the deployment the session belongs to exists as an explainability target.

    Fills ONLY what is empty: a gateway or proxy an operator set by hand stays.
    Returns the target name and whether the config changed. Does not flip
    ``enabled`` — that is ``explainability enable``'s one job, and a command
    that picks a destination must not silently start tracing.
    """
    name = environment_name(api_url)
    environment = environment_for(api_url)
    settings = config.explainability
    target = settings.targets.get(name, ExplainabilityTarget())
    changed = name not in settings.targets
    if environment is not None:
        if not target.gateway_url:
            target.gateway_url = environment.gateway_url
            changed = True
        if not target.proxy_url and environment.proxy_url:
            target.proxy_url = environment.proxy_url
            changed = True
    if changed:
        settings.targets[name] = target
    return name, changed


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
        found.append(
            Studio(
                id=ident,
                uid=str(row["uid"]) if row.get("uid") else None,
                name=str(row.get("name") or ident),
                workspace_id=_int(row.get("workspace_id") or row.get("workspace")),
                is_default=bool(row.get("is_default")),
                is_inbox=bool(row.get("is_inbox")),
                visibility=str(row["visibility"]) if row.get("visibility") else None,
            )
        )
    return sorted(found, key=lambda s: (s.is_inbox, not s.is_default, s.name.lower()))


def pick_workspace(ref: str, workspaces: list[Workspace]) -> Workspace:
    """A workspace by name (case-insensitive), uid or id; ambiguity and absence are errors."""
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

    A re-point into ANOTHER workspace drops the key the CLI minted: it was that
    workspace's credential and cannot serve this one. A re-point within the
    same workspace (another studio) keeps it.
    """
    key_uid = None
    if previous is not None and previous.workspace_id == workspace.id:
        key_uid = previous.key_uid
    if previous is not None and previous.key_uid and key_uid is None:
        # The minted key was the old workspace's; it cannot serve the new one.
        # The key FILE is dropped too: a binding to the old deployment would
        # otherwise keep answering for a project that moved.
        _forget_minted_key(store, project.id, previous)
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
    """Drop the project's destination and the key the CLI minted for it; the old row, or None."""
    previous = store.project_destination(project.id)
    if previous is None:
        return None
    if previous.key_uid:
        _forget_minted_key(store, project.id, previous)
    store.clear_project_destination(project.id)
    return previous


def _forget_minted_key(store: ContextStore, project_id: str, destination: TraceDestination) -> None:
    """Delete a MINTED key: its file, its binding, its uid on the row. Hand-attached keys stay."""
    binding = store.project_explainability(project_id)
    if binding is not None and binding.key_path == project_key_path(project_id):
        clear_project_api_key(project_id)
        store.clear_project_explainability(project_id)
    store.set_project_destination_key(project_id, None)


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
    """
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
    uid = body.get("uid")
    if result.status not in (200, 201) or not isinstance(value, str) or not value:
        raise DestinationError(
            "api_error", f"key creation answered HTTP {result.status}: {_detail(result.body)}"
        )
    path = store_project_api_key(project.id, value)
    store.set_project_explainability(
        project.id, target=destination.environment, key_path=path, set_by=destination.set_by
    )
    store.set_project_destination_key(project.id, str(uid) if uid else "minted")
    return MintedKey(uid=str(uid or "minted"), name=str(body.get("name") or ""), path=str(path))


def revoke_minted_keys(store: ContextStore, session: iam.Session) -> list[str]:
    """``logout``: forget every key the CLI minted, revoking each on the server when it can.

    The revoke call takes the Bearer, so it must run BEFORE the session itself
    is revoked; it is best effort (the endpoint has the same authentication
    gap as the mint, and a machine may be offline), and the local copy goes
    regardless — a credential the CLI obtained on the user's behalf must not
    outlive the sign-in that obtained it. Returns the project ids cleared.
    """
    cleared: list[str] = []
    for destination in store.project_destinations():
        if not destination.key_uid:
            continue
        if destination.key_uid != "minted":
            # Offline, or a mismatched host: the local copy still goes.
            with contextlib.suppress(iam.IamError):
                iam.request(
                    f"api/v2/iam/workspace-api-key/{destination.key_uid}/revoke/",
                    method="POST",
                    api_url=session.api_url,
                    tolerate=(400, 401, 403, 404),
                )
        _forget_minted_key(store, destination.project_id, destination)
        cleared.append(destination.project_id)
    return cleared


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
