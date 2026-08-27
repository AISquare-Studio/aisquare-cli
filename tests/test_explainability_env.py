"""``explainability env`` exports must survive ``eval`` in ANY POSIX shell.

These tests run the emitted line through a real shell instead of asserting on
its text, because the text is exactly what stayed convincing while the command
was broken: the previous emitter produced ``$'…'``, which reads correctly to a
human and to bash, and is silently wrong everywhere else.

Measured under ``/bin/sh`` (dash) before the fix::

    BASE=[$http://127.0.0.1:9190]
    HDR=[$X-Agent-Name: aisquare-runner\\nX-Pipeline-Id: dashtest]
    -> claude: "API Error: Invalid URL", exit 1, nothing reached the proxy

which matters far beyond an interactive dash user: ``/bin/sh`` is what every
Makefile recipe, systemd unit, cron line, CI step and ``subprocess(shell=True)``
runs. Tracing that breaks a launch is the one outcome the fail-open doctrine
rules out, so this is pinned by execution, not by string comparison.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from aisquare.core.config import (
    AppConfig,
    ExplainabilitySettings,
    ExplainabilityTarget,
    save_config,
)


#: Every POSIX shell on this machine, not just the developer's login shell.
def _is_a_working_posix_shell(candidate: str) -> bool:
    r"""Whether ``candidate`` really evaluates a POSIX script.

    `shutil.which` is not enough, and the counter-example is on the CI runner:
    `C:\Windows\System32\bash.exe` is the WSL LAUNCHER, not a shell. It is on
    PATH on every Windows box, `which` finds it, and on a runner with no distro
    installed it exits 1 having evaluated nothing — so every parametrised case
    for "bash" failed with a CalledProcessError that had nothing to do with the
    quoting under test. Git Bash's `bash.exe` sits on the same PATH and IS a
    real shell, so only running one can tell them apart.
    """
    try:
        done = subprocess.run(
            [candidate, "-c", 'printf "%s" ok'],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and done.stdout.strip() == "ok"


SHELLS = [
    path
    for path in ("/bin/sh", "bash", "dash", "ash", "zsh")
    if shutil.which(path) and _is_a_working_posix_shell(path)
]

#: For the cases that need A shell rather than EVERY shell. None when this
#: machine has no POSIX shell at all, which is a skip and not a failure — the
#: property under test is about quoting, not about what is installed.
ANY_SHELL = SHELLS[0] if SHELLS else None


class _HealthHandler(BaseHTTPRequestHandler):
    payload: ClassVar[dict[str, str]] = {
        "status": "ok",
        "service": "aisquare-proxy",
        "mode": "claude_code",
    }

    def do_GET(self) -> None:
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


def _proxy() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _eval(shell: str, script_text: str) -> tuple[str, str]:
    """Eval ``script_text`` in ``shell`` and read the two vars back.

    The exports arrive as ``$1`` rather than interpolated into the script, so
    the test's own quoting cannot mask the quoting under test.
    """
    reader = (
        'eval "$1"; printf "%s\\n" "$ANTHROPIC_BASE_URL"; printf "%s" "$ANTHROPIC_CUSTOM_HEADERS"'
    )
    completed = subprocess.run(
        [shell, "-c", reader, "sh", script_text],
        capture_output=True,
        text=True,
        check=True,
    )
    base, _, headers = completed.stdout.partition("\n")
    return base, headers


@pytest.mark.parametrize("shell", SHELLS)
def test_the_exports_evaluate_the_same_in_every_posix_shell(
    shell: str, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    from aisquare.cli.app import app as cli_app

    server, url = _proxy()
    try:
        save_config(AppConfig(explainability=ExplainabilitySettings(enabled=True, proxy_url=url)))
        result = runner.invoke(
            cli_app, ["explainability", "env", "coder", "--session-id", "sess-9"]
        )
        assert result.exit_code == 0, result.output
        base, headers = _eval(shell, result.output)
    finally:
        server.shutdown()

    assert base == url, f"{shell} corrupted the base URL"
    # A REAL newline, not a literal backslash-n: the proxy silently ignores
    # X-Agent-Name without X-Pipeline-Id and files the run under its default
    # identity, so a glued header is a misattributed run rather than a loud one.
    assert headers == "X-Agent-Name: aisquare-coder\nX-Pipeline-Id: sess-9"


def _eval_one(shell: str, script_text: str) -> str:
    """Eval ``script_text`` in ``shell`` and print ``$X`` back byte for byte."""
    completed = subprocess.run(
        [shell, "-c", 'eval "$1"; printf "%s" "$X"', "sh", script_text],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:9190",
        "two words",
        "it's quoted",
        "line\nbreak",
        "$(echo substituted)",
        "`echo substituted`",
        "back\\slash",
        "semi; colon && amp",
        "X-Agent-Name: a\nX-Pipeline-Id: b",
    ],
)
def test_a_quoted_value_survives_a_real_shell_verbatim(shell: str, value: str) -> None:
    """Literal in, literal out — which also means an UNevaluated ``$(…)``.

    An identity or session id is not always ours to choose, so the emitted line
    must be data in every shell, never a command."""
    # ``shlex.quote`` is the emitter the command uses; this pins that the
    # stdlib form survives every shell, byte for byte.
    assert _eval_one(shell, f"export X={shlex.quote(value)}") == value


def test_env_honours_the_target_it_is_given(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-target proxy has to reach the wiring, or ``enable --proxy-url``
    writes config that every launch then ignores — config that looks applied
    and is not is worse than config that is missing."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    from aisquare.cli.app import app as cli_app

    server, url = _proxy()
    try:
        save_config(
            AppConfig(
                explainability=ExplainabilitySettings(
                    enabled=True,
                    proxy_url="http://127.0.0.1:9",  # the top-level default is dead
                    targets={
                        "prod": ExplainabilityTarget(
                            proxy_url=url, agent_name_template="acme-{role}"
                        )
                    },
                )
            )
        )
        result = runner.invoke(
            cli_app, ["explainability", "env", "coder", "--target", "prod", "--session-id", "s1"]
        )
    finally:
        server.shutdown()

    assert result.exit_code == 0, result.output
    assert url in result.output
    assert "acme-coder" in result.output


def _name_lines(script_text: str) -> set[str]:
    """The variable names a shell would bind, read off the ``export`` lines.

    Only the NAME half is parsed. The values are quoted, can contain newlines,
    and my first attempt at splitting them with ``shlex`` raised on the header
    pair — which is the point of this whole file: the value side is the part
    that must be handed to a real shell rather than re-implemented here.
    """
    return {
        match.group(1)
        for match in (
            re.match(r"export ([A-Z_][A-Z0-9_]*)=", line) for line in script_text.splitlines()
        )
        if match
    }


def _shell_value(name: str, script_text: str) -> str:
    """Eval ``script_text`` in a POSIX shell and print ``$name`` byte for byte.

    Through ``ANY_SHELL`` rather than a hardcoded ``/bin/sh``: that path does
    not exist on Windows, where the shell is Git Bash under Program Files, and
    the resulting FileNotFoundError read as a failure of the exports.
    """
    assert ANY_SHELL is not None, "guarded by the skipif on the caller"
    assert re.fullmatch(r"[A-Z_][A-Z0-9_]*", name), name
    completed = subprocess.run(
        [ANY_SHELL, "-c", f'eval "$1"; printf "%s" "${name}"', "sh", script_text],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


@pytest.mark.skipif(ANY_SHELL is None, reason="no POSIX shell on PATH")
def test_the_json_form_carries_the_same_exports(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json`` was honoured on the branch that FAILS and ignored on the one
    that WORKS.

    The refusal goes through the shared ``fail`` helper and emits
    ``{"error": "untraced"}``, so with no proxy reachable this command looked
    like a good citizen of the machine-readable contract. Its success path
    printed ``export …`` lines. A script piping it into jq therefore passed
    every test an operator could run before §3 and broke on the day the proxy
    came up — the failure arrives exactly when the system starts working.

    Asserted as AGREEMENT between the two renderings rather than against a
    literal payload, so it cannot rot into a snapshot of today's variables. The
    values come back through a real ``/bin/sh``, for this file's own reason: the
    quoted form is what must survive an ``eval``, and re-implementing that
    parse in the test is how a broken emitter stays convincing.
    """
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    from aisquare.cli.app import app as cli_app

    server, url = _proxy()
    try:
        save_config(AppConfig(explainability=ExplainabilitySettings(enabled=True, proxy_url=url)))
        human = runner.invoke(cli_app, ["explainability", "env", "coder", "--session-id", "s1"])
        machine = runner.invoke(
            cli_app, ["--json", "explainability", "env", "coder", "--session-id", "s1"]
        )
    finally:
        server.shutdown()

    assert human.exit_code == 0, human.output
    assert machine.exit_code == 0, machine.output

    payload = json.loads(machine.stdout)
    assert payload["role"] == "coder"
    assert set(payload["env"]) == _name_lines(human.stdout), (
        "the two renderings export different variable NAMES: "
        f"json={sorted(payload['env'])} shell={sorted(_name_lines(human.stdout))}"
    )
    for name, value in payload["env"].items():
        assert _shell_value(name, human.stdout) == value, (
            f"{name} differs between the renderings — a caller reading the JSON "
            "would set a different value than `eval` does"
        )
    # The join key by the name §5 already uses, rather than a second spelling
    # of the same value lifted to the top level.
    assert payload["env"]["AISQUARE_PIPELINE_ID"] == "s1", payload
