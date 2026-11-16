"""Build the package under both of its names, and refuse to finish unless they are one (#137).

Every release is published to PyPI as ``rlx`` and, the same build, as ``aisquare-cli``, so
an install made before the move keeps upgrading under its own name. This builds the sdist
and the wheel of each from THIS tree: ``rlx`` as pyproject names it, into ``<out>/rlx/``, and
``aisquare-cli`` from a copy of the tracked files whose ``[project].name`` alone is
rewritten, into ``<out>/aisquare-cli/``. Then it checks:

- each folder holds one wheel and one sdist of its name;
- both versions are pyproject's, and the tag's when ``--tag`` is given;
- the two wheels hold the same files with the same contents outside their ``.dist-info``;
- their METADATA differs in the ``Name`` alone, and their entry points not at all.

publish.yml uploads ``<out>/rlx/`` first, then ``<out>/aisquare-cli/``; ci.yml builds and
checks them on every pull request. Nothing here uploads anything.

    python scripts/build_dists.py [--out dist] [--tag vX.Y.Z]
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("rlx", "aisquare-cli")
"""The names, in the order they are published: rlx first, then the alias of the same build."""

_PROJECT_NAME = re.compile(r'^name = "([^"]+)"$', re.M)


def renamed(pyproject: str, name: str) -> str:
    """``pyproject`` with ``[project].name`` set to ``name``: the one line, nothing else."""
    project = pyproject.index("[project]\n")
    following = pyproject.find("\n[", project + 1)
    end = len(pyproject) if following == -1 else following
    section = pyproject[project:end]
    lines = _PROJECT_NAME.findall(section)
    if len(lines) != 1:
        raise SystemExit(f"[project] must hold exactly one name line, found {len(lines)}")
    section = _PROJECT_NAME.sub(f'name = "{name}"', section, count=1)
    return pyproject[:project] + section + pyproject[end:]


def _tracked_copy(destination: Path) -> None:
    """The files git tracks in this tree, copied to ``destination`` as they are on disk."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.split(b"\0")
    for raw in listed:
        if not raw:
            continue
        relative = Path(raw.decode())
        source = ROOT / relative
        if not source.is_file():
            continue  # deleted in the working tree
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _build(source: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(out), str(source)],
        check=True,
    )


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _one(folder: Path, name: str, suffix: str) -> Path:
    found = sorted(folder.glob(f"{_normal(name)}-*{suffix}"))
    others = sorted(set(folder.glob(f"*{suffix}")) - set(found))
    if len(found) != 1 or others:
        raise SystemExit(
            f"{folder} must hold one {name} {suffix} and nothing else: {found + others}"
        )
    return found[0]


def _files(wheel: Path) -> tuple[dict[str, str], dict[str, bytes]]:
    """``(the hash of each file outside .dist-info, each .dist-info file by its base name)``."""
    contents: dict[str, str] = {}
    info: dict[str, bytes] = {}
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.namelist():
            data = archive.read(member)
            top = member.split("/", 1)[0]
            if top.endswith(".dist-info"):
                info[member.split("/", 1)[1]] = data
            else:
                contents[member] = hashlib.sha256(data).hexdigest()
    return contents, info


def compare(first: Path, second: Path) -> list[str]:
    """What makes the two wheels more than one package under two names; ``[]`` when nothing."""
    problems: list[str] = []
    files_a, info_a = _files(first)
    files_b, info_b = _files(second)
    for member in sorted(files_a.keys() ^ files_b.keys()):
        problems.append(f"{member} is in only one of the wheels")
    for member in sorted(files_a.keys() & files_b.keys()):
        if files_a[member] != files_b[member]:
            problems.append(f"{member} differs between the wheels")
    parser = email.parser.BytesHeaderParser()
    meta_a = parser.parsebytes(info_a.get("METADATA", b""))
    meta_b = parser.parsebytes(info_b.get("METADATA", b""))
    headers_a = [(key, value) for key, value in meta_a.items() if key != "Name"]
    headers_b = [(key, value) for key, value in meta_b.items() if key != "Name"]
    if headers_a != headers_b:
        problems.append("their METADATA differs in more than the Name")
    if meta_a.get_payload() != meta_b.get_payload():
        problems.append("their long descriptions differ")
    if info_a.get("entry_points.txt") != info_b.get("entry_points.txt"):
        problems.append("their entry points differ")
    return problems


def _version_of(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        member = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = email.parser.BytesHeaderParser().parsebytes(archive.read(member))
    return str(metadata["Version"])


def check(out: Path, tag: str | None) -> list[str]:
    """Every problem with ``out``'s two builds, as :func:`compare` and the versions say."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    wheels = {name: _one(out / name, name, ".whl") for name in NAMES}
    for name in NAMES:
        _one(out / name, name, ".tar.gz")
    problems: list[str] = []
    for name, wheel in wheels.items():
        found = _version_of(wheel)
        if found != declared:
            problems.append(f"{name} {found} is not pyproject's {declared}")
        if tag is not None and tag.removeprefix("v") != found:
            problems.append(f"{name} {found} is not the tag's {tag}")
    problems.extend(compare(wheels[NAMES[0]], wheels[NAMES[1]]))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--tag", help="the release's tag, vX.Y.Z: both versions must equal it")
    options = parser.parse_args(argv)
    out: Path = options.out.resolve()
    for name in NAMES:
        shutil.rmtree(out / name, ignore_errors=True)
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    named = _PROJECT_NAME.search(pyproject[pyproject.index("[project]\n") :])
    if named is None or named.group(1) != NAMES[0]:
        raise SystemExit(f"pyproject.toml must name the project {NAMES[0]}")
    _build(ROOT, out / NAMES[0])
    with tempfile.TemporaryDirectory(prefix="build-dists-") as scratch:
        copy = Path(scratch) / "tree"
        _tracked_copy(copy)
        (copy / "pyproject.toml").write_text(renamed(pyproject, NAMES[1]), encoding="utf-8")
        _build(copy, out / NAMES[1])
    problems = check(out, options.tag)
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"built {', '.join(NAMES)}: one package under two names, in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
