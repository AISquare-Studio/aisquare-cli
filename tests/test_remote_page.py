"""The phone page aisquare-cli bundles (SPEC §6): served by default, safe by construction.

A fresh machine had no page installed, so the first ``R`` press started a server
with nothing to show. The page now ships inside the package
(``aisquare.web.remote``) and the server answers from it whenever neither
``--dist`` nor ``asq remote install-page`` gave it another one. Three layers:

* serving, through ``TestClient``: the bundled page by default, the two overrides,
  the content types, the ETags, and the headers (a strict CSP on the bundled page);
* the files, read as text: every reference resolves, nothing points at another
  origin, none of the DOM sinks a server string could reach appears, and the page's
  API table names only routes the built app has;
* the page's pure core, run by node (``tests/js/remote_page_check.js``): hostile
  pane rows, runs and needs items become text and fixed elements, and nothing else;
* the whole page booted by node in a fake browser (``tests/js/remote_page_boot.js``):
  what it draws and what it sends, scenario by scenario, as the machine answers.

Every static guard below has a control that feeds it the thing it forbids: a
pattern that matches nothing passes on any page.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from aisquare.core.paths import remote_dist_dir
from aisquare.services import remote_needs, remote_page, remote_server, transcript
from aisquare.services.remote_server import (
    NO_PAGE_HINT,
    Runtime,
    Sources,
    build_app,
    write_endpoint_names,
)
from tests.remote_kit_helpers import base, make_client, make_runtime, mounted_routes

WEB = Path(str(resources.files("aisquare.web.remote")))
HARNESS = Path(__file__).resolve().parent / "js" / "remote_page_check.js"
BOOT_HARNESS = Path(__file__).resolve().parent / "js" / "remote_page_boot.js"
PAGE_FILES = (
    "index.html",
    "app.css",
    "app.js",
    "sw.js",
    "manifest.webmanifest",
    "icon.svg",
    "icon-180.png",
)
TEXT_FILES = ("index.html", "app.css", "app.js", "sw.js", "manifest.webmanifest")


def _text(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def _sources() -> Sources:
    return Sources(
        projects=lambda: [],
        fleet=lambda project: {},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
    )


@pytest.fixture
def runtime() -> Runtime:
    return make_runtime()


@pytest.fixture
def client(runtime: Runtime) -> TestClient:
    """The app as the TUI and ``serve`` build it: no ``--dist``, nothing installed."""
    return make_client(build_app(runtime, sources=_sources()))


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A build of another page, as ``install-page`` and ``--dist`` take one."""
    root = tmp_path / "built"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>installed build</title>")
    (root / "assets" / "app.js").write_text("console.log('installed')")
    return root


@pytest.fixture
def no_bundled_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """An install that lost its page: the package's files read as none at all."""
    monkeypatch.setattr(remote_page, "bundled_page_files", dict)


# --- 1. served by default -----------------------------------------------------------------


def test_a_fresh_machine_is_served_the_bundled_page(client: TestClient, runtime: Runtime) -> None:
    assert not remote_dist_dir().exists()

    response = client.get(f"{base(runtime)}/")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.content == (WEB / "index.html").read_bytes()
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "unsafe-" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-frame-options"] == "DENY"


def test_the_policy_lets_nothing_but_this_origin_in() -> None:
    """Each directive the page needs, at 'self' or 'none', and nothing wider anywhere."""
    directives = dict(
        part.strip().split(" ", 1) for part in remote_page.PAGE_CSP.split(";") if part.strip()
    )
    assert directives["default-src"] == "'none'"
    for name in ("script-src", "style-src", "connect-src", "manifest-src", "worker-src"):
        assert directives[name] == "'self'", name
    assert directives["img-src"] == "'self' data:"
    for name in ("base-uri", "form-action", "frame-ancestors"):
        assert directives[name] == "'none'", name
    assert "*" not in remote_page.PAGE_CSP and "http" not in remote_page.PAGE_CSP


def test_the_page_needs_no_device_and_a_top_level_name_is_the_document(
    client: TestClient, runtime: Runtime
) -> None:
    """``#/unlock`` must load for a phone that has not unlocked yet; ``/unlock`` (an older
    page's route) gets the same document, whose relative URLs still resolve from there."""
    response = client.get(f"{base(runtime)}/unlock")

    assert response.status_code == 200
    assert response.content == (WEB / "index.html").read_bytes()


def test_a_deeper_path_is_sent_to_the_top_where_relative_urls_resolve(
    client: TestClient, runtime: Runtime
) -> None:
    """At ``/fleet/coder-1`` the document would ask for ``/fleet/app.js`` and boot blank."""
    for path in ("fleet/coder-1", "fleet/"):
        response = client.get(f"{base(runtime)}/{path}", follow_redirects=False)
        assert response.status_code == 307, path
        assert response.headers["location"] == f"{base(runtime)}/"
        assert response.headers["referrer-policy"] == "no-referrer"


# --- 2. overrides ---------------------------------------------------------------------------


def test_an_installed_page_wins_over_the_bundled_one(
    client: TestClient, runtime: Runtime, built: Path
) -> None:
    remote_server.install_page(built)

    index = client.get(f"{base(runtime)}/")
    asset = client.get(f"{base(runtime)}/assets/app.js")

    assert index.status_code == 200 and "installed build" in index.text
    assert asset.text == "console.log('installed')"
    # The page headers go on an installed build too; the CSP is the bundled page's own.
    assert index.headers["referrer-policy"] == "no-referrer"
    assert index.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in index.headers


def test_the_choice_is_made_per_request_without_a_restart(
    client: TestClient, runtime: Runtime, built: Path
) -> None:
    """``install-page`` takes over a running server, and removing it hands back."""
    assert client.get(f"{base(runtime)}/").content == (WEB / "index.html").read_bytes()
    remote_server.install_page(built)
    assert "installed build" in client.get(f"{base(runtime)}/").text
    shutil.rmtree(remote_dist_dir())
    assert client.get(f"{base(runtime)}/").content == (WEB / "index.html").read_bytes()


def test_an_explicit_dist_wins_over_the_installed_page(
    runtime: Runtime, built: Path, tmp_path: Path
) -> None:
    remote_server.install_page(built)
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (chosen / "index.html").write_text("<!doctype html><title>the --dist page</title>")

    response = make_client(build_app(runtime, sources=_sources(), dist_dir=chosen)).get(
        f"{base(runtime)}/"
    )

    assert "the --dist page" in response.text


def test_an_explicit_dist_without_a_page_is_still_a_404_never_the_bundled_one(
    runtime: Runtime, tmp_path: Path
) -> None:
    """``--dist`` names a directory on purpose: a wrong one says so, per request."""
    missing = tmp_path / "still-building"
    client = make_client(build_app(runtime, sources=_sources(), dist_dir=missing))

    response = client.get(f"{base(runtime)}/")

    assert response.status_code == 404
    assert response.json()["error"] == "no_dist"
    assert "no index.html in" in response.json()["message"]
    assert response.headers["referrer-policy"] == "no-referrer"


def test_an_install_without_its_bundled_page_says_to_reinstall(
    client: TestClient, runtime: Runtime, no_bundled_page: None
) -> None:
    response = client.get(f"{base(runtime)}/")

    assert response.status_code == 404
    assert response.json() == {"error": "no_dist", "message": NO_PAGE_HINT}
    assert "reinstall aisquare-cli" in NO_PAGE_HINT
    assert remote_server._page_missing(None) == NO_PAGE_HINT


# --- 3. types, misses and revalidation -----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("sw.js", "text/javascript; charset=utf-8"),
        ("app.js", "text/javascript; charset=utf-8"),
        ("manifest.webmanifest", "application/manifest+json"),
        ("icon.svg", "image/svg+xml"),
        ("icon-180.png", "image/png"),
        ("app.css", "text/css; charset=utf-8"),
    ],
)
def test_each_file_is_served_with_its_own_type(
    client: TestClient, runtime: Runtime, name: str, content_type: str
) -> None:
    response = client.get(f"{base(runtime)}/{name}")

    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.content == (WEB / name).read_bytes()
    assert response.headers["content-security-policy"] == remote_page.PAGE_CSP


@pytest.mark.parametrize("name", ["nope.js", "__init__.py", "app.js.map", "assets/app.js"])
def test_a_file_the_page_does_not_serve_is_a_404_and_never_the_document(
    client: TestClient, runtime: Runtime, name: str
) -> None:
    response = client.get(f"{base(runtime)}/{name}", follow_redirects=False)

    assert response.status_code == 404, name
    assert "text/html" not in response.headers["content-type"]
    assert response.json()["error"] == "not_found"


def test_an_unchanged_file_is_answered_304_by_its_etag(
    client: TestClient, runtime: Runtime
) -> None:
    first = client.get(f"{base(runtime)}/app.js")
    etag = first.headers["etag"]

    again = client.get(f"{base(runtime)}/app.js", headers={"if-none-match": etag})
    weak = client.get(f"{base(runtime)}/app.js", headers={"if-none-match": f'"x", W/{etag}'})
    stale = client.get(f"{base(runtime)}/app.js", headers={"if-none-match": '"another"'})

    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == etag
    assert again.headers["cache-control"] == "no-cache"
    assert weak.status_code == 304
    assert stale.status_code == 200 and stale.content == first.content
    assert client.get(f"{base(runtime)}/sw.js").headers["etag"] != etag  # per file


def test_the_content_types_are_a_closed_list() -> None:
    assert remote_page.page_content_type("a.HTML") == "text/html; charset=utf-8"
    for name in ("x.py", "x.map", "x.txt", "x", "x.json"):
        assert remote_page.page_content_type(name) is None, name


