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

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# `install.sh` is a POSIX shell script and every test here drives it through
# `sh`, `pty.fork` and `os.execve`. None of that exists on Windows, and `pty`
# used to be imported at MODULE scope, so the whole file failed COLLECTION
# there rather than skipping — an error, not a skip, before a single test ran.
# Skipping at module level is the honest answer and keeps the rest of the
# suite's Windows run clean.
if sys.platform == "win32":  # pragma: no cover - the POSIX installer's own tests
    pytest.skip("install.sh is a POSIX shell script", allow_module_level=True)

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "install.sh"

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
    """`uname -s`/`-m` to the labels the rest of the script branches on."""
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
    assert result.stdout.split() == [expected_os, expected_arch, "0"], result.stderr


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
# the Node floor — §1.4/§6.3, the boundary that matters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("node_version", "expected"),
    [
        ("v12.22.9", "install"),  # Ubuntu 22.04
        ("v18.20.4", "install"),  # Debian 12
        ("v21.7.3", "install"),  # one below the floor
        ("v22.0.0", "current"),  # the floor itself — inclusive
        ("v22.23.2", "current"),
        ("v26.7.0", "current"),
    ],
)
def test_the_node_decision_is_made_at_the_repomix_floor(
    tmp_path: Path, node_version: str, expected: str
) -> None:
    """Repomix declares `>=22.0.0`, so 21 installs and 22 does not.

    An off-by-one here is invisible on every developer machine — they are all far
    above the floor — and would surface only on the one distribution shipping
    exactly 22.
    """
    stubs = stub_dir(tmp_path, "bin", "node", body=f'printf "{node_version}\\n"')
    result = sh(
        'survey >/dev/null 2>&1; resolve >/dev/null 2>&1; printf "%s\\n" "$NODE_ACTION"',
        path=f"{stubs}:{base_path(tmp_path)}",
    )
    assert result.stdout.strip() == expected, (
        f"Node {node_version} -> {result.stdout.strip()}, wanted {expected}\n{result.stderr}"
    )


def test_tmux_is_only_touched_below_its_own_floor(tmp_path: Path) -> None:
    """3.2 is the floor (core/tmux.py MIN_VERSION); 3.3a is fine, 3.0 is not.

    Deliberately NOT the 3.5 recommendation: below 3.5 the fleet works without
    Shift+Enter, and reinstalling a working tmux to chase a nicety is the kind
    of unasked-for change §3.9 exists to prevent.
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


def test_dry_run_runs_nothing(tmp_path: Path) -> None:
    """Against a PATH of stubs that record being called and then FAIL.

    `--dry-run` is not a nicety: it is how a person decides whether to trust a
    script they are about to pipe into a shell, and how a reviewer reads this
    one. If any of it actually executed, the flag would be a lie told to exactly
    the most cautious user.
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
    result = sh(
        "main --dry-run --yes --no-project --offline",
        path=f"{stubs}:{cli}:{base_path(tmp_path)}",
    )

    assert result.returncode == 0, f"--dry-run should exit 0:\n{result.stdout}\n{result.stderr}"
    assert "would run:" in result.stdout, result.stdout

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
        path=f"{cli}:{curl}:{base_path(tmp_path)}",
    )
    assert "aisquare installer" in result.stdout
    assert "~/.aisquare/" in result.stdout, "the banner must list what gets written (§3.7)"
    assert "nothing above was actually run" in result.stdout


# ---------------------------------------------------------------------------
# preflight — §3.7 and §0.9
# ---------------------------------------------------------------------------


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

#: The same, for a run that registered no project: `snapshot` is amber too, and
#: that is the requested state rather than a defect.
_DOCTOR_PAYLOAD_NO_PROJECT = (
    '[{"name": "home", "status": "ok", "detail": "ok", "fix": null},'
    '{"name": "snapshot", "status": "warn", "detail": "no snapshot", "fix": "onboard"},'
    '{"name": "brain", "status": "warn", "detail": "gbrain not found", "fix": "optional"}]'
)

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


