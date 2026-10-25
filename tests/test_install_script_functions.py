"""Unit tests for `install.sh`'s functions, one at a time (§8.2).

`install.sh` sources cleanly with `AISQUARE_INSTALL_LIB=1` set — that is what
the guard on its last line is for — so each function can be called from here
with stub commands on PATH. Every test below runs the REAL shell function, not a
Python reimplementation of it.

WHY THIS EXISTS BESIDE THE CONTAINER MATRIX. The matrix (tests/install/matrix.sh)
proves the script works on five distributions, and it is the only thing that
can; but each cell takes minutes, needs a container engine, and exercises ONE
path through the decisions. These tests are the other half: they run in
milliseconds, need nothing installed, and reach the branches a container cannot
be persuaded into — a machine with four package managers, a Node exactly on the
floor, a WSL2 kernel, a PyPI payload with an adversarial README.

The shell is invoked as `sh`, which on the machine running the suite may be
bash, dash or ash. That is deliberate: the tests then also assert the functions
work under whatever `/bin/sh` the developer has, and CI runs the same file with
`/bin/sh` pointed at dash.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import paths
from aisquare.core.store import store_session
from aisquare.services import diagnostics

# `install.sh` is a POSIX shell script and every test here drives it through
# `sh`, `pty.fork` and `os.execve`. None of that exists on Windows, and `pty`
# used to be imported at MODULE scope, so the whole file failed COLLECTION
# there rather than skipping — an error, not a skip, before a single test ran.
# Skipping at module level is the honest answer and keeps the rest of the
# suite's Windows run clean.
if sys.platform == "win32":  # pragma: no cover - the POSIX installer's own tests
    pytest.skip("install.sh is a POSIX shell script", allow_module_level=True)

REPO = Path(__file__).resolve().parents[1]
#: AISQUARE_TEST_INSTALL_SH points these tests at another copy: the release
#: workflow (.github/workflows/installers.yml) runs them on the STAMPED install.sh
#: it is about to upload, so a release asset is held to the same guards.
SCRIPT = Path(os.environ.get("AISQUARE_TEST_INSTALL_SH") or REPO / "install.sh")

#: Resolved from the REAL PATH, once, and passed to `subprocess.run` absolutely.
#: Every test below hands the child a narrowed PATH so an absent tool is really
#: absent — and a relative "sh" would then be looked up in that narrowed PATH,
#: which fails to launch the shell at all. Measured: 49 of these tests failed
#: with returncode 255 and no output before this was absolute.
SH = shutil.which("sh") or "/bin/sh"


def sh(
    snippet: str,
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    path: str | None = None,
    no_terminal: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Source install.sh as a library, run `snippet`, return the result.

    `NO_COLOR` is always set: the colour variables are chosen at source time
    from `[ -t 1 ]`, and a captured pipe is not a terminal — but pinning it
    means an assertion on output text cannot start failing because someone ran
    the suite through a pty.

    `no_terminal=True` puts the child in a NEW SESSION, so it has no
    controlling terminal at all — `/dev/tty` cannot be opened. That is a
    genuinely different condition from "stdout is a pipe", and it is the one
    §0.9 is about (CI, a Dockerfile, a provisioner). It found a fatal bug in
    `tty_available`, so it is a first-class option here rather than a trick
    inside one test.
    """
    environment = dict(os.environ)
    environment.update({"AISQUARE_INSTALL_LIB": "1", "NO_COLOR": "1"})
    # A pinned version keeps `resolve` off the network in every test that does
    # not deliberately exercise the lookup.
    environment.setdefault("AISQUARE_INSTALL_VERSION", "9.9.9")
    if path is not None:
        environment["PATH"] = path
    if env:
        environment.update(env)
    return subprocess.run(
        [SH, "-c", f'. "{SCRIPT}"\n{snippet}\n'],
        capture_output=True,
        text=True,
        env=environment,
        cwd=cwd,
        timeout=120,
        # setsid(2). Without it the child inherits the session's terminal, so a
        # test of the no-terminal path would silently test the other one.
        start_new_session=no_terminal,
        stdin=subprocess.DEVNULL,
    )


@pytest.fixture(scope="module")
def source_text() -> str:
    """`install.sh`'s text, for the assertions that are about how it is written."""
    return SCRIPT.read_text(encoding="utf-8")