def test_the_page_headers_keep_the_token_in_the_path_to_ourselves() -> None:
    assert remote_page.remote_page_headers() == {
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "x-frame-options": "DENY",
        "permissions-policy": "camera=(), microphone=(), geolocation=()",
    }


@pytest.fixture
def fake_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The package directory replaced by a temporary one; the read cache cleared around it."""
    package = tmp_path / "package"
    package.mkdir()
    monkeypatch.setattr(resources, "files", lambda name: package)
    remote_page._bundled_items.cache_clear()
    yield package
    remote_page._bundled_items.cache_clear()


def test_the_bundled_files_leave_out_the_package_init_and_dotfiles(fake_package: Path) -> None:
    (fake_package / "index.html").write_text("<!doctype html>")
    (fake_package / "app.js").write_text("'use strict';")
    (fake_package / "__init__.py").write_text("")
    (fake_package / ".DS_Store").write_text("finder")
    (fake_package / "__pycache__").mkdir()

    assert remote_page.bundled_page_files() == {
        "app.js": b"'use strict';",
        "index.html": b"<!doctype html>",
    }
    assert remote_page.bundled_page_present()


def test_the_package_ships_exactly_the_page() -> None:
    """The real package, read the way the server reads it: these files and no others."""
    remote_page._bundled_items.cache_clear()

    assert sorted(remote_page.bundled_page_files()) == sorted(PAGE_FILES)


# --- 4. references resolve ------------------------------------------------------------------


def test_every_reference_in_the_document_and_the_manifest_is_in_the_package() -> None:
    index = _text("index.html")
    references = re.findall(r'\s(?:src|href)="([^"]+)"', index)
    manifest = json.loads(_text("manifest.webmanifest"))
    references += [icon["src"] for icon in manifest["icons"]]

    assert {"app.js", "app.css", "manifest.webmanifest", "icon.svg", "icon-180.png"} <= set(
        references
    )
    for reference in references:
        assert (WEB / reference).is_file(), reference
        assert not reference.startswith(("/", ".")), f"{reference} must be relative to the page"
    assert manifest["start_url"] == "./" and manifest["scope"] == "./"
    assert manifest["display"] == "standalone"
    assert 'crossorigin="use-credentials"' in index


def test_the_touch_icon_is_a_180_pixel_png() -> None:
    data = (WEB / "icon-180.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    assert struct.unpack(">II", data[16:24]) == (180, 180)


def test_the_web_package_init_is_empty_as_the_captain_branch_has_it() -> None:
    """#240 adds the same empty ``web/__init__.py``: identical bytes merge cleanly."""
    assert (WEB.parent / "__init__.py").read_bytes() == b""
    assert (WEB / "__init__.py").read_bytes() == b""


# --- 5. no external URLs --------------------------------------------------------------------

_EXTERNAL = re.compile(r"(?i)(?:https?:)?//[a-z0-9]")
_SVG_NAMESPACE = 'xmlns="http://www.w3.org/2000/svg"'


def _external_urls(text: str) -> list[str]:
    return [text[m.start() : m.start() + 40] for m in _EXTERNAL.finditer(text)]


def test_nothing_points_at_another_origin() -> None:
    for name in TEXT_FILES:
        assert _external_urls(_text(name)) == [], name
    svg = _text("icon.svg")
    assert _SVG_NAMESPACE in svg
    assert _external_urls(svg.replace(_SVG_NAMESPACE, "")) == []


def test_the_external_url_check_can_fail() -> None:
    assert _external_urls('<script src="//cdn.example/x.js">')
    assert _external_urls("fetch('https://evil.example/')")
    assert not _external_urls("// a comment, and a path: api/needs")


# --- 6. no dangerous sinks ------------------------------------------------------------------

#: SPEC §6.6: the only attribute names the page may set, each as a literal; and the ARIA
#: states a modal sheet, a tab, the bottom nav and a toggle need, whose values are literals.
ALLOWED_ATTRIBUTES = frozenset(
    {
        "aria-label",
        "aria-hidden",
        "aria-live",
        "aria-modal",
        "aria-labelledby",
        "aria-selected",
        "aria-current",
        "aria-expanded",
        "role",
        "type",
        "autocapitalize",
        "autocorrect",
        "spellcheck",
        "autocomplete",
        "inputmode",
        "enterkeyhint",
    }
)

_SINKS = {
    "innerHTML": re.compile(r"\binnerHTML\b"),
    "outerHTML": re.compile(r"\bouterHTML\b"),
    "insertAdjacentHTML": re.compile(r"\binsertAdjacentHTML\b"),
    "document.write": re.compile(r"\bdocument\s*\.\s*write"),
    "eval(": re.compile(r"\beval\s*\("),
    "new Function": re.compile(r"\bnew\s+Function\b"),
    "javascript:": re.compile(r"(?i)javascript\s*:"),
    ".href =": re.compile(r"\.\s*href\s*=(?!=)"),
    ".src =": re.compile(r"\.\s*src\s*=(?!=)"),
    ".action =": re.compile(r"\.\s*action\s*=(?!=)"),
    ".formAction =": re.compile(r"\.\s*formAction\s*=(?!=)"),
    "cssText": re.compile(r"\bcssText\b"),
    "an on* property": re.compile(r"\.\s*on[a-z]+\s*=(?!=)"),
}
_HTML_SINKS = {
    "an inline handler": re.compile(r"(?i)<[^>]*\son[a-z]+\s*="),
    "a style attribute": re.compile(r"(?i)<[^>]*\sstyle\s*="),
    "a style element": re.compile(r"(?i)<style\b"),
}
_SET_ATTRIBUTE = re.compile(r"setAttribute\s*\(")
_LITERAL_NAME = re.compile(r"setAttribute\s*\(\s*([\"'])([a-z-]+)\1\s*,")
_NAVIGATION = re.compile(r"location\s*\.\s*(?:hash\s*=(?!=)|replace\s*\(|assign\s*\()")


def _sinks(source: str, patterns: dict[str, re.Pattern[str]]) -> list[str]:
    return [name for name, pattern in patterns.items() if pattern.search(source)]


def _bad_attributes(source: str) -> list[str]:
    """``setAttribute`` calls whose name is not one allowed literal."""
    bad = []
    for call in _SET_ATTRIBUTE.finditer(source):
        literal = _LITERAL_NAME.match(source, call.start())
        if literal is None or literal.group(2) not in ALLOWED_ATTRIBUTES:
            bad.append(source[call.start() : call.start() + 50])
    return bad


def _navigations_outside_page_go(source: str) -> tuple[int, list[int]]:
    """(navigations inside ``pageGo``, offsets of any outside it): a hash set, or a
    ``location.replace`` or ``assign``."""
    start = source.index("function pageGo(")
    end = source.index("\n}\n", start)
    inside = [m.start() for m in _NAVIGATION.finditer(source) if start < m.start() < end]
    outside = [m.start() for m in _NAVIGATION.finditer(source) if not start < m.start() < end]
    return len(inside), outside


def _scripts_without_src(html: str) -> list[str]:
    return [tag for tag in re.findall(r"(?i)<script\b[^>]*>", html) if "src=" not in tag]


def test_the_page_uses_none_of_the_sinks_a_server_string_could_reach() -> None:
    for name in ("app.js", "sw.js"):
        source = _text(name)
        assert _sinks(source, _SINKS) == [], name
        assert _bad_attributes(source) == [], name
    html = _text("index.html")
    assert _sinks(html, _SINKS) == []
    assert _sinks(html, _HTML_SINKS) == []
    assert _scripts_without_src(html) == []


def test_the_page_navigates_in_page_go_and_nowhere_else() -> None:
    inside, outside = _navigations_outside_page_go(_text("app.js"))
    assert inside == 2, "pageGo is THE place the page navigates: it pushes, or it replaces"
    assert outside == []


def test_the_sink_checks_can_fail() -> None:
    """Each check, fed the thing it forbids."""
    bad_js = (
        "el.innerHTML = x; a.href = u; img.src = u; f.action = u; b.formAction = u;"
        "s.cssText = c; b.onclick = f; eval(x); new Function(x); document.write(x);"
        "x.outerHTML; y.insertAdjacentHTML; 'javascript:alert(1)';"
    )
    assert set(_sinks(bad_js, _SINKS)) == set(_SINKS)
    assert not _sinks("a.href == b; x.onlyIf; const one = 1; location.hash === h", _SINKS)
    assert len(_bad_attributes('e.setAttribute("href", u); e.setAttribute(name, v)')) == 2
    assert _bad_attributes('e.setAttribute("aria-label", "Send")') == []
    html = '<div onclick="x"><p style="color:red"><style></style><script>alert(1)</script>'
    assert set(_sinks(html, _HTML_SINKS)) == set(_HTML_SINKS)
    assert _scripts_without_src(html) == ["<script>"]
    elsewhere = (
        "function pageGo(r) {\n  location.hash = r;\n}\nfunction other() { location.hash = x; }"
    )
    assert _navigations_outside_page_go(elsewhere)[1] != []
    replaced = "function pageGo(r) {\n  location.hash = r;\n}\nlocation.replace(x);"
    assert len(_navigations_outside_page_go(replaced)[1]) == 1


def test_the_document_is_markup_only() -> None:
    html = _text("index.html")
    assert re.search(r'<script src="app\.js" defer></script>', html)
    assert '<div id="app"></div>' in html
    assert re.search(r'<meta name="referrer" content="no-referrer">', html)


# --- 8. the API table and the write list ----------------------------------------------------


def _js_table(name: str, opener: str, closer: str) -> str:
    table = rf"const {name} = Object\.freeze\({opener}(.*?){closer}\);"
    match = re.search(table, _text("app.js"), re.S)
    assert match is not None, f"the {name} table is not where the page declares it"
    return match.group(1)


