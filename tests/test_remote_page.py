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
import os
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


def test_an_installed_build_is_typed_by_a_closed_list_never_the_machines_tables(
    runtime: Runtime, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed build was served by ``FileResponse``, typed by ``mimetypes``, which reads
    the machine's own tables: on Windows the registry, where ``.js`` can be ``text/plain``.
    Served so beside ``nosniff``, every script of the page was refused and it never booted.
    Here the machine's tables say what such a registry says, as CPython reads it on Windows.
    A type the closed list does not hold comes from Python's own table, not the machine's."""
    import mimetypes

    tables = mimetypes.MimeTypes()
    for suffix in (".js", ".css", ".pdf"):
        tables.add_type("text/plain", suffix)
    monkeypatch.setattr(mimetypes, "_db", tables)
    assert mimetypes.guess_type("app.js")[0] == "text/plain", "the control: the tables say so"
    names = ["index-DfFvQnFu.js", "legacy.js", "chunk.mjs", "app.css", "inter.woff2", "data.json"]
    names.append("guide.pdf")
    for name in [*names, "notes.xyz"]:
        (built / "assets" / name).write_text("x")
    remote_server.install_page(built)
    client = make_client(build_app(runtime, sources=_sources()))

    typed = {
        name: client.get(f"{base(runtime)}/assets/{name}").headers["content-type"]
        for name in [*names, "notes.xyz"]
    }
    index = client.get(f"{base(runtime)}/")

    assert typed == {
        "index-DfFvQnFu.js": "text/javascript; charset=utf-8",
        "legacy.js": "text/javascript; charset=utf-8",
        "chunk.mjs": "text/javascript; charset=utf-8",
        "app.css": "text/css; charset=utf-8",
        "inter.woff2": "font/woff2",
        "data.json": "application/json",
        "guide.pdf": "application/pdf",
        "notes.xyz": "application/octet-stream",
    }
    assert index.headers["content-type"] == "text/html; charset=utf-8"
    assert index.headers["x-content-type-options"] == "nosniff"


def test_the_page_is_decided_and_read_off_the_event_loop(
    runtime: Runtime, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which page answers asks the disk (the installed index, a file's path resolved), and
    the first answer reads the bundled page's files: all of it ran in the request's coroutine,
    on the event loop that serves every request and socket. The bundled page, then an
    installed build, each answered from a worker thread."""
    import asyncio

    ran: list[tuple[str, bool]] = []

    def watched(name: str, real: Any) -> Any:
        def call(*args: Any) -> Any:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                ran.append((name, False))
            else:
                ran.append((name, True))
            return real(*args)

        return call

    for name in ("bundled_page_response", "build_content_type"):
        monkeypatch.setattr(remote_page, name, watched(name, getattr(remote_page, name)))
    client = make_client(build_app(runtime, sources=_sources()))

    assert client.get(f"{base(runtime)}/app.js").status_code == 200
    remote_server.install_page(built)
    assert client.get(f"{base(runtime)}/assets/app.js").status_code == 200

    assert ran == [("bundled_page_response", False), ("build_content_type", False)]


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
    # A sheet's own history entry is this URL again (no URL argument), and its Back that one.
    calls = re.findall(r"\bhistory\s*\.\s*(\w+)\s*\(([^)]*)\)", _text("app.js"))
    assert calls == [("back", ""), ("pushState", '{ sheet: true }, ""')]


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
    # answer a late reply in its own sheet. The fourth, and a sweep of the page in a real
    # browser, found more again. This is their room; the page stays under 150 KB.
    "app.js": 129 * 1024,
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


def test_every_box_a_sentence_lands_in_breaks_a_word_too_long_for_its_line() -> None:
    """A question card's question and options set no overflow-wrap: an absolute path in
    either ran past the card and pushed the Needs screen sideways, 584 px wide on a 390 px
    phone with the question cut at the screen's edge, as headless Chromium measured. So did
    a quick answer's button naming one, a refusal's sentence naming one in a toast, a
    sheet's status, or a screen's empty line, and the sheet's own lead: a whole card wraps
    such a word where it must now, and so does every other box a machine's sentence or a
    typed one lands in, as Chromium measured at 360 px."""
    css = _text("app.css")
    for selector in (".card", ".sheet", ".toast", ".empty", ".status", ".muted", ".title"):
        assert _css_value(css, selector, "overflow-wrap") == "anywhere", selector
    script = _text("app.js")
    assert 'mk(doc, "div", "card " + look[1])' in script, "a card is one box, its detail in it"
    for drawn in ('el("p", "empty", failText(res))', 'el("p", "status")', 'el("div", "toast")'):
        assert drawn in script, drawn


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


def _themes(css: str) -> dict[str, dict[str, str]]:
    """Each theme's colour tokens: the dark one is ``:root``, the light one its media block."""
    dark = re.search(r"\n:root \{([^}]*)\}", css)
    light = re.search(r"prefers-color-scheme: light\) \{\s*:root \{([^}]*)\}", css)
    assert dark is not None and light is not None
    tokens = dict(re.findall(r"(--[a-z0-9-]+): (#[0-9a-f]{6})", dark.group(1)))
    return {
        "dark": tokens,
        "light": {**tokens, **dict(re.findall(r"(--[a-z0-9-]+): (#[0-9a-f]{6})", light.group(1)))},
    }


def _contrast(ink: str, ground: str) -> float:
    """WCAG 2's contrast ratio of two ``#rrggbb`` colours."""

    def luminance(colour: str) -> float:
        rgb = [int(colour[n : n + 2], 16) / 255 for n in (1, 3, 5)]
        r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted((luminance(ink), luminance(ground)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_every_text_colour_reads_at_aa_contrast_in_both_themes() -> None:
    """WCAG AA asks 4.5:1 of text this size. The filled badges were white on the dark
    theme's --alarm and --ask, 2.5:1 and 2.2:1: Permission, Question, Plan, NEEDS YOU and the
    Needs count, the page's most urgent words, were its hardest to read on the theme it
    starts in. The light theme's accent (the primary button, a link, a waiting agent) and
    warn (READ-ONLY, the auto-off, a usage limit) read at 3.3 to 4.2:1. Each ink the
    stylesheet writes with, on each ground it is drawn on, in both themes."""
    css = _text("app.css")
    fills = (".k-urgent, .s-attention", ".k-ask", ".count:not(:empty)", "button.primary", ".toast")
    inks = ("--fg", "--muted", "--accent", "--warn", "--alarm", "--ok")
    for name, theme in _themes(css).items():

        def colour(value: str, theme: dict[str, str] = theme) -> str:
            token = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value)
            return theme[token.group(1)] if token else value

        for rule in fills:
            ink = colour(_css_value(css, rule, "color"))
            ground = colour(_css_value(css, rule, "background"))
            assert _contrast(ink, ground) >= 4.5, (name, rule, ink, ground)
        for ink in inks:
            for ground in ("--bg", "--panel", "--raise"):
                assert _contrast(theme[ink], theme[ground]) >= 4.5, (name, ink, ground)
        for ink in ("--pane-fg", "--pane-muted"):
            assert _contrast(theme[ink], theme["--pane"]) >= 4.5, (name, ink)
    assert _contrast("#ffffff", _themes(css)["dark"]["--alarm"]) < 3, "the control: white on it"


TERMINAL_TOKENS = ("--pane", "--pane-fg", "--pane-muted", *(f"--a{n}" for n in range(16)))
"""What an agent's screen is drawn with: its ground and inks, and the 16 colours of ANSI."""


def test_an_agents_screen_keeps_the_dark_ground_its_own_colours_were_picked_for() -> None:
    """The pane, its card strip and the transcript took their ground from the phone's
    scheme, white in light mode, while an agent's 256 and true colours arrive as fixed rgb(),
    picked for its own theme, dark in Claude Code by default: on a light-mode phone its reply
    bullet was white on white (1.0:1), and the dialog option the pad's ↑ ↓ ⏎ move 1.5:1, as
    Claude Code 2.1 drew them under the fleet's tmux. The terminal keeps one dark palette in
    both themes, and every rule that draws in it takes only its tokens. Its scrollbars are
    dark too: in the root's scheme they were drawn light across that dark ground."""
    css = _text("app.css")
    themes = _themes(css)
    for token in TERMINAL_TOKENS:
        assert themes["light"][token] == themes["dark"][token], token
    assert themes["light"]["--fg"] != themes["dark"]["--fg"], "the control: the page's own change"
    drawn = {
        ("pre.pane, pre.transcript", "background"): "var(--pane)",
        ("pre.pane, pre.transcript", "color"): "var(--pane-fg)",
        ("pre.strip", "background"): "var(--pane)",
        ("pre.strip", "color"): "var(--pane-muted)",
        ("pre.pane, pre.transcript", "color-scheme"): "dark",
        ("pre.strip", "color-scheme"): "dark",
        (".ln.muted", "color"): "var(--pane-muted)",
        (".rf", "color"): "var(--pane)",
        (".rb", "background"): "var(--pane-fg)",
        (".cur", "background"): "var(--pane-fg)",
        (".cur", "color"): "var(--pane)",
    }
    assert {key: _css_value(css, *key) for key in drawn} == drawn


def _rgb_hex(value: str) -> str:
    match = _RGB.fullmatch(value)
    assert match is not None, value
    return "#" + "".join(f"{int(part):02x}" for part in match.groups())


def test_an_agents_own_colours_read_on_its_screen_in_both_themes(
    node_report: dict[str, Any],
) -> None:
    """Claude Code's dark theme as tmux captured it, through the page's own ``ansiToRuns``:
    each colour it drew with reads at AA contrast on the pane's ground whatever the phone's
    scheme, and so does the pane's own ink where it set none."""
    css = _text("app.css")
    runs = [run for row in node_report["agentRows"] for run in row if run["text"].strip()]
    inks = {_rgb_hex(run["color"]) for run in runs if "color" in run}
    assert {"#ffffff", "#afd7ff", "#ffd700", "#949494"} <= inks, inks
    assert any("color" not in run for run in runs), "the control: text in the pane's own ink"
    for name, theme in _themes(css).items():
        for ink in sorted(inks):
            assert _contrast(ink, theme["--pane"]) >= 4.5, (name, ink)


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


def _node_report(harness: Path, cards: list[dict[str, object]] | None = None) -> dict[str, Any]:
    """The harness's report; ``cards``, items the server built, go to it as ``ASQ_CARDS``."""
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
        env=None if cards is None else {**os.environ, "ASQ_CARDS": json.dumps(cards)},
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


def test_an_underlines_colour_or_style_changes_nothing_else_on_the_row(
    node_report: dict[str, Any],
) -> None:
    """tmux's capture writes an underline's colour as ``58;2;r;g;b`` or ``58;5;n``, whatever
    form the app drew it in, and the page read each number after the 58 as a code of its own:
    ``58;5;7`` inverted the cell, ``58;5;31`` turned it red, and the 0 in ``58;2;0;255;0``
    undid the bold red underline it came with. A style of none, ``4:0``, underlined."""
    assert node_report["underlines"] == {
        "rgb": [
            {"text": "RED", "classes": ["b", "f1"]},
            {"text": "UNDER", "classes": ["b", "u", "f1"]},
            {"text": "after", "classes": ["b", "f1"]},
        ],
        "indexed": [{"text": "spell ok", "classes": []}],
        "red": [{"text": "xy", "classes": []}],
        "ones": [{"text": "X", "classes": []}],
        "none": [{"text": "a", "classes": ["u"]}, {"text": "b", "classes": []}],
        "curly": [{"text": "a", "classes": []}, {"text": "b", "classes": ["u"]}],
    }


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


def _served(tmp_path: Path, tool: str, **payload: object) -> dict[str, object]:
    """The item the scan builds for coder-1 waiting on one ``tool`` call, from a transcript
    as the scan reads it, in the shape ``GET api/needs`` sends it."""
    from datetime import UTC, datetime, timedelta

    from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamSession

    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    project = ProjectInfo(id="prj_x", root=Path("/work/x"))
    row = FleetAgent(
        id="agt_1",
        project_id=project.id,
        label="coder-1",
        role="coder",
        pane_id="%1",
        session_id="ses_1",
        cwd=project.root,
        created_at=now - timedelta(hours=1),
    )
    session = TeamSession(
        id="ses_1",
        project_id=project.id,
        role="coder",
        started_at=row.created_at,
        last_seen_at=now - timedelta(minutes=3),
        state="attention",
    )
    status = FleetAgentStatus.model_validate(
        {"agent": row, "state": "attention", "detail": None, "session": session}
    )
    asked = {"role": "user", "content": "go on"}
    call = {"type": "tool_use", "id": "toolu_1", "name": tool, "input": payload}
    records = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-10-07T11:57:00Z", "message": asked},
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-10-07T11:58:00Z",
            "message": {"id": "m1", "role": "assistant", "content": [call], "stop_reason": None},
        },
    ]
    path = tmp_path / f"t{len(list(tmp_path.iterdir()))}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    tail = transcript.read_transcript_tail(path)
    (item,) = remote_needs.needs_from_agent(status, tail, project=project, events=[], now=now)
    return item.needs_item_json()


