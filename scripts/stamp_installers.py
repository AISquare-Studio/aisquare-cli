#!/usr/bin/env python3
"""Stamp install.sh and install.ps1 to one release, for upload as its assets.

    python scripts/stamp_installers.py --tag v0.9.0 --out dist-installers

Run by .github/workflows/installers.yml on every published release, and by the
tests. The repository's copies install the LATEST release; the stamped copies
install exactly the tagged one:

* install.sh: ``STAMPED_VERSION=""`` becomes ``STAMPED_VERSION="0.9.0"``, the
  default pin. ``--version V`` and ``AISQUARE_INSTALL_VERSION=V`` still win.
* install.ps1: ``$StampedVersion`` gets the version, which it passes on as
  ``--version``, and ``$InstallUrl`` names that release's install.sh asset
  instead of main's.

Every replacement must match exactly once: a stamp line that a later edit
renamed or duplicated fails here, before upload, rather than shipping an asset
that silently installs the latest. The tag must also be the version
pyproject.toml builds, since that is what PyPI was handed.

Standard library only: the release workflow runs it before installing anything.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GITHUB_REPO = "AISquare-Studio/aisquare-cli"

#: The assets every release carries, in upload order.
ASSETS = ("install.sh", "install.ps1")

#: A release version, and nothing a shell or PowerShell string could act on:
#: the version is written inside quotes in both scripts.
VERSION = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?")

SH_STAMP = 'STAMPED_VERSION=""'
PS1_STAMP = "$StampedVersion = ''"
PS1_URL = '$InstallUrl = "https://raw.githubusercontent.com/$Repo/main/install.sh"'


class StampError(Exception):
    """The stamp cannot be applied; the message says why."""


def version_of(tag: str) -> str:
    """``v0.9.0`` -> ``0.9.0``; refuses anything that is not a release tag."""
    version = tag.removeprefix("v")
    if not tag.startswith("v") or not VERSION.fullmatch(version):
        raise StampError(f"{tag!r} is not a release tag like v0.9.0")
    return version


def release_url(tag: str, asset: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{asset}"


def _replace_once(text: str, old: str, new: str, name: str) -> str:
    found = text.count(old)
    if found != 1:
        raise StampError(f"{name}: expected the line {old!r} once, found it {found} times")
    return text.replace(old, new)


def stamp_sh(text: str, tag: str) -> str:
    version = version_of(tag)
    return _replace_once(text, SH_STAMP, f'STAMPED_VERSION="{version}"', "install.sh")


def stamp_ps1(text: str, tag: str) -> str:
    version = version_of(tag)
    text = _replace_once(text, PS1_STAMP, f"$StampedVersion = '{version}'", "install.ps1")
    url = release_url(tag, "install.sh").replace(GITHUB_REPO, "$Repo")
    return _replace_once(text, PS1_URL, f'$InstallUrl = "{url}"', "install.ps1")


def pyproject_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version: str = data["project"]["version"]
    return version


def stamp(root: Path, tag: str, out: Path) -> list[Path]:
    """Write both stamped scripts into ``out``; returns their paths."""
    version = version_of(tag)
    built = pyproject_version(root)
    if built != version:
        raise StampError(
            f"tag {tag} does not match pyproject.toml's version {built}: PyPI got {built}, "
            f"so an installer pinned to {version} would install something else"
        )
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for asset, stamper in zip(ASSETS, (stamp_sh, stamp_ps1), strict=True):
        source = root / asset
        # Bytes in and out: no newline translation, so the stamped file differs
        # from the repository's only in the stamped lines.
        text = stamper(source.read_bytes().decode("utf-8"), tag)
        target = out / asset
        target.write_bytes(text.encode("utf-8"))
        target.chmod(source.stat().st_mode & 0o777)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="the release tag, e.g. v0.9.0")
    parser.add_argument("--out", required=True, type=Path, help="directory to write into")
    parser.add_argument("--root", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        written = stamp(args.root, args.tag, args.out)
    except StampError as error:
        print(f"stamp_installers: {error}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