def _api_table() -> dict[str, str]:
    return dict(re.findall(r'(\w+):\s*"([^"]*)"', _js_table("API", r"\{", r"\}")))


def _writes_table() -> list[str]:
    return re.findall(r'"([^"]*)"', _js_table("WRITES", r"\[", r"\]"))


def _concrete_routes(app: Any) -> list[Any]:
    """The built app's routes, the catch-alls left out: they would match any path at all."""
    return [route for route in mounted_routes(app) if ":path}" not in getattr(route, "path", "")]


def _routes_for(app: Any, value: str) -> list[str]:
    path = "/" + re.sub(r"\{(\w+)\}", r"\1", value)
    return [route.path for route in _concrete_routes(app) if route.path_regex.match(path)]


@pytest.fixture
def app(runtime: Runtime) -> Any:
    return build_app(runtime, sources=_sources())


def test_the_route_match_sees_real_routes_and_not_the_catch_alls(app: Any) -> None:
    """The control for the test below: a real read matches, an unknown name matches nothing."""
    assert _routes_for(app, "api/fleet") == ["/api/fleet"]
    assert _routes_for(app, "api/devices/{id}") == ["/api/devices/{device_id}"]
    assert _routes_for(app, "ws") == ["/ws"]
    assert _routes_for(app, "api/no-such-thing") == []


def test_every_path_in_the_page_api_table_is_a_route_of_the_built_app(app: Any) -> None:
    missing = [key for key, value in _api_table().items() if not _routes_for(app, value)]
    assert missing == []


def test_every_write_the_page_sends_is_one_the_dispatcher_answers() -> None:
    assert set(_writes_table()) <= set(write_endpoint_names())


def _requested_paths(source: str) -> list[str]:
    """What every ``apiWrite`` and ``apiCall`` in the page is given as its path."""
    writes = re.findall(r"(?<!function )apiWrite\(([^,]+),", source)
    return writes + re.findall(r'apiCall\("[A-Z]+", ([^,)]+)', source)


def test_every_path_the_page_requests_comes_from_its_tables() -> None:
    """``WRITES`` was held to the dispatcher's list, but the page sent its writes to paths
    typed at each call site, which nothing held to anything: a typo in Post or Reply would
    ship green, and answer 404 on the phone. A write's path is now ``writePath`` of a name
    ``WRITES`` lists, a read's an ``API`` entry, and no ``api/`` path is typed elsewhere."""
    source = _text("app.js")
    assert re.findall(r'"api/[^"]+"', source[source.index("function writePath(") :]) == []
    # path and pending.path: apiWrite's own, passed on; a ternary chooses between two entries.
    allowed = (
        r"API\.\w+|apiPath\(API\.\w+|writePath\(.+\)|(?:pending\.)?path|\w+ \? API\.\w+ : API\.\w+"
    )
    paths = _requested_paths(source)
    assert len(paths) > 20 and [p for p in paths if not re.fullmatch(allowed, p.strip())] == []
    named = re.findall(r'writePath\("([^"]+)"\)', source)
    assert named and set(named) <= set(_writes_table())
    (prefix,) = re.findall(r'writePath\("([^"]+)" \+ kind\)', source)
    actions = re.search(r"const AGENT_ACTIONS = \{(.*?)\n\};", source, re.S)
    assert actions is not None
    kinds = re.findall(r"^  (\w+): \{", actions.group(1), re.M)
    assert kinds == ["stop", "restart", "switch"]
    assert {prefix + kind for kind in kinds} <= set(_writes_table())


def test_the_path_check_can_fail() -> None:
    """The control: a typed path, and a write given a path from neither table."""
    typed = 'function writePath(n) {}\napiWrite("api/notes", body, "Note");'
    assert re.findall(r'"api/[^"]+"', typed[typed.index("function writePath(") :])
    assert _requested_paths('apiWrite(somewhere, body); apiCall("GET", elsewhere);') == [
        "somewhere",
        "elsewhere",
    ]


def test_the_page_sends_only_the_socket_messages_the_server_reads() -> None:
    source = _text("app.js")
    sent = set(re.findall(r'wsSend\(\s*"([a-z_]+)"', source))
    table = re.findall(r'"([a-z_]+)"', _js_table("SOCKET_MESSAGES", r"\[", r"\]"))
    server_source = Path(remote_server.__file__).read_text(encoding="utf-8")
    server = set(re.findall(r'message\.get\("([a-z_]+)"', server_source))

    assert sent == set(table) == {"subscribe", "unsubscribe", "subscribe_fleet", "subscribe_board"}
    assert sent | {"project"} <= server, "a message the server's reader never looks at"
    assert len(re.findall(r"\.send\(", source)) == 1, "the socket is written by wsSend alone"


# --- 9. size budgets ------------------------------------------------------------------------

BUDGETS = {
    "index.html": 6 * 1024,
    "app.css": 16 * 1024,
    # SPEC §6.1 set 110 KB, and the page met it with 13 bytes to spare. The third review of
    # #243 found more for it to do: ask for a board only on its tab, keep keys in tap order,
    # answer a late reply in its own sheet. This is their room; the page stays under 150 KB.
    "app.js": 124 * 1024,
    "sw.js": 4 * 1024,
    "manifest.webmanifest": 1024,
}


def _shipped_size(path: Path) -> int:
    """``path``'s size as committed, which is what the wheel carries: LF line ends.

    git on windows-latest checks text out with CRLF (``core.autocrlf``), a byte more a
    line: app.js read 115 375 bytes there, for 112 627 committed in 2 748 lines (CI run
    37719330211). The wheel PyPI serves is built from the commit on ubuntu-latest
    (publish.yml). A file with a NUL in it is binary to git and checked out as it is, as
    the PNG is, whose signature holds a CR LF of its own.
    """
    data = path.read_bytes()
    return len(data) if b"\0" in data else len(data.replace(b"\r\n", b"\n"))


def test_each_file_and_the_whole_page_fit_their_budgets() -> None:
    for name, budget in BUDGETS.items():
        size = _shipped_size(WEB / name)
        assert size <= budget, f"{name} is {size} bytes, over {budget}"
    icons = sum(_shipped_size(WEB / name) for name in ("icon.svg", "icon-180.png"))
    assert icons <= 10 * 1024
    total = sum(_shipped_size(WEB / name) for name in PAGE_FILES)
    assert total <= 150 * 1024


def test_a_budget_counts_a_crlf_checkout_as_committed_and_a_binary_file_as_it_is(
    tmp_path: Path,
) -> None:
    lf, crlf = tmp_path / "lf.js", tmp_path / "crlf.js"
    lf.write_bytes(b"a();\nb();\n")
    crlf.write_bytes(b"a();\r\nb();\r\n")
    assert _shipped_size(crlf) == _shipped_size(lf) == 10
    png = WEB / "icon-180.png"
    assert b"\r\n" in png.read_bytes()
    assert _shipped_size(png) == png.stat().st_size


KEY_PX = 44
"""SPEC §6.4's touch target: no key of the pad is narrower."""
PHONE_ROW_PX = 360 - 2 * 12
"""The key pad's row on a 360 px phone: the input bar's width, less its 12 px each side."""
MONO_EM = 0.62
"""A monospace character's advance, per em, a little over the fonts the page names
(Menlo, SF Mono, Liberation Mono and DejaVu Sans Mono are 0.60 to 0.61)."""


def _css_value(css: str, selector: str, prop: str) -> str:
    rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert rule is not None, selector
    value = re.search(rf"(?:^|[;\s]){prop}:\s*([^;]+);", rule.group(1))
    assert value is not None, f"{selector} {{ {prop} }}"
    return value.group(1).strip()


def _css_px(css: str, selector: str, prop: str) -> list[int]:
    return [int(px) for px in re.findall(r"(\d+)px", _css_value(css, selector, prop))]


def _pad_labels(script: str, table: str) -> list[str]:
    found = re.search(rf"const {table} = \[(.*?)\];\n", script, re.S)
    assert found is not None, table
    return re.findall(r'\["([^"]+)", "[^"]+"\]', found.group(1))


def test_the_key_pad_fits_a_360_px_phone_and_every_label_its_key() -> None:
    """On a 390 px phone "Enter" (53 px) ran out of its 45 px key into the ↑ beside it, and
    on a 360 px one the row's seven 44 px keys and 6 px gaps (344 px) overran its 336 px:
    ↓ went to a row of its own. Headless Chromium measured both, and measures neither now."""
    script, css = _text("app.js"), _text("app.css")
    row, more = _pad_labels(script, "PAD_ROW"), _pad_labels(script, "PAD_MORE")
    (gap,) = _css_px(css, ".pad", "gap")
    (basis,) = _css_px(css, ".pad .key", "flex")
    _vertical, side = _css_px(css, ".pad .key", "padding")
    (font,) = _css_px(css, "button", "font-size")
    room = basis - 2 - 2 * side  # inside a 1 px border on each side

    assert len(row) == 7 and "Esc" in row
    assert basis >= KEY_PX
    assert len(row) * basis + (len(row) - 1) * gap <= PHONE_ROW_PX
    assert [label for label in row if len(label) * font * MONO_EM > room] == []
    wider = [label for label in more if len(label) * font * MONO_EM > room]
    assert "Space" in wider, "the control: More holds labels a 44 px key cannot"
    assert _css_value(css, ".pad .key", "min-width") == "max-content", "so theirs widen"


DOCS = Path(__file__).resolve().parents[1] / "docs" / "remote.md"


