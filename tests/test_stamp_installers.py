"""The release assets: install.sh and install.ps1 stamped to one version.

``scripts/stamp_installers.py`` writes the copies that
``.github/workflows/installers.yml`` attaches to every GitHub Release, so that
``releases/download/vX.Y.Z/install.sh`` installs exactly X.Y.Z and
``releases/latest/download/install.sh`` the latest. What is pinned here:

* the stamp changes only the stamp lines, and a stamp line that a later edit
  renamed or doubled is refused rather than shipped unstamped;
* the stamped install.sh pins its version by default, and ``--version V``,
  ``AISQUARE_INSTALL_VERSION=V`` and ``--version latest`` still override it;
  the repository's copy stays unpinned;
* the stamped install.ps1 fetches the tagged install.sh asset and passes the
  version on, ahead of the user's own arguments;
* the release workflow is valid YAML, runs only after the PyPI publish, and
  uploads only when asked to.

The stamped install.sh also runs the whole POSIX and function suites in the
release workflow (``AISQUARE_TEST_INSTALL_SH``), and the test at the bottom
proves that override really changes what those suites read.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
STAMPER = REPO / "scripts" / "stamp_installers.py"
WORKFLOWS = REPO / ".github" / "workflows"
SH = shutil.which("sh") or "/bin/sh"

VERSION = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
TAG = f"v{VERSION}"


def _load_stamper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stamp_installers", STAMPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stamper = _load_stamper()


def run_stamper(*args: str) -> subprocess.CompletedProcess[str]:
    """The script as the workflow runs it."""
    return subprocess.run(
        [sys.executable, str(STAMPER), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def stamped(tmp_path: Path) -> Path:
    result = run_stamper("--tag", TAG, "--out", str(tmp_path / "assets"))
    assert result.returncode == 0, result.stderr
    return tmp_path / "assets"


def _changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    old, new = before.splitlines(), after.splitlines()
    assert len(old) == len(new), "the stamp added or removed lines"
    return [(a, b) for a, b in zip(old, new, strict=True) if a != b]


# --- the stamp itself --------------------------------------------------------


def test_the_stamp_changes_only_the_stamp_lines(stamped: Path) -> None:
    sh_changes = _changed_lines(
        (REPO / "install.sh").read_text(encoding="utf-8"),
        (stamped / "install.sh").read_text(encoding="utf-8"),
    )
    main_sh = f"curl -fsSL {stamper.RAW_MAIN}/install.sh | sh"
    own_sh = f"curl -fsSL {stamper.release_url(TAG, 'install.sh')} | sh"
    # The header and --help print the release's own one-liner, not main's.
    assert sh_changes == [
        (f"#   {main_sh}", f"#   {own_sh}"),
        ('STAMPED_VERSION=""', f'STAMPED_VERSION="{VERSION}"'),
        (f"  {main_sh}", f"  {own_sh}"),
    ]

    ps1_changes = _changed_lines(
        (REPO / "install.ps1").read_text(encoding="utf-8"),
        (stamped / "install.ps1").read_text(encoding="utf-8"),
    )
    assert ps1_changes == [
        (
            f"#   irm {stamper.RAW_MAIN}/install.ps1 | iex",
            f"#   irm {stamper.release_url(TAG, 'install.ps1')} | iex",
        ),
        ("$StampedVersion = ''", f"$StampedVersion = '{VERSION}'"),
        (
            '$InstallUrl = "https://raw.githubusercontent.com/$Repo/main/install.sh"',
            f'$InstallUrl = "https://github.com/$Repo/releases/download/{TAG}/install.sh"',
        ),
    ]


def test_the_stamped_installer_stays_executable_and_byte_clean(stamped: Path) -> None:
    if sys.platform != "win32":
        assert os.stat(stamped / "install.sh").st_mode & 0o111, "install.sh lost +x"
    # LF on every OS: .gitattributes holds both files to LF in every checkout
    # (a Windows one with core.autocrlf included), and the stamper writes bytes.
    for asset in stamper.ASSETS:
        assert b"\r\n" not in (stamped / asset).read_bytes(), f"{asset} gained CRLF"
    # install.ps1 is pure ASCII on purpose (its header says why); so is the stamp.
    (stamped / "install.ps1").read_bytes().decode("ascii")


def test_a_tag_that_is_not_the_built_version_is_refused(tmp_path: Path) -> None:
    """PyPI got pyproject's version; an installer pinned to anything else breaks."""
    result = run_stamper("--tag", "v0.0.1", "--out", str(tmp_path / "assets"))
    assert result.returncode == 1
    assert f"pyproject.toml's version {VERSION}" in result.stderr
    assert not (tmp_path / "assets").exists(), "a refused stamp still wrote files"


