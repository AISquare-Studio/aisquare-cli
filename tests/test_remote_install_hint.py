"""A machine without the remote extra is told the one command that adds it there.

It was told ``pip install 'aisquare-cli[remote]' (or: pipx inject aisquare-cli
websockets)``, and neither fixed the install the docs give. That is a uv tool
(``install.sh``, the README): its environment has no pip, so ``pip`` reached
another interpreter or none, and there is no pipx environment to inject into.
Nor did the inject fix a pipx install made without the extra, which misses
starlette and uvicorn too: ``serve`` said the same sentence again after it
(review of #243, the sweep after round 3).
"""

from __future__ import annotations

import importlib.util
import shlex
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.services import remote_push, remote_server

PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"
EVERY_PACKAGE = ("starlette", "uvicorn", "websockets", "cryptography")


def _installed(monkeypatch: pytest.MonkeyPatch, prefix: Path, *, missing: tuple[str, ...]) -> None:
    """Run as aisquare-cli installed at ``prefix`` would, without the packages ``missing``."""
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", str(prefix / "bin" / "python"))
    found = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, package=None: None if name in missing else found(name, package),
    )


def test_a_uv_tool_is_installed_again_with_the_extra_and_keeps_tiktoken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uv cannot add a package to a tool, and installing one again resolves it from that
    command alone: without ``--with tiktoken`` the tiktoken ``install.sh`` put there goes."""
    receipt = '[tool]\nrequirements = [{ name = "aisquare-cli" }, { name = "tiktoken" }]\n'
    (tmp_path / "uv-receipt.toml").write_text(receipt)
    _installed(monkeypatch, tmp_path, missing=EVERY_PACKAGE)
    hint = f"uv tool install --python {PYTHON} --with tiktoken 'aisquare-cli[remote]'"
    assert remote_server.remote_install_hint() == hint
    assert remote_server._remote_dependency_error() == (
        f"the remote extra is not installed (starlette, uvicorn, websockets missing) — {hint}"
    )


@pytest.mark.parametrize(
    ("missing", "injected"),
    [
        (EVERY_PACKAGE, "starlette uvicorn websockets cryptography"),
        (("websockets",), "websockets"),
        (("cryptography",), "cryptography"),
    ],
    ids=["a base install", "a serve install", "a remote extra older than web push"],
)
def test_a_pipx_install_is_told_to_inject_what_it_misses_and_nothing_less(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: tuple[str, ...], injected: str
) -> None:
    """Injecting websockets alone left a base install without starlette and uvicorn, so the
    same sentence came back after the inject it asked for."""
    (tmp_path / "pipx_metadata.json").write_text("{}")
    _installed(monkeypatch, tmp_path, missing=missing)
    assert remote_server.remote_install_hint() == f"pipx inject aisquare-cli {injected}"


@pytest.mark.parametrize(
    ("made_by", "command"),
    [
        ("uv = 0.12.19\n", "uv pip install --python {python} 'aisquare-cli[remote]'"),
        ("", "{python} -m pip install 'aisquare-cli[remote]'"),
    ],
    ids=["made by uv, which gives it no pip", "made by venv"],
)
def test_a_virtualenv_gets_the_extra_from_its_own_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, made_by: str, command: str
) -> None:
    """A bare ``pip`` is whichever one PATH finds first, often another interpreter's."""
    prefix = tmp_path / "a venv with a space"
    prefix.mkdir()
    (prefix / "pyvenv.cfg").write_text(f"home = /usr/bin\n{made_by}version_info = {PYTHON}\n")
    _installed(monkeypatch, prefix, missing=EVERY_PACKAGE)
    python = shlex.quote(str(prefix / "bin" / "python"))
    assert remote_server.remote_install_hint() == command.format(python=python)


def test_serve_on_a_uv_tool_without_the_extra_names_the_uv_command_and_no_other(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_home: Path
) -> None:
    """What ``asq remote serve`` printed on the documented install: two commands, and neither
    could reach its environment."""
    (tmp_path / "uv-receipt.toml").write_text("[tool]\n")
    _installed(monkeypatch, tmp_path, missing=EVERY_PACKAGE)
    result = CliRunner().invoke(cli, ["remote", "serve"])
    assert result.exit_code == 1
    said = " ".join(result.output.split())
    assert f"uv tool install --python {PYTHON} --with tiktoken 'aisquare-cli[remote]'" in said
    assert "pip install" not in said and "pipx" not in said


def test_web_push_names_the_same_command_as_the_server_does() -> None:
    """``pip install`` reaches neither a uv tool's environment nor a pipx one, for
    cryptography any more than for the server's three."""
    hint, said = remote_server.remote_install_hint(), remote_push.PUSH_INSTALL_HINT
    assert said == f"Web Push needs the cryptography package — {hint}"
