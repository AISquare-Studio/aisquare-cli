"""Four steps of ``ci.yml`` that guard something, held to doing it.

Each was edited out, or turned into a no-op, with every test still green (review of
#427, board seq 2775). ``ci-ok`` is the one gate, so a guard in a step is only as good as
the pin on the step.

* ``test``'s "Configure an aisquare home" asserts the HOME axis's premise: that ``enable``
  left a home the suite reads as configured. A ``true ||`` before that check, or an exit
  status swallowed after it, lets a home that did not take through, and ``proxy-up``
  goes vacuously green.
* ``test``'s ambient suite step starts a proxy stub in the background. The EXIT trap that
  kills it is what lets the step end: a runner waits for every process holding the step's
  output, so a stub left running hangs the step until the job's timeout.
* ``package``'s twine step fails the build on metadata PyPI would render badly, and a
  release's project page cannot be edited after the upload.
* ``package``'s other steps, the smoke test of the extra among them, run on BOTH legs: the
  collision of the SDK's import name with ours is a fact about one directory on disk, and
  Windows is where overwriting a file another process holds open answers differently.

The two shell steps are RUN, as the runner runs them (``bash -eo pipefail``), against a
stand-in ``aisquare``, ``python`` and ``pytest`` on PATH: a text pin passes ``true ||``,
``trap '' EXIT`` and a ``kill`` of the wrong variable, and what these steps do is the point.
The package steps are pinned as the YAML says them, as the lint steps are
(``tests/test_ci_is_lean_for_sub_prs.py``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.ci_matrix import ROOT, choose, workflow

_RUNS_BASH = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="runs a step's own bash script, as it runs on ubuntu-latest",
)

_EXPRESSION = re.compile(r"\$\{\{\s*([A-Za-z_.]+)\s*\}\}")

#: ``test``'s ambient steps are the ones gated on a leg whose ``ambient`` is not ``none``.
AMBIENT_GATE = "matrix.ambient != 'none'"

CONFIGURE = "Configure an aisquare home, the way a developer's machine is"

#: The ``package`` step that runs on one leg only, and the gate that says which.
TWINE = "Check the dists' PyPI metadata (twine)"
TWINE_GATE = "matrix.os == 'ubuntu-latest'"
TWINE_RUN = ["python -m pip install twine", "python -m twine check --strict dist/*"]


# ---- the steps, as the YAML has them


def _steps(job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = workflow()["jobs"][job]["steps"]
    return steps


def _named(job: str, name: str) -> dict[str, Any]:
    found = [step for step in _steps(job) if step.get("name") == name]
    assert len(found) == 1, f"{job} has {len(found)} steps named {name!r}, wanted one"
    return found[0]


def _suite_step() -> dict[str, Any]:
    """The ambient step that runs the suite: gated on an ambient leg, and runs pytest."""
    found = [
        step
        for step in _steps("test")
        if step.get("if") == AMBIENT_GATE and "pytest " in str(step.get("run", ""))
    ]
    assert len(found) == 1, f"wanted one ambient step that runs pytest, got {len(found)}"
    return found[0]


def _script(step: dict[str, Any], **context: str) -> str:
    """``step``'s ``run`` with its ``${{ }}`` expressions filled in, as the runner fills them."""

    def value(found: re.Match[str]) -> str:
        assert found[1] in context, f"{step.get('name')}: the test has no value for {found[0]}"
        return context[found[1]]

    return _EXPRESSION.sub(value, str(step["run"]))


# ---- running a shell step as the runner does, against stand-ins for what it calls

#: ``aisquare`` as the ambient steps use it: ``init`` and ``enable`` succeed, and ``status``
#: exits 0 only while something answers on the configured proxy's port, printing what
#: ``FAKE_STATUS`` says under ``--json`` (the real one exits non-zero when the proxy is red).
_FAKE_AISQUARE = """\
import os
import socket
import sys

args = sys.argv[1:]
port = int(os.environ["FAKE_PROXY_PORT"])


def answering() -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except OSError:
        return False
    return True


if args[:1] == ["init"] or args[:2] == ["explainability", "enable"]:
    sys.exit(0)
if args[:2] == ["explainability", "status"]:
    if "--json" in args:
        sys.stdout.write(os.environ["FAKE_STATUS"])
    sys.exit(0 if answering() else 1)
sys.exit(f"the stand-in aisquare was not told to expect {args}")
"""

