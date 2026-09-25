"""``aisquare explainability`` — inspect, join and wire the session tracing.

``aisquare launch`` wires sessions automatically when the config enables
tracing; these commands cover everything else. ``status`` answers "would a
session launched right now be traced, and if not, why" without launching one,
and ``env`` emits the same env delta as shell exports so a terminal (or a
script) can join a session the launcher does not manage. ``enable`` is the one
command that turns tracing on for this machine, and ``register`` declares this
machine's agent identities to a workspace so its spans are routable at all.

Keys are read from the environment variable the active target names, used for
the one call that needs them, and never written down or echoed.

``env``'s output is quoted for POSIX ``sh``, not for bash. It is composed into
printed spawn commands that people paste anywhere and that CI runs through
``/bin/sh`` — on Debian and Ubuntu that is dash, where bash's ``$'…'`` form is
not special at all. Measured before the fix: ``ANTHROPIC_BASE_URL`` arrived as
``$http://127.0.0.1:9190`` and the agent died with ``API Error: Invalid URL``,
exit 1, nothing reaching the proxy — tracing costing a LAUNCH, which the
fail-open doctrine forbids outright. ``shlex.quote`` single-quotes every byte,
newline included, in every shell.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Annotated

import typer

from aisquare.cli.common import expected_config_write_errors, fail
from aisquare.core import orchestrator, outbox
from aisquare.core.config import load_config, save_config
from aisquare.core.state import get_state
from aisquare.core.store import store_session
from aisquare.models import CheckStatus, ProjectInfo, TraceDestination
from aisquare.services import credits as credits_service
from aisquare.services import destinations as dest
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops as ops
from aisquare.services import iam
from aisquare.services import project as project_service
from aisquare.services.explainability import (
    RESERVED_ENV_VARS,
    clear_project_api_key,
    ship_once,
    shipping_state,
    trace_marker,
    wire_session,
)

app = typer.Typer(
    help="Session tracing through the explainability proxy.",
    no_args_is_help=True,
)
key_app = typer.Typer(
    help="A project's own workspace key (#141): attached per project, never one per machine.",
    no_args_is_help=True,
)
app.add_typer(key_app, name="key")

_PROJECT_OPTION = typer.Option(
    "--project",
    "-P",
    help="Project by codename, name or id prefix (default: the one a launch here joins — "
    "$AISQUARE_TEAM_HUB, else this checkout).",
)


def _project_for(ref: str | None) -> ProjectInfo:
    """The project a key command is about: ``--project``, else the one a launch here joins.

    The default is ``orchestrator.team_project`` — ``AISQUARE_TEAM_HUB``, else
    this checkout — because that is the board ``launch`` and ``team spawn
    --exec`` put an agent on, so it is the project whose key authenticates the
    agent. It is NOT the ``project switch`` pin, which launches ignore: this
    used to resolve through the pin, so with project X pinned, ``env`` in
    project Y — and the printed ``team spawn`` line that evals it — handed Y's
    agent X's workspace key while ``launch`` in the same directory used Y's
    (review of #170). It opens no store either, so ``env`` and ``status`` stay
    reads that a damaged ``context.db`` cannot fail.
    """
    if ref is None:
        return orchestrator.team_project(None)
    try:
        return project_service.resolve(ref)
    except KeyError:
        fail(f"no project matches '{ref}'", error="not_found", ref=ref)
    except ValueError as exc:
        fail(str(exc), error="ambiguous_project", ref=ref)


def _key_project_id(ref: str | None) -> str | None:
    """The project id a key is resolved FOR in ``status``, ``env`` and ``register``.

    A ``--project`` that names nothing fails loudly; the default never fails:
    these are the reads a launch is prepared with, and before #141 none of
    them touched the store, so a directory that cannot be resolved costs the
    project's key — the machine's answers — never the command (review of #170).
    """
    if ref is not None:
        return _project_for(ref).id
    try:
        return _project_for(None).id
    except Exception:  # a project is decoration on these reads, never their gate
        return None


def _key_payload(project: ProjectInfo, target: str | None) -> dict[str, object]:
    binding = ops.project_key_binding(project.id)
    present = binding is not None and binding.key_path.is_file()
    return {
        "project": project.id,
        "name": project.root.name or project.id,
        "attached": binding is not None,
        "target": binding.target if binding is not None else None,
        "key_path": str(binding.key_path) if binding is not None else None,
        "file_present": present,
        "set_at": binding.set_at.isoformat() if binding is not None else None,
        "set_by": binding.set_by if binding is not None else None,
        "resolves_for": target,
    }


@key_app.command("set")
def key_set(
    project_ref: Annotated[str | None, _PROJECT_OPTION] = None,
    target_name: Annotated[str | None, _TARGET_OPTION] = None,
    from_env: Annotated[
        str | None,
        typer.Option(
            "--from-env",
            help="Read the key from this environment variable instead of stdin. "
            "The key is never taken from the command line.",
        ),
    ] = None,
) -> None:
    """Attach a workspace key to ONE project, for ONE deployment.

    The key comes from stdin (`echo "$KEY" | aisquare explainability key set`) or
    from a named variable (`--from-env MY_KEY`), never from an argument — argv is
    in every process list and shell history. It lands in the project's data
    directory at mode 600; the store records only the deployment and the path.
    Launches, `fleet spawn` and `explainability env` for this project then
    authenticate the proxy with it; other projects keep the machine key. A
    project not registered yet is registered here: attaching a key is a
    deliberate act, like `team on`.
    """
    project = _project_for(project_ref)
    settings = load_config().explainability
    known = sorted({settings.target, *settings.targets})
    if target_name is not None and target_name not in known:
        # `resolve_target` answers for any name, so a typo (`--target prdo`)
        # bound the key to a deployment nothing ever resolves and still printed
        # success (review of #170). Refused before the key is read or written.
        fail(
            f"no target '{target_name}' on this machine (known: {', '.join(known)}) — "
            f"create it first: aisquare explainability enable --target {target_name} "
            "--gateway-url <url>",
            error="unknown_target",
            ref=target_name,
        )
    # With the project (#142): its destination names the deployment when
    # `--target` does not, so the key lands where the project's traces are
    # resolved — the fallback `use` points at when the API will not mint one.
    target = ops.resolve_target(settings, target_name, project_id=project.id).name
    if from_env is not None:
        value = os.environ.get(from_env, "").strip()
        if not value:
            fail(f"${from_env} is not set or empty", error="no_key")
    else:
        if sys.stdin.isatty():
            fail(
                'pipe the key on stdin (echo "$KEY" | aisquare explainability key set) or '
                "name a variable with --from-env — it is never taken from the command line",
                error="no_key",
            )
        value = sys.stdin.read().strip()
        if not value:
            fail("nothing on stdin — the key was empty", error="no_key")
    # The same file holds a key the CLI minted (#142): this one replaces it. The
    # new binding and the minted key's detachment are one commit, which owes the
    # minted key's revocation; a write or a binding that fails leaves the file,
    # the binding and the uid as they were. The revoke is made once the store is
    # closed, and a key it cannot revoke yet stays owed, and is said (review of
    # #172).
    binding = ops.attach_project_key(project, value, target=target)
    revocations = dest.revoke_owed(iam.signed_in_quietly(), project_ids={project.id})
    payload = _key_payload(project, target)
    if get_state().json_output:
        typer.echo(json.dumps({**payload, "revocations": revocations.as_json()}))
        return
    name = project.root.name or project.id
    # The register step, named: a key for ANOTHER workspace traces nothing until
    # that workspace knows this machine's agent identities — every span is
    # refused 409 agent_not_registered — and `register` resolves the same
    # project's key as the launches do.
    register = shlex.join(
        [
            "aisquare",
            "explainability",
            "register",
            *(["--project", project_ref] if project_ref is not None else []),
            "--target",
            binding.target,
        ]
    )
    typer.echo(
        f"✓ key attached to {name} for target {binding.target} — {binding.key_path} "
        "(mode 600); launches and spawns in this project authenticate the proxy with it. "
        f"If that workspace has not registered this machine's agents yet: {register}"
    )
    _say_revocations(revocations)


@key_app.command("show")
def key_show(
    project_ref: Annotated[str | None, _PROJECT_OPTION] = None,
    target_name: Annotated[str | None, _TARGET_OPTION] = None,
) -> None:
    """Where this project's key comes from — the origin only, never the value."""
    project = _project_for(project_ref)
    settings = load_config().explainability
    resolved = ops.resolve_target(settings, target_name, project_id=project.id)
    payload = _key_payload(project, resolved.name)
    payload["key_source"] = resolved.key_source
    payload["key_origin"] = resolved.key_origin
    payload["key_set"] = bool(resolved.api_key)
    if get_state().json_output:
        typer.echo(json.dumps(payload))
        return
    name = project.root.name or project.id
    binding = ops.project_key_binding(project.id)
    if binding is None:
        typer.echo(
            f"{name}: no key of its own — target {resolved.name} resolves {resolved.key_origin}"
        )
        return
    where = str(binding.key_path) + ("" if binding.key_path.is_file() else " (file MISSING)")
    match = "" if binding.target == resolved.name else f" — not used for target {resolved.name}"
    typer.echo(
        f"{name}: project key for target {binding.target} at {where}, set "
        f"{binding.set_at:%Y-%m-%d %H:%M} by {binding.set_by or 'unknown'}{match}"
    )


@key_app.command("clear")
def key_clear(project_ref: Annotated[str | None, _PROJECT_OPTION] = None) -> None:
    """Detach the project's key and delete its file; the machine key applies again."""
    project = _project_for(project_ref)
    with store_session() as store:
        # A minted key (#142) goes with its binding, its revocation owed in the
        # same commit; revoked below, once the store is closed.
        had_row = store.clear_project_explainability(project.id)
    had_file = clear_project_api_key(project.id)
    revocations = dest.revoke_owed(iam.signed_in_quietly(), project_ids={project.id})
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "project": project.id,
                    "cleared": had_row or had_file,
                    "revocations": revocations.as_json(),
                }
            )
        )
        return
    name = project.root.name or project.id
    if not (had_row or had_file):
        typer.echo(f"{name} had no key of its own — nothing to clear")
    else:
        typer.echo(f"✓ key cleared for {name} — the machine key applies again")
    _say_revocations(revocations)


