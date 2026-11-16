"""Every link to this repository names it as it is now: AISquare-Studio/rlx.

The owner renamed the repository from aisquare-cli to rlx on 2026-11-16 (RLX R4). GitHub
redirects every old URL, so a link that kept the old name still works, and that is exactly
why nothing else would notice one coming back: a copied install line, a manifest's
homepage, a test fixture. This guard is that notice.

The old slug is read in any case. It stays in the few lines ``KEPT`` names, each by its
exact text and with why; a kept line that no longer exists is reported too, so the list
cannot outlive its reasons.

What is NOT the repository and so is not read here: the PyPI package ``aisquare-cli`` (its
own move is RLX R5), the Codex and Claude Code marketplace ``aisquare-cli`` and the plugin
id ``aisquare@aisquare-cli`` (users have them enabled under those names, and a local probe
of Codex 0.164.0 named the marketplace from ``marketplace.json``, not from the repository),
and AISquare's other repositories (``AISquare-Studio-BE``, ``aisquare-ci``).

The slug is assembled from its parts below, so this file never holds it and is swept like
any other (CONTRIBUTING, "Writing a guard that still guards": the rule is a callable that
the controls reach with known input).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

OWNER = "AISquare-Studio"
OLD = f"{OWNER}/" + "aisquare-cli"
NEW = f"{OWNER}/rlx"
_OLD = re.compile(re.escape(OLD), re.IGNORECASE)

#: Lines that keep the old name, by file and exact text (stripped), with why.
KEPT: dict[tuple[str, str], str] = {
    ("CHANGELOG.md", f"`https://github.com/{OLD}/releases/download/v0.9.0/install.sh`"): (
        "0.9.0's release notes, as that release shipped them; GitHub redirects the URL"
    ),
    ("CHANGELOG.md", f"`/plugin marketplace add {OLD}`, then"): (
        "an old release's notes, as it shipped them; the marketplace's name is unchanged"
    ),
}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO / name for name in out.split("\0") if name]


def _texts() -> dict[str, str]:
    """Every tracked file that reads as UTF-8 text, by its path in the repository."""
    texts: dict[str, str] = {}
    for path in _tracked_files():
        try:
            texts[path.relative_to(REPO).as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return texts


def _old_links(texts: Mapping[str, str], kept: Mapping[tuple[str, str], str]) -> list[str]:
    """Each line that names the repository by its old name, outside ``kept``. A kept entry
    excuses ONE line, the first that holds its text: a copy of it elsewhere is reported."""
    excused: set[tuple[str, str]] = set()
    found: list[str] = []
    for where, text in texts.items():
        for number, line in enumerate(text.splitlines(), start=1):
            if not _OLD.search(line):
                continue
            key = (where, line.strip())
            if key in kept and key not in excused:
                excused.add(key)
                continue
            found.append(f"{where}:{number}: {line.strip()[:160]}")
    return found


def _stale_kept(texts: Mapping[str, str], kept: Mapping[tuple[str, str], str]) -> list[str]:
    """Each kept line that its file no longer holds."""
    return sorted(
        f"{where}: {line}"
        for where, line in kept
        if line not in {held.strip() for held in texts.get(where, "").splitlines()}
    )


# --- the repository ----------------------------------------------------------------------------


def test_no_link_names_the_repository_by_its_old_name() -> None:
    offending = _old_links(_texts(), KEPT)
    assert not offending, (
        f"these lines name the repository {OLD}; it is {NEW} since 2026-11-16. Write "
        f"{NEW}, or, for history quoted as it was, add the line to KEPT with why:\n  "
        + "\n  ".join(offending)
    )


def test_every_kept_line_is_still_there() -> None:
    stale = _stale_kept(_texts(), KEPT)
    assert not stale, f"KEPT excuses lines no file holds any longer: {stale}"


def test_the_sweep_sees_the_new_name_where_people_meet_it() -> None:
    """Guard the guard: the files a user copies a link from must be read, and say rlx."""
    texts = _texts()
    assert len(texts) > 500, f"only {len(texts)} files read: the sweep is broken, not the tree"
    for where in ("README.md", "install.sh", "install.ps1", "pyproject.toml", "SECURITY.md"):
        assert NEW in texts.get(where, ""), f"{where} was not read, or no longer links {NEW}"
    assert any(_OLD.search(line) for (_where, line) in KEPT), "KEPT holds the old name"


# --- controls: synthetic input, so they keep controlling whatever the tree says -----------------


def test_the_old_name_is_caught_in_every_shape_and_any_case() -> None:
    shapes = (
        f"curl -fsSL https://raw.githubusercontent.com/{OLD}/main/install.sh | sh",
        f"https://github.com/{OLD}/releases/download/v0.10.0/install.sh",
        f"git@github.com:{OLD}.git",
        f'"repository": "https://github.com/{OLD}"',
        f"codex plugin marketplace add {OLD}",
        f"github.com/{OLD.lower()}",
        f"{OLD.upper()}@main",
    )
    for line in shapes:
        assert _old_links({"x.md": line}, {}) == [f"x.md:1: {line}"], line


def test_other_names_and_kept_lines_are_not_accused() -> None:
    innocent = "\n".join(
        (
            f"curl -fsSL https://raw.githubusercontent.com/{NEW}/main/install.sh | sh",
            "uv tool install aisquare-cli",
            "https://pypi.org/project/aisquare-cli/",
            "codex plugin add aisquare@aisquare-cli",
            "codex plugin marketplace remove aisquare-cli",
            f'"project_ref": "{OWNER}/aisquare-ci@worktree/kernel"',
            f"{OWNER}-BE#3493",
        )
    )
    assert _old_links({"x.md": innocent}, {}) == []
    history = f"  `https://github.com/{OLD}/x`  "
    assert _old_links({"x.md": history}, {("x.md", history.strip()): "why"}) == []
    assert _old_links({"y.md": history}, {("x.md", history.strip()): "why"}), (
        "a kept line excuses only its own file"
    )
    twice = f"{history}\nnew text\n{history}"
    assert _old_links({"x.md": twice}, {("x.md", history.strip()): "why"}) == [
        f"x.md:3: {history.strip()}"
    ], "a kept line excuses one line, not every copy of it"


def test_a_kept_line_that_is_gone_is_reported() -> None:
    kept = {("x.md", "old line"): "why", ("y.md", "gone"): "why"}
    assert _stale_kept({"x.md": "  old line  \nmore", "y.md": "other"}, kept) == ["y.md: gone"]
    assert _stale_kept({}, kept) == ["x.md: old line", "y.md: gone"]