#: ``pytest`` for the suite step: notes whether the proxy was answering while it ran.
_FAKE_PYTEST = """\
#!/bin/sh
if aisquare explainability status >/dev/null 2>&1; then
  echo up > "$FAKE_PYTEST_RAN"
else
  echo down > "$FAKE_PYTEST_RAN"
fi
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _gone(pid: int, *, within: float = 15.0) -> bool:
    """Whether ``pid`` has died, polled: a SIGTERM is delivered a moment after it is sent."""
    deadline = time.monotonic() + within
    while _alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


class Ambient:
    """The stand-ins on PATH, and the way to run one of ``test``'s steps against them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = _free_port()
        self.pidfile = root / "stub.pid"
        self.pytest_ran = root / "pytest.ran"
        self.bin = root / "bin"
        self.bin.mkdir()
        script = self.bin / "aisquare.py"
        script.write_text(_FAKE_AISQUARE, encoding="utf-8")
        self._tool("aisquare", f'exec "{sys.executable}" "{script}" "$@"')
        # Records the stub's pid: `exec` keeps it, so it is the pid the step's `$!` holds.
        self._tool(
            "python",
            'if [ "$1" = "-m" ] && [ "$2" = "tests.proxy_stub" ]; then\n'
            '  echo $$ > "$STUB_PIDFILE"\n'
            "fi\n"
            f'exec "{sys.executable}" "$@"',
        )
        (self.bin / "pytest").write_text(_FAKE_PYTEST, encoding="utf-8")
        (self.bin / "pytest").chmod(0o755)

    def _tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def status(self, **fields: Any) -> str:
        """What ``aisquare explainability status --json`` prints: a home that took, by default."""
        found = {
            "enabled": True,
            "key_source": "file",
            "proxy": f"http://127.0.0.1:{self.port}",
            **fields,
        }
        return json.dumps(found)

    def run(self, step: dict[str, Any], *, ambient: str, status: str) -> tuple[int, str]:
        """``step``'s script under ``bash -eo pipefail`` and its output.

        The output goes to a FILE, not a pipe: a process the step leaves running holds a
        pipe open, and reading it would then wait on that process rather than on the step.
        """
        temp = self.root / "runner-temp"
        temp.mkdir(exist_ok=True)
        script = _script(
            step,
            **{
                "matrix.ambient": ambient,
                "matrix.shard": "1",
                "matrix.shards": "4",
                "matrix.leg": "leg",
                "runner.temp": str(temp),
            },
        )
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "AMBIENT": ambient,
            "RUNNER_TEMP": str(temp),
            "AISQUARE_HOME": str(temp / "aisquare-home"),
            "CLAUDE_CONFIG_DIR": str(temp / "claude-config"),
            "CLAUDE_CODE_TMPDIR": str(temp / "claude-tmp"),
            "FAKE_PROXY_PORT": str(self.port),
            "FAKE_STATUS": status,
            "FAKE_PYTEST_RAN": str(self.pytest_ran),
            "STUB_PIDFILE": str(self.pidfile),
        }
        log = self.root / "step.log"
        with log.open("wb") as out:
            done = subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=120,
                check=False,
            )
        return done.returncode, log.read_text(encoding="utf-8", errors="replace")

    def stub_pid(self) -> int | None:
        if not self.pidfile.exists():
            return None
        text = self.pidfile.read_text(encoding="utf-8").strip()
        return int(text) if text else None

    def reap(self) -> None:
        """Whatever a broken step left running, so a red test leaves nothing behind."""
        pid = self.stub_pid()
        if pid is not None and _alive(pid):
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


@pytest.fixture
def ambient(tmp_path: Path) -> Iterator[Ambient]:
    held = Ambient(tmp_path)
    try:
        yield held
    finally:
        held.reap()


# ---- the home axis's premise (a)


@_RUNS_BASH
def test_a_home_that_took_passes_the_configure_step(ambient: Ambient) -> None:
    """The step passes for the state it exists to assert, so the cases below fail for the
    check and not because the stand-ins are wrong."""
    code, out = ambient.run(
        _named("test", CONFIGURE), ambient="proxy-down", status=ambient.status()
    )
    assert code == 0, out
    assert (ambient.root / "runner-temp" / "aisquare-home" / "explainability-key").exists(), out


@_RUNS_BASH
@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"enabled": False}, "tracing not enabled"),
        ({"key_source": "env"}, "key not from the file"),
        ({"key_source": None}, "no key"),
        ({"enabled": False, "key_source": None}, "nothing configured"),
    ],
    ids=["not-enabled", "key-from-env", "no-key", "nothing"],
)
def test_the_configure_step_fails_for_a_home_that_did_not_take(
    ambient: Ambient, fields: dict[str, Any], why: str
) -> None:
    """``enable`` can succeed and leave a home the suite reads as unconfigured (``status``
    then exits 0 whatever the proxy does, so ``proxy-up`` is vacuously green): the step
    must FAIL for it, with the reason named. ``true ||`` before the check, or an exit status
    swallowed after it, passes every one of these."""
    code, out = ambient.run(
        _named("test", CONFIGURE), ambient="proxy-up", status=ambient.status(**fields)
    )
    assert code != 0, f"the step passed a home with {why}: {out}"
    assert "ambient home is not what this job requires" in out, out