_TARGET_OPTION = typer.Option("--target", help="Deployment to act on, e.g. stg or prod.")


# ── where traces land, chosen while signed in (#142) ─────────────────────────


def _credits_for(target: ops.ResolvedTarget) -> credits_service.WorkspaceCredits | None:
    """The destination workspace's credits (#143), or ``None`` when there is nothing to ask."""
    if target.destination is None:
        return None
    try:
        session = iam.current_session()
    except iam.IamError:
        return None
    return credits_service.for_destination(session, target.destination)


def _session_or_fail() -> iam.Session:
    try:
        session = iam.current_session()
    except iam.IamError as exc:
        fail(exc.message, error=exc.code)
    if session is None:
        fail("Not signed in. Run aisquare login.", error="not_authenticated")
    return session


def _say_revocations(report: dest.Revocations) -> None:
    """The revokes a command made of keys the CLI minted (#142), and what is still live."""
    line = dest.describe_revocations(report)
    if line is not None:
        typer.echo(f"{'⚠' if report.owed else '✓'} {line}")


def _workspace_rows(found: list[dest.Workspace]) -> list[dict[str, object]]:
    return [
        {
            "id": w.id,
            "uid": w.uid,
            "name": w.name,
            "type": w.type,
            "role": w.role,
            "invite_status": w.invite_status,
            "member": w.member,
        }
        for w in found
    ]


@app.command()
def workspaces() -> None:
    """List the workspaces you can see — where a project's traces can land.

    Read with the sign-in session (``aisquare login``); no key is involved.
    Members first, then pending invitations, which are listed so the answer to
    "why can't I pick it" is on screen rather than in the web app.
    """
    session = _session_or_fail()
    try:
        found = dest.list_workspaces(session)
    except iam.IamError as exc:
        fail(exc.message, error=exc.code)
    except dest.DestinationError as exc:
        fail(exc.message, error=exc.code)
    if get_state().json_output:
        typer.echo(json.dumps(_workspace_rows(found)))
        return
    if not found:
        typer.echo("no workspaces — you are not a member of any yet")
        return
    width = max(len(w.name) for w in found)
    for w in found:
        standing = w.role.lower() if w.role else f"invited ({w.invite_status or 'pending'})"
        typer.echo(f"{w.name:<{width}}  {standing:<18} id {w.id}" + (f"  {w.uid}" if w.uid else ""))


