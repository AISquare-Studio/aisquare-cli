"""``aisquare upgrade``: which install is running, the command that moves it, and the run.

Three layers, tested separately because they fail differently:

* the ROUTE — read from the running interpreter's prefix, one positive and one
  negative control per route (a fake prefix on disk, never the real one);
* the COMMAND — the uv receipt restated in uv's own recorded format (measured
  on uv 0.12.19), the pin replaced, never ``uv tool upgrade``;
* the RUN — plan, confirm, install, a version check in a NEW process, and the
  hook refresh, all through the seams in ``services.install_route`` that
  ``tests.installer_seams`` closes for every test here.
"""

from __future__ import annotations

import ast
import inspect
import io
import itertools
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import URLError

import pytest
from typer.testing import CliRunner

from aisquare.cli import install as install_cli
from aisquare.cli.app import app
from aisquare.core import paths, spawn
from aisquare.services import install_route, lifecycle
from aisquare.services.install_route import Captured, Facts, LatestRelease
from tests import installer_seams
from tests.fsperms import can_deny_reads
from tests.installer_seams import no_real_installer  # noqa: F401 — autouse, applied by import

#: The real lookup, captured before the autouse fixture closes it (its own tests
#: drive it with a stand-in ``open_url``, which the fixture also closes).
_REAL_FETCH_LATEST = install_route.fetch_latest

_EVENTS = (
    ("SessionStart", "session-start"),
    ("UserPromptSubmit", "user-prompt-submit"),
    ("SessionEnd", "session-end"),
    ("Stop", "stop"),
    ("Notification", "notification"),
    ("StopFailure", "stop-failure"),
)


def _toml(value: str) -> str:
    """A TOML basic string — JSON's escaping is TOML's, backslashes included (Windows)."""
    return json.dumps(value)


def _receipt(*requirements: str, python: str | None = "3.14", tail: str = "") -> str:
    """A receipt in the exact shape uv 0.12.19 writes (measured in a scratch HOME)."""
    lines = ["[tool]", "requirements = ["]
    lines.extend(f"    {requirement}," for requirement in requirements)
    lines.append("]")
    if python is not None:
        lines.append(f"python = {_toml(python)}")
    return "\n".join(lines) + "\n" + tail


#: What the one-line installer plus `uv tool install --with tiktoken
#: 'aisquare-cli[serve]==0.6.0'` leaves behind.
_OURS_PINNED = '{ name = "aisquare-cli", extras = ["serve"], specifier = "==0.6.0" }'
_TIKTOKEN = '{ name = "tiktoken" }'


def _prefix(root: Path, receipt: str | None = None, name: str = "aisquare-cli") -> Path:
    prefix = root / "tools" / name
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    if receipt is not None:
        (prefix / install_route.RECEIPT_NAME).write_text(receipt, encoding="utf-8")
    return prefix


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


# --- the route: one positive and one negative control per route ------------------------


def test_a_uv_receipt_is_the_uv_tool_route(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path, _receipt(_OURS_PINNED, _TIKTOKEN))

    route = install_route.classify(_facts(prefix))

    assert route.kind == install_route.UV_TOOL
    assert route.receipt is not None and route.receipt.extras == ("serve",)
    assert route.receipt.withs == ("tiktoken",)
    assert route.receipt.python == "3.14"


def test_the_same_prefix_without_a_receipt_is_not_the_uv_tool_route(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path)

    route = install_route.classify(_facts(prefix))

    assert route.kind == install_route.VENV, "a venv with no receipt is a plain venv"
    assert route.receipt is None


def test_pipx_metadata_is_the_pipx_route(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path)
    (prefix / install_route.PIPX_METADATA_NAME).write_text("{}", encoding="utf-8")

    route = install_route.classify(_facts(prefix))

    assert route.kind == install_route.PIPX
    assert install_route.upgrade_argv(route) == ["pipx", "upgrade", "aisquare-cli"]


def test_a_receipt_wins_over_pipx_metadata(tmp_path: Path) -> None:
    """Negative control for pipx: the decision order is the documented one."""
    prefix = _prefix(tmp_path, _receipt(_OURS_PINNED))
    (prefix / install_route.PIPX_METADATA_NAME).write_text("{}", encoding="utf-8")

    assert install_route.classify(_facts(prefix)).kind == install_route.UV_TOOL


def test_a_cellar_prefix_is_homebrew_and_names_its_formula(tmp_path: Path) -> None:
    prefix = tmp_path / "opt" / "Cellar" / "aisquare" / "0.9.0" / "libexec"
    prefix.mkdir(parents=True)

    route = install_route.classify(_facts(prefix))

    assert route.kind == install_route.HOMEBREW
    assert route.formula == "aisquare"
    assert install_route.upgrade_argv(route) == ["brew", "upgrade", "aisquare"]


def test_a_directory_merely_named_like_a_cellar_is_not_homebrew(tmp_path: Path) -> None:
    prefix = tmp_path / "Cellars" / "aisquare" / "libexec"
    prefix.mkdir(parents=True)

    assert install_route.classify(_facts(prefix)).kind != install_route.HOMEBREW


