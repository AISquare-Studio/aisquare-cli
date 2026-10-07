"""``asq remote`` costs nothing to the commands that never use it — the hooks first.

Every command imports ``cli/app.py``, and ``cli/app.py`` imports every command
module to register it. ``cli/remote.py`` once imported ``services.remote_server``
at module scope, and that module imported ``asyncio``: about thirty modules on
every ``asq hook user-prompt-submit``, and on Windows ``asyncio`` loads
``_overlapped``, which opens a socket at import. A child started without
``SYSTEMROOT`` cannot (WinError 10106), so ``asq --json`` and every hook died
before typer ran — the hooks' fail-open handler never got the chance (PR #243's
windows leg).

Module identity, not milliseconds, for the reason
``test_import_cost_of_the_integration.py`` gives: a wall-clock bound in CI is
flaky by construction.
"""

from __future__ import annotations

import inspect
import subprocess
import sys

from aisquare.cli import remote as remote_cli
from aisquare.services import remote_server

#: What ``import aisquare.cli.app`` must not load: the server, and the event loop
#: behind it. ``starlette``/``uvicorn``/``websockets`` are already lazy inside it.
_OFF_THE_HOOK_PATH = ("asyncio", "aisquare.services.remote_server")


def _loaded_after(code: str) -> set[str]:
    """``sys.modules`` after ``code`` runs in a fresh interpreter, not in this one,
    which has imported everything the suite has."""
    result = subprocess.run(
        [sys.executable, "-c", f"{code}\nimport sys\nprint(' '.join(sorted(sys.modules)))"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return set(result.stdout.split())


def test_the_cli_import_loads_neither_the_server_nor_asyncio() -> None:
    loaded = _loaded_after("import aisquare.cli.app")
    assert "aisquare.cli.remote" in loaded, "the premise: the command module IS registered"
    leaked = [name for name in _OFF_THE_HOOK_PATH if name in loaded]
    assert not leaked, f"every command (and every hook) now imports {leaked}"


def test_the_server_module_leaves_asyncio_to_the_app_it_builds() -> None:
    """``asq remote status``, ``allow-write``, ``revoke`` and ``regenerate-password``
    import the module and serve nothing, so they do not pay for the event loop either."""
    loaded = _loaded_after("import aisquare.services.remote_server")
    assert "aisquare.services.remote_server" in loaded
    assert "asyncio" not in loaded


def test_the_walk_reports_each_name_once_something_imports_it() -> None:
    """The control: the same walk sees both names when they ARE imported."""
    loaded = _loaded_after("import aisquare.cli.app, aisquare.services.remote_server, asyncio")
    assert set(_OFF_THE_HOOK_PATH) <= loaded


def test_the_cli_port_default_is_still_the_servers() -> None:
    """``--port`` spells its default out so the option needs no import. Drifted, ``asq
    remote serve`` and the fleet UI's Remote would listen on different ports."""
    default = inspect.signature(remote_cli.serve_remote).parameters["port"].default
    assert default == remote_server.DEFAULT_PORT
