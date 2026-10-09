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
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import URLError

import pytest
from typer.testing import CliRunner

from aisquare.cli import install as install_cli
from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import paths, spawn
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.core.version import DISTRIBUTION
from aisquare.models import FleetAgent
from aisquare.services import agents as agents_service
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


#: What uv writes as ``CACHEDIR.TAG`` into every cache it makes (``~/.cache/uv``, measured).
UV_CACHEDIR_TAG = "Signature: 8a477f597d28d172789f06886806bc55"


def _uv_cache(root: Path) -> Path:
    """A stand-in for uv's cache at ``root``, tagged as uv tags its own."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "CACHEDIR.TAG").write_text(UV_CACHEDIR_TAG, encoding="utf-8")
    return root


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


def test_homebrews_own_python_is_not_a_formula_install(tmp_path: Path) -> None:
    """A pip install into Homebrew's Python sits under a Cellar too, but it is no
    formula's venv: its prefix IS its base (review of #251, finding 4)."""
    prefix = (
        tmp_path
        / "opt"
        / "Cellar"
        / "python@3.13"
        / "3.13.5"
        / "Frameworks"
        / "Python.framework"
        / "Versions"
        / "3.13"
    )
    prefix.mkdir(parents=True)

    route = install_route.classify(_facts(prefix, base_prefix=prefix, user_install=True))

    assert route.kind == install_route.SYSTEM
    assert install_route.upgrade_argv(route)[:2] != ["brew", "upgrade"]


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
    # The reinstall, not only a pull: the version and dependencies live in the
    # install's metadata (review of #251, finding 7).
    assert install_route.upgrade_argv(route) == [
        str(route.facts.executable),
        "-m",
        "pip",
        "install",
        "-e",
        str(checkout),
    ]
    reason = install_route.not_automated(route)
    assert reason is not None
    assert install_route.command_line(["git", "-C", str(checkout), "pull"]) in reason


def test_an_editable_install_on_windows_is_still_told_to_pull_first(tmp_path: Path) -> None:
    """The same advice on every platform; Windows only adds WHEN the reinstall runs
    (a Windows-only CI failure on #251's round 1: the lock branch came first)."""
    checkout = tmp_path / "checkout"
    url = json.dumps({"url": checkout.as_uri(), "dir_info": {"editable": True}})
    windows = install_route.classify(_facts(_prefix(tmp_path), direct_url=url, platform="win32"))

    reason = install_route.not_automated(windows)

    assert reason is not None
    assert install_route.command_line(["git", "-C", str(checkout), "pull"]) in reason
    assert "after aisquare exits" in reason