def test_an_editable_direct_url_is_the_editable_route(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    url = json.dumps({"url": checkout.as_uri(), "dir_info": {"editable": True}})

    route = install_route.classify(_facts(_prefix(tmp_path), direct_url=url))

    assert route.kind == install_route.EDITABLE
    assert Path(route.source or "") == checkout
    assert install_route.upgrade_argv(route) == ["git", "-C", str(checkout), "pull"]


def test_the_same_url_without_editable_is_a_local_source(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    url = json.dumps({"url": checkout.as_uri(), "dir_info": {}})

    route = install_route.classify(_facts(_prefix(tmp_path), direct_url=url))

    assert route.kind == install_route.LOCAL_SOURCE
    assert install_route.upgrade_argv(route)[-1] == str(checkout)


def test_a_venv_with_no_source_record_is_the_venv_route(tmp_path: Path) -> None:
    route = install_route.classify(_facts(_prefix(tmp_path), installer="pip"))

    assert route.kind == install_route.VENV
    assert install_route.upgrade_argv(route) == [
        str(route.facts.executable),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "aisquare-cli",
    ]


def test_a_venv_made_by_uv_is_upgraded_with_uv_pip_because_it_has_no_pip(tmp_path: Path) -> None:
    route = install_route.classify(_facts(_prefix(tmp_path), installer="uv"))

    argv = install_route.upgrade_argv(route)

    assert argv[:3] == ["uv", "pip", "install"]
    assert "-m" not in argv, "a uv-made venv has no pip module to run"


def test_an_interpreter_that_is_its_own_base_is_the_system_route(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path)

    route = install_route.classify(_facts(prefix, base_prefix=prefix, user_install=True))

    assert route.kind == install_route.SYSTEM
    assert "--user" in install_route.upgrade_argv(route)


def test_a_system_install_outside_the_user_site_gets_no_user_flag(tmp_path: Path) -> None:
    prefix = _prefix(tmp_path)

    route = install_route.classify(_facts(prefix, base_prefix=prefix, user_install=False))

    assert route.kind == install_route.SYSTEM
    assert "--user" not in install_route.upgrade_argv(route)


def test_the_suite_itself_runs_from_the_editable_route() -> None:
    """The REAL detection, on the real interpreter: conftest refuses to grade anything
    but an editable checkout, so this is the one route every run can see live — and
    the reason no test here can reinstall the suite by accident."""
    route = install_route.detect()

    assert route.kind == install_route.EDITABLE
    assert route.source is not None
    assert (Path(route.source) / "src" / "aisquare").is_dir()


# --- the fixture that keeps the suite off uv and PyPI ----------------------------------


def test_a_closed_seam_refuses_and_is_recorded(
    no_real_installer: list[str],  # noqa: F811 — pytest resolves fixtures by NAME
) -> None:
    """Positive control on ``tests.installer_seams``: a seam nobody replaced is
    refused AND recorded, so a test that swallows the refusal still fails at
    teardown. The record is emptied here because the refusals are the point."""
    with pytest.raises(AssertionError):
        install_route.run_installer(["uv", "tool", "install"], env={}, to_stderr=False)
    with pytest.raises(AssertionError):
        install_route.fetch_latest()

    assert no_real_installer == ["run_installer", "fetch_latest"]
    no_real_installer.clear()


def test_the_fixture_closes_every_process_seam_this_module_registers() -> None:
    """A process call added to install_route lands in ``core.spawn.SEAMS`` (its own
    guard says so); this makes the same addition reach the fixture."""
    registered = {
        key.split("::", 1)[1]
        for key in spawn.SEAMS
        if key.startswith("aisquare/services/install_route.py::")
    }

    assert registered, "install_route registers no seams — this check would be vacuous"
    assert registered <= set(installer_seams.SEAMS), sorted(registered - set(installer_seams.SEAMS))


# --- the uv receipt, restated ----------------------------------------------------------


def _uv_route(tmp_path: Path, receipt: str, **facts: Any) -> install_route.InstallRoute:
    route = install_route.classify(_facts(_prefix(tmp_path, receipt), **facts))
    assert route.receipt is not None, "the fixture built no uv route"
    return route


def test_the_command_restates_the_receipt_and_replaces_the_pin(tmp_path: Path) -> None:
    route = _uv_route(tmp_path, _receipt(_OURS_PINNED, _TIKTOKEN))

    latest = install_route.upgrade_argv(route)
    pinned = install_route.upgrade_argv(route, "0.9.1")

    assert latest == [
        "uv",
        "tool",
        "install",
        "--force",
        "--python",
        "3.14",
        "--with",
        "tiktoken",
        "aisquare-cli[serve]@latest",
    ]
    assert pinned[-1] == "aisquare-cli[serve]@0.9.1"
    assert not any("==0.6.0" in part for part in latest + pinned), "the old pin must go"


def test_with_requirements_keep_their_specifier_marker_and_extras(tmp_path: Path) -> None:
    route = _uv_route(
        tmp_path,
        _receipt(
            '{ name = "aisquare-cli" }',
            '{ name = "tiktoken", specifier = ">=0.7" }',
            '{ name = "truststore", marker = "sys_platform == \'linux\'" }',
            '{ name = "rich", extras = ["jupyter"] }',
        ),
    )

    argv = install_route.upgrade_argv(route)

    withs = [argv[i + 1] for i, part in enumerate(argv) if part == "--with"]
    assert withs == ["tiktoken>=0.7", "truststore; sys_platform == 'linux'", "rich[jupyter]"]
    assert argv[-1] == "aisquare-cli@latest", "no extras recorded, none invented"


def test_a_receipt_without_a_python_restates_the_running_one(tmp_path: Path) -> None:
    """Measured: without --python uv picked 3.12 where the env had been 3.13."""
    route = _uv_route(tmp_path, _receipt(_OURS_PINNED, python=None), python_version="3.13")

    argv = install_route.upgrade_argv(route)

    assert argv[argv.index("--python") + 1] == "3.13"


def test_index_options_are_restated_so_a_mirror_stays_the_mirror(tmp_path: Path) -> None:
    options = (
        "\n[tool.options]\n"
        'index-url = "https://mirror.example/simple"\n'
        'extra-index-url = ["https://a.example/simple", "https://b.example/simple"]\n'
        'prerelease = "allow"\n'
        "compile-bytecode = true\n"
        "no-sources = false\n"
    )
    route = _uv_route(tmp_path, _receipt(_OURS_PINNED, tail=options))

    argv = install_route.upgrade_argv(route)

    assert argv[argv.index("--index-url") + 1] == "https://mirror.example/simple"
    extra = [argv[i + 1] for i, part in enumerate(argv) if part == "--extra-index-url"]
    assert extra == ["https://a.example/simple", "https://b.example/simple"]
    assert argv[argv.index("--prerelease") + 1] == "allow"
    assert "--compile-bytecode" in argv
    assert "--no-sources" not in argv, "a false switch is uv's default, not a flag"


def test_index_tables_are_restated_as_index_flags(tmp_path: Path) -> None:
    """``--default-index`` / ``--index`` (and their UV_* variables) record TABLES —
    measured on uv 0.12.19 — and those are the modern way to point at a mirror."""
    options = (
        "\n[tool.options]\n"
        'index = [{ url = "https://mirror.example/simple", explicit = false, default = true, '
        'format = "simple", authenticate = "auto" }, { name = "extra", '
        'url = "https://extra.example/simple", explicit = false, default = false, '
        'format = "simple", authenticate = "auto" }]\n'
    )
    route = _uv_route(tmp_path, _receipt(_OURS_PINNED, tail=options))

    argv = install_route.upgrade_argv(route)

    assert argv[argv.index("--default-index") + 1] == "https://mirror.example/simple"
    assert argv[argv.index("--index") + 1] == "extra=https://extra.example/simple"
    assert route.receipt is not None and route.receipt.unrestatable == ()


@pytest.mark.parametrize(
    "entry",
    [
        '{ url = "https://x/simple", explicit = true, default = false }',
        '{ url = "https://x/flat", format = "flat" }',
        '{ url = "https://x/simple", authenticate = "always" }',
        '{ url = "https://x/simple", cache-control = { api = "no-cache" } }',
    ],
    ids=["explicit", "flat", "authenticate", "unknown-key"],
)
def test_an_index_no_flag_can_carry_is_refused_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    route = _uv_route(
        tmp_path, _receipt(_OURS_PINNED, tail=f"\n[tool.options]\nindex = [{entry}]\n")
    )

    reason = install_route.not_automated(route)

    assert reason is not None and "a uv index this CLI cannot restate" in reason


@pytest.mark.parametrize(
    ("tail", "named"),
    [
        ('\n[tool.options]\nconfig-settings = { a = "b" }\n', "uv option config-settings"),
        ("\n[tool.options]\nsomething-uv-adds-later = true\n", "uv option something-uv-adds-later"),
        ('constraints = [{ name = "rich", specifier = "<15" }]\n', "constraints"),
    ],
    ids=["a-table-option", "an-unknown-option", "constraints"],
)
def test_what_a_command_line_cannot_carry_refuses_the_automated_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: str, named: str
) -> None:
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    route = _uv_route(tmp_path, _receipt(_OURS_PINNED, tail=tail))

    reason = install_route.not_automated(route)

    assert reason is not None and named in reason


def test_a_receipt_holding_only_restatable_things_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control for the refusal above: the installer's own receipt is fine."""
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    route = _uv_route(
        tmp_path,
        _receipt(
            _OURS_PINNED, _TIKTOKEN, tail='\n[tool.options]\nindex-url = "https://x/simple"\n'
        ),
    )

    assert install_route.not_automated(route) is None


def test_an_unreadable_receipt_is_still_a_uv_tool_but_is_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    route = _uv_route(tmp_path, "[tool\nthis is not toml")

    reason = install_route.not_automated(route)

    assert route.kind == install_route.UV_TOOL
    assert reason is not None and "unreadable" in reason


@pytest.mark.parametrize(
    ("requirement", "kind", "spec"),
    [
        ('{ name = "aisquare-cli", editable = "/src/aisquare-cli" }', install_route.EDITABLE, None),
        (
            '{ name = "aisquare-cli", extras = ["serve"], directory = "/src/aisquare-cli" }',
            install_route.LOCAL_SOURCE,
            "/src/aisquare-cli[serve]",
        ),
        (
            '{ name = "aisquare-cli", git = "https://github.com/o/r?rev=v0.7.0" }',
            install_route.LOCAL_SOURCE,
            "aisquare-cli @ git+https://github.com/o/r",
        ),
    ],
    ids=["editable", "directory", "git"],
)
def test_a_receipt_from_a_source_is_reported_with_its_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requirement: str, kind: str, spec: str | None
) -> None:
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    route = _uv_route(tmp_path, _receipt(requirement))

    argv = install_route.upgrade_argv(route)

    assert route.kind == kind
    assert install_route.not_automated(route) is not None, "a source install is never run"
    if spec is None:
        assert argv == ["git", "-C", "/src/aisquare-cli", "pull"]
    else:
        assert argv[:4] == ["uv", "tool", "install", "--force"]
        assert argv[-1] == spec