@pytest.mark.parametrize(
    "tag",
    ["0.9.0", "v0.9", "v0.9.0; rm -rf ~", "v0.9.0'", 'v0.9.0"', "v0.9.0 ", "vlatest"],
)
def test_only_a_release_tag_is_stamped(tag: str) -> None:
    """The version lands inside quotes in sh and PowerShell: nothing else may."""
    with pytest.raises(stamper.StampError):
        stamper.version_of(tag)


@pytest.mark.parametrize("tag", ["v0.9.0", "v1.10.2", "v1.0.0rc1", "v1.0.0.post1"])
def test_release_tags_are_accepted(tag: str) -> None:
    assert stamper.version_of(tag) == tag[1:]


@pytest.mark.parametrize(
    ("stamp", "text"),
    [
        ("sh", "#!/bin/sh\nPIN_VERSION=${AISQUARE_INSTALL_VERSION:-}\n"),
        ("sh", 'STAMPED_VERSION=""\nSTAMPED_VERSION=""\n'),
        ("ps1", "$StampedVersion = ''\n"),
        (
            "ps1",
            "$StampedVersion = ''\n"
            '$InstallUrl = "https://raw.githubusercontent.com/$Repo/rc/install.sh"\n',
        ),
    ],
    ids=["sh-missing", "sh-doubled", "ps1-no-url", "ps1-other-url"],
)
def test_a_stamp_line_that_moved_is_refused(stamp: str, text: str) -> None:
    """Otherwise the asset would ship unstamped and install the latest."""
    function: Any = stamper.stamp_sh if stamp == "sh" else stamper.stamp_ps1
    with pytest.raises(stamper.StampError, match="expected the line"):
        function(text, "v0.9.0")


# --- what the stamped install.sh installs ------------------------------------