def test_an_editable_install_in_a_uv_made_venv_reinstalls_with_uv_pip(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    url = json.dumps({"url": checkout.as_uri(), "dir_info": {"editable": True}})

    route = install_route.classify(_facts(_prefix(tmp_path), direct_url=url, installer="uv"))

    argv = install_route.upgrade_argv(route)
    assert argv[:3] == ["uv", "pip", "install"]
    assert argv[-2:] == ["-e", str(checkout)]


def test_the_same_url_without_editable_is_a_local_source(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    url = json.dumps({"url": checkout.as_uri(), "dir_info": {}})

    route = install_route.classify(_facts(_prefix(tmp_path), direct_url=url))

    assert route.kind == install_route.LOCAL_SOURCE
    assert install_route.upgrade_argv(route)[-1] == str(checkout)


@pytest.mark.parametrize(
    ("vcs_info", "subdirectory", "source"),
    [
        (
            {"vcs": "git", "commit_id": "0123abcd", "requested_revision": "main"},
            None,
            "aisquare-cli @ git+https://github.com/AISquare-Studio/aisquare-cli.git@main",
        ),
        (
            {"vcs": "git", "commit_id": "0123abcd"},
            "cli",
            "aisquare-cli @ git+https://github.com/AISquare-Studio/aisquare-cli.git"
            "#subdirectory=cli",
        ),
        (
            {"vcs": "git", "commit_id": "0123abcd", "requested_revision": "v0.8.0"},
            None,
            "aisquare-cli @ git+https://github.com/AISquare-Studio/aisquare-cli.git",
        ),
        (
            {"vcs": "git", "commit_id": "0123abcd", "requested_revision": "0123abcd"},
            None,
            "aisquare-cli @ git+https://github.com/AISquare-Studio/aisquare-cli.git",
        ),
    ],
    ids=["branch", "subdirectory", "release-tag", "commit"],
)
def test_a_vcs_install_is_reinstalled_from_a_reference_pip_can_read(
    tmp_path: Path, vcs_info: dict[str, str], subdirectory: str | None, source: str
) -> None:
    """PEP 610 records the URL without ``git+``; pip reads a bare https URL as an
    archive to download, so the reference is rebuilt (review of #251, finding 5)."""
    record: dict[str, Any] = {
        "url": "https://github.com/AISquare-Studio/aisquare-cli.git",
        "vcs_info": vcs_info,
    }
    if subdirectory:
        record["subdirectory"] = subdirectory

    route = install_route.classify(_facts(_prefix(tmp_path), direct_url=json.dumps(record)))

    assert route.kind == install_route.LOCAL_SOURCE
    assert route.source == source
    assert install_route.upgrade_argv(route)[-1] == source


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


def test_the_seam_registry_names_the_command_the_new_install_runs() -> None:
    """The registry is where someone learns what the seam starts. It said ``agents
    connect`` after #251's review moved the refresh off it (review of #257)."""
    runs = " ".join(lifecycle.REFRESH_HOOKS[:2])
    ruling = spawn.SEAMS["aisquare/services/install_route.py::run_captured"].reason
    listed = (spawn.__doc__ or "").split("``services/install_route.py::run_captured``")[1]
    listed = listed.split("  * ")[0]

    assert runs == "agents refresh-hooks"
    assert f"`{runs}`" in ruling and "agents connect" not in ruling, ruling
    assert f"``{runs}``" in listed and "agents connect" not in listed, listed


def test_the_reference_page_lists_the_hooks_connect_writes() -> None:
    """docs/reference.md said five hooks and had no StopFailure row, while `agents connect`
    writes six (review of #257): its count and its table follow `_HOOKS`."""
    page = (Path(__file__).resolve().parents[1] / "docs" / "reference.md").read_text("utf-8")
    events = [event for event, _ in agent_core._HOOKS]
    words = {5: "five", 6: "six", 7: "seven", 8: "eight"}
    table = page.split("| Hook | What it does |", 1)[1].split("\n\n", 1)[0]
    rows = re.findall(r"^\| `(\w+)` \|", table, flags=re.MULTILINE)

    assert f"writes {words[len(events)]} hooks" in page, "the count follows _HOOKS"
    assert sorted(rows) == sorted(events), (rows, events)
    assert "PreToolUse" not in rows, "control: an event aisquare does not hook is not listed"


def test_contributing_names_the_security_md_section_that_exists() -> None:
    """CONTRIBUTING's rule named two SECURITY.md sections that never existed, so a
    contributor following it looked for headings that are not there (review of #257)."""
    root = Path(__file__).resolve().parents[1]
    rule = " ".join((root / "CONTRIBUTING.md").read_text(encoding="utf-8").split())
    security = (root / "SECURITY.md").read_text(encoding="utf-8")
    headings = {line[3:].strip() for line in security.splitlines() if line.startswith("## ")}
    named = re.search(
        r"updates SECURITY\.md's \"([^\"]+)\" \(its (\w+), (\w+) or (\w+) list\)", rule
    )

    assert named is not None, "the rule no longer names a SECURITY.md section"
    assert named.group(1) in headings, (named.group(1), headings)
    assert "What leaves your machine" not in headings, "control: a name that was never there"
    labels = named.groups()[1:]
    assert all(f"**{label}" in security for label in labels), labels


def test_security_md_lists_what_upgrade_and_the_plugin_change_and_send() -> None:
    """SECURITY.md is an inventory that says only what the code does, and it predated
    upgrade's hook rewrite, its PyPI requests and the plugin's uvx (review of #257). The
    launcher's uvx asks for a Python CI tests, which uv downloads from Astral when none
    is installed, and the plugin's item named only PyPI (a later review of #257)."""
    root = Path(__file__).resolve().parents[1]
    text = (root / "SECURITY.md").read_text(encoding="utf-8")
    changed = text.split("**Changed in Claude Code's settings:**")[1].split("**Sent:**")[0]
    sent = text.split("**Sent:**")[1]
    plugin = sent.split("**The Claude Code plugin.**")[1].split("\n- **")[0]
    launcher = (root / "plugins" / "claude-code" / "scripts" / "aisquare-hook").read_text("utf-8")

    assert (
        "`aisquare upgrade`" in changed
        and f"`aisquare {' '.join(lifecycle.REFRESH_HOOKS[:2])}`" in changed
    )
    assert install_route.PYPI_JSON_URL in sent and "User-Agent" in sent
    assert '--from "$_from" aisquare hook' in launcher, "the launcher still runs uvx --from"
    assert f"uvx --from {DISTRIBUTION}==" in plugin
    assert "--python '>=3.11,<3.14'" in launcher, "the launcher still asks uv for a Python"
    assert "CPython" in plugin and "UV_PYTHON_DOWNLOADS" in plugin, plugin
    assert "`aisquare uninstall`" in changed and "`--purge`" in changed, "uninstall too"
    assert "Keychain" in changed, "a purge leaves the macOS slots' tokens, and says so"


# --- the uv receipt, restated ----------------------------------------------------------


def _uv_route(tmp_path: Path, receipt: str, **facts: Any) -> install_route.InstallRoute:
    route = install_route.classify(_facts(_prefix(tmp_path, receipt), **facts))
    assert route.receipt is not None, "the fixture built no uv route"
    return route


def test_the_command_restates_the_receipt_and_replaces_the_pin(tmp_path: Path) -> None:
    route = _uv_route(tmp_path, _receipt(_OURS_PINNED, _TIKTOKEN))

    latest = install_route.upgrade_argv(route, current="0.9.0")
    pinned = install_route.upgrade_argv(route, "0.9.1", current="0.9.0")

    assert latest == [
        "uv",
        "tool",
        "install",
        "--force",
        "--python",
        "3.14",
        "--with",
        "tiktoken",
        "--refresh-package",
        "aisquare-cli",
        "aisquare-cli[serve]>=0.9.0",
    ]
    assert pinned[-1] == "aisquare-cli[serve]@0.9.1"
    assert not any("==0.6.0" in part for part in latest + pinned), "the old pin must go"


@pytest.mark.parametrize(
    ("current", "spec"),
    [
        ("0.8.0", "aisquare-cli>=0.8.0"),
        ("1.0.0rc1", "aisquare-cli>=1.0.0rc1"),
        ("0.8.1.dev3+g1a2b3c", "aisquare-cli>=0.8.1.dev3"),
        ("not-a-version", "aisquare-cli@latest"),
    ],
    ids=["release", "pre-release", "local-label", "no-version"],
)
def test_the_latest_is_asked_for_no_older_than_the_running_release(
    tmp_path: Path, current: str, spec: str
) -> None:
    """With `@latest`, a cutoff in uv's settings (uv.toml, UV_EXCLUDE_NEWER) or an index that
    is behind took uv to a release OLDER than the one running, which replaced it; the way back
    failed under the same cutoff (measured, uv 0.12.19; sweep of #257). Asked for the running
    release or newer, uv fails to resolve instead, before it touches the environment
    (measured). The refresh is the one `@latest` implied. `>=` takes no local label."""
    route = _uv_route(tmp_path, _receipt('{ name = "aisquare-cli" }'))

    argv = install_route.upgrade_argv(route, current=current)

    assert argv[-1] == spec, argv
    floored = spec != "aisquare-cli@latest"
    assert (argv[-3:-1] == ["--refresh-package", "aisquare-cli"]) is floored, argv


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

    argv = install_route.upgrade_argv(route, current="0.9.0")

    withs = [argv[i + 1] for i, part in enumerate(argv) if part == "--with"]
    assert withs == ["tiktoken>=0.7", "truststore; sys_platform == 'linux'", "rich[jupyter]"]
    assert argv[-1] == "aisquare-cli>=0.9.0", "no extras recorded, none invented"


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


#: A cooldown as uv 0.12.19 records it, whether it came from `exclude-newer = "7 days"` in
#: uv.toml, UV_EXCLUDE_NEWER or `--exclude-newer P7D` (measured in a scratch HOME): the
#: span, and the cutoff uv worked out from it at install time.
_COOLDOWN = (
    "\n[tool.options]\n"
    'exclude-newer = "2026-10-01T16:23:50.494898703Z"\n'
    'exclude-newer-span = "P7D"\n'
)
#: A fixed date given any of those three ways: the date alone.
_FIXED_CUTOFF = '\n[tool.options]\nexclude-newer = "2026-10-01T00:00:00Z"\n'


def test_a_cooldown_is_restated_as_its_span_not_the_cutoff_uv_worked_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The span was refused, and the command printed instead restated the cutoff: that froze
    the cooldown, so no later release could be installed, and the receipt it wrote had no
    span left (measured, uv 0.12.19; sweep of #257). A fixed date is restated as it is."""
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    cooldown = _uv_route(tmp_path / "span", _receipt(_OURS_PINNED, tail=_COOLDOWN))
    fixed = _uv_route(tmp_path / "date", _receipt(_OURS_PINNED, tail=_FIXED_CUTOFF))

    argv = install_route.upgrade_argv(cooldown)
    dated = install_route.upgrade_argv(fixed)

    assert argv[argv.index("--exclude-newer") + 1] == "P7D", argv
    assert argv.count("--exclude-newer") == 1 and "2026-10-01T16:23:50.494898703Z" not in argv
    assert install_route.not_automated(cooldown) is None, "the span is restated, not refused"
    assert dated[dated.index("--exclude-newer") + 1] == "2026-10-01T00:00:00Z", dated
    assert install_route.not_automated(fixed) is None


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
    ("requirement", "kind", "tail"),
    [
        (
            '{ name = "aisquare-cli", editable = "/src/aisquare-cli" }',
            install_route.EDITABLE,
            ["-e", "/src/aisquare-cli"],
        ),
        (
            '{ name = "aisquare-cli", extras = ["serve"], directory = "/src/aisquare-cli" }',
            install_route.LOCAL_SOURCE,
            ["/src/aisquare-cli[serve]"],
        ),
        (
            '{ name = "aisquare-cli", git = "https://github.com/o/r?rev=v0.7.0" }',
            install_route.LOCAL_SOURCE,
            ["aisquare-cli @ git+https://github.com/o/r"],
        ),
        (
            '{ name = "aisquare-cli", git = "https://github.com/o/r?branch=dev#0123abcd" }',
            install_route.LOCAL_SOURCE,
            ["aisquare-cli @ git+https://github.com/o/r@dev"],
        ),
        (
            '{ name = "aisquare-cli", git = "https://g.example/r?subdirectory=cli&rev=v1#01" }',
            install_route.LOCAL_SOURCE,
            ["aisquare-cli @ git+https://g.example/r#subdirectory=cli"],
        ),
        (
            '{ name = "aisquare-cli", git = "https://github.com/o/r?rev=rc%2Ffirst-run" }',
            install_route.LOCAL_SOURCE,
            ["aisquare-cli @ git+https://github.com/o/r@rc/first-run"],
        ),
        (
            '{ name = "aisquare-cli", git = "https://github.com/o/r?rev=dev" }',
            install_route.LOCAL_SOURCE,
            ["aisquare-cli @ git+https://github.com/o/r@dev"],
        ),
        (
            '{ name = "aisquare-cli", git = "https://github.com/o/r?rev=0123abcd9876" }',
            install_route.LOCAL_SOURCE,
            ["aisquare-cli @ git+https://github.com/o/r"],
        ),
        (
            '{ name = "aisquare-cli", url = "https://x.example/a.tar.gz", subdirectory = "cli" }',
            install_route.LOCAL_SOURCE,
            ["aisquare-cli @ https://x.example/a.tar.gz#subdirectory=cli"],
        ),
    ],
    ids=[
        "editable",
        "directory",
        "git-rev-release-tag",
        "git-branch",
        "git-subdirectory",
        "git-rev-branch-encoded",
        "git-rev-branch",
        "git-rev-commit",
        "url-subdirectory",
    ],
)
def test_a_receipt_from_a_source_is_reported_with_its_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requirement: str, kind: str, tail: list[str]
) -> None:
    """A branch is kept (its head is the upgrade), whether uv recorded it as `branch=` or,
    for a `@<ref>`, as `rev=`, URL-encoded (review of #257); a release tag, a commit and
    the commit pin are dropped; a subdirectory survives (review of #251, finding 6)."""
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    route = _uv_route(tmp_path, _receipt(requirement))

    argv = install_route.upgrade_argv(route)

    assert route.kind == kind
    assert install_route.not_automated(route) is not None, "a source install is never run"
    assert argv[:4] == ["uv", "tool", "install", "--force"]
    assert argv[-len(tail) :] == tail


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


def test_a_command_printed_on_windows_pastes_into_cmd_and_powershell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows never runs the uv route, so the printed line is the only way to upgrade. It
    was quoted for the C runtime alone: `--with tiktoken>=0.7` came out bare, and cmd read
    `>=0.7` as a redirection, dropping the constraint (sweep of #257). Both shells read a
    double-quoted argument literally; one with nothing to protect stays bare."""
    receipt = _receipt(
        '{ name = "aisquare-cli", extras = ["mcp", "serve"] }',
        '{ name = "tiktoken", specifier = ">=0.7" }',
        '{ name = "rich", specifier = "<15" }',
        tail='\n[tool.options]\nindex-url = "https://x.example/simple?project=a&token=b"\n',
    )
    argv = install_route.upgrade_argv(
        _uv_route(tmp_path / "w", receipt, platform="win32"), current="0.8.0"
    )
    plain = install_route.upgrade_argv(
        _uv_route(tmp_path / "p", _receipt(_OURS_PINNED, _TIKTOKEN), platform="win32"),
        "0.8.1",
        current="0.8.0",
    )
    with monkeypatch.context() as windows:
        windows.setattr(sys, "platform", "win32")
        line = install_route.command_line(argv)
        bare = install_route.command_line(plain)
    with monkeypatch.context() as posix:
        posix.setattr(sys, "platform", "linux")
        posix_line = install_route.command_line(argv)

    assert line == (
        'uv tool install --force --python 3.14 --with "tiktoken>=0.7" --with "rich<15" '
        '--index-url "https://x.example/simple?project=a&token=b" '
        '--refresh-package aisquare-cli "aisquare-cli[mcp,serve]>=0.8.0"'
    ), line
    outside_quotes = re.sub(r'"[^"]*"', "", line)
    assert not re.search(r"[<>|&^,;]", outside_quotes), outside_quotes
    assert bare == "uv tool install --force --python 3.14 --with tiktoken aisquare-cli[serve]@0.8.1"
    assert posix_line == (
        "uv tool install --force --python 3.14 --with 'tiktoken>=0.7' --with 'rich<15' "
        "--index-url 'https://x.example/simple?project=a&token=b' "
        "--refresh-package aisquare-cli 'aisquare-cli[mcp,serve]>=0.8.0'"
    ), "control: POSIX keeps shlex's quoting"


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
    assert agent_core.version_in("aisquare 0.9.1\n") == "0.9.1"
    assert agent_core.version_in("Traceback (most recent call last):\n") is None


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


#: PyPI's JSON when a pre-release is newer than the newest final: ``info.version`` stays on
#: the final (measured: celery 5.6.3 beside 5.7.0b1, kombu 5.6.2 beside 5.7.0b1).
_PRE_RELEASED = {
    "info": {"version": "0.9.0"},
    "releases": {
        "0.4.0rc1": [{"yanked": False}],
        "0.9.0": [{"yanked": False}],
        "1.0.0rc1": [{"yanked": True}, {"yanked": False}],
        "1.0.0rc2": [{"yanked": True}],
        "1.1.0": [],
    },
}


def test_the_lookup_counts_pre_releases_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """For an install whose upgrade takes pre-releases, the newest is read from every release
    listed, as uv picks: a release whose every file is yanked, or that has none, is not one."""
    body = json.dumps(_PRE_RELEASED).encode()
    monkeypatch.setattr(install_route, "open_url", lambda _request, timeout: _Response(body))

    assert _REAL_FETCH_LATEST() == LatestRelease("0.9.0")
    assert _REAL_FETCH_LATEST(prereleases=True) == LatestRelease("1.0.0rc1")


@pytest.mark.parametrize(
    ("option", "current", "takes"),
    [
        ('prerelease = "allow"', "0.9.0", True),
        (None, "0.9.0", False),
        (None, "1.0.0rc1", True),
        (None, "1.1.0.dev1", True),
        ('prerelease = "if-necessary"', "1.0.0rc1", True),
        ('prerelease = "disallow"', "1.0.0rc1", False),
        ('prerelease = "explicit"', "0.9.0", False),
    ],
    ids=["allow", "a-final", "an-rc", "a-dev", "if-necessary-rc", "disallow-rc", "explicit"],
)
def test_an_upgrade_takes_pre_releases_where_uv_does(
    tmp_path: Path, option: str | None, current: str, takes: bool
) -> None:
    """Measured with uv 0.12.19 resolving `demo-tool>=<current>` among 0.9.0, 1.0.0rc1,
    1.0.0rc2, 1.1.0.dev1 and 1.1.0.dev2: `--prerelease allow` takes the newest of them all;
    a pre-release floor takes them under every mode but disallow, if-necessary included."""
    tail = f"\n[tool.options]\n{option}\n" if option else ""
    route = _uv_route(tmp_path, _receipt('{ name = "aisquare-cli" }', tail=tail))
    pipx = install_route.InstallRoute(install_route.PIPX, route.facts)

    assert install_route.takes_prereleases(route, current) is takes
    assert install_route.takes_prereleases(pipx, current) is False, "pipx upgrade takes none"


# --- the run ---------------------------------------------------------------------------


@dataclass
class Machine:
    """The outside world as one fake: PyPI, uv, and the new install's answers."""

    latest: LatestRelease = field(default_factory=lambda: LatestRelease("0.9.1"))
    installer_exit: int = 0
    new_version: str = "0.9.1"
    connect_exit: int = 0
    connect_stderr: str = "✗ claude-code is not installed on this machine\n"
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

    def fetch_latest(timeout: float = 5.0, *, prereleases: bool = False) -> LatestRelease:
        # `prereleases` changes what the REAL lookup reads; its tests drive it through open_url.
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
            return Captured(world.connect_exit, "", world.connect_stderr)
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
    assert "git -C " in result.stderr and " pull" in result.stderr, "pull the checkout first"
    assert "Upgrade it with: " in result.stderr and " -e " in result.stderr, "then reinstall"
    assert machine.installs == [] and machine.lookups == 0, "refused before PyPI or uv"


def test_a_refused_route_under_json_is_one_error_object(
    runner: CliRunner, machine: Machine
) -> None:
    result = runner.invoke(app, ["--json", "upgrade", "--yes"])

    assert result.exit_code == 1
    error = _one_object(result.stdout)
    assert error["error"] == "upgrade_unsupported_route"
    assert " install " in error["hint"] and " -e " in error["hint"]
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


@pytest.mark.parametrize("latest", ["0.8.1", "0.8.0"], ids=["update", "up-to-date"])
@pytest.mark.parametrize("flags", [[], ["--dry-run"]], ids=["no-yes", "dry-run"])
def test_json_without_yes_prints_the_plan_and_changes_nothing(
    runner: CliRunner,
    tool: Tool,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
    latest: str,
    flags: list[str],
) -> None:
    """Up to date, the plan said "dry_run": false, so a script read a plan as a run
    (sweep of #257)."""
    monkeypatch.setattr(lifecycle, "__version__", "0.8.0")
    machine.latest = LatestRelease(latest)

    result = runner.invoke(app, ["--json", "upgrade", *flags])

    assert result.exit_code == 0, result.output
    plan = _one_object(result.stdout)
    assert plan["dry_run"] is True
    assert plan["update_available"] is (latest == "0.8.1"), plan
    assert plan["argv"][-1] == "aisquare-cli[serve]>=0.8.0"
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


def test_a_site_left_for_its_settings_file_is_not_told_its_hooks_were_removed(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path
) -> None:
    """The reason came from uninstall: "so they cannot be removed safely", in a plan that
    removes nothing (review of #257)."""
    site = _hooked(tmp_path / "claude", tool.script)
    text = (site / "settings.json").read_text(encoding="utf-8")
    (site / "settings.json").write_text(text[: text.rindex("\n}")] + ",\n}", encoding="utf-8")
    _record(site)

    result = runner.invoke(app, ["--json", "upgrade", "--check"])

    left = _one_object(result.stdout)["hooks_left"]
    assert [entry["config_dir"] for entry in left] == [str(site)], left
    assert left[0]["reason"].endswith("so aisquare cannot rewrite it safely"), left
    assert "removed" not in left[0]["reason"], left


def test_a_settings_json_this_user_may_not_write_is_left_not_promised(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path
) -> None:
    """refresh-hooks refuses a settings.json it may not write, so the planned refresh failed
    after a good install: exit 1, asq's Update not reopened, and a remedy that failed the
    same way (review of #257)."""
    site = _hooked(tmp_path / "claude", tool.script)
    _record(site)
    settings_path = site / "settings.json"
    settings_path.chmod(0o444)
    if os.access(settings_path, os.W_OK):
        settings_path.chmod(0o644)
        pytest.skip("this user can write a read-only file (root)")
    try:
        plan = _one_object(runner.invoke(app, ["--json", "upgrade"]).stdout)
        result = runner.invoke(app, ["--json", "upgrade", "--yes"])
    finally:
        settings_path.chmod(0o644)

    assert plan["refresh_hooks"] == [], plan
    left = plan["hooks_left"]
    assert [entry["config_dir"] for entry in left] == [str(site)], left
    assert "its settings.json cannot be rewritten" in left[0]["reason"], left
    assert result.exit_code == 0, result.output
    assert machine.connects() == [], "no refresh was run for it"


def test_a_hook_naming_a_program_this_user_cannot_reach_counts_as_gone(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path
) -> None:
    """Path.exists raised PermissionError on 3.11 to 3.13 for a hook's program in a directory
    this user cannot enter (another user's ~/.local/bin), so upgrade, --check and asq's
    Update ended in a traceback (review of #257). Such a program is gone, and its hooks
    fail every session: re-connecting is the fix."""
    if sys.platform == "win32" or not can_deny_reads():
        pytest.skip("needs a directory this user cannot enter")
    locked = tmp_path / "someone-else"
    program = locked / ".local" / "bin" / "aisquare"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    site = _hooked(tmp_path / "claude", program)
    _record(site)
    locked.chmod(0)
    try:
        result = runner.invoke(app, ["--json", "upgrade", "--check"])
    finally:
        locked.chmod(0o700)

    assert result.exit_code == 0, result.output
    assert _one_object(result.stdout)["refresh_hooks"] == [str(site)], result.stdout


def test_a_recorded_config_dir_this_user_cannot_enter_is_left_with_its_reason(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path
) -> None:
    """exists() before the read raised PermissionError on 3.11 to 3.13, so `upgrade --check`
    and asq's Update failed outright; on 3.14 the directory passes as one with no hooks
    (review of #257)."""
    if sys.platform == "win32" or not can_deny_reads():
        pytest.skip("needs a directory this user cannot enter")
    site = _hooked(tmp_path / "claude-old", tool.script)
    _record(site)
    site.chmod(0)
    try:
        result = runner.invoke(app, ["--json", "upgrade", "--check"])
    finally:
        site.chmod(0o700)

    assert result.exit_code == 0, result.output
    left = _one_object(result.stdout)["hooks_left"]
    assert [entry["config_dir"] for entry in left] == [str(site)], left
    assert "its settings.json could not be read" in left[0]["reason"], left


@pytest.mark.parametrize(
    "program",
    ["~aisquare-no-such-user/.local/bin/aisquare", "~.local/bin/aisquare"],
    ids=["another-users-home", "a-typo"],
)
def test_a_hook_naming_a_home_this_machine_lacks_counts_as_gone(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path, program: str
) -> None:
    """``~olduser/…`` (dotfiles from another machine) or ``~.local/…`` names a home pathlib
    cannot find, and it raised RuntimeError on every Python: upgrade, --check and asq's
    Update ended in a traceback with nothing on stdout (sweep of #257). Those hooks fail
    every session, so re-connecting is the fix."""
    site = _hooked(tmp_path / "claude", program)
    _record(site)

    result = runner.invoke(app, ["--json", "upgrade", "--check"])

    assert result.exit_code == 0, result.output
    assert _one_object(result.stdout)["refresh_hooks"] == [str(site)], result.stdout
    assert agent_core.hook_binary(f"{program} hook stop") is not None


#: A home no machine running this suite has: pathlib raises RuntimeError expanding it.
_NO_SUCH_HOME = "~aisquare-no-such-user"


def _dir_pathlib_raises_on(shape: str, tmp_path: Path) -> Path:
    """A Claude Code directory pathlib raises RuntimeError on, not OSError: a symlink loop
    (on 3.11 and 3.12), or one spelled ``~olduser/.claude`` for a user this machine lacks."""
    if sys.platform == "win32":
        pytest.skip("NTFS reports a link loop differently, and Windows guesses a user's home")
    if shape == "symlink-loop":
        loop = tmp_path / "claude-old"
        loop.symlink_to(loop)
        return loop
    return Path(_NO_SUCH_HOME) / ".claude"


def _json_document(result: Any) -> Any:
    """The one JSON document a --json command printed (an object or a list), raised past
    nothing: an exception other than the command's own exit fails the test."""
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"--json must print exactly one document, got: {result.stdout!r}"
    return json.loads(lines[0])


@pytest.mark.parametrize("shape", ["symlink-loop", "another-users-home"])
def test_a_recorded_dir_pathlib_raises_on_does_not_stop_upgrade_reading_the_rest(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path, shape: str
) -> None:
    """A recorded ~/.claude-old linked to itself, or one a hand-edited agents.json spells
    ``~olduser/.claude``, ended upgrade, --check and asq's Update in a traceback, and the
    good directory after it was never reached (sweep of #257). The loop is left with its
    reason; the ``~olduser`` one is a directory this machine does not have, read as a
    removed one is: nothing to rewrite there."""
    bad = _dir_pathlib_raises_on(shape, tmp_path)
    good = _hooked(tmp_path / "claude", tool.script)
    _record(bad, good)

    result = runner.invoke(app, ["--json", "upgrade", "--check"])

    assert result.exit_code == 0, result.output
    plan = _one_object(result.stdout)
    assert plan["refresh_hooks"] == [str(good)], plan
    left = [entry["config_dir"] for entry in plan["hooks_left"]]
    assert left == ([str(bad)] if shape == "symlink-loop" else []), plan
    assert all("its settings.json could not be read" in e["reason"] for e in plan["hooks_left"])


@pytest.mark.parametrize(
    "command",
    [["uninstall", "--dry-run"], ["doctor"], ["status"], ["agents", "list"]],
    ids=["uninstall", "doctor", "status", "agents-list"],
)
@pytest.mark.parametrize("shape", ["symlink-loop", "another-users-home"])
def test_a_recorded_dir_pathlib_raises_on_does_not_end_the_commands_that_read_it(
    runner: CliRunner, tool: Tool, tmp_path: Path, shape: str, command: list[str]
) -> None:
    """The ``~olduser/.claude`` shape still ended uninstall, doctor (asq's Doctor page,
    where Update is), status and agents list in a traceback, with nothing on stdout under
    --json: `_claude_home` expanded it unguarded (review of #257's fixes). It now reads as
    a directory that does not exist, and the good one beside it is still read."""
    bad = _dir_pathlib_raises_on(shape, tmp_path)
    good = _hooked(tmp_path / "claude", tmp_path / "gone" / "aisquare")
    _record(bad, good)

    document = _json_document(runner.invoke(app, ["--json", *command]))

    assert document, document
    if command == ["agents", "list"]:
        [claude] = [agent for agent in document if agent["name"] == "claude-code"]
        sites = {site["config_dir"]: site for site in claude["sites"]}
        assert set(sites) == {str(bad), str(good)}, sites
        if shape == "another-users-home":
            assert sites[str(bad)]["refused"] == f"{bad} does not exist", sites


def test_a_hook_program_that_is_a_symlink_loop_beside_this_one_is_another_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doctor compares a hook's program with this install by resolving both when they
    share a directory, and resolve() raises RuntimeError on a loop on 3.11 and 3.12."""
    if sys.platform == "win32":
        pytest.skip("NTFS reports a link loop differently, and making one needs a privilege")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    loop = bin_dir / "aisquare-old"
    loop.symlink_to(loop)
    monkeypatch.setattr(agent_core, "current_install", lambda: bin_dir / "aisquare")

    assert agent_core._same_install(agent_core.HookBinary(loop)) is False


@pytest.mark.parametrize("shape", ["symlink-loop", "another-users-home"])
def test_a_claude_config_dir_pathlib_raises_on_is_read_as_absent_and_never_made(
    runner: CliRunner,
    tool: Tool,
    machine: Machine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """An exported ``CLAUDE_CONFIG_DIR=~olduser/.claude`` (quoted, so the shell left it), or
    one that is a symlink loop on 3.11/3.12, ended doctor, status, agents list, accounts list,
    uninstall and upgrade --check in a traceback. Read as written, the ``~olduser`` one is a
    directory that does not exist, and nothing may make it: with ``claude`` on PATH,
    `agents connect` took it for a Claude Code that has never started and wrote hooks into
    ``./~olduser/.claude`` in the cwd (sweep of #257)."""
    exported = _dir_pathlib_raises_on(shape, tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(exported))
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/usr/bin/claude")
    _record(_hooked(tmp_path / "claude", tmp_path / "gone" / "aisquare"))
    reads = [
        ["uninstall", "--dry-run"],
        ["doctor"],
        ["status"],
        ["agents", "list"],
        ["accounts", "list"],
        ["upgrade", "--check"],
    ]

    documents = [_json_document(runner.invoke(app, ["--json", *argv])) for argv in reads]
    connect = runner.invoke(app, ["agents", "connect", "claude-code"])

    assert all(documents), documents
    assert connect.exit_code == 1, connect.output
    assert connect.exception is None or isinstance(connect.exception, SystemExit), connect
    if shape == "another-users-home":
        assert "no such home on this machine" in connect.stderr, connect.stderr
    assert list(cwd.iterdir()) == [], "nothing is made in the cwd"


#: What the new install's own process wrote to a pipe, laid out by Rich at 80 columns.
_WRAPPED_TRACEBACK = (
    "╭───────────────────── Traceback (most recent call last) ──────────────────────╮\n"
    "│ /home/u/.local/share/uv/tools/aisquare-cli/lib/python3.13/site-packages/aisq │\n"
    "│ uare/core/agents.py:347 in install_hooks                                     │\n"
    "╰──────────────────────────────────────────────────────────────────────────────╯\n"
    "PermissionError: [Errno 13] Permission denied:\n"
    "'/home/u/.claude/settings.json'\n"
)
_USAGE_BOX = (
    "Usage: aisquare agents [OPTIONS] COMMAND [ARGS]...\n"
    "Try 'aisquare agents -h' for help.\n"
    "╭─ Error ──────────────────────────────────────────────────────────────────────╮\n"
    "│ No such command 'refresh-hooks'.                                             │\n"
    "╰──────────────────────────────────────────────────────────────────────────────╯\n"
)


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        (
            _WRAPPED_TRACEBACK,
            "PermissionError: [Errno 13] Permission denied: '/home/u/.claude/settings.json'",
        ),
        (_USAGE_BOX, "No such command 'refresh-hooks'."),
    ],
    ids=["wrapped-traceback", "usage-box"],
)
def test_a_site_that_fails_to_reconnect_gets_the_whole_reason(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path, stderr: str, reason: str
) -> None:
    """The reason was the last line of the child's stderr: the tail of a wrapped path
    (``n'``), or a usage box's bottom border (review of #257)."""
    site = _hooked(tmp_path / "claude", tool.script)
    _record(site)
    machine.connect_exit = 1
    machine.connect_stderr = stderr

    result = runner.invoke(app, ["--json", "upgrade", "--yes"])

    hooks = _one_object(result.stdout)["hooks"]
    assert hooks == [
        {"config_dir": str(site), "refreshed": False, "error": reason, "hooks_off": None}
    ], hooks


@pytest.mark.parametrize("entry", ["archive-v0/AbC123", "environments-v2/c5764179/56172ecc"])
def test_a_uvx_run_is_told_nothing_is_installed(
    runner: CliRunner, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """uvx runs aisquare from an entry in uv's cache. Read as a user's venv, upgrade and
    uninstall advised `uv pip` into the cache entry (review of #257)."""
    prefix = _uv_cache(tmp_path / "cache" / "uv") / entry
    (prefix / "bin").mkdir(parents=True)
    found = _facts(prefix, installer="uv")
    monkeypatch.setattr(install_route, "facts", lambda: found)
    venv = install_route.classify(_facts(tmp_path / "venvs" / "work", installer="uv"))

    route = install_route.classify(found)
    check = _one_object(runner.invoke(app, ["--json", "upgrade", "--check"]).stdout)
    package = _one_object(runner.invoke(app, ["--json", "uninstall"]).stdout)["package"]

    assert route.kind == install_route.UVX, route
    assert venv.kind == install_route.VENV, "control: a venv elsewhere is still a venv"
    assert check["route"] == "uvx" and check["command"] == "uv tool install aisquare-cli", check
    assert "nothing is installed to upgrade" in check["reason"], check
    assert package["command"] == "uv cache clean aisquare-cli" and not package["runs"], package
    assert "nothing is installed to remove" in package["reason"], package


@pytest.mark.parametrize(
    "where", ["UV_CACHE_DIR", "the platform default", "a tagged cache", "an archived checkout"]
)
def test_only_uvs_own_cache_makes_a_uvx_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """By the directory names alone, a venv in `~/Code/archive-v1/.venv` read as a uvx run:
    uninstall said nothing is installed, upgrade pointed at a second install, and session
    start named `uvx --from …` (review of #257). Only uv's own cache counts: the one
    UV_CACHE_DIR or the platform default names, or one carrying uv's CACHEDIR.TAG."""
    home = tmp_path / "home"
    for name in ("UV_CACHE_DIR", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    prefix = home / "Code" / "archive-v1" / ".venv"
    if where == "UV_CACHE_DIR":
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "elsewhere"))
        prefix = tmp_path / "elsewhere" / "archive-v0" / "AbC123"
    elif where == "the platform default":
        default = home / ("AppData/Local/uv/cache" if sys.platform == "win32" else ".cache/uv")
        prefix = default / "archive-v0" / "AbC123"
    elif where == "a tagged cache":
        prefix = _uv_cache(tmp_path / "moved-by-cache-dir") / "environments-v2" / "c57" / "561"
    (prefix / "bin").mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(prefix))

    route = install_route.classify(_facts(prefix, installer="pip"))

    in_uvs_cache = where != "an archived checkout"
    assert route.kind == (install_route.UVX if in_uvs_cache else install_route.VENV), route
    assert install_route.runs_from_uv_cache() is in_uvs_cache