@_RUNS_BASH
@pytest.mark.parametrize("status", ["", "not json", "{"], ids=["empty", "text", "truncated"])
def test_the_configure_step_fails_when_status_says_nothing_readable(
    ambient: Ambient, status: str
) -> None:
    """A ``status`` that crashed or printed prose is no evidence the home took."""
    code, out = ambient.run(_named("test", CONFIGURE), ambient="proxy-down", status=status)
    assert code != 0, out


# ---- the stub's kill trap (c)


@_RUNS_BASH
def test_the_proxy_stub_is_up_for_the_suite_and_gone_when_the_step_ends(ambient: Ambient) -> None:
    """The trap is why the step can end: a runner waits for every process holding the step's
    output, so a stub left running hangs it to the job's timeout (and the next shard on the
    same runner finds the port taken). Held for proxy-up, the one variant that starts it."""
    step = _suite_step()
    code, out = ambient.run(step, ambient="proxy-up", status=ambient.status())
    assert code == 0, out
    assert ambient.pytest_ran.read_text(encoding="utf-8").strip() == "up", (
        "the suite ran without the stub answering: proxy-up would test proxy-down"
    )
    pid = ambient.stub_pid()
    assert pid is not None, f"the stub was never started: {out}"
    assert _gone(pid), (
        f"the proxy stub (pid {pid}) outlived its step: nothing kills it when the step ends, "
        "so the runner waits on it until the job times out"
    )


@_RUNS_BASH
def test_proxy_down_starts_no_stub(ambient: Ambient) -> None:
    """The other side of the same step: only proxy-up runs a listener, so the trap is the
    stub's and not a kill of something else; with nothing listening, the suite sees it so."""
    code, out = ambient.run(_suite_step(), ambient="proxy-down", status=ambient.status())
    assert code == 0, out
    assert ambient.stub_pid() is None, out
    assert ambient.pytest_ran.read_text(encoding="utf-8").strip() == "down"


# ---- the package job: twine (b), and every other step on both legs (d)


def _commands(step: dict[str, Any]) -> list[str]:
    return [line.strip() for line in str(step.get("run", "")).splitlines() if line.strip()]


def _package_os(*, lean: bool) -> list[str]:
    """The ``os`` of each leg of ``package`` for a sub-PR (``lean``) or any other run."""
    matrix = workflow()["jobs"]["package"]["strategy"]["matrix"]
    legs: list[str] = choose(matrix["os"], lean=lean)
    return legs


def test_the_dists_metadata_is_checked_strictly_after_the_build() -> None:
    """``--strict`` fails on a missing long description or content type, and a release's
    PyPI page cannot be edited after the upload. The whole script, not a part of it:
    ``twine check dist/*`` without ``--strict`` and ``... || true`` keep the step's name."""
    steps = _steps("package")
    twine = _named("package", TWINE)
    assert _commands(twine) == TWINE_RUN
    assert "shell" not in twine, "a step's shell may drop `-e`, so a red twine is not fatal"
    names = [step.get("name") for step in steps]
    assert names.index("Build sdist and wheel") < names.index(TWINE), (
        "the dists are checked before they are built"
    )
    assert [step.get("name") for step in steps if "twine" in str(step.get("run", ""))] == [TWINE]


def test_the_twine_step_runs_on_ubuntu_only_and_always_there() -> None:
    """publish.yml builds and uploads on Ubuntu, and the metadata does not depend on the
    OS. Gated on any other condition, or on none, it moves to the Windows leg or stops
    running on the one that matters."""
    assert _named("package", TWINE).get("if") == TWINE_GATE
    for lean in (True, False):
        legs = _package_os(lean=lean)
        assert "ubuntu-latest" in legs, (lean, legs)


def test_every_package_step_but_twine_runs_on_both_legs() -> None:
    """``if: matrix.os == 'ubuntu-latest'`` on "Smoke-test the extra" kept the step and ran
    it on Linux only, and the shared ``__init__.py`` collision it exists for is a fact about
    one directory on disk that Windows answers differently. A gate on any step here is
    a decision to make deliberately, so the one that exists (twine's) is named above."""
    gated = {
        str(step.get("name") or step.get("uses")): step["if"]
        for step in _steps("package")
        if "if" in step and step.get("name") != TWINE
    }
    assert gated == {}, f"these package steps skip a leg: {gated}"
    assert "if" not in workflow()["jobs"]["package"], "the package job skips a leg whole"
    names = {step.get("name") for step in _steps("package")}
    assert names >= {
        "Build sdist and wheel",
        "Install the wheel and smoke-test the console scripts",
        "Install the wheel WITH the explainability extra",
        "Smoke-test the extra",
    }, names


def test_the_package_job_has_a_leg_for_each_os_the_extra_must_survive() -> None:
    """What "both legs" is read against: the full matrix runs Linux and Windows."""
    assert _package_os(lean=False) == ["ubuntu-latest", "windows-latest"]
    assert _package_os(lean=True) == ["ubuntu-latest"]