def _pin(script: Path, snippet: str, env: dict[str, str] | None = None) -> str:
    """Source ``script`` as a library and run ``resolve`` offline; return the pin."""
    environment = {
        key: value for key, value in os.environ.items() if key != "AISQUARE_INSTALL_VERSION"
    }
    environment.update({"AISQUARE_INSTALL_LIB": "1", "NO_COLOR": "1"})
    environment.update(env or {})
    result = subprocess.run(
        [
            SH,
            "-c",
            f'. "{script}"\n{snippet}\n'
            "CLI_VERSION=0.1.0; OFFLINE=1; WANT_SYSTEM_DEPS=0; WANT_AGENT=0\n"
            'resolve >/dev/null 2>&1\nprintf "%s|%s\\n" "$PIN_VERSION" "$CLI_ACTION"\n',
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_stamped_installer_pins_its_own_release(stamped: Path) -> None:
    assert _pin(stamped / "install.sh", "parse_args") == f"{VERSION}|upgrade"


def test_the_repository_installer_stays_unpinned() -> None:
    """main's copy installs the latest, as it always has."""
    assert _pin(REPO / "install.sh", "parse_args") == "|current"


@pytest.mark.parametrize(
    ("snippet", "env", "expected"),
    [
        ("parse_args --version 0.5.0", None, "0.5.0|upgrade"),
        ("parse_args --version=0.5.0", None, "0.5.0|upgrade"),
        ("parse_args", {"AISQUARE_INSTALL_VERSION": "0.5.0"}, "0.5.0|upgrade"),
        ("parse_args --version latest", None, "|current"),
        ("parse_args", {"AISQUARE_INSTALL_VERSION": "latest"}, "|current"),
        # Set but empty unpins too, as it always did on main's copy: a stamped
        # asset must not read it as unset and pin (a downgrade once newer ships).
        ("parse_args", {"AISQUARE_INSTALL_VERSION": ""}, "|current"),
    ],
    ids=["flag", "flag=", "env", "flag-latest", "env-latest", "env-empty"],
)
def test_the_stamped_pin_is_still_overridable(
    stamped: Path, snippet: str, env: dict[str, str] | None, expected: str
) -> None:
    """``|current`` is the unpinned answer: offline, there is nothing to compare."""
    assert _pin(stamped / "install.sh", snippet, env) == expected


def test_latest_unpins_the_repository_copy_too() -> None:
    assert _pin(REPO / "install.sh", "parse_args --version latest") == "|current"


# --- what the stamped install.ps1 runs ---------------------------------------


def test_the_stamped_ps1_fetches_the_tagged_installer_and_passes_the_pin(stamped: Path) -> None:
    text = (stamped / "install.ps1").read_text(encoding="ascii")
    assert f"https://github.com/$Repo/releases/download/{TAG}/install.sh" in text, (
        "the stamped shim must fetch its own release's install.sh"
    )
    assert '/main/install.sh"' not in text, "the stamped shim still names main's install.sh"
    assert f"$StampedVersion = '{VERSION}'" in text
    # The pin goes BEFORE the user's arguments: install.sh reads options left to
    # right, so a --version the user typed still wins.
    assert "| sh -s -- $Pin$Forward" in text
    assert re.search(r"\$Pin = if \(\$StampedVersion\) \{ \"--version \$StampedVersion \" \}", text)


def test_git_keeps_both_installers_lf_in_every_checkout() -> None:
    """A Windows checkout (core.autocrlf) gave install.sh CRLF, and the stamp with it."""
    git = shutil.which("git")
    if git is None or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    result = subprocess.run(
        [git, "check-attr", "eol", "--", *stamper.ASSETS],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"{asset}: eol: lf" for asset in stamper.ASSETS]


def test_a_refused_stamp_writes_neither_file(tmp_path: Path) -> None:
    """install.ps1's stamp is checked before install.sh is written, not after."""
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(REPO / "pyproject.toml", root)
    shutil.copy(REPO / "install.sh", root)
    (root / "install.ps1").write_text("# no stamp lines\n", encoding="ascii")
    with pytest.raises(stamper.StampError):
        stamper.stamp(root, TAG, tmp_path / "assets")
    assert not (tmp_path / "assets").exists()


def test_the_repository_ps1_still_fetches_main() -> None:
    text = (REPO / "install.ps1").read_text(encoding="ascii")
    assert '$InstallUrl = "https://raw.githubusercontent.com/$Repo/main/install.sh"' in text
    assert "$StampedVersion = ''" in text


# --- the release workflow -----------------------------------------------------


def _workflow(name: str) -> dict[str, Any]:
    loaded: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # YAML 1.1 reads a bare `on:` key as the boolean True.
    loaded["on"] = loaded.pop(True, loaded.get("on"))
    return loaded


def test_the_installers_are_checked_before_pypi_and_attached_after() -> None:
    """PyPI cannot take a version back, so everything that can fail goes first."""
    publish = _workflow("publish.yml")
    jobs = publish["jobs"]
    precheck = jobs["precheck"]
    assert precheck["needs"] == "build"
    assert precheck["uses"] == "./.github/workflows/installers-check.yml"
    assert precheck["with"]["tag"] == "${{ github.event.release.tag_name }}"
    assert "permissions" not in precheck, "the check needs no more than the default read"
    assert set(jobs["publish"]["needs"]) == {"build", "precheck"}, "PyPI waits for precheck"

    installers = jobs["installers"]
    assert installers["needs"] == "publish", "the installers must wait for the PyPI publish"
    assert installers["uses"] == "./.github/workflows/installers.yml"
    assert installers["with"]["upload"] is True
    assert installers["with"]["tag"] == "${{ github.event.release.tag_name }}"
    assert installers["permissions"] == {"contents": "write"}, "gh release upload needs it"
    # The workflow-wide default stays read-only; only that job can write.
    assert publish["permissions"] == {"contents": "read"}


def test_the_check_workflow_checks_everything_and_writes_nothing() -> None:
    check = _workflow("installers-check.yml")
    assert check["permissions"] == {"contents": "read"}
    (job,) = check["jobs"].values()
    assert "permissions" not in job
    steps = job["steps"]
    names = [step.get("name") for step in steps]
    for name in (
        "Stamp",
        "Check the stamped install.sh",
        "The install.sh suites, on the stamped copy",
        "Check the stamped install.ps1",
    ):
        assert name in names, name
    assert not any("gh release" in step.get("run", "") for step in steps)
    suites = steps[names.index("The install.sh suites, on the stamped copy")]
    assert suites["env"]["AISQUARE_TEST_INSTALL_SH"].endswith("/install.sh")
    # Release day resolves the dependencies CI tested, as ci.yml installs them.
    assert '-c ci/constraints.txt -e ".[dev]"' in suites["run"]


def test_the_installers_workflow_uploads_only_when_asked() -> None:
    installers = _workflow("installers.yml")
    on = installers["on"]
    assert on["workflow_call"]["inputs"]["upload"]["type"] == "boolean"
    # A manual run is a dry run unless someone ticks the box.
    assert on["workflow_dispatch"]["inputs"]["upload"]["default"] is False
    steps = installers["jobs"]["installers"]["steps"]
    by_name = {step.get("name"): step for step in steps}
    upload = by_name["Upload to the release"]
    assert upload["if"] == "${{ inputs.upload }}"
    assert "gh release upload" in upload["run"] and "--clobber" in upload["run"]
    names = [step.get("name") for step in steps]
    for before in ("Stamp", "Wait until PyPI serves the version"):
        assert names.index(before) < names.index("Upload to the release"), before


@pytest.mark.parametrize(
    "pin",
    [
        r"SHFMT=v[\d.]+",
        r"PSScriptAnalyzer -RequiredVersion [\d.]+",
        r"-ExcludeRule [A-Za-z]+",
        r"shfmt -i \d+ -ci",
    ],
)
def test_the_release_checks_match_install_yml(pin: str) -> None:
    """The stamped files are held to install.yml's tools and flags, not a drifted copy."""
    ours = set(re.findall(pin, (WORKFLOWS / "installers-check.yml").read_text(encoding="utf-8")))
    theirs = set(re.findall(pin, (WORKFLOWS / "install.yml").read_text(encoding="utf-8")))
    assert ours and ours == theirs, (ours, theirs)


# The condition and reason of tests/test_install_script_functions.py's own
# module-level skip: the probe imports that module, so it runs where it does.
@pytest.mark.skipif(sys.platform == "win32", reason="install.sh is a POSIX shell script")
def test_the_suites_really_read_the_override(stamped: Path) -> None:
    """``AISQUARE_TEST_INSTALL_SH`` changes the file the POSIX suite reads.

    Without this the workflow step could run the repository's copy twice and
    pass, and the stamped asset would be checked by nothing.
    """
    probe = (
        "import tests.test_install_script_is_posix as posix, "
        "tests.test_install_script_functions as functions; "
        "print(posix.SCRIPT); print(functions.SCRIPT)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={**os.environ, "AISQUARE_TEST_INSTALL_SH": str(stamped / "install.sh")},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [str(stamped / "install.sh")] * 2


# --- the URLs people are given -------------------------------------------------

_RELEASE_URL = re.compile(
    r"https://github\.com/AISquare-Studio/aisquare-cli/releases/"
    r"(latest/download|download/v[^/\s]+)/([^\s)`'\"|]+)"
)
_DOCUMENTED = (REPO / "README.md", REPO / "docs" / "install.md")


@pytest.mark.parametrize("page", _DOCUMENTED, ids=lambda page: page.name)
def test_the_docs_lead_with_main_and_show_a_pin(page: Path) -> None:
    """Owner's decision (#401, MAJOR-2): main's copy stays the default one-liner,
    because the latest/download URL answers 404 until a release's installers are
    attached; a release's own URL is how to pin."""
    text = page.read_text(encoding="utf-8")
    default = f"curl -fsSL {stamper.RAW_MAIN}/install.sh | sh"
    assert default in text, f"{page.name} lost the default one-liner"
    urls = _RELEASE_URL.findall(text)
    for where, asset in urls:
        assert asset in stamper.ASSETS, f"{page.name}: {asset} is not a release asset"
        if where != "latest/download":
            stamper.version_of(where.removeprefix("download/"))
    assert any(where.startswith("download/v") for where, _asset in urls), (
        f"{page.name}: no pin-a-version example"
    )
    # The default comes first: before any release URL on the page.
    first_release = min(text.index(m.group(0)) for m in _RELEASE_URL.finditer(text))
    assert text.index(default) < first_release, f"{page.name} leads with a release URL"


def test_the_latest_url_is_offered_with_its_caveat() -> None:
    text = (REPO / "docs" / "install.md").read_text(encoding="utf-8")
    assert "releases/latest/download/install.sh" in text
    assert "404" in text, "the latest URL must say it can briefly 404 after a release"


def test_the_repair_command_is_mains_one_liner() -> None:
    from aisquare.services import install_route

    main = f"curl -fsSL {stamper.RAW_MAIN}/install.sh | sh"
    assert main == install_route.INSTALLER_ONE_LINER