def test_windows_is_never_run_and_still_gets_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_route, "find_uv", lambda: "uv.exe")
    windows = _uv_route(tmp_path / "w", _receipt(_OURS_PINNED, _TIKTOKEN), platform="win32")
    linux = _uv_route(tmp_path / "l", _receipt(_OURS_PINNED, _TIKTOKEN), platform="linux")

    reason = install_route.not_automated(windows)

    assert reason is not None and "Windows" in reason
    assert install_route.upgrade_argv(windows)[:4] == ["uv", "tool", "install", "--force"]
    assert install_route.not_automated(linux) is None, "the same receipt runs elsewhere"


def test_no_uv_on_path_is_reported_not_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_route, "find_uv", lambda: None)
    route = _uv_route(tmp_path, _receipt(_OURS_PINNED))

    assert install_route.not_automated(route) == "uv is not on PATH"


def test_a_tool_environment_with_another_name_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``uv tool install --force aisquare-cli`` writes ``tools/aisquare-cli``; from an
    environment named anything else it would install a SECOND copy, not this one."""
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    prefix = _prefix(tmp_path, _receipt(_OURS_PINNED), name="aisquare-cli-old")

    reason = install_route.not_automated(install_route.classify(_facts(prefix)))

    assert reason is not None and "aisquare-cli-old" in reason


def test_the_installer_env_pins_uv_to_this_environment(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin-of-the-shims"
    entry = (
        "entrypoints = [\n"
        f'    {{ name = "aisquare", install-path = {_toml(str(bin_dir / "aisquare"))}, '
        'from = "aisquare-cli" },\n]\n'
    )
    route = _uv_route(tmp_path, _receipt(_OURS_PINNED, tail=entry))

    env = install_route.installer_env(route)

    assert env["UV_TOOL_DIR"] == str(route.facts.prefix.parent)
    assert env["UV_TOOL_BIN_DIR"] == str(bin_dir)
    assert env["UV_PYTHON_DOWNLOADS"] == "automatic", "Fedora's uv.toml says manual"
    assert env["UV_NO_PROGRESS"] == "1"


# --- never `uv tool upgrade` -----------------------------------------------------------


def _upgrade_verbs(source: str) -> list[str]:
    """Every place ``source`` could hand ``uv tool upgrade`` to a process.

    Two shapes: a string that spells it (an f-string's pieces included), and
    argv pieces ``"tool", "upgrade"`` side by side in a list or tuple. A string
    that is only a STATEMENT — a docstring, or a string used as a comment — is
    never passed to a process, and the modules explain at length why the verb
    is wrong, so those are skipped.
    """
    tree = ast.parse(source)
    statements = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in statements
            and re.search(r"\btool\s+upgrade\b", node.value)
        ):
            found.append(f"line {node.lineno}: {node.value!r}")
        if isinstance(node, ast.List | ast.Tuple):
            words = [
                element.value
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
                else None
                for element in node.elts
            ]
            if any(a == "tool" and b == "upgrade" for a, b in itertools.pairwise(words)):
                found.append(f"line {node.lineno}: argv with 'tool', 'upgrade'")
    return found


_GUARDED: tuple[ModuleType, ...] = (install_route, lifecycle, install_cli)


@pytest.mark.parametrize("module", _GUARDED, ids=lambda module: module.__name__)
def test_no_upgrade_module_can_run_uv_tool_upgrade(module: ModuleType) -> None:
    """`uv tool upgrade` leaves a pinned install where it is — "Nothing to upgrade",
    exit 0 (docs/plans/one-line-install.md §3.9.1). The copy of this rule that
    guards install.sh is tests/test_install_script_is_posix.py."""
    source = inspect.getsource(module)

    found = _upgrade_verbs(source)

    assert not found, f"{module.__name__} can run `uv tool upgrade`: {found}"


@pytest.mark.parametrize(
    "source",
    [
        'ARGV = ["uv", "tool", "upgrade", "aisquare-cli"]\n',
        'def run():\n    return ("uv", "tool", "upgrade")\n',
        'COMMAND = "uv tool upgrade aisquare-cli"\n',
        'def run(name):\n    return f"uv tool upgrade {name}"\n',
    ],
    ids=["argv-list", "argv-tuple", "string", "f-string"],
)
def test_the_guard_sees_every_shape_of_the_verb(source: str) -> None:
    """Positive control, one per shape the guard claims to catch."""
    assert _upgrade_verbs(source), f"not caught: {source!r}"


def test_the_guard_does_not_accuse_an_explanation_or_the_right_verb() -> None:
    """Negative control: a docstring that warns against the verb, and the verb we use."""
    source = (
        '"""Never `uv tool upgrade`: it leaves a pin where it is."""\n'
        "def run():\n"
        '    """Not uv tool upgrade."""\n'
        '    return ["uv", "tool", "install", "--force", "aisquare-cli@latest"]\n'
    )

    assert _upgrade_verbs(source) == []


@pytest.mark.parametrize("target", [None, "0.9.1", "0.6.0"], ids=["latest", "pin", "rollback"])
def test_every_uv_command_built_is_an_install_with_force(
    tmp_path: Path, target: str | None
) -> None:
    """The behavioural half: whatever the receipt and target, the verb is
    `install --force`, the one that moves a pinned install."""
    route = _uv_route(tmp_path, _receipt(_OURS_PINNED, _TIKTOKEN))

    argv = install_route.upgrade_argv(route, target)

    assert argv[:4] == ["uv", "tool", "install", "--force"]
    assert "upgrade" not in argv


# --- versions --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("newer", "older"),
    [
        ("0.10.0", "0.9.0"),
        ("0.9.0", "0.9.0rc1"),
        ("0.9.0rc1", "0.9.0b2"),
        ("0.9.0rc1", "0.9.0.dev3"),
        ("0.9.0.post1", "0.9.0"),
        ("1!0.1", "9.9"),
    ],
)
def test_versions_order_as_pep_440_orders_them(newer: str, older: str) -> None:
    assert install_route.is_newer(newer, older) is True
    assert install_route.is_newer(older, newer) is False


def test_trailing_zeros_name_the_same_release() -> None:
    assert install_route.same_version("0.7", "0.7.0")
    assert not install_route.same_version("0.7", "0.7.1")


@pytest.mark.parametrize(
    ("typed", "pinned"),
    [("0.9.1", "0.9.1"), (" v0.9.1 ", "0.9.1"), ("1.0.0rc1", "1.0.0rc1"), ("0.7", "0.7")],
)
def test_a_version_argument_is_one_bare_version(typed: str, pinned: str) -> None:
    assert install_route.version_argument(typed) == pinned


@pytest.mark.parametrize(
    "typed", ["latest", "0.9.1 --index-url https://evil", "", "vv0.9", "0.9.1;rm", "==0.9.1"]
)
def test_anything_else_is_not_a_version_argument(typed: str) -> None:
    assert install_route.version_argument(typed) is None


def test_a_non_version_is_not_compared() -> None:
    assert install_route.version_key("latest") is None
    assert install_route.is_newer("latest", "0.9.0") is None


def test_the_version_is_read_out_of_the_version_line() -> None:
    assert install_route.version_in("aisquare 0.9.1\n") == "0.9.1"
    assert install_route.version_in("Traceback (most recent call last):\n") is None


# --- the PyPI lookup -------------------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def test_the_lookup_reads_info_version(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Any] = []

    def open_url(request: Any, timeout: float) -> _Response:
        asked.append((request.full_url, timeout))
        return _Response(json.dumps({"info": {"version": "0.9.1"}}).encode())

    monkeypatch.setattr(install_route, "open_url", open_url)

    latest = _REAL_FETCH_LATEST()

    assert latest == LatestRelease("0.9.1")
    assert asked == [("https://pypi.org/pypi/aisquare-cli/json", 5.0)]


def test_an_unreachable_pypi_is_an_answer_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def open_url(_request: Any, timeout: float) -> _Response:
        raise URLError("name resolution failed")

    monkeypatch.setattr(install_route, "open_url", open_url)

    latest = _REAL_FETCH_LATEST()

    assert latest.version is None
    assert latest.error is not None and "could not reach PyPI" in latest.error


@pytest.mark.parametrize(
    "body", [b"<html>captive portal</html>", b'{"info": {}}', b'{"info": {"version": "x"}}']
)
def test_an_answer_with_no_version_in_it_is_not_a_version(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    monkeypatch.setattr(install_route, "open_url", lambda _request, timeout: _Response(body))

    latest = _REAL_FETCH_LATEST()

    assert latest.version is None and latest.error


# --- the run ---------------------------------------------------------------------------


@dataclass
class Machine:
    """The outside world as one fake: PyPI, uv, and the new install's answers."""

    latest: LatestRelease = field(default_factory=lambda: LatestRelease("0.9.1"))
    installer_exit: int = 0
    new_version: str = "0.9.1"
    connect_exit: int = 0
    lookups: int = 0
    installs: list[tuple[list[str], dict[str, str], bool]] = field(default_factory=list)
    captured: list[list[str]] = field(default_factory=list)

    def connects(self) -> list[list[str]]:
        return [argv for argv in self.captured if argv[-1] != "--version"]


@dataclass
class Tool:
    prefix: Path
    facts: Facts
    script: Path


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> Machine:
    world = Machine()

    def fetch_latest(timeout: float = 5.0) -> LatestRelease:
        world.lookups += 1
        return world.latest

    def run_installer(argv: Any, *, env: Any, to_stderr: bool) -> int:
        world.installs.append((list(argv), dict(env), to_stderr))
        return world.installer_exit

    def run_captured(argv: Any, *, timeout: float) -> Captured:
        world.captured.append(list(argv))
        if argv[-1] == "--version":
            return Captured(0, f"aisquare {world.new_version}\n")
        if world.connect_exit:
            return Captured(
                world.connect_exit, "", "✗ claude-code is not installed on this machine\n"
            )
        return Captured(0, "connected\n")

    monkeypatch.setattr(install_route, "fetch_latest", fetch_latest)
    monkeypatch.setattr(install_route, "run_installer", run_installer)
    monkeypatch.setattr(install_route, "run_captured", run_captured)
    return world


@pytest.fixture
def tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tool:
    """A uv tool install of 0.9.0 the way the installer leaves one, as the RUNNING install."""
    prefix = _prefix(tmp_path / "uv", _receipt(_OURS_PINNED, _TIKTOKEN))
    script = prefix / "bin" / "aisquare"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    found = _facts(prefix)
    monkeypatch.setattr(install_route, "facts", lambda: found)
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(lifecycle, "__version__", "0.9.0")
    return Tool(prefix, found, script)


def _hooked(directory: Path, program: Path | str, *, foreign: bool = False) -> Path:
    """A Claude Code directory whose settings.json carries aisquare's six hook groups."""
    directory.mkdir(parents=True, exist_ok=True)
    hooks: dict[str, list[dict[str, Any]]] = {
        event: [{"hooks": [{"type": "command", "command": f"{program} hook {verb}"}]}]
        for event, verb in _EVENTS
    }
    if foreign:
        hooks["Stop"].append({"hooks": [{"type": "command", "command": "webhook stop"}]})
    (directory / "settings.json").write_text(json.dumps({"hooks": hooks}, indent=2), "utf-8")
    return directory


def _record(*directories: Path) -> None:
    """This home connected these directories (what `agents connect` records)."""
    paths.ensure_home()
    paths.agents_registry_path().write_text(
        json.dumps(
            {
                "connected": ["claude-code"],
                "connections": {"claude-code": [str(directory) for directory in directories]},
            }
        ),
        encoding="utf-8",
    )


def _one_object(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"--json must print exactly one object, got: {stdout!r}"
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


def test_check_reports_the_route_the_latest_and_the_command(
    runner: CliRunner, tool: Tool, machine: Machine
) -> None:
    result = runner.invoke(app, ["--json", "upgrade", "--check"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["current"] == "0.9.0"
    assert report["latest"] == "0.9.1"
    assert report["update_available"] is True
    assert report["route"] == "uv-tool"
    assert report["runnable"] is True and report["reason"] is None
    assert report["command"].startswith("uv tool install --force --python 3.14 --with tiktoken")
    assert machine.installs == [], "--check installs nothing"


def test_check_offline_says_why_and_leaves_no_home_behind(
    runner: CliRunner, tool: Tool, machine: Machine, isolated_home: Path
) -> None:
    machine.latest = LatestRelease(None, "could not reach PyPI (timed out)")

    result = runner.invoke(app, ["--json", "upgrade", "--check"])

    assert result.exit_code == 0, result.output
    report = _one_object(result.stdout)
    assert report["latest"] is None
    assert report["latest_error"] == "could not reach PyPI (timed out)"
    assert report["update_available"] is None
    assert not isolated_home.exists(), "--check must not create the aisquare home"


def test_a_refused_route_exits_1_with_the_command_in_the_message(
    runner: CliRunner, machine: Machine
) -> None:
    """The suite's own route is editable — the real detection, refused offline."""
    result = runner.invoke(app, ["upgrade", "--yes"])

    assert result.exit_code == 1
    assert "Upgrade it with: git -C " in result.stderr and " pull" in result.stderr
    assert machine.installs == [] and machine.lookups == 0, "refused before PyPI or uv"


def test_a_refused_route_under_json_is_one_error_object(
    runner: CliRunner, machine: Machine
) -> None:
    result = runner.invoke(app, ["--json", "upgrade", "--yes"])

    assert result.exit_code == 1
    error = _one_object(result.stdout)
    assert error["error"] == "upgrade_unsupported_route"
    assert error["hint"].startswith("git -C ")
    assert "editable install" in error["detail"]


def test_windows_refuses_with_the_uv_command_to_run_after_quitting(
    runner: CliRunner, tool: Tool, machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = _facts(tool.prefix, platform="win32")
    monkeypatch.setattr(install_route, "facts", lambda: windows)

    result = runner.invoke(app, ["upgrade", "--yes"])

    assert result.exit_code == 1
    assert "Windows" in result.stderr
    assert "uv tool install --force" in result.stderr
    assert machine.installs == []


def test_off_a_terminal_without_yes_it_is_a_dry_run(
    runner: CliRunner, tool: Tool, machine: Machine
) -> None:
    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert "aisquare 0.9.0 → 0.9.1 (latest on PyPI)" in result.stdout
    assert "dry run: nothing installed" in result.stdout
    assert machine.installs == []


def test_json_without_yes_prints_the_plan_and_changes_nothing(
    runner: CliRunner, tool: Tool, machine: Machine
) -> None:
    result = runner.invoke(app, ["--json", "upgrade"])

    assert result.exit_code == 0, result.output
    plan = _one_object(result.stdout)
    assert plan["dry_run"] is True
    assert plan["argv"][-1] == "aisquare-cli[serve]@latest"
    assert machine.installs == []


@pytest.mark.parametrize(
    "argv", [["upgrade", "--dry-run"], ["--json", "upgrade"]], ids=["dry-run", "json"]
)
def test_the_plan_modes_neither_ask_nor_run_even_at_a_terminal(
    runner: CliRunner,
    tool: Tool,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    """Off a terminal every path without --yes ends as a plan, which would hide a
    plan mode that fell through; at a terminal, with every question answered yes,
    only the plan modes themselves keep uv from running."""
    monkeypatch.setattr(install_cli, "_stdin_is_a_terminal", lambda: True)
    asked: list[str] = []

    def confirm(text: str, **_: object) -> bool:
        asked.append(text)
        return True

    monkeypatch.setattr("aisquare.cli.install.typer.confirm", confirm)

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert asked == [] and machine.installs == []


def test_at_a_terminal_it_asks_and_no_means_nothing_runs(
    runner: CliRunner, tool: Tool, machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install_cli, "_stdin_is_a_terminal", lambda: True)
    asked: list[tuple[str, bool]] = []

    def confirm(text: str, *, default: bool = True, **_: object) -> bool:
        asked.append((text, default))
        return False

    monkeypatch.setattr("aisquare.cli.install.typer.confirm", confirm)

    declined = runner.invoke(app, ["upgrade"])

    assert declined.exit_code == 0, declined.output
    assert asked == [("Upgrade aisquare 0.9.0 → 0.9.1?", False)], "the default is NOT to do it"
    assert "nothing changed" in declined.stdout
    assert machine.installs == []

    monkeypatch.setattr("aisquare.cli.install.typer.confirm", lambda *_a, **_k: True)
    agreed = runner.invoke(app, ["upgrade"])

    assert agreed.exit_code == 0, agreed.output
    assert len(machine.installs) == 1


def test_yes_runs_the_restated_command_checks_the_version_and_refreshes_the_hooks(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path
) -> None:
    site = _hooked(tmp_path / "claude", tool.script, foreign=True)
    _record(site)

    result = runner.invoke(app, ["upgrade", "--yes"])

    assert result.exit_code == 0, result.output
    [(argv, env, to_stderr)] = machine.installs
    assert argv == [
        "uv",
        "tool",
        "install",
        "--force",
        "--python",
        "3.14",
        "--with",
        "tiktoken",
        "aisquare-cli[serve]@latest",
    ]
    assert env["UV_TOOL_DIR"] == str(tool.prefix.parent)
    assert to_stderr is False, "a human sees uv's own output as it happens"
    assert machine.captured[0] == [str(tool.facts.executable), "-P", "-m", "aisquare", "--version"]
    assert machine.connects() == [
        [str(tool.script), "agents", "connect", "claude-code", "--config-dir", str(site)]
    ], "re-connected BY the new install's own script, so the hooks name it"
    assert "✓ aisquare 0.9.1 (was 0.9.0)" in result.stdout
    assert f"✓ hooks re-connected in {site}" in result.stdout


def test_under_json_the_report_is_one_object_and_uv_talks_on_stderr(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path
) -> None:
    site = _hooked(tmp_path / "claude", tool.script)
    _record(site)

    result = runner.invoke(app, ["--json", "upgrade", "--yes"])

    assert result.exit_code == 0, result.output
    report = _one_object(result.stdout)
    assert report["upgraded"] is True
    assert report["previous"] == "0.9.0" and report["version"] == "0.9.1"
    assert report["hooks"] == [{"config_dir": str(site), "refreshed": True, "error": None}]
    assert machine.installs[0][2] is True, "the installer's output must go to stderr"


def test_an_unchanged_version_after_success_is_the_silent_no_op_and_fails(
    runner: CliRunner, tool: Tool, machine: Machine
) -> None:
    machine.new_version = "0.9.0"

    result = runner.invoke(app, ["upgrade", "--yes"])

    assert result.exit_code == 1
    assert "still reports 0.9.0, not 0.9.1" in result.stderr
    assert "uv tool install --force" in result.stderr, "the fallback command is named"
    assert machine.connects() == [], "an unconfirmed upgrade refreshes no hooks"


def test_with_pypi_unreachable_an_unchanged_version_is_the_newest_the_index_has(
    runner: CliRunner, tool: Tool, machine: Machine
) -> None:
    machine.latest = LatestRelease(None, "could not reach PyPI (offline)")
    machine.new_version = "0.9.0"

    result = runner.invoke(app, ["upgrade", "--yes"])

    assert result.exit_code == 0, result.output
    assert machine.installs[0][0][-1] == "aisquare-cli[serve]@latest"
    assert "is the newest release your package index serves" in result.stdout


def test_a_pin_is_confirmed_against_the_pin_and_names_both_on_a_mismatch(
    runner: CliRunner, tool: Tool, machine: Machine
) -> None:
    machine.new_version = "0.7.0"
    rolled_back = runner.invoke(app, ["upgrade", "--version", "0.7", "--yes"])

    assert rolled_back.exit_code == 0, rolled_back.output
    assert machine.lookups == 0, "a pin needs no PyPI lookup"
    assert machine.installs[0][0][-1] == "aisquare-cli[serve]@0.7"

    machine.new_version = "0.9.0"
    mismatch = runner.invoke(app, ["upgrade", "--version", "0.7.0", "--yes"])

    assert mismatch.exit_code == 1
    assert "0.7.0 was asked for, but the new install reports 0.9.0" in mismatch.stderr


def test_a_failing_installer_names_the_fallback_and_checks_nothing(
    runner: CliRunner, tool: Tool, machine: Machine
) -> None:
    machine.installer_exit = 2

    result = runner.invoke(app, ["--json", "upgrade", "--yes"])

    assert result.exit_code == 1
    error = _one_object(result.stdout)
    assert error["error"] == "upgrade_failed"
    assert error["hint"].startswith("uv tool install --force")
    assert machine.captured == [], "no version check and no hook refresh after a failed install"


def test_nothing_runs_when_pypi_has_nothing_newer(
    runner: CliRunner, tool: Tool, machine: Machine
) -> None:
    machine.latest = LatestRelease("0.9.0")

    result = runner.invoke(app, ["upgrade", "--yes"])

    assert result.exit_code == 0, result.output
    assert "up to date" in result.stdout
    assert machine.installs == []


def test_a_build_ahead_of_pypi_is_not_moved_back_without_a_pin(
    runner: CliRunner, tool: Tool, machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "__version__", "0.10.0rc1")
    machine.latest = LatestRelease("0.9.1")

    result = runner.invoke(app, ["upgrade", "--yes"])

    assert result.exit_code == 0, result.output
    assert machine.installs == [], "0.9.1 is OLDER than 0.10.0rc1 — that is a downgrade"


def test_a_version_that_is_not_a_version_is_refused_before_anything(
    runner: CliRunner, machine: Machine
) -> None:
    result = runner.invoke(app, ["--json", "upgrade", "--version", "latest; rm -rf ~", "--yes"])

    assert result.exit_code == 1
    assert _one_object(result.stdout)["error"] == "invalid_version"
    assert machine.installs == [] and machine.lookups == 0


# --- which hook sites the new install re-connects --------------------------------------


def test_sites_running_another_install_are_left_and_named(tool: Tool, tmp_path: Path) -> None:
    other = tmp_path / "checkout" / ".venv" / "bin" / "aisquare"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    ours = _hooked(tmp_path / "c-ours", tool.script)
    theirs = _hooked(tmp_path / "c-theirs", other)
    _record(ours, theirs)

    refresh, left = lifecycle.refresh_sites(tool.facts)

    assert [site.config_dir for site in refresh] == [ours]
    assert [site.config_dir for site in left] == [theirs]
    assert left[0].reason is not None and str(other) in left[0].reason


def test_a_site_whose_program_is_gone_is_refreshed_not_left(tool: Tool, tmp_path: Path) -> None:
    """Negative control for the one above: a missing program is no other install —
    those hooks fail every session, and re-connecting is the fix."""
    gone = _hooked(tmp_path / "c-gone", tmp_path / "deleted" / "aisquare")
    _record(gone)

    refresh, left = lifecycle.refresh_sites(tool.facts)

    assert [site.config_dir for site in refresh] == [gone]
    assert left == ()


def test_a_link_into_this_environment_is_this_install(tool: Tool, tmp_path: Path) -> None:
    """The installer's hooks may name ``~/.local/bin/aisquare``, a link into the tool."""
    if sys.platform == "win32":
        pytest.skip("uv copies its shims on Windows rather than linking them")
    link = tmp_path / "local-bin" / "aisquare"
    link.parent.mkdir()
    link.symlink_to(tool.script)
    site = _hooked(tmp_path / "c-link", link)
    _record(site)

    refresh, left = lifecycle.refresh_sites(tool.facts)

    assert [s.config_dir for s in refresh] == [site] and left == ()


def test_a_recorded_site_with_no_aisquare_hooks_is_not_given_any(
    tool: Tool, tmp_path: Path
) -> None:
    bare = tmp_path / "c-bare"
    bare.mkdir()
    (bare / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    _record(bare)

    refresh, left = lifecycle.refresh_sites(tool.facts)

    assert refresh == () and left == ()


def test_a_site_that_fails_to_reconnect_is_named_with_its_command_and_exits_1(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path
) -> None:
    site = _hooked(tmp_path / "claude", tool.script)
    _record(site)
    machine.connect_exit = 1

    result = runner.invoke(app, ["upgrade", "--yes"])

    assert result.exit_code == 1
    assert "✓ aisquare 0.9.1 (was 0.9.0)" in result.stdout, "the upgrade itself succeeded"
    assert "claude-code is not installed on this machine" in result.stdout
    assert f"aisquare agents connect claude-code --config-dir {site}" in result.stdout


@pytest.fixture
def unreadable_settings(tmp_path: Path) -> Iterator[Path]:
    if sys.platform == "win32" or not can_deny_reads():
        pytest.skip("needs a settings.json this user cannot read")
    site = _hooked(tmp_path / "c-locked", "/nowhere/aisquare")
    settings = site / "settings.json"
    settings.chmod(0)
    try:
        yield site
    finally:
        settings.chmod(0o600)


def test_a_site_whose_settings_cannot_be_read_is_left_with_the_reason(
    tool: Tool, unreadable_settings: Path
) -> None:
    _record(unreadable_settings)

    refresh, left = lifecycle.refresh_sites(tool.facts)

    assert refresh == ()
    assert [site.config_dir for site in left] == [unreadable_settings]
    assert left[0].reason is not None and "could not be read" in left[0].reason