def stub_dir(tmp_path: Path, name: str, *commands: str, body: str = "exit 0") -> Path:
    """A directory of executable stubs, for putting on PATH."""
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    for command in commands:
        script = directory / command
        script.write_text(f"#!/bin/sh\n# stub: {command}\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
    return directory


#: The real utilities every stubbed PATH still needs — the script is written in
#: terms of them, and a PATH without them tests nothing but their absence.
def base_path(tmp_path: Path) -> str:
    real = tmp_path / "realbin"
    real.mkdir(exist_ok=True)
    for tool in (
        "sed",
        "cut",
        "tr",
        "head",
        "grep",
        "printf",
        "id",
        "uname",
        "dirname",
        "basename",
        "cat",
        "sort",
        "wc",
        "mktemp",
        "rm",
        "chmod",
        "mkdir",
        "env",
        "tee",
        "readlink",
        "sh",
    ):
        found = shutil.which(tool)
        if found and not (real / tool).exists():
            (real / tool).symlink_to(found)
    return str(real)


# ---------------------------------------------------------------------------
# version_lt — the comparison every "current or behind?" decision rests on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lhs", "rhs", "expected"),
    [
        ("0.5.0", "0.6.0", True),
        ("0.6.0", "0.6.0", False),
        ("0.6.0", "0.5.0", False),
        ("0.6.0", "0.6.1", True),
        # THE BUSYBOX TRAP, and the reason `sort -V` is not used: BusyBox `sort`
        # has no -V, so a version compare built on it degrades to a LEXICAL one
        # on Alpine, where "0.10.0" < "0.9.0" is true and wrong. A field-by-field
        # numeric compare is immune, and this is the case that proves it.
        ("0.9.0", "0.10.0", True),
        ("0.10.0", "0.9.0", False),
        ("1.9", "1.10", True),
        # tmux's non-numeric suffix, which is why the fields are stripped to
        # digits rather than compared as strings.
        ("3.7c", "3.8", True),
        ("3.2a", "3.2", False),
        ("3.3a", "3.2", False),
        # Missing fields read as zero, so a two-field version compares against a
        # three-field floor without a special case.
        ("22", "22.0.0", False),
        ("21", "22.0.0", True),
        ("2.1.263", "2.1.263", False),
    ],
)
def test_version_lt(lhs: str, rhs: str, expected: bool) -> None:
    """`version_lt A B` is true exactly when A is older than B."""
    result = sh(f'if version_lt "{lhs}" "{rhs}"; then echo yes; else echo no; fi')
    assert result.stdout.strip() == ("yes" if expected else "no"), (
        f"version_lt {lhs} {rhs} = {result.stdout.strip()}, wanted {expected}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# the six version parsers, against the strings the six tools really print
# ---------------------------------------------------------------------------

#: Measured on 2026-09-08, not recalled. Six tools, six shapes — which is why
#: there are six parsers and not one regex (§3.9).
VERSION_STRINGS = [
    ("aisquare", "aisquare 0.6.0", "0.6.0"),
    ("uv", "uv 0.12.3 (x86_64-unknown-linux-gnu)", "0.12.3"),
    ("claude", "2.1.263 (Claude Code)", "2.1.263"),
    ("tmux", "tmux 3.7c", "3.7c"),
    ("gh", "gh version 2.97.0 (2026-07-31)", "2.97.0"),
    ("node", "v26.7.0", "26.7.0"),
    ("git", "git version 2.55.0", "2.55.0"),
]


@pytest.mark.parametrize(
    ("tool", "output", "expected"), VERSION_STRINGS, ids=[t for t, _, _ in VERSION_STRINGS]
)
def test_the_version_parsers_read_the_real_strings(
    tmp_path: Path, tool: str, output: str, expected: str
) -> None:
    """Each parser against its own tool's actual banner.

    `gh` prints a multi-line banner and `claude` puts the number FIRST, so a
    single "field 2 of line 1" parser would be wrong for two of the seven. The
    stub prints a second line for every tool, so a parser that forgot `head -1`
    fails here rather than on someone's machine.
    """
    binary = "tmux" if tool == "tmux" else tool
    stubs = stub_dir(
        tmp_path,
        "bin",
        binary,
        body=f'printf "%s\\n" "{output}"\nprintf "%s\\n" "a second line no parser may read"',
    )
    path = f"{stubs}:{base_path(tmp_path)}"
    result = sh(f"version_of_{tool}", path=path)
    assert result.stdout.strip() == expected, (
        f"version_of_{tool} read {result.stdout.strip()!r} from {output!r}, wanted {expected!r}"
    )


def test_a_missing_binary_yields_an_empty_version(tmp_path: Path) -> None:
    """Absent is the empty string, never an error — `survey` branches on it."""
    result = sh("version_of_tmux", path=base_path(tmp_path))
    assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# detect_os — §0.2, "no pick-your-platform step"
# ---------------------------------------------------------------------------


def host_is_wsl(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``detect_os`` finds a WSL host HERE — the script's own two checks, in order.

    ``install.sh`` sets ``IS_WSL=1`` when ``WSL_DISTRO_NAME`` is non-empty (a shell
    started by WSL), and otherwise when ``/proc/version`` names ``microsoft`` or
    ``wsl`` (a service or a cron job inside WSL has no variable). A test cannot stub
    that path — the script reads it literally, and ``grep`` is on every stubbed
    PATH (:func:`base_path`) — so the real ``detect_os`` on a WSL2 kernel reports
    ``IS_WSL=1`` whatever ``uname`` is made to say. An expectation that includes
    ``IS_WSL`` asks this rather than assuming a plain Linux runner: there the answer
    is ``False`` and the assertion is exactly what it was.
    """
    source = os.environ if env is None else env
    if source.get("WSL_DISTRO_NAME", ""):
        return True
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"microsoft|wsl", version, re.IGNORECASE) is not None


@pytest.mark.parametrize(
    ("kernel", "machine", "expected_os", "expected_arch"),
    [
        ("Linux", "x86_64", "linux", "x86_64"),
        ("Linux", "aarch64", "linux", "aarch64"),
        ("Darwin", "arm64", "macos", "arm64"),
        ("Darwin", "x86_64", "macos", "x86_64"),
        ("FreeBSD", "amd64", "bsd", "amd64"),
    ],
)
def test_detect_os_maps_uname(
    tmp_path: Path, kernel: str, machine: str, expected_os: str, expected_arch: str
) -> None:
    """`uname -s`/`-m` to the labels the rest of the script branches on.

    The third field, ``IS_WSL``, is the HOST's, not the stub's: on a WSL2 machine
    ``detect_os`` reads ``/proc/version`` for real, so the expectation is what
    :func:`host_is_wsl` says — ``0`` on the plain Linux runners CI uses, exactly as
    before. These five failed on every WSL2 host until this read the same signal.
    """
    stubs = stub_dir(
        tmp_path,
        "bin",
        "uname",
        body=f'case "$1" in -s) echo {kernel} ;; -m) echo {machine} ;; esac',
    )
    result = sh(
        'detect_os >/dev/null; printf "%s %s %s\\n" "$OS" "$ARCH" "$IS_WSL"',
        path=f"{stubs}:{base_path(tmp_path)}",
    )
    wsl = "1" if host_is_wsl() else "0"
    assert result.stdout.split() == [expected_os, expected_arch, wsl], result.stderr


def test_host_is_wsl_agrees_with_detect_os_on_this_host(tmp_path: Path) -> None:
    """The helper the expectation above leans on is checked against the real function.

    With ``WSL_DISTRO_NAME`` set, both say WSL. With it blank, both fall back to
    ``/proc/version`` — whatever this host's says, the helper and ``detect_os`` must
    agree, or the uname test would be asserting the helper's opinion of the host.
    """
    assert host_is_wsl({"WSL_DISTRO_NAME": "Ubuntu-24.04"}) is True
    stubs = stub_dir(
        tmp_path, "bin", "uname", body='case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac'
    )
    result = sh(
        'detect_os >/dev/null; printf "%s\n" "$IS_WSL"',
        env={"WSL_DISTRO_NAME": ""},
        path=f"{stubs}:{base_path(tmp_path)}",
    )
    expected = "1" if host_is_wsl({"WSL_DISTRO_NAME": ""}) else "0"
    assert result.stdout.strip() == expected, result.stderr


def test_wsl_is_detected_from_the_environment(tmp_path: Path) -> None:
    """`WSL_DISTRO_NAME`, which a shell started by WSL carries."""
    stubs = stub_dir(
        tmp_path, "bin", "uname", body='case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac'
    )
    result = sh(
        'detect_os >/dev/null; printf "%s\\n" "$IS_WSL"',
        env={"WSL_DISTRO_NAME": "Ubuntu-24.04"},
        path=f"{stubs}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == "1", result.stderr


def test_windows_native_is_refused_with_the_wsl_command(tmp_path: Path) -> None:
    """MSYS/Git Bash is a real fork, not a `case` branch (§2).

    The fleet runs agents in tmux and there is no tmux on Windows, so the honest
    answer is the command that installs WSL2 — the same answer
    `services/diagnostics.py install_hint()` already gives on win32. Refusing
    with an explanation beats installing something that cannot run the feature
    the installer just advertised.
    """
    stubs = stub_dir(
        tmp_path,
        "bin",
        "uname",
        body='case "$1" in -s) echo MINGW64_NT-10.0 ;; -m) echo x86_64 ;; esac',
    )
    result = sh("detect_os", path=f"{stubs}:{base_path(tmp_path)}")
    assert result.returncode == 1
    assert "wsl --install" in result.stderr, result.stderr
    assert "install.ps1" in result.stderr, "the refusal should name the shim that does it for you"


# ---------------------------------------------------------------------------
# pkg_manager — the one OS-varying part of the script (§2, §5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        (["apt-get"], "apt"),
        (["dnf"], "dnf"),
        (["pacman"], "pacman"),
        (["zypper"], "zypper"),
        (["apk"], "apk"),
        ([], ""),
    ],
)
def test_pkg_manager_picks_the_platform_manager(
    tmp_path: Path, available: list[str], expected: str
) -> None:
    """One manager present, and the unrecognised machine (§5's last paragraph).

    An unknown platform is not a failure: it degrades to warn-only with the
    three tools named, exactly as `install_hint()` returns all three hints
    rather than a wrong one.
    """
    stubs = stub_dir(tmp_path, "bin", *available, body="echo 'dnf 5.0.0'; exit 0")
    result = sh(
        'OS=linux; pkg_manager; printf "%s\\n" "$PKG"',
        path=f"{stubs}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == expected, result.stderr


def test_the_native_manager_wins_over_homebrew_on_linux(tmp_path: Path) -> None:
    """Homebrew-on-Linux beside apt must not take over /usr's package manager.

    A real shape — Linuxbrew installed for one tool — and picking brew there
    would install tmux into ~/.linuxbrew while the machine's own tmux stayed
    absent, for no benefit.
    """
    stubs = stub_dir(tmp_path, "bin", "apt-get", "brew")
    result = sh(
        'OS=linux; pkg_manager; printf "%s\\n" "$PKG"',
        path=f"{stubs}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == "apt"


def test_brew_is_the_answer_on_macos(tmp_path: Path) -> None:
    stubs = stub_dir(tmp_path, "bin", "brew", "apt-get")
    result = sh(
        'OS=macos; pkg_manager; printf "%s\\n" "$PKG"',
        path=f"{stubs}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == "brew"


@pytest.mark.parametrize(("banner", "expected"), [("dnf 5.2.1", "5"), ("4.14.0", "4")])
def test_the_dnf_major_is_read_because_the_repo_syntax_differs(
    tmp_path: Path, banner: str, expected: str
) -> None:
    """dnf5 spells it `config-manager addrepo`; dnf4 spells it `--add-repo`.

    Fedora 41+ is dnf5, RHEL 9 and derivatives are dnf4, and the `gh` repository
    step branches on this rather than on whether a command errored — so a
    failure is a failure and not a silent fallback.
    """
    stubs = stub_dir(tmp_path, "bin", "dnf", body=f'echo "{banner}"')
    # `dnf_major`, not `$DNF_MAJOR` after `pkg_manager`: the read is LAZY now,
    # because doing it in `pkg_manager` meant the read-only phase invoked a
    # package manager on every Fedora box (see
    # test_the_read_only_phase_calls_no_package_manager).
    result = sh(
        "OS=linux; pkg_manager; dnf_major; echo",
        path=f"{stubs}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == expected, result.stderr


def test_sudo_is_not_used_when_already_root(tmp_path: Path) -> None:
    """§3.7: sudo for one command, and never when it would be pointless."""
    stubs = stub_dir(tmp_path, "bin", "apt-get", "sudo")
    fake_id = stub_dir(tmp_path, "idbin", "id", body='[ "$1" = -u ] && echo 0 || echo root')
    result = sh(
        'OS=linux; pkg_manager; printf "[%s]\\n" "$PKG_SUDO"',
        path=f"{fake_id}:{stubs}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == "[]", result.stderr


# ---------------------------------------------------------------------------
# the Chrome DevTools extra's Node floor — §1.4/§6.3, the boundary that matters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("node_version", "expected"),
    [
        ("v12.22.9", "install"),  # Ubuntu 22.04
        ("v18.20.4", "install"),  # Debian 12
        ("v22.11.0", "install"),  # one minor below the floor
        ("v22.12.0", "current"),  # the floor itself — inclusive
        ("v22.23.2", "current"),
        ("v26.7.0", "current"),
    ],
)
def test_the_chrome_devtools_extra_is_current_from_its_node_floor(
    tmp_path: Path, node_version: str, expected: str
) -> None:
    """chrome-devtools-mcp declares `^20.19.0 || ^22.12.0 || >=23`, and the extra installs
    the 22 line, so 22.11 is below its floor and 22.12 is not.

    An off-by-one here is invisible on every developer machine — they are all far
    above the floor — and would surface only on the one distribution shipping
    exactly 22.11. Under --no-agent, so the Node half is all there is to the extra.
    """
    stubs = stub_dir(tmp_path, "bin", "node", body=f'printf "{node_version}\\n"')
    stub_dir(tmp_path, "bin", "npx")
    result = sh(
        "WANT_AGENT=0; survey >/dev/null 2>&1; "
        "if extra_call current chrome-devtools; then echo current; else echo install; fi",
        env={"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": ""},
        path=f"{stubs}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == expected, (
        f"Node {node_version} -> {result.stdout.strip()}, wanted {expected}\n{result.stderr}"
    )


def test_tmux_is_only_touched_below_its_own_floor(tmp_path: Path) -> None:
    """3.2 is the floor (core/tmux.py MIN_VERSION); 3.3a is fine, 3.0 is not.

    Deliberately NOT the 3.5 recommendation: below 3.5 the fleet works without
    the shifted chords (shift+enter travels as ctrl+J), and reinstalling a
    working tmux to chase a nicety is the kind of unasked-for change §3.9
    exists to prevent.
    """
    for version, expected in (("3.0a", "install"), ("3.2a", "current"), ("3.3a", "current")):
        stubs = stub_dir(tmp_path / version, "bin", "tmux", body=f'printf "tmux {version}\\n"')
        result = sh(
            'survey >/dev/null 2>&1; resolve >/dev/null 2>&1; printf "%s\\n" "$TMUX_ACTION"',
            path=f"{stubs}:{base_path(tmp_path)}",
        )
        assert result.stdout.strip() == expected, f"tmux {version}: {result.stdout!r}"


# ---------------------------------------------------------------------------
# latest_version — reading one field out of PyPI's JSON without a JSON parser
# ---------------------------------------------------------------------------


def test_the_pypi_extraction_ignores_a_version_string_inside_the_readme(tmp_path: Path) -> None:
    """The reason the extraction is anchored rather than greedy (§3.9.2).

    PyPI's payload embeds the whole README in `info.description`, where every
    quote is BACKSLASH-escaped. A greedy `.*"version":"…` takes the LAST match in
    a 44 KB blob; anchoring on `^"version":"` after splitting on commas cannot
    match `\\"version\\":\\"9.9.9\\"` in prose at all. This payload is the
    adversarial case, and it is the shape a README showing JSON output would
    really produce.
    """
    payload = (
        '{"info":{"description":"docs say \\\\"version\\\\":\\\\"9.9.9\\\\" somewhere",'
        '"name":"aisquare-cli","version":"0.6.0"},"last_serial":1}'
    )
    # No python3 on PATH, so the sed path is the one under test — the fallback
    # is what runs on a machine that has no Python yet, which is the whole
    # premise of §3.1.
    curl = stub_dir(tmp_path, "bin", "curl", body=f"cat <<'JSON'\n{payload}\nJSON")
    # HAVE_CURL is what `fetch` branches on and `preflight` is what sets it, so
    # calling one function in isolation has to supply it. Without this the test
    # silently exercised the wget branch against a PATH with no wget.
    result = sh(
        "HAVE_CURL=1; latest_version",
        env={"AISQUARE_INSTALL_VERSION": ""},
        path=f"{curl}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == "0.6.0", (
        f"read {result.stdout.strip()!r} — the greedy form would take 9.9.9\n{result.stderr}"
    )


def test_an_unreachable_pypi_is_reported_and_never_read_as_behind(tmp_path: Path) -> None:
    """§3.9.2: an empty answer means "could not determine", not "out of date".

    The failure that matters is the other one — an installer that treats a
    network blip as "you are behind" and reinstalls on every run.
    """
    curl = stub_dir(tmp_path, "bin", "curl", body="exit 7")
    aisquare = stub_dir(tmp_path, "cli", "aisquare", body='printf "aisquare 0.6.0\\n"')
    result = sh(
        'HAVE_CURL=1; survey >/dev/null 2>&1; resolve 2>&1; printf "ACTION=%s\\n" "$CLI_ACTION"',
        env={"AISQUARE_INSTALL_VERSION": ""},
        path=f"{curl}:{aisquare}:{base_path(tmp_path)}",
    )
    assert "ACTION=current" in result.stdout, result.stdout
    assert "could not read the latest" in result.stdout + result.stderr


def test_offline_does_not_reach_the_network_at_all(tmp_path: Path) -> None:
    """`--offline` is for a machine with no network, so it must not try (§3.9.2).

    A stub that FAILS if called is the assertion: a timeout that reads as a
    broken installer is exactly what this flag exists to avoid.
    """
    marker = tmp_path / "curl-was-called"
    curl = stub_dir(tmp_path, "bin", "curl", "wget", body=f'echo called >"{marker}"\nexit 1')
    aisquare = stub_dir(tmp_path, "cli", "aisquare", body='printf "aisquare 0.6.0\\n"')
    result = sh(
        'OFFLINE=1; survey >/dev/null 2>&1; resolve >/dev/null 2>&1; printf "%s\\n" "$CLI_ACTION"',
        env={"AISQUARE_INSTALL_VERSION": ""},
        path=f"{curl}:{aisquare}:{base_path(tmp_path)}",
    )
    assert not marker.exists(), "--offline still called a downloader"
    assert result.stdout.strip() == "current", result.stderr


# ---------------------------------------------------------------------------
# choose_project — §4, "which project?"
# ---------------------------------------------------------------------------


def test_a_git_repo_at_the_cwd_is_the_project(tmp_path: Path) -> None:
    repo = tmp_path / "myproj"
    (repo / ".git").mkdir(parents=True)
    result = sh('choose_project; printf "%s\\n" "$PROJECT_DIR"', cwd=repo, path=base_path(tmp_path))
    assert result.stdout.strip() == str(repo), result.stderr


def test_the_repo_is_found_from_a_subdirectory_without_git_installed(tmp_path: Path) -> None:
    """The walk-up, and why it cannot use `git`.

    `choose_project` runs BEFORE `install_git`, so on the bare machine this
    installer exists for there is no git to ask — and asking it would mean never
    finding the repository the user is standing in. Measured: the first version
    of this step used `git rev-parse` and registered nothing on every fresh
    container.
    """
    repo = tmp_path / "myproj"
    deep = repo / "src" / "aisquare"
    deep.mkdir(parents=True)
    (repo / ".git").mkdir()
    result = sh('choose_project; printf "%s\\n" "$PROJECT_DIR"', cwd=deep, path=base_path(tmp_path))
    assert result.stdout.strip() == str(repo), result.stderr


def test_a_git_file_counts_as_a_repo(tmp_path: Path) -> None:
    """A worktree and a submodule have a `.git` FILE, not a directory.

    The fleet gives every agent its own `git worktree`, so this is not an exotic
    case for this project — it is where its own agents run.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")
    result = sh(
        'choose_project; printf "%s\\n" "$PROJECT_DIR"', cwd=worktree, path=base_path(tmp_path)
    )
    assert result.stdout.strip() == str(worktree), result.stderr


def test_a_directory_that_is_not_a_repo_registers_nothing(tmp_path: Path) -> None:
    """§4: registering $HOME because someone ran the installer there is a mess
    that persists in the store. No repo means machine setup only."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = sh(
        'choose_project; printf "[%s]\\n" "$PROJECT_DIR"', cwd=plain, path=base_path(tmp_path)
    )
    assert result.stdout.strip() == "[]", result.stderr


def test_no_project_beats_a_repo_underfoot(tmp_path: Path) -> None:
    repo = tmp_path / "myproj"
    (repo / ".git").mkdir(parents=True)
    result = sh(
        'WANT_PROJECT=0; choose_project; printf "[%s]\\n" "$PROJECT_DIR"',
        cwd=repo,
        path=base_path(tmp_path),
    )
    assert result.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# --dry-run, as a guarantee rather than a claim (§3.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [[], ["--with", "chrome-devtools"]], ids=["plain", "with-extra"])
def test_dry_run_runs_nothing(tmp_path: Path, extra: list[str]) -> None:
    """Against a PATH of stubs that record being called and then FAIL.

    `--dry-run` is not a nicety: it is how a person decides whether to trust a
    script they are about to pipe into a shell, and how a reviewer reads this
    one. If any of it actually executed, the flag would be a lie told to exactly
    the most cautious user. With the Chrome DevTools extra too, whose `claude mcp
    add` writes Claude Code's own config.
    """
    log = tmp_path / "calls.log"
    mutators = (
        "uv",
        "apt-get",
        "dnf",
        "pacman",
        "zypper",
        "apk",
        "brew",
        "npm",
        "fnm",
        "claude",
        "curl",
        "wget",
    )
    stubs = stub_dir(
        tmp_path,
        "bin",
        *mutators,
        body=f'printf "%s %s\\n" "$(basename "$0")" "$*" >>"{log}"\nexit 1',
    )
    # `aisquare` answers --version so `survey` sees a machine that already has
    # it; every other invocation of it would be a mutation and is logged.
    cli = stub_dir(
        tmp_path,
        "cli",
        "aisquare",
        body=(
            f'printf "%s %s\\n" aisquare "$*" >>"{log}"\n'
            'case "$1" in --version) printf "aisquare 0.6.0\\n"; exit 0 ;; esac\nexit 1'
        ),
    )
    (tmp_path / "home").mkdir()
    result = sh(
        " ".join(["main --dry-run --yes --no-project --offline", *extra]),
        env={"HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": ""},
        path=f"{stubs}:{cli}:{base_path(tmp_path)}",
    )

    assert result.returncode == 0, f"--dry-run should exit 0:\n{result.stdout}\n{result.stderr}"
    assert "would run:" in result.stdout, result.stdout
    mcp = f"would run: claude {_MCP_ADD}"
    assert (mcp in result.stdout) is bool(extra), result.stdout

    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    # Reading a version is not a mutation, and `survey` is allowed to do it —
    # that is the step whose whole job is to look. Anything else is a write.
    mutations = [
        call for call in calls if call.split()[1:2] not in ([], ["--version"], ["-V"], ["version"])
    ]
    assert not mutations, f"--dry-run executed something: {mutations}\nfull log: {calls}"


def test_dry_run_still_reports_what_it_would_do(tmp_path: Path) -> None:
    """A dry run that printed nothing useful would be worse than none."""
    cli = stub_dir(tmp_path, "cli", "aisquare", body='printf "aisquare 0.1.0\\n"')
    curl = stub_dir(tmp_path, "bin", "curl")
    result = sh(
        "main --dry-run --yes --no-project --offline --no-agent --no-system-deps",
        # The suite's own home is in AISQUARE_HOME, and the banner names the home this
        # run resolves; the default is the one asserted here.
        env={"AISQUARE_HOME": ""},
        path=f"{cli}:{curl}:{base_path(tmp_path)}",
    )
    assert "aisquare installer" in result.stdout
    assert "~/.aisquare/" in result.stdout, "the banner must list what gets written (§3.7)"
    assert "nothing above was actually run" in result.stdout


# ---------------------------------------------------------------------------
# preflight — §3.7 and §0.9
# ---------------------------------------------------------------------------


def _container_sign() -> str | None:
    """What makes install.sh's ``in_container()`` true on this machine whatever a test sets.

    The script also reads ``$container``, which a test clears; it cannot hide these. So the
    same false failure the docker gate met (the test run inside a container, where root is
    allowed by design) is skipped under podman and a cgroup-only container too.
    """
    for marker in ("/.dockerenv", "/run/.containerenv"):
        if Path(marker).is_file():  # the script's `[ -f … ]`
            return marker
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return "/proc/1/cgroup" if re.search(r"docker|lxc|containerd|kubepods", cgroup) else None


_CONTAINER_SIGN = _container_sign()


@pytest.mark.skipif(
    _CONTAINER_SIGN is not None,
    reason=f"inside a container ({_CONTAINER_SIGN} exists): install.sh's in_container() is "
    "true here whatever the test sets, so root is allowed by design (§3.7); the premise "
    "'outside a container' cannot hold",
)
def test_root_is_refused_outside_a_container(tmp_path: Path) -> None:
    """Everything in the Bootstrap and Ours classes lands under $HOME.

    So a root run installs uv, aisquare and Claude Code into ROOT's home and
    leaves the user's own PATH without them — a failure that looks like success
    until the first `aisquare` command. The refusal has to say that, because
    "don't run installers as root" alone reads as superstition.
    """
    fake_id = stub_dir(tmp_path, "idbin", "id", body='[ "$1" = -u ] && echo 0 || echo root')
    result = sh(
        "preflight",
        env={"AISQUARE_INSTALL_ALLOW_ROOT": "0", "container": ""},
        path=f"{fake_id}:{base_path(tmp_path)}",
    )
    assert result.returncode == 1, result.stdout
    assert "root" in result.stderr.lower()
    assert "AISQUARE_INSTALL_ALLOW_ROOT" in result.stderr, "the refusal must name its own override"


def test_root_is_allowed_in_a_container(tmp_path: Path) -> None:
    """A container has no other user, so refusing would make Dockerfiles impossible (§0.9)."""
    fake_id = stub_dir(tmp_path, "idbin", "id", body='[ "$1" = -u ] && echo 0 || echo root')
    curl = stub_dir(tmp_path, "bin", "curl")
    result = sh(
        "preflight",
        env={"container": "podman"},
        path=f"{fake_id}:{curl}:{base_path(tmp_path)}",
    )
    assert result.returncode == 0, result.stderr
    assert "container" in result.stdout


def test_no_downloader_is_a_clear_refusal(tmp_path: Path) -> None:
    """Every installer this script uses is fetched over HTTPS."""
    result = sh("preflight", path=base_path(tmp_path))
    assert result.returncode == 1
    assert "curl" in result.stderr and "wget" in result.stderr


def test_wget_alone_is_enough(tmp_path: Path) -> None:
    """Alpine ships BusyBox wget and no curl — measured. It must not be refused."""
    stubs = stub_dir(tmp_path, "bin", "wget")
    result = sh(
        'preflight >/dev/null; printf "curl=%s wget=%s\\n" "$HAVE_CURL" "$HAVE_WGET"',
        path=f"{stubs}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == "curl=0 wget=1", result.stderr


# ---------------------------------------------------------------------------
# confirm / handoff — §3.3 and §3.5
# ---------------------------------------------------------------------------


def test_no_terminal_takes_the_default_without_prompting(tmp_path: Path) -> None:
    """§0.9: CI, a Dockerfile and a provisioner have no terminal at all.

    The subprocess here has no controlling terminal, which is the real condition
    rather than a simulated one.
    """
    result = sh(
        'if confirm "install a thing?" n; then echo yes; else echo no; fi',
        path=base_path(tmp_path),
        no_terminal=True,
    )
    assert result.stdout.strip() == "no", result.stdout
    assert "install a thing?" not in result.stdout, "it must not print a prompt nobody can answer"


def test_yes_and_no_terminal_both_take_the_stated_default(tmp_path: Path) -> None:
    """`--yes` means "never block on a question", not "answer yes to anything".

    THIS TEST USED TO ASSERT THE OPPOSITE, and its docstring had the reasoning
    backwards: it claimed that collapsing `--yes` with the no-terminal case
    would "make a Dockerfile install Homebrew by accident", when in fact NOT
    collapsing them is what made `--yes` install Homebrew. The only prompt that
    reaches `confirm` has a default of `n` precisely because it installs a
    system-wide package manager.

    The distinction that does matter is kept where it belongs: `handoff` checks
    `ASSUME_YES` separately, because "do not ask me" and "do not launch a TUI at
    me" are genuinely different instructions —
    `test_yes_names_the_ui_rather_than_launching_it` pins that.
    """
    for default, expected in (("n", "no"), ("y", "yes")):
        for unattended in (True, False):
            setup = "ASSUME_YES=1; " if not unattended else ""
            result = sh(
                f'{setup}if confirm "install a thing?" {default}; then echo yes; else echo no; fi',
                path=base_path(tmp_path),
                no_terminal=True,
            )
            assert result.stdout.strip() == expected, (
                f"default={default} unattended={unattended} -> "
                f"{result.stdout.strip()!r}, wanted {expected!r}"
            )


def test_the_handoff_survives_having_no_controlling_terminal(tmp_path: Path) -> None:
    """The §0.9 regression guard, for a bug that was fatal and silent.

    `tty_available` was written as `{ : </dev/tty; } 2>/dev/null`, which reads as
    obviously safe and is not: `:` is a POSIX SPECIAL BUILT-IN, and a redirection
    error on a special built-in makes a non-interactive shell EXIT — not return
    non-zero, and not obey `set -e` or the `if`-condition suspension. Measured
    under bash, which is `/bin/sh` on Fedora, RHEL, Arch and macOS: with no
    controlling terminal the installer died at that line, exit 1, having printed
    nothing since its last step. Under dash it survived, so the bug was
    invisible on Debian and Ubuntu and fatal on everything else.

    NOT CAUGHT BY THE CONTAINER MATRIX, which is why this test exists: every
    cell passes `--yes`, and both `confirm` and `handoff` short-circuit on
    `--yes` before the probe is reached. So the matrix proved five distributions
    install correctly while never once evaluating the line that would break an
    unattended run.

    `handoff` IS CALLED DIRECTLY, with DRY_RUN left at 0. The first version of
    this test ran `main --dry-run` and passed with the bug still in place —
    `handoff` returns at its `--dry-run` branch before it ever looks for a
    terminal, so the test was vacuous. Proven by reverting the fix and watching
    it stay green, which is the only way that kind of hole shows up.
    """
    asq = stub_dir(tmp_path, "cli", "asq", body='printf "THE UI STARTED\\n"')
    result = sh(
        "DOCTOR_AMBER=brain; UNEXPECTED=0; handoff",
        path=f"{asq}:{base_path(tmp_path)}",
        no_terminal=True,
    )

    assert result.returncode == 0, (
        "handoff did not survive having no controlling terminal "
        f"(rc={result.returncode}) — see the docstring\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Open the fleet UI with" in result.stdout, (
        f"it exited before naming the next step:\n{result.stdout}"
    )


def test_no_terminal_means_the_ui_is_named_rather_than_launched(tmp_path: Path) -> None:
    """§3.3: `asq` must not be exec'd where there is no terminal to give it.

    Measured on 0.6.0: bare `aisquare` with a piped stdin prints the usage page
    and exits 2. So an installer that handed over regardless would end a CI run
    by printing a help page and reporting failure — which reads as the install
    itself having failed.
    """
    asq = stub_dir(tmp_path, "cli", "asq", body='printf "THE UI STARTED\\n"')
    result = sh(
        "DOCTOR_AMBER=brain; UNEXPECTED=0; handoff",
        path=f"{asq}:{base_path(tmp_path)}",
        no_terminal=True,
    )
    assert "THE UI STARTED" not in result.stdout, (
        f"the UI was launched with no terminal to read:\n{result.stdout}"
    )
    assert "asq" in result.stdout, "it should still tell the user the command"


def test_yes_names_the_ui_rather_than_launching_it(tmp_path: Path) -> None:
    """`--yes` means "do not stop to ask me", and so it does NOT hand over (§3.5).

    A provisioning run that ended in a full-screen TUI would hang the thing that
    invoked it. This is the reason `--yes` and "no terminal" are separate
    conditions in `handoff` rather than one flag.
    """
    asq = stub_dir(tmp_path, "cli", "asq", body='printf "THE UI STARTED\\n"')
    result = sh(
        "ASSUME_YES=1; DOCTOR_AMBER=brain; UNEXPECTED=0; handoff",
        path=f"{asq}:{base_path(tmp_path)}",
    )
    assert result.returncode == 0, result.stderr
    assert "THE UI STARTED" not in result.stdout, result.stdout


def test_an_unexpected_amber_check_exits_2(tmp_path: Path) -> None:
    """§3.8: exit 0 onto a broken machine is worse than never running.

    0 means installed and nothing unexpected; 1 means a fatal step failed; 2
    means the install finished but something is amber for a reason the script
    did not anticipate. A caller can act on that distinction; a single "did it
    work?" bit cannot.
    """
    result = sh(
        "UNEXPECTED=1; handoff",
        path=base_path(tmp_path),
        no_terminal=True,
    )
    assert result.returncode == 2, f"rc={result.returncode}, wanted 2\n{result.stdout}"


# ---------------------------------------------------------------------------
# The one shape nothing else covers: a HUMAN at a terminal, with the script
# itself on stdin. §0.5 and §3.3.
# ---------------------------------------------------------------------------


def _piped_into_sh_with_a_terminal(
    answer: str, *, path: str, home: Path, extra: list[str] | None = None
) -> str:
    """Run install.sh exactly as `curl … | sh` does, with a real terminal.

    THE SHAPE IS THE TEST, and no other test in this repo reproduces it:

        stdin           a PIPE carrying the script's own bytes
        stdout/stderr   a terminal
        /dev/tty        the same terminal, and openable

    That is the state §3.3 is entirely about, and it is the one state where a
    `read -r answer` with no `< /dev/tty` does its damage — it consumes the rest
    of the script instead of the person's answer. Under the container matrix
    stdin is a pipe but there is no terminal; under `pytest` there is neither. So
    a pty is the only way to reach it, and `answer` is typed into that pty the
    way a person would type it.

    `pty.fork()` rather than `subprocess`, because the child has to be a session
    leader with the pty as its CONTROLLING terminal — not merely have it on fd
    1. `start_new_session=True` plus a pty on stdout gives the first and not the
    second, and `/dev/tty` would then fail to open, which would quietly turn this
    into the no-terminal test that already exists.
    """
    script = SCRIPT.read_bytes()
    argv = [SH, "-s", "--", *(extra or [])]

    environment = dict(os.environ)
    environment.update(
        {
            "PATH": path,
            "HOME": str(home),
            "NO_COLOR": "1",
            "AISQUARE_INSTALL_LIB": "0",
            "AISQUARE_INSTALL_VERSION": "",
        }
    )

    # The module-level skip above guarantees this; the assert is what tells
    # MYPY so, since the suite is type-checked under Windows too now and `pty`
    # is POSIX-only in typeshed.
    assert sys.platform != "win32"
    import pty

    read_end, write_end = os.pipe()
    pid, master = pty.fork()
    if pid == 0:  # child
        try:
            os.close(write_end)
            os.dup2(read_end, 0)  # stdin IS the pipe — the whole point
            os.close(read_end)
            os.execve(argv[0], argv, environment)
        finally:  # pragma: no cover - only reached if exec fails
            os._exit(127)

    os.close(read_end)
    # Feed the script down the pipe and close it, exactly as curl finishing does.
    with os.fdopen(write_end, "wb") as pipe:
        pipe.write(script)

    # Type the answer at the terminal.
    os.write(master, answer.encode())

    output = b""
    try:
        while True:
            chunk = os.read(master, 4096)
            if not chunk:
                break
            output += chunk
    except OSError:
        # EIO on Linux when the child closes the slave side. Expected.
        pass
    os.waitpid(pid, 0)
    os.close(master)
    return output.decode(errors="replace")


#: The flags the three pty tests run with. `--force` is load-bearing: without
#: it these machines now SHORT-CIRCUIT — a `--no-system-deps` run with a current
#: CLI genuinely has nothing to do, which is the correct behaviour the review
#: asked for — and `handoff`, the thing under test, is never reached. `--force`
#: is the documented way to say "do the work anyway", so it puts the run back on
#: the path that ends at the prompt.
_HANDOFF_FLAGS = [
    "--no-project",
    "--no-agent",
    "--no-system-deps",
    "--offline",
    "--force",
]

#: One doctor payload in the target state — `brain` the only non-ok check — so
#: the stub machine reaches the happy summary and the closing prompt.
_DOCTOR_PAYLOAD = (
    '[{"name": "home", "status": "ok", "detail": "ok", "fix": null},'
    '{"name": "brain", "status": "warn", "detail": "gbrain not found", "fix": "optional"}]'
)


@pytest.fixture
def piped_machine(tmp_path: Path) -> tuple[str, Path]:
    """A stubbed machine that install.sh will run to completion against.

    Everything that would touch the real system is a stub, so `main` reaches
    `handoff` — which is the only step under test here — without installing
    anything. `asq` reports whether ITS stdin is a terminal, which is the
    assertion the second test rests on.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # `preflight` requires a downloader — every installer this script uses is
    # fetched over HTTPS — so the stub machine needs one even though --offline
    # means nothing is actually fetched.
    (bin_dir / "curl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "uv").write_text(
        '#!/bin/sh\ncase "$1" in --version) echo "uv 0.12.3 (stub)" ;; esac\nexit 0\n',
        encoding="utf-8",
    )
    (bin_dir / "aisquare").write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  --version) echo "aisquare 0.6.0" ;;\n'
        # A payload whose only non-ok check is `brain`: the target state, so the
        # summary is the happy one and the prompt is reached.
        "  --json) printf '%s' '" + _DOCTOR_PAYLOAD + "' ;;\n"
        "esac\nexit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "asq").write_text(
        "#!/bin/sh\n"
        "if [ -t 0 ]; then\n"
        '  echo "UI-STARTED stdin=terminal"\n'
        "else\n"
        '  echo "UI-STARTED stdin=NOT-a-terminal"\n'
        "fi\n",
        encoding="utf-8",
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    return f"{bin_dir}:{base_path(tmp_path)}", home


def test_the_prompt_reads_the_terminal_and_not_the_script(
    piped_machine: tuple[str, Path],
) -> None:
    """Answering `n` at the prompt must decline — not consume the script.

    THE BUG THIS EXISTS FOR (§3.3, §10): with `read -r answer` and no
    `< /dev/tty`, stdin is the pipe carrying install.sh, so the read either eats
    the remainder of the script or hits EOF and returns instantly — the prompt
    appears to answer itself and the run ends. Invisible in every other test
    here, and the first thing a real user would hit.

    A declined prompt must still name the command, because the user has a
    working install either way.
    """
    path, home = piped_machine
    output = _piped_into_sh_with_a_terminal(
        "n\n",
        path=path,
        home=home,
        extra=_HANDOFF_FLAGS,
    )

    assert "Open the aisquare fleet UI now?" in output, (
        f"the prompt never appeared — the read did not reach a terminal:\n{output}"
    )
    assert "UI-STARTED" not in output, f"answering 'n' still launched the UI:\n{output}"
    assert "Open it any time with" in output, (
        f"a declined prompt must still name the command:\n{output}"
    )


def test_answering_yes_hands_over_with_the_terminal_reconnected(
    piped_machine: tuple[str, Path],
) -> None:
    """`exec asq < /dev/tty` — and the `< /dev/tty` half is what is asserted.

    Measured on 0.6.0: bare `aisquare` with a piped stdin prints the usage page
    and exits 2. So handing over WITHOUT reconnecting the terminal would end the
    installer by printing a help page and reporting failure — the worst possible
    last impression, and one that reads as the install having failed rather than
    as a handoff bug.

    The `asq` stub reports whether its own stdin is a terminal, so this
    distinguishes "the UI started" from "the UI started correctly". Nothing else
    can: with the redirection missing the UI still starts, and still looks fine
    in a log.
    """
    path, home = piped_machine
    output = _piped_into_sh_with_a_terminal(
        "y\n",
        path=path,
        home=home,
        extra=_HANDOFF_FLAGS,
    )

    assert "UI-STARTED" in output, f"answering 'y' did not hand over:\n{output}"
    assert "UI-STARTED stdin=terminal" in output, (
        "the UI was exec'd with the SCRIPT's pipe still on its stdin rather than "
        f"the terminal — see §3.3:\n{output}"
    )


def test_an_empty_answer_takes_the_default_and_opens_the_ui(
    piped_machine: tuple[str, Path],
) -> None:
    """The prompt is `[Y/n]`, so a bare Enter means yes (§0.5).

    Worth pinning separately: pressing Enter is what most people do, and a
    default that silently flipped to `n` would make the closing promise of the
    whole feature stop working without any error.
    """
    path, home = piped_machine
    output = _piped_into_sh_with_a_terminal(
        "\n",
        path=path,
        home=home,
        extra=_HANDOFF_FLAGS,
    )

    assert "[Y/n]" in output, f"the prompt must show Y as the default:\n{output}"
    assert "UI-STARTED stdin=terminal" in output, f"a bare Enter should open the UI:\n{output}"


# ---------------------------------------------------------------------------
# expected_amber — which amber lines are the REQUESTED state, not a defect
# ---------------------------------------------------------------------------


def test_the_expected_amber_is_brain_and_the_releases_snapshot_rows(tmp_path: Path) -> None:
    """`brain`, and the installed release's rows for the codebase snapshot this tree
    removed (`repomix`, `snapshot`, `tiktoken`), with a project or without one: the
    script installs the latest release, whose doctor still has them (review of #399,
    MAJOR 1). This tree's own doctor has none, and an expected row that is not amber is
    good news, so a cell grading this tree is unaffected."""
    for setup in ("WANT_PROJECT=0", 'WANT_PROJECT=1; PROJECT_DIR=""', "PROJECT_DIR=/p"):
        result = sh(f"{setup}; expected_amber; echo", path=base_path(tmp_path))
        assert result.stdout.strip() == "brain repomix snapshot tiktoken", (
            f"with `{setup}` expected_amber said {result.stdout.strip()!r}\n{result.stderr}"
        )
    flags = sh("WANT_AGENT=0; WANT_SYSTEM_DEPS=0; expected_amber; echo", path=base_path(tmp_path))
    assert flags.stdout.strip() == "brain claude-code gh repomix snapshot tiktoken tmux", (
        "alphabetical, as doctor_amber sorts"
    )


def test_doctor_amber_is_sorted_so_the_comparison_is_about_the_set(tmp_path: Path) -> None:
    """A reordering inside `doctor()` is not a regression and must not read as one.

    The short-circuit compares the amber list against `expected_amber` as a
    STRING, so both sides have to be sorted or a harmless reshuffle of the check
    order would silently disable the whole no-op path.
    """
    payload = (
        '[{"name": "gh", "status": "warn", "detail": "x", "fix": "y"},'
        '{"name": "home", "status": "ok", "detail": "x", "fix": null},'
        '{"name": "brain", "status": "warn", "detail": "x", "fix": "y"}]'
    )
    cli = stub_dir(
        tmp_path,
        "cli",
        "aisquare",
        body=f"case \"$1\" in --json) printf '%s' '{payload}' ;; esac\nexit 0",
    )
    result = sh("doctor_amber; echo", path=f"{cli}:{base_path(tmp_path)}")
    assert result.stdout.strip() == "brain gh", (
        f"doctor_amber returned {result.stdout.strip()!r} — it must sort, or the "
        f"short-circuit breaks on a reordering\n{result.stderr}"
    )


@pytest.mark.parametrize("node", [True, False], ids=["with-node", "without-node"])
def test_the_short_circuit_fires_for_a_current_no_project_machine(
    tmp_path: Path, node: bool
) -> None:
    """§3.9.4 end to end, in the mode where it was unreachable.

    A machine that has everything, with `--no-project`, must print its summary
    and exit 0 having installed nothing. The stubs record any call that is not a
    version read, so "installed nothing" is observed rather than asserted. Node is
    an optional extra, so a machine without one is current too, and is told how to
    add it.
    """
    log = tmp_path / "calls.log"
    record = f'printf "%s %s\\n" "$(basename "$0")" "$*" >>"{log}"\nexit 1'
    tools = stub_dir(tmp_path, "bin", "apt-get", "dnf", "pacman", "apk", "brew", body=record)
    versions = tmp_path / "versions"
    versions.mkdir()
    for name, output in (
        ("uv", "uv 0.12.3 (stub)"),
        ("tmux", "tmux 3.7c"),
        ("gh", "gh version 2.97.0 (2026-07-31)"),
        ("git", "git version 2.55.0"),
        *([("node", "v26.7.0"), ("npx", "10.9.2")] if node else []),
        ("curl", ""),
    ):
        script = versions / name
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "{output}"\nexit 0\n', encoding="utf-8")
        script.chmod(0o755)
    cli = stub_dir(
        tmp_path,
        "cli",
        "aisquare",
        body=(
            'case "$1" in\n'
            '  --version) printf "aisquare 0.6.0\\n"; exit 0 ;;\n'
            f"  --json) printf '%s' '{_DOCTOR_PAYLOAD}'; exit 0 ;;\n"
            "esac\n"
            f'printf "%s %s\\n" aisquare "$*" >>"{log}"\nexit 1'
        ),
    )
    asq = stub_dir(tmp_path, "asqbin", "asq")

    result = sh(
        "main --yes --offline --no-project --no-agent",
        # The helper's default pin has to be cleared here: a pinned version is
        # a version to move TO, so `resolve` calls the CLI "behind" and the
        # short-circuit cannot fire. Found by this test failing with the
        # §3.9.1 verification error — which is itself a small proof that the
        # upgrade check works, since it caught a version that had not moved.
        env={"AISQUARE_INSTALL_VERSION": ""},
        path=f"{versions}:{tools}:{cli}:{asq}:{base_path(tmp_path)}",
        no_terminal=True,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "Nothing to do" in result.stdout, (
        f"a fully current --no-project machine must short-circuit:\n{result.stdout}"
    )
    assert "except brain (gbrain is out of scope)" in result.stdout, result.stdout
    offered = (
        "optional: the Chrome DevTools MCP for browser tools: Node 22.12+ (--no-agent: not"
        " registered with Claude Code) — rerun with --with chrome-devtools"
    )
    assert (offered in result.stdout) is not node, result.stdout
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert not calls, f"the short-circuit still ran something: {calls}"


# ---------------------------------------------------------------------------
# Findings from the code review of 2026-09-08. Each of these fires on a bug
# that shipped in the first implementation and that no existing test could see.
# ---------------------------------------------------------------------------


def test_a_failed_download_is_not_reported_as_a_successful_install(tmp_path: Path) -> None:
    """`curl … | sh` hands you the SHELL's exit status, not curl's.

    When the fetch fails the interpreter reads an empty stdin and exits 0, so
    `fetch_into_shell` returned success on a download that never happened.
    Every caller then misdiagnosed it — `install_uv`'s carefully written `die`
    about the uv installer was unreachable, and the user got "uv installed but
    is not on PATH — open a new shell", which is false and sends them chasing a
    PATH problem that does not exist. Same for Claude Code, fnm and Homebrew.

    `set -o pipefail` is not POSIX (dash rejects it), so the fix is a file: the
    fetch's status and the interpreter's status are then separate things.
    """
    failing = stub_dir(tmp_path, "bin", "curl", "wget", body="exit 22")
    result = sh(
        'HAVE_CURL=1; if fetch_into_shell "https://example.invalid/x" sh; '
        "then echo SUCCEEDED; else echo FAILED; fi",
        path=f"{failing}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == "FAILED", (
        "a download that exited 22 was reported as a successful install: "
        f"{result.stdout!r}\n{result.stderr}"
    )


def test_an_empty_download_is_a_failure_too(tmp_path: Path) -> None:
    """A downloader that exits 0 having written nothing is not a success.

    The redirect creates the file before the transfer, so a fetch that dies
    mid-way leaves a zero-byte one behind — and an empty script is a silent
    no-op rather than an error. That is also the shape that poisoned the `gh`
    keyring permanently.
    """
    empty = stub_dir(tmp_path, "bin", "curl", body="exit 0")
    result = sh(
        'HAVE_CURL=1; if fetch_to_file "https://example.invalid/x" >/dev/null; '
        "then echo SUCCEEDED; else echo FAILED; fi",
        path=f"{empty}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == "FAILED", result.stdout


def test_the_temporary_file_is_not_a_predictable_name(source_text: str) -> None:
    """`$$` in /tmp is a symlink target and a race window — and one of these
    files is executed BY ROOT (`_nodesource_setup`).

    `$$` is a small guessable number and /tmp is world-writable, so a local
    unprivileged attacker can pre-create the path (mode 666, or a symlink) and
    have their content run as root. `mktemp` answers both halves: an
    unpredictable name and O_EXCL creation at mode 600.
    """
    offenders = [
        line.strip()
        for line in source_text.splitlines()
        if not line.lstrip().startswith("#") and re.search(r"(TMPDIR|/tmp)[^\n]*\$\$", line)
    ]
    assert not offenders, f"a predictable temporary filename in a shared directory: {offenders}"
    assert "mktemp" in source_text, "fetch_to_file should be using mktemp"


def test_a_doctor_that_cannot_be_asked_blocks_the_short_circuit(tmp_path: Path) -> None:
    """§3.8, and the worst way to get it wrong.

    `doctor_amber` returns non-zero when it cannot get a trustworthy payload,
    but `short_circuit` discarded that with `|| true` — so `_amber` came back
    empty, the "is anything unexpected?" loop never ran, and a machine whose
    `doctor` crashes read as perfectly healthy. Measured output: "doctor: every
    check ok / Nothing to do", exit 0.

    An unanswerable doctor is a reason to do the work, not to skip it.
    """
    broken = stub_dir(
        tmp_path,
        "cli",
        "aisquare",
        body='case "$1" in --version) echo "aisquare 0.6.0"; exit 0 ;; esac\nexit 1',
    )
    versions = tmp_path / "v"
    versions.mkdir()
    for name, out in (
        ("uv", "uv 0.12.3"),
        ("tmux", "tmux 3.7c"),
        ("gh", "gh version 2.97.0 (x)"),
        ("git", "git version 2.55.0"),
        ("node", "v26.7.0"),
        ("curl", ""),
    ):
        script = versions / name
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "{out}"\nexit 0\n', encoding="utf-8")
        script.chmod(0o755)

    result = sh(
        "WANT_AGENT=0; WANT_PROJECT=0; OFFLINE=1; "
        "survey >/dev/null 2>&1; resolve >/dev/null 2>&1; "
        "if short_circuit; then echo CLAIMED_HEALTHY; else echo REFUSED; fi",
        env={"AISQUARE_INSTALL_VERSION": ""},
        path=f"{versions}:{broken}:{base_path(tmp_path)}",
    )
    assert "REFUSED" in result.stdout, (
        "a broken doctor was read as a healthy machine — the exact §3.8 failure "
        f"this feature exists to prevent:\n{result.stdout}"
    )


def test_a_truncated_doctor_payload_blocks_the_short_circuit(tmp_path: Path) -> None:
    """A `{` inside a check's detail splits an object and loses it.

    `run_doctor` cross-checked the name count against the status count;
    `short_circuit` did not. "The extraction dropped something" must never
    present as "nothing is amber".
    """
    # Three names, two statuses: a payload the extraction cannot be trusted on.
    payload = (
        '[{"name": "a", "status": "ok", "detail": "x"},'
        '{"name": "b", "detail": "y"},'
        '{"name": "c", "status": "ok", "detail": "z"}]'
    )
    cli = stub_dir(
        tmp_path,
        "cli",
        "aisquare",
        body=(
            'case "$1" in\n'
            '  --version) echo "aisquare 0.6.0"; exit 0 ;;\n'
            f"  --json) printf '%s' '{payload}'; exit 0 ;;\n"
            "esac\nexit 0"
        ),
    )
    result = sh(
        "if doctor_amber >/dev/null 2>&1; then echo TRUSTED; else echo DISTRUSTED; fi",
        path=f"{cli}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == "DISTRUSTED", result.stdout


def test_yes_takes_the_default_so_a_mac_is_not_given_homebrew_unasked(tmp_path: Path) -> None:
    """`--yes` means "never block on a question", NOT "answer yes to anything".

    `confirm` returned 0 unconditionally under `--yes`, and the only prompt that
    reaches it has a default of `n` because it installs **Homebrew**. So
    `sh install.sh --yes` on a Mac at a terminal silently installed a
    system-wide package manager — the opposite of the answer given to §11.3.
    CI could not see it: macos-latest ships Homebrew, so the prompt is never
    reached there.

    The distinction that does matter is kept where it belongs: `handoff` checks
    `ASSUME_YES` separately, because "do not ask me" and "do not launch a TUI at
    me" are different instructions.
    """
    for default, expected in (("n", "no"), ("y", "yes")):
        result = sh(
            f'ASSUME_YES=1; if confirm "big irreversible thing?" {default}; '
            "then echo yes; else echo no; fi",
            path=base_path(tmp_path),
            no_terminal=True,
        )
        assert result.stdout.strip() == expected, (
            f"--yes with a default of {default!r} answered "
            f"{result.stdout.strip()!r}, wanted {expected!r}"
        )


def test_no_system_deps_expects_the_checks_it_skipped(tmp_path: Path) -> None:
    """`--no-system-deps` asked for those tools to be absent.

    Their amber lines are the requested state, not a surprise. Before this,
    `tmux` and the packer's row were classed UNEXPECTED, `UNEXPECTED` was incremented
    twice and `handoff` exited **2** — whose documented meaning is "amber for a
    reason this script did not expect". The script expected them exactly; the
    user typed the flag. So every `--yes --no-system-deps` provisioning run
    failed its caller, on every invocation.
    """
    result = sh(
        "WANT_SYSTEM_DEPS=0; WANT_PROJECT=0; expected_amber; echo",
        path=base_path(tmp_path),
    )
    expected = set(result.stdout.split())
    assert {"tmux", "gh"} <= expected, (
        f"--no-system-deps must expect the checks it skipped, got {sorted(expected)}"
    )
    for name in ("tmux", "gh"):
        member = sh(
            f"WANT_SYSTEM_DEPS=0; WANT_PROJECT=0; "
            f"if is_expected_amber {name}; then echo yes; else echo no; fi",
            path=base_path(tmp_path),
        )
        assert member.stdout.strip() == "yes", name


def _summary_then_handoff(
    tmp_path: Path,
    assignments: str,
    *,
    env: dict[str, str] | None = None,
    npx: bool = True,
) -> subprocess.CompletedProcess[str]:
    """`summary` then `handoff` after a run with a project; ``assignments`` say the rest.
    An `npx` is on PATH unless ``npx=False``: the Chrome DevTools extra needs one."""
    path = base_path(tmp_path)
    if npx:
        path = f"{stub_dir(tmp_path, 'npxbin', 'npx')}:{path}"
    return sh(
        f"DRY_RUN=0; WANT_PROJECT=1; PROJECT_DIR=/p; UNEXPECTED=0; {assignments}; "
        'summary; echo "UNEXPECTED=$UNEXPECTED"; handoff',
        env=env,
        path=path,
        no_terminal=True,
    )


#: What the summary says of the Chrome DevTools extra when it was chosen and its Node is
#: not there.
_NODE_FAILED = (
    "chrome-devtools — you chose the Chrome DevTools MCP for browser tools: Node 22.12+, "
    "registered with Claude Code, and it did not install"
)


def test_an_extra_this_run_chose_and_could_not_install_is_unexpected(tmp_path: Path) -> None:
    """The Chrome DevTools extra chosen, by `--with chrome-devtools` or at its prompt, and its
    Node still missing after the run: exit 2, and the summary names it.

    No doctor row can: Node is optional, so a machine without it is green (review of #244,
    finding 1, when the packer's row was what turned a failed Node install into exit 2).
    The installer knows it was asked for the extra, so it says so itself.
    """
    result = _summary_then_handoff(
        tmp_path, "CHOSEN_EXTRAS=chrome-devtools; NODE_VERSION=; DOCTOR_AMBER=brain"
    )

    assert result.returncode == 2, result.stdout
    assert "UNEXPECTED=1" in result.stdout
    assert _NODE_FAILED in result.stdout


def test_a_usage_back_off_at_install_time_is_expected_not_amber(tmp_path: Path) -> None:
    """The tester's pass on #401, (e): an account whose usage reads were backed off after a
    429 (doctor's `claude-account-backoff`) was "Not expected, and worth a look" and exited
    2, so a transient rate limit turned a good install amber. It says nothing about the
    install: expected, explained, exit 0. Another amber row still exits 2 (control)."""
    result = _summary_then_handoff(
        tmp_path, "WANT_SYSTEM_DEPS=1; DOCTOR_AMBER='brain claude-account-backoff'", node=True
    )
    control = _summary_then_handoff(
        tmp_path, "WANT_SYSTEM_DEPS=1; DOCTOR_AMBER='brain claude-accounts'", node=True
    )

    assert "UNEXPECTED=0" in result.stdout and result.returncode == 0, result.stdout
    assert "claude-account-backoff — an account's usage reads wait out a rate" in result.stdout
    assert "Not expected" not in result.stdout
    assert control.returncode == 2 and "UNEXPECTED=1" in control.stdout, control.stdout


@pytest.mark.parametrize(
    ("assignments", "npx", "unexpected"),
    [
        ("CHOSEN_EXTRAS=chrome-devtools; NODE_VERSION=22.12.0; DOCTOR_AMBER=brain", True, 0),
        # Not chosen: a machine with no Node is what the opt-in leaves.
        ("CHOSEN_EXTRAS=; NODE_VERSION=; DOCTOR_AMBER=brain", False, 0),
        ("WANT_SYSTEM_DEPS=0; CHOSEN_EXTRAS=; NODE_VERSION=; DOCTOR_AMBER=brain", False, 0),
        # --dry-run installs nothing, so nothing failed to install.
        ("CHOSEN_EXTRAS=chrome-devtools; NODE_VERSION=; DOCTOR_AMBER=brain; DRY_RUN=1", False, 0),
        # Chosen, and what the run left is below the floor: it did not install.
        ("CHOSEN_EXTRAS=chrome-devtools; NODE_VERSION=22.11.0; DOCTOR_AMBER=brain", True, 1),
        # Chosen, Node at the floor, and no npx to start the MCP with (review of #399,
        # m5): npm did not install, and that is the extra not installing.
        ("CHOSEN_EXTRAS=chrome-devtools; NODE_VERSION=22.12.0; DOCTOR_AMBER=brain", False, 1),
    ],
    ids=["installed", "not-chosen", "no-system-deps", "dry-run", "too-old", "no-npx"],
)
def test_only_a_chosen_extra_that_is_missing_is_named(
    tmp_path: Path, assignments: str, npx: bool, unexpected: int
) -> None:
    result = _summary_then_handoff(tmp_path, assignments, npx=npx)

    assert f"UNEXPECTED={unexpected}" in result.stdout, result.stdout
    assert result.returncode == (2 if unexpected else 0)
    assert ("did not install" in result.stdout) is bool(unexpected), result.stdout


def test_a_missing_extra_alone_is_not_sent_to_the_doctor(tmp_path: Path) -> None:
    """No doctor row is about an extra, so for one that did not install the summary's
    "the full detail and a fix for each: aisquare doctor" sent people nowhere (review of
    #257). The pointer follows only checks the doctor explains, and the extra's line,
    which points at the warning its step printed, comes after it.
    """
    alone = _summary_then_handoff(
        tmp_path, "CHOSEN_EXTRAS=chrome-devtools; NODE_VERSION=; DOCTOR_AMBER=brain"
    )
    both = _summary_then_handoff(
        tmp_path, "CHOSEN_EXTRAS=chrome-devtools; NODE_VERSION=; DOCTOR_AMBER='brain python'"
    )

    assert _NODE_FAILED in alone.stdout
    assert "aisquare doctor" not in alone.stdout, alone.stdout
    assert alone.returncode == 2
    pointer = both.stdout.index("the full detail and a fix for each: aisquare doctor")
    assert both.stdout.index("  python\n") < pointer < both.stdout.index(_NODE_FAILED)
    assert "  chrome-devtools\n" not in both.stdout, "an extra is not listed as a doctor check"
    assert both.returncode == 2


def test_exit_2_is_documented_for_an_extra_that_did_not_install(tmp_path: Path) -> None:
    """The run exits 2 for an extra it was asked for and could not install, with no check
    amber (review of #257), and only for what it installs: a failed registration with
    Claude Code warns and exits 0, the owner's call (review of #399, n7)."""
    usage = sh("usage", path=base_path(tmp_path)).stdout
    exit_codes = " ".join(usage.split("Exit codes", 1)[1].split("\n\n", 1)[0].split())
    page = " ".join((REPO / "docs" / "install.md").read_text(encoding="utf-8").split())

    assert "a fatal step failed" in exit_codes, "control: this is the exit-code section"
    said = (
        "or an optional extra you chose did not install: Node or npx for {}--with "
        "chrome-devtools{}. Its Claude Code registration only warns."
    )
    assert said.format("", "") in exit_codes, exit_codes
    assert "unexpectedly amber, " + said.format("`", "`") in page


# ---------------------------------------------------------------------------
# the optional extras — opt-in, one prompt each
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("assignments", "installed"),
    [
        ("ASSUME_YES=1", False),  # --yes asks nothing, so it chooses nothing
        ("ASSUME_YES=0", False),  # no terminal to ask on: the default, no
        ("ASSUME_YES=1; CHOSEN_EXTRAS=chrome-devtools", True),  # --with needs no prompt
        ("ASSUME_YES=1; WANT_SYSTEM_DEPS=0; CHOSEN_EXTRAS=chrome-devtools", True),  # named wins
    ],
    ids=["yes", "no-terminal", "with", "with-no-system-deps"],
)
def test_an_extra_is_installed_only_when_chosen(
    tmp_path: Path, assignments: str, installed: bool
) -> None:
    result = sh(
        f"{assignments}; NODE_VERSION=; install_node() {{ echo NODE-INSTALLED; }}; install_extras",
        path=base_path(tmp_path),
        no_terminal=True,
    )

    assert ("NODE-INSTALLED" in result.stdout) is installed, result.stdout + result.stderr
    if not installed:
        assert "optional; --with chrome-devtools adds it" in result.stdout + result.stderr


def test_no_system_deps_says_a_named_extra_still_installs(tmp_path: Path) -> None:
    """For provisioning scripts: --no-system-deps asks about no extra, but one named with
    --with wins (the `with-no-system-deps` case above), and that one still uses the
    package manager and sudo. The help and the page say so (review of #399, n13)."""
    usage = " ".join(sh("usage", path=base_path(tmp_path)).stdout.split())
    page = " ".join((REPO / "docs" / "install.md").read_text(encoding="utf-8").split())

    said = (
        "An extra named with {0}--with{0} still installs, with the package manager and {0}sudo{0}."
    )
    assert "ask about no optional extra. " + said.format("") in usage, usage
    assert "ask about no optional extra. " + said.format("`") in page


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (
            0,
            "npm installed but npx is not on PATH. The Chrome DevTools MCP is started with"
            " npx. Put the directory that holds npm's npx on PATH.",
        ),
        (
            100,
            "npm did not install. The Chrome DevTools MCP is started with npx. Install npm by"
            " hand: sudo apt install npm",
        ),
    ],
    ids=["npm-installed-no-npx", "npm-failed"],
)
def test_npm_is_installed_only_for_a_run_that_chose_the_extra_and_a_failure_is_said(
    tmp_path: Path, status: int, said: str
) -> None:
    """A Node at the floor with no npx (npm is its own package on Arch, Alpine and
    Debian's nodejs): a run that did not choose the extra installs nothing, and one that
    did installs npm beside it. When that brings no npx, that is said, where it happened
    (review of #399, m5), and as it happened: npm failing, or npm in and npx still not on
    PATH (n12). The summary's "see the warning above" points at it."""
    log = tmp_path / "calls.log"
    pkg = stub_dir(tmp_path, "pkg", "apt-get", body=f'printf "%s\\n" "$*" >>"{log}"\nexit {status}')

    def run(chosen: str) -> str:
        result = sh(
            f"PKG=apt; PKG_SUDO=; NODE_VERSION=22.12.0; CHOSEN_EXTRAS={chosen}; install_extras",
            path=f"{pkg}:{base_path(tmp_path)}",
            no_terminal=True,
        )
        return result.stdout + result.stderr

    unchosen = run("")
    assert "not installing the Chrome DevTools MCP" in unchosen, unchosen
    assert not log.exists(), "npm was installed for a run that did not choose the extra"
    chosen = " ".join(run("chrome-devtools").split())
    assert "Node 22.12.0 — current" in chosen
    assert "install -y npm" in log.read_text(encoding="utf-8"), "the chosen run gets its npx"
    assert f"Node 22.12.0 is here without npx: {said}" in chosen, chosen


@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "dry-run"])
def test_no_package_manager_for_npm_is_said_as_it_is(tmp_path: Path, dry_run: bool) -> None:
    """No package manager, so nothing was tried: the line says so rather than "npm did not
    install". A dry run says what the run would warn and counts no warning (review of
    #399, n10)."""
    result = sh(
        f"DRY_RUN={int(dry_run)}; PKG=; NODE_VERSION=22.12.0; CHOSEN_EXTRAS=chrome-devtools; "
        'install_extras; echo "WARNINGS=$WARNINGS"',
        path=base_path(tmp_path),
        no_terminal=True,
    )
    said = " ".join((result.stdout + result.stderr).split())

    assert "npm did not install" not in said
    reason = "Node 22.12.0 is here without npx: there is no package manager to install npm with"
    if dry_run:
        assert f"would warn: {reason}. Install npm by hand: apt install npm" in said, said
        assert "WARNINGS=0" in said
    else:
        assert f"{reason}. The Chrome DevTools MCP is started with npx." in said, said
        assert "WARNINGS=1" in said


