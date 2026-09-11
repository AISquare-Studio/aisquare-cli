"""Office: the local HTTP/SSE/WebSocket view of the fleet.

This package is the CLI-side implementation of the ``asq Office`` wire contract
frozen at revision 1.4 (``tests/fixtures/office-contract/``). It is split so
that the *typed foundation* — models, configuration and ports — costs nothing
to import:

* :mod:`aisquare.office.models` — the wire and internal value types.
* :mod:`aisquare.office.config` — :class:`~aisquare.office.config.OfficeConfig`.
* :mod:`aisquare.office.ports` — the protocols later packets implement.

**This module imports nothing optional, and nothing from the server.** The
Office serving dependencies (Starlette, uvicorn, websockets, an HTTP client)
live behind the ``office`` extra, and only the server modules may import them.
Every ordinary CLI command and every hook must keep working in a base install,
which is what ``tests/office/test_foundation_imports.py`` pins: importing
``aisquare`` — or this package, or any of the three modules above — must never
pull an Office extra in.

Re-exporting the models from here would defeat that on the first day someone
adds a serving import to a sibling module, so this file deliberately exports
nothing. Import the module you need.
"""

from __future__ import annotations
