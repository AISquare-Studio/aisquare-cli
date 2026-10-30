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
``get_state()`` every error path reads) the usage error is emitted as ONE
JSON object on stdout with exit code 2. A ``--json`` placed after the typo
is never parsed by click, so it deliberately falls back to the human path —
lead with ``--json`` for a guaranteed machine-readable error.

Two root-level failures happen before ``get_state()`` has anything to read,
though (issue #5): click's ``resolve_command`` rejects an unknown top-level
subcommand (``aisquare --json nosuchcmd``), and an unrecognised root option
(``aisquare --json --badopt``) dies inside ``parse_args`` — both before
``main_callback`` (``cli/app.py``) ever runs ``set_state``. For those two
spots only, :func:`_json_led` reads the raw argv directly (everywhere else
stays on ``get_state()``) so a leading ``--json`` still wins.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from difflib import get_close_matches
from typing import Any

import typer
from typer.core import TyperGroup, TyperOption

from aisquare.cli.common import USAGE_ERROR, expected_home_creation_errors, fail
from aisquare.core.state import get_state
from aisquare.core.store import (
    StoreUnopenable,
    damaged_data_message,
    damaged_store_message,
    damaged_store_recovery,
    is_corrupt_error,
)

DISPATCHED_WORDS = "aisquare.dispatched_words"
"""``ctx.meta`` key under which the root group leaves the words after its own options."""

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


def _value_taking_root_options(command: Any) -> set[str]:
    """Declarations among the root's own options that consume a following
    token (``--profile NAME``), as opposed to a bare flag like ``--json``.

    Derived from the live params instead of a hardcoded name so a future
    value-taking root option is picked up automatically.
    """
    opts: set[str] = set()
    for param in getattr(command, "params", []):
        if getattr(param, "is_flag", False) or getattr(param, "count", False):
            continue
        opts |= set(getattr(param, "opts", ()))
    return opts


def _json_led(args: Any, value_taking: frozenset[str] = frozenset()) -> bool:
    """True when ``--json`` appears among the root's own options before the
    first non-flag token (the attempted subcommand name).

    Only called where ``get_state()`` cannot yet answer — a root option or
    subcommand failing before ``main_callback`` runs (see module docstring,
    issue #5) — so this, not ``get_state()``, is what decides those two
    spots. A trailing ``--json`` stops the scan same as everywhere else:
    click never parses past the unknown token either, so the two agree.

    ``value_taking`` lists the root's own value-taking option declarations
    (``--profile``): the token right after one of those is that option's
    value, not the attempted subcommand, even though it does not itself
    start with ``-`` (``--profile default --json --badopt``).
    """
    skip_next = False
    for token in args:
        token_str = str(token)
        if skip_next:
            skip_next = False
            continue
        if token_str == "--json":
            return True
        if not token_str.startswith("-"):
            break
        if token_str in value_taking:
            skip_next = True
    return False


def _handle_usage_error(error: Any) -> None:
    """The #21 contract for a usage error bubbling out of dispatch.

    Either raises ``typer.Exit(2)`` after printing ONE JSON object to stdout
    (only when the already-parsed runtime state says ``--json`` — the same
    source :func:`aisquare.cli.common.fail` reads), or returns so the caller
    re-raises for typer's normal stderr rendering — appending a did-you-mean
    when typer's built-in matcher found nothing (synonym-only typos).
    Every other usage error — an unknown option, a bad or missing parameter,
    two options that conflict — is the same object under ``--json``: a caller
    that asked for JSON gets JSON or nothing. This rendered ``NoSuchOption``
    alone and let ``BadParameter`` fall through to typer's stderr prose, so
    ``--json fleet shutdown --project x --all`` printed nothing on stdout
    (round 7 of #203). Without ``--json`` it returns so the caller re-raises
    for typer's normal rendering.
    """
    message = str(getattr(error, "message", error))
    unknown = _NO_SUCH_COMMAND.search(message)
    if unknown is None:
        if get_state().json_output:
            # `message` is EMPTY on click's `MissingParameter` (its text lives in
            # `format_message()`) and lacks the `Invalid value for --all:` prefix
            # on `BadParameter`, so `--json fleet stop` printed
            # `{"error": "usage", "message": ""}` — the information lost for
            # both audiences at once (round 8 of #203). `format_message()` is
            # what typer would have rendered on stderr, so JSON says the same.
            rendered = message
            formatter = getattr(error, "format_message", None)
            if callable(formatter):
                with contextlib.suppress(Exception):
                    rendered = str(formatter())
            typer.echo(json.dumps({"error": "usage", "message": rendered or message or str(error)}))
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

    def resolve_command(self, ctx: Any, args: list[str]) -> Any:
        """Resolve as click does, having noted the words that follow the root's options.

        Click empties ``ctx.args`` before the root callback runs, so the callback cannot see
        which command it is dispatching: the feature counters (``core.feature_use``) read the
        words from here, and walk them against the command tree by NAME — see
        ``aisquare.cli.app.command_path``, which uses nothing else of them."""
        ctx.meta[DISPATCHED_WORDS] = list(args)
        return super().resolve_command(ctx, args)

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        """Catch a bad root option (``--json --badopt``) before it reaches
        :meth:`invoke`.

        This is the one usage error the whole dispatch tree raises before
        ``main_callback`` ever runs: an unrecognised option dies inside
        click's own option parser, so ``ctx.params`` is still empty and
        ``get_state()`` has nothing from this invocation yet (issue #5).
        :func:`_json_led` reads ``args`` — the raw, as-yet-unparsed argv —
        instead. Snapshotted first: click's parser consumes ``args`` in
        place, so by the time the ``except`` below runs it has already been
        emptied out from under us.
        """
        original_args = list(args)
        try:
            return super().parse_args(ctx, args)
        except USAGE_ERROR as error:
            if _json_led(original_args, frozenset(_value_taking_root_options(self))):
                get_state().json_output = True
            _handle_usage_error(error)
            raise

    def invoke(self, ctx: Any) -> Any:
        """Dispatch, translating usage errors per the #21 contract.

                ``StoreUnopenable`` is translated here for the same reason the flags are
                injected here: every command in the tree passes through this one place,
                on every entry path. Eleven commands were measured printing 59-75 lines
                of traceback against a damaged store — they all die in ``open_store``,
                so wrapping each call site would be eleven chances to miss one and a
                twelfth defect the day someone adds a command.

        ``expected_home_creation_errors`` sits here for the same reason: ``open_store``
                calls ``ensure_home()`` unconditionally, so a home that CANNOT exist (an
                ``AISQUARE_HOME`` pointing at a file) kills every command that touches
                the store — measured at ~90 lines of traceback with empty stdout, under
                ``--json`` included. Wrapping the one dispatch point translates all of
                them; the alternative was the same three lines in forty commands.

                Narrow on purpose, and not a general tidier: ONE exception type whose
                entire meaning is "the store would not open", raised from one function.
                Every other error — including a ``DatabaseError`` from a later query,
                which may be a bug in our SQL against a healthy store — keeps its
                traceback, because burying one of those costs whoever debugs it far
                more than a buried message costs an operator.
        """
        try:
            with expected_home_creation_errors():
                return super().invoke(ctx)
        except StoreUnopenable as damaged:
            fail(
                damaged_store_message(damaged),
                error="store_unopenable",
                hint=damaged_store_recovery(),
                detail=str(damaged),
            )
        except sqlite3.DatabaseError as damaged:
            # Damage a QUERY found: the file opened, so StoreUnopenable never
            # fired. Keyed on the error rather than on where it came from,
            # because by here the frame is long gone — and narrowed to
            # corruption, since OperationalError is also a DatabaseError and
            # "database is locked" must keep meaning "try again", not "your
            # board is damaged".
            if not is_corrupt_error(damaged):
                raise
            fail(
                damaged_data_message(damaged),
                error="store_damaged",
                hint=damaged_store_recovery(),
                detail=str(damaged),
            )
        except USAGE_ERROR as error:
            # An unknown top-level subcommand (``aisquare --json nosuchcmd``):
            # click's ``resolve_command`` rejects it before ``main_callback``
            # runs, so ``get_state().json_output`` is still the pre-call
            # default even though ``--json`` already parsed clean into
            # ``ctx.params`` (issue #5).
            if ctx.params.get("json_output"):
                get_state().json_output = True
            _handle_usage_error(error)
            raise
