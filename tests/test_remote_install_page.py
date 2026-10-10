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
