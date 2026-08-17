"""The five global output flags, accepted anywhere on the command line.

The root callback (``aisquare.cli.app.main_callback``) defines the canonical
``--json``, ``--verbose/-v``, ``--quiet/-q``, ``--no-color`` and ``--profile``
and *replaces* the runtime state from them. This module makes their placement
irrelevant (issue #24): the root group is a ``TyperGroup`` subclass that walks
the finished click tree once and injects the same five flags into every group
and leaf command that does not already define them. Because the injection
happens while the tree is built, every entry path behaves identically —
``app()`` via ``main``, ``CliRunner.invoke(app)`` in tests and
``typer.main.get_command(app)``.

Injected flags never reach command functions (``expose_value=False``); their
callbacks *merge* into the state the root callback installed, exactly like the
root callback they only touch :mod:`aisquare.core.state`. Booleans OR across
positions (duplicates are idempotent); ``--profile``'s last occurrence wins
(the leaf parses after the root, so leaf overrides root). ``--version`` stays
root-only deliberately.

The root group is also where usage errors from the whole dispatch tree
surface (issue #21): an unknown subcommand gains a did-you-mean over the
failing group's real verbs, and when ``--json`` was already parsed (the same
``get_state()`` every error path reads — never argv scanning) the usage
error is emitted as ONE JSON object on stdout with exit code 2. A ``--json``
placed after the typo is never parsed by click, so it deliberately falls
back to the human path — lead with ``--json`` for a guaranteed
machine-readable error.
"""

from __future__ import annotations

import json
import re
from difflib import get_close_matches
from typing import Any

import typer
from typer.core import TyperGroup, TyperOption

from aisquare.cli.common import fail
from aisquare.core.state import get_state
from aisquare.core.store import (
    StoreUnopenable,
    damaged_store_message,
    damaged_store_recovery,
)

INJECTED_MARK = "aisquare_injected_global_flag"
"""Attribute stamped on injected params so tests can tell them from locals."""

SHARED_FLAG_DECLARATIONS: tuple[str, ...] = (
    "--json",
    "--verbose",
    "-v",
    "--quiet",
    "-q",
    "--no-color",
    "--profile",
)
"""Every declaration the five shared flags claim on each command."""

VERB_SYNONYMS: dict[str, tuple[str, ...]] = {
    "del": ("drop", "remove", "delete"),
    "get": ("show", "list"),
    "ls": ("list",),
    "rm": ("drop", "remove", "delete"),
}
"""Classic verb synonyms edit-similarity cannot reach (zero shared letters):
``task get`` must still point at ``show``. Only verbs the failing group
really has are ever suggested."""

_NO_SUCH_COMMAND = re.compile(r"No such command '([^']+)'")

# click is vendored (``typer._click``); like tests/cli_tree.py we stay on the
# public typer surface and duck-type instead — UsageError is the direct base
# of the public ``typer.BadParameter``.
_USAGE_ERROR: type[Exception] = typer.BadParameter.__bases__[0]

_JSON_HELP = (
    "Emit machine-readable JSON on stdout. Put --json before the subcommand "
    "to also get usage errors (unknown command/option) as JSON."
)


def _merge_flag(field: str) -> Any:
    """A click-style callback that ORs a boolean flag into the runtime state."""

    def merge(ctx: Any, param: Any, value: Any) -> Any:
        if value:
            setattr(get_state(), field, True)
        return value

    return merge


def _merge_profile(ctx: Any, param: Any, value: Any) -> Any:
    """Adopt ``--profile`` wherever it appears; the last occurrence parses last."""
    if value is not None:
        get_state().profile = value
    return value


def _shared_options() -> list[TyperOption]:
    """Fresh per-command instances of the five shared flags.

    Fresh instances (never shared across commands) so no click state leaks
    between nodes; help texts mirror the root callback's so ``-h`` reads the
    same everywhere.
    """
    options = [
        TyperOption(
            param_decls=["--verbose", "-v"],
            is_flag=True,
            default=False,
            expose_value=False,
            callback=_merge_flag("verbose"),
            show_default=False,
            help="Enable verbose output.",
        ),
        TyperOption(
            param_decls=["--quiet", "-q"],
            is_flag=True,
            default=False,
            expose_value=False,
            callback=_merge_flag("quiet"),
            show_default=False,
            help="Suppress non-essential output.",
        ),
        TyperOption(
            param_decls=["--json"],
            is_flag=True,
            default=False,
            expose_value=False,
            callback=_merge_flag("json_output"),
            show_default=False,
            help=_JSON_HELP,
        ),
        TyperOption(
            param_decls=["--profile"],
            metavar="NAME",
            default=None,
            expose_value=False,
            callback=_merge_profile,
            show_default=False,
            help="Configuration profile to use.",
        ),
        TyperOption(
            param_decls=["--no-color"],
            is_flag=True,
            default=False,
            expose_value=False,
            callback=_merge_flag("no_color"),
            show_default=False,
            help="Disable coloured output.",
        ),
    ]
    for option in options:
        setattr(option, INJECTED_MARK, True)
    return options


