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
"""

from __future__ import annotations

from typing import Any

from typer.core import TyperGroup, TyperOption

from aisquare.core.state import get_state

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
            help="Emit machine-readable JSON on stdout.",
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


class GlobalFlagsGroup(TyperGroup):
    """Root group of the CLI: the click tree is complete when it constructs,
    so it is the one place a single walk reaches every node on every entry
    path."""

    def __init__(self, **attrs: Any) -> None:
        super().__init__(**attrs)
        _inject_tree(self)