def _workspace_for(
    session: iam.Session, ref: str | None, project_ref: str | None
) -> dest.Workspace:
    """``--workspace`` by name/uid/id, else the project's chosen workspace.

    The project is :func:`_project_for`'s: ``--project``, else the one a launch here joins.
    """
    if ref is not None:
        return dest.pick_workspace(ref, dest.list_workspaces(session))
    project = _project_for(project_ref)
    with store_session() as store:
        chosen = store.project_destination(project.id)
    if chosen is None:
        fail(
            f"{project.root.name or project.id} has no destination yet — name one: "
            "aisquare explainability studios --workspace <name>",
            error="no_destination",
        )
    return dest.Workspace(
        id=chosen.workspace_id, uid=chosen.workspace_uid, name=chosen.workspace_name
    )


@app.command()
def studios(
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", "-w", help="Workspace by name, uid or id."),
    ] = None,
    project_ref: Annotated[str | None, _PROJECT_OPTION] = None,
) -> None:
    """List a workspace's studios (default: this project's chosen workspace)."""
    session = _session_or_fail()
    try:
        chosen = _workspace_for(session, workspace, project_ref)
        found = dest.list_studios(session, chosen)
    except iam.IamError as exc:
        fail(exc.message, error=exc.code)
    except dest.DestinationError as exc:
        fail(exc.message, error=exc.code)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "workspace": {"id": chosen.id, "uid": chosen.uid, "name": chosen.name},
                    "studios": [
                        {
                            "id": s.id,
                            "uid": s.uid,
                            "name": s.name,
                            "default": s.is_default,
                            "inbox": s.is_inbox,
                            "visibility": s.visibility,
                        }
                        for s in found
                    ],
                }
            )
        )
        return
    if not found:
        typer.echo(f"{chosen.name}: no studios you can see")
        return
    width = max(len(s.name) for s in found)
    for s in found:
        marks = " ".join(m for m, on in (("default", s.is_default), ("inbox", s.is_inbox)) if on)
        typer.echo(f"{s.name:<{width}}  id {s.id}" + (f"  ({marks})" if marks else ""))


def _moved(previous: TraceDestination, row: TraceDestination) -> bool:
    """Whether ``use`` re-pointed the project into another workspace (ids are per API)."""
    return (previous.workspace_id, previous.api_url) != (row.workspace_id, row.api_url)


def _routing_lines(report: dest.RosterReport) -> list[str]:
    return [f"{b.agent} → {'bound' if b.ok else 'not bound: ' + b.detail}" for b in report.bound]


def _next_check(target: ops.ResolvedTarget, project_ref: str | None) -> str:
    """The step ``use`` names once tracing is on: one that resolves the key ``use`` set up.

    ``doctor`` resolves the MACHINE's key and opens no store, so the project's
    own key — minted or attached by hand — is invisible to it: it fails the
    config row and tells the operator to export a machine key, the one thing
    that never stands in for the project's. ``explainability status`` resolves
    the project's key and probes the destination's proxy. With no key at all
    the next step is attaching one, not a check. ``doctor --live`` stays for a
    machine key, which it resolves too and is the one check that puts it to the
    gateway. ``--target`` pins the destination's deployment whatever the shell
    exports, and ``--project`` is repeated when ``use`` was given one.
    """
    name = shlex.quote(target.name)
    project = f" --project {shlex.quote(project_ref)}" if project_ref is not None else ""
    if target.key_source == "project":
        return f"aisquare explainability status --target {name}{project}"
    if target.key_source == "unset":
        return (
            f"aisquare explainability key set --from-env VAR --target {name}{project}"
            "   (the project has no key for this destination yet)"
        )
    return f"aisquare doctor --live --target {name}"


