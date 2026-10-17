"""Every private helper the source cites by its module exists.

Docstrings and comments here cite helpers by module (``team._nudge_manager``,
``fleet._board_of_row``) to say where a rule lives. A helper renamed or removed leaves
each citation pointing nowhere, and nothing failed: ``fleet.nudge_manager`` cited
``team._target_of``, which never existed (review of #259, round 29, 7/8).

The census has a floor, so a pattern narrowed to nothing cannot pass by checking nothing.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import aisquare
from aisquare import core, services

#: ``module._name`` in backticks, the module optionally ``core.`` or ``services.``.
_CITED = re.compile(r"`(?:aisquare\.)?(core\.|services\.)?([a-z]\w*)\.(_\w+)`")
_PACKAGES = {"core.": core.__name__, "services.": services.__name__}


def _modules() -> dict[str, list[str]]:
    """Each module of ``aisquare.core`` and ``aisquare.services`` by its short name."""
    found: dict[str, list[str]] = {}
    for package in (core, services):
        for info in pkgutil.iter_modules(package.__path__):
            found.setdefault(info.name, []).append(f"{package.__name__}.{info.name}")
    return found


def test_every_private_helper_the_source_cites_by_module_exists() -> None:
    src = Path(aisquare.__file__).parent
    modules = _modules()
    checked = 0
    missing: list[str] = []
    for path in sorted(src.rglob("*.py")):
        for match in _CITED.finditer(path.read_text(encoding="utf-8")):
            prefix, module, name = match.groups()
            candidates = [
                full
                for full in modules.get(module, [])
                if prefix is None or full.startswith(_PACKAGES[prefix] + ".")
            ]
            if not candidates:
                continue  # not a module of ours: a config key, an attribute
            checked += 1
            if not any(hasattr(importlib.import_module(full), name) for full in candidates):
                missing.append(f"{path.relative_to(src)}: {match.group(0)}")

    assert checked >= 50, f"only {checked} citations found: the pattern no longer reads them"
    assert missing == []