@pytest.mark.parametrize("flag", [["--with", "chrome-devtools"], ["--with=chrome-devtools"]])
def test_with_chooses_an_extra_and_refuses_one_there_is_not(
    tmp_path: Path, flag: list[str]
) -> None:
    quoted = " ".join(flag)
    chosen = sh(f'parse_args {quoted}; printf "[%s]\\n" "$CHOSEN_EXTRAS"', path=base_path(tmp_path))
    refused = sh("parse_args --with nope; echo PARSED", path=base_path(tmp_path))

    assert chosen.stdout.strip() == "[ chrome-devtools]", chosen.stdout + chosen.stderr
    assert refused.returncode != 0 and "PARSED" not in refused.stdout
    assert "--with nope: not an optional extra (there is: chrome-devtools)" in refused.stderr


def test_an_extra_named_twice_is_chosen_once(tmp_path: Path) -> None:
    """`--with X --with=X`, and X does not install: one unexpected, named once (review of
    #399, n3)."""
    result = _summary_then_handoff(
        tmp_path,
        "parse_args --with chrome-devtools --with=chrome-devtools; NODE_VERSION=; "
        "DOCTOR_AMBER=brain",
    )

    assert "UNEXPECTED=1" in result.stdout, result.stdout
    assert result.stdout.count(_NODE_FAILED) == 1, result.stdout