def test_the_pad_is_one_row_where_eight_keys_fit_and_the_docs_say_where_they_do_not() -> None:
    """SPEC §6.3 has the pad as one row, ``Esc 1 2 3 ⏎ ↑ ↓ More``, and so did the docs. Eight
    44 px keys and their gaps need 380 px, more than a 390 px phone's row: there More went
    to a line of its own, and at 320 px ↓ with it (r2 smoke of #243). More joins the row
    where the eight fit, as headless Chromium measured at 412 and 430 px, and takes the
    line under the seven where they do not; under 360 px, where not even seven fit, the
    eight are two rows of four. The docs say which phone gets which."""
    script, css = _text("app.js"), _text("app.css")
    row = _pad_labels(script, "PAD_ROW")
    (gap,) = _css_px(css, ".pad", "gap")
    (basis,) = _css_px(css, ".pad .key", "flex")

    eight = (len(row) + 1) * basis + len(row) * gap
    assert 390 - 2 * 12 < eight < 412 - 2 * 12, "More under on a 390 px phone, not on a 412"
    assert '"ghost key", "More"' in script, "More is a key of the row like the others"
    assert not re.search(r"\.more\b", css), "and no rule sends it under where the eight fit"
    narrow = re.search(
        r"@media \(max-width: 359px\) \{\s*\.pad > \.key \{ flex-basis: calc\(25% - (\d+)px\); \}",
        css,
    )
    assert narrow is not None, "under 360 px, four to a row"
    assert 4 * int(narrow.group(1)) == 3 * gap, "four keys and their three gaps fill the row"
    assert (320 - 2 * 12 - 3 * gap) / 4 >= KEY_PX
    prose = " ".join(DOCS.read_text("utf-8").split())
    wide = "On a phone wide enough for eight keys (412 px is, 390 px is not) the key pad is"
    described = re.search(re.escape(wide) + r" one row, `([^`]+)`", prose)
    assert described is not None and described.group(1).split() == [*row, "More"]
    assert "with the rest under More. On a narrower phone More takes the line under the" in prose
    assert "below 360 px the eight are two rows of four, More last" in prose


def test_the_status_strip_and_the_bottom_nav_keep_to_one_line_on_a_phone() -> None:
    """Read-only with an auto-off, the strip needs 400 px, and at 360 px Extend 1 h went to
    a second line; the Needs tab put its count under its label in a 90 px tab. The strip's
    name starts from nothing and takes what is left, so it gives way first and never pushes
    the rest down, and a tab has 2 px of side padding where the button's 14 px crowded its
    count out. Both still wrap where nothing else would fit: a strip that wrapped only under
    300 px ran Extend 12 px past the edge of a 300 px screen, and a nowrap tab ran its
    count over the next one at 280 px. Headless Chromium measured one line each from 325 px
    up, and nothing past an edge from 280 px."""
    css = _text("app.css")
    strip = re.search(r"\n\.top \{([^}]*)\}", css)
    assert strip is not None and re.search(r"(?:^|[;\s])flex-wrap:\s*wrap;", strip.group(1))
    assert not re.search(r"@media[^{]*\{\s*\.top \{", css), "it wraps at whatever width it must"
    assert _css_value(css, ".top > *", "flex") == "none"
    assert _css_value(css, ".top > *", "white-space") == "nowrap"
    assert _css_value(css, ".top .where", "flex") == "1 1 0", "never the reason for a wrap"
    assert _css_value(css, ".top .where", "min-width") == "0"
    assert _css_value(css, ".top .where", "text-overflow") == "ellipsis"
    tab = re.search(r"\n\.bottom \.tab \{([^}]*)\}", css)
    assert tab is not None and "nowrap" not in tab.group(1)
    assert _css_px(css, ".bottom .tab", "padding") == [8, 2]


def test_fit_width_sizes_the_pane_inside_the_screens_side_insets() -> None:
    """Fit width scaled the font to the screen's width less 48 px, but in landscape a phone's
    sides give up their safe-area insets too, 59 px a side on an iPhone 15 Pro: the pane ran
    104 px past its box, and overflow hid its rightmost columns, ten of eighty, with nothing to
    say so. Everything that pads the pane across the screen comes off the width it fits to."""
    css = _text("app.css")
    fit = _css_value(css, "pre.pane.fit", "font-size")
    main = _css_value(css, ".main", "padding")
    for side in ("left", "right"):
        assert f"env(safe-area-inset-{side})" in main, "the control: the insets pad the screen"
        assert re.search(rf"-\s*env\(safe-area-inset-{side}", fit), side
    fixed = re.search(r"100vw - (\d+)px", fit)
    assert fixed is not None
    (pad,) = _css_px(css, "pre.pane, pre.transcript", "padding")
    border = _css_px(css, "pre.pane, pre.transcript", "border")[0]
    sides = _css_px(css, ".main", "padding")[1::2]
    assert int(fixed.group(1)) >= sum(sides) + 2 * (pad + border)


# --- 10. the service worker -----------------------------------------------------------------

NGROK_SUFFIXES = (".ngrok-free.app", ".ngrok.app", ".ngrok.io", ".ngrok-free.dev", ".ngrok.dev")


def test_the_worker_shows_pushes_opens_cards_and_never_serves_from_a_cache() -> None:
    source = _text("sw.js")
    listeners = set(re.findall(r'addEventListener\(\s*"([a-z]+)"', source))

    assert {"push", "notificationclick"} <= listeners
    assert "fetch" not in listeners, "a fetch handler would let the page go stale"
    for suffix in NGROK_SUFFIXES:
        assert f'"{suffix}"' in source, suffix
    assert "postMessage" in source and "openWindow" in source


# --- 7 and 10. the pure core and safeUrl under node ----------------------------------------

ALLOWED_ELEMENTS = frozenset(
    {"div", "span", "pre", "p", "h2", "h3", "ul", "li", "button", "details", "summary"}
)
_RGB = re.compile(r"rgb\((\d{1,3}), (\d{1,3}), (\d{1,3})\)")
_BIDI = "؜‎‏‪‫‬‭‮⁦⁧⁨⁩"


