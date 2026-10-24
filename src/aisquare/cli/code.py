"""``aisquare code``: ask the project's symbol index before grepping (#285).

``find`` a definition, list the ``refs`` to a name, and check for ``similar`` helpers
before writing a new one. ``index`` builds or refreshes the index, ``--status`` says what
it holds. Every command takes the global ``--json``.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from aisquare.cli.common import fail
from aisquare.core import code_index
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.services import code as code_service

app = typer.Typer(
    help="The project's symbol index: find a definition, its references, similar helpers.",
    no_args_is_help=True,
)

KINDS = ("function", "method", "class", "interface", "module")


def _print(line: str) -> None:
    stdout_console().print(line, markup=False, highlight=False, soft_wrap=True)


def _symbol_line(symbol: code_index.Symbol) -> str:
    return f"{symbol.path}:{symbol.line}  {symbol.kind:<8} {symbol.signature}"


def _first_build_note() -> None:
    """Say so on stderr when a query builds the index first (a first one takes a while)."""
    if get_state().json_output or not code_service.settings().enabled:
        return
    if not code_index.ready(code_service.project_here().id):
        stderr_console().print("no code index yet: building it first", markup=False)


def _or_fail(exc: code_service.CodeIndexError) -> None:
    fail(str(exc), error="no_index" if "no code index" in str(exc) else "index_busy")


@app.command("find")
def find_command(
    name: Annotated[str, typer.Argument(help="A name, or a qualified one (Class.method).")],
    kind: Annotated[
        str | None,
        typer.Option("--kind", help=f"Only this kind: {', '.join(KINDS)}."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="At most this many.")] = 20,
) -> None:
    """Where NAME is defined: exact, case-insensitive, prefix, then by name part."""
    if kind is not None and kind not in KINDS:
        raise typer.BadParameter(f"one of {', '.join(KINDS)}", param_hint="--kind")
    _first_build_note()
    try:
        found = code_service.find_symbols(name, kind=kind, limit=limit)
    except code_service.CodeIndexError as exc:
        _or_fail(exc)
        return
    if get_state().json_output:
        typer.echo(json.dumps({"query": name, "symbols": [s.as_json() for s in found]}))
        return
    if not found:
        _print(f"no definition of {name!r} in the index")
        return
    for symbol in found:
        _print(_symbol_line(symbol))


@app.command("refs")
def refs_command(
    name: Annotated[str, typer.Argument(help="The name whose uses to list.")],
    defs: Annotated[bool, typer.Option("--defs", help="List its definitions first.")] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, help="At most this many.")] = 50,
) -> None:
    """Every use of NAME: calls, attribute reads, type annotations."""
    _first_build_note()
    try:
        found = code_service.find_refs(name, limit=limit)
        definitions = code_service.find_symbols(name, limit=limit) if defs else []
    except code_service.CodeIndexError as exc:
        _or_fail(exc)
        return
    bare = name.rsplit(".", 1)[-1]
    definitions = [s for s in definitions if s.name == bare]
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "query": name,
                    "definitions": [s.as_json() for s in definitions],
                    "refs": [r.as_json() for r in found],
                }
            )
        )
        return
    for symbol in definitions:
        _print(f"{_symbol_line(symbol)}  (definition)")
    if not found:
        _print(f"no use of {bare!r} in the index")
        return
    for ref in found:
        _print(f"{ref.path}:{ref.line}")


@app.command("similar")
def similar_command(
    target: Annotated[str, typer.Argument(help="A name, or path:line of a definition.")],
    limit: Annotated[int, typer.Option("--limit", min=1, help="At most this many.")] = 10,
) -> None:
    """Definitions whose names share parts with TARGET: run it before writing a helper."""
    _first_build_note()
    try:
        anchor, found = code_service.find_similar(target, limit=limit)
    except code_service.CodeIndexError as exc:
        _or_fail(exc)
        return
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "query": target,
                    "anchor": anchor.as_json() if anchor is not None else None,
                    "similar": [s.as_json() for s in found],
                }
            )
        )
        return
    if anchor is not None:
        _print(f"like {_symbol_line(anchor)}")
    if not found:
        _print(f"nothing like {target!r} in the index")
        return
    for item in found:
        shared = ", ".join(item.shared)
        _print(f"{_symbol_line(item.symbol)}  ({item.score:.2f}: {shared})")


@app.command("index")
def index_command(
    full: Annotated[bool, typer.Option("--full", help="Rebuild from nothing.")] = False,
    show_status: Annotated[
        bool, typer.Option("--status", help="Say what the index holds; build nothing.")
    ] = False,
) -> None:
    """Build or refresh the project's index (only changed files are read again)."""
    as_json = get_state().json_output
    if show_status:
        found = code_service.index_status()
        if as_json:
            typer.echo(json.dumps(found.as_json()))
            return
        if not found.exists or found.built_at is None:  # none, or a first build uncommitted
            if code_index.building(code_service.project_here().id):
                _print("the code index is building in the background — ask again in a moment")
            else:
                _print(f"no code index yet — build it: {code_service.INDEX_COMMAND}")
            return
        parsers = ", ".join(f"{p} {n}" for p, n in sorted(found.parsers.items()))
        _print(f"{found.files} files, {found.symbols} symbols, {found.refs} refs ({parsers})")
        _print(
            f"built {found.built_at} at {found.head or 'no commit'}"
            + (f" — stale: the checkout is at {found.current_head}" if found.stale else "")
        )
        if found.not_indexed:
            missing = ", ".join(f"{lang} {n}" for lang, n in sorted(found.not_indexed.items()))
            _print(f"not indexed: {missing}{_extra_hint(found.not_indexed)}")
        return
    try:
        report = code_service.build_index(full=full)
    except code_service.CodeIndexError as exc:
        _or_fail(exc)
        return
    if as_json:
        typer.echo(json.dumps(report.as_json()))
        return
    _print(
        f"✓ indexed {report.files} files ({report.parsed} read, {report.unchanged} unchanged, "
        f"{report.removed} removed) in {report.seconds:.1f}s: {report.symbols} symbols, "
        f"{report.refs} refs"
    )
    if report.not_indexed:
        missing = ", ".join(f"{lang} {n}" for lang, n in sorted(report.not_indexed.items()))
        _print(f"  not indexed: {missing}{_extra_hint(report.not_indexed)}")
    for path, why in sorted(report.failed.items()):
        _print(f"  could not parse {path}: {why}")


def _extra_hint(missing: dict[str, int]) -> str:
    if {"typescript", "tsx", "javascript"} & set(missing):
        return f" — TypeScript and JavaScript need the extra: {code_service.EXTRA_HINT}"
    return ""