def _declared(command: Any) -> set[str]:
    """Every option declaration the command already claims."""
    declared: set[str] = set()
    for param in getattr(command, "params", []):
        declared |= set(getattr(param, "opts", ()))
        declared |= set(getattr(param, "secondary_opts", ()))
    return declared


def _inject(command: Any) -> None:
    if getattr(command, "context_settings", {}).get("ignore_unknown_options"):
        # An arg-forwarding command (`launch`): everything after it belongs to
        # the program it execs. Injecting parseable flags would make click
        # consume --verbose/--json/-q (and --profile plus its value) OUT of
        # the forwarded argv. Put the flags before the subcommand instead —
        # the root callback's canonical five still apply there.
        return
    declared = _declared(command)
    for option in _shared_options():
        # A node's own definition wins — the root callback's five stay the
        # canonical ones there, and a (test-guarded) local collision is never
        # silently shadowed.
        if declared & set(option.opts):
            continue
        command.params.append(option)


def _inject_tree(command: Any) -> None:
    _inject(command)
    for sub in getattr(command, "commands", {}).values():
        _inject_tree(sub)


def _suggestions(given: str, verbs: list[str]) -> list[str]:
    """Nearest real verbs for a typo: synonym hits first, then edit-similar."""
    near = [verb for verb in VERB_SYNONYMS.get(given, ()) if verb in verbs]
    for match in get_close_matches(given, verbs, n=3):
        if match not in near:
            near.append(match)
    return near


def _handle_usage_error(error: Any) -> None:
    """The #21 contract for a usage error bubbling out of dispatch.

    Either raises ``typer.Exit(2)`` after printing ONE JSON object to stdout
    (only when the already-parsed runtime state says ``--json`` — the same
    source :func:`aisquare.cli.common.fail` reads), or returns so the caller
    re-raises for typer's normal stderr rendering — appending a did-you-mean
    when typer's built-in matcher found nothing (synonym-only typos).
    Anything that is neither an unknown command nor an unknown option passes
    through untouched.
    """
    message = str(getattr(error, "message", error))
    unknown = _NO_SUCH_COMMAND.search(message)
    if unknown is None:
        if get_state().json_output and type(error).__name__ == "NoSuchOption":
            typer.echo(json.dumps({"error": "usage", "message": message}))
            raise typer.Exit(code=2)
        return
    given = unknown.group(1)
    context = getattr(error, "ctx", None)
    group = getattr(context, "command", None)
    verbs = [
        name
        for name, command in getattr(group, "commands", {}).items()
        if not getattr(command, "hidden", False)
    ]
    suggested = _suggestions(given, verbs)
    if get_state().json_output:
        path = str(getattr(context, "command_path", "") or "")
        typer.echo(
            json.dumps(
                {
                    "error": "unknown_command",
                    "group": " ".join(path.split()[1:]),
                    "given": given,
                    "did_you_mean": suggested,
                }
            )
        )
        raise typer.Exit(code=2)
    if suggested and "Did you mean" not in message:
        quoted = ", ".join(f"'{verb}'" for verb in suggested)
        error.message = f"{message.rstrip('.')}. Did you mean {quoted}?"


class GlobalFlagsGroup(TyperGroup):
    """Root group of the CLI: the click tree is complete when it constructs,
    so it is the one place a single walk reaches every node on every entry
    path — and the one place every usage error in the tree passes through."""

    def __init__(self, **attrs: Any) -> None:
        super().__init__(**attrs)
        _inject_tree(self)

    def invoke(self, ctx: Any) -> Any:
        """Dispatch, translating usage errors per the #21 contract.

        ``StoreUnopenable`` is translated here for the same reason the flags are
        injected here: every command in the tree passes through this one place,
        on every entry path. Eleven commands were measured printing 59-75 lines
        of traceback against a damaged store — they all die in ``open_store``,
        so wrapping each call site would be eleven chances to miss one and a
        twelfth defect the day someone adds a command.

        Narrow on purpose, and not a general tidier: ONE exception type whose
        entire meaning is "the store would not open", raised from one function.
        Every other error — including a ``DatabaseError`` from a later query,
        which may be a bug in our SQL against a healthy store — keeps its
        traceback, because burying one of those costs whoever debugs it far
        more than a buried message costs an operator.
        """
        try:
            return super().invoke(ctx)
        except StoreUnopenable as damaged:
            fail(
                damaged_store_message(damaged),
                error="store_unopenable",
                hint=damaged_store_recovery(),
                detail=str(damaged),
            )
        except _USAGE_ERROR as error:
            _handle_usage_error(error)
            raise
