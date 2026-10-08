"""A machine without the remote extra is told the one command that adds it there.

It was told ``pip install 'aisquare-cli[remote]' (or: pipx inject aisquare-cli
websockets)``, and neither fixed the install the docs give. That is a uv tool
(``install.sh``, the README): its environment has no pip, so ``pip`` reached
another interpreter or none, and there is no pipx environment to inject into.
Nor did the inject fix a pipx install made without the extra, which misses
starlette and uvicorn too: ``serve`` said the same sentence again after it
(review of #243, the sweep after round 3).

Then the uv command was a fixed one, and installing a tool again keeps only what
the command names: following it took the ``serve`` extra, and ``aisquare
serve`` with it, from a tool installed with ``[serve]``, and any other package
the tool was given. And on Windows its words were quoted for a POSIX shell,
which cmd.exe and PowerShell cannot run (verification of the sweep's fix).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.services import remote_push, remote_server

PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"
EVERY_PACKAGE = ("starlette", "uvicorn", "websockets", "cryptography")
INSTALL_SH_RECEIPT = """\
[tool]
requirements = [
    { name = "aisquare-cli" },
    { name = "tiktoken" },
]
python = "3.13"
entrypoints = [
    { name = "aisquare", install-path = "/home/me/.local/bin/aisquare", from = "aisquare-cli" },
    { name = "asq", install-path = "/home/me/.local/bin/asq", from = "aisquare-cli" },
]
"""
"""What ``install.sh`` leaves (``uv tool install --python 3.13 --with tiktoken aisquare-cli``),
as uv 0.12.19 writes it."""


def _installed(
    monkeypatch: pytest.MonkeyPatch,
    prefix: Path,
    *,
    missing: tuple[str, ...] = EVERY_PACKAGE,
    platform: str = "linux",
    executable: str | None = None,
) -> None:
    """Run as aisquare-cli installed at ``prefix`` on ``platform`` would, without the packages
    ``missing``."""
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", executable or str(prefix / "bin" / "python"))
    monkeypatch.setattr(sys, "platform", platform)
    found = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, package=None: None if name in missing else found(name, package),
    )


def _uv_tool(
    monkeypatch: pytest.MonkeyPatch, prefix: Path, receipt: str | bytes, *, platform: str = "linux"
) -> str:
    """The hint for a uv tool at ``prefix`` whose ``uv-receipt.toml`` says ``receipt``."""
    prefix.mkdir(parents=True, exist_ok=True)
    data = receipt.encode() if isinstance(receipt, str) else receipt
    (prefix / "uv-receipt.toml").write_bytes(data)
    _installed(monkeypatch, prefix, platform=platform)
    return remote_server.remote_install_hint()


def _receipt(*requirements: str, python: str | None = "3.13", tail: str = "") -> str:
    """A receipt naming ``requirements``, as uv writes one, with ``tail`` after them."""
    lines = ["[tool]", "requirements = [", *(f"    {r}," for r in requirements), "]"]
    if python is not None:
        lines.append(f'python = "{python}"')
    return "\n".join([*lines, tail]) + "\n"


def test_a_uv_tool_installed_by_install_sh_is_installed_again_with_the_extra_and_tiktoken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uv cannot add a package to a tool, and installing one again resolves it from that
    command alone: without ``--with tiktoken`` the tiktoken ``install.sh`` put there goes."""
    hint = _uv_tool(monkeypatch, tmp_path, INSTALL_SH_RECEIPT)
    assert hint == "uv tool install --python 3.13 --with tiktoken 'aisquare-cli[remote]'"
    assert remote_server._remote_dependency_error() == (
        f"the remote extra is not installed (starlette, uvicorn, websockets missing) — {hint}"
    )