def test_the_extras_prompt_defaults_to_no(tmp_path: Path) -> None:
    """Enter at the prompt installs nothing: `confirm`'s default is what Enter, `--yes`
    and a run with no terminal all answer (review of #399, m6)."""
    result = sh(
        "NODE_VERSION=; extras_are_asked() { return 0; }; "
        'confirm() { printf "ASKED [%s] default=%s\\n" "$1" "$2"; [ "$2" = y ]; }; '
        "install_node() { echo NODE-INSTALLED; }; install_extras",
        path=base_path(tmp_path),
        no_terminal=True,
    )

    assert (
        "ASKED [Install the Chrome DevTools MCP for browser tools: Node 22.12+, registered with"
        " Claude Code?] default=n"
    ) in result.stdout, result.stdout + result.stderr
    assert "NODE-INSTALLED" not in result.stdout


def _short_circuits(tmp_path: Path, assignments: str, amber: str, *, npx: bool) -> str:
    """`short_circuit` alone, on a machine whose CLI, uv and system packages are current
    and whose doctor answers ``amber``: SHORT when it says "Nothing to do", else WORK."""
    path = f"{stub_dir(tmp_path, 'sccli', 'aisquare')}:{base_path(tmp_path)}"
    if npx:
        path = f"{stub_dir(tmp_path, 'scnpx', 'npx')}:{path}"
    result = sh(
        "INSTALL_TARGET=$PYPI_PACKAGE; CLI_ACTION=current; CLI_VERSION=0.9.0; PIN_VERSION=; "
        "FORCE=0; UPGRADE_ALL=0; UV_VERSION=0.12.3; TMUX_ACTION=current; GH_ACTION=current; "
        "GIT_ACTION=current; WANT_AGENT=0; WANT_PROJECT=0; NODE_VERSION=; CHOSEN_EXTRAS=; "
        f"doctor_amber() {{ printf '%s' '{amber}'; }}; {assignments or ':'}; "
        "if short_circuit; then echo SHORT; else echo WORK; fi",
        path=path,
        no_terminal=True,
    )
    return result.stdout


