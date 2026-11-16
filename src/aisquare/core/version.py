"""The installed version of this CLI, resolved without the top-level package.

Load-bearing, not a stylistic split. The explainability SDK is distributed as
``aisquare`` and installs its own ``aisquare/__init__.py`` into the very
directory this package occupies; pip's RECORD for the two distributions
overlaps on exactly that one file and the last writer wins it without a
warning. Subpackages never collide, and an absent ``__init__.py`` just makes
``aisquare`` a PEP 420 namespace package where ``aisquare.cli`` still imports —
so ``from aisquare import __version__`` was the single import in the tree that
could not survive having the SDK installed alongside us. Distribution metadata
is immune to which ``__init__.py`` is on disk, and to there being none.

See ``tests/test_sdk_coexistence.py``, which pins the whole reduction.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTIONS = ("rlx", "aisquare-cli")
"""Every name this package is published under (#137): ``rlx``, and ``aisquare-cli``, which every
release is ALSO published as, at the same version and from the same commit, so each install
made before the move keeps upgrading under its own name. Never one beside the other: installed
side by side, the two share every file, and removing one takes the other's."""

PLUGIN_DISTRIBUTION = "aisquare-cli"
"""What the Claude Code and Codex plugins' launchers run through ``uvx --from``: published at
every version, so a plugin installed before the move keeps working as it is."""


def _installed() -> tuple[str, str] | None:
    """The first of :data:`DISTRIBUTIONS` installed here, with its version, or ``None``."""
    for name in DISTRIBUTIONS:
        try:
            return name, version(name)
        except PackageNotFoundError:
            continue
    return None


def installed_distributions() -> tuple[str, ...]:
    """Every one of :data:`DISTRIBUTIONS` installed in this environment: one, or both when
    a person installed the second beside the first (the doctor names that)."""
    found: list[str] = []
    for name in DISTRIBUTIONS:
        try:
            version(name)
        except PackageNotFoundError:
            continue
        found.append(name)
    return tuple(found)


_FOUND = _installed()

DISTRIBUTION = _FOUND[0] if _FOUND is not None else DISTRIBUTIONS[0]
"""The name THIS install was made under: ``rlx``, or ``aisquare-cli`` for one made before the
move (or for an install of that name since). Everything that reads PyPI, reinstalls, removes
or advises an install of this package uses it, so each acts on its own name and never puts
the other beside it. Both installed: ``rlx``."""

PRODUCT_NAME = "rlx"
"""The product's name, as its command is spelled (#133): what asq's sidebar header reads.
The package, the ``aisquare`` and ``asq`` commands and every other name are unchanged."""

# The version of whichever is installed; from a raw checkout, none is.
__version__ = _FOUND[1] if _FOUND is not None else "0.0.0+uninstalled"

__all__ = [
    "DISTRIBUTION",
    "DISTRIBUTIONS",
    "PLUGIN_DISTRIBUTION",
    "PRODUCT_NAME",
    "__version__",
    "installed_distributions",
]
