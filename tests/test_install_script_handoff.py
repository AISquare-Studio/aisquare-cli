"""main's install.sh hands off to the latest release's own stamped install.sh.

main's copy installs the latest release from PyPI, but its steps and checks are
main's, and those can be ahead of that release (#399 dropped ``--with tiktoken``
while the latest release's doctor still had a tiktoken row). So an UNSTAMPED
copy downloads the release's own install.sh and runs that with the same
arguments, and carries on itself on any failure. What is pinned here:

* the hand-off happens, with the arguments and the exit status passed through;
* a 404, a network error, a timeout, and a download that is not a stamped POSIX
  sh script for the wanted version each fall back, in one line;
* a stamped copy never hands off, and the guard it hands to its child stops a
  second hop;
* ``--version V`` fetches vV's installer; the opt-outs and
  ``AISQUARE_INSTALL_PACKAGE`` turn the hand-off off;
* the download is removed after the run, and after an interrupted one;
* under ``curl … | sh`` the child never reads stdin, so the rest of the parent's
  input is still unread when it ends, and the release's prompt reads the
  terminal;
* install.ps1 has no hand-off of its own: it hands WSL main's install.sh, in a
  command WSL's default shell passes through untouched.

Every test drives the real script with ``sh install.sh`` or ``sh -s`` and a fake
downloader on PATH that serves files from a directory, so nothing reaches the
network. The served release is either a copy of THIS tree stamped by
scripts/stamp_installers.py, or a small stamped script that reports how it ran.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

if sys.platform == "win32":  # pragma: no cover - the POSIX installer's own tests
    pytest.skip("install.sh is a POSIX shell script", allow_module_level=True)

REPO = Path(__file__).resolve().parents[1]
#: Always the repository's copy, never AISQUARE_TEST_INSTALL_SH: the hand-off is
#: what an UNSTAMPED script does, and a stamped one is made here when needed.
SCRIPT = REPO / "install.sh"
SH = shutil.which("sh") or "/bin/sh"
RELEASES = "https://github.com/AISquare-Studio/aisquare-cli/releases"
LATEST_URL = f"{RELEASES}/latest/download/install.sh"
PYPI_URL = "https://pypi.org/pypi/aisquare-cli/json"
#: The release the served installer is stamped to. Not the tree's own version,
#: so nothing here moves when pyproject.toml does.
STAMP = "7.7.7"
#: The flags of a run that ends quickly and changes nothing.
DRY = ("--dry-run", "--yes", "--no-project", "--no-agent", "--no-system-deps")


def _load_stamper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "stamp_installers", REPO / "scripts" / "stamp_installers.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stamper = _load_stamper()


def stamped_copy(version: str = STAMP) -> str:
    """THIS tree's install.sh as release ``version`` would attach it."""
    text: str = stamper.stamp_sh(SCRIPT.read_text(encoding="utf-8"), f"v{version}")
    return text


def reporting_release(
    version: str = STAMP,
    *,
    options: tuple[str, ...] = (
        "--yes",
        "--no-agent",
        "--no-system-deps",
        "--no-project",
        "--project",
        "--version",
        "--version=*",
        "--dry-run",
    ),
    body: str = "exit 3",
) -> str:
    """A stamped release installer that reports how it was run.

    Its `case` has a branch for each of ``options``, written as install.sh's
    parse_args writes them, which is what the hand-off's option check reads;
    exit 3 is a status neither script uses, so it can only have come from here.
    """
    branches = "".join(f"        {option}) ;;\n" for option in options)
    return (
        "#!/bin/sh\n"
        f'STAMPED_VERSION="{version}"\n'
        'for arg in "$@"; do\n    case "$arg" in\n' + branches + "    esac\ndone\n"
        'printf "RELEASE RAN handed_off=%s\\n" "${AISQUARE_INSTALL_HANDED_OFF:-}"\n'
        'printf "RELEASE VERSION_ENV=%s\\n" "${AISQUARE_INSTALL_VERSION-unset}"\n'
        'printf "RELEASE IS %s\\n" "$0"\n'
        'for arg in "$@"; do printf "ARG[%s]\\n" "$arg"; done\n'
        f"{body}\n"
    )


#: The utilities the script and the fakes use, linked into a directory so the
#: PATH can be narrowed without losing them.
_TOOLS = (
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
    "sleep",
    "touch",
    "sh",
)


def _fake_downloader(name: str, served: Path, log: Path) -> str:
    """curl or wget, serving release URLs and PyPI's JSON from ``served``.

    A missing file is an HTTP 404, and an ``HTTP`` file makes every release URL
    answer the status it holds. Either is reported the way the real tool does it:
    curl exits 22 saying "error: 404", GNU wget exits 8 saying "ERROR 404". A
    ``STATUS`` file makes every release URL fail with the exit code it holds
    instead (6 cannot resolve, 28 timed out), and a ``HANG`` file makes it hang,
    after touching ``HANG.started``.
    """
    if name == "curl":
        http_error = 'echo "curl: (22) The requested URL returned error: $1" >&2; exit 22'
    else:
        http_error = 'echo "ERROR $1: Not Found." >&2; exit 8'
    return f"""#!/bin/sh
printf '%s %s\\n' {name} "$*" >>"{log}"
http_error() {{ {http_error}; }}
out=-
url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o | -O) out=$2; shift ;;
        -qO- | -O-) out=- ;;
        http*) url=$1 ;;
    esac
    shift
done
case "$url" in
    {RELEASES}/*)
        [ ! -f "{served}/STATUS" ] || exit "$(cat "{served}/STATUS")"
        if [ -f "{served}/HANG" ]; then touch "{served}/HANG.started"; sleep 30; fi
        [ ! -f "{served}/HTTP" ] || http_error "$(cat "{served}/HTTP")"
        file="{served}/${{url#{RELEASES}/}}"
        ;;
    {PYPI_URL}) file="{served}/pypi.json" ;;
    *) exit 7 ;;
esac
[ -f "$file" ] || http_error 404
if [ "$out" = - ]; then cat "$file"; else cat "$file" >"$out"; fi
"""


