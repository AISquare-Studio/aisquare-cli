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
import re
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from aisquare.core import paths
from aisquare.core.config import AppConfig, ExplainabilitySettings, ExplainabilityTarget
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


def key_env_for(name: str) -> str:
    """``EXPLAINABILITY_<NAME>_API_KEY``: the variable a target this module creates names.

    The shape the settings docstring already shows for a hand-written prod
    target, derived from the target name so an unknown host still yields a
    legal variable name.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "TARGET"
    return f"EXPLAINABILITY_{slug}_API_KEY"


def _machine_key_serves(settings: ExplainabilitySettings, environment: Environment | None) -> bool:
    """Whether the machine's unlabelled key is already this deployment's.

    ``init --explainability`` writes the top-level gateway and the key file
    together, so a top-level gateway equal to the deployment's IS the
    single-deployment machine pointing at it; anything else is a key issued
    for somewhere this function cannot see.
    """
    if environment is None or not settings.gateway_url:
        return False
    return settings.gateway_url.rstrip("/") == environment.gateway_url


def ensure_target(config: AppConfig, api_url: str) -> tuple[str, bool]:
    """Make sure the deployment the session belongs to exists as an explainability target.

    Fills ONLY what is empty: a gateway or proxy an operator set by hand stays.
    Returns the target name and whether the config changed. Does not flip
    ``enabled`` — that is ``explainability enable``'s one job, and a command
    that picks a destination must not silently start tracing.

    A target CREATED here names a key variable of its own
    (:func:`key_env_for`). With the default one, the unlabelled machine key —
    ``~/.aisquare/explainability-key`` or ``$EXPLAINABILITY_API_KEY`` — would
    answer for every deployment anyone signs in to, and ``use`` would bind the
    roster and every launch would authenticate with a key issued for somewhere
    else: the hazard ``tests/test_key_never_crosses_deployments.py`` pins.
    The one exception is the machine whose top-level gateway already is this
    deployment's, where that key is exactly the right one and a new variable
    would only take it away.
    """
    name = environment_name(api_url)
    environment = environment_for(api_url)
    settings = config.explainability
    target = settings.targets.get(name, ExplainabilityTarget())
    changed = name not in settings.targets
    if changed and not _machine_key_serves(settings, environment):
        target.api_key_env = key_env_for(name)
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
    """
    found = _pick_workspace(ref, workspaces)
    if members_only and not found.member:
        raise DestinationError(
            "not_a_member",
            f"you are invited to {found.name} ({found.invite_status or 'pending'}) but not a "
            "member yet — accept the invitation in the web app, then choose it",
        )
    return found


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

    A re-point into ANOTHER workspace drops the key the CLI minted: it was that
    workspace's credential and cannot serve this one, so it is revoked (with
    ``session``, when it belongs to the host that minted it) and forgotten. A
    re-point within the same workspace (another studio) keeps it. "The same
    workspace" is the id on the same API: workspace ids are per deployment, so
    a staging 7 and a production 7 are two workspaces.

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
        _forget_minted_key(store, project.id, previous, session)
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


def forget(
    store: ContextStore, project: ProjectInfo, *, session: iam.Session | None = None
) -> TraceDestination | None:
    """Drop the project's destination and the key the CLI minted for it; the old row, or None.

    The minted key is revoked on the server when ``session`` belongs to the
    host that minted it — dropped locally only, it would stay a live
    ``ingest:write`` credential that nothing on this machine remembers.
    """
    previous = store.project_destination(project.id)
    if previous is None:
        return None
    if previous.key_uid:
        _forget_minted_key(store, project.id, previous, session)
    store.clear_project_destination(project.id)
    return previous


def _same_api(one: str, other: str) -> bool:
    return one.rstrip("/") == other.rstrip("/")


def _forget_minted_key(
    store: ContextStore,
    project_id: str,
    destination: TraceDestination,
    session: iam.Session | None,
) -> None:
    """Revoke and delete a MINTED key: on the server, its file, its binding, its uid on the row.

    Only ever called for a row with ``key_uid``, and that uid is set only while
    the project's key file holds the key the CLI minted: ``key set`` and
    ``key clear`` retire it (:func:`retire_minted_key`) before they touch the
    file. That invariant is what keeps a hand-attached key out of here — the
    file and the binding are the same for both kinds.
    """
    _revoke(destination, session)
    binding = store.project_explainability(project_id)
    if binding is not None and binding.key_path == project_key_path(project_id):
        clear_project_api_key(project_id)
        store.clear_project_explainability(project_id)
    store.set_project_destination_key(project_id, None)


def retire_minted_key(
    store: ContextStore, project_id: str, *, session: iam.Session | None = None
) -> None:
    """A key attached (or cleared) by hand takes the minted key's place: revoke it, drop its uid.

    One key file per project serves both kinds, so ``key set`` over a minted
    key overwrites it. Left with its uid, the operator's key would go on being
    described as minted, and ``logout``, ``use --clear`` or a re-point would
    delete it. The FILE is the caller's: it is about to write or clear it.
    Revoked on the server when ``session`` belongs to the host that minted it;
    without one the old key stays in the workspace's key list, named
    ``aisquare-cli <host> <project>``.
    """
    destination = store.project_destination(project_id)
    if destination is None or not destination.key_uid:
        return
    _revoke(destination, session)
    store.set_project_destination_key(project_id, None)


def _revoke(destination: TraceDestination, session: iam.Session | None) -> None:
    """Revoke the destination's minted key on the server — best effort, and only where it lives.

    Only against the API the key was minted on: a session belongs to one host,
    and sending the uid to another one gets a 404 that reads like success. The
    endpoint has the mint's authentication gap and a machine may be offline, so
    a refusal is tolerated, never raised.
    """
    uid = destination.key_uid
    if session is None or not uid or uid == "minted":
        return
    if not _same_api(destination.api_url, session.api_url):
        return
    with contextlib.suppress(iam.IamError):
        iam.request(
            f"api/v2/iam/workspace-api-key/{uid}/revoke/",
            method="POST",
            api_url=session.api_url,
            tolerate=(400, 401, 403, 404),
        )


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

    A KEY THE CLI MINTED BEFORE IS REVOKED: the row's uid now names the new
    one, so the old one would stay a live ``ingest:write`` key that nothing on
    this machine remembers (``use`` mints over one when its file is gone).
    Revoked only once the new key is stored, so a key is never revoked before
    its replacement is in place.
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
    _revoke(destination, session)  # the uid it carried is the key just replaced
    return MintedKey(uid=str(uid or "minted"), name=str(body.get("name") or ""), path=str(path))


def revoke_minted_keys(store: ContextStore, session: iam.Session) -> list[str]:
    """``logout``: forget every key the CLI minted, revoking each on the server when it can.

    The revoke call takes the Bearer, so it must run BEFORE the session itself
    is revoked; it is best effort (the endpoint has the same authentication
    gap as the mint, and a machine may be offline), and the local copy goes
    regardless — a credential the CLI obtained on the user's behalf must not
    outlive the sign-in that obtained it. A key minted on another host is not
    revoked from this one (:func:`_revoke` says why), and one project's file
    that will not delete does not keep the others'. Returns the project ids
    cleared.
    """
    cleared: list[str] = []
    for destination in store.project_destinations():
        if not destination.key_uid:
            continue
        try:
            _forget_minted_key(store, destination.project_id, destination, session)
        except OSError:
            continue
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
