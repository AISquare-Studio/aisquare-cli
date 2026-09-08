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
import shutil
import subprocess
from pathlib import Path

import pytest

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
    result = sh(
        'OS=linux; pkg_manager; printf "%s\\n" "$DNF_MAJOR"',
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


def test_yes_answers_without_a_terminal_and_without_asking(tmp_path: Path) -> None:
    """`--yes` and "no terminal" are different reasons for the same silence.

    `--yes` means "do not stop to ask me" and answers y; no-terminal means
    "there is nobody to ask" and takes the default. Collapsing them would make
    `--yes` unable to accept anything on a CI box, or make a Dockerfile install
    Homebrew by accident.
    """
    result = sh(
        'ASSUME_YES=1; if confirm "install a thing?" n; then echo yes; else echo no; fi',
        path=base_path(tmp_path),
        no_terminal=True,
    )
    assert result.stdout.strip() == "yes", result.stdout


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
