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

DISTRIBUTION = "aisquare-cli"

PRODUCT_NAME = "rlx"
"""The product's name, as its command is spelled (#133): the app window's title. The package,
the ``aisquare`` and ``asq`` commands and every other name are unchanged."""

PRODUCT_LOGO = r"r\x"
"""The product's logo, ``r\\x``: r, a backslash, x, in the terminal's own font (the owner,
2026-11-17, R23). Only the app's top-left corner draws it, where :data:`PRODUCT_NAME` was
(``cli.ui.sidebar.fleet_title``); everything else says rlx. Plain text, never markup."""

try:
    __version__ = version(DISTRIBUTION)
except PackageNotFoundError:  # pragma: no cover - only when running from a raw checkout
    __version__ = "0.0.0+uninstalled"

__all__ = ["DISTRIBUTION", "PRODUCT_LOGO", "PRODUCT_NAME", "__version__"]