@app.command()
def use(
    destination: Annotated[
        str | None,
        typer.Argument(
            help="WORKSPACE or WORKSPACE/STUDIO, each by name, uid or id; without a studio, "
            "the workspace's default studio is used when it has one.",
            show_default=False,
        ),
    ] = None,
    project_ref: Annotated[str | None, _PROJECT_OPTION] = None,
    no_key: Annotated[
        bool,
        typer.Option("--no-key", help="Record the choice without obtaining an ingest key."),
    ] = False,
    clear: Annotated[
        bool, typer.Option("--clear", help="Forget the project's destination instead.")
    ] = False,
) -> None:
    """Pick where ONE project's traces land: a workspace and a studio, as the signed-in user.

    What it does, in order, and each step is reported: records the choice per
    project; makes the deployment the session belongs to an explainability
    target (gateway and proxy filled from the environment, nothing typed);
    obtains a workspace ingest key on your behalf when the project has none for
    that deployment (the API refuses this for a sign-in token today — the
    message names the backend issue and the ``key set`` fallback); and, with
    the project's own key, binds this machine's agent identities to the
    studio, which is what makes spans land THERE rather than in the
    workspace's inbox. Tracing itself stays off
    until ``aisquare explainability enable`` — picking a destination must not
    silently start sending.

    Idempotent: re-running with the same destination changes nothing; a
    different workspace revokes and drops the key the CLI minted for the old
    one. Only the project's own key skips the mint — a machine key was issued
    for whichever workspace set the machine up, so it only answers meanwhile.
    Every key the CLI minted and could not revoke yet (signed out, another
    host, offline) is tried again here, and what is still live is said.
    """
    project = _project_for(project_ref)
    pname = project.root.name or project.id
    if clear:
        with store_session() as store:
            previous = dest.forget(store, project)
        revocations = dest.revoke_owed(iam.signed_in_quietly(), project_ids={project.id})
        if get_state().json_output:
            typer.echo(
                json.dumps(
                    {
                        "project": project.id,
                        "cleared": dest.as_json(previous),
                        "revocations": revocations.as_json(),
                    }
                )
            )
            return
        if previous is None:
            typer.echo(f"{pname} had no destination")
        else:
            typer.echo(f"✓ {pname} no longer points at {previous.label}")
        _say_revocations(revocations)
        return
    if destination is None:
        fail(
            "name a destination: WORKSPACE or WORKSPACE/STUDIO — see: aisquare explainability "
            "workspaces",
            error="usage",
        )
    session = _session_or_fail()
    workspace_ref, _, studio_ref = destination.partition("/")
    try:
        workspace = dest.pick_workspace(
            workspace_ref, dest.list_workspaces(session), members_only=True
        )
        studios_seen = dest.list_studios(session, workspace)
        studio = dest.pick_studio(studio_ref or None, studios_seen, workspace)
    except iam.IamError as exc:
        fail(exc.message, error=exc.code)
    except dest.DestinationError as exc:
        fail(exc.message, error=exc.code)

    config = load_config()
    target_name, changed = dest.ensure_target(config, session.api_url)
    if changed:
        with expected_config_write_errors():
            save_config(config)
    with store_session() as store:
        previous = store.project_destination(project.id)
        row = dest.choose(store, project, workspace, studio, session, previous=previous)
        # By the destination's own deployment, never the shell's: whether the
        # project already has THIS destination's key must not depend on an
        # exported $AISQUARE_EXPLAINABILITY_TARGET, or every `use` under it
        # mints again, and the roster is bound with the other deployment's key.
        target = ops.resolve_target(config.explainability, target_name, project_id=project.id)
        key_note: str
        minted = None
        # Only the PROJECT's own key is taken as this destination's credential.
        # A machine key — the target's variable, or the file — was issued for
        # whichever workspace set the machine up, so it never stands in for a
        # mint; it only answers meanwhile, and the line says it is unchecked.
        if target.key_source == "project":
            key_note = "the project's own key"
            if previous is not None and not row.key_uid and _moved(previous, row):
                key_note += (
                    f" — attached by hand while it pointed at {previous.label}; if it is not "
                    f"{row.workspace_name}'s, attach that workspace's: aisquare explainability "
                    "key set"
                )
        elif no_key:
            key_note = "none — skipped (--no-key)"
        else:
            try:
                minted = dest.mint_key(store, project, row, session)
                row = store.project_destination(project.id) or row
                target = ops.resolve_target(
                    config.explainability, target_name, project_id=project.id
                )
                key_note = f"minted on your behalf → {minted.path} (mode 600)"
            except iam.IamError as exc:
                key_note = f"none — {exc.message}"
            except dest.DestinationError as exc:
                key_note = f"none — {exc.message}"
        machine = {
            "env": f"${target.api_key_env} from this shell",
            "file": "the machine key file",
        }.get(target.key_source)
        if machine is not None:
            key_note += (
                f"; meanwhile {machine} answers — a machine key, not checked to be "
                f"{row.workspace_name}'s"
            )
    # With the project's own key only, as the note above takes it: a machine key
    # was issued for whichever workspace set the machine up, so binding this
    # workspace's roster with it is the stand-in the mint refuses — and the line
    # that called it "not checked to be <workspace>'s" bound with it all the same
    # (review of #172).
    own_key = target.key_source == "project" and bool(target.api_key)
    routing = dest.bind_roster(row, target) if own_key else dest.RosterReport()
    # Outside the store session, and every key owed, not only a replaced one:
    # `use` is where a signed-in operator lands, so it is one of the retries.
    revocations = dest.revoke_owed(session)

    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "project": project.id,
                    "name": pname,
                    "destination": dest.as_json(row),
                    "target": {
                        "name": target.name,
                        "gateway": target.gateway_url,
                        "proxy": target.proxy_url,
                        "enabled": config.explainability.enabled,
                    },
                    "key": {
                        "source": target.key_source,
                        "minted": minted is not None,
                        "note": key_note,
                    },
                    "routing": [
                        {
                            "agent": b.agent,
                            "bound": b.ok,
                            "detail": b.detail,
                            "studio_id": b.studio_id,
                        }
                        for b in routing.bound
                    ],
                    "revocations": revocations.as_json(),
                }
            )
        )
        return
    typer.echo(f"✓ traces from {pname} land in {row.label} ({row.environment})")
    proxy = f"  [proxy {target.proxy_url}]" if target.proxy_url else ""
    typer.echo(f"  target:   {target_name} → {target.gateway_url or '(no gateway known)'}{proxy}")
    typer.echo(f"  key:      {key_note}")
    if routing.bound:
        typer.echo(f"  routing:  {'; '.join(_routing_lines(routing))}")
    elif target.api_key:
        typer.echo(
            f"  routing:  not applied — a machine key is not checked to be {row.workspace_name}'s; "
            "attach the workspace's key (aisquare explainability key set) and run use again"
        )
    else:
        typer.echo("  routing:  not applied — no key to bind the agent identities with")
    revoked = dest.describe_revocations(revocations)
    if revoked is not None:
        typer.echo(f"  revoke:   {revoked}")
    if not config.explainability.enabled:
        typer.echo("  next:     aisquare explainability enable   (tracing is off on this machine)")
    else:
        typer.echo(f"  next:     {_next_check(target, project_ref)}")