@dataclass
class Machine:
    """A stubbed machine: a fake downloader, an installed aisquare, a private TMPDIR."""

    root: Path
    bin: Path
    served: Path
    log: Path
    tmp: Path
    home: Path
    path: str

    def serve(self, where: str, text: str) -> None:
        """Serve ``text`` at ``{RELEASES}/{where}``."""
        target = self.served / where
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def serve_latest(self, text: str) -> None:
        self.serve("latest/download/install.sh", text)

    def pypi(self, version: str) -> None:
        (self.served / "pypi.json").write_text(
            '{"info":{"name":"aisquare-cli","version":"' + version + '"},"last_serial":1}',
            encoding="utf-8",
        )

    def fail_downloads(self, status: int) -> None:
        (self.served / "STATUS").write_text(str(status), encoding="utf-8")

    def requests(self) -> list[str]:
        """Each URL the downloader was asked for, in order."""
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [word for line in lines for word in line.split() if word.startswith("http")]

    def release_requests(self) -> list[str]:
        return [url for url in self.requests() if url.startswith(RELEASES)]

    def leftovers(self) -> list[str]:
        """Whatever the run left in its TMPDIR: the download must not be there."""
        return sorted(entry.name for entry in self.tmp.iterdir())

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        # Built from nothing, so no AISQUARE_INSTALL_* of the developer's leaks in.
        environment = {
            "PATH": self.path,
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "NO_COLOR": "1",
        }
        environment.update(extra or {})
        return environment

    def run(
        self, *args: str, env: dict[str, str] | None = None, script: Path = SCRIPT
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [SH, str(script), *args],
            capture_output=True,
            text=True,
            env=self.env(env),
            cwd=self.root,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    bin_dir = tmp_path / "bin"
    real = tmp_path / "realbin"
    served = tmp_path / "served"
    tmp = tmp_path / "tmp"
    home = tmp_path / "home"
    for directory in (bin_dir, real, served, tmp, home):
        directory.mkdir()
    for tool in _TOOLS:
        found = shutil.which(tool)
        if found:
            (real / tool).symlink_to(found)
    log = tmp_path / "downloads.log"
    _executable(bin_dir / "curl", _fake_downloader("curl", served, log))
    _executable(
        bin_dir / "aisquare",
        '#!/bin/sh\ncase "$1" in --version) echo "aisquare 0.6.0" ;; esac\nexit 0\n',
    )
    return Machine(tmp_path, bin_dir, served, log, tmp, home, f"{bin_dir}:{real}")


def _args(output: str) -> list[str]:
    return re.findall(r"^ARG\[(.*)\]$", output, re.MULTILINE)


# ---------------------------------------------------------------------------
# The hand-off
# ---------------------------------------------------------------------------


def test_main_hands_off_to_the_latest_release_with_the_same_arguments(machine: Machine) -> None:
    """The release's script runs with every argument as given, and its status is the run's.

    With no version named, ``--version latest`` comes last: the release installs unpinned."""
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)
    spaced = machine.root / "a project with spaces"
    spaced.mkdir()

    result = machine.run("--yes", "--project", str(spaced), "--no-agent")

    assert result.returncode == 3, f"not the release's status:\n{result.stdout}\n{result.stderr}"
    assert _args(result.stdout) == [
        "--yes",
        "--project",
        str(spaced),
        "--no-agent",
        "--version",
        "latest",
    ]
    assert result.stdout.count("using the v7.7.7 installer") == 1, result.stdout
    # The second-hop guard names the script it was handed to, which is the child.
    child = re.search(r"^RELEASE IS (.+)$", result.stdout, re.MULTILINE)
    assert child and child.group(1).startswith(str(machine.tmp)), result.stdout
    assert f"RELEASE RAN handed_off={child.group(1)}" in result.stdout, result.stdout
    # main's own steps never ran: the hand-off comes before anything prints.
    assert "Checking this machine" not in result.stdout, result.stdout
    assert machine.requests() == [LATEST_URL, PYPI_URL]
    assert machine.leftovers() == [], "the downloaded installer was left behind"


def test_both_downloads_are_short_and_https_only(machine: Machine) -> None:
    """The release's installer and PyPI's answer: seconds, not minutes, over HTTPS.

    A hand-off that hung on a stalled GitHub or PyPI before printing a line
    would read as a frozen installer; and a redirect must not leave HTTPS.
    """
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)
    machine.run("--yes")
    lines = machine.log.read_text(encoding="utf-8").splitlines()
    for url in (LATEST_URL, PYPI_URL):
        line = next(line for line in lines if url in line)
        for flag in (
            "--connect-timeout 5",
            "--max-time 30",
            "--proto =https",
            "--proto-redir =https",
        ):
            assert flag in line, f"{url} was fetched without {flag}: {line}"


