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
from aisquare.services import ci_client, ci_me
from aisquare.services import settings as settings_service

app = typer.Typer(
    help="Collective Intelligence test bed.",
    hidden=True,
    no_args_is_help=True,
)

WORKSPACE_KEY = "experiment.workspace"
"""The one config key this module writes. A selector, never authority: the
server refuses a run in a workspace the user is not a member of whatever this
says (ADR 0008 decision 4), so a wrong value here yields a refusal and never a
widening."""


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
        bool, typer.Option("--clear", help="Forget the binding; a single workspace needs none.")
    ] = False,
) -> None:
    """Bind this project to one of the signed-in user's workspaces.

    The run the hooks ask against is the one the controller published in the
    bound workspace, so a developer in several workspaces has to pick; one in a
    single workspace needs nothing. Asks GET /v1/me for the list, uncached, and
    refuses an id that is not on it.
    """
    if clear:
        with expected_config_write_errors():
            settings_service.set_value(WORKSPACE_KEY, "")
        ci_client.reset_cache()
        typer.echo(f"cleared {WORKSPACE_KEY}")
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
        fail(
            "no bearer — sign in with `aisquare login`, or export AISQUARE_CI_KEY",
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
    with expected_config_write_errors():
        stored = settings_service.set_value(WORKSPACE_KEY, member.workspace_id)
    ci_client.reset_cache()
    run = member.active_run_id or "none published yet"
    typer.echo(f"bound this project to {stored} ({member.role}), run {run}")
