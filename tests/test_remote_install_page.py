"""``asq remote install-page``, and what a machine with no page installed is told.

``RemoteController.turn_on()`` passes ``dist_dir=None``, the server falls back to
``remote_dist_dir()`` (``~/.aisquare/remote-dist``) — and nothing populated it,
so pressing ``R`` on a fresh machine used to start a server that answered every
page request with a 404 nobody was looking at. These tests pin the two halves of
the fix: the command that installs a built dist, and the ONE sentence
(:data:`remote_server.NO_PAGE_HINT`) that ``start_remote_server()``, ``run_foreground()`` and
``asq remote serve`` all report when it has not been run.

Self-contained on purpose: ``tests/test_remote_server.py`` owns the server's
gates and its own fixtures, and this file must not inherit them.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.paths import remote_audit_path, remote_dist_dir, remote_state_path
from aisquare.services import remote_server
from aisquare.services.remote_server import NO_PAGE_HINT, NoRemotePage, Runtime, build_app
from tests.remote_kit_helpers import make_client


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A built page as the FE repo's ``dist/`` looks: an index and one asset."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>asq remote</title>")
    (root / "assets" / "app.js").write_text("console.log('remote')")
    return root


@pytest.fixture(autouse=True)
def fresh_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module's process-wide runtime/server are per-test here, as the server's own tests do."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    monkeypatch.setattr(remote_server, "_server", None)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_of(runner: CliRunner, *args: str) -> dict[str, object]:
    result = runner.invoke(cli, ["--json", *args])
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert isinstance(payload, dict)
    return payload


# --- installing ---------------------------------------------------------------------------


def test_install_page_copies_the_dist_into_the_directory_the_server_serves(
    isolated_home: Path, built: Path
) -> None:
    destination = remote_server.install_page(built)

    assert destination == remote_dist_dir()
    assert (destination / "index.html").read_text().startswith("<!doctype html>")
    assert (destination / "assets" / "app.js").read_text() == "console.log('remote')"
    assert remote_server._page_missing(None) is None  # the default dir now has a page


def test_install_page_replaces_an_older_page_and_leaves_no_staging_directory(
    isolated_home: Path, built: Path, tmp_path: Path
) -> None:
    remote_server.install_page(built)
    newer = tmp_path / "newer"
    newer.mkdir()
    (newer / "index.html").write_text("<!doctype html><title>build 2</title>")

    destination = remote_server.install_page(newer)

    assert (destination / "index.html").read_text().endswith("build 2</title>")
    assert not (destination / "assets").exists()  # replaced wholesale, not merged into
    leftovers = [p.name for p in destination.parent.iterdir() if ".remote-dist." in p.name]
    assert leftovers == []  # the staging and previous directories are cleaned up


def test_install_page_without_an_index_html_refuses_and_installs_nothing(
    isolated_home: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "not-a-build"
    empty.mkdir()
    (empty / "app.js").write_text("console.log('no index')")

    with pytest.raises(NoRemotePage, match=r"no index\.html in"):
        remote_server.install_page(empty)

    assert not remote_dist_dir().exists()


def test_the_cli_installs_a_page_and_reports_where_it_landed(
    isolated_home: Path, built: Path
) -> None:
    runner = CliRunner()

    payload = _json_of(runner, "remote", "install-page", str(built))

    assert payload == {"installed": str(remote_dist_dir())}
    assert (remote_dist_dir() / "index.html").is_file()
    human = runner.invoke(cli, ["remote", "install-page", str(built)])
    assert human.exit_code == 0 and "installed the remote page" in human.stdout


def test_the_cli_refuses_a_directory_that_is_not_a_build(
    isolated_home: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "not-a-build"
    empty.mkdir()
    runner = CliRunner()

    result = runner.invoke(cli, ["--json", "remote", "install-page", str(empty)])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "invalid_dist"
    assert payload["ref"] == str(empty)
    # ``fail`` puts the sentence on stderr and the code in the JSON (cli/common.py).
    human = runner.invoke(cli, ["remote", "install-page", str(empty)])
    assert human.exit_code == 1
    assert "no index.html in" in human.stderr and "npm run build" in human.stderr
    assert not remote_dist_dir().exists()


# --- what a machine with no page installed is told -----------------------------------------


def test_start_with_no_page_installed_raises_the_actionable_sentence(isolated_home: Path) -> None:
    with pytest.raises(NoRemotePage) as raised:
        remote_server.start_remote_server(port=free_port())

    assert str(raised.value) == NO_PAGE_HINT
    assert "aisquare remote install-page" in str(raised.value)
    assert remote_server.remote_server_status()["running"] is False  # nothing was left listening


def test_run_foreground_with_no_page_installed_raises_before_it_binds(
    isolated_home: Path,
) -> None:
    port = free_port()

    with pytest.raises(NoRemotePage, match="install-page"):
        remote_server.run_foreground(port=port)

    with socket.socket() as probe:  # the port is still free: it never got that far
        probe.bind(("127.0.0.1", port))


def test_cli_serve_exits_non_zero_with_the_same_sentence(isolated_home: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--json", "remote", "serve"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "no_remote_page"
    human = runner.invoke(cli, ["remote", "serve"])
    assert human.exit_code == 1
    assert human.stderr.strip() == f"✗ {NO_PAGE_HINT}"
    # It never printed the banner: no link or password for a server that cannot serve.
    assert "Remote Control on" not in human.stderr


def test_an_explicit_dist_is_still_the_callers_business(
    isolated_home: Path, tmp_path: Path
) -> None:
    """``--dist`` names a path on purpose — maybe still building — so it stays a per-request 404.

    The up-front refusal is for the DEFAULT directory only; ``build_app`` keeps
    the ``no_dist`` behaviour ``tests/test_remote_server.py`` pins.
    """
    assert remote_server._page_missing(tmp_path / "still-building") == NO_PAGE_HINT
    runtime = Runtime(remote_state_path(), remote_audit_path())
    response = make_client(build_app(runtime, dist_dir=tmp_path / "still-building")).get(
        f"/r/{runtime.token}/"
    )
    assert response.status_code == 404 and response.json()["error"] == "no_dist"


def test_after_install_page_the_server_serves_the_spa_with_no_dist_flag(
    isolated_home: Path, built: Path
) -> None:
    remote_server.install_page(built)
    runtime = Runtime(remote_state_path(), remote_audit_path())
    client = make_client(build_app(runtime))  # dist_dir=None: the installed page

    index = client.get(f"/r/{runtime.token}/")
    deep = client.get(f"/r/{runtime.token}/fleet/coder-1")  # SPA fallback
    asset = client.get(f"/r/{runtime.token}/assets/app.js")

    assert index.status_code == 200 and index.text.startswith("<!doctype html>")
    assert deep.status_code == 200 and deep.text.startswith("<!doctype html>")
    assert asset.status_code == 200 and asset.text == "console.log('remote')"