@app.command()
def status(
    target_name: Annotated[str | None, _TARGET_OPTION] = None,
    project_ref: Annotated[str | None, _PROJECT_OPTION] = None,
) -> None:
    """Show the tracing config and whether the proxy would accept a session.

    Exits non-zero only when tracing is enabled and the proxy lane is RED --
    ``ProxyState.problem``, the same verdict ``doctor`` and the fleet tab
    render. Red is two states, and the second is newer than the first: the
    proxy would not take a session (launches silently fall back to untraced),
    or the proxy is alive and REPORTS that it ships to another deployment than
    the target, so sessions are traced onto a gateway nobody is watching. Both
    are "the traces are not arriving where you think", which is what a cutover
    script gating on this code is asking, so the second case joined without a
    flag day. Amber -- a destination that cannot be checked from here -- exits
    0; ``probe_severity`` in the JSON says which.

    Honours ``--json``, because this is the command a cutover gets scripted
    against: without it every check in the runbook is a grep against prose,
    and prose is the part most likely to be reworded.
    """
    config = load_config()
    settings = config.explainability
    # The key is resolved FOR the project a launch from here joins (#141) — the
    # one `env`, `launch` and the key commands use — so the origin shown is the
    # key a launch would authenticate with; a project with its own key shows
    # that, everything else the machine's. `--project` names another one — the
    # check `use --project` sends the operator to (#142).
    target = ops.resolve_target(settings, target_name, project_id=_key_project_id(project_ref))
    # One description of the proxy lane for both surfaces. It also decides
    # whether to probe at all: a machine that never configured tracing has
    # nothing to dial, and reporting a refused connection to a default address
    # the operator never chose reads as a broken machine when nothing is wrong.
    proxy = ops.proxy_state(target, on=settings.enabled)
    state = shipping_state(target_name)
    level = config.redaction.level
    # WHERE the counters count, resolved once for both renderings. The counter
    # says "spool" and the directory is `queue/`: the word is this codebase's
    # (insight_sweeper, "drain the spool") and the path is not it, which cost a
    # senior engineer ninety minutes and produced a false "the spool is empty"
    # while the record was on disk. Nothing shipped points at a WRONG path —
    # the gap was that the tool never said the right one.
    #
    # Fail open: this is decoration on a status line, and `status`'s exit code
    # has exactly one documented meaning (tracing on, proxy refusing). A home
    # that cannot be resolved costs the path, never the command.
    try:
        queue_dir: str | None = str(outbox.queue_dir())
    except Exception:
        queue_dir = None
    # The workspace's credits (#143): ONE request, cached a minute, only when a
    # destination is chosen and a session exists for its host — otherwise the
    # line is not shown at all. Never a reason for `status` to fail.
    credits = _credits_for(target)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "enabled": settings.enabled,
                    "target": target.name,
                    "gateway": target.gateway_url,
                    "gateway_source": target.gateway_source,
                    # Where `key_project`'s traces land (#142); null until chosen.
                    "destination": dest.as_json(target.destination),
                    # That workspace's credits (#143); null until a destination is chosen.
                    "credits": credits.as_json() if credits is not None else None,
                    "key_env": target.api_key_env,
                    "key_set": bool(target.api_key),
                    # `key_set` alone said "the named variable holds a key",
                    # which stopped being true when the resolver gained the
                    # key-file fallback. `key_source` is the field a script
                    # should branch on; `key_env` stays the variable the target
                    # NAMES, set or not.
                    "key_source": target.key_source,
                    "key_origin": target.key_origin,
                    # The project the key was resolved FOR (#141), so a script
                    # can tell which project's origin it is reading.
                    "key_project": target.project_id,
                    "proxy": target.proxy_url,
                    "identity": target.agent_name_template,
                    "agents": list(target.agent_names),
                    "probe": proxy.summary,
                    # The verdict as a FIELD, not only as prose in `probe`. A
                    # script watching for a misroute had to regex an English
                    # sentence that this PR is free to reword; `probe_severity`
                    # is the same vocabulary `doctor --json` publishes.
                    "probe_severity": str(proxy.severity),
                    "probe_fix": proxy.remediation or None,
                    "redaction": str(level),
                    # The spool counters live HERE, not under a top-level
                    # "spool", even though the human view below prints them on
                    # a line of their own. Both surfaces render ONE
                    # shipping_state object; a second path to the same three
                    # integers would give this payload two answers and no
                    # canonical one. The runbook promised `.spool` and it never
                    # existed — `jq -r` answers a missing key with null and
                    # exits 0, so the command written to catch a silent backlog
                    # was itself silent. Fixed on the page rather than here,
                    # which is only safe while these three stay reachable:
                    # tests/test_spool_counters_agree.py pins that they agree
                    # with the human line, and that no top-level "spool"
                    # quietly reappears.
                    "shipping": {
                        "gateway": state.gateway_url,
                        "reason": state.reason,
                        "queued": state.queued,
                        "sent": state.sent,
                        "dead": state.dead,
                        # Beside the counters rather than at the top level: a
                        # script that reads the numbers is the one that wants
                        # the directory, and a second home for the same subject
                        # would give this payload two answers.
                        "queue_dir": queue_dir,
                    },
                },
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(f"enabled:  {settings.enabled}")
        typer.echo(f"target:   {target.name}")
        typer.echo(f"gateway:  {target.gateway_url or '(unset)'} [{target.gateway_source}]")
        # `destination:` — the same word as the JSON key, because
        # tests/test_redaction_surface.py holds every human label to a key.
        described = dest.describe(target.destination, key_source=target.key_source)
        typer.echo(f"destination: {described}")
        if credits is not None:
            typer.echo(f"credits:  {credits_service.describe(credits)}")
        typer.echo(f"key:      {target.key_origin} {'is set' if target.api_key else 'is NOT set'}")
        typer.echo(f"proxy:    {target.proxy_url}")
        typer.echo(f"identity: {target.agent_name_template}")
        typer.echo(f"agents:   {', '.join(target.agent_names) or '(none)'}")
        typer.echo(f"probe:    {proxy.summary}")
        # "A red line without its next command is half a doctor" -- this
        # module's own rule, and the amber verdict reached the operator without
        # one: `status` and the fleet tab both rendered `summary` and dropped
        # `remediation`, so the only surface carrying the fix was `doctor`.
        if proxy.remediation and proxy.severity is not CheckStatus.ok:
            typer.echo(f"          → {proxy.remediation}")
        typer.echo(f"shipping: {state.reason}")
        # On THIS line and not a new one: "how much is queued" and "where is it"
        # are one question, and the empty case is exactly when someone goes
        # looking — so the path is printed at 0 queued too.
        located = f" — {queue_dir}" if queue_dir else ""
        typer.echo(
            f"spool:    {state.queued} queued, {state.sent} sent, {state.dead} dead-letter{located}"
        )
        # Directly under the spool counts on purpose: "how much am I sending"
        # and "what is in it" are one question, and an operator who reads the
        # first without the second is the person this line exists for.
        typer.echo(f"redaction: {ops.redaction_summary(level)}")
    # Non-zero exactly when the lane is red -- the ONE derived verdict, so this
    # cannot disagree with what `doctor` and the tab render. Red is the proxy
    # refusing a session OR a live proxy shipping to another deployment (see
    # the docstring); amber is not red. This read a separate `healthy` boolean
    # until it was removed, and agreed with the severity only because every
    # construction site happened to set both consistently.
    if settings.enabled and proxy.problem:
        raise typer.Exit(code=1)