def test_a_card_says_what_it_leaves_out_of_the_call_its_buttons_answer(tmp_path: Path) -> None:
    """A permission's command was cut at 2 000 characters, and its detail fit to 4 KiB, marked
    by a "…" in mid-text alone: a 3 549-character heredoc that ends in ``rm -rf`` showed as
    ``print('step…`` and then ``description: Run the steps``, all of it to the eye, with 1
    and 2 live. An input over 16 KiB never reaches the scan, and its card said ``tool:
    Write`` and nothing more, as a call with no input would; a plan over 16 KiB, no plan at
    all. Each card says what it leaves out now, from the transcript the scan reads to the
    text the page draws; a call shown whole says nothing of the kind."""
    steps = "python3 - <<'EOF'\n" + "print('step')\n" * 250 + "EOF\nrm -rf ~/projects/important"
    big = "z" * (transcript.TOOL_INPUT_MAX + 1)
    cards = [
        _served(tmp_path, "Bash", command=steps, description="Run the steps"),
        _served(tmp_path, "Write", file_path="/src/big.py", content=big),
        _served(tmp_path, "ExitPlanMode", plan="1. a step\n" * 2_000),
        _served(tmp_path, "Bash", command="pytest -q", description="Run the tests"),
    ]
    cut, write, plan, whole = (card["detail"] for card in cards)
    assert isinstance(cut, dict) and cut["cut"] == {"command": len(steps)} and len(steps) > 2_000
    assert len(json.dumps(cut, ensure_ascii=False, separators=(",", ":")).encode()) <= 4_096
    assert write == {"tool": "Write", "input": {}, "dropped": True}
    assert plan == {"plan": "", "dropped": True}
    assert whole == {
        "tool": "Bash",
        "input": {"command": "pytest -q", "description": "Run the tests"},
    }
    said = [
        [text for name, text in card if name == "cut"]
        for card in _node_report(HARNESS, cards)["served"]
    ]
    tail = " Open the agent to read it before you answer."
    assert said[0] == [
        f"Not all of it: the card shows the start of its command ({len(steps)} characters in"
        f" all).{tail}"
    ]
    assert said[1] == said[2] == [f"Not all of it: this was too long to send to the phone.{tail}"]
    assert said[3] == []


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


