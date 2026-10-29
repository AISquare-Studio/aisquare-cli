"""A fleet tmux server started from a Codex tool shell pins nothing of that shell's session,
sandbox or home, and the doctor names each such variable a server still holds.

SMOKE8 of #113 (task #126): a fleet server started by an escalated ``aisquare fleet spawn``
in a Codex 0.164.0 tool shell held ``CODEX_SESSION_ID`` (the spawner's thread),
``CODEX_TOOL_CALL_ID``, ``CODEX_CI=1``, ``CODEX_SANDBOX_NETWORK_DISABLED=1`` and the
spawner's ``CODEX_HOME``; only ``CODEX_THREAD_ID`` and the team variables were stripped.
The coder it started inherited the spawner's session id and sandbox markers, and the
doctor's row said ✓. The variables are what ``RUN env`` printed in a ``codex exec`` tool
shell (values sanitized), the adapter's ``tool_shell_vars``: none is named here outside
these measured shells.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aisquare.core import spawn
from aisquare.models import CheckStatus
from aisquare.services import agent_launch
from tests.test_doctor_fleet import FakeServer, _server_env_row

#: ``RUN env`` in a ``codex exec`` tool shell, Codex 0.162.1 (standalone install), the
#: variables it adds to the shell it was started from.
SHELL_0_162_1 = {
    "CODEX_APPLY_PATCH_PRESERVE_LINE_ENDINGS": "1",
    "CODEX_CI": "1",
    "CODEX_HOME": "/home/u/.aisquare/codex-accounts/3",
    "CODEX_PERMISSION_PROFILE": ":read-only",
    "CODEX_SANDBOX_NETWORK_DISABLED": "1",
    "CODEX_SESSION_ID": "01a16d0e-9189-72d0-a64d-89a5e7546731",
    "CODEX_THREAD_ID": "01a16d0e-9189-72d0-a64d-89a5e7546731",
    "CODEX_VERSION": "0.162.1",
}
#: The same on Codex 0.164.0 (npm): its tool call's id and its package's two variables too.
SHELL_0_164_0 = {
    **SHELL_0_162_1,
    "CODEX_MANAGED_BY_NPM": "1",
    "CODEX_MANAGED_PACKAGE_ROOT": "/usr/lib/node_modules/@openai/codex",
    "CODEX_SESSION_ID": "01a16d0e-9932-72b0-b892-1130e86e6404",
    "CODEX_THREAD_ID": "01a16d0e-9932-72b0-b892-1130e86e6404",
    "CODEX_TOOL_CALL_ID": "call_1",
    "CODEX_VERSION": "0.164.0",
}
OPERATOR = {"AISQUARE_HOME": "/srv/aisquare", "PATH": "/usr/bin", "HOME": "/home/u"}


@pytest.mark.parametrize("shell", [SHELL_0_162_1, SHELL_0_164_0], ids=["0.162.1", "0.164.0"])
def test_a_tmux_client_in_a_codex_tool_shell_carries_none_of_its_variables(
    shell: dict[str, str],
) -> None:
    """The client that finds no server starts it with its own environment: from a Codex tool
    shell, nothing of that shell's session, sandbox, install or home reaches the server."""
    env = spawn.tmux_client_env({**OPERATOR, **shell})

    assert env == OPERATOR, sorted(set(env) - set(OPERATOR))


def test_an_operators_own_codex_home_outside_a_tool_shell_stays() -> None:
    """Only a tool shell's home is the spawner's: a shell that set ``CODEX_HOME`` itself, with
    no tool shell around it, keeps it for the windows it starts, as before."""
    mine = {**OPERATOR, "CODEX_HOME": "/home/u/.codex-work"}

    assert spawn.tmux_client_env(mine) == mine
    assert spawn.tool_shell_homes(mine) == ()
    assert spawn.tool_shell_homes({**mine, "CODEX_CI": "1"}) == ("CODEX_HOME",)


def test_the_agent_launch_starts_from_such_a_window_has_none_of_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A window of a server an older aisquare started from a tool shell still hands them on:
    the Codex ``aisquare launch`` starts there ran with the spawner's ``CODEX_SESSION_ID``,
    ``CODEX_CI=1`` and ``CODEX_SANDBOX_NETWORK_DISABLED=1``. Its own home is the launch's."""
    for name, value in SHELL_0_164_0.items():
        monkeypatch.setenv(name, value)
    selected = agent_launch.resolve("coder", agent="codex", cwd=tmp_path)
    env = dict(os.environ)

    agent_launch.launch_identity(env, selected, None)

    assert {name for name in SHELL_0_164_0 if name in env} == {"CODEX_HOME"}
    assert env["CODEX_HOME"] == str(selected.config_dir)


def test_the_doctor_names_each_tool_shell_variable_a_fleet_server_pins(tmp_path: Path) -> None:
    """The server SMOKE8 read: ``CODEX_THREAD_ID`` already gone, the rest pinned, and the row
    said ``✓ … pins no team variable and no agent's process identity``. It warns now, naming
    each with its unset, the spawner's ``CODEX_HOME`` among them."""
    pinned = {k: v for k, v in SHELL_0_164_0.items() if k != "CODEX_THREAD_ID"}
    server = FakeServer(absent=False, socket="asqui")
    server.scripted = {
        ("show-environment", "-g"): "AISQUARE_HOME=/h\n"
        + "".join(f"{name}={value}\n" for name, value in pinned.items())
    }

    check = _server_env_row(server, (tmp_path,))

    assert check.status is CheckStatus.warn
    assert check.fix is not None
    for name, value in pinned.items():
        assert f"{name}={value}" in check.detail, name
        assert f"tmux -L asqui set-environment -gu {name}" in check.fix, name


def test_the_doctor_leaves_an_operators_own_codex_home_on_the_server(tmp_path: Path) -> None:
    server = FakeServer(absent=False, socket="asqui")
    server.scripted = {("show-environment", "-g"): "AISQUARE_HOME=/h\nCODEX_HOME=/home/u/.cx\n"}

    check = _server_env_row(server, (tmp_path,))

    assert check.status is CheckStatus.ok, check.detail