@pytest.mark.parametrize(
    ("assignments", "npx", "expected"),
    [
        ("", False, "SHORT"),
        # Chosen, and not there: work to do, however current the rest (m6).
        ("CHOSEN_EXTRAS=chrome-devtools", False, "WORK"),
        ("CHOSEN_EXTRAS=chrome-devtools; NODE_VERSION=22.12.0", True, "SHORT"),
        # Node at the floor and no npx: the short-circuit used to fire here, past
        # _ensure_npx, and a missing npm was never installed or reported (m5).
        ("CHOSEN_EXTRAS=chrome-devtools; NODE_VERSION=22.12.0", False, "WORK"),
    ],
    ids=["not-chosen", "chosen-no-node", "chosen-and-there", "chosen-no-npx"],
)
def test_a_chosen_extra_that_is_not_there_is_work_for_the_short_circuit(
    tmp_path: Path, assignments: str, npx: bool, expected: str
) -> None:
    said = _short_circuits(tmp_path, assignments, "brain", npx=npx)

    assert said.strip().splitlines()[-1] == expected, said


#: The installed release's rows for the codebase snapshot (`RELEASE_ERA_AMBER`).
_RELEASE_ROWS = ("brain repomix", "brain snapshot", "brain tiktoken", "brain repomix snapshot")


@pytest.mark.parametrize("amber", _RELEASE_ROWS)
@pytest.mark.parametrize(
    "flags",
    [
        "",
        "WANT_SYSTEM_DEPS=0",
        "WANT_PROJECT=0; PROJECT_DIR=",
        "WANT_SYSTEM_DEPS=0; WANT_PROJECT=0",
    ],
    ids=["default", "no-system-deps", "no-project", "no-system-deps-no-project"],
)
def test_the_installed_releases_snapshot_rows_are_expected(
    tmp_path: Path, amber: str, flags: str
) -> None:
    """install.sh installs the latest RELEASE, and up to 0.8.2 its doctor has `repomix`
    (amber with a Node below 22, which this script no longer upgrades unasked, or no
    npm), `snapshot` (0.8.2, with no project or no Node to pack with) and `tiktoken`.
    Review of #399, MAJOR 1: `--yes` on Debian 12's own Node 18 exited 2 on `repomix`.
    They are expected, under every flag, and the summary says why in one line."""
    result = _summary_then_handoff(tmp_path, f"{flags or ':'}; DOCTOR_AMBER='{amber}'")

    assert "UNEXPECTED=0" in result.stdout, result.stdout
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"expected: {amber}" in result.stdout, result.stdout
    era = "/".join(amber.split()[1:])
    assert f"  {era} — this release's rows for the codebase snapshot," in result.stdout
    assert "Not expected" not in result.stdout


@pytest.mark.parametrize("flags", ["", "WANT_SYSTEM_DEPS=0"], ids=["default", "no-system-deps"])
def test_the_installed_releases_snapshot_rows_do_not_stop_the_short_circuit(
    tmp_path: Path, flags: str
) -> None:
    """A current machine whose release doctor shows them still has nothing to do, and
    the one line says why."""
    said = _short_circuits(tmp_path, flags, "brain repomix snapshot", npx=False)

    assert said.strip().splitlines()[-1] == "SHORT", said
    assert (
        "except brain repomix snapshot (gbrain is out of scope; this release's"
        " codebase-snapshot rows, gone in the next)"
    ) in said


def test_the_plan_names_an_extra_only_as_it_will_happen(tmp_path: Path) -> None:
    """Chosen: the plan says it installs, and what it registers with Claude Code is listed
    with what the run writes. Not chosen, with nothing to ask on: nothing is promised.
    Already there: not named at all. Under --no-agent the plan says the MCP is not
    registered."""

    npx = stub_dir(tmp_path, "npxbin", "npx")

    def plan(assignments: str) -> str:
        return sh(
            f"CLI_ACTION=current; CLI_VERSION=0.9.0; UV_VERSION=0.12.3; WANT_AGENT=1; "
            f"WANT_SYSTEM_DEPS=0; DEVTOOLS_MCP=absent; {assignments}; banner",
            env={"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": ""},
            path=f"{npx}:{base_path(tmp_path)}",
            no_terminal=True,
        ).stdout

    chosen = plan("NODE_VERSION=; CHOSEN_EXTRAS=chrome-devtools")
    assert (
        "install  the Chrome DevTools MCP for browser tools: Node 22.12+, registered with"
        " Claude Code (--with chrome-devtools)"
    ) in chosen
    assert (
        "~/.claude.json                    ADDED — the chrome-devtools MCP server (user scope)"
        in (chosen)
    )
    # Node already there, the MCP not: the extra still has its registration to do.
    assert "(--with chrome-devtools)" in plan("NODE_VERSION=26.7.0; CHOSEN_EXTRAS=chrome-devtools")
    no_agent = plan("WANT_AGENT=0; NODE_VERSION=; CHOSEN_EXTRAS=chrome-devtools")
    assert "Node 22.12+ (--no-agent: not registered with Claude Code)" in no_agent
    assert ".claude.json" not in no_agent
    for nothing in (
        plan("NODE_VERSION=; CHOSEN_EXTRAS="),
        plan("NODE_VERSION=26.7.0; DEVTOOLS_MCP=registered; CHOSEN_EXTRAS=chrome-devtools"),
        plan("WANT_AGENT=0; NODE_VERSION=26.7.0; CHOSEN_EXTRAS=chrome-devtools"),
    ):
        assert "Chrome DevTools" not in nothing and ".claude.json" not in nothing, nothing


def test_the_fnm_warning_says_what_a_shell_without_its_node_still_gets(tmp_path: Path) -> None:
    """fnm's Node is on PATH for this run only (review of #257). What a shell without
    its hook cannot do is start the browser tool the extra is for."""
    home = tmp_path / "home"
    fnm_dir = home / ".local" / "share" / "fnm"
    alias = fnm_dir / "aliases" / "default" / "bin"
    alias.mkdir(parents=True)
    for script, body in ((fnm_dir / "fnm", "exit 0"), (alias / "node", "echo v22.11.0")):
        script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
        script.chmod(0o755)
    path = f"{stub_dir(tmp_path, 'bashbin', 'bash')}:{base_path(tmp_path)}"

    result = sh(
        "fetch_into_shell() { return 0; }; _install_node_via_fnm",
        env={"HOME": str(home)},
        path=path,
    )

    warning = " ".join(result.stderr.split())
    assert "Node 22.11.0 via fnm" in result.stdout, result.stdout + result.stderr
    assert "fnm env --use-on-cd" in warning, "control: this is the warning in question"
    assert "`npx chrome-devtools-mcp` cannot start there" in warning
    assert "snapshot" not in warning


# ---------------------------------------------------------------------------
# the Chrome DevTools MCP — registered with Claude Code for a run that chose it
# ---------------------------------------------------------------------------

#: What `register_devtools_mcp` runs, as the package's own guide gives it.
_MCP_ADD = "mcp add chrome-devtools --scope user npx chrome-devtools-mcp@latest"
_MCP_UNDO = "to undo: claude mcp remove -s user chrome-devtools"

#: `.claude.json` as Claude Code 2.1.310 writes it: two-space indents, a user-scope
#: server under the top-level mcpServers and a project's local ones under projects.
_USER_SCOPE = """{
  "numStartups": 1,
  "mcpServers": {
    "chrome-devtools": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "chrome-devtools-mcp@latest"
      ],
      "env": {}
    }
  }
}
"""
_LOCAL_SCOPE = """{
  "projects": {
    "/work/proj": {
      "mcpServers": {
        "chrome-devtools": {
          "type": "stdio",
          "command": "npx",
          "args": [
            "chrome-devtools-mcp@latest"
          ],
          "env": {}
        }
      }
    }
  }
}
"""
#: Other top-level maps are keyed by name too: skillUsage counts a skill named
#: chrome-devtools, which the chrome-devtools-mcp plugin ships (review of #399, m9).
_SKILL_USAGE = """{
  "mcpServers": {},
  "skillUsage": {
    "chrome-devtools": {
      "usageCount": 3,
      "lastUsedAt": 1761300000000
    }
  }
}
"""
_ANOTHER_SERVER_THEN_SKILL_USAGE = """{
  "mcpServers": {
    "github": {
      "type": "http",
      "url": "https://example.invalid/mcp"
    }
  },
  "skillUsage": {
    "chrome-devtools": {
      "usageCount": 3,
      "lastUsedAt": 1761300000000
    }
  }
}
"""
#: The user's server after another, with skillUsage after the block: read to its end.
_USER_SCOPE_AMONG_OTHERS = """{
  "mcpServers": {
    "github": {
      "type": "http",
      "url": "https://example.invalid/mcp"
    },
    "chrome-devtools": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "chrome-devtools-mcp@latest"
      ],
      "env": {}
    }
  },
  "skillUsage": {}
}
"""
#: The same package under a name the operator chose: doctor's browser tools row counts
#: it by package, and so does the check (review of #399, m11).
_NAMED_OTHERWISE = """{
  "mcpServers": {
    "devtools": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "-y",
        "chrome-devtools-mcp@latest"
      ],
      "env": {}
    }
  }
}
"""
#: A checkout of it run by node, and a scoped fork from npx: the package by path or
#: under a scope is still it (review of #399, n14).
_BY_PATH = """{
  "mcpServers": {
    "devtools": {
      "type": "stdio",
      "command": "node",
      "args": [
        "/opt/chrome-devtools-mcp/build/src/index.js"
      ],
      "env": {}
    }
  }
}
"""
_SCOPED_FORK = """{
  "mcpServers": {
    "devtools": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "@acme/chrome-devtools-mcp@1.2.0"
      ],
      "env": {}
    }
  }
}
"""
#: A package whose name only starts with the MCP's: not it.
_LOOKALIKE_PACKAGE = """{
  "mcpServers": {
    "docs": {
      "type": "stdio",
      "command": "npx",
      "args": [
        "chrome-devtools-mcp-docs"
      ],
      "env": {}
    }
  }
}
"""
#: Nor is one by path or under a scope whose name only contains it.
_LOOKALIKE_PATHS = """{
  "mcpServers": {
    "docs": {
      "type": "stdio",
      "command": "node",
      "args": [
        "/opt/chrome-devtools-mcp-docs/index.js",
        "@acme/my-chrome-devtools-mcp@1.2.0"
      ],
      "env": {}
    }
  }
}
"""


def _fake_claude(
    tmp_path: Path, *, answer: str = "Added stdio MCP server chrome-devtools", status: int = 0
) -> tuple[Path, Path]:
    """A `claude` that logs each call and answers anything but --version as told, beside
    the `node` and `npx` the registration needs. Returns its directory and its log."""
    log = tmp_path / "claude.log"
    stubs = tmp_path / "claudebin"
    stubs.mkdir(exist_ok=True)
    for name, body in (
        (
            "claude",
            f'printf "%s\\n" "$*" >>"{log}"\n'
            'case "$1" in --version) echo "2.1.310 (Claude Code)"; exit 0 ;; esac\n'
            f"printf '%s\\n' '{answer}'\nexit {status}",
        ),
        ("node", 'echo "v22.12.0"'),
        ("npx", "exit 0"),
    ):
        script = stubs / name
        script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
    return stubs, log


def _mcp_adds(log: Path) -> list[str]:
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return [call for call in calls if call.startswith("mcp ")]


