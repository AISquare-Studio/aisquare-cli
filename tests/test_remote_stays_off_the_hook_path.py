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
import json
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli import remote as remote_cli
from aisquare.cli.app import app
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import HookSpec
from aisquare.core.store import store_session
from aisquare.services import remote_server
from tests.projects import add_project

SERVICES = Path(remote_server.__file__).parent

#: What ``import aisquare.cli.app`` must not load: the event loop, the crypto push
#: needs, and every remote service module, computed so a new lane module is covered
#: the day it lands. ``starlette``/``uvicorn``/``websockets`` are lazy inside them.
_OFF_THE_HOOK_PATH = (
    "asyncio",
    "cryptography",
    *(f"aisquare.services.{module.stem}" for module in sorted(SERVICES.glob("remote_*.py"))),
)


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
    """The control: the same walk sees every name when they ARE imported."""
    assert {"aisquare.services.remote_server", "aisquare.services.remote_needs"} <= set(
        _OFF_THE_HOOK_PATH
    ), "the glob found the lane modules"
    imports = ", ".join(name for name in _OFF_THE_HOOK_PATH if name != "cryptography")
    loaded = _loaded_after(f"import aisquare.cli.app, {imports}")
    assert set(_OFF_THE_HOOK_PATH) - {"cryptography"} <= loaded


def test_the_cli_port_default_is_still_the_servers() -> None:
    """``--port`` spells its default out so the option needs no import. Drifted, ``asq
    remote serve`` and the fleet UI's Remote would listen on different ports."""
    default = inspect.signature(remote_cli.serve_remote).parameters["port"].default
    assert default == remote_server.DEFAULT_PORT


# --- every hook, run as Claude Code runs it (#514's CI, brief 212) ------------------------------

#: What a Claude Code hook loads beyond ``import aisquare.cli.app``: the transcript readers its
#: Stop and SessionEnd read a turn's usage with, and nothing else. Claude Code 2.1.318 gives
#: SessionEnd hooks 1.5 s (``CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS``'s default, when the
#: hook names no ``timeout``), the interpreter and its imports included, and cancels one past
#: that: the session's end never reaches the board. So a hook's import graph does not grow
#: in silence; a module added here is a cost on every hook it reaches.
_HOOK_EXTRAS = frozenset(
    {
        "aisquare.core.agent_adapters.claude_transcript",
        "aisquare.core.agent_adapters.claude_usage",
        "aisquare.core.agent_adapters.usage_tally",
    }
)


@cache
def _cli_loads() -> frozenset[str]:
    return frozenset(_loaded_after("import aisquare.cli.app"))


def _hook_loads(command: str, payload: dict[str, object], folder: Path) -> set[str]:
    """``sys.modules`` once ``python -P -m aisquare hook <command>`` has handled ``payload``, in
    a fresh interpreter with this test's home (the environment the suite isolated)."""
    out = folder / f"{command}.modules"
    code = (
        "import runpy, sys\n"
        f"sys.argv = ['aisquare', 'hook', {command!r}]\n"
        "try:\n"
        "    runpy.run_module('aisquare', run_name='__main__', alter_sys=True)\n"
        "except SystemExit:\n"
        "    pass\n"
        f"open({str(out)!r}, 'w').write(' '.join(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-P", "-c", code],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        cwd=str(payload["cwd"]),
    )
    assert result.returncode == 0, result.stderr
    return set(out.read_text().split())


@pytest.mark.parametrize(
    "spec", get_adapter("claude-code").capabilities.hooks, ids=lambda spec: spec.command
)
def test_each_claude_code_hook_loads_the_cli_and_its_transcript_readers_only(
    spec: HookSpec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each hook Claude Code runs, on a session the board knows: what it loads is the CLI's
    import graph and its transcript readers, never the remote lane."""
    work = tmp_path / "repo"
    work.mkdir()
    add_project(work)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    sid = "hook-imports-0000-0000-0000-000000000001"
    payload: dict[str, object] = {
        "cwd": str(work),
        "session_id": sid,
        "source": "startup",
        "reason": "prompt_input_exit",
        "prompt": "an ordinary turn",
        "stop_hook_active": False,
        "transcript_path": str(transcript),
    }
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    started = CliRunner().invoke(app, ["hook", "session-start"], input=json.dumps(payload))
    assert started.exit_code == 0, started.output
    loaded = _hook_loads(spec.command, payload, tmp_path)
    if spec.command == "session-end":
        with store_session() as store:
            session = store.get_session(sid)
        assert session is not None and session.ended_at is not None, "the premise: it ran"
    leaked = [name for name in _OFF_THE_HOOK_PATH if name in loaded]
    assert not leaked, f"`aisquare hook {spec.command}` now imports {leaked}"
    extra = {name for name in loaded - _cli_loads() if name.split(".")[0] == "aisquare"}
    assert extra <= _HOOK_EXTRAS, (
        f"`aisquare hook {spec.command}` now also imports {sorted(extra - _HOOK_EXTRAS)}"
    )
