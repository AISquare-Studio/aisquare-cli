"""The phone page that ships inside aisquare-cli, served when no other page is installed.

``asq remote serve --dist DIR`` serves DIR, and a build ``asq remote install-page``
copied to ``~/.aisquare/remote-dist`` overrides this one; otherwise the server
answers from the files of the ``aisquare.web.remote`` package: one hand-written
HTML page, one script, one stylesheet, the service worker, the manifest and two
icons, with no build step and nothing fetched from anywhere else (SPEC §6).
``remote_server`` decides per request which of the three a request gets; this
module knows only the bundled page.

Every page response carries :func:`remote_page_headers`. The bundled page also
carries :data:`PAGE_CSP`, which lets nothing but this origin load, run or connect:
the markup holds no inline script and no inline style, so the policy needs no
``unsafe-`` anything, and a server string that ever reached the page as markup
would still run nothing. An installed build may need a policy of its own, so it
gets none from here.

The token is in the path, so the page must never leak its URL: no referrer, no
framing, and no URL in it that points anywhere but here.
"""

from __future__ import annotations

import functools
import hashlib
import mimetypes
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

PAGE_PACKAGE = "aisquare.web.remote"
"""The package whose files are the bundled page (``importlib.resources``)."""

PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
"""The bundled page's Content-Security-Policy: this origin only, for everything.

``connect-src 'self'`` covers the same host's ``wss:`` in Chrome, Firefox and
Safari 15.4 on (iOS push needs 16.4 anyway). ``base-uri 'none'`` is why the page
uses relative URLs and no ``<base>``: it is served at ``/r/<token>/``."""

PAGE_CACHE_CONTROL = "no-cache"
"""Revalidate every file on every load, answered 304 by ETag when it has not changed.

The names carry no content hash, so a file kept for any length of time without
asking would be a page from a previous aisquare-cli talking to this one's server."""

_PAGE_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def page_content_type(name: str) -> str | None:
    """The ``Content-Type`` a page file is served with; ``None``: not served at all.

    A closed list rather than ``mimetypes``, whose answers come from the machine's
    own tables (on Windows, the registry, where ``.js`` can be ``text/plain``), and
    under ``nosniff`` a browser refuses to run a script served with the wrong type.
    """
    return _PAGE_TYPES.get(PurePosixPath(name).suffix.lower())


_BUILD_TYPES = {
    **_PAGE_TYPES,
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".wasm": "application/wasm",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".txt": "text/plain; charset=utf-8",
}


@functools.cache
def _python_types() -> mimetypes.MimeTypes:
    """Python's own table of types: a ``MimeTypes()`` reads none of the machine's files,
    and not its registry, which only ``mimetypes.init`` reads into the module's table."""
    return mimetypes.MimeTypes()


def build_content_type(name: str) -> str:
    """The ``Content-Type`` a file of an installed build (``install-page``, ``--dist``) is
    served with: the page's own types and the rest of what a web build holds from the same
    kind of closed list, any other from Python's own table, else ``application/octet-stream``.

    Typed by ``mimetypes``, as starlette types a file, an installed page's scripts took what
    the machine's tables said: under a Windows registry that maps ``.js`` to ``text/plain``
    they were served so, with ``nosniff``, and the browser refused every one of them.
    """
    suffix = PurePosixPath(name).suffix.lower()
    known = _BUILD_TYPES.get(suffix) or _python_types().guess_type(f"file{suffix}")[0]
    return known or "application/octet-stream"


def remote_page_headers() -> dict[str, str]:
    """What every page response carries, the bundled page's and an installed build's alike.

    ``Referrer-Policy`` matters most: the token is in the path, and a page that
    linked out with a referrer would hand the link to whoever it linked to.
    """
    return {
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "x-frame-options": "DENY",
        "permissions-policy": "camera=(), microphone=(), geolocation=()",
    }


@functools.cache
def _bundled_items() -> tuple[tuple[str, bytes], ...]:
    """The package's files, read once per process: the page never changes under a server."""
    from importlib import resources

    try:
        entries = list(resources.files(PAGE_PACKAGE).iterdir())
    except (ModuleNotFoundError, OSError):  # a broken install: the caller says so
        return ()
    return tuple(
        sorted(
            (entry.name, entry.read_bytes())
            for entry in entries
            if entry.is_file() and not entry.name.startswith(".") and entry.name != "__init__.py"
        )
    )


def bundled_page_files() -> dict[str, bytes]:
    """Every file of the bundled page by name, ``__init__.py`` and dotfiles left out."""
    return dict(_bundled_items())


def bundled_page_present() -> bool:
    """Whether this install carries the bundled page at all (its ``index.html``)."""
    return "index.html" in bundled_page_files()


def _page_etag(body: bytes) -> str:
    return '"' + hashlib.blake2b(body, digest_size=16).hexdigest() + '"'


def _page_not_modified(request: Request, etag: str) -> bool:
    """Whether the browser's ``If-None-Match`` already names this version."""
    header = request.headers.get("if-none-match", "")
    if header.strip() == "*":
        return True
    return etag in {tag.strip().removeprefix("W/") for tag in header.split(",")}


def bundled_page_response(rel: str, request: Request) -> Response | None:
    """One file of the bundled page, served from memory; ``None`` when this install has none.

    ``rel`` is the path below ``/r/<token>/``. The page routes by hash, so the
    document is only ever asked for at the top; any other name without an
    extension is a navigation and gets the document too. A path with a ``/`` in
    it (``fleet/coder-1``, a bookmark of an older page) is sent to the top
    instead: the document's relative URLs would resolve below it and load
    nothing. A name with an extension that the page does not have is a 404,
    never the document: a script answered with HTML fails to boot and says
    nothing about why.
    """
    from starlette.responses import JSONResponse, RedirectResponse
    from starlette.responses import Response as PlainResponse

    files = bundled_page_files()
    if "index.html" not in files:
        return None
    headers = remote_page_headers()
    name = rel or "index.html"
    if name not in files:
        if PurePosixPath(rel).suffix:
            missing = {"error": "not_found", "message": f"no such file in the bundled page: {rel}"}
            return JSONResponse(missing, status_code=404, headers=headers)
        if "/" in rel:
            token = request.path_params.get("token", "")
            return RedirectResponse(f"/r/{token}/", status_code=307, headers=headers)
        name = "index.html"
    content_type = page_content_type(name)
    if content_type is None:  # a file the page carries but no browser needs
        unserved = {"error": "not_found", "message": f"not served: {rel}"}
        return JSONResponse(unserved, status_code=404, headers=headers)
    body = files[name]
    etag = _page_etag(body)
    headers.update(
        {"etag": etag, "cache-control": PAGE_CACHE_CONTROL, "content-security-policy": PAGE_CSP}
    )
    if _page_not_modified(request, etag):
        return PlainResponse(status_code=304, headers=headers)
    return PlainResponse(body, headers=headers, media_type=content_type)


__all__ = [
    "PAGE_CACHE_CONTROL",
    "PAGE_CSP",
    "PAGE_PACKAGE",
    "build_content_type",
    "bundled_page_files",
    "bundled_page_present",
    "bundled_page_response",
    "page_content_type",
    "remote_page_headers",
]