def _register(
    tmp_path: Path, assignments: str = "", *, path: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """A run that chose the extra, with Claude Code to register it with: the survey, the
    step, then the summary and its exit status. ``assignments`` say what differs."""
    environment = {"HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": ""}
    environment.update(env or {})
    return sh(
        "DRY_RUN=0; WANT_AGENT=1; WANT_PROJECT=1; PROJECT_DIR=/p; DOCTOR_AMBER=brain; "
        "CHOSEN_EXTRAS=chrome-devtools; survey; "
        f"{assignments or ':'}; register_devtools_mcp; summary; "
        'echo "WARNINGS=$WARNINGS UNEXPECTED=$UNEXPECTED"; handoff',
        env=environment,
        path=path,
        no_terminal=True,
    )


@pytest.mark.parametrize("registered", [False, True], ids=["mcp-absent", "mcp-registered"])
def test_the_survey_decides_whether_the_extra_still_has_its_registration_to_do(
    tmp_path: Path, registered: bool
) -> None:
    """Through the real survey, not a DEVTOOLS_MCP set by hand (review of #399, m8): Claude
    Code, Node 22.12 and npx already here, `--with chrome-devtools`, everything else
    current. With the MCP missing the run has work, and the plan lists the write; with it
    registered there is nothing to do and nothing to list."""
    stubs, _ = _fake_claude(tmp_path)
    cli = stub_dir(tmp_path, "clibin", "aisquare")
    home = tmp_path / "home"
    home.mkdir()
    if registered:
        (home / ".claude.json").write_text(_USER_SCOPE, encoding="utf-8")

    result = sh(
        "WANT_AGENT=1; CHOSEN_EXTRAS=chrome-devtools; survey; "
        "INSTALL_TARGET=$PYPI_PACKAGE; CLI_ACTION=current; CLI_VERSION=0.9.0; PIN_VERSION=; "
        "FORCE=0; UPGRADE_ALL=0; UV_VERSION=0.12.3; WANT_SYSTEM_DEPS=0; WANT_PROJECT=0; "
        "doctor_amber() { printf brain; }; "
        "if short_circuit; then echo SC=SHORT; else echo SC=WORK; fi; banner",
        env={"HOME": str(home), "CLAUDE_CONFIG_DIR": ""},
        path=f"{stubs}:{cli}:{base_path(tmp_path)}",
        no_terminal=True,
    )

    assert "Node 22.12.0" in result.stdout and "Claude Code 2.1.310" in result.stdout
    added = "~/.claude.json                    ADDED — the chrome-devtools MCP server"
    if registered:
        assert "SC=SHORT" in result.stdout, result.stdout
        assert added not in result.stdout
    else:
        assert "SC=WORK" in result.stdout, result.stdout
        assert added in result.stdout, result.stdout


def test_a_run_that_chose_the_extra_registers_the_mcp_once(tmp_path: Path) -> None:
    """Once, with the guide's command, and the summary says how to undo it. A second pass
    in the same run finds it registered and adds nothing."""
    stubs, log = _fake_claude(tmp_path)

    result = _register(tmp_path, "register_devtools_mcp", path=f"{stubs}:{base_path(tmp_path)}")

    assert _mcp_adds(log) == [_MCP_ADD]
    assert "Chrome DevTools MCP — not registered" in result.stdout, "the survey names it"
    assert "✓ Chrome DevTools MCP registered: npx chrome-devtools-mcp@latest" in result.stdout
    assert "already registered with Claude Code, left as it is" in result.stdout
    assert f"registered: the chrome-devtools MCP with Claude Code — {_MCP_UNDO}" in result.stdout
    assert "WARNINGS=0 UNEXPECTED=0" in result.stdout
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "where",
    [
        "user-scope",
        "among-others",
        "named-otherwise",
        "by-path",
        "scoped-fork",
        "claude-config-dir",
        "plugin",
        "claude-says-so",
    ],
)
def test_an_mcp_already_registered_is_left_alone_in_one_line(tmp_path: Path, where: str) -> None:
    """In the user config, in CLAUDE_CONFIG_DIR's, or brought by the chrome-devtools-mcp
    plugin, the survey sees it and `claude` is never asked. Laid out so the survey cannot,
    `claude mcp add` refuses it, and that refusal is the same one line, not a warning."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    env: dict[str, str] = {}
    answer, status = "Added stdio MCP server chrome-devtools", 0
    if where == "user-scope":
        (home / ".claude.json").write_text(_USER_SCOPE, encoding="utf-8")
    elif where == "among-others":
        (home / ".claude.json").write_text(_USER_SCOPE_AMONG_OTHERS, encoding="utf-8")
    elif where == "named-otherwise":
        (home / ".claude.json").write_text(_NAMED_OTHERWISE, encoding="utf-8")
    elif where == "by-path":
        (home / ".claude.json").write_text(_BY_PATH, encoding="utf-8")
    elif where == "scoped-fork":
        (home / ".claude.json").write_text(_SCOPED_FORK, encoding="utf-8")
    elif where == "claude-config-dir":
        config = tmp_path / "config"
        config.mkdir()
        (config / ".claude.json").write_text(_USER_SCOPE, encoding="utf-8")
        env["CLAUDE_CONFIG_DIR"] = str(config)
    elif where == "plugin":
        (home / ".claude" / "settings.json").write_text(
            '{\n  "enabledPlugins": {\n    "chrome-devtools-mcp@chrome-devtools-plugins": true\n'
            "  }\n}\n",
            encoding="utf-8",
        )
    else:
        answer, status = "MCP server chrome-devtools already exists in user config", 1
    stubs, log = _fake_claude(tmp_path, answer=answer, status=status)

    result = _register(tmp_path, path=f"{stubs}:{base_path(tmp_path)}", env=env)

    said = [line for line in result.stdout.splitlines() if "Chrome DevTools MCP" in line]
    assert said[-1].strip() == (
        "Chrome DevTools MCP — already registered with Claude Code, left as it is"
    ), result.stdout
    assert _mcp_adds(log) == ([_MCP_ADD] if where == "claude-says-so" else [])
    if where != "claude-says-so":
        assert "Chrome DevTools MCP — registered with Claude Code" in result.stdout, "survey"
    assert _MCP_UNDO not in result.stdout, "nothing this run registered is undone"
    assert "WARNINGS=0 UNEXPECTED=0" in result.stdout
    assert result.returncode == 0


@pytest.mark.parametrize(
    ("claude_json", "settings", "in_config_dir"),
    [
        (_LOCAL_SCOPE, None, False),  # a project's own server is not the user's
        (None, '{"enabledPlugins": {"chrome-devtools-mcp@chrome-devtools-plugins": false}}', False),
        (_USER_SCOPE, None, True),  # in ~/.claude.json while Claude Code reads elsewhere
        (_SKILL_USAGE, None, False),  # a skill of that name, no server
        (_ANOTHER_SERVER_THEN_SKILL_USAGE, None, False),
        (_LOOKALIKE_PACKAGE, None, False),
        (_LOOKALIKE_PATHS, None, False),
        (None, None, False),
    ],
    ids=[
        "local-scope-only",
        "plugin-disabled",
        "outside-claude-config-dir",
        "skill-usage-only",
        "another-server-then-skill-usage",
        "lookalike-package",
        "lookalike-paths",
        "no-config",
    ],
)
def test_what_claude_code_would_not_load_reads_as_not_registered(
    tmp_path: Path, claude_json: str | None, settings: str | None, in_config_dir: bool
) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    if claude_json is not None:
        (home / ".claude.json").write_text(claude_json, encoding="utf-8")
    if settings is not None:
        (home / ".claude" / "settings.json").write_text(settings, encoding="utf-8")
    env = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "elsewhere") if in_config_dir else "",
    }

    result = sh(
        "survey; if devtools_mcp_registered; then echo REGISTERED; else echo ABSENT; fi",
        env=env,
        path=base_path(tmp_path),
    )

    assert result.stdout.splitlines()[-1] == "ABSENT", result.stdout
    assert "Chrome DevTools MCP — not registered" in result.stdout


def test_without_claude_code_the_mcp_is_skipped_in_one_line(tmp_path: Path) -> None:
    """Claude Code missing, here because its install failed: nothing to register with.
    One line, no warning of its own, and the run's status is not touched by it."""
    stubs, _ = _fake_claude(tmp_path)
    (stubs / "claude").unlink()

    result = _register(tmp_path, path=f"{stubs}:{base_path(tmp_path)}")

    said = [line.strip() for line in result.stdout.splitlines() if "MCP" in line]
    assert said[-1] == (
        "Chrome DevTools MCP not registered: Claude Code is not installed. Once it is: "
        "claude mcp add chrome-devtools --scope user npx chrome-devtools-mcp@latest"
    ), result.stdout
    assert "WARNINGS=0 UNEXPECTED=0" in result.stdout
    assert result.returncode == 0


def test_a_failed_registration_warns_and_never_fails_the_install(tmp_path: Path) -> None:
    """Browser tools are optional, so `claude mcp add` failing is a warning with the
    command to run by hand, and the run exits 0 with nothing unexpected."""
    stubs, log = _fake_claude(tmp_path, answer="Error: config is locked", status=1)

    result = _register(tmp_path, path=f"{stubs}:{base_path(tmp_path)}")

    warning = " ".join(result.stderr.split())
    assert _mcp_adds(log) == [_MCP_ADD]
    assert "could not register the Chrome DevTools MCP: Error: config is locked" in warning
    assert f"To add it: claude {_MCP_ADD}" in warning
    assert _MCP_UNDO not in result.stdout and "did not install" not in result.stdout
    assert "WARNINGS=1 UNEXPECTED=0" in result.stdout
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("assignments", "says"),
    [
        ("CHOSEN_EXTRAS=", None),
        # --yes, as the CI cells run without --with: nothing asked, nothing chosen.
        (
            "CHOSEN_EXTRAS=; ASSUME_YES=1; install_node() { :; }; install_extras >/dev/null",
            None,
        ),
        ("WANT_AGENT=0", "Chrome DevTools MCP not registered: --no-agent leaves Claude Code alone"),
        ("DRY_RUN=1", f"would run: claude {_MCP_ADD}"),
        (
            "NODE_VERSION=",
            "Chrome DevTools MCP not registered: there is no Node 22.12+ to start it",
        ),
        ("NODE_VERSION=22.11.0", "there is no Node 22.12+ to start it"),
    ],
    ids=["not-chosen", "yes-without-the-extra", "no-agent", "dry-run", "no-node", "old-node"],
)
def test_claude_is_asked_to_register_only_for_a_run_that_chose_it_and_can(
    tmp_path: Path, assignments: str, says: str | None
) -> None:
    stubs, log = _fake_claude(tmp_path)

    result = _register(tmp_path, assignments, path=f"{stubs}:{base_path(tmp_path)}")

    assert _mcp_adds(log) == [], result.stdout
    if says is None:
        assert "Chrome DevTools MCP —" in result.stdout, "control: the survey ran"
        assert "registered:" not in result.stdout and "MCP not registered" not in result.stdout
    else:
        assert says in result.stdout, result.stdout


def test_a_node_without_npx_is_not_registered_and_the_extra_did_not_install(
    tmp_path: Path,
) -> None:
    """A Node with no npx cannot start the MCP, so registering it would leave Claude Code
    a server that fails. No npx after npm is the extra not installing: said once, where
    it happened, and the run exits 2 naming the extra."""
    stubs, log = _fake_claude(tmp_path)
    (stubs / "npx").unlink()
    pkg = stub_dir(tmp_path, "pkg", "apt-get")

    result = _register(
        tmp_path,
        "PKG=apt; PKG_SUDO=; install_extras",
        path=f"{stubs}:{pkg}:{base_path(tmp_path)}",
    )

    warning = " ".join(result.stderr.split())
    assert _mcp_adds(log) == []
    assert "is here without npx: npm installed but npx is not on PATH" in warning
    assert "Chrome DevTools MCP not registered: there is no npx to start it" in result.stdout
    assert _NODE_FAILED in result.stdout
    assert "WARNINGS=1 UNEXPECTED=1" in result.stdout
    assert result.returncode == 2


def test_the_mcp_is_registered_after_claude_code_is_installed(source_text: str) -> None:
    """After install_claude, so a Claude Code this run installs is there to take it, and
    after the Node half; before init, whose doctor reads the result."""
    body = source_text.split("\nmain() {\n", 1)[1].split("\n}\n", 1)[0]
    steps = re.findall(r"^    ([a-z_]+)$", body, re.MULTILINE)
    assert steps.index("install_extras") < steps.index("install_claude")
    assert steps.index("install_claude") < steps.index("register_devtools_mcp")
    assert steps.index("register_devtools_mcp") < steps.index("init_home")


def test_a_version_pin_moves_a_machine_that_is_ahead_of_it(tmp_path: Path) -> None:
    """`--version V` is documented as a PIN, so anything that is not V must move.

    The comparison was `version_lt CLI PIN`, which is false for a downgrade — so
    `--version 0.5.0` on a 0.6.0 machine left 0.6.0 in place and reported
    "aisquare-cli 0.6.0 is current", and `short_circuit` (which never consulted
    the pin) printed "Nothing to do" on a machine the user had just asked to
    hold at 0.5.0.

    Untested before because the helper pins 9.9.9 for every case — always an
    upgrade, never a downgrade.
    """
    for pin, installed, expected in (
        ("0.5.0", "0.6.0", "upgrade"),  # a downgrade IS work
        ("0.7.0", "0.6.0", "upgrade"),
        ("0.6.0", "0.6.0", "current"),
    ):
        result = sh(
            f"PIN_VERSION={pin}; CLI_VERSION={installed}; OFFLINE=1; "
            "INSTALL_TARGET=$PYPI_PACKAGE; resolve >/dev/null 2>&1; "
            'printf "%s\\n" "$CLI_ACTION"',
            path=base_path(tmp_path),
        )
        assert result.stdout.strip() == expected, (
            f"pin {pin} on {installed} -> {result.stdout.strip()}, wanted {expected}"
        )


def test_the_read_only_phase_calls_no_package_manager(tmp_path: Path) -> None:
    """§3.9.4's headline guarantee, on a dnf machine.

    `pkg_manager` runs before `short_circuit`, and it used to read
    `dnf --version` to decide dnf4-vs-dnf5 repository syntax — so the phase
    documented as making no writes and calling no package manager did call one,
    on every Fedora and RHEL box. `DNF_MAJOR` is only ever needed by
    `_gh_add_dnf_repo`, which runs long after the decision, so it is read there.

    The container cells could not catch this: they stubbed `apt-get` too, and
    install.sh's detection tries `apt-get` first, so every cell on every image
    took the apt branch.
    """
    log = tmp_path / "pkg.log"
    managers = stub_dir(
        tmp_path,
        "dnfonly",
        "dnf",
        body=f'printf "dnf %s\\n" "$*" >>"{log}"\necho "dnf 5.2.1"',
    )
    result = sh(
        'OS=linux; pkg_manager; printf "PKG=%s\\n" "$PKG"',
        path=f"{managers}:{base_path(tmp_path)}",
    )
    assert "PKG=dnf" in result.stdout, result.stdout
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert not calls, f"the read-only phase invoked a package manager: {calls}"


def test_the_path_warning_can_fire_after_an_install(tmp_path: Path) -> None:
    """§3.6's warning is about the user's PROFILE, not this process's `$PATH`.

    `install_uv` and `install_cli` both prepend `~/.local/bin` to `$PATH` in
    this very shell, so by the time `path_check` looked, the entry was always
    there and the warning could never fire on the one path where it matters. A
    first-time user whose profile lacks `~/.local/bin` got no warning, opened a
    new shell, and `aisquare` was not found — with the installer having said
    nothing.
    """
    curl = stub_dir(tmp_path, "bin", "curl")
    result = sh(
        # preflight samples the inherited PATH; then simulate what the install
        # steps do to it, and ask again.
        "preflight >/dev/null 2>&1\n"
        'PATH="$HOME/.local/bin:$PATH"\n'
        "path_check 2>/dev/null\n"
        'printf "HINT=[%s]\\n" "$PATH_HINT"',
        path=f"{curl}:{base_path(tmp_path)}",
        env={"HOME": str(tmp_path)},
    )
    assert "HINT=[export PATH=" in result.stdout, (
        "path_check saw the PATH this script had already fixed up, so the "
        f"warning could not fire:\n{result.stdout}"
    )


def test_declining_homebrew_is_only_asked_once(tmp_path: Path) -> None:
    """Four call sites route through `ensure_pkg_manager`; the decline was not
    recorded, so a Mac with no Homebrew asked the same question four times.

    Bad on exactly the platform §5 calls awkward, and it is the only interactive
    prompt before the handoff — so a user who says no was stuck in it.
    """
    result = sh(
        "OS=macos; ASSUME_YES=0\n"
        "for _ in 1 2 3 4; do ensure_pkg_manager >/dev/null 2>&1 || true; done\n"
        'printf "declined=%s\\n" "$_brew_unavailable"',
        path=base_path(tmp_path),
        no_terminal=True,
    )
    assert "declined=1" in result.stdout, (
        f"the decline was not recorded, so it will be asked again:\n{result.stdout}"
    )


def test_the_short_circuit_reason_names_only_the_checks_that_are_amber(
    tmp_path: Path,
) -> None:
    """The bug 98b3626 fixed in `summary`, which was still live in `short_circuit`.

    On a machine that HAS gbrain — reachable, since `brain` reports ok when
    gbrain is installed and also when `AISQUARE_BRAIN=0` — a `--no-project` run
    read "everything ok except snapshot (gbrain is out of scope)", blaming a
    check that was perfectly fine. The same shape today, with the `gh` a
    `--no-system-deps` run skipped.
    """
    payload = (
        '[{"name": "home", "status": "ok", "detail": "x"},'
        '{"name": "gh", "status": "warn", "detail": "y", "fix": "z"}]'
    )
    versions = tmp_path / "v"
    versions.mkdir()
    for name, out in (
        ("uv", "uv 0.12.3"),
        ("tmux", "tmux 3.7c"),
        ("gh", "gh version 2.97.0 (x)"),
        ("git", "git version 2.55.0"),
        ("node", "v26.7.0"),
        ("curl", ""),
    ):
        script = versions / name
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "{out}"\nexit 0\n', encoding="utf-8")
        script.chmod(0o755)
    cli = stub_dir(
        tmp_path,
        "cli",
        "aisquare",
        body=(
            'case "$1" in\n'
            '  --version) echo "aisquare 0.6.0"; exit 0 ;;\n'
            f"  --json) printf '%s' '{payload}'; exit 0 ;;\n"
            "esac\nexit 0"
        ),
    )
    result = sh(
        "WANT_AGENT=0; WANT_PROJECT=0; WANT_SYSTEM_DEPS=0; OFFLINE=1; "
        "survey >/dev/null 2>&1; resolve >/dev/null 2>&1; short_circuit",
        env={"AISQUARE_INSTALL_VERSION": ""},
        path=f"{versions}:{cli}:{base_path(tmp_path)}",
    )
    assert "Nothing to do" in result.stdout, result.stdout
    assert "gbrain" not in result.stdout, (
        "the reason named gbrain while `brain` was green:\n" + result.stdout
    )
    assert "except gh (system deps skipped)" in result.stdout, result.stdout