def test_a_release_copy_of_this_tree_runs_its_own_dry_run(machine: Machine) -> None:
    """The real thing: this tree stamped as a release, handed the same flags.

    The child installs the latest release UNPINNED, as main's copy would: no
    version was named, so it is handed ``--version latest``, the stamped script's
    way out of its own pin (REVIEW-401 prep for #402, and m4). It honours
    ``--dry-run``, and the parent prints its one line and nothing else.
    """
    machine.serve_latest(stamped_copy())
    machine.pypi(STAMP)

    result = machine.run(*DRY)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout.splitlines()[0].strip() == "using the v7.7.7 installer"
    assert "pinned to" not in result.stdout, result.stdout
    assert "aisquare-cli@latest" in result.stdout, "the child did not install unpinned"
    assert "aisquare-cli@7.7.7" not in result.stdout and "aisquare-cli==" not in result.stdout
    pypi = [url for url in machine.requests() if "pypi" in url]
    assert len(pypi) == 1, f"PyPI asked {len(pypi)} times: the child reuses the parent's answer"
    assert "nothing above was actually run" in result.stdout
    assert result.stdout.count("Checking this machine") == 1, "the survey ran twice"
    # The stamped child hops nowhere: one request for the release, one for PyPI.
    assert machine.release_requests() == [LATEST_URL]
    assert machine.leftovers() == []


def _node_lines(output: str) -> list[str]:
    """What a run says about Node and the Chrome DevTools extra that needs it."""
    return [
        line.strip() for line in output.splitlines() if "Node" in line or "chrome-devtools" in line
    ]


@pytest.mark.parametrize(
    "extra",
    [(), ("--with", "chrome-devtools"), ("--with=chrome-devtools",)],
    ids=["no-extra", "with", "with="],
)
def test_the_hand_off_and_this_copy_agree_on_node(machine: Machine, extra: tuple[str, ...]) -> None:
    """#399 made Node optional: installed for the Chrome DevTools extra alone, chosen with
    ``--with chrome-devtools``. Whichever script runs, the release's (handed off, ``--with``
    passed through and known to its parse_args) or this one (no hand-off), the same flags
    plan the same Node. Main's copy installs nothing before it hands off."""
    machine.serve_latest(stamped_copy())
    machine.pypi(STAMP)

    handed = machine.run(*DRY, *extra)
    kept = machine.run(*DRY, *extra, env={"AISQUARE_INSTALL_NO_HANDOFF": "1"})

    assert handed.returncode == 0 and kept.returncode == 0, f"{handed.stdout}\n{kept.stdout}"
    assert handed.stdout.splitlines()[0].strip() == "using the v7.7.7 installer", handed.stdout
    assert "using" not in kept.stdout.splitlines()[0], kept.stdout
    assert _node_lines(handed.stdout) == _node_lines(kept.stdout)
    installs_node = "==> Installing Node 22 (browser tools need 22.12+)"
    if extra:
        assert _node_lines(handed.stdout).count(installs_node) == 1, handed.stdout
    else:
        assert installs_node not in _node_lines(handed.stdout), handed.stdout
        assert any(
            "optional; --with chrome-devtools adds it" in line
            for line in _node_lines(handed.stdout)
        )


def test_a_release_copy_of_this_tree_keeps_a_pin(machine: Machine) -> None:
    """The real thing, pinned: ``--version V`` goes as given, so vV's own installer installs
    exactly V (review of #401, m4: the unpinned run's ``--version latest`` is the only way
    out of the stamp, and it is never added to a pin)."""
    machine.serve(f"download/v{STAMP}/install.sh", stamped_copy())

    result = machine.run(*DRY, "--version", STAMP)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout.splitlines()[0].strip() == "using the v7.7.7 installer"
    assert f"pinned to {STAMP}" in result.stdout, result.stdout
    assert "aisquare-cli@latest" not in result.stdout, result.stdout
    assert machine.release_requests() == [f"{RELEASES}/download/v{STAMP}/install.sh"]


def test_a_wget_only_machine_hands_off_too(machine: Machine) -> None:
    """Alpine ships BusyBox wget and no curl, and gets the release's script all the same."""
    (machine.bin / "curl").unlink()
    _executable(machine.bin / "wget", _fake_downloader("wget", machine.served, machine.log))
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)

    result = machine.run("--yes")

    assert result.returncode == 3, f"{result.stdout}\n{result.stderr}"
    assert "using the v7.7.7 installer" in result.stdout
    # One try: GNU wget's default is 20, and -T limits each read, not the download.
    for url in (LATEST_URL, PYPI_URL):
        line = next(
            line for line in machine.log.read_text(encoding="utf-8").splitlines() if url in line
        )
        assert line.startswith("wget ") and "-t 1 -T 30" in line, line


@pytest.mark.parametrize(
    ("status", "why"),
    [
        ("404", "the latest release has no installer attached"),
        ("503", "GitHub answered with an error for the latest release's installer"),
    ],
)
def test_wgets_http_errors_read_like_curls(machine: Machine, status: str, why: str) -> None:
    """GNU wget exits 8 for any HTTP error, and says which in its own words."""
    (machine.bin / "curl").unlink()
    _executable(machine.bin / "wget", _fake_downloader("wget", machine.served, machine.log))
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)
    (machine.served / "HTTP").write_text(status, encoding="utf-8")

    result = machine.run(*DRY)

    assert result.returncode == 0, result.stderr
    assert f"using this installer ({why})" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Every failure falls back to this script, in one line
# ---------------------------------------------------------------------------


def _half(text: str) -> str:
    """Cut inside a function body, so the result cannot parse."""
    return text[: text.index("release_checked() {") + 200]


