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
from aisquare.services import remote_page, remote_server
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

#: SPEC §6.6: the only attribute names the page may set, each as a literal.
ALLOWED_ATTRIBUTES = frozenset(
    {
        "aria-label",
        "aria-hidden",
        "aria-live",
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
_HASH_WRITE = re.compile(r"location\s*\.\s*hash\s*=(?!=)")


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


def _hash_writes_outside_page_go(source: str) -> tuple[int, list[int]]:
    """(writes inside ``pageGo``, offsets of any outside it)."""
    start = source.index("function pageGo(")
    end = source.index("\n}\n", start)
    inside = [m.start() for m in _HASH_WRITE.finditer(source) if start < m.start() < end]
    outside = [m.start() for m in _HASH_WRITE.finditer(source) if not start < m.start() < end]
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


def test_location_hash_is_set_in_page_go_and_nowhere_else() -> None:
    inside, outside = _hash_writes_outside_page_go(_text("app.js"))
    assert inside == 1, "pageGo is THE place the page navigates"
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
    assert _hash_writes_outside_page_go(elsewhere)[1] != []


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
    "app.js": 110 * 1024,
    "sw.js": 4 * 1024,
    "manifest.webmanifest": 1024,
}


def test_each_file_and_the_whole_page_fit_their_budgets() -> None:
    for name, budget in BUDGETS.items():
        size = (WEB / name).stat().st_size
        assert size <= budget, f"{name} is {size} bytes, over {budget}"
    icons = sum((WEB / name).stat().st_size for name in ("icon.svg", "icon-180.png"))
    assert icons <= 10 * 1024
    total = sum((WEB / name).stat().st_size for name in PAGE_FILES)
    assert total <= 150 * 1024


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
    repeats = ("question", "questions", "cutQuestion", "permission", "plan", "dialog")
    assert {name: shown[name] for name in repeats} == {name: [] for name in repeats}
    assert shown["otherQuestion"] == ["Pick one before the release"]
    assert shown["bareTool"] == ["Bash(pytest -q tests/test_cache.py)"]


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