@app.command()
def enable(
    target_name: Annotated[str | None, _TARGET_OPTION] = None,
    gateway_url: Annotated[
        str | None,
        typer.Option("--gateway-url", help="Explainability gateway base URL for the target."),
    ] = None,
    key_env: Annotated[
        str | None,
        typer.Option(
            "--key-env",
            help="Name of the environment variable holding the workspace key. "
            "The key itself is never stored.",
        ),
    ] = None,
    proxy_url: Annotated[
        str | None,
        typer.Option(
            "--proxy-url", help="claude_code proxy the launcher should point sessions at."
        ),
    ] = None,
    identity: Annotated[
        str | None,
        typer.Option("--identity", help="Agent name template, e.g. 'aisquare-{role}'."),
    ] = None,
) -> None:
    """Turn session tracing on for this machine (and set up a target).

    This is the one command that flips the switch. Every option is optional:
    with none of them it just enables tracing against the configured target,
    and repeating it with ``--target prod --gateway-url …`` is how a machine
    gains a second deployment without editing config by hand.
    """
    config = load_config()
    settings = config.explainability
    try:
        name = explainability_service.configure_target(
            config,
            target_name=target_name,
            gateway_url=gateway_url,
            key_env=key_env,
            proxy_url=proxy_url,
            identity=identity,
        )
    except ValueError as exc:
        # The writer refused a URL or an identity template and changed nothing.
        # One `✗` line naming the fix rather than a stored value that fails
        # later: `--gateway-url stg.example` is the runbook command four
        # characters short, and this command used to store it -- after which
        # the proxy lane read green over a gateway nothing could reach.
        fail(str(exc), error="bad-setting")
    with expected_config_write_errors():
        save_config(config)

    resolved = ops.resolve_target(settings, name)
    if get_state().json_output:
        # Deliberately the field NAMES `status` already publishes rather than a
        # second vocabulary for the same facts: two runbooks tell the operator
        # to read `.shipping.gateway` off `status`, and a script that learns
        # `gateway` there should not have to learn `gateway_url` here.
        typer.echo(
            json.dumps(
                {
                    "enabled": settings.enabled,
                    "target": resolved.name,
                    "gateway": resolved.gateway_url,
                    "key_env": resolved.api_key_env,
                    "key_set": bool(resolved.api_key),
                    "key_source": resolved.key_source,
                    "key_origin": resolved.key_origin,
                    "proxy": resolved.proxy_url,
                    "identity": resolved.agent_name_template,
                    "agents": list(resolved.agent_names),
                }
            )
        )
        return
    typer.echo(f"✓ tracing enabled for target '{resolved.name}'")
    typer.echo(f"  gateway:  {resolved.gateway_url or '(unset)'}")
    typer.echo(f"  key from: {resolved.key_origin} {'(set)' if resolved.api_key else '(NOT set)'}")
    typer.echo(f"  proxy:    {resolved.proxy_url}")
    typer.echo(f"  agents:   {', '.join(resolved.agent_names) or '(none)'}")
    typer.echo("  next:     aisquare doctor --live")


@app.command()
def disable() -> None:
    """Turn session tracing off for this machine (targets are kept)."""
    config = load_config()
    config.explainability.enabled = False
    with expected_config_write_errors():
        save_config(config)
    # Config is ours; the operator's shell is not. §5 tells them to export these,
    # so after `disable` the config says off while THIS shell still routes model
    # traffic through the proxy — and the next rollback step stops that proxy,
    # leaving every launch here pointed at a dead port. The launcher cannot help:
    # an ANTHROPIC_* with no marker beside it is a gateway the operator set up and
    # is theirs to keep, and the tracing block is skipped entirely when config is
    # off so the default launch stays byte-identical. A child cannot unset a
    # variable in its parent's shell either, so telling is the only honest move.
    #
    # Narrow on purpose: only when the value IS the proxy this machine was
    # configured to use, and only when that proxy was CHOSEN. Without the second
    # condition this fires on the shipped default 127.0.0.1:9090 — the address
    # this project documents as someone else's long-running proxy — and tells an
    # operator to unset a variable pointing at their own service.
    #
    # Decided ONCE, above both renderings. It was briefly written twice — the
    # condition in the JSON branch and again below — which is two answers to one
    # question waiting for someone to edit one of them. `status` states the same
    # rule about its spool counters for the same reason.
    target = ops.resolve_target(config.explainability, None)
    ambient = os.environ.get(RESERVED_ENV_VARS[0])
    stale = (
        ambient
        if ambient and ambient == target.proxy_url and target.proxy_source != "default"
        else None
    )

    if get_state().json_output:
        # The stale-export warning is the only thing here an operator ACTS on,
        # so it survives into the machine-readable form rather than being
        # dropped as decoration. An explicit null says "checked, nothing set",
        # which a caller cannot otherwise tell from "never looked".
        typer.echo(
            json.dumps(
                {
                    "enabled": False,
                    "target": target.name,
                    "stale_shell_export": (
                        None
                        if stale is None
                        else {"variable": RESERVED_ENV_VARS[0], "value": stale}
                    ),
                }
            )
        )
        return

    typer.echo("✓ tracing disabled — sessions launch untraced, targets left in place")
    if stale is not None:
        names = " ".join(RESERVED_ENV_VARS)
        typer.echo(
            f"  note: this shell still exports {names.split()[0]}={stale} — "
            "launches from here keep using the proxy and will fail once it stops. "
            f"We cannot change your shell: unset {names}"
        )


