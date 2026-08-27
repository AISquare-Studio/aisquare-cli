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

try:
    __version__ = version(DISTRIBUTION)
except PackageNotFoundError:  # pragma: no cover - only when running from a raw checkout
    __version__ = "0.0.0+uninstalled"

__all__ = ["DISTRIBUTION", "__version__"]