def test_no_claude_code_row_is_called_a_sign_in_to_finish(tmp_path: Path) -> None:
    """The summary mapped amber claude-code rows to "run `claude` once to authenticate
    it". No claude-code row is fixed by a sign-in: it writes no hooks, and `agents
    connect` makes a never-started ~/.claude itself. Round 9 took the hint from a
    settings.json `agents connect` refuses; the fix review found the one row it kept,
    missing hooks, no better (review of #257). Neither gets a fix here."""

    def advice(detail: str) -> subprocess.CompletedProcess[str]:
        row = {"name": "claude-code", "status": "warn", "detail": detail, "fix": ""}
        # Assigned after the source, which starts DOCTOR_RAW empty.
        return sh(
            "DOCTOR_RAW=$PAYLOAD; _actionable_fix claude-code; echo",
            env={"PAYLOAD": json.dumps([row])},
            path=base_path(tmp_path),
        )

    refused = advice(
        "Claude Code 2.1.294 hooks cannot be written in /h/.claude: can't read "
        "/h/.claude/settings.json: it is not valid JSON"
    )
    missing = advice("Claude Code hooks are missing or outdated in: /h/.claude")

    assert refused.returncode == 0 and missing.returncode == 0, refused.stderr + missing.stderr
    assert refused.stdout.strip() == "", f"a refusal was given a fix: {refused.stdout!r}"
    assert missing.stdout.strip() == "", f"missing hooks were given a fix: {missing.stdout!r}"


# The rows below are the real check's, built in a temp home and printed as
# `aisquare --json doctor` prints them, so a reworded doctor row is read here as the
# installer will read it.


def _claude_code_row() -> dict[str, Any]:
    """The claude-code row as `aisquare --json doctor` prints it (cli/common.py emit_doctor)."""
    row: dict[str, Any] = diagnostics._check_claude_code().model_dump(mode="json")
    return row


def _connect_claude_code(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    argv = ["agents", "connect", "claude-code", "--config-dir", str(config_dir)]
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 0, result.output


def _edit_settings(config_dir: Path, change: Callable[[dict[str, Any]], object]) -> None:
    path = config_dir / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    change(settings)
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _lose_the_stop_hook(config_dir: Path) -> None:
    """What an install from before the Stop event left: "hooks are missing or outdated"."""
    _edit_settings(config_dir, lambda settings: settings["hooks"].pop("Stop"))


def _switch_hooks_off(config_dir: Path) -> None:
    _edit_settings(config_dir, lambda settings: settings.update(disableAllHooks=True))


def _install_the_plugin(config_dir: Path) -> None:
    """What `/plugin install aisquare@aisquare-cli` leaves (Claude Code 2.1.292)."""
    plugin = agent_core.CLAUDE_PLUGIN_ID
    _edit_settings(
        config_dir, lambda settings: settings.setdefault("enabledPlugins", {plugin: True})
    )
    record = {"scope": "user", "installPath": str(config_dir / "plugins"), "version": "0.8.0"}
    (config_dir / "plugins").mkdir()
    (config_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {plugin: [record]}}), encoding="utf-8"
    )


def _run_another_install(config_dir: Path, other: Path) -> None:
    """Every hook names `other` in place of this install's aisquare."""

    def rename(settings: dict[str, Any]) -> None:
        for groups in settings["hooks"].values():
            for hook in (hook for group in groups for hook in group["hooks"]):
                hook["command"] = f"{other} hook {hook['command'].rsplit(' ', 1)[1]}"

    _edit_settings(config_dir, rename)


