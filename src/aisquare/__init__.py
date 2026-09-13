"""aisquare — a portable memory layer for coding agents.

``__version__`` stays re-exported here for anyone who already imports it from
the package root, but nothing INSIDE this package may read it back off this
module: the explainability SDK ships as the ``aisquare`` distribution and
overwrites this very file whenever it installs last. :mod:`aisquare.core.version`
is the real home — see ``tests/test_sdk_coexistence.py``.
"""

from pkgutil import extend_path

from aisquare.core.version import __version__

# The optional SDK contributes aisquare.explainability from a separate wheel.
# An editable CLI checkout must see that namespace as well as its own source.
__path__ = extend_path(__path__, __name__)

__all__ = ["__version__"]
