"""A loopback HTTP server for what the user machine downloads and the browser loads.

It serves the terminal client page and its vendored files, plus — for the ``main``
track — ``install.sh`` and the wheel under test, so the user types the SAME
``curl … | sh`` one-liner the README gives, pointed at this tree instead of PyPI.
The wheel is served over HTTP rather than as a ``file://`` path because the doctor's
provenance check warns when an install's source file later disappears.
"""

from __future__ import annotations

import http.server
import threading
from pathlib import Path
from typing import ClassVar


class _Handler(http.server.SimpleHTTPRequestHandler):
    routes: ClassVar[dict[str, Path]] = {}

    def translate_path(self, path: str) -> str:
        """Map a URL to a file through ``routes`` only; anything else is a 404."""
        url_path = path.split("?", 1)[0].split("#", 1)[0]
        for prefix, root in self.routes.items():
            if prefix.endswith("/") and url_path.startswith(prefix):
                base = root.resolve()
                target = (base / url_path[len(prefix) :]).resolve()
                if target == base or base in target.parents:
                    return str(target)
            elif url_path == prefix:
                return str(root.resolve())
        return "/nonexistent-route"

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class StaticServer:
    """Serve ``routes``: ``{"/vendor/": dir, "/install.sh": file}``; a trailing slash = a dir."""

    def __init__(self, routes: dict[str, Path], host: str = "127.0.0.1") -> None:
        handler = type("Handler", (_Handler,), {"routes": dict(routes)})
        self.host = host
        self._httpd = http.server.ThreadingHTTPServer((host, 0), handler)
        self.port: int = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> StaticServer:
        self._thread.start()
        return self

    def url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
