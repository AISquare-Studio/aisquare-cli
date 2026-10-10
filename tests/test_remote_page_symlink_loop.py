"""A page directory that is a symbolic link loop is refused by name, on every Python CI runs.

``Path.resolve`` raises ``RuntimeError`` ("Symlink loop from ...") for a loop on Python
3.11 and 3.12 and returns the path on 3.13. ``install-page``'s source, ``serve --dist``
and the installed build were resolved with nothing to catch it, so each ended in a
traceback with no ``--json`` answer (review of #243, round 7). On 3.13 these tests make
``resolve`` raise for the loop as 3.12's does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.paths import remote_audit_path, remote_dist_dir, remote_state_path
from aisquare.services import remote_server
from aisquare.services.remote_server import NoRemotePage, Runtime, build_app
from tests.remote_kit_helpers import make_client


@pytest.fixture(autouse=True)
def fresh_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_server, "_runtime", None)
    monkeypatch.setattr(remote_server, "_server", None)


def _loop(at: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``at`` made a link to its sibling, which links back: ``a -> b -> a``. On 3.13
    ``resolve`` raises for a path through it, as 3.11's and 3.12's do."""
    other = at.with_name(at.name + "-other")
    try:
        at.symlink_to(other)
        other.symlink_to(at)
    except OSError:
        pytest.skip("this platform cannot make the symlink")
    if sys.version_info >= (3, 13):
        resolve = Path.resolve
        looped = (str(at), str(other))

        def resolve_as_3_12(self: Path, strict: bool = False) -> Path:
            if str(self).startswith(looped):
                raise RuntimeError(f"Symlink loop from {str(self)!r}")
            return resolve(self, strict)

        monkeypatch.setattr(Path, "resolve", resolve_as_3_12)
    return at


def test_install_page_of_a_symlink_loop_is_invalid_dist_not_a_traceback(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _loop(tmp_path / "dist", monkeypatch)
    runner = CliRunner()

    scripted = runner.invoke(cli, ["--json", "remote", "install-page", str(loop)])
    human = runner.invoke(cli, ["remote", "install-page", str(loop)])

    assert scripted.exit_code == 1, scripted.output
    assert isinstance(scripted.exception, SystemExit), "a traceback"
    assert json.loads(scripted.stdout)["error"] == "invalid_dist"
    assert human.exit_code == 1 and human.stderr.startswith(f"✗ {loop} does not resolve")
    with pytest.raises(NoRemotePage, match="does not resolve"):
        remote_server.install_page(loop)
    assert not remote_dist_dir().exists(), "nothing was installed"


def test_serve_with_a_dist_that_is_a_symlink_loop_is_no_remote_page_not_a_traceback(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _loop(tmp_path / "dist", monkeypatch)
    runner = CliRunner()

    scripted = runner.invoke(cli, ["--json", "remote", "serve", "--dist", str(loop)])
    human = runner.invoke(cli, ["remote", "serve", "--dist", str(loop)])

    assert scripted.exit_code == 1, scripted.output
    assert isinstance(scripted.exception, SystemExit), "a traceback"
    assert json.loads(scripted.stdout)["error"] == "no_remote_page"
    assert human.exit_code == 1 and human.stderr.startswith(f"✗ {loop} does not resolve")
    assert "Remote Control on" not in human.stderr, "no banner for a server that cannot serve"


def test_an_installed_page_that_became_a_symlink_loop_is_served_as_none_installed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The R panel's start and ``serve`` without ``--dist`` check the installed build, and the
    app resolves it: a loop there is a build with no index, so the bundled page is served,
    as it is for an installed directory that is gone."""
    remote_dist_dir().parent.mkdir(parents=True, exist_ok=True)
    _loop(remote_dist_dir(), monkeypatch)

    assert remote_server._page_missing(None) is None, "the bundled page is there"
    runtime = Runtime(remote_state_path(), remote_audit_path())
    page = make_client(build_app(runtime)).get(f"/r/{runtime.token}/")
    assert page.status_code == 200 and page.text.lstrip().lower().startswith("<!doctype html>")
