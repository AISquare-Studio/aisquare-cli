"""One package under two names (#137): ``rlx``, and ``aisquare-cli``, which every release is
also published as, at the same version and from the same commit.

Each install reads PyPI, upgrades, is removed and is advised under its OWN name, and nothing
this package runs puts the other name beside it: measured with uv 0.11.6, a second tool with
the same commands is refused, and forced, the first one's uninstall later deletes the second's
links. A uv tool's receipt and a pipx venv say the name; anything else is the dist-info that
runs (``version.DISTRIBUTION``). install.sh's side is in tests/test_install_script_functions.py.
"""

from __future__ import annotations

import io
import json
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import install as install_cli
from aisquare.cli.app import app
from aisquare.core import selfcli
from aisquare.core import version as version_module
from aisquare.core.agent_adapters import lasting
from aisquare.core.version import DISTRIBUTION, DISTRIBUTIONS
from aisquare.services import diagnostics, install_route, lifecycle
from aisquare.services.install_route import Facts, InstallRoute
from tests.installer_seams import no_real_installer  # noqa: F401 — autouse, applied by import
from tests.test_lifecycle_upgrade import Machine
from tests.test_lifecycle_upgrade import machine as _machine

machine = _machine

OTHER = {"rlx": "aisquare-cli", "aisquare-cli": "rlx"}


# --- the name this install was made under ---------------------------------------------------


def _metadata(monkeypatch: pytest.MonkeyPatch, installed: dict[str, str]) -> None:
    def version(name: str) -> str:
        if name in installed:
            return installed[name]
        raise PackageNotFoundError(name)

    monkeypatch.setattr(version_module, "version", version)


@pytest.mark.parametrize(
    ("installed", "found", "both"),
    [
        ({"rlx": "0.11.0"}, ("rlx", "0.11.0"), ("rlx",)),
        ({"aisquare-cli": "0.11.0"}, ("aisquare-cli", "0.11.0"), ("aisquare-cli",)),
        ({"rlx": "0.11.0", "aisquare-cli": "0.10.0"}, ("rlx", "0.11.0"), DISTRIBUTIONS),
        ({}, None, ()),
    ],
    ids=["rlx", "aisquare-cli", "both", "neither"],
)
def test_the_distribution_is_the_one_installed_here_and_rlx_when_both_are(
    monkeypatch: pytest.MonkeyPatch,
    installed: dict[str, str],
    found: tuple[str, str] | None,
    both: tuple[str, ...],
) -> None:
    _metadata(monkeypatch, installed)

    assert version_module._installed() == found
    assert version_module.installed_distributions() == both


def test_this_tree_runs_as_the_distribution_installed_for_it() -> None:
    """The suite's editable install: whichever name it was installed under, the module says
    that one, and its version is that distribution's."""
    assert DISTRIBUTION in DISTRIBUTIONS
    assert version_module._installed() == (DISTRIBUTION, version_module.__version__)
    assert DISTRIBUTIONS == ("rlx", "aisquare-cli") and version_module.PLUGIN_DISTRIBUTION == (
        "aisquare-cli"
    )


# --- each route's own name ------------------------------------------------------------------


def _tool(tmp_path: Path, name: str, *, receipt_name: str | None = None) -> InstallRoute:
    """A uv tool at ``tools/<name>`` whose receipt requires ``receipt_name`` (``name``)."""
    prefix = tmp_path / "uv" / "tools" / name
    (prefix / "bin").mkdir(parents=True)
    required = receipt_name or name
    (prefix / install_route.RECEIPT_NAME).write_text(
        f'[tool]\nrequirements = [\n    {{ name = "{required}" }},\n    {{ name = "tiktoken" }},\n'
        ']\npython = "3.13"\n',
        encoding="utf-8",
    )
    return install_route.classify(_facts(prefix))


def _facts(prefix: Path, **overrides: Any) -> Facts:
    values: dict[str, Any] = {
        "prefix": prefix,
        "base_prefix": prefix.parent / "base-python",
        "executable": prefix / "bin" / "python",
        "platform": "linux",
        "python_version": "3.13",
    }
    values.update(overrides)
    return Facts(**values)


@pytest.mark.parametrize("name", DISTRIBUTIONS)
def test_a_uv_tool_upgrades_and_is_removed_under_the_name_its_receipt_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Whichever distribution runs: the receipt is the install's own record."""
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    route = _tool(tmp_path, name)

    upgrade = install_route.upgrade_argv(route, current="0.11.0")
    pinned = install_route.upgrade_argv(route, "0.12.0")
    remove = install_route.remove_argv(route)

    assert route.kind == install_route.UV_TOOL and route.receipt is not None
    assert route.receipt.name == route.distribution == name
    assert upgrade[-3:] == ["--refresh-package", name, f"{name}>=0.11.0"], upgrade
    assert pinned[-1] == f"{name}@0.12.0", pinned
    assert remove == ["uv", "tool", "uninstall", name]
    assert install_route.not_automated(route) is None
    for argv in (upgrade, pinned, remove):
        assert not any(OTHER[name] in word for word in argv), f"never the other name: {argv}"


def test_a_receipt_requiring_neither_name_is_refused_naming_both(tmp_path: Path) -> None:
    route = _tool(tmp_path, "aisquare-cli", receipt_name="something-else")

    assert route.receipt is not None and route.receipt.name is None
    assert "no rlx or aisquare-cli requirement" in route.receipt.unrestatable