def test_the_page_has_a_badge_for_every_kind_the_feed_has() -> None:
    """A kind the page has no badge for is a card that says only "Needs you": every kind of
    ``remote_needs.NEEDS_KINDS`` is in the page's own table, and nothing else is."""
    table = re.search(r"\nconst KINDS = \{(.*?)\};", _text("app.js"), re.S)
    assert table is not None, "the KINDS table is not where the page declares it"
    assert re.findall(r"(\w+): \[", table.group(1)) == list(remote_needs.NEEDS_KINDS)


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


def test_an_answer_cut_off_halfway_is_a_lost_request_not_a_remote_that_went_off(
    boot_report: dict[str, Any],
) -> None:
    """The page read an answer's body in the same try as its JSON, so a connection that
    dropped halfway through a body read as an answer that was not JSON: the off screen, Remote
    is off, over a send-keys that had run, its result never shown and no retry to fetch it,
    while the machine was up all along (Chromium, ERR_CONTENT_LENGTH_MISMATCH). It is a lost
    request now: the write goes again, with its request_id, once the page reconnects, and the
    machine's ledger answers it; a read says it could not reach the machine."""
    cut = boot_report["bodyCut"]
    write = cut["write"]
    assert write["waiting"]["off"] is None and write["waiting"]["sockets"] == 2
    assert write["waiting"]["send"] == {"busy": True, "disabled": True}
    first, retry = write["bodies"]
    assert first == retry and first["text"] == "hello"  # the same request_id: run at most once
    assert write["off"] is None and write["typed"] == ""
    assert write["pending"] == 0 and write["orphans"] == 0
    assert cut["read"] == {
        "off": None,
        "said": "Could not reach the machine — try again in a moment.",
        "sockets": 1,
    }


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


def test_a_sheet_whose_write_was_lost_lets_go_once_the_write_would_not_go_again(
    boot_report: dict[str, Any],
) -> None:
    """A lost write waited for the next socket to open, and its sheet with it: busy, Close
    disabled, Escape and a tap beside it refused, the page behind inert. With the phone still
    offline nothing ever settled it, while the sheet went on promising a retry "within 15
    seconds" long after, and an installed app has no Back. Past those 15 s the write would not
    go again anyway: the wait ends there, the sheet says so and can be closed, and nothing is
    sent when the phone is back. A Reply, and a note on the Board tab, said nothing at all
    while they waited."""
    offline = boot_report["offlineSheet"]
    waiting = "The phone lost the connection; this goes out again if it is back within 15 seconds."
    tell = {"title": "Tell coder-1", "busy": True, "close": True, "said": waiting}
    assert offline["waiting"] == tell
    assert offline["late"] == {
        **tell,
        "busy": False,
        "close": False,
        "said": "Not sent again — the phone was away too long to be sure the agent still shows "
        "what you saw. If the machine got it, its result shows here; if not, look, then send "
        "it again.",
    }
    assert offline["escaped"] is None and offline["told"] == 1
    assert offline["replying"] == {**tell, "title": "Reply on the board"}
    assert offline["noting"] == waiting, "a note on the Board tab says it waits too"


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
    to say that is the button the sheet offers next. And what Stop will do then says the
    prompt is dismissed first (SPEC §6.3), as the next tap does it: no test read it."""
    stop = boot_report["stopAtAPrompt"]
    assert stop["said"] == (
        "coder-1 may be showing a prompt that stopping it now would answer. "
        "Press Esc (No) first to dismiss it."
    )
    assert stop["lead"] == (
        "Stop coder-1: /exit, then its window is killed after 5 s. "
        "Its prompt is dismissed (No) first."
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


def test_the_page_asks_for_a_fleet_only_on_a_projects_screens_and_an_agents(
    boot_report: dict[str, Any],
) -> None:
    """r4 7/9: every socket was read the current project's fleet every second from the moment
    it opened, a ``fleet ls`` on the machine each time, though only a project's screens and
    an agent's draw it. The page asked for one on every new socket, wherever it was."""
    steps = boot_report["fleetOnItsScreens"]
    assert steps["feed"] == [], "the feed draws no fleet"
    assert steps["project"] == ["prj_x"]
    assert steps["agent"] == ["prj_x"], "its tabs and its agents ask once"
    assert steps["left"] == ["prj_x", False], "leaving them says so"
    assert steps["woken"] == [], "a new socket asks for none where none is drawn"


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


def test_the_board_tab_draws_the_board_its_project_answers_with(
    boot_report: dict[str, Any],
) -> None:
    """r4 2/9: under ``AISQUARE_TEAM_HUB`` every project's board is the hub's, and the tab drew
    a board only when its own project id was the tab's: it dropped every frame and every read,
    and said Loading… for as long as it was open. A frame names the pid it answers now, and a
    read answers the pid it asked about."""
    board = boot_report["boardAnswers"]
    assert board["other"] == ["Loading…"], "a frame for another pid is not this tab's"
    assert board["frame"] == ["on the hub"] and board["read"] == ["on the hub"]


def test_a_board_that_cannot_be_read_says_why_on_the_board_tab(
    boot_report: dict[str, Any],
) -> None:
    """r4 4/9: the Board tab's read acted only on an answer that was ok, and the stream sent
    no frame for a board that raised, so the tab said Loading… for as long as it was open:
    with the orchestrator off, the project removed, the store locked. A refused read and a
    frame that says why are each said there now, and a board that comes after is drawn."""
    board = boot_report["boardAnswers"]
    assert board["refused"] == ["no project matches 'prj_x'"]
    assert board["readable"] == ["on the hub"], "a board read since takes the refusal's place"
    assert board["unread"] == ["the agent orchestrator is disabled"], "the read after it is no news"


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