@pytest.mark.parametrize(
    ("arrange", "why"),
    [
        (lambda m: None, "the latest release has no installer attached"),
        (lambda m: m.fail_downloads(6), "could not download the latest release's installer"),
        (lambda m: m.fail_downloads(28), "GitHub did not answer in time"),
        (
            lambda m: m.serve_latest("<!DOCTYPE html><title>Not Found</title>\n"),
            "the latest release's installer is not a POSIX sh script",
        ),
        (
            lambda m: m.serve_latest(""),
            "the latest release's installer is not a POSIX sh script",
        ),
        (
            lambda m: m.serve_latest(_half(stamped_copy())),
            "the latest release's installer is not a POSIX sh script",
        ),
        (
            lambda m: m.serve_latest(SCRIPT.read_text(encoding="utf-8")),
            "the latest release's installer carries no release stamp",
        ),
        (
            lambda m: m.serve_latest(stamped_copy("7.7.6")),
            "the latest release's installer is stamped 7.7.6, not 7.7.7",
        ),
        (
            lambda m: m.serve_latest(
                reporting_release(options=("--dry-run", "--yes", "--no-agent", "--no-project"))
            ),
            "the v7.7.7 installer has no --no-system-deps option",
        ),
        (
            # Named in its text, as a usage line would, but not a branch it parses.
            lambda m: m.serve_latest(
                reporting_release(
                    options=("--dry-run", "--yes", "--no-agent", "--no-project"),
                    body="# --no-system-deps   Skip tmux, gh and Node.\nexit 3",
                )
            ),
            "the v7.7.7 installer has no --no-system-deps option",
        ),
        (
            lambda m: (m.served / "HTTP").write_text("503", encoding="utf-8"),
            "GitHub answered with an error for the latest release's installer",
        ),
    ],
    ids=[
        "404",
        "network-error",
        "timeout",
        "error-page",
        "empty",
        "truncated",
        "no-stamp",
        "mismatched-stamp",
        "unknown-option",
        "mentioned-not-parsed",
        "http-503",
    ],
)
def test_a_failed_hand_off_falls_back_in_one_line(
    machine: Machine, arrange: Callable[[Machine], object], why: str
) -> None:
    machine.pypi(STAMP)
    arrange(machine)

    result = machine.run(*DRY)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"using this installer ({why})" in result.stdout, result.stdout
    assert len(re.findall(r"^  using ", result.stdout, re.MULTILINE)) == 1, result.stdout
    assert "RELEASE RAN" not in result.stdout, "a refused installer still ran"
    # And this script carried on: its own survey and its own dry run.
    assert "Checking this machine" in result.stdout
    assert "nothing above was actually run" in result.stdout
    assert machine.leftovers() == [], "the refused download was left behind"


def test_a_fallback_reuses_pypis_answer(machine: Machine) -> None:
    """The hand-off asked PyPI already; `resolve` does not ask again."""
    machine.serve_latest(stamped_copy("7.7.6"))
    machine.pypi(STAMP)

    result = machine.run(*DRY)

    assert "is stamped 7.7.6, not 7.7.7" in result.stdout, result.stdout
    assert machine.requests().count(PYPI_URL) == 1, machine.requests()
    # And resolve has that answer: 0.6.0 is behind 7.7.7, with no warning.
    assert "Upgrading aisquare-cli 0.6.0 -> 7.7.7" in result.stdout, result.stdout
    assert "could not read the latest" not in result.stdout + result.stderr


def test_an_unreadable_pypi_falls_back(machine: Machine) -> None:
    """Without PyPI's latest there is nothing to check the release's stamp against."""
    machine.serve_latest(reporting_release())

    result = machine.run(*DRY)

    assert result.returncode == 0, result.stderr
    assert "using this installer (could not read the latest version from PyPI" in result.stdout
    assert "RELEASE RAN" not in result.stdout


# ---------------------------------------------------------------------------
# No recursion
# ---------------------------------------------------------------------------


def test_a_stamped_copy_never_hands_off(machine: Machine) -> None:
    """A release's own installer is the end of the line, whatever is served."""
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)
    release = machine.root / "release-install.sh"
    release.write_text(stamped_copy(), encoding="utf-8")

    result = machine.run(*DRY, script=release)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert machine.release_requests() == [], "a stamped installer asked for a release"
    assert "using " not in result.stdout
    assert "aisquare-cli@7.7.7" in result.stdout


def test_the_guard_stops_a_second_hop(machine: Machine) -> None:
    """The child gets AISQUARE_INSTALL_HANDED_OFF naming itself (asserted above):
    the script it names does not hop again."""
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)

    result = machine.run(*DRY, env={"AISQUARE_INSTALL_HANDED_OFF": str(SCRIPT)})

    assert result.returncode == 0, result.stderr
    assert machine.release_requests() == []
    assert "RELEASE RAN" not in result.stdout


@pytest.mark.parametrize("stale", ["1", "/tmp/aisquare-release-gone"])
def test_a_guard_inherited_from_an_old_hand_off_does_not_stop_this_one(
    machine: Machine, stale: str
) -> None:
    """0.9.0 does not unset the guard, so asq, tmux and every agent it starts
    inherit it; the one-liner run later from an agent's pane must still hand off."""
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)

    result = machine.run("--yes", env={"AISQUARE_INSTALL_HANDED_OFF": stale})

    assert result.returncode == 3, f"{result.stdout}\n{result.stderr}"
    assert f"handed_off={stale}" not in result.stdout, "the stale value reached the child"


@pytest.mark.parametrize("stamped", [False, True], ids=["main", "release"])
def test_the_guard_is_unset_before_anything_else_runs(
    machine: Machine, tmp_path: Path, stamped: bool
) -> None:
    """Whatever the run starts afterwards, asq, tmux and agents, never sees it.

    The release's copy matters most: it is the script whose run ends in
    `exec asq`, so it unsets the guard before its own stamp check returns.
    """
    script = SCRIPT
    if stamped:
        script = tmp_path / "release-install.sh"
        script.write_text(stamped_copy(), encoding="utf-8")
    result = subprocess.run(
        [
            SH,
            "-c",
            f'. "{script}"; NO_HANDOFF=1; hand_off; '
            'echo "[${AISQUARE_INSTALL_HANDED_OFF-unset}]"',
        ],
        capture_output=True,
        text=True,
        env=machine.env({"AISQUARE_INSTALL_LIB": "1", "AISQUARE_INSTALL_HANDED_OFF": "/somewhere"}),
        timeout=60,
    )
    assert result.stdout.strip() == "[unset]", result.stdout + result.stderr


