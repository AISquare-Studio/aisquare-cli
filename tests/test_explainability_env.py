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
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from aisquare.cli.explainability import _posix_quoted
from aisquare.core.config import (
    AppConfig,
    ExplainabilitySettings,
    ExplainabilityTarget,
    save_config,
)

#: Every POSIX shell on this machine, not just the developer's login shell.
SHELLS = [path for path in ("/bin/sh", "bash", "dash", "ash", "zsh") if shutil.which(path)]


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
    assert _eval_one(shell, f"export X={_posix_quoted(value)}") == value


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
