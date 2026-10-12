"""``asq remote install-page``, and what a machine with no page installed gets.

``RemoteController.turn_on()`` passes ``dist_dir=None``, the server falls back to
``remote_dist_dir()`` (``~/.aisquare/remote-dist``) — and nothing populated it,
so pressing ``R`` on a fresh machine used to start a server that answered every
page request with a 404 nobody was looking at. aisquare-cli now carries its own
page (``services/remote_page.py``), served whenever none is installed, so a fresh
machine just works. These tests pin what is left of the problem: the command
that installs a build over the bundled page, and the ONE sentence
(:data:`remote_server.NO_PAGE_HINT`) that ``start_remote_server()``,
``run_foreground()`` and ``asq remote serve`` all report for an install that
lost its bundled page.

Self-contained on purpose: ``tests/test_remote_server.py`` owns the server's
gates and its own fixtures, and this file must not inherit them.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import socket
import stat
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.paths import remote_audit_path, remote_dist_dir, remote_state_path
from aisquare.services import remote_page, remote_server
from aisquare.services.remote_server import NO_PAGE_HINT, NoRemotePage, Runtime, build_app
from tests.fsperms import can_deny_reads
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


@pytest.fixture
def no_bundled_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """An install that lost the page aisquare-cli carries: its files read as none at all."""
    monkeypatch.setattr(remote_page, "bundled_page_files", dict)


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
    isolated_home: Path, built: Path, no_bundled_page: None
) -> None:
    destination = remote_server.install_page(built)

    assert destination == remote_dist_dir()
    assert (destination / "index.html").read_text().startswith("<!doctype html>")
    assert (destination / "assets" / "app.js").read_text() == "console.log('remote')"
    # The installed page counts on its own: here there is no bundled one to fall back to.
    assert remote_server._page_missing(None) is None


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps no group or other bits")
def test_an_installed_page_is_writable_by_no_one_else_whatever_the_builds_modes(
    isolated_home: Path, built: Path
) -> None:
    """``copytree`` kept the build's modes and never applied the umask: a build that came
    with 0777 directories and 0666 files (a FAT drive, ``/mnt/c``, an archive) was installed
    world-writable, and another account could replace the page that takes the passphrase,
    served before it (sweep 5 of #243). The build's other bits are left as they were."""
    for path in (built, built / "assets"):
        path.chmod(0o777)
    for path in (built / "index.html", built / "assets" / "app.js"):
        path.chmod(0o666)

    destination = remote_server.install_page(built)

    modes = {
        str(path.relative_to(destination)): stat.S_IMODE(path.stat().st_mode)
        for path in (destination, *destination.rglob("*"))
    }
    assert modes == {".": 0o755, "assets": 0o755, "index.html": 0o644, "assets/app.js": 0o644}


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps no group or other bits")
def test_an_installed_page_is_copied_where_no_other_account_can_reach_it(
    isolated_home: Path, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrowed only once copied, the copy took the build's 0666 and 0777 as it was written
    beside the destination: another account could open a file for writing then and keep it
    open past the narrowing, or put its own file in a directory the walk then left as it
    was (sweep of #243, round 7). The copy is made inside a directory only this account may
    enter, and the swap leaves nothing of it behind."""
    for path in (built, built / "assets"):
        path.chmod(0o777)
    for path in (built / "index.html", built / "assets" / "app.js"):
        path.chmod(0o666)
    home = remote_dist_dir().parent
    reachable: list[str] = []
    real_copytree = shutil.copytree

    def copy_and_look(src: Path, dst: Path, *args: Any, **kwargs: Any) -> Any:
        copied = real_copytree(src, dst, *args, **kwargs)
        within = [Path(dst), *Path(dst).parents]
        between = within[: within.index(home)]
        if not any(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0 for path in between):
            reachable.append(str(Path(dst).relative_to(home)))
        return copied

    monkeypatch.setattr(shutil, "copytree", copy_and_look)

    destination = remote_server.install_page(built)

    assert reachable == [], "the copy, its build's modes still on it, was in reach"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755
    assert [p.name for p in home.iterdir() if p.name.startswith(".remote-dist.")] == []


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


@pytest.mark.parametrize("fails", ["the copy", "the swap"])
def test_a_failed_install_leaves_no_partial_copy_and_the_page_before_it_served(
    isolated_home: Path, built: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    """The staging directory is named for the process, so no later install's clean-up ever
    matched it: each failed ``install-page`` left a hidden partial copy in ``~/.aisquare``,
    holding the very space its refusal says to free (sweep 4 of #243)."""
    remote_server.install_page(built)
    newer = tmp_path / "newer"
    (newer / "assets").mkdir(parents=True)
    (newer / "index.html").write_text("<!doctype html><title>build 2</title>")
    (newer / "assets" / "big.js").write_text("x" * 4096)
    full = OSError(errno.ENOSPC, "No space left on device")
    if fails == "the copy":
        real_copytree = shutil.copytree

        def copy_then_fill_the_disk(src: Path, dst: Path, *args: Any, **kwargs: Any) -> Any:
            copied = real_copytree(src, dst, *args, **kwargs)
            if ".staging-" in Path(dst).name:  # the whole copy made, not one directory of it
                raise full
            return copied

        monkeypatch.setattr(shutil, "copytree", copy_then_fill_the_disk)
    else:
        real_rename = Path.rename

        def refuse_the_staging_rename(self: Path, target: Path) -> Path:
            if ".staging-" in self.name:
                raise full
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", refuse_the_staging_rename)
    with pytest.raises(OSError, match="No space left"):
        remote_server.install_page(newer)
    home = remote_dist_dir().parent
    assert [p.name for p in home.iterdir() if p.name.startswith(".remote-dist.")] == []
    assert (
        remote_dist_dir() / "index.html"
    ).read_text() == "<!doctype html><title>asq remote</title>"


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps no owner write bit on a directory")
@pytest.mark.parametrize("fails", [None, "the copy"], ids=["installs", "fails"])
def test_a_read_only_build_installs_and_leaves_no_copy_behind(
    isolated_home: Path,
    built: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fails: str | None,
) -> None:
    """A build whose directories are 0555 (a Nix store, a read-only mount, an archive that
    kept its modes) was copied with them into the private directory, and a directory moved
    to another parent needs write on itself for its ``..``: the swap raised
    ``PermissionError``, and ``rmtree`` could not empty the 0555 copy, so each try left one
    in ``~/.aisquare`` (review of #243, round 8, 1/6). An older install whose directories
    are 0555, as a read-only build's were before the copy moved parent, is removed too."""
    remote_server.install_page(built)
    older = remote_dist_dir()
    readonly = tmp_path / "readonly"
    shutil.copytree(built, readonly)
    (readonly / "index.html").write_text("<!doctype html><title>build 2</title>")
    for path in (older / "assets", older, readonly / "assets", readonly):
        path.chmod(0o555)
    if fails == "the copy":
        real_copytree = shutil.copytree

        def copy_then_fill_the_disk(src: Path, dst: Path, *args: Any, **kwargs: Any) -> Any:
            copied = real_copytree(src, dst, *args, **kwargs)
            if ".staging-" in Path(dst).name:
                raise OSError(errno.ENOSPC, "No space left on device")
            return copied

        monkeypatch.setattr(shutil, "copytree", copy_then_fill_the_disk)
    try:
        if fails:
            with pytest.raises(OSError, match="No space left"):
                remote_server.install_page(readonly)
        else:
            destination = remote_server.install_page(readonly)
            assert (destination / "index.html").read_text().endswith("build 2</title>")
            modes = [stat.S_IMODE(p.stat().st_mode) for p in (destination, destination / "assets")]
            assert modes == [0o755, 0o755], "the next install can empty it"
        home = remote_dist_dir().parent
        assert [p.name for p in home.iterdir() if p.name.startswith(".remote-dist.")] == []
    finally:
        for path in (readonly, readonly / "assets", older, older / "assets"):
            if path.exists():
                path.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps no owner write bit on a directory")
@pytest.mark.parametrize("leads", ["to a build", "nowhere"])
def test_install_page_over_a_linked_page_replaces_the_link_and_leaves_its_build_as_it_was(
    isolated_home: Path, built: Path, tmp_path: Path, leads: str
) -> None:
    """A ``remote-dist`` that is a link to a build of the human's own was moved aside as a
    link, and its removal walked through it: the build's read-only directories were given
    their owner's write, and the link was left in ``~/.aisquare`` for each install (review
    of #243, round 9, 4/6). One that leads nowhere is replaced too."""
    own = tmp_path / "own-build"
    shutil.copytree(built, own)
    (own / "index.html").write_text("<!doctype html><title>my build</title>")
    (own / "assets").chmod(0o555)
    remote_dist_dir().parent.mkdir(parents=True, exist_ok=True)
    remote_dist_dir().symlink_to(own if leads == "to a build" else tmp_path / "gone")
    try:
        destination = remote_server.install_page(built)

        assert not destination.is_symlink()
        assert (destination / "index.html").read_text() == (built / "index.html").read_text()
        assert stat.S_IMODE((own / "assets").stat().st_mode) == 0o555, "the build is its own"
        assert (own / "index.html").read_text().endswith("my build</title>")
        home = remote_dist_dir().parent
        assert [p.name for p in home.iterdir() if p.name.startswith(".remote-dist.")] == []
    finally:
        (own / "assets").chmod(0o755)


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


# --- a fresh machine, and an install that lost its page ------------------------------------


def test_a_fresh_machine_starts_and_serves_the_bundled_page(isolated_home: Path) -> None:
    """The first ``R`` press on a machine nobody set up: a real server, on loopback."""
    assert remote_server._page_missing(None) is None
    info = remote_server.start_remote_server(port=free_port())
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, never a proxy
    try:
        with direct.open(info.url_local, timeout=10) as response:
            body = response.read()
            headers = response.headers
        assert response.status == 200
        assert body == remote_page.bundled_page_files()["index.html"]
        assert headers["content-security-policy"] == remote_page.PAGE_CSP
    finally:
        remote_server.stop_remote_server()


def test_start_with_no_page_at_all_raises_the_actionable_sentence(
    isolated_home: Path, no_bundled_page: None
) -> None:
    with pytest.raises(NoRemotePage) as raised:
        remote_server.start_remote_server(port=free_port())

    assert str(raised.value) == NO_PAGE_HINT
    assert "reinstall aisquare-cli" in str(raised.value)
    assert remote_server.remote_server_status()["running"] is False  # nothing was left listening


def test_run_foreground_with_no_page_at_all_raises_before_it_binds(
    isolated_home: Path, no_bundled_page: None
) -> None:
    port = free_port()

    with pytest.raises(NoRemotePage, match="reinstall"):
        remote_server.run_foreground(port=port)

    with socket.socket() as probe:  # the port is still free: it never got that far
        probe.bind(("127.0.0.1", port))


def test_cli_serve_on_a_fresh_machine_serves_without_an_install_page(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[tuple[Path | None, int]] = []

    def run_foreground(
        dist: Path | None, port: int, *_more: object, ready: Callable[[], None] | None = None
    ) -> bool:
        """What ``serve`` asked to serve. The auto-off and public URL that lane b-security's
        ``serve`` passes too are not this test's business; its ``ready`` banner prints once
        the port is bound, as the real one does, and then the timer did not end it."""
        served.append((dist, port))
        if ready is not None:
            ready()
        return False

    monkeypatch.setattr(remote_server, "run_foreground", run_foreground)

    result = CliRunner().invoke(cli, ["--json", "remote", "serve", "--port", "9001"])

    assert result.exit_code == 0, result.output
    assert served == [(None, 9001)]


def test_cli_serve_exits_non_zero_with_the_same_sentence(
    isolated_home: Path, no_bundled_page: None
) -> None:
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

    Up front it is named in the refusal, never answered with the bundled page:
    whoever passed ``--dist`` meant that directory. ``build_app`` keeps the
    ``no_dist`` behaviour ``tests/test_remote_server.py`` pins.
    """
    missing = tmp_path / "still-building"
    assert remote_server._page_missing(missing) == f"no index.html in {missing.resolve()}"
    runtime = Runtime(remote_state_path(), remote_audit_path())
    response = make_client(build_app(runtime, dist_dir=missing)).get(f"/r/{runtime.token}/")
    assert response.status_code == 404 and response.json()["error"] == "no_dist"


def test_an_installed_index_that_goes_as_a_request_is_answered_is_the_bundled_page(
    isolated_home: Path, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index is looked at twice, and one an ``install-page`` swap took away in between
    was answered as a ``--dist`` without its index is: 404 ``no_dist`` naming the
    directory, ``~/.aisquare/remote-dist`` resolved, to anyone holding the link (sweep 4 of
    #243). It is answered as the next request will be, with the bundled page."""
    remote_server.install_page(built)
    index = remote_dist_dir().resolve() / "index.html"
    looks: list[Path] = []
    real_is_file = Path.is_file

    def gone_after_the_first_look(self: Path) -> bool:
        if self == index:
            looks.append(self)
            return len(looks) == 1
        return real_is_file(self)

    runtime = Runtime(remote_state_path(), remote_audit_path())
    client = make_client(build_app(runtime))
    monkeypatch.setattr(Path, "is_file", gone_after_the_first_look)
    response = client.get(f"/r/{runtime.token}/", headers={"accept": "text/html"})
    assert len(looks) == 2, "the race this answers: present, then gone"
    assert response.status_code == 200, response.text
    assert response.text == remote_page.bundled_page_files()["index.html"].decode("utf-8")
    assert str(isolated_home) not in response.text


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


@pytest.mark.skipif(os.name == "nt", reason="a symlink needs a privilege on Windows")
def test_a_linked_page_that_install_page_replaces_or_that_goes_is_no_longer_served(
    isolated_home: Path, built: Path, tmp_path: Path
) -> None:
    """The installed build was looked up once, as the app was built, and a ``remote-dist``
    that is a link was taken for the link's target: after ``install-page`` replaced the link
    with a build, and after the link was removed to go back to the bundled page, a running
    server still served the build it had led to (review of #243, round 10, 3/7)."""
    own = tmp_path / "own-build"
    shutil.copytree(built, own)
    (own / "index.html").write_text("<!doctype html><title>MY OWN</title>")
    other = tmp_path / "other-build"
    shutil.copytree(built, other)
    (other / "index.html").write_text("<!doctype html><title>NEW INSTALL</title>")
    remote_dist_dir().parent.mkdir(parents=True, exist_ok=True)
    remote_dist_dir().symlink_to(own)
    runtime = Runtime(remote_state_path(), remote_audit_path())
    client = make_client(build_app(runtime))  # dist_dir=None: the installed page

    def page() -> str:
        response = client.get(f"/r/{runtime.token}/", headers={"accept": "text/html"})
        assert response.status_code == 200, response.text
        return response.text

    assert "MY OWN" in page()
    remote_server.install_page(other)
    assert "NEW INSTALL" in page()
    shutil.rmtree(remote_dist_dir())
    remote_dist_dir().symlink_to(own)
    assert "MY OWN" in page()
    remote_dist_dir().unlink()
    assert page() == remote_page.bundled_page_files()["index.html"].decode("utf-8")


# --- what a request may name in an installed build -------------------------------------------


@pytest.mark.parametrize("installed", [False, True], ids=["dist", "install-page"])
@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("%00", 200),
        ("a%00.js", 404),
        ("assets/%00", 404),
        ("x" * 300 + ".js", 404),
        ("x" * 3_000, 200),
        ("a/" * 2_100 + "b.js", 404),
    ],
    ids=[
        "nul",
        "nul-in-a-name",
        "nul-in-assets",
        "a-name-past-255",
        "a-path-past-the-limit",
        "deep",
    ],
)
def test_a_page_path_the_system_refuses_is_a_miss_not_a_500(
    isolated_home: Path, built: Path, installed: bool, path: str, status: int
) -> None:
    """A NUL byte made ``resolve`` raise ``ValueError``, and a name past 255 bytes made
    ``is_file`` raise ``ENAMETOOLONG``: each answered a bare 500 ``text/plain`` with a
    traceback in the log and no page headers, to anyone holding the link (sweep 3 of
    #243). A miss is a miss: a file request's JSON 404, a navigation's document."""
    if installed:
        remote_server.install_page(built)
    runtime = Runtime(remote_state_path(), remote_audit_path())
    app = build_app(runtime) if installed else build_app(runtime, dist_dir=built)
    response = make_client(app).get(f"/r/{runtime.token}/{path}")
    assert response.status_code == status, response.text[:200]
    assert response.headers["referrer-policy"] == "no-referrer"
    if status == 404:
        assert response.json()["error"] == "not_found"
    else:
        assert response.text.startswith("<!doctype html>")


SECRET = "OPENAI_API_KEY=sk-test-123"


def _hidden_files(root: Path) -> None:
    """What a project keeps beside its page and must never be handed to a link holder."""
    (root / ".env").write_text(SECRET)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text(f"url = https://user:{SECRET}@github.com/x/y")
    (root / "assets" / ".secret.js").write_text(SECRET)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """A web project's own directory, as Vite lays it out: its SOURCE index.html at the top."""
    root = tmp_path / "aisquare-remote"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<!doctype html><script src="/src/main.ts"></script>')
    (root / "package.json").write_text('{"name": "aisquare-remote"}')
    (root / "node_modules" / "vite").mkdir(parents=True)
    (root / "node_modules" / "vite" / "package.json").write_text('{"name": "vite"}')
    _hidden_files(root)
    return root


def test_install_page_refuses_a_projects_own_directory_and_installs_nothing(
    isolated_home: Path, project_root: Path
) -> None:
    """Its source ``index.html`` passed for a page, and the whole project was copied in,
    ``.env``, ``.git`` and ``node_modules`` included, and served to anyone holding the
    link, with no passphrase (sweep 3 of #243)."""
    with pytest.raises(NoRemotePage) as refused:
        remote_server.install_page(project_root)
    assert str(refused.value) == (
        f"{project_root.resolve()} holds package.json: it is the project, not its build — "
        "point at its dist/ after npm run build"
    )
    assert not remote_dist_dir().exists()
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "remote", "install-page", str(project_root)])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "invalid_dist"
    human = runner.invoke(cli, ["remote", "install-page", str(project_root)])
    assert human.exit_code == 1 and "it is the project, not its build" in human.stderr
    assert not remote_dist_dir().exists()


def test_serve_refuses_a_projects_own_directory_as_its_dist(
    isolated_home: Path, project_root: Path
) -> None:
    problem = remote_server._page_missing(project_root)
    assert problem is not None and "it is the project, not its build" in problem
    with pytest.raises(NoRemotePage):
        remote_server.run_foreground(dist_dir=project_root, port=free_port())


@pytest.mark.parametrize("installed", [False, True], ids=["dist", "installed"])
@pytest.mark.parametrize(
    "change",
    [
        "a file others may write",
        "a directory others may write",
        "a file a shared group may write",
        "a file another account owns",
        "a file this account's own group may write",
        "a hidden file others may write",
        "a link",
    ],
)
def test_a_build_another_account_may_change_is_no_page_to_serve(
    isolated_home: Path,
    built: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed: bool,
    change: str,
) -> None:
    """``install-page`` narrows its copy's modes, but ``serve --dist`` serves a build in
    place, and a page installed before the narrowing kept its build's: a build off a FAT
    drive, ``/mnt/c`` or an archive, 0777 and 0666, was served as it was, and another
    account could replace the page that takes the passphrase (review of #243, round 8, 3/6).
    What a server never serves, a hidden file or a link's own mode, is no matter, nor is a
    group that is this account's own."""
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("Windows keeps no group or other bits")
    root = built
    if installed:
        root = remote_server.install_page(built)
    script = root / "assets" / "app.js"
    own_group = script.stat().st_gid if change.startswith("a file this account's") else None
    monkeypatch.setattr(remote_server, "_remote_private_group", lambda: own_group)
    if change == "a file others may write":
        script.chmod(0o666)
    elif change == "a directory others may write":
        (root / "assets").chmod(0o777)
    elif change.startswith("a file") and change.endswith("group may write"):
        script.chmod(0o664)
    elif change == "a file another account owns":
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    elif change == "a hidden file others may write":
        (root / "assets" / ".cache").write_text("")
        (root / "assets" / ".cache").chmod(0o666)
    else:
        try:
            (root / "linked.js").symlink_to(script)
        except OSError:
            pytest.skip("this platform cannot make the symlink")

    problem = remote_server._page_missing(None if installed else built)

    if change in {"a file this account's own group may write", "a hidden file others may write"}:
        assert problem is None
    elif change == "a link":
        assert problem is None, "a link's own mode is 0777 and says nothing"
    else:
        assert problem is not None and "can be written by another account" in problem
        assert str(root) in problem and "chmod -R go-w" in problem
        if change != "a file another account owns":
            changed = root / "assets" / ("app.js" if change.startswith("a file") else "")
            assert problem.startswith(str(changed).rstrip("/") + " ")
        with pytest.raises(NoRemotePage, match="can be written by another account"):
            remote_server.run_foreground(dist_dir=None if installed else built, port=free_port())


def _setfacl(path: Path, entry: str) -> None:
    """``setfacl -m entry path``, or a skip where there is no ``setfacl`` or no ACL."""
    import subprocess

    try:
        done = subprocess.run(["setfacl", "-m", entry, str(path)], capture_output=True)
    except OSError:
        pytest.skip("no setfacl on this machine")
    if done.returncode != 0:
        pytest.skip(f"this filesystem keeps no ACL: {done.stderr!r}")


@pytest.mark.parametrize("entry", ["file", "directory"])
def test_a_build_entry_a_posix_acl_lets_another_account_write_is_no_page_to_serve(
    isolated_home: Path, built: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Under a POSIX ACL the group bits are its mask, the most any named user or group gets,
    and the check read them as the file's group alone: ``setfacl -m u:nobody:rw`` on a file
    of this account's own group showed ``0664 anmol:anmol``, passed as start and as served,
    and let ``nobody`` rewrite the page that takes the passphrase (review of #243, round 10,
    4/7). A shared checkout's default ACL does as much for every file a build writes there.
    ``chmod g-w``, which lowers the mask, closes it again."""
    if sys.platform != "linux":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("POSIX ACLs are read on Linux")
    changed = built / "index.html" if entry == "file" else built / "assets"
    served = built / "index.html" if entry == "file" else built / "assets" / "app.js"
    own_group = changed.stat().st_gid
    monkeypatch.setattr(remote_server, "_remote_private_group", lambda: own_group)
    remedy = remote_server.PAGE_DIST_REMEDY
    changed.chmod(0o664 if entry == "file" else 0o775)
    assert remote_server._page_open_to_others(built, remedy) is None, "this account's group"

    _setfacl(changed, "u:nobody:rw" if entry == "file" else "u:nobody:rwx")

    assert stat.S_IMODE(changed.stat().st_mode) == (0o664 if entry == "file" else 0o775)
    problem = remote_server._page_open_to_others(built, remedy)
    assert problem is not None and problem.startswith(f"{changed} can be written by another")
    as_served = remote_server._page_served_open_to_others(built, served, remedy, own_group)
    assert as_served is not None and as_served.startswith(f"{changed} can be written")
    changed.chmod(0o644 if entry == "file" else 0o755)
    assert remote_server._page_open_to_others(built, remedy) is None
    assert remote_server._page_served_open_to_others(built, served, remedy, own_group) is None


def test_a_posix_acl_is_read_from_the_entry_itself(
    built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ACL as Linux keeps it, ``system.posix_acl_access``, on a machine with or without
    ``setfacl``: one there, of a file of this account's group, makes its group bit
    another account's; none, or none on the filesystem, keeps it this account's own."""
    import errno

    if sys.platform != "linux":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("POSIX ACLs are read on Linux")
    index = built / "index.html"
    index.chmod(0o664)
    info = index.stat()
    answers: dict[str, object] = {}

    def getxattr(path: object, name: str, *, follow_symlinks: bool = True) -> bytes:
        assert name == "system.posix_acl_access" and follow_symlinks is False
        answer = answers["now"]
        if isinstance(answer, OSError):
            raise answer
        assert isinstance(answer, bytes)
        return answer

    monkeypatch.setattr(os, "getxattr", getxattr)
    for answer, opened in [
        (b"\x02\x00\x00\x00", True),
        (OSError(errno.ENODATA, "No data available"), False),
        (OSError(errno.EOPNOTSUPP, "Operation not supported"), False),
        (OSError(errno.EIO, "Input/output error"), True),
    ]:
        answers["now"] = answer
        assert remote_server._page_entry_open(index, info, info.st_gid) is opened, answer
    assert remote_server._page_entry_open(index, info, info.st_gid + 1) is True, "not its own"


HOME_FOLDERS = "group:ABCDEFAB-CDEF-ABCD-EFAB-CDEF0000000C:everyone:12:deny:delete"
"""The ACL macOS puts on each folder of a home (``ls -le ~``), as ``acl_to_text`` writes it."""
BOB = "user:FFFFEEEE-DDDD-CCCC-BBBB-AAAA000001F5:bob:501"
ADMIN = "group:ABCDEFAB-CDEF-ABCD-EFAB-CDEF00000050:admin:80"


@pytest.mark.parametrize(
    ("entries", "opened"),
    [
        (HOME_FOLDERS, False),
        (f"{BOB}:allow:read,readattr", False),
        (f"{BOB}:allow:read,write", True),
        (f"{ADMIN}:allow:add_file,file_inherit", True),
        (f"{HOME_FOLDERS}\n{ADMIN}:allow:list,delete_child", True),
        ("user:FFFFEEEE-DDDD-CCCC-BBBB-AAAA00000ABC:me:{own}:allow:write,delete", False),
    ],
    ids=["home-folders", "allows-a-read", "allows-a-write", "allows-an-add", "both", "own"],
)
def test_a_macos_acl_that_allows_another_account_to_change_an_entry_opens_it(
    built: Path, monkeypatch: pytest.MonkeyPatch, entries: str, opened: bool
) -> None:
    """macOS's ACLs grant beside the mode, which shows none of them: ``chmod +a "bob allow
    write"`` on a 0644 file let ``bob`` rewrite it, and the check passed it (review of #243,
    round 10, 4/7). An entry that allows another account a right that changes it is open;
    a deny, as macOS puts on each home's folders, or a right that only reads, is not. Read
    as ``acl_to_text`` writes it, on any machine."""
    assert (
        remote_server._page_acl_text_allows_write(f"!#acl 1\n{entries.format(own=4242)}\n", 4242)
        is opened
    )
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        return
    index = built / "index.html"
    index.chmod(0o644)
    acl = f"!#acl 1\n{entries.format(own=os.getuid())}\n"
    monkeypatch.setattr(remote_server, "_darwin_acl_text", lambda path: acl)
    monkeypatch.setattr(sys, "platform", "darwin")
    seen = remote_server._page_entry_open(index, index.stat(), None)
    monkeypatch.undo()
    assert seen is opened


def test_a_macos_acl_that_cannot_be_read_opens_the_entry(
    built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("Windows keeps no such modes")
    index = built / "index.html"
    index.chmod(0o644)

    def unreadable(path: str) -> str | None:
        raise OSError(5, "Input/output error", path)

    monkeypatch.setattr(remote_server, "_darwin_acl_text", unreadable)
    monkeypatch.setattr(sys, "platform", "darwin")
    seen = remote_server._page_entry_open(index, index.stat(), None)
    monkeypatch.undo()
    assert seen is True


@pytest.mark.skipif(sys.platform != "darwin", reason="chmod +a is macOS's")
def test_a_build_file_a_macos_acl_lets_another_account_write_is_no_page_to_serve(
    isolated_home: Path, built: Path
) -> None:
    """On macOS itself: ``chmod +a "nobody allow write"`` leaves the mode 0644."""
    import subprocess

    index = built / "index.html"
    subprocess.run(["chmod", "+a", "nobody allow write", str(index)], check=True)
    problem = remote_server._page_open_to_others(built, remote_server.PAGE_DIST_REMEDY)
    assert problem is not None and problem.startswith(f"{index} can be written by another")


def test_a_group_that_is_another_accounts_own_is_no_private_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An account whose primary group is this account's own is in it, and never in its
    member list, which was all that was asked: the group was taken for a private one, and
    a file it could write for this account's alone (review of #243, round 10, 4/7)."""
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("Windows has no groups to ask")
    import grp
    import pwd
    from types import SimpleNamespace

    me = SimpleNamespace(pw_name="anmol", pw_uid=1000, pw_gid=1000)
    accounts = [me, SimpleNamespace(pw_name="root", pw_uid=0, pw_gid=0)]
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: me)
    monkeypatch.setattr(grp, "getgrgid", lambda gid: SimpleNamespace(gr_name="anmol", gr_mem=[]))
    monkeypatch.setattr(pwd, "getpwall", lambda: accounts)
    assert remote_server._remote_private_group() == 1000
    accounts.append(SimpleNamespace(pw_name="build", pw_uid=1001, pw_gid=1000))
    assert remote_server._remote_private_group() is None


@pytest.mark.skipif(not can_deny_reads(), reason="this account lists any directory (root, or NTFS)")
@pytest.mark.parametrize("installed", [False, True], ids=["dist", "installed"])
@pytest.mark.parametrize(
    ("mode", "says"),
    [(0o333, "can be written by another account"), (0o311, "cannot be read to check")],
    ids=["others-may-write", "this-account-may-not-list"],
)
def test_a_directory_of_the_build_this_account_cannot_list_is_still_looked_at(
    isolated_home: Path,
    built: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed: bool,
    mode: int,
    says: str,
) -> None:
    """The walk passed over a directory this account could not list, its own mode and owner
    with it, and the server still serves a file inside it by name, which needs only search:
    a world-writable ``assets`` at 0333 left the build served (review of #243, round 9,
    3/6). Its mode is read where its parent lists it, and one that cannot be listed is a
    build that cannot be checked."""
    root = remote_server.install_page(built) if installed else built
    monkeypatch.setattr(remote_server, "_remote_private_group", lambda: None)
    assets = root / "assets"
    assets.chmod(mode)
    try:
        served = remote_server._built_page_file(root.resolve(), "assets/app.js")
        problem = remote_server._page_missing(None if installed else built)
    finally:
        assets.chmod(0o755)

    assert served is not None, "the premise: a file in it is served by name"
    assert problem is not None and problem.startswith(f"{assets} "), problem
    assert says in problem and f"go-w {root}," in problem


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps no group or other bits")
@pytest.mark.parametrize("installed", [False, True], ids=["dist", "installed"])
@pytest.mark.parametrize("up", [1, 2], ids=["parent", "grandparent"])
@pytest.mark.parametrize(
    ("mode", "group", "served"),
    [
        (0o775, "shared", False),
        (0o777, "shared", False),
        (0o1777, "shared", True),
        (0o775, "this account's own", True),
    ],
    ids=["a-shared-group-may-write", "others-may-write", "others-may-write-sticky", "own-group"],
)
def test_a_build_in_a_directory_another_account_may_change_is_no_page_to_serve(
    isolated_home: Path,
    built: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed: bool,
    up: int,
    mode: int,
    group: str,
    served: bool,
) -> None:
    """The check looked at the build and below it, never above: a member of the group of a
    shared checkout's ``root:dev 2775`` could rename the build away and put its own in its
    place, every file of which passed (review of #243, round 9, 2/6). For the installed
    build, what is above is ``~/.aisquare`` and the home. A directory with the sticky bit,
    as ``/tmp`` has, lets no one else rename what is in it."""
    if installed:
        root = remote_server.install_page(built)
    else:
        root = tmp_path / "outer" / "inner" / "dist"
        shutil.copytree(built, root)
    above = root.parents[up - 1]
    shared = group == "shared"
    monkeypatch.setattr(
        remote_server, "_remote_private_group", lambda: None if shared else above.stat().st_gid
    )
    above.chmod(mode)
    try:
        problem = remote_server._page_missing(None if installed else root)
    finally:
        above.chmod(0o700)

    if served:
        assert problem is None
    else:
        assert problem is not None
        assert problem.startswith(f"{above} can be changed by another account"), problem
        assert f"in place of {root} " in problem and f"chmod go-w {above} " in problem


def _closed(root: Path) -> Path:
    """``root``, a build, with the modes a umask of 022 gives it, whatever this one's is."""
    for path in (root, *root.rglob("*")):
        path.chmod(0o755 if path.is_dir() else 0o644)
    return root


_ASKED = {"index": "", "asset": "assets/app.js", "navigation": "fleet/coder-1"}
"""What a page load asks a build for: its index, a file it loads, and a deep link, which
the index answers."""


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps no group or other bits")
def test_a_build_installed_by_hand_while_remote_is_on_is_asked_about_as_it_is_served(
    isolated_home: Path, built: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The check ran as Remote started, and the server picks its build per request: one
    put in ``~/.aisquare/remote-dist`` by hand while the bundled page was served, 0777 and
    0666 as ``cp -r`` leaves a build off an exFAT drive, was served unasked (review of
    #243, round 9, 1/6). Each file is now asked about as it is served."""
    runtime = Runtime(remote_state_path(), remote_audit_path())
    client = make_client(build_app(runtime))
    assert remote_server._page_missing(None) is None
    assert client.get(f"/r/{runtime.token}/").status_code == 200, "the bundled page"

    installed = shutil.copytree(_closed(built), remote_dist_dir())
    for path in (installed, installed / "assets"):
        path.chmod(0o777)
    for path in (installed / "index.html", installed / "assets" / "app.js"):
        path.chmod(0o666)
    with caplog.at_level(logging.WARNING, logger=remote_server.__name__):
        answers = {
            name: client.get(f"/r/{runtime.token}/{rel}", headers={"accept": "text/html"})
            for name, rel in _ASKED.items()
        }

    assert {name: answer.status_code for name, answer in answers.items()} == dict.fromkeys(
        _ASKED, 404
    )
    assert {answer.json()["message"] for answer in answers.values()} == {
        remote_server.PAGE_NOT_SERVED
    }
    assert not any(str(isolated_home) in answer.text for answer in answers.values())
    said = [r.getMessage() for r in caplog.records if r.name == remote_server.__name__]
    assert len(said) == 3 and all("can be written by another account" in line for line in said)
    assert f"remote: {installed.resolve()}/index.html can be written" in said[0]
    assert "install the build again" in said[0]


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps no group or other bits")
@pytest.mark.parametrize(
    ("changed", "refused"),
    [
        ("index.html", {"index", "navigation"}),
        ("assets", {"asset"}),
        ("the directory above", {"index", "asset", "navigation"}),
    ],
    ids=["index.html", "assets", "the-directory-above"],
)
def test_a_dist_build_changed_in_place_while_remote_is_on_is_not_served(
    isolated_home: Path,
    built: Path,
    caplog: pytest.LogCaptureFixture,
    changed: str,
    refused: set[str],
) -> None:
    """A ``serve --dist`` build checked as it started and then opened to others in place,
    or the directory it is in, was served as it was (review of #243, round 9, 1/6). Only
    what is served through what changed is refused."""
    dist = _closed(built)
    runtime = Runtime(remote_state_path(), remote_audit_path())
    client = make_client(build_app(runtime, dist_dir=dist))

    def answers() -> dict[str, int]:
        return {
            name: client.get(
                f"/r/{runtime.token}/{rel}", headers={"accept": "text/html"}
            ).status_code
            for name, rel in _ASKED.items()
        }

    assert remote_server._page_missing(dist) is None
    assert answers() == dict.fromkeys(_ASKED, 200)
    opened = dist.parent if changed == "the directory above" else dist / changed
    opened.chmod(0o666 if opened.is_file() else 0o777)
    try:
        with caplog.at_level(logging.WARNING, logger=remote_server.__name__):
            after = answers()
    finally:
        opened.chmod(0o700 if opened == dist.parent else 0o755)

    assert after == {name: 404 if name in refused else 200 for name in _ASKED}
    said = {r.getMessage() for r in caplog.records if r.name == remote_server.__name__}
    assert len(said) == 1 and next(iter(said)).startswith(f"remote: {opened} can be ")
    assert "serve without --dist" in next(iter(said))


def test_install_page_leaves_behind_what_a_server_would_not_serve(
    isolated_home: Path, built: Path, tmp_path: Path
) -> None:
    """Hidden files, and a link out of the build or to a hidden file, whose content the
    copy held under the link's own name: the installed page is what ``--dist`` of the
    same build would serve, and no more (sweep 3 of #243)."""
    _hidden_files(built)
    (tmp_path / "elsewhere.txt").write_text(SECRET)
    links = {"linked.js": built / ".env", "away.js": tmp_path / "elsewhere.txt"}
    try:
        for name, target in links.items():
            (built / name).symlink_to(target)
        (built / "kept.js").symlink_to(built / "assets" / "app.js")
    except OSError:
        links = {}
    destination = remote_server.install_page(built)
    copied = sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*"))
    expected = ["assets", "assets/app.js", "index.html", *(["kept.js"] if links else [])]
    assert copied == sorted(expected)
    assert all(SECRET not in path.read_text() for path in destination.rglob("*.*"))


@pytest.mark.parametrize("installed", [False, True], ids=["dist", "install-page"])
@pytest.mark.parametrize(
    "path", [".env", ".git/config", "assets/.secret.js", "assets/../.env", "linked.js"]
)
@pytest.mark.parametrize("accept", ["*/*", "text/html"])
def test_a_hidden_file_of_the_build_is_never_served(
    isolated_home: Path, built: Path, installed: bool, path: str, accept: str
) -> None:
    """The bundled page leaves its dotfiles out; a build's were served whole, to anyone
    holding the link, with no passphrase (sweep 3 of #243). Judged where a link resolves
    too: ``linked.js`` points at ``.env``."""
    _hidden_files(built)
    try:
        (built / "linked.js").symlink_to(built / ".env")
    except OSError:
        if path == "linked.js":
            pytest.skip("this platform cannot make the symlink")
    if installed:
        remote_server.install_page(built)
        target = remote_dist_dir()
        _hidden_files(target)  # put there by hand: a server still never serves them
        if (built / "linked.js").is_symlink():
            (target / "linked.js").unlink(missing_ok=True)
            (target / "linked.js").symlink_to(target / ".env")
    runtime = Runtime(remote_state_path(), remote_audit_path())
    app = build_app(runtime) if installed else build_app(runtime, dist_dir=built)
    response = make_client(app).get(f"/r/{runtime.token}/{path}", headers={"accept": accept})
    assert SECRET not in response.text
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        assert response.text.startswith("<!doctype html>"), "only ever the document"


@pytest.mark.parametrize("installed", [False, True], ids=["dist", "install-page"])
def test_a_symlink_loop_in_the_build_is_a_file_it_does_not_have_on_every_python(
    isolated_home: Path, built: Path, installed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Path.resolve`` of a symlink loop raises ``RuntimeError`` ("Symlink loop from ...") on
    Python 3.11 and 3.12, which CI runs, and returns the path on 3.13. The page's lookup and
    install-page's copy caught ``OSError`` and ``ValueError``: a loop in a build answered a
    bare 500, a traceback in the log, to anyone holding the link, and stopped install-page
    with one. On 3.13 resolve is made to raise for the loop as 3.12's does."""
    loop = built / "loop.js"
    try:
        loop.symlink_to(loop)
    except OSError:
        pytest.skip("this platform cannot make the symlink")
    if sys.version_info >= (3, 13):
        resolve = Path.resolve

        def resolve_as_3_12(self: Path, strict: bool = False) -> Path:
            if self.name == "loop.js":
                raise RuntimeError(f"Symlink loop from {str(self)!r}")
            return resolve(self, strict)

        monkeypatch.setattr(Path, "resolve", resolve_as_3_12)
    if installed:
        destination = remote_server.install_page(built)
        assert not (destination / "loop.js").is_symlink()
    runtime = Runtime(remote_state_path(), remote_audit_path())
    app = build_app(runtime) if installed else build_app(runtime, dist_dir=built)
    response = make_client(app).get(f"/r/{runtime.token}/loop.js")
    assert response.status_code == 404, response.text