# ---------------------------------------------------------------------------
# A pinned version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (("--version", STAMP), {}),
        ((f"--version={STAMP}",), {}),
        ((), {"AISQUARE_INSTALL_VERSION": STAMP}),
    ],
    ids=["flag", "flag=", "env"],
)
def test_a_pin_fetches_that_releases_installer(
    machine: Machine, args: tuple[str, ...], env: dict[str, str]
) -> None:
    """``--version V`` runs vV's own installer, and never needs PyPI to check it."""
    machine.serve(f"download/v{STAMP}/install.sh", reporting_release())
    machine.serve_latest(reporting_release("9.9.9"))

    result = machine.run("--yes", *args, env=env)

    assert result.returncode == 3, f"{result.stdout}\n{result.stderr}"
    assert machine.requests() == [f"{RELEASES}/download/v{STAMP}/install.sh"]
    assert _args(result.stdout) == ["--yes", *args]


@pytest.mark.parametrize(
    ("args", "env", "handed", "version_env"),
    [
        ((), {}, ("--version", "latest"), "unset"),
        (("--version", "latest"), {}, ("--version", "latest"), "unset"),
        (("--version", STAMP), {}, ("--version", STAMP), "unset"),
        ((), {"AISQUARE_INSTALL_VERSION": STAMP}, (), STAMP),
    ],
    ids=["no-version", "latest", "pinned-flag", "pinned-env"],
)
def test_no_version_named_hands_off_unpinned_and_a_pin_stays_a_pin(
    machine: Machine,
    args: tuple[str, ...],
    env: dict[str, str],
    handed: tuple[str, ...],
    version_env: str,
) -> None:
    """REVIEW-401 prep for #402: handed only its arguments, the release's stamped script fell
    back on its stamp, so every plain one-liner install was pinned (``aisquare-cli==V``) and
    ``uv tool upgrade`` never moved it. With no version named the child is handed
    ``--version latest`` and installs unpinned; ``--version V`` and
    ``AISQUARE_INSTALL_VERSION=V`` pin as before.

    Review of #401, m4: it was handed ``AISQUARE_INSTALL_VERSION`` set and empty, which its
    ``exec asq`` carried into tmux and every agent, so a release's own installer run later
    from one of them installed the latest, not its version. The child's environment, and so
    asq's, carries no ``AISQUARE_INSTALL_VERSION`` that the user did not set."""
    machine.serve(f"download/v{STAMP}/install.sh", reporting_release())
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)

    result = machine.run("--yes", *args, env=env)

    assert result.returncode == 3, f"{result.stdout}\n{result.stderr}"
    assert _args(result.stdout) == ["--yes", *handed], result.stdout
    assert f"RELEASE VERSION_ENV={version_env}" in result.stdout.splitlines(), result.stdout


def test_a_pin_to_a_release_without_installers_falls_back(machine: Machine) -> None:
    """Releases before 0.9.0 attach no installers; this script installs the pin itself."""
    result = machine.run(*DRY, "--version", "0.8.0")

    assert result.returncode == 0, result.stderr
    assert "using this installer (v0.8.0 has no installer attached)" in result.stdout
    assert "pinned to 0.8.0" in result.stdout
    assert machine.release_requests() == [f"{RELEASES}/download/v0.8.0/install.sh"]


def test_a_pinned_installer_must_carry_that_pin(machine: Machine) -> None:
    machine.serve(f"download/v{STAMP}/install.sh", reporting_release("7.7.6"))

    result = machine.run(*DRY, "--version", STAMP)

    assert "using this installer (v7.7.7's installer is stamped 7.7.6, not 7.7.7)" in result.stdout
    assert "RELEASE RAN" not in result.stdout


@pytest.mark.parametrize("pin", ["v0.9.0", "0.9.0;touch x", "../latest", "-1"])
def test_a_pin_that_is_not_a_version_never_reaches_a_url(machine: Machine, pin: str) -> None:
    """Only a release version goes into the URL; anything else is this script's to refuse."""
    result = machine.run(*DRY, f"--version={pin}")

    assert f"using this installer ({pin} is not a release version)" in result.stdout, result.stdout
    assert machine.release_requests() == []


def test_latest_is_the_latest_release(machine: Machine) -> None:
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)

    result = machine.run("--yes", "--version", "latest")

    assert result.returncode == 3, result.stderr
    assert machine.release_requests() == [LATEST_URL]


# ---------------------------------------------------------------------------
# The opt-outs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "env"),
    [
        (("--no-handoff",), {}),
        ((), {"AISQUARE_INSTALL_NO_HANDOFF": "1"}),
        ((), {"AISQUARE_INSTALL_PACKAGE": "/mnt/dist/aisquare_cli-0.0.0-py3-none-any.whl"}),
        (("--offline",), {}),
    ],
    ids=["--no-handoff", "AISQUARE_INSTALL_NO_HANDOFF", "AISQUARE_INSTALL_PACKAGE", "--offline"],
)
def test_the_opt_outs_run_this_script(
    machine: Machine, args: tuple[str, ...], env: dict[str, str]
) -> None:
    """A release that WOULD be handed to is served; none of these may ask for it.

    AISQUARE_INSTALL_PACKAGE is how CI installs THIS tree, so the install matrix
    keeps grading this tree's script rather than the last release's.
    """
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)

    result = machine.run(*DRY, *args, env=env)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert machine.release_requests() == []
    assert "RELEASE RAN" not in result.stdout
    assert "using " not in result.stdout, "an opt-out is not news"
    assert "nothing above was actually run" in result.stdout


