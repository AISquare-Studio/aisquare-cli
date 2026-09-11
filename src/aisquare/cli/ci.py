"""``aisquare ci`` — the Collective Intelligence test bed's own commands.

Hidden while the experiment runs: the hooks and the recall tool are wired by
``agents connect`` and driven by the server's descriptor, so the only thing a
developer ever has to do by hand is say which of their workspaces a project
asks in — and only when they belong to several.
"""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare.cli.common import expected_config_write_errors, fail
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.core.workspace import active_project
from aisquare.services import ci_client, ci_me

app = typer.Typer(
    help="Collective Intelligence test bed.",
    hidden=True,
    no_args_is_help=True,
)


def _project_id() -> str:
    """The project this checkout is, resolved exactly as the hooks resolve it.

    ``active_project``: the pinned project only while it is still registered,
    else the one containing the current directory. The first draft returned the
    pin unconditionally, so a stale pin left by ``project switch`` filed the
    binding under an id no hook would ever read while ``doctor`` - using the
    same shortcut - confirmed it. One answer to "which project is this".
    """
    with store_session() as store:
        return active_project(store).id


@app.command("bind-workspace")
def bind_workspace(
    workspace: Annotated[
        str | None,
        typer.Argument(
            help="A ws_… id from the workspaces GET /v1/me lists for you. Omit to bind "
            "the only one you belong to, or to see the list."
        ),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option(
            "--clear", help="Forget this project's binding; a single workspace needs none."
        ),
    ] = False,
) -> None:
    """Bind THIS project to one of the signed-in user's workspaces.

    The run the hooks ask against is the one the controller published in the
    bound workspace, so a developer in several workspaces has to pick; one in a
    single workspace needs nothing. The binding is stored per project
    (``[experiment].bindings`` in config.toml, keyed by the project id), so
    binding one checkout never re-tenants another on the same machine. Asks
    GET /v1/me for the list, uncached, and refuses an id that is not on it.
    """
    project_id = _project_id()
    if clear:
        config = load_config()
        was = config.experiment.bindings.pop(project_id, None)
        with expected_config_write_errors():
            save_config(config)
        ci_client.reset_cache()
        typer.echo(
            f"cleared this project's binding ({was})" if was else "this project had no binding"
        )
        return
    if not ci_client.enabled():
        fail("the CI test bed is off (AISQUARE_CI=1 enables it)", error="ci_disabled")
    base = ci_client.endpoint()
    if not base:
        fail(
            "no usable server URL — export AISQUARE_CI_URL=https://…",
            error="not_configured",
        )
    key = ci_client.api_key()
    if not key:
        problem, fix = ci_client.bearer_problem()
        fail(
            f"{problem} — {fix}"
            if problem
            else "no bearer — sign in with `aisquare login`, or export AISQUARE_CI_KEY",
            error="not_authenticated",
        )
    answer = ci_me.fetch(base=base, key=key, cache=False)
    if answer.me is None:
        # The actionable token goes in the MESSAGE: `fail` prints only that for
        # a human, and `hint` reaches the --json payload alone.
        why = answer.detail
        if answer.status == 401:
            why += " — sign in again: aisquare login"
        fail(f"GET /v1/me: {why}", error="me_unavailable")
    me = answer.me
    if not me.workspaces:
        fail("signed in, but a member of no workspace", error="no_workspace")
    listing = "\n".join(
        f"  {m.workspace_id}  {m.role}  run {m.active_run_id or '(none published)'}"
        for m in me.workspaces
    )
    if workspace is None:
        if len(me.workspaces) != 1:
            fail(
                f"a member of {len(me.workspaces)} workspaces — pass the one this project "
                f"asks in:\n{listing}",
                error="ambiguous_workspace",
            )
        workspace = me.workspaces[0].workspace_id
    member = me.membership(workspace)
    if member is None:
        fail(
            f"{workspace} is not one of your workspaces:\n{listing}",
            error="not_a_member",
            ref=workspace,
        )
    config = load_config()
    config.experiment.bindings[project_id] = member.workspace_id
    with expected_config_write_errors():
        save_config(config)
    ci_client.reset_cache()
    run = member.active_run_id or "none published yet"
    typer.echo(
        f"bound this project ({project_id}) to {member.workspace_id} ({member.role}), run {run}"
    )