@app.command()
def register(
    target_name: Annotated[str | None, _TARGET_OPTION] = None,
    role: Annotated[
        list[str] | None,
        typer.Option("--role", help="Role to register; repeat for several. Defaults to config."),
    ] = None,
    project_ref: Annotated[str | None, _PROJECT_OPTION] = None,
) -> None:
    """Declare this machine's agent identities to the workspace.

    Spans whose ``agent.name`` the workspace does not know are rejected, so a
    fresh deployment traces nothing until this runs. Idempotent: an already
    registered name returns its existing publication id.

    The workspace is the one the project's key names when it has its own
    (#141) — the key its launches authenticate with — else the machine's.
    Registering at machine level only left a project pointed at another
    workspace with every span refused 409 (review of #170).
    """
    settings = load_config().explainability
    target = ops.resolve_target(settings, target_name, project_id=_key_project_id(project_ref))
    if not target.gateway_url:
        fail(
            f"target '{target.name}' has no gateway URL — set one with: "
            f"aisquare explainability enable --target {target.name} --gateway-url <url>",
            error="unconfigured",
        )
    if not target.api_key:
        fail(
            f"${target.api_key_env} is not set in this shell — export the workspace key "
            "there (it is never stored by the CLI) and re-run",
            error="no-key",
        )

    if role:
        try:
            names = tuple(target.agent_name_template.format(role=r) for r in role)
        except (KeyError, IndexError, ValueError) as exc:
            fail(
                f"agent_name_template {target.agent_name_template!r} is invalid ({exc})",
                error="bad-template",
            )
    else:
        names = target.agent_names
        # A role this CLI can launch but this TARGET's roster does not list —
        # off `target.roles`, the resolved one, because a per-target `roles`
        # override is exactly the configuration this hint is for. Named here,
        # where registering is one flag away, and carried into the --json
        # payload below so automation sees the same gap.
        unlisted = ops.unregistered_roles(target)
        if unlisted and not get_state().json_output:
            typer.echo(f"note: {ops.unregistered_roles_note(unlisted, target)}", err=True)
    if not names:
        fail("no agent identities to register — check explainability.roles", error="no-agents")

    verdict = ops.register_roster(target, names)
    if not verdict.ok:
        fail(
            f"registration refused by {target.gateway_url}: {verdict.detail}",
            error="register-failed",
            hint="a workspace key is required here; a studio-scoped key cannot declare a roster",
        )

    published = ops.publication_ids(verdict.payload)
    if get_state().json_output:
        # Every FAILURE above answers in JSON through the shared `fail` helper,
        # so before this the flag was honoured on five branches that fail and
        # ignored on the one that works — §1 of the runbook, and the first step
        # that touches the gateway.
        #
        # One key, and it is the code's own word for the mapping
        # (`publication_ids`). The two cases the human output distinguishes —
        # an id, or registered with none — are an id or a null, so a caller
        # reads `.publications["aisquare-coder"]` and gets the same answer the
        # operator reads. A separate list of names would repeat the keys.
        typer.echo(
            json.dumps(
                {
                    "target": target.name,
                    "publications": {name: published.get(name) for name in names},
                    # The roster gap, for the caller that cannot read stderr
                    # prose — the note above is suppressed under --json, and a
                    # payload without this field made the gap invisible to
                    # automation in the very function whose comment records
                    # honouring --json on the failing branches but not the
                    # succeeding one as an already-fixed bug.
                    "unregistered_roles": list(ops.unregistered_roles(target)),
                }
            )
        )
        return
    # Whose workspace, when it is not the machine's: a project's own key (#141)
    # registers in the workspace it names.
    under = f" under {target.key_origin}" if target.key_source == "project" else ""
    typer.echo(f"✓ registered {len(names)} identities with target '{target.name}'{under}")
    for agent_name in names:
        publication = published.get(agent_name)
        suffix = f"publication_id {publication}" if publication else "registered"
        typer.echo(f"  {agent_name}: {suffix}")
    if not published:
        typer.echo("  (the workspace returned no publication ids; re-run after it syncs)")


@app.command()
def ship(
    limit: Annotated[int, typer.Option("--limit", help="Most records to drain in one pass.")] = 500,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero when this run could not ship at all — for timers.",
        ),
    ] = False,
) -> None:
    """Drain buffered insights to the gateway (prompts, notes, task events).

    This is the only place the CLI talks to the gateway. It is deliberately a
    separate command and a separate process: the capture seams buffer, this
    delivers, and a gateway that is down therefore costs a delay rather than a
    prompt. Exits non-zero only when records were dead-lettered — a deferral is
    the design working, not a failure.
    """
    report = ship_once(limit=limit)
    if get_state().json_output:
        # The whole report. `blocked` is in it because its own comment says it
        # is set rather than inferred "so no caller has to match on message
        # text" — and a timer wrapper reading stdout is exactly that caller.
        typer.echo(
            json.dumps(
                {
                    "sent": report.sent,
                    "deferred": report.deferred,
                    "dead": report.dead,
                    "runs": list(report.runs),
                    "reason": report.reason,
                    "blocked": report.blocked,
                }
            )
        )
    else:
        typer.echo(report.reason)
        if report.runs:
            typer.echo(f"runs: {', '.join(report.runs)}")
    if report.dead:
        raise typer.Exit(code=1)
    # Opt-in, because the quiet default is doctrine: no key or config means
    # nothing captured and nothing logged as an error, and a non-zero default
    # would mail every operator who deliberately does not ship. A TIMER wants
    # the opposite — cron discards stdout by convention, so exit 0 is the only
    # thing it reads, and a blocked run reporting healthy forever is how the
    # insight lane goes silently missing while the proxy lane looks perfect.
    # Fires on BLOCKED only: a deferral is retried by the next tick, and
    # shouting about a transient outage teaches the operator to ignore the mail.
    if strict and report.blocked:
        raise typer.Exit(code=1)