def test_help_documents_the_hand_off_and_its_opt_outs(machine: Machine) -> None:
    result = machine.run("--help")
    assert result.returncode == 0
    for needle in ("--no-handoff", "AISQUARE_INSTALL_NO_HANDOFF=1", "hands off"):
        assert needle in result.stdout, f"--help does not mention {needle}"
    assert machine.requests() == [], "--help must not reach the network"


# ---------------------------------------------------------------------------
# Cleaning up
# ---------------------------------------------------------------------------


def _interrupted(
    machine: Machine, body: str, signum: signal.Signals, *, during_download: bool = False
) -> tuple[int, str]:
    """Run main's script into a release whose script runs ``body``, signal the
    whole process group once that script has started (or, ``during_download``,
    while its download hangs), as Ctrl-C at a terminal does, and return the
    run's status and output.

    Started with SIGINT and SIGTERM at their defaults, as a terminal gives them.
    A suite started as a background job (`pytest &` in a script) runs with
    SIGINT IGNORED, POSIX's rule for an asynchronous list, and an ignored
    disposition is inherited across exec; a non-interactive shell cannot trap a
    signal ignored on entry, so the INT case would then grade the runner rather
    than the script. Measured: it failed only when the suite ran under `&`.
    """
    started = machine.root / "release-started"
    machine.serve_latest(reporting_release(body=f'touch "{started}"\n{body}'))
    machine.pypi(STAMP)
    if during_download:
        (machine.served / "HANG").touch()
        started = machine.served / "HANG.started"
    as_from_a_terminal = (
        "import os, signal, sys\n"
        "for signum in (signal.SIGINT, signal.SIGTERM):\n"
        "    signal.signal(signum, signal.SIG_DFL)\n"
        "os.execv(sys.argv[1], sys.argv[1:])\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", as_from_a_terminal, SH, str(SCRIPT), "--yes"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=machine.env(),
        cwd=machine.root,
        start_new_session=True,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not started.exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "the release installer never started"
            time.sleep(0.05)
        assert len(machine.leftovers()) == 1, "the download should exist while it runs"
        os.killpg(process.pid, signum)
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failure above
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return process.returncode, output


@pytest.mark.parametrize(
    ("signum", "status"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)], ids=["INT", "TERM"]
)
def test_an_interrupted_hand_off_removes_the_download(
    machine: Machine, signum: signal.Signals, status: int
) -> None:
    """Ctrl-C reaches the whole process group; the download is removed all the same."""
    returncode, _ = _interrupted(machine, "sleep 30\nexit 3", signum)

    assert returncode == status
    assert machine.leftovers() == [], "an interrupted hand-off left its download behind"


@pytest.mark.parametrize(
    ("signum", "status"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)], ids=["INT", "TERM"]
)
def test_an_interrupted_download_removes_it_too(
    machine: Machine, signum: signal.Signals, status: int
) -> None:
    """Before the release's script runs, the parent's own traps end the run, and
    its EXIT trap removes the half-made download; a shell killed outright by the
    signal would run no trap at all."""
    returncode, output = _interrupted(machine, "exit 3", signum, during_download=True)

    assert returncode == status, output
    assert "RELEASE RAN" not in output
    assert machine.leftovers() == [], "an interrupted download was left behind"


def test_a_release_that_survives_the_signal_keeps_its_status(machine: Machine) -> None:
    """The run's status is the release's: a script that handles Ctrl-C and ends
    cleanly ends the run cleanly, rather than as the parent's 130."""
    returncode, output = _interrupted(
        machine, "trap 'echo SURVIVED; exit 0' INT\nsleep 30\nexit 3", signal.SIGINT
    )

    assert "SURVIVED" in output, output
    assert returncode == 0, output
    assert machine.leftovers() == []


# ---------------------------------------------------------------------------
# `curl … | sh`: stdin is the parent's script, and the child must leave it alone
# ---------------------------------------------------------------------------

#: What follows install.sh on the parent's stdin. It must still be there when
#: the run ends: the parent stops reading at `main "$@"` and the child never
#: reads stdin. Far longer than any shell's read-ahead, and if anything ever
#: executed it, it would say so.
_SENTINEL = b"# END-OF-THE-PARENT'S-STDIN\n"
_TRAILER = (
    b"\n"
    + b"# padding that no shell should ever read past main's call\n" * 900
    + b"echo TRAILER-RAN\n"
    + _SENTINEL
)
#: The most of the trailer a shell may have buffered before running `main`:
#: dash and BusyBox ash read a pipe in blocks, bash a byte at a time.
_READ_AHEAD = 16 * 1024


def _feed(write_end: int, data: bytes) -> threading.Thread:
    """Write ``data`` into the pipe from a thread, as curl would, then close it.

    A thread because nobody reads the trailer: once the pipe is full the write
    blocks until the test drains what was left.
    """

    def write() -> None:
        # BrokenPipeError only if every reader vanished, which the test then reports.
        with os.fdopen(write_end, "wb") as pipe, contextlib.suppress(BrokenPipeError):
            pipe.write(data)

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    return thread


def _drain(read_end: int, writer: threading.Thread) -> bytes:
    """Everything still unread in the pipe once the run has ended."""
    left = b""
    with os.fdopen(read_end, "rb") as pipe:
        while chunk := pipe.read(65536):
            left += chunk
    writer.join(timeout=60)
    return left