def test_a_tool_environment_named_for_the_other_name_is_not_ours_to_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``uv tool … rlx`` acts on ``tools/rlx``: a receipt requiring rlx in ``tools/aisquare-cli``
    would reinstall into another environment, so it is reported, not run."""
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    route = _tool(tmp_path, "aisquare-cli", receipt_name="rlx")

    assert install_route.not_automated(route) == (
        "the tool environment is named 'aisquare-cli', not 'rlx'"
    )


@pytest.mark.parametrize("name", DISTRIBUTIONS)
def test_a_pipx_install_is_named_by_its_venv(tmp_path: Path, name: str) -> None:
    venv = tmp_path / "pipx" / "venvs" / name
    venv.mkdir(parents=True)
    (venv / install_route.PIPX_METADATA_NAME).write_text("{}", encoding="utf-8")
    route = install_route.classify(_facts(venv))

    assert route.kind == install_route.PIPX and route.distribution == name
    assert install_route.upgrade_argv(route) == ["pipx", "upgrade", name]
    assert install_route.remove_argv(route) == ["pipx", "uninstall", name]


@pytest.mark.parametrize("name", DISTRIBUTIONS)
def test_any_other_install_is_named_by_the_distribution_that_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(install_route, "DISTRIBUTION", name)
    venv = tmp_path / "venv"
    venv.mkdir()
    route = install_route.classify(_facts(venv, installer="pip"))

    assert route.kind == install_route.VENV and route.distribution == name
    assert install_route.upgrade_argv(route)[-1] == name
    assert install_route.remove_argv(route)[-1] == name


@pytest.mark.parametrize("name", DISTRIBUTIONS)
def test_pypi_is_asked_about_the_install_s_own_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    """Every release is published under both names alike; each install still reads its own
    page, the one its upgrade installs from."""
    asked: list[str] = []

    class _Answer(io.BytesIO):
        def __enter__(self) -> _Answer:
            return self

        def __exit__(self, *exc: object) -> None:
            self.close()

    def open_url(request: Any, *, timeout: float) -> _Answer:
        asked.append(request.full_url)
        return _Answer(json.dumps({"info": {"version": "0.12.0"}, "releases": {}}).encode())

    monkeypatch.setattr(install_route, "fetch_latest", _REAL_FETCH_LATEST)
    monkeypatch.setattr(install_route, "open_url", open_url)
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    route = _tool(tmp_path, name)

    found = lifecycle._latest_for(route)

    assert found.version == "0.12.0", found
    assert asked == [f"https://pypi.org/pypi/{name}/json"]


_REAL_FETCH_LATEST = install_route.fetch_latest


# --- condition 1: nothing installs the other name beside an install -------------------------


@pytest.mark.parametrize("name", DISTRIBUTIONS)
@pytest.mark.parametrize("how", ["upgrade --yes", "upgrade --one-click"])
def test_upgrade_and_asq_s_update_reinstall_under_the_install_s_own_name(
    runner: CliRunner,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    how: str,
) -> None:
    """`aisquare upgrade`, and asq's Update (`--one-click`, which Doctor's Update button runs
    too): the one reinstall names the install's own name and never the other, whichever
    distribution runs the command."""
    route = _tool(tmp_path, name)
    monkeypatch.setattr(install_route, "facts", lambda: route.facts)
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(lifecycle, "__version__", "0.9.0")
    monkeypatch.setattr(install_cli, "_stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    monkeypatch.setattr(selfcli, "exec_self", lambda args: None)

    result = runner.invoke(app, how.split())

    assert result.exit_code == 0, result.output
    [(argv, env, _)] = machine.installs
    assert f"{name}>=0.9.0" in argv, argv
    assert not any(OTHER[name] in word for word in argv), argv
    assert env["UV_TOOL_DIR"] == str(route.facts.prefix.parent)


def test_the_doctor_s_install_advice_names_this_install_s_own_name() -> None:
    """The install row's fix (and every one built from it) installs this install's name."""
    assert (
        f"pipx install {DISTRIBUTION} (or: uv tool install {DISTRIBUTION})"
    ) == diagnostics._GLOBAL_INSTALL
    assert OTHER[DISTRIBUTION] not in diagnostics._GLOBAL_INSTALL


# --- both installed in one environment ------------------------------------------------------


def test_the_doctor_warns_while_both_names_share_one_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnostics, "installed_distributions", lambda: DISTRIBUTIONS)
    both = diagnostics._check_both_names()
    names = [check.name for check in diagnostics.doctor()]
    monkeypatch.setattr(diagnostics, "installed_distributions", lambda: (DISTRIBUTION,))
    one = diagnostics._check_both_names()
    without = [check.name for check in diagnostics.doctor()]

    assert both is not None and both.status == "warn" and both.name == "package names"
    assert "rlx and aisquare-cli are both installed in" in both.detail
    assert "removing either breaks the other" in both.detail
    assert both.fix is not None and both.fix.startswith("Keep one: uninstall aisquare-cli")
    assert names[names.index("install") + 1] == "package names", names
    assert one is None and "package names" not in without


# --- the MCP reader takes either name -------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "fetches"),
    [
        ({"command": "uvx", "args": ["--from", "rlx", "rlx", "serve"]}, True),
        ({"command": "uvx", "args": ["--from", "rlx==0.11.0", "aisquare", "serve"]}, True),
        ({"command": "uvx", "args": ["--from", "aisquare-cli", "relax", "serve"]}, True),
        ({"command": "uv", "args": ["run", "--with", "rlx", "aisquare", "serve"]}, True),
        ({"command": "/home/u/.local/bin/rlx", "args": ["serve"]}, False),
    ],
    ids=["uvx-rlx", "uvx-rlx-pinned", "uvx-aisquare-cli-relax", "uv-run-with-rlx", "installed"],
)
def test_an_mcp_server_fetching_either_name_is_ours(spec: dict[str, object], fetches: bool) -> None:
    assert lasting.runs_aisquare(spec)
    assert lasting.fetches_aisquare(spec) is fetches