def _node_report(harness: Path) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH; the page's runtime checks need it")
    # node writes UTF-8 to a pipe whatever the locale; Windows' cp1252 would garble it.
    result = subprocess.run(
        [node, str(harness)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report: dict[str, Any] = json.loads(result.stdout)
    return report


@pytest.fixture(scope="module")
def node_report() -> dict[str, Any]:
    return _node_report(HARNESS)


def _forbidden_writes(log: dict[str, Any]) -> list[str]:
    props = [p for p in log["props"] if p in {"href", "src", "action", "formAction"}]
    props += [p for p in log["props"] if p.startswith("on") or p in {"innerHTML", "outerHTML"}]
    attrs = [name for name, _value in log["attrs"] if name not in ALLOWED_ATTRIBUTES]
    styles = [
        f"{prop}={value}"
        for prop, value in log["styles"]
        if prop not in {"color", "backgroundColor"}
        or not (m := _RGB.fullmatch(value))
        or any(int(part) > 255 for part in m.groups())
    ]
    return props + attrs + styles


def test_hostile_input_renders_inert(node_report: dict[str, Any]) -> None:
    log = node_report["main"]
    text = "".join(log["text"])

    assert set(log["tags"]) <= ALLOWED_ELEMENTS, set(log["tags"]) - ALLOWED_ELEMENTS
    assert _forbidden_writes(log) == []
    assert {name for name, _value in log["attrs"]} <= ALLOWED_ATTRIBUTES
    assert set(log["listeners"]) == {"click"}
    assert "OSCPAYLOAD" not in text, "an OSC's payload reached the page"
    for control in ("\x1b", "\x07", "\x00", "\x7f", "\x9b", "\x9d", *_BIDI):
        assert control not in text, repr(control)
    # The hostile strings that are only text are shown, as text: nothing was dropped to pass.
    assert "javascript:alert(1)" in text
    assert "<img src=x onerror=alert(1)>" in text
    assert log["styles"], "no colour was written at all: the colour assertions saw nothing"


def test_the_recorder_sees_what_the_assertions_forbid(node_report: dict[str, Any]) -> None:
    """The control: one element made by hand with each forbidden thing, all of them caught."""
    control = node_report["control"]
    assert set(control["tags"]) - ALLOWED_ELEMENTS == {"a"}
    assert _forbidden_writes(control) == ["href", "onclick", "color=red"]


def test_colours_are_clamped_integers_and_the_cursor_cell_is_marked(
    node_report: dict[str, Any],
) -> None:
    assert node_report["clamped"] == [{"text": "X", "classes": [], "color": "rgb(255, 0, 255)"}]
    assert node_report["cursor"] == [
        {"text": "ab", "classes": []},
        {"text": "c", "classes": ["f1", "cur"]},
        {"text": "d", "classes": ["f1"]},
    ]


def test_a_card_says_once_what_its_detail_shows_in_full(node_report: dict[str, Any]) -> None:
    """Asked-you, interrupted and board cards said their text twice, as the excerpt and in
    the detail, and on a phone that doubled the card. An excerpt from the end of a long
    text stays, as the text's box may hold it below the fold; so does one the text lacks."""
    shown = node_report["excerpts"]
    assert shown["whole"] == shown["head"] == shown["shortTail"] == []
    assert shown["longTail"] == [
        "Should I cut the 0.8.0 release branch now, or wait until the remote-control PR lands?"
    ]
    assert shown["elsewhere"] == ["Which store?"]


def test_a_question_permission_or_plan_card_says_once_what_its_detail_leads_with(
    node_report: dict[str, Any],
) -> None:
    """The question card showed its question three times (the excerpt, the detail, the pane
    strip), the permission card's ``Bash(pytest -q …)`` repeated the tool and command below
    it, and a plan's excerpt was its first line: the server builds each excerpt from what
    the detail shows whole. An excerpt the detail does not lead with stays, and so does one
    with no detail to say it."""
    shown = node_report["builtExcerpts"]
    repeats = ["question", "questions", "cutQuestion", "permission", "heredoc", "longCommand"]
    repeats += ["path", "plan", "dialog"]
    assert {name: shown[name] for name in repeats} == {name: [] for name in repeats}
    assert shown["otherQuestion"] == ["Pick one before the release"]
    assert shown["otherCommand"] == ["Bash(rm -rf build)"]
    assert shown["bareTool"] == ["Bash(pytest -q tests/test_cache.py)"]


def test_a_permission_whose_input_was_too_large_to_send_keeps_the_excerpt_naming_the_call(
    node_report: dict[str, Any],
) -> None:
    """An input over 16 KiB, a Write of a whole file or a Bash heredoc, reaches the card as
    ``{"tool": "Write", "input": {}}``, while its excerpt is built from the whole call. The
    excerpt was hidden because it opens with the tool, and with it went the only line that
    named the file or the command: the card said ``tool: Write``, and a 1 or a 2 on the
    phone approved blind."""
    shown = node_report["builtExcerpts"]
    assert shown["droppedWrite"] == ["Write(/home/me/app/src/big_module.py)"]
    assert shown["droppedHeredoc"] == ["Bash(cat > schema.sql <<'EOF')"]


def test_the_page_names_a_tool_call_by_the_keys_the_server_summarises_it_by() -> None:
    """The page hides a permission's excerpt only when the detail holds the value the
    server built it from, so its keys must be the summariser's, in the summariser's order,
    and each one the card's detail carries."""
    keys = re.findall(r'"([^"]*)"', _js_table("SUMMARY_KEYS", r"\[", r"\]"))
    for n, key in enumerate(keys):
        call = {"name": "Tool", "input": {later: f"{later} value" for later in keys[n:]}}
        assert transcript._summarise_tool(call) == f"Tool({key} value)"
    others = {key: "value" for key in remote_needs._DETAIL_INPUT_KEYS if key not in keys}
    assert transcript._summarise_tool({"name": "Tool", "input": others}) == "Tool"
    assert set(keys) <= set(remote_needs._DETAIL_INPUT_KEYS)


def test_routes_are_built_only_from_ids_that_validate(node_report: dict[str, Any]) -> None:
    routes = node_report["routes"]
    assert routes["#/n/ny_0123456789abcdef/p/prj_x/a/coder-1"] == {
        "name": "card",
        "id": "ny_0123456789abcdef",
        "pid": "prj_x",
        "label": "coder-1",
    }
    assert routes["#/p/prj_x/a/coder-1/transcript"]["tab"] == "transcript"
    for hostile in (
        "#/n/javascript:alert(1)",
        "#/p/../fleet",
        "#/p/prj_x/a/%3Cimg%3E/live",
        "#/n/ny_0123456789ABCDEF",
        "#/p/prj_x/a/coder-1/evil",
        "#/%E0%A4%A",
        "#/settings/extra",
    ):
        assert routes[hostile] is None, hostile
    hashes = node_report["hashes"]
    assert hashes["card"] == "#/n/ny_0123456789abcdef/p/prj_x/a/coder-1"
    assert hashes["badCard"] == hashes["badAgent"] == hashes["nothing"] == "#/"
    assert hashes["badTab"] == "#/p/prj_x/fleet"


def test_safe_url_opens_only_this_origin_or_an_ngrok_page(node_report: dict[str, Any]) -> None:
    safe = node_report["safeUrl"]
    own = "https://own.example/r/t/#/n/ny_0123456789abcdef"
    assert safe[own] == own
    assert safe["https://x.ngrok-free.app/r/t/"] == "https://x.ngrok-free.app/r/t/"
    for refused in (
        "https://evil.example/r/t/",
        "http://x.ngrok-free.app/r/t/",
        "https://x.ngrok-free.app.evil.com/r/",
        "javascript:alert(1)",
        "https://u:p@x.ngrok.app/r/t/",
        "https://x.ngrok.io/no-token-path",
        "not a url",
    ):
        assert safe[refused] is None, refused
    assert tuple(node_report["suffixes"]) == NGROK_SUFFIXES


def test_every_notification_alerts_even_when_it_replaces_one_still_shown(
    node_report: dict[str, Any],
) -> None:
    """Every needs push carries the tag ``asq-needs``, and without ``renotify`` a push that
    replaces one still in the shade is shown in silence: the second prompt of a turn never
    rang on Android, desktop Chrome or Firefox."""
    notices = node_report["notices"]
    assert notices["needs"] == {
        "title": "api: coder-1 needs you",
        "options": {
            "body": "coder-1 asks you a question",
            "tag": "asq-needs",
            "renotify": True,
            "data": {"url": None},
        },
    }
    assert notices["bare"]["options"]["tag"] == "asq-needs", "renotify needs a tag"
    assert notices["bare"]["options"]["renotify"] is True


def test_the_python_reading_of_the_tables_is_what_the_script_holds(
    node_report: dict[str, Any],
) -> None:
    """The route and write tests parse the tables as text; node runs them. They agree."""
    assert node_report["api"] == _api_table()
    assert node_report["writes"] == _writes_table()
    assert {"ansiToRuns", "renderRuns", "renderNeedsCard"} <= set(node_report["exports"])


# --- the page booted in a fake browser (node) ----------------------------------------------


@pytest.fixture(scope="module")
def boot_report() -> dict[str, Any]:
    return _node_report(BOOT_HARNESS)


def test_a_page_opened_at_unlock_without_a_sign_in_draws_the_unlock_form(
    boot_report: dict[str, Any],
) -> None:
    """A reload at ``#/unlock`` (pull to refresh, a discarded tab) drew its route while the
    boot still asked who this is, and the 401 then found the page "already" at unlock: it
    said "Connecting to the machine…" for good. The bare link is the control."""
    reloaded = boot_report["reloadAtUnlock"]
    assert reloaded["form"], reloaded["main"]
    assert "Connecting" not in reloaded["main"]
    assert boot_report["bareLink"]["hash"] == "#/unlock" and boot_report["bareLink"]["form"]


def test_an_unlock_the_browser_did_not_keep_says_so_and_keeps_the_route(
    boot_report: dict[str, Any],
) -> None:
    """The passphrase was right but the next request is still signed out: the form said
    "Unlocking…" forever and forgot where the phone was going."""
    lost = boot_report["unlockNotKept"]
    assert lost["hash"] == "#/unlock"
    assert lost["form"] and lost["typed"] == "amber birch cedar delta"
    assert "did not keep the sign-in" in lost["said"]
    assert lost["remembered"] == "#/p/prj_x/board"
    assert boot_report["unlockKept"] == {"hash": "#/p/prj_x/board", "form": False}


def test_an_unlock_at_a_link_that_moved_says_so_instead_of_not_found(
    boot_report: dict[str, Any],
) -> None:
    """After ``regenerate-password --new-link``, or past auto-off, the machine answers the old
    link's unlock 404, and the page put the bare code ``not_found`` under the button: nothing
    said the link changed, or to open the new one. A wrong passphrase is the control."""
    moved = boot_report["unlockMoved"]
    assert not moved["form"]
    assert moved["heading"] == "Remote is off on the machine, or the link changed"
    assert "open the link the machine shows now" in moved["main"]
    assert "not_found" not in moved["main"]
    wrong = boot_report["unlockWrong"]
    assert wrong["form"] and wrong["said"] == "That is not the passphrase."


def test_a_write_whose_request_was_lost_goes_out_again_once_on_a_new_socket(
    boot_report: dict[str, Any],
) -> None:
    """SPEC §6.3: the retry follows the next reconnect, and a healthy socket never
    reconnects on its own, so Send stayed busy and nothing reached the machine."""
    lost = boot_report["lostWrite"]
    assert lost["waiting"]["sockets"] == 2 and lost["waiting"]["firstClosed"]
    assert lost["waiting"]["send"] == {"busy": True, "disabled": True}
    first, retry = lost["bodies"]
    assert first == retry and first["text"] == "hello"  # the same request_id: run at most once
    assert lost["send"] == {"busy": False, "disabled": False} and lost["pending"] == 0
    assert lost["typed"] == ""  # sent, so the box is cleared
    assert lost["offline"] is False and lost["bannerHidden"] is True


def test_a_retry_lost_too_is_not_confirmed_and_its_result_still_arrives(
    boot_report: dict[str, Any],
) -> None:
    lost = boot_report["lostTwice"]
    assert len(lost["bodies"]) == 2, "one retry, never more"
    assert lost["bodies"][0]["request_id"] == lost["bodies"][1]["request_id"]
    assert lost["said"].startswith("Not confirmed")
    assert "once it reconnects" not in lost["said"]  # no third attempt is coming
    assert lost["orphaned"] and lost["later"] == "Send: done"
    assert lost["send"] == {"busy": False, "disabled": False}


def test_a_write_lost_long_ago_is_not_sent_again_when_the_phone_is_back(
    boot_report: dict[str, Any],
) -> None:
    """The retry had no age limit: a pad "1" whose request never arrived went out at the
    next reconnect, an hour later, and approved whatever prompt the agent showed by then."""
    late = boot_report["lostKeyLongAgo"]
    assert [body["keys"] for body in late["bodies"]] == [["1"]], "never sent a second time"
    assert late["said"].startswith("Not sent again — the phone was away too long")
    assert late["orphaned"] and late["pending"] == 0, "its result still shows if it arrived"


def test_a_write_lost_before_the_phone_had_to_unlock_is_not_sent_again(
    boot_report: dict[str, Any],
) -> None:
    """After a 4401 the next unlock may be a new device, whose ledger knows none of the old
    ids: the retry would run a request the machine may already have run."""
    gone = boot_report["lostThenSignedOut"]
    assert gone["hash"] == "#/unlock"
    assert gone["said"].startswith("Not sent again — the phone was signed out")
    assert len(gone["bodies"]) == 1 and gone["pending"] == 0


def test_send_with_nothing_typed_presses_no_enter(boot_report: dict[str, Any]) -> None:
    """With ⏎ on, as it is by default, Send on an empty box posted ``{enter: true}``: a bare
    Enter into the pane, which picks a dialog's highlighted option ("1. Yes")."""
    empty = boot_report["emptySend"]
    assert empty["enterOn"], "the toggle is on, as the page starts it"
    assert empty["bodies"] == []
    assert empty["toast"] == "Type something first — Enter on its own is ⏎ on the key pad."


def test_the_keyboards_return_key_is_not_called_send_where_it_types_a_newline(
    boot_report: dict[str, Any],
) -> None:
    """The textarea takes several lines, so return is a newline there and Send is the button;
    an ``enterkeyhint`` of ``send`` labelled the key with what it does not do."""
    assert boot_report["emptySend"]["keyHint"] is None


def test_turning_notifications_on_replaces_a_subscription_made_with_another_key(
    boot_report: dict[str, Any],
) -> None:
    """A lost ``remote-push.json`` costs the machine its VAPID keys, and a subscription made
    against the old public key is refused at every push. Turn on re-posted it as it was,
    the machine took it, and every push failed again until the site's data was cleared."""
    changed = boot_report["pushKeyChanged"]
    assert changed["offered"]
    assert changed["log"] == ["unsubscribe old", f"subscribe {boot_report['keyNow']}"]
    assert changed["subscribed"] == ["https://fcm.googleapis.com/fcm/send/new"]
    kept = boot_report["pushKeyKept"]  # the control: the same key, the same subscription
    assert kept["log"] == [] and kept["subscribed"] == ["https://fcm.googleapis.com/fcm/send/old"]


def test_an_unlock_sends_a_subscription_made_anew_when_the_key_changed(
    boot_report: dict[str, Any],
) -> None:
    changed = boot_report["pushKeyChangedAtUnlock"]
    assert changed["log"] == ["unsubscribe old", f"subscribe {boot_report['keyNow']}"]
    assert changed["subscribed"] == ["https://fcm.googleapis.com/fcm/send/new"]
    kept = boot_report["pushKeyKeptAtUnlock"]
    assert kept == {"log": [], "subscribed": ["https://fcm.googleapis.com/fcm/send/old"]}


def test_a_feed_whose_scans_stopped_says_so_instead_of_passing_for_live(
    boot_report: dict[str, Any],
) -> None:
    """The heartbeat carries when the machine last looked for what needs you. A watcher that
    stopped (a tmux call hung inside a scan) left the feed frozen while the dot stayed green;
    the page read the heartbeat only as proof that the link was alive."""
    report = boot_report["scansStopped"]
    assert "the machine has stopped checking" in report["stopped"]["said"]
    assert report["stopped"]["greyed"] == 1
    assert "stopped checking" not in report["again"]["said"]
    assert report["again"]["greyed"] == 0


def test_a_frame_clears_the_offline_banner_a_lost_read_raised(
    boot_report: dict[str, Any],
) -> None:
    lost = boot_report["lostRead"]
    assert lost["lost"]["offline"] is True and lost["lost"]["banner"]
    assert lost["offline"] is False and lost["bannerHidden"] is True


def test_only_a_phone_the_browser_calls_offline_is_told_to_get_back_online(
    boot_report: dict[str, Any],
) -> None:
    """With serve stopped, or ngrok down, the phone is online and the machine is the one
    away; the banner said "Offline — the page reconnects once the phone is back online"
    all the same, and a lost read said to try again then."""
    lost = boot_report["lostRead"]
    assert lost["lost"]["banner"] == (
        "The machine is not answering — the page reconnects as soon as it does."
    )
    assert "Could not reach the machine — try again in a moment." in lost["lost"]["said"]
    assert lost["phone"] == "Offline — the page reconnects once the phone is back online."


def test_a_quick_answer_holds_its_row_until_the_machine_answers(
    boot_report: dict[str, Any],
) -> None:
    """A second tap went out under a second request_id, and the server's re-check
    answered it "no longer needs you" because the first one had worked."""
    taps = boot_report["quickAnswerTwice"]
    assert taps["inFlight"] == [True, True]
    assert taps["sentWhileHeld"] == 1
    assert taps["after"] == [False, False]
    assert taps["toast"] == "Sent 1. A to coder-1"


def test_sign_out_stays_available_while_the_page_is_stale(boot_report: dict[str, Any]) -> None:
    """SPEC §6.3: sign out is a plain DELETE that needs no socket. Revoke is the control."""
    assert boot_report["staleDevices"] == {"stale": True, "signOut": False, "revoke": True}


def test_a_tell_that_was_not_typed_in_says_what_happened_instead(
    boot_report: dict[str, Any],
) -> None:
    """Text pasted at the prompt that Enter never sent holds the agent's next question back;
    the page said "left a note" instead of the machine's own sentence."""
    told = boot_report["tellNotSent"]
    assert told["told"] == ["interrupt"]
    assert told["toast"] == f"coder-1: {told['how']}"


def test_the_live_pane_draws_no_cursor_where_the_program_hid_it(
    boot_report: dict[str, Any],
) -> None:
    """Claude Code hides the terminal's cursor, and the pane drew it anyway: a stray
    inverted cell after its dialog, where the hidden cursor rests. A frame that does not
    say (an older machine) still draws it."""
    assert boot_report["paneCursor"] == {"shown": 1, "hidden": 0, "unsaid": 1}


def test_a_stop_refused_at_a_prompt_is_explained_in_the_pages_own_words(
    boot_report: dict[str, Any],
) -> None:
    """The sheet showed the machine's sentence, which is written for curl: "send
    dismiss_dialog: true to press Esc (No) first" means nothing on a phone, where the way
    to say that is the button the sheet offers next."""
    stop = boot_report["stopAtAPrompt"]
    assert stop["said"] == (
        "coder-1 may be showing a prompt that stopping it now would answer. "
        "Press Esc (No) first to dismiss it."
    )
    assert stop["dismissed"] == [False, True]
    assert stop["toast"] == "Stopped coder-1"


def test_a_write_refused_read_only_shuts_every_write_button_at_once(
    boot_report: dict[str, Any],
) -> None:
    """After a 403 ``read_only`` the pad and Send stayed live, and the READ-ONLY pill hidden,
    until a ``remote`` frame or a reconnect said what the machine had just said itself."""
    refused = boot_report["refusedReadOnly"]
    assert refused["before"] == {"send": {"busy": False, "disabled": False}, "pill": False}
    assert refused["send"] == {"busy": False, "disabled": True}
    assert refused["keys"] and all(refused["keys"]), refused["keys"]
    assert refused["pill"] is True and refused["readOnly"] is True
    assert "can watch but not act" in refused["sheet"]


def test_every_key_of_the_pad_has_a_name_a_screen_reader_can_say(
    boot_report: dict[str, Any],
) -> None:
    """A glyph key (⏎ ↑ ↓ ← → ⌫ ⇧Tab) had no accessible name, so a screen reader read the
    symbol, or nothing, never the Enter or the arrow it sends; nor did ``^C`` or the ⏎
    toggle beside Send. A key whose label is a word or a digit is its own name."""
    names = boot_report["keyNames"]
    spoken = dict(names["keys"])
    assert len(spoken) == len(names["keys"]) > 40, "every key, each label once"
    assert (spoken["⏎"], spoken["↑"], spoken["↓"]) == ("Enter", "Up arrow", "Down arrow")
    assert (spoken["←"], spoken["→"], spoken["⌫"]) == ("Left arrow", "Right arrow", "Backspace")
    assert (spoken["⇧Tab"], spoken["^C"], spoken["PgUp"]) == ("Shift Tab", "Control C", "Page up")
    unnamed = [label for label, name in spoken.items() if name is None]
    assert [label for label in unnamed if not re.fullmatch(r"[A-Za-z0-9]+", label)] == []
    assert {"1", "Space", "More", "F12"} <= set(unnamed), "words and digits say themselves"
    assert names["enterToggle"] == "Press Enter after the text"


def test_the_live_tab_opens_at_the_foot_of_the_pane_where_a_prompt_waits(
    boot_report: dict[str, Any],
) -> None:
    """The Live tab opened at the top of a 40-row pane, and the prompt that needed the human
    sat at its foot, below the fold and half under the input bar. Only the first screen
    moves the scroll: a later frame leaves it where the human put it to read. The key pad,
    opened at the foot, grew the bar back over the options it is there to answer: the view
    stays at the foot, and one scrolled up to read stays where it is."""
    assert boot_report["liveScroll"] == {"unread": 0, "first": 2400, "later": 300}
    assert boot_report["padScroll"] == {"atFoot": 2400, "reading": 300, "open": True}


def test_the_page_asks_for_a_board_only_while_its_board_tab_shows(
    boot_report: dict[str, Any],
) -> None:
    """r3 #6: every socket was streamed a project's whole board, every session and task,
    again with every session's heartbeat, though only the Board tab draws it: the page
    asked for one wherever a project or an agent was open. Leaving the tab stops it."""
    steps = boot_report["boardOnItsTab"]
    assert steps["feed"] == [] and steps["fleet"] == []
    assert steps["board"] == ["prj_x"]
    assert steps["agent"] == ["prj_x", False], "leaving the tab says so"
    assert steps["sockets"] == 2 and steps["woken"] == ["prj_x"], "a new socket asks again"


def test_a_board_tab_opened_again_reads_its_board_anew_and_never_shows_the_last_one(
    boot_report: dict[str, Any],
) -> None:
    """No board frame comes while the tab is not asked for, so a board the page kept from its
    last visit is as old as that visit: drawn again, it showed the board as it was then as
    if it were now, notes posted since missing until a frame came, and the tab made no read.
    It says Loading… until its read answers."""
    board = boot_report["boardReopened"]
    assert board["first"] == ["from before"]
    assert board["reopened"] == {"shown": ["Loading…"], "reads": 2}
    assert board["answered"] == ["since", "from before"]


def test_a_transcript_tells_each_turns_time_by_the_phones_own_clock(
    boot_report: dict[str, Any],
) -> None:
    """r3 #9: the machine wrote its own clock's HH:MM into the speaker's line, so a phone in
    UTC-7 read ``> you 17:05`` for 10:05, beside a page whose other times are the phone's.
    The machine sends when, and the page tells it on the turn's first line."""
    lines = boot_report["transcriptTimes"]
    assert lines[0].startswith("> you ") and "10:05" in lines[0] and "17:05" not in lines[0]
    assert lines[3].startswith("* claude ") and "10:06" in lines[3]
    assert (lines[1], lines[4]) == ("  commit it", "  done")


def test_a_read_answered_after_a_newer_frame_of_its_kind_is_dropped(
    boot_report: dict[str, Any],
) -> None:
    """A wake reads the feed and reconnects at once, and a read answered after the new
    socket's frame put back what was there before it: the card the frame brought was gone,
    and the socket, which sends the feed only when it changes, never sent it again. So it
    went for the write switch on a wake, and for the Board and Fleet tabs' first reads, a
    failed one included, which blanked the fleet. A read with no frame before it is drawn."""
    reads = boot_report["readsAfterFrames"]
    assert reads["wake"] == {"cards": 1, "writable": True}
    assert (reads["board"], reads["fleet"], reads["fleetFailed"]) == (2, 2, 2)
    assert reads["boardAlone"] == 1


def test_back_leaves_the_page_once_a_redirect_took_the_place_of_the_screen_it_left(
    boot_report: dict[str, Any],
) -> None:
    """Each redirect pushed an entry: Back from the feed went to ``#/unlock``, which sent the
    unlocked page on to the feed again, so Back never left the tab or the installed app. A
    gone agent's tab did the same: its 404 sent the page to the fleet, and Back to the tab
    asked again. Signed out, Back went to the screen the lock had left, and back to the lock.
    And a tab left at ``#/unlock`` and opened again once signed in (another tab unlocked, or
    a reload) is sent to the feed in that entry's place, or Back went to it and bounced."""
    report = boot_report["backLeaves"]
    assert report["afterUnlock"] == {"at": "#/", "landed": [], "left": True}
    assert report["afterGone"] == {
        "at": "#/p/prj_x/fleet",
        "landed": ["#/p/prj_x/a/coder-1/live", "#/p/prj_x/fleet"],
        "left": True,
    }
    assert report["afterSignedOut"] == {"at": "#/unlock", "landed": ["#/unlock"], "left": True}
    assert report["unlockedAtUnlock"] == {"at": "#/", "landed": [], "left": True}


def test_an_answer_that_comes_after_the_human_moved_on_acts_on_its_own_sheet_only(
    boot_report: dict[str, Any],
) -> None:
    """There is one sheet, and a write's answer acted on whatever was open by then. A second
    ^C to coder-1 refused double_press opened an agent-less "Send it again?" over coder-2's
    own Ctrl-C sheet, its Send and exit where coder-2's button was. A restart answered after
    Back closed the Tell sheet opened since, and what was typed in it; failed, it said why in
    its own sheet, off the screen, so nothing was said at all. With that Tell sent and still
    out, the restart's answer took the busy mark off the Tell's sheet, and Escape or a tap
    beside it closed the sheet while it waited. And a Tell, a Reply or a restart answered
    once another sheet was open, done or stale, closed that one, with what was typed in it.
    The double_press sheet waits for coder-1's own screen with no other sheet on it; a Stop
    answered dialog_open once its sheet is gone says why, but not to press a button that is
    gone with it."""
    late = boot_report["lateAnswers"]
    not_sent = "coder-1: the second Ctrl-C was not sent — it would exit Claude Code."
    assert late["doublePress"] == {"sheet": "Send Ctrl-C?", "toast": not_sent, "exits": 0}
    assert late["doubleUnderASheet"] == {"sheet": "Act on coder-1", "toast": not_sent}
    assert late["doubleElsewhere"] == {"sheet": None, "toast": not_sent}
    assert late["promptAfterBack"] == {
        "sheet": None,
        "toast": "Stop coder-1: coder-1 may be showing a prompt that stopping it now would answer.",
    }
    told = {"sheet": "Tell coder-1", "typed": "carry on"}
    assert late["restartDone"] == {**told, "toast": "Restarted coder-1 on its own conversation"}
    assert late["restartFailed"] == {
        **told,
        "toast": "Restart coder-1: The machine could not answer — try again in a moment.",
    }
    assert late["restartStale"] == {
        **told,
        "toast": "coder-1 changed since this screen loaded — look again, then retry.",
    }
    assert late["staleUnderATell"] == {"sheet": "Tell coder-2", "typed": "not yet", "told": 1}
    assert late["restartUnderATell"] == {
        "waiting": {"busy": True, "close": True, "sheet": "Tell coder-1"},
        "told": {"sheet": "Stop coder-1", "toast": "Typed into coder-1"},
    }
    assert late["replyUnderAReply"] == {
        "typed": "8080",
        "toast": "Posted on the board",
        "dismissed": ["ny_0123456789abcdef"],
        "at": "#/n/ny_00000000000000b2",
    }


def test_a_late_refusal_neither_moves_the_page_nor_covers_a_sheet_opened_since(
    boot_report: dict[str, Any],
) -> None:
    """So it went for what a refusal does besides its sentence: a Dismiss answered once
    another card was open sent the page to the feed, a read_only answered once a Tell sheet
    was open put the read-only sheet in its place, and a gone agent sent the page to its
    fleet from another agent's screen: a transcript read, a pad key, a Tell. On the gone
    agent's own screen it still goes, in that screen's place, so Back then leaves; and a
    Tell refused read_only still puts the read-only sheet in its own sheet's place."""
    elsewhere = boot_report["lateAnswers"]["elsewhere"]
    assert elsewhere["dismissedAt"] == "#/n/ny_fedcba9876543210"
    assert elsewhere["readOnly"] == {
        "sheet": "Tell coder-1",
        "typed": "wait for me",
        "writable": False,
    }
    assert elsewhere["readOnlyOwn"] == "Read-only"
    assert elsewhere["goneAt"] == "#/p/prj_x/a/coder-2/live"
    for gone in (elsewhere["goneKey"], elsewhere["goneTell"]):
        assert gone["elsewhere"] == {"at": "#/p/prj_x/a/coder-2/live", "left": False}
        assert gone["own"] == {"at": "#/p/prj_x/fleet", "left": True}


def test_a_sheet_opened_before_the_fleet_came_finds_its_agent_when_tapped(
    boot_report: dict[str, Any],
) -> None:
    """Stop, Restart and Switch need the agent's id, and a sheet opened before the fleet
    frame kept the null it opened with: every tap after the fleet came said "try again in a
    second" again, and nothing was sent until the sheet was closed and opened anew. A fleet
    without the agent said the same, though no second would help; and a Tell opened that
    early went out without the id that keeps it off a replacement agent."""
    early = boot_report["sheetBeforeFleet"]
    assert early["waiting"].startswith("Waiting for the fleet to say which coder-1 this is")
    assert early["stopped"] == ["agt_1"] and early["toast"] == "Stopped coder-1"
    assert early["absent"] == "coder-1 is not in this project's fleet any more."
    assert early["restarts"] == 0
    assert early["told"] == ["agt_1"]


def test_keys_reach_an_agent_one_at_a_time_in_the_order_they_were_tapped(
    boot_report: dict[str, Any],
) -> None:
    """Each pad key was its own request, sent without waiting for the one before, and the
    machine could type them in any order: ↓ ↓ ⏎ on a plan dialog could be ⏎ first, which
    accepts option 1 where No was meant. Lost keys were resent together on a reconnect, and
    a key tapped behind a lost one went first. Now a key goes once the one before it was
    answered, and not at all behind one that did not go through; a card's quick answer is
    keys too, and a key tapped while it is typed waits for it."""
    keys = boot_report["keysInOrder"]
    assert keys["quick"] == {"atOnce": 1, "order": ["Down", "Down", "Enter"]}
    assert keys["refused"]["sent"] == 1 and keys["refused"]["toast"].startswith("Not sent — ")
    assert keys["refused"]["after"] == ["Down", "Enter"], "a key tapped after the refusal goes"
    assert keys["lost"] == ["Down", "Down", "Enter"]
    assert keys["afterAnswer"] == {"whileAnswering": 0, "after": 1}


def test_the_pads_exit_and_rewind_guards_each_ask_before_a_key_goes(
    boot_report: dict[str, Any],
) -> None:
    """Only the page asks before ^C or ^D (one interrupts the agent, a second within 3 s exits
    Claude Code) and before a second Esc within 1.5 s (two open its Rewind selector): the
    machine lets the first of each through. Nothing tested any of them, nor the resend of a
    second ^C the machine refused. Each step: the key, the sheet it left, the keys sent."""
    pad = boot_report["padConfirms"]
    assert pad["steps"] == [
        ["^C", "Send Ctrl-C?", 0],
        ["Send Ctrl-C", None, 1],
        ["^D", "Send Ctrl-D?", 1],
        ["Close", None, 1],
        ["^C", "Send Ctrl-C?", 1],
        ["Send Ctrl-C", "Send Ctrl-C to coder-1 again?", 2],
        ["Send and exit", None, 3],
        ["Esc", None, 4],
        ["Esc", None, 5],  # two seconds after the last
        ["Esc", "Press Esc again?", 5],
        ["Send Esc", None, 6],
    ]
    escapes = [["Escape"]] * 3
    assert pad["keys"] == [["C-c"], ["C-c"], ["C-c", "confirm_exit"], *escapes]


def test_a_page_that_slept_holds_its_keys_until_the_machine_says_what_is_on_screen(
    boot_report: dict[str, Any],
) -> None:
    """A wake's new socket counted as an update the moment it opened, before any frame: the
    next second's check found the page fresh and let the pad and Send act on the screen from
    before the sleep, for as long as the first frame took. And the pane comes a tick after the
    other frames, so even a quick wake left a second of that: the Live tab's keys now wait
    for its pane from the socket open now. Each step is [stale, Send disabled]."""
    assert boot_report["staleAcrossAWake"] == [
        [False, False],  # the pane is in
        [True, True],  # a minute with nothing heard
        [True, True],  # a wake's socket opened, and the next second's check ran
        [False, True],  # its first frame came, not the pane
        [False, False],  # the pane came
    ]


def test_the_transcript_asks_for_lines_as_wide_as_fit_inside_its_padding(
    boot_report: dict[str, Any],
) -> None:
    """The page measured its columns as the box's clientWidth less 8 px, but the box has 8 px
    of padding a side, which clientWidth counts: it asked the machine to wrap a column or two
    wider than fit, and every full line wrapped again on the phone (45 asked where 44 fit on a
    360 px phone, 52 where 51 fit on a 412). A line of the width asked fits now, and a line
    one column longer would not."""
    report = boot_report["transcriptColumns"]
    for width, columns in report["asked"].items():
        inside = int(width) - 2 * report["padding"]
        assert columns * report["charPx"] <= inside, (width, columns)
        assert (columns + 1) * report["charPx"] > inside - 0.5, (width, columns)
    assert report["asked"] == {"334": 44, "364": 48, "386": 51}


def test_the_transcript_draws_one_read_at_a_time_and_the_newest_wins(
    boot_report: dict[str, Any],
) -> None:
    """Load older had nothing to wait on: a double tap asked for the same page twice and put
    it in twice. A Refresh answered while Load older was out drew the newest page, then the
    late older page went on top of it with the turns between them gone, and its "no more"
    hid Load older, so the gap could never be filled."""
    loads = boot_report["transcriptLoads"]
    assert loads["twice"] == {
        "asked": [None, "100"],
        "shown": ["t1", "t2", "t3", "t4"],
        "older": False,
    }
    assert loads["spliced"] == {"asked": [None, "100", None], "shown": ["t5", "t6"], "older": True}


def test_extend_and_revoke_wait_for_their_answer_before_another_tap_goes(
    boot_report: dict[str, Any],
) -> None:
    """Extend 1 h stayed live while its request ran, as no other write button does, and a
    second tap went out under a second request_id: Remote stayed on the internet an hour
    longer than asked, and the phone has no way to take it back. Revoke did the same, and
    the second answer, "no such device", read as if the first had failed."""
    taps = boot_report["buttonsInFlight"]
    assert taps["extend"] == {"sent": 1, "waited": True, "after": False}
    assert taps["revoke"] == {"sent": ["api/devices/dev_4e5f6a7b"], "waited": True}


def test_the_feed_keeps_to_six_pane_strips_as_prompts_come_in_above_the_rest(
    boot_report: dict[str, Any],
) -> None:
    """Only a card built anew counted against the six strips, and a kept card held its own:
    three permission prompts ranked above six plan cards made nine, past the socket's eight
    panes, and the ninth was refused with a toast on every reconnect. The first six cards
    that show a strip get one, and a card that loses its strip lets go first."""
    cap = boot_report["stripCap"]
    assert cap["most"] == 6
    assert cap["held"] == ["coder-7", "coder-8", "coder-9", "planner-1", "planner-2", "planner-3"]


def test_a_screen_reader_is_told_which_tab_is_open_and_that_a_sheet_is_a_modal_dialog(
    boot_report: dict[str, Any],
) -> None:
    """Tabs and the bottom nav marked the current one with a class, so a screen reader heard
    none of them selected. A sheet was an unnamed dialog that left focus on the button behind
    it, with the page under it still in the reading order and Tab moving through it; Escape
    did nothing. Each sheet is named by its heading now, modal, the page behind it inert,
    focus in it, and back on what opened it once Escape closes it."""
    said = boot_report["spoken"]
    assert said["dots"] == ["Live", "Stale: nothing heard for 25 s"], "the dot said it by colour"
    assert said["tabs"] == [["Live", "true"], ["Transcript", "false"], ["Card", "false"]]
    assert said["nav"] == ["false", "page", "false", "false"]
    assert said["opened"] == {
        "dialog": ["dialog", "true", "sheet-title"],
        "named": "Act on coder-1",
        "behind": [True] * 4,
        "focusIn": True,
    }
    assert said["replaced"] == {"named": "Stop coder-1", "behind": [True] * 4, "focusIn": True}
    assert said["escaped"] == {"open": False, "behind": [False] * 4, "focusBack": True}
    assert said["toggles"] == [["false", "false"], ["true", "true"]]


def test_each_write_reaches_the_route_that_answers_it(boot_report: dict[str, Any]) -> None:
    """No scenario sent the board's Post, a Reply, a Restart or a Switch: their paths could
    change, or be typed wrong, with every test green. Each, as the machine received it."""
    sent = boot_report["writesReachTheirRoutes"]
    assert sent["board"] == ["POST api/note"]
    assert sent["reply"] == ["POST api/note", "POST api/needs/dismiss"]
    assert sent["agent"] == ["POST api/agent/restart", "POST api/agent/switch"]


def test_the_page_reconnects_and_reads_again_when_the_phone_wakes(
    boot_report: dict[str, Any],
) -> None:
    """SPEC §6.4, and the docs' "Waking the phone reconnects at once", had no test: deleting
    the 4409 branch (two tabs then take the socket from each other for ever), any of the
    three wake listeners, the reads a wake makes, or a message by which a new socket asks
    for what the screen shows, left every test green."""
    wakes = boot_report["wakes"]
    assert wakes["replaced"] == {
        "sockets": 1,
        "state": "replaced",
        "banner": "Another tab of this phone took over the live view.Reconnect here",
        "timers": [],
    }
    assert wakes["hiddenShow"] == 1, "pageshow on a hidden tab takes no socket back"
    reread = ["api/actions/recent", "api/needs", "api/remote"]
    assert wakes["shown"] == {"sockets": 2, "reads": reread}
    for event in ("visibilitychange", "pageshow", "online"):
        assert wakes["each"][event] == {"oldClosed": True, "sockets": 2, "reads": reread}, event
    board = ["subscribe_fleet prj_x", "subscribe_board prj_x"]
    assert wakes["asks"]["wake"] == {"sockets": 2, "sent": board}
    pane = ["subscribe_fleet prj_x", "subscribe coder-1"]
    assert wakes["asks"]["drop"] == {"sockets": 2, "sent": pane}


def test_a_card_is_dismissed_by_hand_and_after_a_tell_only_once_it_was_typed_in(
    boot_report: dict[str, Any],
) -> None:
    """Nothing tested Dismiss, nor the dismissal after a Tell or a Reply: breaking any of
    them, or dismissing a card after a Tell the machine only left as a note (SPEC §6.3:
    "only when the response says delivered: true"), left every test green."""
    gone = boot_report["dismissals"]
    card = [{"id": "ny_0123456789abcdef"}]
    assert gone["byHand"] == {"sent": card, "cards": 0}
    assert gone["gone"] == {"sent": card, "cards": 0}, "a 404 is a card already gone"
    told = ["ny_0123456789abcdef"]
    assert gone["delivered"] == {"sent": card, "cards": 0, "told": told}
    assert gone["notDelivered"] == {"sent": [], "cards": 1, "told": told}
    assert gone["reply"] == {"sent": [{"id": "ny_00000000000000b2"}], "cards": 0}


def test_an_answer_after_its_screen_was_left_neither_lands_on_the_next_nor_goes_unsaid(
    boot_report: dict[str, Any],
) -> None:
    """Three more of the kinds fixed above, each without a test of its own: a note posted
    from the Board tab said "Posted." into a composer no longer on screen once the tab was
    left, so its result went unseen; a transcript read answered after the Live tab opened
    scrolled that tab to its foot; and a hash typed by hand that is no route went to the feed
    with a push, so Back went to it and was sent on again, for ever."""
    after = boot_report["afterLeaving"]
    assert after["note"] == "Note: Posted."
    assert after["scrolled"] == 0
    assert after["back"] == {"landed": ["#/"], "left": True}


# --- 11. the wheel --------------------------------------------------------------------------


def test_the_wheel_carries_the_bundled_page(tmp_path: Path) -> None:
    """An editable install reads the page from the tree whether or not a wheel would ship
    it, so only a real build proves ``pip install aisquare-cli`` serves a page. Built with
    hatchling, the project's own backend, once it is a dev dependency (#240 adds it)."""
    import zipfile

    wheel = pytest.importorskip("hatchling.builders.wheel")
    root = Path(__file__).resolve().parents[1]
    builder = wheel.WheelBuilder(str(root))
    wheels = list(builder.build(directory=str(tmp_path), versions=["standard"]))

    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
    assert "aisquare/web/__init__.py" in names
    for name in (*PAGE_FILES, "__init__.py"):
        assert f"aisquare/web/remote/{name}" in names, name