def _piped(
    machine: Machine, args: tuple[str, ...]
) -> tuple[subprocess.CompletedProcess[str], bytes]:
    """``cat install.sh <trailer> | sh -s -- args``, keeping a read end of the pipe."""
    read_end, write_end = os.pipe()
    writer = _feed(write_end, SCRIPT.read_bytes() + _TRAILER)
    try:
        result = subprocess.run(
            [SH, "-s", "--", *args],
            capture_output=True,
            text=True,
            stdin=read_end,
            env=machine.env(),
            cwd=machine.root,
            timeout=120,
        )
    except BaseException:
        os.close(read_end)
        raise
    return result, _drain(read_end, writer)


def test_a_piped_hand_off_leaves_the_parents_stdin_unread(machine: Machine) -> None:
    """The manager's question for #62, answered with the real stamped copy.

    Under `curl … | sh` the parent's own text arrives on stdin, and the child
    inherits that stdin. The child must never read it (its prompts go through
    /dev/tty), and the parent must have read all of its script before handing
    off, which it has because `main` is called on the last line. So everything
    after install.sh is still in the pipe when the run ends, and none of it ran.
    """
    machine.serve_latest(stamped_copy())
    machine.pypi(STAMP)

    result, left = _piped(machine, DRY)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout.splitlines()[0].strip() == "using the v7.7.7 installer"
    assert "aisquare-cli@latest" in result.stdout  # no version named: unpinned
    assert "nothing above was actually run" in result.stdout
    assert "TRAILER-RAN" not in result.stdout + result.stderr
    assert left.endswith(_SENTINEL), "the hand-off consumed the parent's stdin"
    assert len(left) >= len(_TRAILER) - _READ_AHEAD, (
        f"only {len(left)} of the trailer's {len(_TRAILER)} bytes were left unread"
    )
    assert machine.leftovers() == []


def test_the_stdin_check_sees_a_child_that_reads_stdin(machine: Machine) -> None:
    """The check above can fail: a release that slurps stdin leaves nothing behind."""
    machine.serve_latest(reporting_release(body="cat >/dev/null\nexit 0"))
    machine.pypi(STAMP)

    result, left = _piped(machine, ("--yes",))

    assert "RELEASE RAN" in result.stdout, result.stdout
    assert left == b"", "the harness cannot see a child that reads stdin"


def _piped_with_a_terminal(
    machine: Machine, args: tuple[str, ...], answer: str
) -> tuple[str, bytes]:
    """`curl … | sh` with a human at the terminal: stdin the pipe, /dev/tty a pty.

    The shape of tests/test_install_script_functions.py's
    `_piped_into_sh_with_a_terminal`, keeping a read end of the pipe so what the
    run left unread can be measured.
    """
    assert sys.platform != "win32"
    import pty

    argv = [SH, "-s", "--", *args]
    environment = machine.env()
    read_end, write_end = os.pipe()
    pid, master = pty.fork()
    if pid == 0:  # child
        try:
            os.close(write_end)
            os.dup2(read_end, 0)
            os.close(read_end)
            os.chdir(machine.root)
            os.execve(argv[0], argv, environment)
        finally:  # pragma: no cover - only reached if exec fails
            os._exit(127)

    writer = _feed(write_end, SCRIPT.read_bytes() + _TRAILER)
    os.write(master, answer.encode())
    output = b""
    try:
        while chunk := os.read(master, 4096):
            output += chunk
    except OSError:
        pass  # EIO on Linux once the child closes the terminal
    os.waitpid(pid, 0)
    os.close(master)
    return output.decode(errors="replace"), _drain(read_end, writer)


@pytest.fixture
def current_machine(machine: Machine) -> Machine:
    """The functions suite's `piped_machine`, at the served release's version.

    `--force` puts the run on the path that ends at the prompt; the installed
    aisquare is already 7.7.7, so the stamped child's upgrade check holds.
    """
    _executable(
        machine.bin / "uv",
        '#!/bin/sh\ncase "$1" in --version) echo "uv 0.12.3 (stub)" ;; esac\nexit 0\n',
    )
    payload = (
        '[{"name": "home", "status": "ok", "detail": "ok", "fix": null},'
        '{"name": "snapshot", "status": "warn", "detail": "no snapshot", "fix": "onboard"},'
        '{"name": "brain", "status": "warn", "detail": "gbrain not found", "fix": "optional"}]'
    )
    _executable(
        machine.bin / "aisquare",
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  --version) echo "aisquare {STAMP}" ;;\n'
        "  --json) printf '%s' '" + payload + "' ;;\n"
        "esac\nexit 0\n",
    )
    _executable(
        machine.bin / "asq",
        '#!/bin/sh\nif [ -t 0 ]; then echo "UI-STARTED stdin=terminal"; '
        'else echo "UI-STARTED stdin=NOT-a-terminal"; fi\n'
        'echo "UI-GUARD=${AISQUARE_INSTALL_HANDED_OFF-unset}"\n',
    )
    machine.serve_latest(stamped_copy())
    machine.pypi(STAMP)
    return machine


_PROMPT_FLAGS = ("--no-project", "--no-agent", "--no-system-deps", "--force")


def test_the_releases_prompt_reads_the_terminal_and_not_the_script(
    current_machine: Machine,
) -> None:
    """Answering `n` to the handed-off installer's prompt declines, as on main's own."""
    output, left = _piped_with_a_terminal(current_machine, _PROMPT_FLAGS, "n\n")

    assert "using the v7.7.7 installer" in output, output
    assert "Open the aisquare fleet UI now?" in output, f"the prompt never appeared:\n{output}"
    assert "UI-STARTED" not in output, f"answering 'n' still launched the UI:\n{output}"
    assert "Open it any time with" in output, output
    assert left.endswith(_SENTINEL), "the prompt read the parent's stdin"
    assert current_machine.leftovers() == []


