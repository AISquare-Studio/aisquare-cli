"""The phone page aisquare-cli bundles (SPEC §6): safe by construction.

The page ships inside the package (``aisquare.web.remote``): hand-written HTML,
one script, one stylesheet, a service worker and a manifest. Two layers:

* the files, read as text: every reference resolves, nothing points at another
  origin, none of the DOM sinks a server string could reach appears, and the page's
  API table names only routes the built app has;
* the page's pure core, run by node (``tests/js/remote_page_check.js``): hostile
  pane rows, runs and needs items become text and fixed elements, and nothing else.

Every static guard below has a control that feeds it the thing it forbids: a
pattern that matches nothing passes on any page.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from aisquare.services import remote_server
from aisquare.services.remote_server import (
    Runtime,
    Sources,
    build_app,
    write_endpoint_names,
)
from tests.remote_kit_helpers import make_runtime, mounted_routes

WEB = Path(str(resources.files("aisquare.web.remote")))
HARNESS = Path(__file__).resolve().parent / "js" / "remote_page_check.js"
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


@pytest.mark.xfail(
    strict=True,
    reason="needs lanes c-needs-you, d-push and e-agent-actions: api/needs*, api/push* and "
    "api/actions/recent are routes only once they merge (SPEC §6.7 item 8)",
)
def test_every_path_in_the_page_api_table_is_a_route_of_the_built_app(app: Any) -> None:
    missing = [key for key, value in _api_table().items() if not _routes_for(app, value)]
    assert missing == []


@pytest.mark.xfail(
    strict=True,
    reason="needs lane e-agent-actions: agent/tell, agent/stop, agent/restart and "
    "agent/switch join write_endpoint_names() with ACTION_ENDPOINTS",
)
def test_every_write_the_page_sends_is_one_the_dispatcher_answers() -> None:
    assert set(_writes_table()) <= set(write_endpoint_names())


def test_the_plan_writes_the_page_sends_are_the_dispatchers_already() -> None:
    """The half of the write list that is on this branch now, so it cannot rot meanwhile."""
    plan = [name for name in _writes_table() if not name.startswith("agent/")]
    assert plan == ["send-keys", "note"]
    assert set(plan) <= set(remote_server.WRITE_ENDPOINTS)


def test_the_page_sends_only_the_socket_messages_the_server_reads() -> None:
    source = _text("app.js")
    sent = set(re.findall(r'wsSend\(\s*"([a-z_]+)"', source))
    table = re.findall(r'"([a-z_]+)"', _js_table("SOCKET_MESSAGES", r"\[", r"\]"))
    server = set(re.findall(r'message\.get\("([a-z_]+)"', Path(remote_server.__file__).read_text()))

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


# --- 10. the service worker -----------------------------------------------------------------

NGROK_SUFFIXES = (".ngrok-free.app", ".ngrok.app", ".ngrok.io", ".ngrok-free.dev", ".ngrok.dev")


def test_the_worker_shows_pushes_opens_cards_and_never_serves_from_a_cache() -> None:
    source = _text("sw.js")
    listeners = set(re.findall(r'addEventListener\(\s*"([a-z]+)"', source))

    assert {"push", "notificationclick"} <= listeners
    assert "fetch" not in listeners, "a fetch handler would let the page go stale"
    assert "renotify" not in source, "Safari ignores it; every push carries a fresh title"
    for suffix in NGROK_SUFFIXES:
        assert f'"{suffix}"' in source, suffix
    assert "postMessage" in source and "openWindow" in source


# --- 7 and 10. the pure core and safeUrl under node ----------------------------------------

ALLOWED_ELEMENTS = frozenset(
    {"div", "span", "pre", "p", "h2", "h3", "ul", "li", "button", "details", "summary"}
)
_RGB = re.compile(r"rgb\((\d{1,3}), (\d{1,3}), (\d{1,3})\)")
_BIDI = "؜‎‏‪‫‬‭‮⁦⁧⁨⁩"


@pytest.fixture(scope="module")
def node_report() -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH; the page's runtime check needs it")
    result = subprocess.run(
        [node, str(HARNESS)], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    report: dict[str, Any] = json.loads(result.stdout)
    return report


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


def test_the_python_reading_of_the_tables_is_what_the_script_holds(
    node_report: dict[str, Any],
) -> None:
    """The route and write tests parse the tables as text; node runs them. They agree."""
    assert node_report["api"] == _api_table()
    assert node_report["writes"] == _writes_table()
    assert {"ansiToRuns", "renderRuns", "renderNeedsCard"} <= set(node_report["exports"])


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