def test_a_move_back_plans_no_reconnect_and_asks_to_move_back(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """upgrade() leaves the hooks alone on a move back, so the plan's "then: re-connects"
    and its --json promised a re-connect the run never made (review of #257)."""
    site = _hooked(tmp_path / "claude", tool.script)
    _record(site)
    monkeypatch.setattr(install_cli, "_stdin_is_a_terminal", lambda: True)
    asked: list[str] = []

    def confirm(text: str, **_: object) -> bool:
        asked.append(text)
        return False

    monkeypatch.setattr("aisquare.cli.install.typer.confirm", confirm)
    back = _one_object(runner.invoke(app, ["--json", "upgrade", "--version", "0.7.0"]).stdout)
    forward = _one_object(runner.invoke(app, ["--json", "upgrade", "--version", "0.9.1"]).stdout)
    runner.invoke(app, ["upgrade", "--version", "0.7.0"])
    runner.invoke(app, ["upgrade", "--version", "0.9.1"])

    assert back["refresh_hooks"] == [], back
    assert [entry["config_dir"] for entry in back["hooks_left"]] == [str(site)], back
    assert back["hooks_left"][0]["reason"].startswith("0.7.0 is older than 0.9.0"), back
    assert forward["refresh_hooks"] == [str(site)], "control: a move forward re-connects"
    assert asked == ["Move aisquare 0.9.0 back to 0.7.0?", "Upgrade aisquare 0.9.0 → 0.9.1?"]


def _live_fleet_agent(root: Path, label: str = "coder-1") -> None:
    """A fleet row the board lists as live, as `fleet spawn` records one."""
    root.mkdir(parents=True, exist_ok=True)
    project = team_project(root)
    with store_session() as store:
        store.ensure_project(project)
        store.upsert_fleet_agent(
            FleetAgent(
                id=f"agt_{label}",
                project_id=project.id,
                label=label,
                role="coder",
                pane_id="%1",
                cwd=project.root,
                created_at=datetime.now(tz=UTC),
            )
        )


def test_live_fleet_agents_are_named_before_the_install_is_replaced_under_them(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uninstall refuses while fleet agents run; upgrade never read the fleet, and a hook
    fired while uv recreates the environment fails, losing that turn's board update and
    the manager's wake-up (review of #257). Said in the plan, the question and under --yes."""
    monkeypatch.setattr(lifecycle, "_tmux_on_path", lambda: True)
    monkeypatch.setattr(install_cli, "_stdin_is_a_terminal", lambda: True)
    asked: list[str] = []

    def confirm(text: str, **_: object) -> bool:
        asked.append(text)
        return False

    monkeypatch.setattr("aisquare.cli.install.typer.confirm", confirm)
    quiet = runner.invoke(app, ["upgrade"])
    _live_fleet_agent(tmp_path / "repo")
    plan = _one_object(runner.invoke(app, ["--json", "upgrade"]).stdout)
    declined = runner.invoke(app, ["upgrade"])
    agreed = runner.invoke(app, ["upgrade", "--yes"])

    warning = "⚠ 1 fleet agent is running (coder-1 (repo)): a hook it fires while the install"
    assert plan["live_agents"] == ["coder-1 (repo)"], plan
    assert asked == [
        "Upgrade aisquare 0.9.0 → 0.9.1?",
        "Upgrade aisquare 0.9.0 → 0.9.1 while 1 fleet agent runs?",
    ]
    assert warning in declined.stdout and "aisquare fleet shutdown --all --yes" in declined.stdout
    assert warning in agreed.stdout and len(machine.installs) == 1, "--yes upgrades, having said it"
    assert "fleet agent" not in quiet.stdout, "control: no fleet, no warning"


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
        "--refresh-package",
        "aisquare-cli",
        "aisquare-cli[serve]>=0.9.0",
    ]
    assert env["UV_TOOL_DIR"] == str(tool.prefix.parent)
    assert to_stderr is False, "a human sees uv's own output as it happens"
    assert machine.captured[0] == [str(tool.facts.executable), "-P", "-m", "aisquare", "--version"]
    assert machine.connects() == [
        [str(tool.script), "agents", "refresh-hooks", "claude-code", "--config-dir", str(site)]
    ], "hooks only, BY the new install's own script, so the hooks name it"
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
    assert report["hooks"] == [
        {"config_dir": str(site), "refreshed": True, "error": None, "hooks_off": None}
    ]
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
    assert machine.installs[0][0][-1] == "aisquare-cli[serve]>=0.9.0"
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


@pytest.mark.parametrize(
    "answers", ["0.9.0", "0.9.1", None], ids=["the-release-that-ran", "another", "nothing"]
)
def test_a_failed_install_offers_a_reinstall_only_when_the_release_that_ran_is_gone(
    runner: CliRunner,
    tool: Tool,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
    answers: str | None,
) -> None:
    """uv resolves before it replaces anything (measured, uv 0.12.19), and the upgrade asks
    for the running release or newer, so a cutoff in uv's settings that excludes even that
    release fails with the install as it was. The advice was a reinstall from nothing, whose
    own `@latest` takes the same cutoff back to an older release (sweep of #257). Asked its
    version, only an install that no longer answers as the release that ran gets it."""
    machine.installer_exit = 2
    if answers is None:
        monkeypatch.setattr(
            install_route, "run_captured", lambda argv, *, timeout: Captured(None, error="gone")
        )
    else:
        machine.new_version = answers

    human = runner.invoke(app, ["upgrade", "--yes"])
    error = _one_object(runner.invoke(app, ["--json", "upgrade", "--yes"]).stdout)

    command = install_route.command_line(machine.installs[0][0])
    kept = answers == "0.9.0"
    assert human.exit_code == 1
    assert f"Run it again by hand: {command}" in human.stderr, human.stderr
    assert ("aisquare 0.9.0 is still installed" in human.stderr) is kept, human.stderr
    assert (install_route.INSTALLER_ONE_LINER in human.stderr) is not kept, human.stderr
    assert error["error"] == "upgrade_failed" and error["hint"] == command, error
    assert machine.connects() == [], "no hook refresh after a failed install"


def test_nothing_runs_when_pypi_has_nothing_newer(
    runner: CliRunner, tool: Tool, machine: Machine
) -> None:
    machine.latest = LatestRelease("0.9.0")

    result = runner.invoke(app, ["upgrade", "--yes"])
    report = _one_object(runner.invoke(app, ["--json", "upgrade", "--yes"]).stdout)

    assert result.exit_code == 0, result.output
    assert "up to date" in result.stdout
    assert report["dry_run"] is False and report["up_to_date"] is True, "a run, not a plan"
    assert machine.installs == []


def test_a_build_ahead_of_pypi_is_not_moved_back_without_a_pin(
    runner: CliRunner, tool: Tool, machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "__version__", "0.10.0rc1")
    machine.latest = LatestRelease("0.9.1")

    result = runner.invoke(app, ["upgrade", "--yes"])

    assert result.exit_code == 0, result.output
    assert machine.installs == [], "0.9.1 is OLDER than 0.10.0rc1 — that is a downgrade"


@pytest.mark.parametrize(
    "index", [None, "https://mirror.example/simple"], ids=["pypi", "own-index"]
)
def test_the_upgrade_asks_for_nothing_older_and_an_older_answer_still_fails(
    runner: CliRunner,
    tool: Tool,
    machine: Machine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index: str | None,
) -> None:
    """A cooldown added to uv.toml after the install, which the receipt therefore does not
    record, or an index that is behind, took uv's @latest to an OLDER release with exit 0,
    and the way back printed then failed under the same cutoff (measured, uv 0.12.19; sweep
    of #257). The command asks for the running release or newer, which uv refuses to
    resolve before it replaces anything. A new install that still reports an older release
    is no upgrade: no ✓, no hook refresh, and asq stays shut."""
    monkeypatch.setattr(lifecycle, "__version__", "0.8.1")
    machine.latest = LatestRelease("0.8.2")
    machine.new_version = "0.8.0"
    if index is not None:
        options = f"\n[tool.options]\nindex-url = {_toml(index)}\n"
        (tool.prefix / install_route.RECEIPT_NAME).write_text(
            _receipt(_OURS_PINNED, _TIKTOKEN, tail=options), encoding="utf-8"
        )
    _record(_hooked(tmp_path / "claude", tool.script))
    prompts: list[str] = []

    def enter(prompt: str = "") -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(install_cli, "_stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", enter)

    human = runner.invoke(app, ["upgrade", "--yes", "--reopen"])
    error = _one_object(runner.invoke(app, ["--json", "upgrade", "--yes"]).stdout)

    argv = machine.installs[0][0]
    assert argv[-3:] == ["--refresh-package", "aisquare-cli", "aisquare-cli[serve]>=0.8.1"]
    assert index is None or index in argv, argv
    assert human.exit_code == 1, human.output
    assert "the new install reports 0.8.0, which is older than 0.8.1" in human.stderr
    assert "Go back" not in human.stderr, "no way back that the same cutoff refuses"
    assert "✓" not in human.stdout and prompts == [], "no success, and asq is not reopened"
    assert machine.connects() == [], "no hook refresh for a release that moved back"
    assert error["error"] == "upgrade_not_confirmed", error
    assert error["hint"] == install_route.command_line(argv), error


def test_a_version_that_is_not_a_version_is_refused_before_anything(
    runner: CliRunner, machine: Machine
) -> None:
    result = runner.invoke(app, ["--json", "upgrade", "--version", "latest; rm -rf ~", "--yes"])

    assert result.exit_code == 1
    assert _one_object(result.stdout)["error"] == "invalid_version"
    assert machine.installs == [] and machine.lookups == 0


def test_a_bad_version_is_answered_with_an_example_this_flag_takes(
    runner: CliRunner, machine: Machine
) -> None:
    """The example was 0.9.1, a release of this product that does not exist; a user who
    copied it asked uv for it (sweep of #257). It is a patch of this release series."""
    result = runner.invoke(app, ["upgrade", "--version", "garbage"])

    example = re.search(r"--version takes a version such as (\S+), not 'garbage'", result.stderr)
    assert result.exit_code == 1
    assert example is not None, result.stderr
    assert example[1] == "0.8.1" and install_route.version_argument(example[1]) == example[1]
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
    remedy = install_route.command_line(
        ["aisquare", "agents", "refresh-hooks", "claude-code", "--config-dir", str(site)]
    )
    assert remedy in result.stdout


def test_a_site_whose_hooks_are_switched_off_is_rewritten_and_not_called_connected(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path
) -> None:
    """With `"disableAllHooks": true` Claude Code runs none of a directory's hooks. The plan
    said "re-connects" and the report "✓ hooks re-connected" there, where `agents connect`,
    init and the doctor say they are switched off (sweep of #257). They are still rewritten,
    so they run once the key goes."""
    off = _hooked(tmp_path / "claude-off", tool.script)
    settings = off / "settings.json"
    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    settings.write_text(json.dumps({"disableAllHooks": True, "hooks": hooks}), encoding="utf-8")
    on = _hooked(tmp_path / "claude-on", tool.script)
    _record(off, on)

    plan = runner.invoke(app, ["upgrade", "--dry-run"])
    planned = _one_object(runner.invoke(app, ["--json", "upgrade", "--dry-run"]).stdout)
    run = runner.invoke(app, ["upgrade", "--yes"])
    report = _one_object(runner.invoke(app, ["--json", "upgrade", "--yes"]).stdout)

    said = f'rewrites the Claude Code hooks in {off} (switched off there: "disableAllHooks": true)'
    assert said in plan.stdout, plan.stdout
    assert f"re-connects the Claude Code hooks in {off}" not in plan.stdout
    assert f"re-connects the Claude Code hooks in {on}" in plan.stdout, "control: hooks that run"
    assert planned["refresh_hooks"] == [str(off), str(on)], planned
    assert planned["hooks_off"] == [str(settings)], planned
    assert run.exit_code == 0, run.output
    told = f'· hooks rewritten in {off}, but switched off: remove "disableAllHooks" from {settings}'
    assert told in run.stdout, run.stdout
    assert f"✓ hooks re-connected in {off}" not in run.stdout
    assert f"✓ hooks re-connected in {on}" in run.stdout, "control: hooks that run"
    assert [hook["hooks_off"] for hook in report["hooks"]] == [str(settings), None], report
    assert len(machine.connects()) == 4, "both sites are rewritten, on both runs"


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


# --- review of #251, round 1 -------------------------------------------------------------


def _claude_with_memory(home: Path) -> Path:
    """A Claude Code directory whose CLAUDE.md `agents connect` would import."""
    directory = home / ".claude"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "CLAUDE.md").write_text("# Style\nuse tabs\n", encoding="utf-8")
    return directory