def test_a_uv_tool_keeps_every_extra_and_package_it_was_installed_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fixed command took ``serve`` (mcp, so ``aisquare serve`` stopped working) and every
    ``--with`` but tiktoken from the tool it was meant to add Remote to."""
    receipt = _receipt(
        '{ name = "aisquare-cli", extras = ["serve"] }',
        '{ name = "tiktoken" }',
        '{ name = "llm", specifier = ">=0.12" }',
        python="3.12",
    )
    assert _uv_tool(monkeypatch, tmp_path, receipt) == (
        "uv tool install --python 3.12 --with tiktoken --with 'llm>=0.12' "
        "'aisquare-cli[remote,serve]'"
    )


@pytest.mark.parametrize(
    ("requirements", "tail", "command"),
    [
        (
            ['{ name = "aisquare-cli", specifier = "==0.7.0" }'],
            "",
            "uv tool install --python 3.13 'aisquare-cli[remote]==0.7.0'",
        ),
        (
            [
                '{ name = "aisquare-cli", git = "https://github.com/AISquare-Studio/aisquare-cli'
                '?rev=feat%2Fremote&subdirectory=cli" }'
            ],
            "",
            "uv tool install --python 3.13 'aisquare-cli[remote] @ git+https://github.com/"
            "AISquare-Studio/aisquare-cli@feat/remote#subdirectory=cli'",
        ),
        (
            ['{ name = "aisquare-cli", git = "https://github.com/AISquare-Studio/aisquare-cli" }'],
            "",
            "uv tool install --python 3.13 "
            "'aisquare-cli[remote] @ git+https://github.com/AISquare-Studio/aisquare-cli'",
        ),
        (
            [
                '{ name = "aisquare-cli", url = "https://example.com/cli.zip", '
                'subdirectory = "cli" }'
            ],
            "",
            "uv tool install --python 3.13 "
            "'aisquare-cli[remote] @ https://example.com/cli.zip#subdirectory=cli'",
        ),
        (
            [
                '{ name = "aisquare-cli" }',
                '{ name = "tiktoken", marker = "sys_platform != \'win32\'" }',
            ],
            "",
            "uv tool install --python 3.13 --with 'tiktoken ; sys_platform != '\"'\"'win32'\"'\"'' "
            "'aisquare-cli[remote]'",
        ),
        (
            ['{ name = "aisquare-cli" }', '{ name = "llm" }'],
            'entrypoints = [{ name = "llm", install-path = "/b/llm", from = "llm" }]',
            "uv tool install --python 3.13 --with-executables-from llm 'aisquare-cli[remote]'",
        ),
    ],
    ids=["a pin", "a git branch", "a git default branch", "a url", "a marker", "executables"],
)
def test_a_uv_tool_is_asked_for_again_as_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requirements: list[str],
    tail: str,
    command: str,
) -> None:
    """A pin, a source and a marker are what the requirement was; a package whose executables
    the tool took is given again as such, or they go."""
    assert _uv_tool(monkeypatch, tmp_path, _receipt(*requirements, tail=tail)) == command


def test_a_uv_tool_from_a_local_source_stays_on_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``'aisquare-cli[remote]'`` would have swapped a checkout, editable or not, for the
    index's release."""
    source = tmp_path / "aisquare cli"
    for key, editable in (("directory", ""), ("editable", "--editable ")):
        receipt = _receipt(f'{{ name = "aisquare-cli", {key} = "{source.as_posix()}" }}')
        assert _uv_tool(monkeypatch, tmp_path / key, receipt) == (
            f"uv tool install --python 3.13 {editable}'aisquare-cli[remote] @ {source.as_uri()}'"
        )


def test_a_uv_tool_whose_receipt_names_no_python_stays_on_the_one_it_runs_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``--python`` uv picks its own default, and makes the tool anew on it when
    that is another one."""
    receipt = _receipt('{ name = "aisquare-cli", extras = ["serve"] }', python=None)
    assert _uv_tool(monkeypatch, tmp_path, receipt) == (
        f"uv tool install --python {PYTHON} 'aisquare-cli[remote,serve]'"
    )


@pytest.mark.parametrize(
    "receipt",
    [b"[tool\n", b"\xff[tool]\n", b"[tool]\n", b"[tool]\nrequirements = 3\n", b"tool = 3\n"],
    ids=["not toml", "not utf-8", "no requirements", "requirements not a list", "no table"],
)
def test_a_receipt_that_cannot_be_read_gets_the_command_install_sh_installs_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, receipt: bytes
) -> None:
    """Nothing to say again: the documented install's own command is the best left."""
    assert _uv_tool(monkeypatch, tmp_path, receipt) == (
        f"uv tool install --python {PYTHON} --with tiktoken 'aisquare-cli[remote]'"
    )


@pytest.mark.parametrize(
    ("requirements", "tail"),
    [
        (['{ name = "aisquare-cli" }'], 'constraints = [{ name = "tiktoken", specifier = "<1" }]'),
        (['{ name = "aisquare-cli" }'], 'excludes = ["tiktoken"]'),
        (['{ name = "aisquare-cli", index = "https://example.com/simple" }'], ""),
        (['{ name = "aisquare-cli", git = "https://example.com/cli?lfs=true" }'], ""),
        (['{ name = "aisquare-cli", path = "relative.whl" }'], ""),
        (['{ name = "other-tool" }', '{ name = "aisquare-cli" }'], ""),
    ],
    ids=["constraints", "excludes", "an index", "git lfs", "a relative path", "another tool"],
)
def test_a_receipt_with_what_no_command_carries_is_named_instead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, requirements: list[str], tail: str
) -> None:
    """A command that drops what the tool was installed with is the bug, so these get a
    sentence naming where uv recorded it."""
    receipt = _receipt(*requirements, tail=tail)
    assert _uv_tool(monkeypatch, tmp_path, receipt) == (
        f"install aisquare-cli again with uv tool install, as {tmp_path / 'uv-receipt.toml'} "
        "records it, adding remote to its extras"
    )