def test_a_usage_limits_reset_is_told_by_the_phones_own_clock(
    node_report: dict[str, Any], boot_report: dict[str, Any]
) -> None:
    """The limited card, its push and the Fleet tab said when a limit lifts by the machine's
    clock, ``(13:10)`` on a phone in UTC-7 where it lifts at 06:10, beside a page that tells
    every other time by the phone's. The machine sends the instant (a card's
    ``detail.resets_at``, a row's ``session.limit_resets_at``) and the page tells it, with
    the weekday when it is not today."""
    times = node_report["limitTimes"]
    (today,) = times["today"]
    assert today.startswith("Resets at ") and "06:10" in today and "13:10" not in today
    assert re.fullmatch(r"Resets at \S+ 06:10.*", times["later"][0]), times["later"]
    assert times["none"] == []
    rows = boot_report["limitTimes"]
    assert rows[0] == "coder", "a row that is not limited keeps the detail it was sent"
    assert rows[1].startswith("coder · limit resets in 3 h (") and "06:10" in rows[1]
    assert "13:10" not in rows[1]


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
        "toast": "Restart coder-1: The machine could not answer: tmux did not answer",
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
    keys too, and a key tapped while it is typed waits for it. A key waiting its turn says
    so, marked sending until its answer: on a slow link the taps behind it said nothing
    until their toasts, long after."""
    keys = boot_report["keysInOrder"]
    assert keys["quick"] == {
        "atOnce": 1,
        "order": ["Down", "Down", "Enter"],
        "sending": [["⏎", "↓"], ["⏎", "↓"], ["⏎"], []],  # ↓ until both its taps are answered
    }
    assert keys["refused"]["marked"] == [], "a key that was not sent is not left marked"
    assert _css_value(_text("app.css"), ".pad .key.sending", "border-color") == "var(--accent)"
    assert keys["refused"]["sent"] == 1 and keys["refused"]["toast"].startswith("Not sent — ")
    assert keys["refused"]["after"] == ["Down", "Enter"], "a key tapped after the refusal goes"
    waited = keys["waitedTooLong"]  # tapped for a screen 16 s gone by the time it could go
    assert waited["sent"] == ["Down"] and waited["toast"].startswith("Not sent — ")
    assert waited["marked"] == []
    assert keys["lost"] == ["Down", "Down", "Enter"]
    queued = keys["lostAfterItsTurn"]  # tapped 16 s ago, sent 6 s ago, its request lost
    assert queued["sent"] == ["Down", "Enter"], "its retry counts from the tap, not the send"
    assert queued["toast"].startswith("Not sent again — the phone was away too long")
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
        ["Esc", None, 7],  # the confirmed one starts no new pair
        ["Esc", "Press Esc again?", 7],  # 1.4 s after the last: still within 1.5 s
        ["Close", None, 7],
    ]
    escapes = [["Escape"]] * 4
    assert pad["keys"] == [["C-c"], ["C-c"], ["C-c", "confirm_exit"], *escapes]


def test_a_page_that_slept_holds_its_keys_until_the_machine_says_what_is_on_screen(
    boot_report: dict[str, Any],
) -> None:
    """A wake's new socket counted as an update the moment it opened, before any frame: the
    next second's check found the page fresh and let the pad and Send act on the screen from
    before the sleep, for as long as the first frame took. And the pane comes a tick after the
    other frames, so even a quick wake left a second of that: the Live tab's keys now wait
    for its pane from the socket open now, and the pane is greyed while they do. Without the
    grey, the first frame turned the dot green and the old pane looked live, its keys off
    with nothing to say why. Each step is [stale, Send disabled, the pane greyed as held]."""
    assert boot_report["staleAcrossAWake"] == [
        [False, False, False],  # the pane is in
        [True, True, False],  # a minute with nothing heard: the whole screen greyed as stale
        [True, True, True],  # a wake's socket opened, and the next second's check ran
        [False, True, True],  # its first frame came, not the pane
        [False, False, False],  # the pane came
    ]
    assert _css_value(_text("app.css"), "body.held pre.pane", "filter") == "grayscale(1)"


def test_a_socket_that_died_without_a_close_is_replaced_once_the_page_goes_stale(
    boot_report: dict[str, Any],
) -> None:
    """Only a close, a wake or a lost write opened a new socket. One that died with no close
    (Wi-Fi gave way to cellular, a NAT or the tunnel's edge forgot it) fired none of them: the
    page went stale after 25 s and stayed so, every button waiting, so no write could be lost
    to reconnect it either, and an installed app has no reload. The tick that finds the page
    stale replaces a socket still open or connecting, once a stale span; a page whose socket
    another tab took waits for its own Reconnect, or the two would take it from each other.
    Each step is the sockets opened, stale, and whether Send waits."""
    silent = boot_report["silentSocket"]
    assert silent["steps"] == [
        {"sockets": 1, "stale": False, "send": False},
        {"sockets": 2, "stale": True, "send": True},  # 30 s with nothing heard
        {"sockets": 2, "stale": True, "send": True},  # the same second: no third
        {"sockets": 3, "stale": True, "send": True},  # 26 s on, the new one silent too
        {"sockets": 3, "stale": False, "send": False},  # its pane came
    ]
    assert silent["taken"] == {"sockets": 1, "stale": True, "state": "replaced"}


def test_a_socket_made_after_the_page_went_stale_has_its_own_span_to_bring_a_frame(
    boot_report: dict[str, Any],
) -> None:
    """The tick replaced a stale page's socket a stale span after the last wake, and only a
    wake marked one. An unlock, a Retry on the off screen and the backoff's reconnect connect
    without one, after a lock, an off screen or a drop that left the page stale: the next
    second closed the socket each had just made, still in its handshake, opened another and
    read the feed, the remote and the actions again (sweep4-13). The span now runs from the
    socket's own connect, so a silent one is still replaced 25 s after it was made. Each step
    is the sockets opened, whether the newest is still connecting, stale, and the reads since
    it was made."""
    made = {"sockets": 2, "connecting": True, "stale": True, "reads": 0}
    replaced = {"sockets": 3, "connecting": True, "stale": True, "reads": 3}
    for how, steps in boot_report["staleBeforeASocket"].items():
        assert steps == [made, made, replaced], how  # made; a second on; 26 s on, still silent


def test_the_first_socket_after_an_unlock_that_fails_is_tried_again_after_a_second(
    boot_report: dict[str, Any],
) -> None:
    """A sign-in that ran out while the link was down locks the page from a probe, with the
    backoff as far on as those failures took it. A wake and Retry start it again from the first
    step (SPEC §6.4); the unlock kept it, so the first socket after the passphrase that failed
    waited 8 s here, and up to 30 s, with the machine just heard from and every button held.
    Each is how long the reconnect it set waits: the drop, two handshakes the machine was away
    for, and the first after the unlock."""
    across = boot_report["backoffAcrossAnUnlock"]
    assert across == {"waits": [1, 2, 4, 1], "locked": True}


def test_the_live_tabs_keys_wait_from_the_moment_its_socket_is_lost(
    boot_report: dict[str, Any],
) -> None:
    """The Live tab's keys waited for the pane only once the next socket had opened. Between the
    loss and that open, a wake's handshake, a reconnect's backoff after a drop, or for good
    after another tab took the socket (4409), the pad and Send were live beside a pane from the
    old socket: a "1" tapped there was typed into whatever the agent showed by then, a newer
    prompt included (docs/remote.md: held "until its pane has come through again"). Each case
    is held, nothing sent, and live again once the next socket's pane came."""
    held = boot_report["heldBetweenSockets"]
    for how in ("wake", "dropped", "taken"):
        assert held[how] == {"held": [True] * 3, "sent": 0, "after": [False] * 3}, how


def test_the_transcript_tabs_keys_never_wait_for_a_pane(boot_report: dict[str, Any]) -> None:
    """The Live tab's keys wait for its pane to come on the socket open now. The Transcript
    tab has the same input bar and watches no pane: were it held as Live is, Send and the
    pad there would wait for a frame that never comes."""
    assert boot_report["transcriptSend"] == {
        "send": {"busy": False, "disabled": False},
        "sent": [["Enter"]],
    }


def test_send_on_the_transcript_tab_types_nothing_while_a_prompt_may_be_up(
    boot_report: dict[str, Any],
) -> None:
    """Sweep of #243, round 4: the Transcript tab shows no pane, and its Send typed the text
    and Enter into whatever the agent showed, ⏎ being on by default: into a Bash prompt,
    the Enter took "1. Yes". It asks the machine to type nothing while a prompt may be up
    (``dialog_guard``), and a refusal keeps the text and says where to look. The Live
    tab's Send, beside the prompt, goes without it (the write bodies' test)."""
    guarded = boot_report["transcriptSendGuarded"]
    sent = {
        "agent": "coder-1",
        "project": "prj_x",
        "enter": True,
        "dialog_guard": True,
        "agent_id": "agt_1",
    }
    assert guarded["bodies"] == [
        {**sent, "text": "no - run the tests instead"},
        {**sent, "text": "run the tests"},
    ]
    assert guarded["refused"] == {
        "toast": "Not sent — coder-1 may be showing a prompt that this would answer. "
        "Look at it on Live first.",
        "typed": "no - run the tests instead",
    }
    assert guarded["typed"] == "", "sent, so the box is cleared"


def test_keys_and_send_carry_the_agent_id_of_the_screen_they_were_typed_at(
    boot_report: dict[str, Any],
) -> None:
    """Review of #243, round 5: a key carried no agent, so a ``1`` tapped at the permission
    prompt the Live tab showed went into the replacement a restart or a hand-over had started
    on the machine before the next frame came. Each key and line carries the ``agent_id``
    of the frame drawn when it was tapped, a ^C confirmed on its sheet after the next
    frame came included, and the Transcript tab's Send that of the page it read. A frame
    that could not be read shows no agent to type at: the pad and Send wait for a screen,
    as they wait for the first one on a new socket. Nor does the Transcript tab before a
    page of it came, or while none could be read: Send and the pad there wait for one, as
    a line sent with no id went to whichever agent held the label by then."""
    pinned = boot_report["pinnedKeys"]
    assert pinned["live"] == [
        [["1"], "agt_1"],
        [["C-c"], "agt_1"],  # asked at agt_1's screen, confirmed once agt_2's came
        ["hello", "agt_2"],
        [["2"], "agt_2"],  # refused stale
        [["4"], "agt_3"],  # the "3" tapped at the unread frame never went
    ]
    assert pinned["staleSaid"] == "'coder-1' is another agent now (agt_3) — nothing was sent"
    assert pinned["unread"] == [True, True, True], "Send, the pad, and the pane greyed"
    assert pinned["read"] == [False, False, False]
    assert pinned["transcriptHeld"] == {
        "unpaged": [True, True],
        "failed": [True, True],
        "paged": [False, False],
    }
    assert pinned["transcript"] == [["yes", "agt_1", True]], "nothing went before the page"


def test_a_sheet_keeps_focus_where_it_put_it_and_closes_onto_the_screen_once_its_opener_went(
    boot_report: dict[str, Any],
) -> None:
    """A sheet takes focus for a screen reader, but a Tell's message box, which its sheet
    focuses itself, keeps it there: taken by the sheet, the phone's keyboard would close.
    Closed, a sheet gives focus back to what opened it, and to the screen itself when that
    is gone: here the card's Tell…, drawn anew by the next feed frame."""
    focus = boot_report["sheetFocus"]
    assert focus == {"typing": "TEXTAREA", "redrawn": True, "closedOnto": "main"}


def test_focus_lands_on_the_new_screen_when_the_route_changes(
    boot_report: dict[str, Any],
) -> None:
    """A route change cleared the screen under the focused control and moved focus nowhere: it
    fell to the page, so a screen reader lost its place and was never told the screen had
    changed (a row, a tab, a card's Open, Back with a sheet open). Focus goes to the new
    screen's heading now, or to the tab chosen on a tab switch, and stays where it was when
    that is still on the page (the bottom nav). A page's first screen leaves it alone."""
    lands = boot_report["focusLands"]
    on = {"connected": True, "main": True, "nav": False}
    assert lands["loaded"]["tag"] == "BODY", "a page's first screen leaves focus where loads do"
    assert lands["row"] == {"tag": "H2", "text": "x", **on}
    assert lands["tab"] == {"tag": "BUTTON", "text": "Board", **on}
    assert lands["nav"] == {
        "tag": "BUTTON",
        "text": "Settings",
        "connected": True,
        "main": False,
        "nav": True,
    }
    assert lands["open"] == {"tag": "H2", "text": "coder-1", **on}
    assert lands["back"] == {"tag": "H2", "text": "x", **on}


def test_focus_stays_on_what_a_redraw_puts_in_place_of_the_focused_control(
    boot_report: dict[str, Any],
) -> None:
    """A redraw took the focused control away as a route change did, and focus fell to the
    page: a row at every fleet frame and every 15 s poll on Projects, a card's button when a
    needs frame changed the card, Reconnect here when the banner redrew, Settings' Turn on
    when its answer redrew the panel. Focus stays on what took its place now, and goes to
    the screen when nothing did: a revoke, the feed's notifications line hidden, Load older
    hidden once the transcript's first page came."""
    kept = boot_report["focusKept"]
    on = {"connected": True, "main": True, "nav": False}
    assert kept["frame"] == {"tag": "BUTTON", "text": "coder-1workingcoder", **on}
    assert kept["poll"] == {"tag": "BUTTON", "text": "xno agents", **on}
    assert kept["card"] == {"tag": "BUTTON", "text": "Open", **on, "redrawn": True}
    assert kept["revoked"]["tag"] == "MAIN" and kept["revoked"]["connected"]
    banner = {"tag": "BUTTON", "text": "Reconnect here", "connected": True, "main": False}
    assert kept["banner"] == {**banner, "nav": False}
    assert kept["toggled"] == {"tag": "BUTTON", "text": "Turn on", **on}
    assert kept["hidden"] == {"tag": "MAIN", "text": kept["hidden"]["text"], **on}
    assert boot_report["transcriptLoads"]["lastFocus"] == "main", "Load older hid itself"


def test_androids_back_closes_the_sheet_and_leaves_the_screen_under_it(
    boot_report: dict[str, Any],
) -> None:
    """A sheet took no close request: Android's Back went back a screen behind it, and what was
    typed in it went with the sheet; from the feed the app opens on, or a card a notification
    opened, it left the app. A CloseWatcher takes Back for the sheet now, on the screen it
    covers. Close lets the watcher go, and one sheet in another's place keeps a single one; a
    busy sheet refuses the first Back, as it does Escape, and closes on the next."""
    back = boot_report["backOverASheet"]
    feed = {"sheet": None, "at": "#/", "cards": 1, "active": 0}
    assert back["typed"] == {"did": "closed", **feed}
    assert back["shut"] == 0
    assert back["busy"] == [
        {**feed, "did": "refused", "sheet": "Tell coder-1", "active": 1},
        {**feed, "did": "closed"},
    ]
    assert back["replaced"] == {
        "did": "closed",
        "sheet": None,
        "at": "#/p/prj_x/a/coder-1/live",
        "made": 2,
        "active": 0,
    }


def test_without_close_watcher_a_sheet_holds_a_history_entry_back_takes(
    boot_report: dict[str, Any],
) -> None:
    """Safari has no CloseWatcher (nor Firefox before 149): there Back, iOS's swipe too, went
    back a screen behind an open sheet, or out of the page from its first entry. The sheet
    holds a history entry of its own now, which Back takes with it; closed by its Close, it
    takes the entry off, so the next Back is not spent on nothing. A route asked for as a
    sheet closes waits for that: pushed first, the Back undid it. A route a sheet leads to
    takes the sheet's entry, and Back from there is the screen under the sheet."""
    back = boot_report["backWithoutCloseWatcher"]
    feed = {"sheet": None, "at": "#/", "cards": 1}
    assert back["opened"] == {"at": 1, "length": 2}
    assert back["typed"] == {"did": "back", **feed, "history": {"at": 0, "length": 2}}
    assert back["shut"] == {"did": "close", **feed, "history": {"at": 0, "length": 2}}
    projects = {"sheet": None, "at": "#/projects", "cards": 0, "history": {"at": 1, "length": 2}}
    assert back["raced"] == {"did": "close, then go", **projects}
    card = {"sheet": None, "at": "#/n/ny_0123456789abcdef", "cards": 0}
    assert back["led"] == {"did": "card", **card, "history": {"at": 1, "length": 2}}
    agent = {"sheet": None, "at": "#/p/prj_x/a/coder-1/live", "cards": 0}
    assert back["back"] == {"did": "back", **agent, "history": {"at": 0, "length": 2}}


def test_the_transcript_asks_for_lines_as_wide_as_fit_inside_its_padding(
    boot_report: dict[str, Any],
) -> None:
    """The page measured its columns as the box's clientWidth less 8 px, but the box has 8 px
    of padding a side, which clientWidth counts: it asked the machine to wrap a column or two
    wider than fit, and every full line wrapped again on the phone (45 asked where 44 fit on a
    360 px phone, 52 where 51 fit on a 412). A line of the width asked fits now, and a line
    one column longer would not. clientWidth is whole pixels, and may be the box's width
    rounded up: half a pixel less is what is sure to be there, so a box of exactly 45
    columns by it is asked for 44."""
    report = boot_report["transcriptColumns"]
    for width, columns in report["asked"].items():
        inside = int(width) - 2 * report["padding"]
        assert columns * report["charPx"] <= inside - 0.5, (width, columns)
        assert (columns + 1) * report["charPx"] > inside - 0.5, (width, columns)
    assert report["asked"] == {"334": 44, "340": 44, "364": 48, "386": 51}


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


def test_a_transcript_whose_conversation_changed_is_read_again_from_its_end(
    boot_report: dict[str, Any],
) -> None:
    """After a ``/clear`` or a fresh restart, Load older read the new conversation from the old
    one's offset and put it above the old turns as their past. The machine refuses a cursor
    of another conversation (``stale_cursor``), and the page reads this one from its end."""
    stale = boot_report["transcriptStale"]
    assert stale == {
        "asked": [None, "ses_1:100", None],
        "shown": ["NEW 0", "NEW 1"],
        "older": False,
    }


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
    change, or be typed wrong, with every test green. Each, as the machine received it. And
    writePath throws for a name ``WRITES`` does not list, as the name of a write made up at
    the call (``"agent/" + kind``) would otherwise go out to a path nothing answers; its
    refusal could go with every test green, the static check reading only typed names."""
    sent = boot_report["writesReachTheirRoutes"]
    assert sent["board"] == ["POST api/note"]
    assert sent["reply"] == ["POST api/note", "POST api/needs/dismiss"]
    assert sent["agent"] == ["POST api/agent/restart", "POST api/agent/switch"]
    assert sent["refused"] == {"agents/stop": True, "notes": True, "agent/stop": False}


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


def test_a_card_refused_stale_says_so_in_its_place_and_the_feed_is_read_again(
    boot_report: dict[str, Any],
) -> None:
    """SPEC §6.3 and §6.4: a 409 ``stale`` puts "No longer needs you" and what is current in
    the card's place, and reads the feed again; the card stays hidden while the note shows,
    even if that read still lists it, and comes back once the note's 6 s are up and the feed
    lists it still. No test reached any of it: the note, the read, the hidden card and the
    reason could each go with every test green."""
    stale = boot_report["staleCards"]
    assert stale["answer"] == {
        "shown": ["No longer needs you: coder-1 asks to run a command"],
        "reads": 1,
        "later": ["card: coder-1 asks which approach to take"],
    }
    nothing = ["No longer needs you: nothing waits on coder-1 now."]
    assert stale["tell"] == {"shown": nothing, "sheet": None}
    assert stale["stop"] == {"shown": nothing, "sheet": None}


def test_each_refusal_is_said_in_the_sentence_the_spec_gives_it(
    boot_report: dict[str, Any],
) -> None:
    """SPEC §6.4 gives each refusal its sentence, and failText says them, but few were ever
    asked for: a bad origin, a busy agent, a body too long or too many tries could each lose
    its sentence, or fall through to the machine's own words for curl, with every test
    green."""
    assert boot_report["refusalSentences"] == {
        "badOrigin": "Open this page from the link the machine shows.",
        "readOnly": "Read-only: writes are off",
        "gone": "That agent is gone.",
        "busy": "Still running — another action on coder-1 is still running",
        "inProgress": "Still running — the result shows here when it finishes.",
        "other": "coder-1's pane is not running the agent — nothing was sent",
        "tooLong": "Too long (max 8000 characters).",
        "tooMany": "Too many tries — wait 30 s.",
        "unavailable": "The machine could not answer: tmux did not answer",
        "unavailableBare": "The machine could not answer — try again in a moment.",
        "unwritable": (
            "the machine could not save that: its ~/.aisquare/remote.json would not write (a full"
            " disk, or a home it may not write) — nothing was changed; fix that on the machine,"
            " then try again"
        ),
        "notJson": "Remote is off on the machine, or the link changed.",
    }


def test_a_503_says_the_machines_reason_and_a_revoke_it_holds_unsaved_leaves_the_list(
    boot_report: dict[str, Any],
) -> None:
    """Every 503 said "try again in a moment", whatever the machine said: Tasks with Team
    off, a store that would not open, tmux missing, all for good. A revoke the running Remote
    held but could not save (remote.json would not write) is answered 503 with the command that
    saves it, and the phone said to try again while the device stayed listed; tapped again it
    answered 404, which looked like success, and a later change to remote.json from a shell
    signed the stolen phone back in. The page says the machine's sentence, reads the list
    again (as after a 404: another tab had revoked it), and a sign-out held that way goes to
    unlock."""
    given = boot_report["reasonsGiven"]
    revoked = given["revoked"]
    assert "run  aisquare remote revoke dev_4e5f6a7b  on the machine" in revoked["toast"]
    assert revoked["toast"].startswith("revoked on the running Remote, but")
    assert revoked["rows"] == ["This device"] and revoked["reads"] == 2
    signed_out = given["signedOut"]
    assert signed_out["hash"] == "#/unlock"
    assert "aisquare remote revoke dev_0a1b2c3d" in signed_out["toast"]
    assert given["revokedElsewhere"] == {"toast": "no such device", "rows": ["This device"]}
    assert given["tasks"] == [
        "The machine could not answer: the agent orchestrator is disabled (AISQUARE_TEAM=0)"
    ]


def test_each_close_code_and_a_failed_handshake_lead_where_the_spec_says(
    boot_report: dict[str, Any],
) -> None:
    """SPEC §6.4: 4404 says the link is no longer valid and 4410 that Remote is off, and a
    handshake that failed before open asks ``api/remote`` why: a 404 is the off-or-moved
    screen, an answer a reconnect. A socket that dropped once open reconnects without
    asking. None of it had a test: each branch could go with every test green, and a page
    on a link that changed would reconnect for ever."""
    closes = boot_report["socketCloses"]
    assert closes["link"] == {"shown": "This link is no longer valid", "probes": 0, "timers": []}
    assert closes["off"] == {"shown": "Remote is off on the machine", "probes": 0, "timers": []}
    assert closes["probedGone"] == {
        "shown": "Remote is off on the machine, or the link changed",
        "probes": 1,
        "timers": [],
    }
    assert closes["probedHere"] == {"shown": None, "probes": 1, "timers": ["connect"]}
    assert closes["dropped"] == {"shown": None, "probes": 0, "timers": ["connect"]}


def test_a_page_off_asks_again_when_the_phone_wakes_or_a_notification_is_tapped(
    boot_report: dict[str, Any],
) -> None:
    """SPEC §6.4 reconnects on every wake, but the page returned early while off: once Remote
    went off, or stopped answering, it said so until Retry was tapped, though a fleet UI
    started again turns Remote back on under the same link. A notification of the Remote
    back on, tapped, opened on "Remote is off" and no card. A wake asks the machine again, as
    Retry does, and so does a tap; a link the machine refused (4404) has nothing to ask."""
    back = boot_report["offAndBack"]
    assert back["wentOff"] == {
        "off": "off",
        "heading": "Remote is off on the machine",
        "cards": 0,
        "reads": 1,
    }
    assert back["wokeUp"] == {
        "off": None,
        "heading": None,
        "cards": 1,
        "reads": 2,
        "sockets": 2,
        "open": True,
    }
    assert back["gone"]["off"] == "gone" and back["gone"]["reads"] == 1
    assert back["landed"] == {
        "off": None,
        "heading": None,
        "cards": 1,
        "reads": 2,
        "hash": "#/n/ny_0123456789abcdef/p/prj_x/a/coder-1",
    }
    assert back["link"] == {
        "off": "link",
        "heading": "This link is no longer valid",
        "cards": 0,
        "reads": 1,
    }


def test_a_page_that_cannot_go_on_keeps_no_timer_extend_or_connecting_dot(
    boot_report: dict[str, Any],
) -> None:
    """The strip drew from the machine's last word whatever the screen: under "Remote is off on
    the machine" it still said "off in 0 min", or counted down a deadline the machine had
    dropped, beside a live Extend 1 h whose tap was refused, and a dot saying "Connecting"
    though nothing would connect until Retry. Signed out, the dot said the same over the
    unlock form, and the READ-ONLY pill offered a reason for a page that showed nothing; the
    tab's title kept the feed's count from before; and a banner said another tab took the
    live view, its Reconnect here doing nothing, over the unlock form."""
    strip = boot_report["offStrip"]
    gone = {
        "off": False,
        "extend": False,
        "readOnly": False,
        "dot": "Not connected",
        "title": "aisquare remote",
    }
    for how in ("off", "link", "signedOut", "probedGone"):
        assert strip[how]["before"]["off"] and strip[how]["before"]["extend"], how
        assert strip[how]["before"]["readOnly"], how
        assert strip[how]["before"]["title"] == "(1) aisquare remote", how
        assert strip[how]["after"] == gone, how
    assert strip["signedOut"]["at"] == "#/unlock" and strip["off"]["at"] == "#/"
    taken = strip["takenThenLocked"]
    assert taken == {"said": True, "after": False, "at": "#/unlock"}, "no Reconnect over a lock"


def test_an_unlock_refused_for_too_many_tries_counts_down_to_the_next(
    boot_report: dict[str, Any],
) -> None:
    """SPEC §6.4: a 429 waits ``Retry-After``, with a countdown on the unlock form (60 s when
    the machine gives none). It had no test: the form could say "wait a few s", and let the
    next try go at once, with every test green."""
    wait = boot_report["unlockWait"]
    assert wait["form"] and wait["said"] == "too many tries — try again in 60 s"


def test_the_strip_and_the_nav_show_what_the_spec_lists(boot_report: dict[str, Any]) -> None:
    """SPEC §6.3's status strip: "off in 23 min" while an auto-off is set, amber within 15
    min, Extend 1 h, a READ-ONLY pill that shows the reason when tapped, a toast when writes
    flip; and the Needs tab's count. None of it had a test."""
    strip = boot_report["statusStrip"]
    assert strip["writesOn"] == {
        "off": "off in 10 min",
        "soon": True,
        "extend": True,
        "readOnly": False,
        "needs": "2",
    }
    assert strip["writesOff"] == {
        "off": "off in 2 h",
        "soon": False,
        "extend": True,
        "readOnly": True,
        "needs": "2",
        "toast": "Writes are off — read-only",
    }
    assert strip["tapped"] == "Read-only"


def test_each_listing_screen_shows_what_the_spec_lists(boot_report: dict[str, Any]) -> None:
    """SPEC §6.3's screens: the feed's empty state; a NEEDS YOU count on a project's row, and
    a NEEDS YOU badge on an agent the feed has a card for; tasks grouped doing, review,
    blocked, todo, done; memory without what was deleted; a cleared card that says what its
    agent does now and leads back to the feed; an empty transcript; the Card tab's model.
    Each could be dropped with every test green."""
    screens = boot_report["screensListed"]
    assert screens["feed"] == "Nothing needs you."
    assert screens["projects"] == ["NEEDS YOU 2"]
    assert screens["fleet"] == ["waiting", "NEEDS YOU"]
    assert screens["tasks"] == [
        *("# doing · 1", "fix the bug"),
        *("# review · 1", "look it over | for reviewer · claimed"),
        *("# todo · 1", "write the docs"),
        *("# done · 1", "ship it"),
    ]
    assert screens["memory"] == ["kept"]
    assert screens["cleared"] == {
        "said": [
            "No longer needs you",
            "coder-1 is waiting at its prompt now.",
            "Open coder-1",
            "Back to the feed",
        ],
        "back": "#/",
    }
    assert screens["transcript"] == ["No conversation recorded yet."]
    assert screens["card"].splitlines()[:2] == ["Explainability: on", "model: claude-x"]


def test_a_board_whose_tasks_were_all_dropped_says_so_instead_of_drawing_nothing(
    boot_report: dict[str, Any],
) -> None:
    """A dropped task has no group on the Tasks tab (SPEC §6.3 groups doing, review, blocked,
    todo and done), yet the empty-state check counted it: a board of dropped tasks alone drew
    nothing under its tabs, while ``asq task list`` listed them. The tab says it has none to
    show, and how many it leaves out."""
    tasks = boot_report["droppedTasks"]
    assert tasks["all"] == ["No tasks on this board.", "Not shown: 2 dropped."]
    assert tasks["some"] == ["done · 1", "ship it", "Not shown: 1 dropped."]


def test_a_card_screen_says_its_card_cleared_every_time_it_clears(
    boot_report: dict[str, Any],
) -> None:
    """The card screen, a push link's target, said "No longer needs you" only the first time
    its card cleared. A permission card goes for a few seconds when its pane prints, or for a
    scan that failed, and comes back: when it then cleared for good, the screen was blank, no
    card, no sentence and no way back but the nav. And the cleared view's fleet read, answered
    after the card came back, put "Back to the feed" under the live card."""
    flicker = boot_report["cardFlicker"]
    cleared = ["No longer needs you", "coder-1 is waiting at its prompt now."]
    links = ["Open coder-1", "Back to the feed"]
    assert flicker["first"] == ["No longer needs you", "Asking the machine about coder-1…", *links]
    assert flicker["readLate"] == ["card"]
    assert flicker["cleared"] == flicker["again"] == [*cleared, *links]
    assert flicker["back"] == ["card"]


def test_a_refused_read_stays_said_through_the_redraws_that_follow(
    boot_report: dict[str, Any],
) -> None:
    """The Fleet tab put a refused read's sentence in its body, and the next redraw (any needs
    frame, a heartbeat, a wake, another project's fleet) said "Loading…" in its place for as
    long as the tab was open: a project the machine no longer has never said so. The Projects
    screen added the sentence without clearing, so each 15 s poll that failed added a copy,
    and a needs frame's redraw then left the screen blank."""
    kept = boot_report["failuresKept"]
    assert kept["fleet"] == [["no project matches 'prj_gone'"]] * 4
    assert kept["projects"] == [["The machine could not answer: tmux did not answer"]] * 4
    assert kept["reads"] == 3, "the polls did run"


def test_a_refused_read_of_the_feed_is_said_where_the_feed_would_be(
    boot_report: dict[str, Any],
) -> None:
    """The feed and a card screen took a refused ``GET api/needs`` in silence and said
    "Loading…" until a frame brought the feed, for good when none came: the class sweep of
    the refused reads above. They say why now, until the feed comes."""
    refused = boot_report["needsRefused"]
    sentence = "The machine could not answer: the scan failed"
    assert refused["feed"] == [[sentence, 0], ["", 1]]
    assert refused["card"] == [sentence]


def test_notifications_say_where_they_stand_wherever_the_page_offers_them(
    boot_report: dict[str, Any],
) -> None:
    """SPEC §6.3: the feed says when notifications are not on for this device and links to
    Settings; Settings says when they are on, and what a test answered "no subscription"
    means; in Safari on an iPhone it gives the Add to Home Screen steps. None had a test."""
    push = boot_report["pushScreens"]
    assert push["banner"] == ["Notifications are not on for this device.", "Settings", "Hide"]
    assert push["on"] == {
        "said": "Notifications are on for this device.",
        "test": "The machine has no subscription for this device — turn notifications on again.",
    }
    assert push["iphone"].startswith("On iPhone and iPad, notifications need the page on the")


def test_a_tapped_notification_opens_its_card_in_the_page_already_open(
    boot_report: dict[str, Any],
) -> None:
    """docs/remote.md: tapping a notification opens the card. With the page open, that is the
    worker focusing it and posting it the card's hash, and the page going there; with none
    open, or the link on another ngrok address after the domain changed, a new window. No
    test ran either end (one grepped sw.js for "postMessage"): the message's type or hash,
    the page's listener, or the worker's choice of window could each break, a tap then only
    bringing the page up on its last screen, with every test green. A link the worker may not
    open brings up the page itself, at its feed; and a push is shown under its tag."""
    tap = boot_report["notificationTap"]
    scope = "https://x.ngrok-free.app/r/" + "t" * 32 + "/"
    card = "#/n/ny_0123456789abcdef/p/prj_x/a/coder-1"
    assert tap["open"] == [
        ["close"],
        ["focus", f"{scope}#/settings"],
        ["post", f"{scope}#/settings", {"type": "open", "hash": card}],
    ]
    assert tap["landed"] == card, "the page went where the worker's message said"
    assert tap["none"] == [["close"], ["open", scope + card]]
    moved = "https://y.ngrok-free.app/r/" + "t" * 32 + "/" + card
    assert tap["moved"] == [["close"], ["open", moved]]
    assert tap["forged"][-1] == ["post", f"{scope}#/settings", {"type": "open", "hash": ""}]
    assert tap["shown"] == [["show", "x: coder-1 needs you", "asq-needs"]]


def test_the_socket_opens_under_the_pages_own_path_and_scheme(
    boot_report: dict[str, Any],
) -> None:
    """Everything is under ``/r/<token>/`` and the stream is its ``ws`` (above). A page under
    https must open ``wss:``: a ``ws://`` from it is mixed content, blocked, and the live view
    never connects through ngrok; one rooted at ``/ws`` is outside the token, a handshake the
    server answers 404. The fake browser booted only under http and kept no socket's URL, so
    either change passed every test."""
    token = "t" * 32
    urls = boot_report["socketUrls"]
    assert {key: urls[key] for key in ("http", "https")} == {
        "http": [f"ws://127.0.0.1:8750/r/{token}/ws"],
        "https": [f"wss://x.ngrok-free.app/r/{token}/ws"],
    }


def test_every_request_asks_as_the_spec_says(boot_report: dict[str, Any]) -> None:
    """SPEC §6.3: same-origin credentials (the session cookie), JSON, and the header that
    skips ngrok's warning page, without which the free plan's tunnel answers its own HTML
    and the page reads it as Remote being off. No test looked at how the page asked: the
    fake browser kept only the path and body."""
    headers = {"content-type": "application/json", "ngrok-skip-browser-warning": "1"}
    asked = {"credentials": "same-origin", "cache": "no-store", "headers": headers}
    assert boot_report["socketUrls"]["asked"] == [asked]


def test_each_write_carries_what_the_spec_says_it_sends(boot_report: dict[str, Any]) -> None:
    """SPEC §6.3's writes, as the machine received them. Send carries the ⏎ toggle; a note
    and a Reply their project, a Reply the question's author; a card's Tell mode prompt and
    the card's needs_id; a card's Switch agent_id, confirm and needs_id, the fields the
    machine's stale and double-run guards stand on. A Tell refused agent_busy offers
    Interrupt & tell, and an agent at its usage limit has Switch account first. No test
    read these fields: each could go with every test green, and the machine then refuse
    the write, post it to another project, or act on a card that no longer needs you."""
    writes = boot_report["writeBodies"]
    agent = {"agent": "coder-1", "project": "prj_x"}
    assert writes["send"] == [{**agent, "text": "hi", "enter": False}]
    assert writes["note"] == [
        {"text": "shipping now", "kind": "decision", "project": "prj_x", "to": "lead-1"}
    ]
    assert writes["reply"] == [
        {"text": "Postgres", "kind": "note", "project": "prj_x", "to": "lead-1"}
    ]
    card = {"needs_id": "ny_0123456789abcdef", "agent_id": "agt_1"}
    assert writes["tell"] == [{**agent, "text": "yes, merge", "mode": "prompt", **card}]
    assert writes["switched"] == [{**agent, "confirm": "coder-1", **card}]
    assert writes["busy"] == {"offered": True, "modes": ["auto", "interrupt"]}
    assert writes["limitedMenu"] == [
        "Switch account…",
        "Tell…",
        "Interrupt & tell…",
        "Stop…",
        "Restart…",
    ]


def test_the_key_pad_and_the_phones_keyboard_never_share_the_screen(
    boot_report: dict[str, Any],
) -> None:
    """SPEC §6.3: focusing the box closes the pad, and opening the pad takes the focus from
    the box, so the phone's keyboard goes. Neither had a test. Each step is [the pad is
    open, the box has the focus]."""
    assert boot_report["padOrKeyboard"] == [[False, True], [True, False], [False, True]]


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
    it, so only a real build proves ``pip install aisquare-cli`` serves a page. Built as
    ``python -m build`` and the release build it, the sdist first and the wheel from that,
    with hatchling, the project's own backend. It is a dev dependency, imported rather
    than skipped without it: skipped, this ran in no CI job, and a wheel without app.js
    passed every one, a blank page on every phone (sweep of #243)."""
    import importlib
    import tarfile
    import zipfile

    sdist = importlib.import_module("hatchling.builders.sdist")
    wheel = importlib.import_module("hatchling.builders.wheel")
    root = Path(__file__).resolve().parents[1]
    (built,) = sdist.SdistBuilder(str(root)).build(
        directory=str(tmp_path / "sdist"), versions=["standard"]
    )
    with tarfile.open(built) as archive:
        if hasattr(tarfile, "data_filter"):
            archive.extractall(tmp_path / "unpacked", filter="data")
        else:  # 3.11.0 to 3.11.3, which requires-python lets in: the filters came in 3.11.4
            archive.extractall(tmp_path / "unpacked")  # the sdist this test just built
    (unpacked,) = (tmp_path / "unpacked").iterdir()
    wheels = list(
        wheel.WheelBuilder(str(unpacked)).build(
            directory=str(tmp_path / "wheel"), versions=["standard"]
        )
    )

    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
    assert "aisquare/web/__init__.py" in names
    for name in (*PAGE_FILES, "__init__.py"):
        assert f"aisquare/web/remote/{name}" in names, name