def test_connect_imports_claude_md_which_is_why_the_refresh_must_not(
    isolated_agent_home: Path,
) -> None:
    """Positive control for the one below: `agents connect` does import CLAUDE.md, so
    running it on every upgrade re-adds sections the user removed (finding 1)."""
    directory = _claude_with_memory(isolated_agent_home)

    connection = agents_service.connect("claude-code", directory)

    assert connection.imported == 1
    assert paths.db_path().exists()


def test_refresh_hooks_writes_the_hooks_and_imports_nothing(
    isolated_agent_home: Path, runner: CliRunner
) -> None:
    directory = _claude_with_memory(isolated_agent_home)

    result = runner.invoke(
        app, ["--json", "agents", "refresh-hooks", "claude-code", "--config-dir", str(directory)]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"name": "claude-code", "hooks_installed": True}
    assert agent_core.hooks_installed("claude-code", directory)
    assert not paths.db_path().exists(), "a refresh imports nothing, so it never opens the store"


def test_the_refresh_the_upgrade_runs_is_a_command_this_cli_has(runner: CliRunner) -> None:
    """The NEW install runs ``lifecycle.REFRESH_HOOKS``. A release that dropped or
    renamed it would break the upgrade from every earlier one."""
    result = runner.invoke(app, [*lifecycle.REFRESH_HOOKS, "--help"])

    assert result.exit_code == 0, result.output
    assert lifecycle.REFRESH_HOOKS[1] == "refresh-hooks"


