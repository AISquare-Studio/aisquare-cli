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
  terminal.

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
    options: str = "--yes --no-agent --no-system-deps --no-project --project --version --dry-run",
    body: str = "exit 3",
) -> str:
    """A stamped release installer that reports how it was run.

    Its text names the options it "parses", which is what the hand-off's option
    check reads; exit 3 is a status neither script uses, so it can only have
    come from here.
    """
    return (
        "#!/bin/sh\n"
        f"# parses: {options}\n"
        f'STAMPED_VERSION="{version}"\n'
        'printf "RELEASE RAN handed_off=%s\\n" "${AISQUARE_INSTALL_HANDED_OFF:-}"\n'
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

    A missing file is curl's HTTP-error exit (22); a ``STATUS`` file makes every
    release URL fail with the exit code it holds (6 cannot resolve, 28 timed out).
    """
    return f"""#!/bin/sh
printf '%s %s\\n' {name} "$*" >>"{log}"
out=-
url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o | -O) out=$2; shift ;;
        -qO-) out=- ;;
        http*) url=$1 ;;
    esac
    shift
done
case "$url" in
    {RELEASES}/*)
        [ ! -f "{served}/STATUS" ] || exit "$(cat "{served}/STATUS")"
        file="{served}/${{url#{RELEASES}/}}"
        ;;
    {PYPI_URL}) file="{served}/pypi.json" ;;
    *) exit 7 ;;
esac
[ -f "$file" ] || exit 22
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
    """The release's script runs with every argument as given, and its status is the run's."""
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)
    spaced = machine.root / "a project with spaces"
    spaced.mkdir()

    result = machine.run("--yes", "--project", str(spaced), "--no-agent")

    assert result.returncode == 3, f"not the release's status:\n{result.stdout}\n{result.stderr}"
    assert _args(result.stdout) == ["--yes", "--project", str(spaced), "--no-agent"]
    assert result.stdout.count("using the v7.7.7 installer") == 1, result.stdout
    assert "RELEASE RAN handed_off=1" in result.stdout, "the child must get the second-hop guard"
    # main's own steps never ran: the hand-off comes before anything prints.
    assert "Checking this machine" not in result.stdout, result.stdout
    assert machine.requests() == [LATEST_URL, PYPI_URL]
    assert machine.leftovers() == [], "the downloaded installer was left behind"


def test_the_download_has_a_short_timeout(machine: Machine) -> None:
    """A release installer that has not arrived in seconds is not worth the wait."""
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)
    machine.run("--yes")
    line = next(
        line for line in machine.log.read_text(encoding="utf-8").splitlines() if LATEST_URL in line
    )
    assert "--connect-timeout" in line and "--max-time" in line, line
    assert "-fsSL" in line, line


def test_a_release_copy_of_this_tree_runs_its_own_dry_run(machine: Machine) -> None:
    """The real thing: this tree stamped as a release, handed the same flags.

    The child installs its OWN stamp (``@7.7.7``), honours ``--dry-run``, and
    the parent prints its one line and nothing else.
    """
    machine.serve_latest(stamped_copy())
    machine.pypi(STAMP)

    result = machine.run(*DRY)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert result.stdout.splitlines()[0].strip() == "using the v7.7.7 installer"
    assert "pinned to 7.7.7" in result.stdout, result.stdout
    assert "aisquare-cli@7.7.7" in result.stdout, "the child did not install its own stamp"
    assert "nothing above was actually run" in result.stdout
    assert result.stdout.count("Checking this machine") == 1, "the survey ran twice"
    # The stamped child hops nowhere: one request for the release, one for PyPI.
    assert machine.release_requests() == [LATEST_URL]
    assert machine.leftovers() == []


def test_a_wget_only_machine_hands_off_too(machine: Machine) -> None:
    """Alpine ships BusyBox wget and no curl, and gets the release's script all the same."""
    (machine.bin / "curl").unlink()
    _executable(machine.bin / "wget", _fake_downloader("wget", machine.served, machine.log))
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)

    result = machine.run("--yes")

    assert result.returncode == 3, f"{result.stdout}\n{result.stderr}"
    assert "using the v7.7.7 installer" in result.stdout
    release = next(
        line for line in machine.log.read_text(encoding="utf-8").splitlines() if LATEST_URL in line
    )
    assert release.startswith("wget ") and "-T" in release.split(), release


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
                reporting_release(options="--dry-run --yes --no-agent --no-project")
            ),
            "the v7.7.7 installer has no --no-system-deps option",
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
    """The child gets AISQUARE_INSTALL_HANDED_OFF=1 (asserted above); with it set, no hop."""
    machine.serve_latest(reporting_release())
    machine.pypi(STAMP)

    result = machine.run(*DRY, env={"AISQUARE_INSTALL_HANDED_OFF": "1"})

    assert result.returncode == 0, result.stderr
    assert machine.release_requests() == []
    assert "RELEASE RAN" not in result.stdout


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


@pytest.mark.parametrize(
    ("signum", "status"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)], ids=["INT", "TERM"]
)
def test_an_interrupted_hand_off_removes_the_download(
    machine: Machine, signum: signal.Signals, status: int
) -> None:
    """Ctrl-C reaches the whole process group; the parent's trap removes the file."""
    started = machine.root / "release-started"
    machine.serve_latest(reporting_release(body=f'touch "{started}"\nsleep 30\nexit 3'))
    machine.pypi(STAMP)

    process = subprocess.Popen(
        [SH, str(SCRIPT), "--yes"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=machine.env(),
        cwd=machine.root,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not started.exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "the release installer never started"
            time.sleep(0.05)
        assert len(machine.leftovers()) == 1, "the download should exist while it runs"
        os.killpg(process.pid, signum)
        process.communicate(timeout=60)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failure above
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    assert process.returncode == status
    assert machine.leftovers() == [], "an interrupted hand-off left its download behind"


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
    assert "aisquare-cli@7.7.7" in result.stdout
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
        'else echo "UI-STARTED stdin=NOT-a-terminal"; fi\n',
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
    assert left.endswith(_SENTINEL)


# ---------------------------------------------------------------------------
# install.ps1: the same hand-off, from Windows into WSL
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ps1() -> str:
    return (REPO / "install.ps1").read_text(encoding="ascii")


def _ps1_handoff(ps1: str) -> str:
    start = ps1.index("if (-not $StampedVersion) {")
    return ps1[start : ps1.index("& wsl.exe -d $target -- bash -lc $inner", start)]


def test_mains_ps1_fetches_the_releases_install_sh_and_falls_back_to_mains(ps1: str) -> None:
    """The latest release's install.sh, or V's for --version V; main's when that fails."""
    block = _ps1_handoff(ps1)
    assert '"https://github.com/$Repo/releases/latest/download/install.sh"' in block
    assert '"https://github.com/$Repo/releases/download/v$Want/install.sh"' in block
    # The fallback is main's script told not to try the same hand-off again.
    assert (
        "$Fallback = \"curl -fsSL '$InstallUrl' | AISQUARE_INSTALL_NO_HANDOFF=1 sh -s -- $Forward\""
        in block
    )
    assert "else echo '  using this installer'; $Fallback; fi" in block
    # A release's copy is stamped, fetches its own release's install.sh, and
    # keeps the delegation it always had.
    assert "$inner = \"set -e; curl -fsSL '$InstallUrl' | sh -s -- $Pin$Forward\"" in ps1


def test_the_ps1_checks_what_install_sh_checks(ps1: str) -> None:
    """Shebang, parse, the wanted stamp, the options, the timeout, the opt-outs."""
    block = _ps1_handoff(ps1)
    for check in (
        "grep -qx '#!/bin/sh'",
        "sh -n `$f",
        "grep -qx '$StampLine' `$f$Knows",
        "grep -qF -e '$flag' `$f",
        "--connect-timeout 5 --max-time 30",
        "https://pypi.org/pypi/aisquare-cli/json",
        "AISQUARE_INSTALL_HANDED_OFF=1 sh `$f $Forward",
        "'--no-handoff'",
        "'--offline'",
        "$env:AISQUARE_INSTALL_NO_HANDOFF -eq '1'",
    ):
        assert check in block, f"install.ps1's hand-off lacks {check!r}"
    assert "--connect-timeout 5 --max-time 30" in SCRIPT.read_text(encoding="utf-8")


def test_no_double_quote_reaches_bash(ps1: str) -> None:
    """Windows PowerShell 5.1 does not escape a `"` inside a native argument.

    So the bash command is written with single quotes only; an escaped double
    quote in one of its strings would arrive at bash split or mangled.
    """
    block = _ps1_handoff(ps1)
    built = block[block.index("$Fallback = ") :]
    assert '`"' not in built and '""' not in built, (
        "a double quote is written into the bash command"
    )


# ---------------------------------------------------------------------------
# The docs
# ---------------------------------------------------------------------------


def _page(name: str) -> str:
    return " ".join((REPO / name).read_text(encoding="utf-8").split())


def test_the_install_page_says_main_runs_the_releases_installer() -> None:
    page = _page("docs/install.md")
    assert "The copy on `main` runs that release's own installer for you." in page
    assert "`--no-handoff`" in page
    assert "falls back to main's `install.sh`" in page, "the Windows path is not described"


def test_the_runbook_says_main_hands_off_and_falls_back() -> None:
    page = _page("docs/runbooks/release.md")
    assert "hand off to the latest release's own installer once it is attached" in page
    assert "the main one-liner falls back to main's own script" in page
