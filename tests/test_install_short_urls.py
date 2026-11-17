"""What a person is told to type installs through the short URLs; what the code fetches stays.

RLX R10 (the owner, 2026-11-16): the one-liners read ``curl -fsSL https://aisquare.studio/rlx/
install.sh | sh`` and ``irm https://aisquare.studio/rlx/install.ps1 | iex``. Each short URL is a
307 redirect (AISquare-Homepage-Next #190) to the file of the same name on this repository's
``main``, raw. Only what a PERSON types moves: the scripts' own fetches (install.ps1's
``$InstallUrl``, install.sh's hand-off to a release) and every pinned release URL stay on
GitHub, since a versioned URL is not redirected.

Nothing here fetches anything: the short URLs answer 404 until #190 deploys, and CI must not
depend on that. So this file checks the text alone:

- ``_raw_one_liners`` — no line a person reads or a program prints writes the one-liner with
  the raw GitHub URL (tests are left out: their controls hold that form on purpose, and so are
  the workflows, which are CI's own fetches);
- the short URLs' redirect targets are files of this repository;
- the scripts still fetch from GitHub, and print the short form exactly where they did.

The rule is a callable the controls below reach with known input (CONTRIBUTING, "Writing a
guard that still guards").
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

from aisquare.services import install_route

REPO = Path(__file__).resolve().parents[1]


def _load_stamper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "stamp_installers_short", REPO / "scripts" / "stamp_installers.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stamper = _load_stamper()

#: The one-liner with the raw GitHub URL of main's installer: what a person no longer types.
_RAW_ONE_LINER = re.compile(
    r"(?:\bcurl\b[^|\n]*?|\birm)\s+['\"]?https://raw\.githubusercontent\.com/"
    r"AISquare-Studio/rlx/(?:refs/heads/)?main/install\.(?:sh|ps1)\b"
)
#: Where the rule does not look: the tests' controls, and CI's own fetches.
_NOT_READ = ("tests/", ".github/workflows/")


def _tracked_texts() -> dict[str, str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, text=True, check=True
    ).stdout
    texts: dict[str, str] = {}
    for name in filter(None, out.split("\0")):
        if name.startswith(_NOT_READ):
            continue
        try:
            texts[name] = (REPO / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return texts


def _raw_one_liners(texts: Mapping[str, str]) -> list[str]:
    """Each line that writes the install one-liner with the raw GitHub URL."""
    return [
        f"{where}:{number}: {line.strip()[:160]}"
        for where, text in texts.items()
        for number, line in enumerate(text.splitlines(), start=1)
        if _RAW_ONE_LINER.search(line)
    ]


# --- the repository ----------------------------------------------------------------------------


def test_no_one_liner_a_person_reads_names_the_raw_github_url() -> None:
    texts = _tracked_texts()
    assert len(texts) > 300 and "README.md" in texts and "install.sh" in texts, (
        "the sweep went blind"
    )
    offending = _raw_one_liners(texts)
    assert not offending, (
        f"write the one-liner with the short URL ({stamper.SH_ONE_LINER}, "
        f"{stamper.PS1_ONE_LINER}):\n  " + "\n  ".join(offending)
    )


def test_the_short_urls_are_printed_where_a_person_reads_them() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    sh = (REPO / "install.sh").read_text(encoding="utf-8")
    ps1 = (REPO / "install.ps1").read_text(encoding="utf-8")
    assert stamper.SH_ONE_LINER in readme and stamper.PS1_ONE_LINER in readme
    assert sh.count(stamper.SH_ONE_LINER) == 2, "install.sh's header and --help"
    assert ps1.count(stamper.PS1_ONE_LINER) == 1, "install.ps1's header"
    assert install_route.INSTALLER_ONE_LINER == stamper.SH_ONE_LINER, "the repair hint"


def test_each_short_url_redirects_to_a_file_of_this_repository() -> None:
    """The 307's target is RAW_MAIN/<asset>: that file must exist here, under that name."""
    for asset in stamper.ASSETS:
        short = f"{stamper.SHORT}/{asset}"
        target = f"{stamper.RAW_MAIN}/{asset}"
        assert short.rsplit("/", 1)[1] == target.rsplit("/", 1)[1] == asset
        assert (REPO / asset).is_file(), f"{target} would 404: no {asset} in the repository"
    assert stamper.SHORT == "https://aisquare.studio/rlx"
    raw_main = f"https://raw.githubusercontent.com/{stamper.GITHUB_REPO}/main"
    assert raw_main == stamper.RAW_MAIN, "the redirect target is main's raw file"


def test_what_the_code_fetches_stays_on_github() -> None:
    """Only what a person types moved: install.ps1 still fetches main's install.sh from GitHub
    (the stamp rewrites that line to a release's), install.sh still hands off through GitHub's
    releases, and the short URL appears nowhere else in the code."""
    sh = (REPO / "install.sh").read_text(encoding="utf-8")
    ps1 = (REPO / "install.ps1").read_text(encoding="utf-8")
    assert stamper.PS1_URL in ps1
    assert f'RELEASES_URL="https://github.com/{stamper.GITHUB_REPO}/releases"' in sh
    in_code = {
        where: sum(stamper.SHORT in line for line in text.splitlines())
        for where, text in _tracked_texts().items()
        if where.startswith(("src/", "scripts/")) or where in stamper.ASSETS
    }
    assert {where: n for where, n in in_code.items() if n} == {
        "install.sh": 2,  # its header and --help: printed
        "install.ps1": 1,  # its header: printed
        "scripts/stamp_installers.py": 1,  # SHORT itself
        "src/aisquare/services/install_route.py": 1,  # INSTALLER_ONE_LINER: printed
    }


# --- controls: synthetic input, so they keep controlling whatever the tree says -----------------


def test_the_raw_one_liner_is_caught_in_each_shape() -> None:
    raw = "https://raw.githubusercontent.com/AISquare-Studio/rlx"
    for line in (
        f"curl -fsSL {raw}/main/install.sh | sh",
        f"  curl -sSfL --proto =https {raw}/main/install.sh | sh -s -- --agent both",
        f"#   irm {raw}/main/install.ps1 | iex",
        f"run `irm {raw}/refs/heads/main/install.ps1 | iex` in PowerShell",
        f"curl -fsSL '{raw}/main/install.sh' | sh",
    ):
        assert _raw_one_liners({"x.md": line}) == [f"x.md:1: {line.strip()}"], line


def test_the_short_form_and_the_code_s_own_fetches_are_not_accused() -> None:
    innocent = "\n".join(
        (
            stamper.SH_ONE_LINER,
            stamper.PS1_ONE_LINER,
            f"{stamper.SH_ONE_LINER.removesuffix(' | sh')} | sh -s -- --agent both",
            '$InstallUrl = "https://raw.githubusercontent.com/$Repo/main/install.sh"',
            "$inner = \"set -e; curl -fsSL '$InstallUrl' | sh -s -- $Pin$Forward\"",
            f"curl -fsSL {stamper.release_url('v0.10.0', 'install.sh')} | sh",
            "![x](https://raw.githubusercontent.com/AISquare-Studio/rlx/main/docs/demo.gif)",
        )
    )
    assert _raw_one_liners({"x.md": innocent}) == []