def test_a_move_back_leaves_the_hooks_and_says_how_to_rewrite_them(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path
) -> None:
    """An older release may predate `agents refresh-hooks` (0.8 and earlier do)."""
    _record(_hooked(tmp_path / "claude", tool.script))
    machine.new_version = "0.7.0"

    result = runner.invoke(app, ["upgrade", "--version", "0.7.0", "--yes"])

    assert result.exit_code == 0, result.output
    assert machine.connects() == [], "no refresh on a downgrade"
    assert "0.7.0 is older than 0.9.0, so the hooks were left as they were" in result.stdout


@pytest.mark.parametrize(
    ("content", "left"),
    [
        (
            b'{"note": "caf\xe9", "hooks": {"Stop": [{"hooks": [{"type": "command", '
            b'"command": "/x/aisquare hook stop"}]}]}}',
            True,
        ),
        (b'{"hooks": {"Stop": 1}}', False),
    ],
    ids=["not-utf-8", "hooks-of-the-wrong-shape"],
)
def test_a_settings_file_that_cannot_be_read_as_hooks_is_left_not_raised(
    tool: Tool, tmp_path: Path, runner: CliRunner, machine: Machine, content: bytes, left: bool
) -> None:
    """Finding 2: a ValueError or TypeError from one recorded settings.json used to end
    `upgrade` and `upgrade --check` in a traceback. A file that is not UTF-8 is left
    and named (Claude Code may still run hooks from it); a hooks table of the wrong
    shape holds no hooks of ours, so there is nothing to refresh or report."""
    bad = tmp_path / "c-bad"
    bad.mkdir()
    (bad / "settings.json").write_bytes(content)
    _record(bad)

    refresh, kept = lifecycle.refresh_sites(tool.facts)
    result = runner.invoke(app, ["--json", "upgrade", "--check"])

    assert refresh == ()
    assert [site.config_dir for site in kept] == ([bad] if left else [])
    assert result.exit_code == 0, result.output
    named = [site["config_dir"] for site in _one_object(result.stdout)["hooks_left"]]
    assert named == ([str(bad)] if left else [])