@app.command()
def env(
    role: Annotated[
        str,
        typer.Argument(help="Role identity for the traced session, e.g. 'coder'."),
    ],
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Key the Run to this session id."),
    ] = None,
    target_name: Annotated[str | None, _TARGET_OPTION] = None,
    project_ref: Annotated[str | None, _PROJECT_OPTION] = None,
    post_root: Annotated[
        bool,
        typer.Option(
            "--post-root",
            help=(
                "Post the Run's root first, so the agent the NEXT command in this shell "
                "starts owns its Run (one Run per session). Only for a line that starts "
                "the agent right after, which is what the printed `team spawn` command "
                "is; a bare eval with this flag leaves an empty Run on the dashboard."
            ),
        ),
    ] = False,
) -> None:
    """Print shell exports that trace the next agent run from this terminal.

    Use as ``eval "$(aisquare explainability env coder)"``. Unlike the
    launcher, this refuses loudly (exit 1) when the session would not be
    traced — a human asked for tracing explicitly, so silence would lie.

    ``AISQUARE_PIPELINE_ID`` is exported alongside the header pair so the
    command that follows can start the agent ON that id
    (``claude --session-id "$AISQUARE_PIPELINE_ID"``) and have its board row
    join the Run. Exported rather than printed as a comment because the
    output's only job is to be eval'd.

    THIS OUTPUT NOW CONTAINS A CREDENTIAL. ``ANTHROPIC_CUSTOM_HEADERS`` carries
    the workspace key when the active target resolves one, because that is what a
    hosted proxy authenticates on. The output is meant to be piped into ``eval``,
    not read aloud: it is not safe for scrollback, a screen-share or CI logs. It
    is a write-scoped ingest key — it sends spans and reads nothing — which is
    what makes printing it acceptable at all. A loopback proxy needs no key and
    the header is omitted entirely there.

    BY DEFAULT THIS COMMAND WRITES NOTHING TO THE GATEWAY. The delta is the
    proxy-keyed form — ``X-Pipeline-Id``, never ``traceparent``, and no
    ``AISQUARE_RUN_TRACE_ID`` — because the Run's root is not posted. Posting
    it is how a launch OWNS its Run, and it mints a dashboard Run on the spot:
    one parentless, already-ended span that the gateway files as a
    ``completed`` Run of 0 ms and zero tokens, named after the role. A command
    whose whole job is to print exports cannot know whether an agent will ever
    start on the id it printed, and without ``--session-id`` every invocation
    mints a fresh one — so a second terminal, a shell rc that evals this, a
    script reading ``--json``, or an operator inspecting the delta each left an
    empty Run behind, after up to three seconds of WAN I/O behind a print.
    The price is the documented fallback: the proxy keys the Run for a session
    started from this output and the client lane opens its own. That is the
    pre-ownership shape, and it is exactly what a print with no side effects
    can promise. ``aisquare launch`` and ``team spawn --exec`` still post the
    root, because they are about to start the agent that fills it.

    ``--post-root`` IS THE OPT-IN, for the one caller on a launch's footing:
    the command ``team spawn`` prints, ``eval "$(aisquare
    explainability env <role> --post-root)"; claude …``. There "will an agent
    ever start on this id" is not unknowable — the agent starts on the very
    next command in the same shell — so the eval posts the root exactly as a
    launch does, the pasted session owns its Run (``traceparent`` on the wire,
    ``AISQUARE_RUN_TRACE_ID`` exported) and the launch line goes to stderr,
    where an eval leaves it for the human. Same fail-open, same direction: a
    refused root falls back to ``X-Pipeline-Id`` with no run key exported, a
    dead proxy to untraced, and neither costs the paste. The flag is visible
    rather than hidden because the line that carries it is printed for a
    human to read, and a flag the CLI's own ``--help`` disowns is a trap; the
    help text says when it is wrong to add by hand.
    """
    settings = load_config().explainability
    # The project's own key when it has one (#141), resolved without a store
    # open that could fail: this output is evaled into a shell about to start
    # an agent, and a context.db it cannot read costs the project's key, never
    # the exports — the bar `status` and `launch` already hold.
    project_id = _key_project_id(project_ref)
    target = ops.resolve_target(settings, target_name, project_id=project_id)
    # Print-only by default: no Run root is posted, so the target's gateway is
    # not even handed over — the key still is, because a hosted proxy
    # authenticates on it. `--post-root` takes the path `launch` takes: gateway,
    # key, root, fail-open. See the docstring for what a post costs and why
    # only a line that starts the agent next may pay it.
    wiring = wire_session(
        # The same project as the key: its destination may name another target
        # than the machine's, and a proxy from one with a key from the other
        # hands that key to the wrong deployment (#142).
        ops.effective_settings(settings, target_name, project_id=project_id),
        role,
        session_id=session_id,
        base_env=dict(os.environ),
        api_key=target.api_key,
        gateway_url=target.gateway_url if post_root else None,
        post_root=post_root,
    )
    if not wiring.traced:
        fail(wiring.reason, error="untraced")
    if post_root:
        # A write happened, or was refused: say what `launch` says, on stderr.
        # Stdout is the exports and nothing else may land there — an eval would
        # execute it — and stderr is exactly where the substitution leaves a
        # line for the human, so the pasted command reports its Run the way
        # `--exec` does. The default stays silent: it changed nothing.
        typer.echo(f"explainability: {wiring.reason}", err=True)
    exports = dict(wiring.env)
    if wiring.pipeline_id:
        # Marks the ANTHROPIC_* beside it as OURS, so a second paste in the
        # same terminal clears our leftovers instead of silently inheriting
        # this session's pipeline id and merging two sessions into one Run.
        exports.update(trace_marker(wiring))
    if get_state().json_output:
        # The refusal above already answered in JSON, through the shared `fail`
        # helper — so before this, the flag was honoured on the branch that
        # FAILS and ignored on the branch that WORKS. A script piping this into
        # jq passed every test while the proxy was down and broke on the day it
        # came up.
        #
        # The variables are the payload, under one key, spelled exactly as they
        # are exported: a caller reads `.env.AISQUARE_PIPELINE_ID`, which is the
        # name §5 of the runbook already uses. Lifting the pipeline id to a
        # second top-level field would give the same value two names.
        typer.echo(json.dumps({"role": role, "env": exports}))
        return
    for key, value in exports.items():
        typer.echo(f"export {key}={shlex.quote(value)}")