def _claude_code_state(
    state: str, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code in `home` as `state` names it, from a ~/.claude `agents connect` made."""
    claude = home / ".claude"
    never = {
        "never connected": "{}",
        "switched off, never connected": '{"disableAllHooks": true}',
        "refused, never connected": '{"broken": \n',
    }
    if state in never:
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text(never[state], encoding="utf-8")
        return
    _connect_claude_code(claude)
    if state == "switched off":
        _switch_hooks_off(claude)
    elif state == "plugin and hooks":
        _install_the_plugin(claude)
    elif state == "another install":
        other = tmp_path / "old" / "aisquare"
        other.parent.mkdir()
        other.write_text("#!/bin/sh\n", encoding="utf-8")
        other.chmod(0o755)  # one that can start: one that cannot is graded gone
        _run_another_install(claude, other)
        monkeypatch.setattr(agent_core, "hook_binary_version", lambda argv, **_kwargs: "0.7.0")
    elif state == "switched off beside missing hooks":
        work = home / ".claude-work"
        _connect_claude_code(work)
        _lose_the_stop_hook(work)
        _switch_hooks_off(claude)
    elif state == "missing hooks":
        _lose_the_stop_hook(claude)
    else:
        assert state == "missing hooks in two directories", state
        work = home / ".claude-work"
        _connect_claude_code(work)
        _lose_the_stop_hook(work)
        _lose_the_stop_hook(claude)


@pytest.mark.parametrize(
    ("state", "row_says"),
    [
        ("switched off", 'hooks are switched off ("disableAllHooks": true)'),
        ("plugin and hooks", "runs aisquare two ways"),
        ("another install", "(0.7.0) — this install is"),
        ("switched off beside missing hooks", "; hooks are missing or outdated"),
        ("missing hooks", "hooks are missing or outdated"),
        ("missing hooks in two directories", "hooks are missing or outdated"),
    ],
    ids=[
        "switched-off",
        "plugin-and-hooks",
        "another-install",
        "switched-off-and-missing",
        "missing",
        "missing-in-two-dirs",
    ],
)
def test_an_amber_claude_code_row_is_unexpected_and_printed_in_the_doctors_words(
    tmp_path: Path,
    isolated_agent_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    row_says: str,
) -> None:
    """Every amber claude-code row after a run that wired the hooks got "run `claude`
    once to authenticate it" and exit 0, and round 9 and its sweep kept that for missing
    hooks, which a sign-in does not fix either (review of #257). Each is unexpected now,
    exit 2, and the summary prints the row's own detail and fix: the row this verdict
    read, which `aisquare doctor` run inside a repository whose plugin is its route does
    not show. So claude-code is not sent to that doctor."""
    _claude_code_state(state, isolated_agent_home, tmp_path, monkeypatch)
    row = _claude_code_row()

    result = _summary_then_handoff(
        tmp_path,
        "WANT_SYSTEM_DEPS=0; DOCTOR_RAW=$PAYLOAD; DOCTOR_AMBER='brain claude-code'",
        env={"PAYLOAD": json.dumps([row])},
    )

    assert row_says in row["detail"], row
    assert "authenticate" not in result.stdout, f"{state}: a sign-in to finish\n{result.stdout}"
    printed = (
        f"Not expected, and worth a look:\n  claude-code — {row['detail']}\n    → {row['fix']}\n"
    )
    assert printed in result.stdout, result.stdout
    assert "the full detail and a fix for each" not in result.stdout, result.stdout
    assert "UNEXPECTED=1" in result.stdout
    assert result.returncode == 2


@pytest.mark.parametrize(
    ("text", "printed"),
    [
        ("Zoë", "Zoë"),
        ("café — naïve Ångström", "café — naïve Ångström"),
        ('a "quote", a back\\slash, a /slash', 'a "quote", a back\\slash, a /slash'),
        ("tab\there, newline\nthere", "tab\there, newline\nthere"),
        ("日本語 नमस्ते", "日本語 नमस्ते"),
        ("snake \U0001f40d and \U0001d11e", "snake \U0001f40d and \U0001d11e"),
        # A directory name must not send the terminal a sequence: controls stay escaped.
        ("esc \x1b[31m, C1 \x9b, DEL \x7f", "esc \\u001b[31m, C1 \\u009b, DEL \\u007f"),
        ("ends with a backslash \\", "ends with a backslash \\"),
        ("\\u0041 is text, not an escape", "\\u0041 is text, not an escape"),
    ],
    ids=[
        "latin",
        "dash",
        "quote-backslash-slash",
        "tab-newline",
        "cjk-devanagari",
        "surrogate-pairs",
        "controls",
        "trailing-backslash",
        "escaped-backslash-u",
    ],
)
def test_a_doctor_string_prints_as_the_text_it_encodes(
    tmp_path: Path, text: str, printed: str
) -> None:
    """`aisquare --json doctor` writes every non-ASCII character as an escape, and the
    summary decoded only a quote, a backslash and the dash, so a home named Zoë printed
    escaped in the very line meant to say where to look (fix review of #257). It is
    decoded in the shell alone, exactly: no interpreter is assumed. Compared as bytes,
    so no locale is in the way."""
    environment = {
        **os.environ,
        "AISQUARE_INSTALL_LIB": "1",
        "PATH": base_path(tmp_path),
        "BODY": json.dumps(text)[1:-1],
    }
    result = subprocess.run(
        [SH, "-c", f'. "{SCRIPT}"\n_json_text "$BODY"'],
        capture_output=True,
        env=environment,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )

    assert json.dumps(text).isascii(), "the doctor's JSON is ASCII"
    assert result.returncode == 0, result.stderr
    assert result.stdout == (printed + "\n").encode("utf-8"), result.stdout


def test_a_hand_written_escape_the_doctor_never_emits_still_decodes(tmp_path: Path) -> None:
    """`\\/`, upper-case hex, and a lone surrogate (U+FFFD), which json.dumps never writes."""
    environment = {
        **os.environ,
        "AISQUARE_INSTALL_LIB": "1",
        "PATH": base_path(tmp_path),
        "BODY": "\\/ and \\u00E9 and \\ud83d alone",
    }
    result = subprocess.run(
        [SH, "-c", f'. "{SCRIPT}"\n_json_text "$BODY"'],
        capture_output=True,
        env=environment,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )

    assert result.stdout == "/ and é and \N{REPLACEMENT CHARACTER} alone\n".encode(), result


def test_a_home_with_a_non_ascii_name_is_printed_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The summary prints the claude-code row's detail and fix in the doctor's words, and
    printed Zo\\u00eb for a home named Zoë (fix review of #257)."""
    home = tmp_path / "Zoë"
    monkeypatch.setattr("aisquare.core.agents._home", lambda: home)
    monkeypatch.setattr("aisquare.core.claude_accounts._home", lambda: home)
    _claude_code_state("switched off", home, tmp_path, monkeypatch)
    row = _claude_code_row()

    result = _summary_then_handoff(
        tmp_path,
        "WANT_SYSTEM_DEPS=0; DOCTOR_RAW=$PAYLOAD; DOCTOR_AMBER='brain claude-code'",
        env={"PAYLOAD": json.dumps([row])},
    )

    assert str(home) in row["detail"] and str(home) in row["fix"], row
    assert f"  claude-code — {row['detail']}\n    → {row['fix']}\n" in result.stdout, result.stdout
    assert result.returncode == 2


@pytest.mark.parametrize(
    "state",
    [
        "never connected",
        "missing hooks",
        "switched off",
        "switched off, never connected",
        "refused, never connected",
        "another install",
    ],
    ids=[
        "never-connected",
        "missing",
        "switched-off",
        "switched-off-never-connected",
        "refused-never-connected",
        "another-install",
    ],
)
def test_a_no_agent_run_expects_claude_code_whatever_its_row_says(
    tmp_path: Path,
    isolated_agent_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """`--no-agent` asked for Claude Code to be left alone, and the run touches none of
    it. Its unconnected row was told to sign in (fix review of #257), and then a
    settings.json `agents connect` refuses, or hooks switched off, were "Not expected":
    exit 2 on every run, never a short-circuit, and told to "connect again" (delta
    review). No claude-code state is this run's to change, so each is expected, exit 0,
    and named in the doctor's words as information, without its fix."""
    _claude_code_state(state, isolated_agent_home, tmp_path, monkeypatch)
    row = _claude_code_row()

    result = _summary_then_handoff(
        tmp_path,
        "WANT_AGENT=0; WANT_SYSTEM_DEPS=0; DOCTOR_RAW=$PAYLOAD; DOCTOR_AMBER='brain claude-code'",
        env={"PAYLOAD": json.dumps([row])},
    )

    assert row["status"] == "warn" and row["fix"], row
    assert "expected: brain claude-code" in result.stdout, result.stdout
    told = "  claude-code — left alone by --no-agent; aisquare doctor here says:\n"
    assert f"{told}               {row['detail']}\n" in result.stdout, result.stdout
    assert row["fix"] not in result.stdout, result.stdout
    assert "Not expected" not in result.stdout and "authenticate" not in result.stdout
    assert result.returncode == 0


def test_a_no_agent_row_whose_detail_cannot_be_read_says_where_to_read_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `{` in a path the claude-code row names splits the payload this script reads,
    so its detail cannot be extracted, and the --no-agent line ended at "aisquare doctor
    here says:" with nothing under it (second delta review of #257). It names where the
    state can be read in full instead."""
    home = tmp_path / "ho{me}"
    monkeypatch.setattr("aisquare.core.agents._home", lambda: home)
    monkeypatch.setattr("aisquare.core.claude_accounts._home", lambda: home)
    _claude_code_state("refused, never connected", home, tmp_path, monkeypatch)
    row = _claude_code_row()

    result = _summary_then_handoff(
        tmp_path,
        "WANT_AGENT=0; WANT_SYSTEM_DEPS=0; DOCTOR_RAW=$PAYLOAD; DOCTOR_AMBER='brain claude-code'",
        env={"PAYLOAD": json.dumps([row])},
    )

    assert "{" in row["detail"], row
    said = "  claude-code — left alone by --no-agent; aisquare doctor here shows its state.\n"
    assert said in result.stdout, result.stdout
    assert "here says:" not in result.stdout, result.stdout
    assert result.returncode == 0


@pytest.mark.parametrize(
    ("machine", "want_agent", "bin_line", "tool_env", "hooks"),
    [
        ("fresh", 1, "uv, aisquare, asq, claude", True, True),
        ("fresh", 0, "uv, aisquare, asq", True, False),
        ("current", 1, None, False, True),
        ("current", 0, None, False, False),
        ("force", 1, "aisquare, asq", True, True),
        ("upgrade-all", 1, None, False, True),
    ],
    ids=["fresh", "fresh-no-agent", "current", "current-no-agent", "force", "upgrade-all"],
)
def test_the_banner_lists_only_what_the_run_writes(
    tmp_path: Path,
    machine: str,
    want_agent: int,
    bin_line: str | None,
    tool_env: bool,
    hooks: bool,
) -> None:
    """Under --no-agent the plan said "register DIR as a project, and connect
    claude-code's hooks" and listed ~/.claude/settings.json and `claude` as written;
    init runs without --agent then and no Claude Code is installed (fix and delta
    reviews of #257). And a current uv or aisquare-cli is left where it is, so
    "Written to" names only what this run writes; --force (an upgrade) rewrites the CLI,
    and --upgrade-all's `uv self update` is named in the plan (second delta review)."""
    decided = {
        "fresh": 'UV_VERSION=""; CLI_ACTION=install; CLAUDE_ACTION=install; CLAUDE_VERSION=""',
        "current": (
            "UV_VERSION=0.12.3; CLI_ACTION=current; CLI_VERSION=0.8.0; "
            "CLAUDE_ACTION=update; CLAUDE_VERSION=2.1.294"
        ),
        "force": (
            "UV_VERSION=0.12.3; FORCE=1; CLI_ACTION=upgrade; CLI_VERSION=0.8.0; "
            "CLAUDE_ACTION=update; CLAUDE_VERSION=2.1.294"
        ),
        "upgrade-all": (
            "UV_VERSION=0.12.3; UPGRADE_ALL=1; CLI_ACTION=current; CLI_VERSION=0.8.0; "
            "CLAUDE_ACTION=update; CLAUDE_VERSION=2.1.294"
        ),
    }[machine]
    result = sh(
        f"WANT_AGENT={want_agent}; WANT_PROJECT=1; PROJECT_DIR=/p; {decided}; banner",
        env={"AISQUARE_HOME": ""},  # the default home, not the suite's (see below)
        path=base_path(tmp_path),
    )

    written = result.stdout.split("Written to:\n", 1)[1]
    assert "register /p as a project" in result.stdout, result.stdout + result.stderr
    assert "  ~/.aisquare/  " in written, written
    if bin_line is None:
        assert "~/.local/bin/" not in written, written
    else:
        assert f"  ~/.local/bin/                     {bin_line}\n" in written, written
    assert ("~/.local/share/uv/tools/" in written) is tool_env, written
    assert ("claude-code's hooks" in result.stdout) is hooks, result.stdout
    assert ("~/.claude/settings.json" in written) is hooks, written
    moves_uv = "  update   uv 0.12.3 (uv self update, --upgrade-all)\n"
    assert (moves_uv in result.stdout) is (machine == "upgrade-all"), result.stdout


# A repository's own plugin answers for that repository only, and the installer wires
# Claude Code's own config. These run the real CLI as the script does, so the doctor
# sees the folder it is started in (review of #257).


def _the_cli(tmp_path: Path) -> Path:
    """A directory for PATH whose `aisquare` is this tree's CLI, in a process of its own."""
    directory = tmp_path / "cli"
    directory.mkdir()
    script = directory / "aisquare"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" -P -m aisquare "$@"\n', "utf-8")
    script.chmod(0o755)
    return directory


def _repo_scope_plugin(config_dir: Path, repo: Path, scope: str) -> None:
    """What `claude plugin install aisquare@aisquare-cli --scope <scope>`, run in `repo`,
    leaves (Claude Code 2.1.294): the key in the repository's settings, none in the
    config dir's, and a record in the config dir naming the scope and the repository."""
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / ".claude").mkdir()
    name = "settings.json" if scope == "project" else "settings.local.json"
    plugin = agent_core.CLAUDE_PLUGIN_ID
    (repo / ".claude" / name).write_text(json.dumps({"enabledPlugins": {plugin: True}}), "utf-8")
    record = {"scope": scope, "projectPath": str(repo), "version": "0.8.0"}
    (config_dir / "plugins").mkdir(parents=True)
    (config_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {plugin: [record]}}), encoding="utf-8"
    )


def _claude_code_row_from(where: Path, cli: Path, path: str, home: Path) -> dict[str, Any]:
    """The claude-code row of the real `aisquare --json doctor`, run in `where` with `path`."""
    doctor = subprocess.run(
        [str(cli / "aisquare"), "--json", "doctor"],
        cwd=where,
        env={**os.environ, "HOME": str(home), "PATH": path},
        capture_output=True,
        text=True,
        timeout=120,
    )
    row: dict[str, Any] = next(c for c in json.loads(doctor.stdout) if c["name"] == "claude-code")
    return row


@pytest.mark.parametrize(("scope", "inside"), [("project", "."), ("local", "src")])
def test_a_repositorys_plugin_does_not_answer_for_claude_codes_own_hooks(
    tmp_path: Path, isolated_agent_home: Path, scope: str, inside: str
) -> None:
    """Run from inside a repository whose project- or local-scope plugin is the only
    route, the doctor's claude-code row is green there, and the installer read it as
    "claude-code hooks installed": it short-circuited, wired nothing in ~/.claude, and
    every other repository ran without aisquare (review of #257). The installer's
    reading of the row is now Claude Code's own config's, asked from `/`."""
    claude = isolated_agent_home / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text("{}", encoding="utf-8")
    repo = tmp_path / "team-repo"
    _repo_scope_plugin(claude, repo, scope)
    cli = _the_cli(tmp_path)
    path = f"{cli}:{base_path(tmp_path)}"

    here = _claude_code_row_from(repo / inside, cli, path, isolated_agent_home)
    amber = sh(
        "doctor_amber; echo",
        cwd=repo / inside,
        env={"HOME": str(isolated_agent_home)},
        path=path,
    )

    assert here["status"] == "ok" and f"at {scope} scope in {repo}" in here["detail"], here
    assert "claude-code" in amber.stdout.split(), amber.stdout + amber.stderr


def test_a_run_from_that_repository_lists_hooks_it_could_not_write(
    tmp_path: Path, isolated_agent_home: Path
) -> None:
    """The closing doctor ran in the same folder. With a settings.json `agents connect`
    refuses, `init` only notes it and the plugin read as connected there, so the run
    listed nothing and exited 0 with no hooks wired. It is unexpected now, as anywhere
    else. And `aisquare doctor` run there still reads green, so the summary prints the
    row its verdict came from, Claude Code's own config's, rather than send the user to
    a doctor that disagrees (fix review of #257)."""
    paths.ensure_home()
    with store_session():
        pass
    claude = isolated_agent_home / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text('{"broken": \n', encoding="utf-8")
    repo = tmp_path / "team-repo"
    _repo_scope_plugin(claude, repo, "project")
    cli = _the_cli(tmp_path)
    path = f"{cli}:{base_path(tmp_path)}"

    here = _claude_code_row_from(repo, cli, path, isolated_agent_home)
    result = sh(
        "DRY_RUN=0; WANT_SYSTEM_DEPS=0; PROJECT_DIR=$PWD; run_doctor >/dev/null; "
        'summary; echo "UNEXPECTED=$UNEXPECTED"; handoff',
        cwd=repo,
        env={"HOME": str(isolated_agent_home)},
        path=path,
        no_terminal=True,
    )

    assert here["status"] == "ok", here
    said = result.stdout.split("Not expected, and worth a look:\n", 1)[-1]
    assert said.startswith(f"  claude-code — Claude Code hooks cannot be written in {claude}"), (
        result.stdout
    )
    assert f"    → repair {claude / 'settings.json'} (it is not valid JSON" in said, result.stdout
    assert "the full detail and a fix for each" not in result.stdout, result.stdout
    assert "UNEXPECTED=1" in result.stdout
    assert result.returncode == 2


def _claude_code_doctor(status: str, detail: str) -> str:
    """A doctor payload with an amber brain and this claude-code row."""
    return json.dumps(
        [
            {"name": "brain", "status": "warn", "detail": "gbrain not found", "fix": "x"},
            {"name": "claude-code", "status": status, "detail": detail, "fix": "y"},
        ]
    )


_CONNECTED = _claude_code_doctor("ok", "Claude Code connected (all lifecycle hooks installed)")
_MISSING = _claude_code_doctor(
    "warn",
    "Claude Code hooks are missing or outdated (older installs lack the "
    "Stop/Notification/SessionEnd events) in: /h/.claude (found on disk, not connected in "
    "this home)",
)
_SWITCHED_OFF = _claude_code_doctor(
    "warn", 'Claude Code hooks are switched off ("disableAllHooks": true) in: /h/.claude'
)
_REFUSED = _claude_code_doctor(
    "warn",
    "Claude Code hooks cannot be written in /h/.claude: can't read /h/.claude/settings.json: "
    "it is not valid JSON",
)
_LEFT_ALONE = (
    "doctor: everything ok except brain claude-code "
    "(gbrain is out of scope; Claude Code left alone (--no-agent))"
)
_ONLY_BRAIN = "doctor: everything ok except brain (gbrain is out of scope)"


@pytest.mark.parametrize(
    ("want_agent", "plugin_file", "here", "from_root", "doctor_line"),
    [
        (1, "settings.json", _CONNECTED, _MISSING, None),
        (1, "settings.json", _CONNECTED, _CONNECTED, _ONLY_BRAIN),
        # No repository plugin can load here, so this folder's row is `/`'s: not asked.
        (1, None, _CONNECTED, _MISSING, _ONLY_BRAIN),
        # --no-agent wires nothing, so `/` is not asked: its row would be listed here.
        (0, "settings.json", _CONNECTED, _SWITCHED_OFF, _ONLY_BRAIN),
        (0, "settings.json", _MISSING, _MISSING, _LEFT_ALONE),
        (0, "settings.json", _SWITCHED_OFF, _SWITCHED_OFF, _LEFT_ALONE),
        (0, "settings.json", _REFUSED, _REFUSED, _LEFT_ALONE),
    ],
    ids=[
        "agent-root-missing",
        "agent-root-connected",
        "agent-no-repo-plugin-root-not-asked",
        "no-agent-root-not-asked",
        "no-agent-not-connected",
        "no-agent-switched-off",
        "no-agent-refused",
    ],
)
def test_the_short_circuit_takes_claude_code_from_where_the_run_wires_it(
    tmp_path: Path,
    want_agent: int,
    plugin_file: str | None,
    here: str,
    from_root: str,
    doctor_line: str | None,
) -> None:
    """`short_circuit` printed "claude-code hooks installed" and did nothing whenever the
    folder it ran in answered green. With the agent wanted it goes by `/`'s answer, and
    a machine whose hooks are there still has nothing to do. `--no-agent` wires nothing:
    it reads this folder's row, the one `aisquare doctor` here shows, and leaves every
    claude-code state alone, so none of them stops it (fix and delta reviews of #257)."""
    versions = tmp_path / "v"
    versions.mkdir()
    for name, out in (
        ("uv", "uv 0.12.3"),
        ("tmux", "tmux 3.7c"),
        ("gh", "gh version 2.97.0 (x)"),
        ("git", "git version 2.55.0"),
        ("node", "v26.7.0"),
        ("claude", "2.1.294 (Claude Code)"),
        ("curl", ""),
    ):
        script = versions / name
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "{out}"\nexit 0\n', encoding="utf-8")
        script.chmod(0o755)
    cli = stub_dir(
        tmp_path,
        "cli",
        "aisquare",
        body=(
            'case "$1" in\n'
            '  --version) echo "aisquare 0.6.0"; exit 0 ;;\n'
            '  --json) if [ "$(pwd)" = / ]; then printf %s "$FROM_ROOT"; '
            'else printf %s "$HERE"; fi; exit 0 ;;\n'
            "esac\nexit 0"
        ),
    )
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    if plugin_file is not None:
        # Where a project-scope plugin is enabled: why this folder's answer may differ.
        (repo / ".claude" / plugin_file).write_text("{}", encoding="utf-8")

    result = sh(
        f"WANT_AGENT={want_agent}; WANT_PROJECT=0; OFFLINE=1; "
        "survey >/dev/null 2>&1; resolve >/dev/null 2>&1; "
        "if short_circuit; then echo FIRED; else echo REFUSED; fi",
        cwd=repo,
        env={"AISQUARE_INSTALL_VERSION": "", "HERE": here, "FROM_ROOT": from_root},
        path=f"{versions}:{cli}:{base_path(tmp_path)}",
    )

    fired = doctor_line is not None
    assert ("FIRED" in result.stdout) is fired, result.stdout + result.stderr
    assert not fired or f"  {doctor_line}\n" in result.stdout, result.stdout
    hooks = "claude-code hooks installed" if want_agent else "(--no-agent: no agent hooks)"
    assert not fired or hooks in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("layout", "want_agent", "asked_from_root"),
    [
        ("plain", 1, False),
        ("project-scope file here", 1, True),
        ("local-scope file at the repository", 1, True),
        ("local-scope file above the repository", 1, False),
        ("the home's local file, from the repository", 1, False),
        ("the home's local file, from a folder in no repository", 1, False),
        ("the home's local file, from the home", 1, True),
        ("the home's own settings", 1, False),
        ("project-scope file here", 0, False),
    ],
    ids=[
        "plain",
        "project-here",
        "local-at-repo",
        "local-above-repo",
        "home-local-from-repo",
        "home-local-from-a-plain-folder",
        "home-local-from-home",
        "home-settings",
        "no-agent",
    ],
)
def test_the_doctor_runs_from_root_only_where_this_folder_could_answer_differently(
    tmp_path: Path, layout: str, want_agent: int, asked_from_root: bool
) -> None:
    """`doctor_json` ran a second full doctor from `/` for its claude-code row on every
    call, so a run started four doctors where it used to start two (review of #257,
    round 14). This folder changes that row only through a repository's aisquare plugin,
    enabled in .claude/settings.json of the folder a session starts in, or in
    .claude/settings.local.json where the doctor's `_local_settings_root` reads it: the
    first folder up that holds a .git, or this folder, never the home unless the session
    starts there. Counted every ancestor's file, a ~/.claude/settings.local.json (one
    saved permission) asked `/` again from every folder (round 15). The home's own
    settings.json is Claude Code's user settings."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    code = home / "code"
    repo = code / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src" / ".claude").mkdir(parents=True)
    files = {
        "project-scope file here": repo / "src" / ".claude" / "settings.json",
        "local-scope file at the repository": repo / ".claude" / "settings.local.json",
        "local-scope file above the repository": code / ".claude" / "settings.local.json",
        "the home's local file, from the repository": home / ".claude" / "settings.local.json",
        "the home's local file, from a folder in no repository": (
            home / ".claude" / "settings.local.json"
        ),
        "the home's local file, from the home": home / ".claude" / "settings.local.json",
    }
    if layout in files:
        files[layout].parent.mkdir(parents=True, exist_ok=True)
        files[layout].write_text("{}", encoding="utf-8")
    (home / "notes").mkdir()
    here = {
        "the home's local file, from a folder in no repository": home / "notes",
        "the home's local file, from the home": home,
        "the home's own settings": home,
    }.get(layout, repo / "src")
    log = tmp_path / "doctor-runs.log"
    row = '{"name": "claude-code", "status": "ok", "detail": "d", "fix": null}'
    cli = stub_dir(
        tmp_path,
        "cli",
        "aisquare",
        body=f"""[ "$1" = --json ] && pwd -P >>"{log}"\nprintf '[%s]' '{row}'""",
    )

    result = sh(
        f"WANT_AGENT={want_agent}; doctor_json >/dev/null",
        cwd=here,
        env={"HOME": str(home)},
        path=f"{cli}:{base_path(tmp_path)}",
    )

    runs = log.read_text(encoding="utf-8").splitlines()
    assert result.returncode == 0, result.stderr
    assert runs == ([str(here.resolve()), "/"] if asked_from_root else [str(here.resolve())]), runs


def test_a_path_in_the_plan_prints_as_written(tmp_path: Path) -> None:
    """The plan is printed as text. As the printf format a % in a path was a conversion
    (round 15), and under %b a \\c in one ended the banner there, and \\t or \\0NNN were
    rewritten (review of #257, round 16). Every backslash and % prints as written, under
    whichever sh runs the suite."""
    project = "/tmp/we\\cird\\tx\\0101 100%sure"
    home_dir = "/data/a\\cq%d"
    result = sh(
        'WANT_AGENT=1; WANT_PROJECT=1; PROJECT_DIR=$P; CLI_ACTION=install; UV_VERSION=""; '
        'CLAUDE_ACTION=install; CLAUDE_VERSION=""; banner',
        env={"P": project, "AISQUARE_HOME": home_dir, "CLAUDE_CONFIG_DIR": ""},
        path=base_path(tmp_path),
    )

    assert "\\c" in project and "%" in project, "control: the path holds both"
    assert f"  register {project} as a project\n" in result.stdout, result.stdout
    assert "  connect  claude-code's hooks\n" in result.stdout, result.stdout
    assert f"  {home_dir + '/':<33} config.toml" in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("environment", "home_shown", "settings_shown"),
    [
        ({}, "~/.aisquare/", "~/.claude/settings.json"),
        (
            {"AISQUARE_HOME": "{home}/aq-home/", "CLAUDE_CONFIG_DIR": "{home}/.claude-work"},
            "~/aq-home/",
            "~/.claude-work/settings.json",
        ),
        (
            {"AISQUARE_HOME": "/srv/aq%d", "CLAUDE_CONFIG_DIR": "/srv/claude"},
            "/srv/aq%d/",
            "/srv/claude/settings.json",
        ),
    ],
    ids=["defaults", "redirected-in-home", "redirected-elsewhere"],
)
def test_the_banner_and_the_steps_name_the_paths_this_run_resolves(
    tmp_path: Path, environment: dict[str, str], home_shown: str, settings_shown: str
) -> None:
    """The banner said "Written to: ~/.claude/settings.json" and "~/.aisquare/" while
    `init --agent claude-code` wrote where CLAUDE_CONFIG_DIR and AISQUARE_HOME point, which
    the one-liner inherits (review of #257, round 15). It names the paths aisquare
    resolves, and a % in one prints as a %: the plan is no longer a printf format."""
    home = tmp_path / "home"
    home.mkdir()
    # Both cleared first: the suite's own home is in AISQUARE_HOME.
    env = {"HOME": str(home), "AISQUARE_HOME": "", "CLAUDE_CONFIG_DIR": ""}
    env.update({k: v.format(home=home) for k, v in environment.items()})
    result = sh(
        "WANT_AGENT=1; WANT_PROJECT=0; DRY_RUN=1; CLI_ACTION=current; CLI_VERSION=0.8.0; "
        "UV_VERSION=0.12.3; CLAUDE_ACTION=update; CLAUDE_VERSION=2.1.294; banner; init_home",
        env=env,
        path=base_path(tmp_path),
    )

    written = result.stdout.split("Written to:\n", 1)[1]
    assert f"  {home_shown:<33} config.toml, context.db, projects/\n" in written, written
    assert f"  {settings_shown:<33} MERGED" in written, written
    assert f"  set up   {home_shown.rstrip('/')} (no project registered)\n" in result.stdout
    assert f"==> Setting up {home_shown.rstrip('/')}\n" in result.stdout, result.stdout


def test_a_folder_that_was_removed_may_load_a_repo_plugin(tmp_path: Path) -> None:
    """In a folder that was removed, dash and BusyBox ash answer `pwd -P` with nothing and
    succeed. Walked up, "" became "." for ever, and the installer hung (review of round
    14's fixes for #257). A path that cannot be read answers "may", as a failing `pwd -P`
    (bash) already did."""
    gone = tmp_path / "gone"
    gone.mkdir()

    result = sh(
        f'cd "{gone}" && rmdir "{gone}" && {{ _folder_may_load_a_repo_plugin; echo "may=$?"; }}',
        env={"HOME": str(tmp_path)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1:] == ["may=0"], result.stdout


def test_the_gh_advice_matches_whether_gh_exists(tmp_path: Path) -> None:
    """ "Log in" is wrong advice for a binary that is not installed.

    `_actionable_fix` answered `gh auth login` unconditionally, which is right
    for a gh that is present and logged out and wrong for one whose install
    failed — a reachable state, because the System class is warn-only (§3.2).
    Same class as the two the review caught: advice that does not match the
    state it is given for.
    """
    present = sh("GH_VERSION=2.97.0; _actionable_fix gh; echo", path=base_path(tmp_path))
    assert present.stdout.strip() == "gh auth login", present.stdout

    absent = sh('GH_VERSION=""; PKG=apt; _actionable_fix gh; echo', path=base_path(tmp_path))
    assert "install it" in absent.stdout, f"an absent gh was told to log in: {absent.stdout!r}"
    assert "apt install gh" in absent.stdout, absent.stdout