def test_an_install_on_its_own_index_does_not_take_pypis_word_for_latest(
    runner: CliRunner, tool: Tool, machine: Machine
) -> None:
    """Finding 3: uv resolves @latest against the restated index, so PyPI's number is
    neither "nothing to do" nor proof of a silent no-op there."""
    (tool.prefix / install_route.RECEIPT_NAME).write_text(
        _receipt(
            _OURS_PINNED, tail='\n[tool.options]\nindex-url = "https://mirror.example/simple"\n'
        ),
        encoding="utf-8",
    )
    machine.new_version = "0.9.0"  # the mirror has nothing newer

    check = runner.invoke(app, ["--json", "upgrade", "--check"])
    run = runner.invoke(app, ["upgrade", "--yes"])

    assert machine.lookups == 0, "PyPI is not asked for an install that resolves elsewhere"
    report = _one_object(check.stdout)
    assert report["latest"] is None and "--index-url" in report["latest_error"]
    assert run.exit_code == 0, run.output
    assert "is the newest release your package index serves" in run.stdout


@pytest.mark.parametrize(
    ("tail", "restated"),
    [(_COOLDOWN, "--exclude-newer P7D"), (_FIXED_CUTOFF, "--exclude-newer 2026-10-01T00:00:00Z")],
    ids=["cooldown", "fixed-date"],
)
def test_an_install_under_a_uv_cutoff_does_not_take_pypis_word_for_latest(
    runner: CliRunner,
    tool: Tool,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
    tail: str,
    restated: str,
) -> None:
    """uv takes nothing uploaded after the cutoff, so PyPI's newest may be out of reach.
    Taken as the target, the version uv rightly left unchanged failed every run as
    §3.9.1's silent no-op, after --check had said an update was available (sweep of #257)."""
    (tool.prefix / install_route.RECEIPT_NAME).write_text(
        _receipt(_OURS_PINNED, tail=tail), encoding="utf-8"
    )
    monkeypatch.setattr(lifecycle, "__version__", "0.8.0")
    machine.new_version = "0.8.0"  # nothing newer was uploaded before the cutoff

    check = runner.invoke(app, ["--json", "upgrade", "--check"])
    run = runner.invoke(app, ["upgrade", "--yes"])

    assert machine.lookups == 0, "PyPI's newest says nothing about what the cutoff allows"
    report = _one_object(check.stdout)
    assert report["runnable"] is True, report
    assert report["latest"] is None and f"its uv cutoff ({restated})" in report["latest_error"]
    assert run.exit_code == 0, run.output
    assert f"✓ aisquare 0.8.0 is the newest release your uv cutoff allows ({restated})" in (
        run.stdout
    )
    assert machine.installs[0][0][-5:] == [
        *restated.split(),
        "--refresh-package",
        "aisquare-cli",
        "aisquare-cli[serve]>=0.8.0",
    ]


