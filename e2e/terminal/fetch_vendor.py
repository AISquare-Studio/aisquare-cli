"""Fetch the terminal client's third-party files, pinned and verified, into ``vendor/``.

The client page is a real terminal emulator (xterm.js, the engine inside VS Code's
and Cursor's terminals). It is VENDORED rather than loaded from a CDN so a nightly
run is byte-for-byte the same terminal every night and works offline, and so a
screenshot that changed means the PRODUCT changed, not a CDN's ``latest``.

Why 6.0.0 and not the 5.5 that ttyd's own page bundles: xterm.js 6 implements DEC
mode 2026 (synchronized output). Textual 8 brackets each frame in BSU/ESU only when
the terminal answers that it supports 2026; without it a screenshot can catch a
frame half painted. The instrumented client counts those brackets to know when a
frame is complete.

Each npm tarball is checked against the ``dist.integrity`` (sha512) npm publishes;
each extracted file is then recorded in ``vendor/SHA256SUMS`` so CI can verify the
committed copies without network access (``--verify``).

    python e2e/terminal/fetch_vendor.py           # fetch + write SHA256SUMS
    python e2e/terminal/fetch_vendor.py --verify  # check committed files only
"""

from __future__ import annotations

import base64
import hashlib
import io
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VENDOR = Path(__file__).resolve().parent / "vendor"

#: (package, version, sha512 integrity from `npm view <pkg>@<ver> dist.integrity`, files to extract)
NPM: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "@xterm/xterm",
        "6.0.0",
        "sha512-TQwDdQGtwwDt+2cgKDLn0IRaSxYu1tSUjgKarSDkUM0ZNiSRXFpjxEsvc/Zgc5kq5omJ+V0a8/kIM2WD3sMOYg==",
        (("package/lib/xterm.js", "xterm.js"), ("package/css/xterm.css", "xterm.css")),
    ),
    (
        "@xterm/addon-unicode11",
        "0.9.0",
        "sha512-FxDnYcyuXhNl+XSqGZL/t0U9eiNb/q3EWT5rYkQT/zuig8Gz/VagnQANKHdDWFM2lTMk9ly0EFQxxxtZUoRetw==",
        (("package/lib/addon-unicode11.js", "addon-unicode11.js"),),
    ),
)

#: JetBrains Mono (SIL OFL 1.1): one release zip, the two web weights the client uses.
FONT_ZIP = (
    "https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip"
)
FONT_ZIP_SHA256 = "6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf"
FONT_FILES = (
    ("fonts/webfonts/JetBrainsMono-Regular.woff2", "JetBrainsMono-Regular.woff2"),
    ("fonts/webfonts/JetBrainsMono-Bold.woff2", "JetBrainsMono-Bold.woff2"),
)


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        data: bytes = response.read()
    return data


def _npm_tarball(package: str, version: str) -> str:
    name = package.split("/")[-1]
    return f"https://registry.npmjs.org/{package}/-/{name}-{version}.tgz"


def fetch() -> None:
    VENDOR.mkdir(parents=True, exist_ok=True)
    for package, version, integrity, files in NPM:
        blob = _get(_npm_tarball(package, version))
        digest = "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()
        if digest != integrity:
            sys.exit(
                f"{package}@{version}: integrity mismatch\n  want {integrity}\n  got  {digest}"
            )
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member, target in files:
                extracted = tar.extractfile(member)
                if extracted is None:
                    sys.exit(f"{package}@{version}: {member} missing from the tarball")
                (VENDOR / target).write_bytes(extracted.read())
        print(f"✓ {package}@{version}")
    archive = _get(FONT_ZIP)
    got = hashlib.sha256(archive).hexdigest()
    if got != FONT_ZIP_SHA256:
        sys.exit(f"JetBrainsMono zip: sha256 mismatch\n  want {FONT_ZIP_SHA256}\n  got  {got}")
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        for member, target in FONT_FILES:
            (VENDOR / target).write_bytes(zf.read(member))
    print("✓ JetBrains Mono 2.304 (Regular, Bold)")
    lines = []
    for path in sorted(p for p in VENDOR.iterdir() if p.is_file() and p.name != "SHA256SUMS"):
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (VENDOR / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ wrote {VENDOR / 'SHA256SUMS'}")


def verify() -> int:
    sums = VENDOR / "SHA256SUMS"
    if not sums.is_file():
        print("vendor/SHA256SUMS missing — run without --verify first", file=sys.stderr)
        return 1
    bad = 0
    for line in sums.read_text(encoding="utf-8").splitlines():
        want, name = line.split("  ", 1)
        path = VENDOR / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != want:
            print(f"✗ {name}", file=sys.stderr)
            bad += 1
    print("vendor verified" if not bad else f"{bad} vendored file(s) do not match SHA256SUMS")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--verify" in sys.argv[1:]:
        sys.exit(verify())
    fetch()