@pytest.mark.parametrize(
    ("requirements", "python", "tail", "said"),
    [
        (
            ['{ name = "aisquare-cli" }', '{ name = "llm" }'],
            "3.13",
            'entrypoints = [{ name = "llm", install-path = "/b/llm", from = ["llm"] }]',
            "uv tool install --python 3.13 --with llm 'aisquare-cli[remote]'",
        ),
        (['{ name = "aisquare-cli", git = "https://[::1/cli" }'], "3.13", "", None),
        (['"aisquare-cli"'], "3.13", "", None),
        (['{ name = "aisquare-cli", extras = "serve" }'], "3.13", "", None),
        (['{ name = "aisquare-cli" }'], None, "python = 3.13", None),
        (['{ name = "aisquare-cli" }'], "3.13", "entrypoints = 3", None),
    ],
    ids=["a list for a from", "a broken url", "a string", "a string of extras", "a number", "3"],
)
def test_a_receipt_uv_would_not_write_is_read_without_an_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requirements: list[str],
    python: str | None,
    tail: str,
    said: str | None,
) -> None:
    """The hint is made when ``remote_push`` is imported, so a receipt it cannot follow costs
    the hint its command, never Remote: a ``from`` that cannot be hashed and a URL that
    cannot be split raised there."""
    hint = _uv_tool(monkeypatch, tmp_path, _receipt(*requirements, python=python, tail=tail))
    sentence = (
        f"install aisquare-cli again with uv tool install, as {tmp_path / 'uv-receipt.toml'} "
        "records it, adding remote to its extras"
    )
    assert hint == (sentence if said is None else said)


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
        ("uv = 0.12.19\n", "uv pip install --python '{python}' 'aisquare-cli[remote]'"),
        ("", "'{python}' -m pip install 'aisquare-cli[remote]'"),
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
    _installed(monkeypatch, prefix)
    python = prefix / "bin" / "python"
    assert remote_server.remote_install_hint() == command.format(python=python)


@pytest.mark.parametrize(
    ("made_by", "executable", "command"),
    [
        (
            "",
            r"C:\Users\me\.venv\Scripts\python.exe",
            r'C:\Users\me\.venv\Scripts\python.exe -m pip install "aisquare-cli[remote]"',
        ),
        (
            "",
            r"C:\Users\Jo Doe\.venv\Scripts\python.exe",
            r'"C:\Users\Jo Doe\.venv\Scripts\python.exe" -m pip install "aisquare-cli[remote]"',
        ),
        (
            "uv = 0.12.19\n",
            r"C:\Users\me\.venv\Scripts\python.exe",
            r'uv pip install --python C:\Users\me\.venv\Scripts\python.exe "aisquare-cli[remote]"',
        ),
    ],
    ids=["a virtualenv", "one under a name with a space", "one uv made"],
)
def test_on_windows_a_virtualenv_is_told_a_command_its_shells_can_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, made_by: str, executable: str, command: str
) -> None:
    """``shlex.quote`` single-quoted every Windows path, for its ``\\``: PowerShell cannot run
    a quoted string as a command, and cmd.exe has no single quotes at all (nor for the
    extra, which reached pip quotes and all)."""
    (tmp_path / "pyvenv.cfg").write_text(f"home = C:\\Python313\n{made_by}")
    _installed(monkeypatch, tmp_path, platform="win32", executable=executable)
    assert remote_server.remote_install_hint() == command