@pytest.mark.parametrize(
    ("tail", "newest", "held"),
    [
        (_COOLDOWN, "your uv cutoff allows (--exclude-newer P7D)", "your uv cutoff allows"),
        (
            _FIXED_CUTOFF,
            "your uv cutoff allows (--exclude-newer 2026-10-01T00:00:00Z)",
            "your uv cutoff allows",
        ),
        (
            '\n[tool.options]\nindex-url = "https://mirror.example/simple"\n',
            "your package index serves",
            "your package index served",
        ),
    ],
    ids=["cooldown", "fixed-date", "index"],
)
def test_an_unchanged_version_under_uvs_own_settings_is_the_newest_they_allow(
    runner: CliRunner,
    tool: Tool,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
    tail: str,
    newest: str,
    held: str,
) -> None:
    """A cutoff or an index set in uv's own settings (uv.toml, UV_EXCLUDE_NEWER) is in no
    receipt the plan can read, so PyPI's newer release was the target, and the version uv
    rightly left as it was failed as §3.9.1's silent no-op (sweep of #257). uv records those
    settings in the receipt it writes (measured, uv 0.12.19), which is read after the
    install."""
    machine.new_version = "0.9.0"  # PyPI says 0.9.1; uv's settings allow nothing newer
    plain = _receipt(_OURS_PINNED, _TIKTOKEN)
    installer = install_route.run_installer

    def install_under_settings(argv: Any, *, env: Any, to_stderr: bool) -> int:
        written = '{ name = "aisquare-cli", extras = ["serve"], specifier = ">=0.9.0" }'
        (tool.prefix / install_route.RECEIPT_NAME).write_text(
            _receipt(written, _TIKTOKEN, tail=tail), encoding="utf-8"
        )
        return installer(argv, env=env, to_stderr=to_stderr)

    monkeypatch.setattr(install_route, "run_installer", install_under_settings)

    result = runner.invoke(app, ["upgrade", "--yes"])
    (tool.prefix / install_route.RECEIPT_NAME).write_text(plain, encoding="utf-8")
    report = _one_object(runner.invoke(app, ["--json", "upgrade", "--yes"]).stdout)

    assert machine.lookups == 2, "PyPI was asked: the plan's receipt named neither"
    assert result.exit_code == 0, result.output
    assert f"✓ aisquare 0.9.0 is the newest release {newest}" in result.stdout, result.stdout
    assert f"· PyPI's latest is 0.9.1; {held} 0.9.0" in result.stdout, result.stdout
    assert "§3.9.1" not in result.output
    assert report["upgraded"] is True and report["version"] == "0.9.0", report
    assert f"PyPI's latest is 0.9.1; {held} 0.9.0" in report["notes"], report


