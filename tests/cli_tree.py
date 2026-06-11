"""Helpers that walk the generated command tree at collection time.

typer ≥ 0.26 vendors its click fork as ``typer._click``, so these helpers
stay on typer's public surface (``typer.main.get_command``, ``TyperGroup``)
and duck-type the command/parameter attributes instead of importing click.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import Enum
from typing import Any

import typer.main
from typer.core import TyperGroup

from aisquare.cli.app import app


def root_command() -> TyperGroup:
    """Build the command tree for the whole CLI."""
    command = typer.main.get_command(app)
    assert isinstance(command, TyperGroup)
    return command


def _iter_nodes(command: Any, path: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], Any]]:
    yield path, command
    if isinstance(command, TyperGroup):
        for name, sub in command.commands.items():
            yield from _iter_nodes(sub, (*path, name))


def all_command_paths() -> list[tuple[str, ...]]:
    """Every node in the tree: the root, all groups and all leaf commands."""
    return [path for path, _ in _iter_nodes(root_command(), ())]


def _placeholder(parameter: Any) -> str:
    choices = getattr(parameter.type, "choices", None)
    if choices:
        first = choices[0]
        return first.value if isinstance(first, Enum) else str(first)
    return "sample"


def leaf_invocations() -> list[list[str]]:
    """argv for every leaf command, with required arguments filled in."""
    invocations: list[list[str]] = []
    for path, command in _iter_nodes(root_command(), ()):
        if isinstance(command, TyperGroup):
            continue
        argv = [*path]
        for parameter in command.params:
            if parameter.param_type_name == "argument" and parameter.required:
                argv.append(_placeholder(parameter))
        invocations.append(argv)
    return invocations
