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

import json
import socket
import urllib.request
from collections.abc import Callable
from pathlib import Path

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