def test_check_with_a_pin_advises_the_pin(runner: CliRunner, tool: Tool, machine: Machine) -> None:
    """Finding 8: the advice must name the version the check was asked about."""
    result = runner.invoke(app, ["upgrade", "--check", "--version", "0.7.0"])

    assert result.exit_code == 0, result.output
    assert "upgrade with: aisquare upgrade --version 0.7.0" in result.stdout


@pytest.mark.parametrize(
    ("latest", "verdict", "advice"),
    [
        ("0.7.0", "latest: 0.7.0 (yours is newer)", "nothing to upgrade"),
        ("0.8.0", "latest: 0.8.0 (you have it)", "nothing to upgrade"),
        ("0.8.1", "latest: 0.8.1 (an update is available)", "upgrade with: aisquare upgrade"),
    ],
    ids=["pypi-older", "the-same", "pypi-newer"],
)
def test_check_tells_a_newer_build_from_the_latest_and_advises_only_an_upgrade(
    runner: CliRunner,
    tool: Tool,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
    latest: str,
    verdict: str,
    advice: str,
) -> None:
    """With 0.8.0 running and PyPI's newest 0.7.0, --check said "(you have it)" and "upgrade
    with: aisquare upgrade", a command that then answered "nothing to do" (sweep of #257)."""
    monkeypatch.setattr(lifecycle, "__version__", "0.8.0")
    machine.latest = LatestRelease(latest)

    result = runner.invoke(app, ["upgrade", "--check"])

    lines = result.stdout.splitlines()
    assert result.exit_code == 0, result.output
    assert verdict in lines and advice in lines, lines
    assert ("upgrade with: aisquare upgrade" in lines) is (latest == "0.8.1"), lines


@pytest.mark.parametrize(
    ("option", "running", "offered"),
    [
        ('prerelease = "allow"', "0.9.0", "1.0.0rc2"),
        (None, "1.0.0rc1", "1.0.0rc2"),
        (None, "0.9.0", None),
    ],
    ids=["opted-in", "running-an-rc", "neither"],
)
def test_an_install_that_takes_pre_releases_is_offered_the_newest_one(
    runner: CliRunner,
    tool: Tool,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
    option: str | None,
    running: str,
    offered: str | None,
) -> None:
    """An install made with `--prerelease allow`, which the command restates, or one running a
    pre-release, which its `>=` names, gets uv's newest pre-release. PyPI's info.version is
    its newest final, so --check said "(you have it)" or "(yours is newer)" and "nothing to
    upgrade", and `upgrade --yes` and asq's Update "nothing to do" (sweep of #257)."""
    tail = f"\n[tool.options]\n{option}\n" if option else ""
    (tool.prefix / install_route.RECEIPT_NAME).write_text(
        _receipt(_OURS_PINNED, _TIKTOKEN, tail=tail), encoding="utf-8"
    )
    monkeypatch.setattr(lifecycle, "__version__", running)
    releases = {version: [{"yanked": False}] for version in ("0.9.0", "1.0.0rc1", "1.0.0rc2")}
    body = json.dumps({"info": {"version": "0.9.0"}, "releases": releases}).encode()
    monkeypatch.setattr(install_route, "fetch_latest", _REAL_FETCH_LATEST)
    monkeypatch.setattr(install_route, "open_url", lambda _request, timeout: _Response(body))
    machine.new_version = offered or running

    check = runner.invoke(app, ["upgrade", "--check"])
    run = runner.invoke(app, ["upgrade", "--yes"])

    lines = check.stdout.splitlines()
    assert check.exit_code == 0 and run.exit_code == 0, check.output + run.output
    if offered is None:
        assert "latest: 0.9.0 (you have it)" in lines and "nothing to upgrade" in lines, lines
        assert machine.installs == [] and "nothing to do" in run.stdout, run.stdout
    else:
        assert f"latest: {offered} (an update is available)" in lines, lines
        [(argv, _env, _to_stderr)] = machine.installs
        assert argv[-1] == f"aisquare-cli[serve]>={running}", argv
        assert f"✓ aisquare {offered} (was {running})" in run.stdout, run.stdout


@pytest.mark.parametrize(
    "route", ["pipx", "venv", "Homebrew", "uvx", "native Windows uv tool", "editable"]
)
def test_check_on_a_route_upgrade_does_not_run_advises_only_an_update(
    runner: CliRunner, machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """Only the uv tool route had learnt to say "nothing to upgrade". On a route `aisquare
    upgrade` does not run itself, --check said "(you have it)" and then "upgrade with: pipx
    upgrade aisquare-cli" (review of #257). A checkout keeps its command: PyPI's number
    says nothing about its source."""
    extra: dict[str, Any] = {}
    prefix = tmp_path / "pip-e"
    if route == "pipx":
        prefix = tmp_path / "pipx" / "venvs" / "aisquare-cli"
        prefix.mkdir(parents=True)
        (prefix / install_route.PIPX_METADATA_NAME).write_text("{}", encoding="utf-8")
    elif route == "venv":
        prefix = tmp_path / "venv"
    elif route == "Homebrew":
        prefix = tmp_path / "opt" / "Cellar" / "aisquare" / "0.8.0" / "libexec"
    elif route == "uvx":
        prefix = _uv_cache(tmp_path / "cache" / "uv") / "archive-v0" / "AbC123"
    elif route == "native Windows uv tool":
        prefix = _prefix(tmp_path / "win", _receipt(_OURS_PINNED, _TIKTOKEN))
        extra["platform"] = "win32"
    else:
        checkout = (tmp_path / "checkout").as_uri()
        extra["direct_url"] = json.dumps({"url": checkout, "dir_info": {"editable": True}})
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    found = _facts(prefix, **extra)
    monkeypatch.setattr(install_route, "facts", lambda: found)
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(lifecycle, "__version__", "0.8.0")

    def advice(latest: str) -> list[str]:
        machine.latest = LatestRelease(latest)
        result = runner.invoke(app, ["upgrade", "--check"])
        assert result.exit_code == 0, result.output
        lines = result.stdout.splitlines()
        return [line for line in lines if line.startswith(("upgrade with: ", "nothing to"))]

    plan = _one_object(runner.invoke(app, ["--json", "upgrade", "--check"]).stdout)
    same, newer = advice("0.8.0"), advice("0.8.1")

    assert plan["runnable"] is False, plan
    if route == "editable":
        assert same and same[0].startswith("upgrade with: "), same
    else:
        assert same == ["nothing to upgrade"], same
    assert newer and newer[0].startswith("upgrade with: "), f"control: {newer}"


def test_a_failing_sites_remedy_survives_a_space_in_its_path(
    runner: CliRunner, tool: Tool, machine: Machine, tmp_path: Path
) -> None:
    """Finding 9: the printed remedy is shell-quoted like every other command printed."""
    site = _hooked(tmp_path / "Jane Doe" / ".claude", tool.script)
    _record(site)
    machine.connect_exit = 1

    result = runner.invoke(app, ["upgrade", "--yes"])

    quoted = install_route.command_line(
        ["aisquare", "agents", "refresh-hooks", "claude-code", "--config-dir", str(site)]
    )
    assert result.exit_code == 1
    assert quoted in result.stdout
    assert f"--config-dir {site}" not in result.stdout, "an unquoted path would split there"