def test_snapshot_is_expected_amber_when_no_project_was_registered(tmp_path: Path) -> None:
    """`--no-project` asks for a machine with no project. So `snapshot` is amber
    by design, and calling that unexpected has two consequences that were both
    measured before this was fixed:

    * §3.9.4's "nothing to do" became UNREACHABLE in the whole `--no-project`
      mode. However current the machine was, `snapshot` was in the amber set,
      the set never equalled `brain`, and every re-run walked the full flow —
      installing nothing, but printing a page of steps and never saying the one
      thing the user wanted to hear.
    * the summary told the reader to run `aisquare project onboard` for a
      project that does not exist. Advice that cannot work is worse than no
      advice.
    """
    for flag, expected in (("WANT_PROJECT=0", "brain snapshot"), ("", "brain")):
        setup = flag or "PROJECT_DIR=/somewhere; WANT_PROJECT=1"
        result = sh(f"{setup}; expected_amber; echo", path=base_path(tmp_path))
        assert result.stdout.strip() == expected, (
            f"with `{setup}` expected_amber said {result.stdout.strip()!r}, "
            f"wanted {expected!r}\n{result.stderr}"
        )


def test_a_repoless_directory_also_expects_snapshot_amber(tmp_path: Path) -> None:
    """Not just the flag: running from a directory that is no git repo lands in
    the same state (§4), and the honest report is the same."""
    result = sh('WANT_PROJECT=1; PROJECT_DIR=""; expected_amber; echo', path=base_path(tmp_path))
    assert result.stdout.strip() == "brain snapshot", result.stderr


def test_doctor_amber_is_sorted_so_the_comparison_is_about_the_set(tmp_path: Path) -> None:
    """A reordering inside `doctor()` is not a regression and must not read as one.

    The short-circuit compares the amber list against `expected_amber` as a
    STRING, so both sides have to be sorted or a harmless reshuffle of the check
    order would silently disable the whole no-op path.
    """
    payload = (
        '[{"name": "snapshot", "status": "warn", "detail": "x", "fix": "y"},'
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
    assert result.stdout.strip() == "brain snapshot", (
        f"doctor_amber returned {result.stdout.strip()!r} — it must sort, or the "
        f"short-circuit breaks on a reordering\n{result.stderr}"
    )


def test_the_short_circuit_fires_for_a_current_no_project_machine(tmp_path: Path) -> None:
    """§3.9.4 end to end, in the mode where it was unreachable.

    A machine that has everything, with `--no-project`, must print its summary
    and exit 0 having installed nothing. The stubs record any call that is not a
    version read, so "installed nothing" is observed rather than asserted.
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
        ("node", "v26.7.0"),
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
            f"  --json) printf '%s' '{_DOCTOR_PAYLOAD_NO_PROJECT}'; exit 0 ;;\n"
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
    assert "no project registered" in result.stdout, (
        f"and it must say WHY snapshot is amber:\n{result.stdout}"
    )
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
    `tmux` and `repomix` were classed UNEXPECTED, `UNEXPECTED` was incremented
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
    assert {"tmux", "gh", "repomix"} <= expected, (
        f"--no-system-deps must expect the checks it skipped, got {sorted(expected)}"
    )
    for name in ("tmux", "repomix", "gh"):
        member = sh(
            f"WANT_SYSTEM_DEPS=0; WANT_PROJECT=0; "
            f"if is_expected_amber {name}; then echo yes; else echo no; fi",
            path=base_path(tmp_path),
        )
        assert member.stdout.strip() == "yes", name


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
    check that was perfectly fine.
    """
    payload = (
        '[{"name": "home", "status": "ok", "detail": "x"},'
        '{"name": "snapshot", "status": "warn", "detail": "y", "fix": "z"}]'
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
        "WANT_AGENT=0; WANT_PROJECT=0; OFFLINE=1; "
        "survey >/dev/null 2>&1; resolve >/dev/null 2>&1; short_circuit",
        env={"AISQUARE_INSTALL_VERSION": ""},
        path=f"{versions}:{cli}:{base_path(tmp_path)}",
    )
    assert "Nothing to do" in result.stdout, result.stdout
    assert "gbrain" not in result.stdout, (
        "the reason named gbrain while `brain` was green:\n" + result.stdout
    )
    assert "no project registered" in result.stdout, result.stdout


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
