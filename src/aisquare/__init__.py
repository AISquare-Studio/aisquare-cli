"""aisquare — a portable memory layer for coding agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("aisquare")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw checkout
    __version__ = "0.0.0+uninstalled"

__all__ = ["__version__"]