def test_on_windows_a_uv_tool_is_told_a_command_its_shells_can_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Double quotes, which cmd.exe and PowerShell both read: PowerShell splits a bare
    ``aisquare-cli[remote,serve]`` at its comma, and cmd.exe redirects at a ``>=``."""
    receipt = _receipt(
        '{ name = "aisquare-cli", extras = ["serve"] }',
        '{ name = "tiktoken" }',
        '{ name = "llm", specifier = ">=0.12" }',
    )
    assert _uv_tool(monkeypatch, tmp_path, receipt, platform="win32") == (
        'uv tool install --python 3.13 --with tiktoken --with "llm>=0.12" '
        '"aisquare-cli[remote,serve]"'
    )


def test_serve_on_a_uv_tool_without_the_extra_names_the_uv_command_and_no_other(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_home: Path
) -> None:
    """What ``asq remote serve`` printed on the documented install: two commands, and neither
    could reach its environment."""
    (tmp_path / "uv-receipt.toml").write_text(INSTALL_SH_RECEIPT)
    _installed(monkeypatch, tmp_path, platform=sys.platform)
    result = CliRunner().invoke(cli, ["remote", "serve"])
    assert result.exit_code == 1
    said = " ".join(result.output.split())
    extra = '"aisquare-cli[remote]"' if sys.platform == "win32" else "'aisquare-cli[remote]'"
    assert f"uv tool install --python 3.13 --with tiktoken {extra}" in said
    assert "pip install" not in said and "pipx" not in said


def test_web_push_names_the_same_command_as_the_server_does() -> None:
    """``pip install`` reaches neither a uv tool's environment nor a pipx one, for
    cryptography any more than for the server's three."""
    hint, said = remote_server.remote_install_hint(), remote_push.PUSH_INSTALL_HINT
    assert said == f"Web Push needs the cryptography package — {hint}"


def _wheel(into: Path, name: str, *, requires: tuple[str, ...] = (), script: str = "") -> None:
    """A wheel of ``name`` 1.0 that installs nothing but its metadata, its ``requires`` and
    ``script``, enough for uv to resolve and install offline."""
    dist = name.replace("-", "_")
    files = {f"{dist}/__init__.py": "def main():\n    pass\n"}
    extras = sorted({r.split('extra == "')[1].rstrip('"') for r in requires if "extra ==" in r})
    meta = [
        "Metadata-Version: 2.1",
        f"Name: {name}",
        "Version: 1.0",
        *(f"Provides-Extra: {extra}" for extra in extras),
        *(f"Requires-Dist: {requirement}" for requirement in requires),
    ]
    info = f"{dist}-1.0.dist-info"
    files[f"{info}/METADATA"] = "\n".join(meta) + "\n"
    files[f"{info}/WHEEL"] = "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    if script:
        files[f"{info}/entry_points.txt"] = f"[console_scripts]\n{script} = {dist}:main\n"
    record = []
    for path, text in files.items():
        digest = hashlib.sha256(text.encode()).digest()
        record.append(f"{path},sha256={base64.urlsafe_b64encode(digest).rstrip(b'=').decode()},")
    files[f"{info}/RECORD"] = "\n".join([*record, f"{info}/RECORD,,"]) + "\n"
    with zipfile.ZipFile(into / f"{dist}-1.0-py3-none-any.whl", "w") as wheel:
        for path, text in files.items():
            wheel.writestr(path, text)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed; this test runs it")
@pytest.mark.skipif(sys.platform == "win32", reason="runs the hint as a POSIX shell would")
def test_a_real_uv_keeps_a_tool_installed_with_serve_whole_when_the_hint_is_followed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hint run as printed, by uv itself, on a tool installed as the README's MCP section
    and ``install.sh`` leave it plus one more package: offline, from wheels made here."""
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    requires = ('starlette; extra == "remote"', 'mcp; extra == "serve"')
    _wheel(wheels, "aisquare-cli", requires=requires, script="asq")
    for name in ("starlette", "mcp", "tiktoken", "llm"):
        _wheel(wheels, name)
    config = tmp_path / "uv.toml"
    config.write_text(f'no-index = true\nfind-links = ["{wheels.as_posix()}"]\n')
    env = {
        **os.environ,
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"),
        "UV_CACHE_DIR": str(tmp_path / "cache"),
        "UV_CONFIG_FILE": str(config),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    install = ["--python", sys.executable, "--with", "tiktoken", "--with", "llm"]
    subprocess.run(
        ["uv", "tool", "install", *install, "aisquare-cli[serve]"],
        env=env,
        check=True,
        capture_output=True,
    )
    tool = tmp_path / "tools" / "aisquare-cli"
    _installed(monkeypatch, tool)
    hint = remote_server.remote_install_hint()
    subprocess.run(shlex.split(hint), env=env, check=True, capture_output=True)
    (site,) = tool.glob("lib/python*/site-packages")
    names = {dist.metadata["Name"] for dist in importlib.metadata.distributions(path=[str(site)])}
    assert {"aisquare-cli", "starlette", "mcp", "tiktoken", "llm"} <= names