def test_the_handed_off_ui_gets_the_terminal(current_machine: Machine) -> None:
    """`y` hands over to asq with the terminal on its stdin, through two shells."""
    output, left = _piped_with_a_terminal(current_machine, _PROMPT_FLAGS, "y\n")

    assert "using the v7.7.7 installer" in output, output
    assert "UI-STARTED stdin=terminal" in output, output
    # The guard ends with the hand-off: the UI, its tmux server and every agent
    # never see it.
    assert "UI-GUARD=unset" in output, output
    assert left.endswith(_SENTINEL)


# ---------------------------------------------------------------------------
# install.ps1: main's install.sh does the hand-off, inside WSL
# ---------------------------------------------------------------------------

PS1 = REPO / "install.ps1"


def _expand(literal: str, values: dict[str, str]) -> str:
    """A PowerShell double-quoted string's text: `` `x `` is x, `$Name` its value
    (`X` for a name not in ``values``)."""
    out, index = "", 0
    while index < len(literal):
        char = literal[index]
        name = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)", literal[index:])
        if char == "`" and index + 1 < len(literal):
            out, index = out + literal[index + 1], index + 2
        elif name:
            out, index = out + values.get(name.group(1), "X"), index + len(name.group(0))
        else:
            out, index = out + char, index + 1
    return out


def _ps1_commands(text: str, args: tuple[str, ...]) -> list[str]:
    """Every command install.ps1 assigns to `$inner`, as PowerShell builds it.

    A statement runs on across lines that end in `+`. $Repo, $StampedVersion and
    $InstallUrl are read from the file; $Pin and $Forward are built from
    ``args`` as install.ps1 builds them.
    """
    repo = re.search(r"^\$Repo = '([^']*)'$", text, re.MULTILINE)
    stamp = re.search(r"^\$StampedVersion = '([^']*)'$", text, re.MULTILINE)
    url = re.search(r'^\$InstallUrl = "([^"]*)"$', text, re.MULTILINE)
    assert repo and stamp and url, "install.ps1 lost a line this test reads"
    values = {"Repo": repo.group(1), "StampedVersion": stamp.group(1)}
    values["InstallUrl"] = _expand(url.group(1), values)
    values["Pin"] = f"--version {stamp.group(1)} " if stamp.group(1) else ""
    values["Forward"] = " ".join(args)
    commands, lines = [], text.splitlines()
    for number, line in enumerate(lines):
        if not re.match(r"\s*\$inner = ", line):
            continue
        statement = line
        while statement.rstrip().endswith("+"):
            number += 1
            statement += "\n" + lines[number]
        literals = re.findall(r'"((?:[^"`]|`.)*)"', statement)
        commands.append("".join(_expand(literal, values) for literal in literals))
    return commands


@pytest.mark.parametrize("stamped", [False, True], ids=["main", "release"])
def test_wsls_default_shell_passes_the_command_through_untouched(
    machine: Machine, stamped: bool
) -> None:
    """wsl.exe gives `bash -lc <command>` to the distribution's DEFAULT SHELL,
    which sees the command inside double quotes.

    A `$`, a backtick, a `"` or a `\\` in it is expanded THERE, before bash
    runs it. #402's first install.ps1 hand-off died of exactly that: the outer
    shell ran its `$(mktemp)` and emptied every `$f`, so curl had no URL, every
    run fell back, and each left a temporary file behind. Here the default shell
    runs `printf` in bash's place and must print the command as it was sent.
    """
    text = PS1.read_text(encoding="ascii")
    if stamped:
        text = stamper.stamp_ps1(text, f"v{STAMP}")
    for command in _ps1_commands(text, ("--yes", "--no-agent")):
        seen = subprocess.run(
            [SH, "-c", f'printf "%s\\n" "{command}"'],
            capture_output=True,
            text=True,
            env=machine.env(),
            cwd=machine.root,
            timeout=60,
        )
        assert seen.stdout == command + "\n", (
            f"WSL's default shell rewrote the command:\nsent: {command}\ngot:  {seen.stdout}"
        )


def test_install_ps1_leaves_the_hand_off_to_install_sh() -> None:
    """One command, main's install.sh into `sh -s --`, so install.sh's own
    `hand_off` runs inside WSL: no second implementation to drift from it."""
    text = PS1.read_text(encoding="ascii")
    main = f"{stamper.RAW_MAIN}/install.sh"
    assert _ps1_commands(text, ("--yes",)) == [f"set -e; curl -fsSL '{main}' | sh -s -- --yes"]
    release = stamper.release_url(f"v{STAMP}", "install.sh")
    assert _ps1_commands(stamper.stamp_ps1(text, f"v{STAMP}"), ("--yes",)) == [
        f"set -e; curl -fsSL '{release}' | sh -s -- --version {STAMP} --yes"
    ]
    for second in ("AISQUARE_INSTALL_NO_HANDOFF", "pypi.org", "releases/latest"):
        assert second not in text, f"install.ps1 has a hand-off of its own again ({second})"


# ---------------------------------------------------------------------------
# The docs
# ---------------------------------------------------------------------------


def _page(name: str) -> str:
    return " ".join((REPO / name).read_text(encoding="utf-8").split())


def test_the_install_page_says_main_runs_the_releases_installer() -> None:
    page = _page("docs/install.md")
    assert "The copy on `main` runs that release's own installer for you." in page
    assert "`--no-handoff`" in page
    assert "It runs main's `install.sh` inside WSL, so the hand-off above happens there" in page, (
        "the Windows path is not described"
    )


def test_the_runbook_says_main_hands_off_and_falls_back() -> None:
    page = _page("docs/runbooks/release.md")
    assert "hand off to the latest release's own installer once it is attached" in page
    assert "main's `install.ps1` runs main's `install.sh` inside WSL" in page
    assert "the main one-liner falls back to main's own script" in page
