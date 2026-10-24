"""What an agent's MCP server entry runs: aisquare, and whether through a runner that fetches it.

The shared reading of the ``{"command": …, "args": […]}`` entry Claude Code's
``.claude.json``/``.mcp.json`` and Codex's ``config.toml`` record alike, for each adapter's
:meth:`~aisquare.core.agent_adapters.types.AgentAdapter.lasting_installs`: an entry that
runs aisquare is one ``aisquare uninstall`` names, and one that fetches the package
outlives it and makes the aisquare home again (``services.lifecycle``). Pure; no I/O.
"""

from __future__ import annotations

import re
from pathlib import PureWindowsPath

from aisquare.core.version import DISTRIBUTION


def _canonical_name(name: str) -> str:
    """A distribution name as PEP 503 normalises it, to compare with :data:`DISTRIBUTION`."""
    return re.sub(r"[-_.]+", "-", name).lower()


#: The executable names that ARE aisquare, as either platform writes them.
_PROGRAM_NAMES = frozenset({"aisquare", "asq", "aisquare.exe", "asq.exe"})


def runs_aisquare(spec: object) -> bool:
    """Whether an ``mcpServers`` entry starts aisquare — by program, never by mention.

    ``command`` counts when its file name IS the program (read as a Windows path
    whatever this machine is, since the file may come from either; ``/`` splits
    there too). An argument counts only as a BARE program name — ``uvx --from
    aisquare-cli aisquare serve`` — or as the ``-m aisquare`` pair. A path or a
    file that merely ends in ``aisquare`` (``~/Code/AISquare``, ``aisquare.json``)
    is another server's argument, and naming that server for removal would be
    wrong (review of #253).
    """
    if not isinstance(spec, dict):
        return False
    command = spec.get("command")
    if isinstance(command, str) and PureWindowsPath(command).name.lower() in _PROGRAM_NAMES:
        return True
    args = spec.get("args")
    tokens = [str(arg) for arg in args] if isinstance(args, list) else []
    for index, token in enumerate(tokens):
        if token.lower() in _PROGRAM_NAMES:
            return True
        if token == "-m" and tokens[index + 1 : index + 2] == ["aisquare"]:
            return True
    return False


#: A ``--with`` item that NAMES its requirement (PEP 508): the name, then extras, a version,
#: a marker, an ``@`` direct reference (``aisquare_cli @ https://…``) or nothing.
_NAMED_REQUIREMENT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\]\s*)?(?:[;@<>=!~]|$)")
#: The archives a ``--with`` item may be a file of (a wheel, an sdist), whose names a
#: requirement's name could otherwise be read off.
_ARCHIVES = (".whl", ".tar.gz", ".tgz", ".tar.bz2", ".zip")


def _requirement_items(value: str) -> list[str]:
    """A ``uv run --with`` value's comma-separated requirements: split outside extras'
    brackets, as uv splits them, with a part that starts with a version operator
    (``rich>=1,<2``) kept with the one before it."""
    parts: list[str] = []
    depth, current = 0, ""
    for char in value:
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        depth = max(0, depth + {"[": 1, "]": -1}.get(char, 0))
        current += char
    parts.append(current)
    items: list[str] = []
    for part in (part.strip() for part in parts):
        if items and part[:1] in ("<", ">", "=", "!", "~"):
            items[-1] += f",{part}"
        elif part:
            items.append(part)
    return items


def _may_install_aisquare(value: str) -> bool:
    """Whether a ``uv run --with`` value may install aisquare: an item that names it, or one
    that names nothing — a URL (``git+https://…/aisquare-cli``), a path (a checkout) or an
    archive (a wheel) — which may hold it. Read as fetching: the entry runs aisquare, so an
    unnamed item is likely where it comes from, and a purge read as lasting is undone by
    the next session (round 25 of #257). An item named for another package is not."""
    for item in _requirement_items(value):
        named = _NAMED_REQUIREMENT.match(item)
        if named is None or item.lower().endswith(_ARCHIVES):
            return True
        if _canonical_name(named.group(1)) == DISTRIBUTION:
            return True
    return False


def _with_values(tokens: list[str]) -> list[tuple[str, str]]:
    """``uv run``'s ``--with``/``-w``, ``--with-editable`` and ``--with-requirements`` among
    ``tokens`` as ``(flag, value)``, in each spelling clap takes: ``--with X``, ``--with=X``,
    ``-w X``, ``-w=X``, ``-wX``."""
    values: list[tuple[str, str]] = []
    for index, token in enumerate(tokens):
        flag, glued, value = token.partition("=")
        if flag in ("--with", "--with-editable", "--with-requirements"):
            values.extend(
                (flag, after) for after in ([value] if glued else tokens[index + 1 : index + 2])
            )
        elif token == "-w":
            values.extend(("--with", after) for after in tokens[index + 1 : index + 2])
        elif token.startswith("-w") and not token.startswith("--"):
            values.append(("--with", token[2:].removeprefix("=")))
    return values


def fetches_aisquare(spec: object) -> bool:
    """Whether an ``mcpServers`` entry that runs aisquare (:func:`runs_aisquare`) runs it
    through a runner that fetches the package when it is not installed, so the server
    outlives an uninstall: ``uvx``, ``uv tool run``, ``uv run --with aisquare-cli`` and
    ``pipx run``.

    The subcommand is looked for among the arguments BEFORE the program (the bare
    ``aisquare`` or ``-m aisquare`` :func:`runs_aisquare` reads), not at a fixed
    position: uv and pipx take their own options first, so ``uv --offline tool run``
    and ``pipx --verbose run`` read as other commands, and the purge they undo went
    ahead (review of #257). ``uv run`` fetches only what its ``--with`` flags install
    (:func:`_may_install_aisquare`; an editable path or a requirements file may hold it,
    so either counts); without them the program comes from the project or PATH, as an
    installed one does.
    """
    if not isinstance(spec, dict) or not isinstance(spec.get("command"), str):
        return False
    program = PureWindowsPath(spec["command"]).name.lower().removesuffix(".exe")
    args = spec.get("args")
    tokens = [str(arg) for arg in args] if isinstance(args, list) else []
    end = next(
        (
            index
            for index, token in enumerate(tokens)
            if token.lower() in _PROGRAM_NAMES
            or (token == "-m" and tokens[index + 1 : index + 2] == ["aisquare"])
        ),
        len(tokens),
    )
    before = tokens[:end]
    if program == "uvx":
        return True
    if program == "pipx":
        return "run" in before
    if program != "uv":
        return False
    if any(before[index : index + 2] == ["tool", "run"] for index in range(len(before))):
        return True
    return "run" in before and any(
        flag != "--with" or _may_install_aisquare(value) for flag, value in _with_values(before)
    )
