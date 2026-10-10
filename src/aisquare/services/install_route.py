"""How THIS aisquare was installed, and the commands that upgrade and remove it.

Read from the RUNNING interpreter (``sys.prefix``), never from ``PATH``. The two
disagree on ordinary machines: a developer whose shell runs a checkout's
editable ``.venv`` can have the installer's uv tool first on ``PATH``, and an
upgrade decided from ``PATH`` would replace the install the user did not run.

The routes, in the order they are decided:

* ``uv-tool`` — ``uv-receipt.toml`` in ``sys.prefix``, the package from an index.
  What the one-line installer makes, and the ONLY route ``aisquare upgrade``
  runs itself (never on Windows, which locks a running program's files).
* ``editable`` — the receipt or pip's ``direct_url.json`` names an editable
  checkout. Upgrading it is the checkout's business: ``git pull``.
* ``local-source`` — a directory, wheel, git or URL source, recorded by the
  receipt or by ``direct_url.json``.
* ``pipx`` — ``pipx_metadata.json`` in ``sys.prefix``.
* ``homebrew`` — a ``Cellar`` in the resolved prefix.
* ``venv`` — any other virtual environment (``sys.prefix != sys.base_prefix``).
* ``system`` — a system or ``--user`` pip install.

Every route has an exact command; a route this CLI does not run is reported
WITH that command, so the answer is never just "no".

``uv tool install --force`` REPLACES the tool environment — measured on uv
0.12.19: the environment directory's inode changes, and any extra or ``--with``
not named again is gone (``[serve]`` lost ``mcp``). So the command restates
what uv recorded in the receipt: the extras, every ``--with``, the Python and
the index options. It is never ``uv tool upgrade``, which leaves a pinned
install where it is ("Nothing to upgrade", exit 0 — docs/plans/one-line-install.md
§3.9.1); ``tests/test_lifecycle_upgrade.py`` holds both modules to that.

The outside world is reached through four functions here and nowhere else —
:func:`open_url` (the network, for :func:`fetch_latest`), :func:`run_installer`,
:func:`run_captured` and :func:`exec_replace` (processes) — so a test replaces
them and never starts uv or touches PyPI; :func:`find_uv` is the one PATH
lookup, for the same reason. The process ones are registered spawn seams
(``core.spawn.SEAMS``).

What the run touches AFTER the installer is imported at module top, on purpose.
``uv tool install --force`` deletes the environment this process was loaded from
while it is still running, so a later import of anything in that environment
would load the new version's module into the old process, or fail outright. The
one exception is the PyPI lookup's network modules, imported inside it to keep
them off the path every command pays to start (``tests/test_iam_single_reader.py``,
the hooks included). That is safe twice over: the lookup runs before any
installer, and those are standard-library modules, which live with the
interpreter rather than in the tool environment uv replaces.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import site
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qsl, unquote, urlparse, urlsplit, urlunsplit

from aisquare.core.version import DISTRIBUTION, __version__

UV_TOOL = "uv-tool"
EDITABLE = "editable"
LOCAL_SOURCE = "local-source"
PIPX = "pipx"
HOMEBREW = "homebrew"
UVX = "uvx"
VENV = "venv"
SYSTEM = "system"

ROUTES = (UV_TOOL, EDITABLE, LOCAL_SOURCE, UVX, PIPX, HOMEBREW, VENV, SYSTEM)
"""Every route :func:`classify` can answer, in the order it decides them."""

#: The directories of uv's cache that hold the environments ``uvx`` runs:
#: ``archive-v0/<id>/`` is the environment, and ``environments-v2/<hash>/<hash>``
#: links to one (measured, uv 0.12.19). They count only inside uv's cache
#: (:func:`_uv_cache_environment`).
_UV_CACHE_ENVIRONMENTS = re.compile(r"(?:archive|environments)-v\d+")

PYPI_JSON_URL = f"https://pypi.org/pypi/{DISTRIBUTION}/json"
LOOKUP_TIMEOUT_SECONDS = 5.0

INSTALLER_ONE_LINER = (
    "curl -fsSL https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.sh | sh"
)
"""The last resort every failure names: it repairs a broken install from nothing."""

RECEIPT_NAME = "uv-receipt.toml"
PIPX_METADATA_NAME = "pipx_metadata.json"

#: What the installer's environment needs whatever the user's uv config says.
#: ``python-downloads = "manual"`` ships in Fedora's /etc/uv/uv.toml, and then a
#: receipt naming a Python that is not installed fails to resolve — the trap
#: install.sh documents beside its own copy of these two lines.
INSTALLER_ENV = {"UV_PYTHON_DOWNLOADS": "automatic", "UV_NO_PROGRESS": "1"}

#: ``[tool.options]`` keys uv records and the ``uv tool install`` flag that
#: restates each. uv records options given on the command line AND through its
#: environment variables (measured: ``UV_INDEX_URL`` lands here as
#: ``index-url``), so an install from a mirror carries its index here — and a
#: reinstall that dropped it would resolve against pypi.org. A key outside this
#: table is not guessed at: the route is reported with the command instead of
#: run, because a dropped option is a silent change to how the next resolve
#: behaves.
_UV_OPTION_FLAGS = {
    "index-url": "--index-url",
    "extra-index-url": "--extra-index-url",
    "find-links": "--find-links",
    "no-index": "--no-index",
    "index-strategy": "--index-strategy",
    "keyring-provider": "--keyring-provider",
    "prerelease": "--prerelease",
    "resolution": "--resolution",
    "fork-strategy": "--fork-strategy",
    "exclude-newer": "--exclude-newer",
    "link-mode": "--link-mode",
    "compile-bytecode": "--compile-bytecode",
    "no-sources": "--no-sources",
    "no-sources-package": "--no-sources-package",
    "no-build": "--no-build",
    "no-binary": "--no-binary",
    "no-build-package": "--no-build-package",
    "no-binary-package": "--no-binary-package",
    "no-build-isolation": "--no-build-isolation",
    "no-build-isolation-package": "--no-build-isolation-package",
}

#: Receipt keys naming where a requirement comes from when it is not an index.
_SOURCE_KEYS = ("editable", "directory", "path", "git", "url")

#: Receipt lists ``uv tool install`` only takes as FILES (``--constraints`` and
#: friends), so a command line cannot restate them.
_FILE_ONLY_LISTS = ("constraints", "overrides", "build-constraint-dependencies")

_REQUIREMENT_KEYS = frozenset({"name", "extras", "specifier", "marker"})


# --- versions ---------------------------------------------------------------------------


_VERSION = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>\d+)!)?
    (?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_l>alpha|beta|preview|pre|rc|a|b|c)[-_.]?(?P<pre_n>\d+)?)?
    (?:-(?P<post_n1>\d+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>\d+)?)?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>\d+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}


def version_key(text: str) -> tuple[Any, ...] | None:
    """A sort key for a PEP 440 version, or ``None`` when ``text`` is not one.

    Written here because a uv tool environment has no ``packaging`` to import,
    and a string comparison is wrong exactly where it matters: ``"0.10.0" <
    "0.2.0"``. Release segments compare as numbers with trailing zeros ignored
    (``0.7`` is ``0.7.0``); a dev release sorts before its pre-releases, which
    sort before the final, which sorts before its post-releases — packaging's
    order. The local label (``+abc``) compares as text, a simplification no
    published release exercises.
    """
    match = _VERSION.match(text)
    if match is None:
        return None
    release = tuple(int(part) for part in match["release"].split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    pre = match["pre_l"]
    post_number = match["post_n1"] or match["post_n2"]
    has_post = bool(match["post_l"] or match["post_n1"])
    has_dev = bool(match["dev_l"])
    if pre is not None:
        pre_key: tuple[int, ...] = (1, _PRE_RANK[pre.lower()], int(match["pre_n"] or 0))
    elif has_dev and not has_post:
        pre_key = (0, 0, 0)
    else:
        pre_key = (2, 0, 0)
    post_key = (1, int(post_number or 0)) if has_post else (0, 0)
    dev_key = (0, int(match["dev_n"] or 0)) if has_dev else (1, 0)
    return (int(match["epoch"] or 0), release, pre_key, post_key, dev_key, match["local"] or "")


_VERSION_ARGUMENT = re.compile(r"\d[0-9A-Za-z.!+_-]*")


def version_argument(text: str) -> str | None:
    """``--version``'s value as the bare version uv's ``name@version`` takes, or ``None``.

    It is pasted into the package spec, so it must be ONE version token: a
    leading ``v`` (``v0.8.0``, how tags are written) is dropped, and anything
    that does not then parse as a PEP 440 version is refused.
    """
    candidate = text.strip()
    if candidate[:1] in ("v", "V"):
        candidate = candidate[1:]
    if not _VERSION_ARGUMENT.fullmatch(candidate) or version_key(candidate) is None:
        return None
    return candidate


def same_version(left: str, right: str) -> bool:
    """Whether two version strings name one release (``0.7`` is ``0.7.0``)."""
    left_key, right_key = version_key(left), version_key(right)
    if left_key is None or right_key is None:
        return left.strip() == right.strip()
    return left_key == right_key


def is_newer(candidate: str, than: str) -> bool | None:
    """Whether ``candidate`` sorts after ``than``; ``None`` when either is not a version."""
    candidate_key, than_key = version_key(candidate), version_key(than)
    if candidate_key is None or than_key is None:
        return None
    return bool(candidate_key > than_key)


def is_prerelease(text: str) -> bool:
    """Whether ``text`` is a pre-release (``1.0.0rc1``) or a dev release, as PEP 440 counts them."""
    match = _VERSION.match(text)
    return match is not None and bool(match["pre_l"] or match["dev_l"])


_PYTHON = re.compile(r"\d+(?:\.\d+)+")
_REQUIRES_CLAUSE = re.compile(r"(~=|==|!=|<=|>=|<|>)\s*(\d+(?:\.\d+)*)(\.\*)?")


def admits_python(requires: object, python: str) -> bool | None:
    """Whether a release's Requires-Python (``>=3.11``) admits ``python``, as uv reads it
    before it takes the release: ``python`` is every version it begins (``3.11`` is any
    3.11.x). ``None`` when it admits only some of those, or cannot be read; a release
    that declares none admits every Python."""
    if requires is None or (isinstance(requires, str) and not requires.strip()):
        return True
    if not isinstance(requires, str) or not _PYTHON.fullmatch(python):
        return None
    ours = tuple(int(part) for part in python.split("."))
    verdicts: list[bool | None] = []
    for clause in filter(None, (part.strip() for part in requires.split(","))):
        match = _REQUIRES_CLAUSE.fullmatch(clause)
        if match is None:
            return None
        bound = tuple(int(part) for part in match[2].split("."))
        if match[1] == "~=":
            if len(bound) < 2:
                return None
            verdicts += [_clause(">=", bound, ours), _prefix_clause("==", bound[:-1], ours)]
        elif match[3]:
            if match[1] not in ("==", "!="):
                return None
            verdicts.append(_prefix_clause(match[1], bound, ours))
        else:
            verdicts.append(_clause(match[1], bound, ours))
    if False in verdicts:
        return False
    return None if None in verdicts else True


def _clause(operator: str, bound: tuple[int, ...], ours: tuple[int, ...]) -> bool | None:
    """One ``<op> <version>`` clause for every version ``ours`` begins."""
    head = (bound + (0,) * len(ours))[: len(ours)]
    if head != ours:
        # Every version ``ours`` begins is on one side of the bound.
        above = ours > head
        verdict = {">=": above, ">": above, "<=": not above, "<": not above, "==": False}
        return verdict.get(operator, True)  # `!=`
    lowest = not any(bound[len(ours) :])
    if lowest and operator in (">=", "<"):
        return operator == ">="
    return None


def _prefix_clause(operator: str, bound: tuple[int, ...], ours: tuple[int, ...]) -> bool | None:
    """``==<version>.*`` (or ``!=``) for every version ``ours`` begins."""
    if len(bound) <= len(ours):
        matches: bool | None = ours[: len(bound)] == bound
    else:
        matches = None if bound[: len(ours)] == ours else False
    if operator == "!=" and matches is not None:
        return not matches
    return matches


# --- the latest release -----------------------------------------------------------------


@dataclass(frozen=True)
class LatestRelease:
    """What PyPI says is newest — ``version`` or, when it could not say, ``error``."""

    version: str | None
    error: str | None = None
    cutoff: str | None = None
    """The uv cutoff (``--exclude-newer P14D``) ``version`` is the newest release under, when
    it is that rather than PyPI's newest."""


def fetch_latest(
    timeout: float = LOOKUP_TIMEOUT_SECONDS,
    *,
    prereleases: bool = False,
    uploaded_before: datetime | None = None,
    python: str | None = None,
) -> LatestRelease:
    """The newest ``aisquare-cli`` on PyPI. Never raises; an unreachable PyPI is an answer.

    Called only when ``aisquare upgrade`` runs — never by ``doctor``, which stays
    offline unless ``--live``. PyPI's number decides only whether there is
    anything to do; whether an upgrade WORKED is decided by asking the new
    install its version, because a mirror may serve a different "latest".

    ``info.version`` is PyPI's newest FINAL release, even after a pre-release was
    uploaded (measured). With ``prereleases``, for an install whose upgrade takes them
    (:func:`takes_prereleases`), it is the newest release PyPI lists with a file that
    is not yanked, as uv picks: such an install was told "up to date" while uv would
    have installed a newer pre-release (sweep of #257). With ``uploaded_before``, for an
    install under a uv cutoff (:func:`cutoff_time`), it is the newest such release with a
    file uploaded before then, as uv's ``--exclude-newer`` filters them. With ``python``
    (:func:`reinstall_python`), a release none of whose files' Requires-Python admits it is
    not an answer: uv passes over it, and the run that changed nothing failed as §3.9.1's
    silent no-op (review of #257).
    """
    # Here, not at module top: see the module docstring's one exception.
    from http.client import HTTPException
    from urllib.error import URLError
    from urllib.request import Request

    request = Request(
        PYPI_JSON_URL,
        headers={"Accept": "application/json", "User-Agent": f"{DISTRIBUTION}/{__version__}"},
    )
    try:
        with open_url(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, HTTPException, OSError, TimeoutError, ValueError) as exc:
        # HTTPException is not an OSError: a truncated body raises IncompleteRead.
        return LatestRelease(None, f"could not reach PyPI ({exc})")
    releases = payload.get("releases") if isinstance(payload, dict) else None
    info = payload.get("info") if isinstance(payload, dict) else None
    if uploaded_before is not None:
        version = _newest_listed(releases, finals_only=not prereleases, before=uploaded_before)
        if version is None:
            return LatestRelease(None, "PyPI lists no release uploaded before the cutoff")
    else:
        version = info.get("version") if isinstance(info, dict) else None
        if not isinstance(version, str) or version_key(version) is None:
            return LatestRelease(None, "PyPI's answer named no version")
        if prereleases:
            listed = _newest_listed(releases)
            if listed is not None and is_newer(listed, version):
                version = listed
    if python is None:
        return LatestRelease(version)
    return _for_python(version, python, releases, info, uploaded_before)


def _for_python(
    version: str, python: str, releases: object, info: object, before: datetime | None
) -> LatestRelease:
    """``version`` when a file of it uv would take admits ``python`` by its Requires-Python,
    else why it is no answer. With no file listed, ``info``'s, which describes the newest."""
    files = releases.get(version) if isinstance(releases, dict) else None
    if isinstance(files, list) and files:
        specs = [file.get("requires_python") for file in files if _installable(file, before)]
    elif isinstance(info, dict) and info.get("version") == version:
        specs = [info.get("requires_python")]
    else:
        specs = []
    verdicts = [admits_python(spec, python) for spec in specs]
    if not verdicts or True in verdicts:
        return LatestRelease(version)
    shown = ", ".join(sorted({str(spec) for spec in specs}))
    if None in verdicts:
        return LatestRelease(
            None, f"can't tell whether Python {python} meets {version}'s Requires-Python ({shown})"
        )
    return LatestRelease(
        None,
        f"{version} on PyPI requires Python {shown}, and this install's upgrade runs on "
        f"Python {python}",
    )


def _newest_listed(
    releases: object, *, finals_only: bool = False, before: datetime | None = None
) -> str | None:
    """The newest version in PyPI's ``releases`` that has a file not yanked (and, with
    ``before``, uploaded before then), pre-releases left out with ``finals_only``."""
    if not isinstance(releases, dict):
        return None
    newest: str | None = None
    for version, files in releases.items():
        if not isinstance(version, str) or not isinstance(files, list):
            continue
        if version_key(version) is None or (finals_only and is_prerelease(version)):
            continue
        if not any(_installable(file, before) for file in files):
            continue
        if newest is None or is_newer(version, newest):
            newest = version
    return newest


def _installable(file: object, before: datetime | None) -> bool:
    """Whether one file of PyPI's ``releases`` is one uv would take: not yanked, and with
    ``before``, uploaded before then. A time that cannot be read is not before anything."""
    if not isinstance(file, dict) or file.get("yanked") is True:
        return False
    if before is None:
        return True
    uploaded = _instant(file.get("upload_time_iso_8601"))
    return uploaded is not None and uploaded < before


def _instant(text: object) -> datetime | None:
    """An RFC 3339 timestamp (``2026-09-26T00:58:02.070800Z``) with its zone, else ``None``."""
    if not isinstance(text, str):
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def open_url(request: Any, *, timeout: float) -> Any:
    """``urllib.request.urlopen``: the one network call in this module, behind a name a
    test can replace. Imported inside for the reason the module docstring gives."""
    from urllib.request import urlopen

    return urlopen(request, timeout=timeout)


# --- the uv receipt ---------------------------------------------------------------------


@dataclass(frozen=True)
class UvReceipt:
    """What uv recorded about the tool environment, restated as command-line pieces.

    ``unrestatable`` lists what the receipt holds that a ``uv tool install``
    command line cannot carry; when it is not empty the route is reported, not
    run.
    """

    extras: tuple[str, ...] = ()
    withs: tuple[str, ...] = ()
    python: str | None = None
    options: tuple[str, ...] = ()
    source: tuple[str, str] | None = None
    """``(kind, where)`` when the package came from somewhere other than an index."""
    subdirectory: str | None = None
    """The project's directory inside a ``url`` or ``git`` source, when uv recorded one."""
    bin_dir: Path | None = None
    """Where uv put the ``aisquare`` executable — so a reinstall puts it there again."""
    unrestatable: tuple[str, ...] = ()
    holds: tuple[str, ...] = ()
    """What may change which aisquare-cli release uv resolves, named as recorded: ``--with``
    requirements with a version specifier (or from a source a command cannot name), the
    receipt's ``constraints`` and ``overrides``, and every ``[tool.options]`` key set to
    something (not ``false`` or empty) but :data:`_BUILD_ONLY`'s."""
    unreadable: str | None = None
    """``<path>: <reason>`` when the receipt could not be read for what it records."""


def _canonical(name: object) -> str:
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def _requirement_string(requirement: Mapping[str, Any]) -> str | None:
    """``name[extras]specifier; marker`` for a plain index requirement, else ``None``."""
    if not set(requirement) <= _REQUIREMENT_KEYS:
        return None
    name = requirement.get("name")
    extras = requirement.get("extras") or []
    specifier = requirement.get("specifier") or ""
    marker = requirement.get("marker")
    if not isinstance(name, str) or not isinstance(specifier, str):
        return None
    if not isinstance(extras, list) or not all(isinstance(extra, str) for extra in extras):
        return None
    text = name + (f"[{','.join(extras)}]" if extras else "") + specifier
    if marker is not None:
        if not isinstance(marker, str):
            return None
        text += f"; {marker}"
    return text


#: The ``index`` table keys :func:`_index_flags` can restate, and their defaults.
_INDEX_DEFAULTS = {"explicit": False, "format": "simple", "authenticate": "auto"}


def _index_flags(entries: object) -> list[str] | None:
    """``--default-index`` / ``--index`` for uv's recorded ``index`` tables, or ``None``.

    What ``--default-index``, ``--index`` and their ``UV_DEFAULT_INDEX`` /
    ``UV_INDEX`` variables leave in the receipt (measured):
    ``index = [{ url = "…", explicit = false, default = true, format =
    "simple", authenticate = "auto" }]``. Only that plain shape is restated; an
    explicit index, a flat one or a non-default authentication policy has no
    flag that carries it, so the route is reported instead.
    """
    if not isinstance(entries, list):
        return None
    flags: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        url, name = entry.get("url"), entry.get("name")
        if not isinstance(url, str) or not url:
            return None
        if set(entry) - {"url", "name", "default", *_INDEX_DEFAULTS}:
            return None
        if any(entry.get(key, default) != default for key, default in _INDEX_DEFAULTS.items()):
            return None
        if entry.get("default") is True:
            flags.extend(["--default-index", url])
        else:
            flags.extend(["--index", f"{name}={url}" if isinstance(name, str) and name else url])
    return flags


def _option_flags(options: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """The ``uv tool install`` flags that restate ``[tool.options]``, and what cannot be."""
    flags: list[str] = []
    refused: list[str] = []
    options = dict(options)
    if "exclude-newer-span" in options:
        # A cooldown (`exclude-newer = "7 days"` or `P7D`, from a flag, UV_EXCLUDE_NEWER or
        # uv.toml) is recorded as this span AND the cutoff uv worked out from it at install
        # time (measured, uv 0.12.19). The span is the setting: restating the cutoff froze
        # it, so no later release could be installed, and the span was gone from the
        # receipt the reinstall wrote (sweep of #257).
        options["exclude-newer"] = options.pop("exclude-newer-span")
    recorded = options.get("exclude-newer")
    if isinstance(recorded, str) and recorded.startswith("-"):
        # "1 day ago" is recorded as `-P1D`, the same cooldown as `P1D` (measured, uv 0.12.19:
        # one timestamp for both). Restated with its sign, uv read it as a flag and refused
        # the command ("a value is required for '--exclude-newer'") on every run (#257).
        options["exclude-newer"] = recorded[1:]
    for key, value in options.items():
        if key == "index":
            indexes = _index_flags(value)
            if indexes is None:
                refused.append("a uv index this CLI cannot restate")
            else:
                flags.extend(indexes)
            continue
        flag = _UV_OPTION_FLAGS.get(key)
        if flag is None:
            refused.append(f"uv option {key}")
        elif value is True:
            flags.append(flag)
        elif value is False:
            continue
        elif isinstance(value, str):
            flags.extend([flag, value])
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            for item in value:
                flags.extend([flag, item])
        else:
            refused.append(f"uv option {key}")
    return flags, refused


def read_receipt(prefix: Path) -> UvReceipt | None:
    """The receipt uv left in ``prefix``, or ``None`` when ``prefix`` is not a uv tool."""
    path = prefix / RECEIPT_NAME
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return UvReceipt(
            unrestatable=(f"an unreadable {RECEIPT_NAME} ({exc})",),
            unreadable=f"{path}: {exc}",
        )
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return UvReceipt(
            unrestatable=(f"a {RECEIPT_NAME} with no [tool] table",),
            unreadable=f"{path}: no [tool] table",
        )
    refused: list[str] = []
    requirements = tool.get("requirements")
    if not isinstance(requirements, list):
        requirements = []
    ours: dict[str, Any] | None = None
    withs: list[str] = []
    held_by_withs: list[str] = []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            refused.append("a requirement uv wrote in a shape this CLI does not know")
            continue
        if ours is None and _canonical(requirement.get("name")) == DISTRIBUTION:
            ours = requirement
            continue
        text = _requirement_string(requirement)
        if text is None:
            refused.append(f"--with {requirement.get('name')} from a source a command cannot name")
            held_by_withs.append(f"--with {requirement.get('name')}")
        else:
            withs.append(text)
            if requirement.get("specifier"):
                # `--with 'rich<14.3'` held 0.7.0 back, which needs rich>=14.3 (measured, uv
                # 0.12.19), while --check said an update is available (review of #257). A
                # marker alone says only when it is installed; it resolves as a bare one does.
                held_by_withs.append(f"--with {text}")
    extras: tuple[str, ...] = ()
    source: tuple[str, str] | None = None
    subdirectory: str | None = None
    if ours is None:
        refused.append(f"no {DISTRIBUTION} requirement")
    else:
        raw_extras = ours.get("extras") or []
        if isinstance(raw_extras, list) and all(isinstance(extra, str) for extra in raw_extras):
            extras = tuple(sorted(set(raw_extras)))
        else:
            refused.append(f"{DISTRIBUTION} extras in a shape this CLI does not know")
        for key in _SOURCE_KEYS:
            if key in ours:
                source = (key, str(ours[key]))
                break
        if isinstance(ours.get("subdirectory"), str) and ours["subdirectory"]:
            subdirectory = ours["subdirectory"]
        unknown = set(ours) - _REQUIREMENT_KEYS - set(_SOURCE_KEYS) - {"subdirectory"}
        if unknown:
            refused.append(f"{DISTRIBUTION} requirement keys {', '.join(sorted(unknown))}")
    for key in _FILE_ONLY_LISTS:
        if tool.get(key):
            refused.append(f"{key} (uv takes those only as files)")
    options = tool.get("options")
    flags: list[str] = []
    holds = [*held_by_withs, *(key for key in _HOLDING_LISTS if tool.get(key))]
    if isinstance(options, dict):
        flags, refused_options = _option_flags(options)
        refused.extend(refused_options)
        # A key recorded as `false` or empty sets nothing (`no-index = false`, measured).
        holds.extend(
            key
            for key, value in options.items()
            if key not in _BUILD_ONLY and value not in (False, "", [], {})
        )
    python = tool.get("python")
    return UvReceipt(
        extras=extras,
        withs=tuple(withs),
        python=python if isinstance(python, str) and python else None,
        options=tuple(flags),
        source=source,
        subdirectory=subdirectory,
        bin_dir=_bin_dir(tool.get("entrypoints")),
        unrestatable=tuple(refused),
        holds=tuple(holds),
    )


#: Recorded options that change only how a release is built or installed, never which one
#: uv resolves. Every other key that is set (not ``false`` or empty) holds, unknown ones
#: included, so a setting uv adds later is never compared by mistake (review of #257's
#: fixes).
_BUILD_ONLY = frozenset(
    {
        "torch-backend",
        "config-settings",
        "config-settings-package",
        "build-isolation",
        "no-build-isolation",
        "no-build-isolation-package",
        "extra-build-dependencies",
        "extra-build-variables",
        "link-mode",
        "compile-bytecode",
        # Copied from a user's uv.toml into every receipt (measured): how an index is
        # authenticated, and whether a project's [tool.uv.sources] apply, not which
        # aisquare-cli release an index offers (review of #257).
        "keyring-provider",
        "no-sources",
        "no-sources-package",
    }
)
#: The receipt's own lists that constrain the resolution.
_HOLDING_LISTS = ("constraints", "overrides")


def _bin_dir(entrypoints: object) -> Path | None:
    """The directory uv installed the ``aisquare`` executable into, from the receipt."""
    if not isinstance(entrypoints, list):
        return None
    for entry in entrypoints:
        if isinstance(entry, dict) and entry.get("name") == "aisquare":
            where = entry.get("install-path")
            if isinstance(where, str) and where:
                return Path(where).parent
    return None


# --- the route --------------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    """Everything route detection reads, gathered in one place so a test can supply it."""

    prefix: Path
    base_prefix: Path
    executable: Path
    platform: str
    python_version: str
    """``major.minor`` of the running interpreter."""
    direct_url: str | None = None
    """``direct_url.json`` of the installed distribution, as text."""
    installer: str | None = None
    """``INSTALLER`` of the installed distribution: ``pip``, ``uv``, …"""
    user_install: bool = False
    """Whether the distribution lives in the user site (``pip install --user``)."""


@dataclass(frozen=True)
class InstallRoute:
    """How this aisquare was installed: the route, and what that route records."""

    kind: str
    facts: Facts
    receipt: UvReceipt | None = None
    source: str | None = None
    """The checkout, file or URL an editable or local-source install came from."""
    formula: str | None = None
    """The Homebrew formula, read from the ``Cellar/<formula>/…`` prefix."""

    @property
    def manager(self) -> str:
        """The tool that manages this install, as a person would name it."""
        if self.receipt is not None:
            return "uv tool"
        if self.kind == PIPX:
            return "pipx"
        if self.kind == HOMEBREW:
            return "Homebrew"
        if self.kind == UVX:
            return "uvx"
        return "uv pip" if self.facts.installer == "uv" else "pip"

    def describe(self) -> str:
        """One line for a person: what this install is and where it lives."""
        where = self.facts.prefix
        if self.kind == UV_TOOL:
            return f"a uv tool at {where}"
        if self.kind == EDITABLE:
            return f"an editable install of {self.source} ({self.manager})"
        if self.kind == LOCAL_SOURCE:
            return f"installed from {self.source} ({self.manager}), not from PyPI"
        if self.kind == PIPX:
            return f"a pipx install at {where}"
        if self.kind == HOMEBREW:
            return f"a Homebrew install ({self.formula})"
        if self.kind == UVX:
            return f"a uvx run from uv's cache ({where}): nothing is installed"
        if self.kind == VENV:
            return f"a virtual environment at {where} ({self.manager})"
        return f"a {'user' if self.facts.user_install else 'system'} {self.manager} install"


def _read_distribution_text(name: str) -> str | None:
    try:
        return metadata.distribution(DISTRIBUTION).read_text(name)
    except Exception:  # absent metadata, an unreadable file: detection only reads
        return None


def _user_install() -> bool:
    try:
        location = Path(str(metadata.distribution(DISTRIBUTION).locate_file(""))).resolve()
        user_site = Path(site.getusersitepackages()).resolve()
    except Exception:  # no distribution, no user site: then it is not a user install
        return False
    return location == user_site or user_site in location.parents


def facts() -> Facts:
    """The facts of THIS process: its interpreter and its installed distribution."""
    installer = _read_distribution_text("INSTALLER")
    return Facts(
        prefix=Path(sys.prefix),
        base_prefix=Path(sys.base_prefix),
        executable=Path(sys.executable),
        platform=sys.platform,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        direct_url=_read_distribution_text("direct_url.json"),
        installer=installer.strip() if installer else None,
        user_install=_user_install(),
    )


def _homebrew_formula(prefix: Path) -> str | None:
    """``<formula>`` when the prefix sits under a Homebrew ``Cellar/<formula>/<version>``."""
    try:
        parts = prefix.resolve().parts
    except OSError:
        parts = prefix.parts
    if "Cellar" not in parts:
        return None
    index = parts.index("Cellar")
    return parts[index + 1] if index + 1 < len(parts) else None


#: A git ref that names a commit: 7 to 40 hex characters.
_COMMIT = re.compile(r"[0-9a-fA-F]{7,40}")


def _moving_ref(ref: object) -> str | None:
    """``ref`` when following it is the upgrade (a branch), else ``None``.

    uv records PEP 508's ``@<ref>`` as ``rev=``, and pip's ``requested_revision``
    is the same ``@<ref>``: neither says whether it names a branch or a tag. A
    commit (7 to 40 hex characters) or a release tag (one :func:`version_key`
    reads: ``v0.8.0``) pins what is installed, so reinstalling it moves nothing,
    and it goes. Any other ref is a branch, whose head is the upgrade: dropping
    it moved a ``rc/first-run`` install to the default branch (review of #257).
    """
    if not isinstance(ref, str) or not ref or _COMMIT.fullmatch(ref):
        return None
    return None if version_key(ref) is not None else ref


def _direct_url(text: str | None) -> tuple[str, bool] | None:
    """``(source, editable)`` from ``direct_url.json``, or ``None`` for an index install.

    The source is what pip takes back: a path for a ``file://`` URL, and for a
    VCS install the PEP 508 reference rebuilt from ``vcs_info`` — PEP 610
    records ``https://…/r.git`` without its ``git+``, and pip reads a bare URL
    as an archive to download. The requested revision is kept when it is a
    branch (:func:`_moving_ref`), and so is a subdirectory.
    """
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("url"), str):
        return None
    url: str = parsed["url"]
    dir_info = parsed.get("dir_info")
    editable = isinstance(dir_info, dict) and dir_info.get("editable") is True
    vcs_info = parsed.get("vcs_info")
    if isinstance(vcs_info, dict) and isinstance(vcs_info.get("vcs"), str):
        revision = _moving_ref(vcs_info.get("requested_revision"))
        reference = f"{vcs_info['vcs']}+{url}"
        if revision:
            reference += f"@{revision}"
        subdirectory = parsed.get("subdirectory")
        if isinstance(subdirectory, str) and subdirectory:
            reference += f"#subdirectory={subdirectory}"
        return f"{DISTRIBUTION} @ {reference}", editable
    return _path_of(url), editable


def _path_of(url: str) -> str:
    """A ``file://`` URL as the path a person types HERE; any other URL unchanged.

    Rebuilt through ``Path`` so it reads the way this platform writes paths: on
    Windows ``file:///C:/src/x`` is ``C:\\src\\x``, not ``C:/src/x``.
    """
    if not url.startswith("file://"):
        return url
    parsed = urlparse(url)
    path = unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:", path):  # file:///C:/x on Windows
        path = path[1:]
    return str(Path(path))


def classify(found: Facts) -> InstallRoute:
    """The route ``found`` describes. Pure: everything it reads is in ``found`` or on disk
    under ``found.prefix``."""
    receipt = read_receipt(found.prefix)
    if receipt is not None:
        if receipt.source is not None:
            kind, where = receipt.source
            return InstallRoute(
                EDITABLE if kind == "editable" else LOCAL_SOURCE,
                found,
                receipt=receipt,
                source=where,
            )
        return InstallRoute(UV_TOOL, found, receipt=receipt)
    if _uv_cache_environment(found.prefix):
        # What `uvx --from aisquare-cli aisquare` (and the plugin's launcher) runs: an
        # entry uv may drop or rebuild, not an install. Read as a user's venv, upgrade
        # and uninstall advised `uv pip` into the cache (review of #257).
        return InstallRoute(UVX, found)
    if (found.prefix / PIPX_METADATA_NAME).is_file():
        return InstallRoute(PIPX, found)
    # A formula installs its app into a venv under Cellar/<formula>/<version>/libexec,
    # so only an environment that is not its own base is a formula's. Homebrew's own
    # Python lives under a Cellar too, and a pip install into it is not a formula:
    # "brew upgrade python@3.13" would move Python and leave aisquare where it was.
    formula = _homebrew_formula(found.prefix) if found.prefix != found.base_prefix else None
    if formula is not None:
        return InstallRoute(HOMEBREW, found, formula=formula)
    direct = _direct_url(found.direct_url)
    if direct is not None:
        source, editable = direct
        return InstallRoute(EDITABLE if editable else LOCAL_SOURCE, found, source=source)
    if found.prefix != found.base_prefix:
        return InstallRoute(VENV, found)
    return InstallRoute(SYSTEM, found)


def _uv_cache_environment(prefix: Path) -> bool:
    """Whether ``prefix`` is an environment in uv's cache, the kind ``uvx`` runs.

    Its parent or grandparent is one of the cache's environment directories, and the
    directory above that is uv's cache. That is the one ``UV_CACHE_DIR`` or the
    platform default names (:func:`_uv_cache_roots`), or one holding the
    ``CACHEDIR.TAG`` uv writes into every cache it makes: a ``--cache-dir`` or a uv.toml
    ``cache-dir`` moves the cache where only uv could name it. Not ``uv cache dir``,
    which would start a process on every session start and cannot see that
    ``--cache-dir``. By the names alone, a venv in ``~/Code/archive-v1/.venv`` read as
    a uvx run (review of #257). Never raises.
    """
    roots: list[str] | None = None
    for parent in prefix.parents[:2]:
        if _UV_CACHE_ENVIRONMENTS.fullmatch(parent.name) is None:
            continue
        cache = parent.parent
        if os.path.isfile(cache / "CACHEDIR.TAG"):
            return True
        if roots is None:
            roots = _uv_cache_roots()
        if _cache_key(str(cache)) in roots:
            return True
    return False


def _uv_cache_roots() -> list[str]:
    """Where uv keeps its cache unless its command line or a uv.toml moves it, as
    :func:`_cache_key` compares them: ``UV_CACHE_DIR``, and the platform default
    (``$XDG_CACHE_HOME/uv`` or ``~/.cache/uv``; ``%LOCALAPPDATA%\\uv\\cache`` on Windows)."""
    named = [os.environ.get("UV_CACHE_DIR") or ""]
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        named.append(os.path.join(local, "uv", "cache") if local else "")
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        named.append(
            os.path.join(xdg, "uv")
            if xdg
            else os.path.join(os.path.expanduser("~"), ".cache", "uv")
        )
    return [key for root in named if root and (key := _cache_key(root))]


def _cache_key(path: str) -> str:
    """``path`` as two names for one directory compare equal; empty where it cannot be read."""
    try:
        return os.path.normcase(os.path.realpath(os.path.expanduser(path)))
    except (OSError, ValueError):
        return ""


def detect() -> InstallRoute:
    """The route of THIS process."""
    return classify(facts())


def runs_from_uv_cache() -> bool:
    """Whether THIS process runs from an environment in uv's cache, the way ``uvx`` runs it.

    The plugin's launcher takes that route only where no aisquare is installed, so
    nothing on the agent's PATH answers to ``aisquare`` there. One path check, for
    the session-start hook that asks; :func:`detect` also reads the package metadata.
    """
    return _uv_cache_environment(Path(sys.prefix))


# --- what upgrades it -------------------------------------------------------------------


def _uv_spec(route: InstallRoute, target: str | None, current: str) -> list[str]:
    """The package argument(s) for ``uv tool install``: ``name[extras]@target``, the latest
    release no older than ``current`` (``name[extras]>=current``), or a source."""
    receipt = route.receipt or UvReceipt()
    extras = f"[{','.join(receipt.extras)}]" if receipt.extras else ""
    if receipt.source is None:
        floor = _floor(current) if target is None else None
        if floor is not None:
            # Not `@latest`: under a cutoff in uv's own settings (uv.toml, UV_EXCLUDE_NEWER),
            # or from an index that is behind, uv resolved it BELOW the running release and
            # replaced the install with that, and the way back failed under the same cutoff
            # (sweep of #257). A floor fails to resolve instead, before uv touches the
            # environment (measured, uv 0.12.19). The refresh is the one `@latest` implied,
            # so a release published minutes ago is seen.
            return ["--refresh-package", DISTRIBUTION, f"{DISTRIBUTION}{extras}>={floor}"]
        return [f"{DISTRIBUTION}{extras}@{target or 'latest'}"]
    kind, where = receipt.source
    if kind == "editable":
        return ["-e", f"{where}{extras}"]
    if kind in ("directory", "path"):
        return [f"{where}{extras}"]
    if kind == "git":
        return [f"{DISTRIBUTION}{extras} @ {_git_reference(where, receipt.subdirectory)}"]
    subdirectory = f"#subdirectory={receipt.subdirectory}" if receipt.subdirectory else ""
    return [f"{DISTRIBUTION}{extras} @ {where}{subdirectory}"]


def _git_reference(recorded: str, subdirectory: str | None) -> str:
    """uv's recorded git source as a PEP 508 ``git+`` URL that moves forward.

    uv writes ``https://…/r?branch=dev#<commit>`` or ``?rev=…`` / ``?tag=…``,
    with ``subdirectory=`` in the same query, URL-encoded (``rev=rc%2Ffirst-run``).
    A branch is kept — its head is the upgrade — and so is a ``rev`` that names one
    (:func:`_moving_ref`): uv records a ``@<ref>`` as ``rev`` whether it is a branch
    or a tag (measured, uv 0.12.19). A ``tag``, a ``rev`` that is a commit or a
    release tag, and the ``#<commit>`` pin what is installed, so they go. The
    subdirectory moves to PEP 508's fragment.
    """
    parts = urlsplit(recorded)
    query = dict(parse_qsl(parts.query))
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if not base.startswith("git+"):
        base = "git+" + base
    branch = query.get("branch") or _moving_ref(query.get("rev"))
    if branch:
        base += f"@{branch}"
    inner = query.get("subdirectory") or subdirectory
    return base + (f"#subdirectory={inner}" if inner else "")


def _floor(version: str) -> str | None:
    """``version`` as the oldest release an upgrade may land on, or ``None`` when it is not a
    version (the upgrade then asks for ``@latest``). A local label goes: ``>=`` takes none."""
    public = version_argument(version)
    return public.split("+", 1)[0] if public is not None else None


def _uv_install_argv(route: InstallRoute, target: str | None, current: str) -> list[str]:
    receipt = route.receipt or UvReceipt()
    argv = ["uv", "tool", "install", "--force", "--python"]
    argv.append(receipt.python or route.facts.python_version)
    for requirement in receipt.withs:
        argv.extend(["--with", requirement])
    argv.extend(receipt.options)
    argv.extend(_uv_spec(route, target, current))
    return argv


def _pip_argv(route: InstallRoute, verb: str, *arguments: str) -> list[str]:
    """``pip <verb> …`` for the environment this interpreter runs in, by the tool that made it.

    A venv made by uv has no pip (measured: no ``pip*`` in site-packages,
    ``INSTALLER`` reads ``uv``), so ``python -m pip`` would fail there; and
    ``uv pip`` has no ``--user``.
    """
    if route.facts.installer == "uv":
        kept = [argument for argument in arguments if argument != "--user"]
        return ["uv", "pip", verb, "--python", str(route.facts.executable), *kept]
    return [str(route.facts.executable), "-m", "pip", verb, *arguments]


def reinstall_python(route: InstallRoute) -> str:
    """The Python the reinstall of ``route`` runs on (:func:`fetch_latest`): the version its
    uv receipt records, which the command restates as ``--python``, else this interpreter's.
    A recorded request that names no version (a path) was resolved to the one that runs."""
    recorded = route.receipt.python if route.receipt is not None else None
    if recorded is not None and _PYTHON.fullmatch(recorded.strip()):
        return recorded.strip()
    return route.facts.python_version


def upgrade_argv(
    route: InstallRoute, target: str | None = None, *, current: str | None = None
) -> list[str]:
    """The command that moves this install to ``target``; with ``None``, to the latest
    release, and for a uv tool never to one older than ``current`` (the running version
    unless given): a move back is only made by asking for it with ``--version``."""
    pinned = f"{DISTRIBUTION}=={target}" if target else DISTRIBUTION
    if route.receipt is not None:
        return _uv_install_argv(route, target, __version__ if current is None else current)
    if route.kind == EDITABLE:
        # The reinstall, not only `git pull`: hatchling writes the version and the
        # dependencies into the install's metadata, so a pulled checkout still
        # reports its old version and lacks any dependency the release added.
        return _pip_argv(route, "install", "-e", route.source or ".")
    if route.kind == LOCAL_SOURCE:
        return _pip_argv(route, "install", "--upgrade", "--force-reinstall", route.source or ".")
    if route.kind == PIPX:
        if target:
            return ["pipx", "install", "--force", pinned]
        return ["pipx", "upgrade", DISTRIBUTION]
    if route.kind == HOMEBREW:
        return ["brew", "upgrade", route.formula or DISTRIBUTION]
    if route.kind == UVX:
        return ["uv", "tool", "install", pinned]  # an install to keep, as uvx keeps none
    user = ["--user"] if route.kind == SYSTEM and route.facts.user_install else []
    if target:
        return _pip_argv(route, "install", *user, pinned)
    return _pip_argv(route, "install", "--upgrade", *user, DISTRIBUTION)


#: Why each route is reported rather than run. Short, because it is printed
#: beside the command that does the job.
_NOT_AUTOMATED = {
    LOCAL_SOURCE: "it was installed from a local or VCS source, not from PyPI",
    PIPX: "pipx installs are upgraded with pipx",
    HOMEBREW: "Homebrew installs are upgraded with brew",
    UVX: "it runs through uvx from uv's cache, so nothing is installed to upgrade: uvx runs "
    "the release its --from names, and this installs one to keep",
    VENV: "it lives in a virtual environment this CLI does not manage",
    SYSTEM: "it was installed with pip outside a virtual environment",
}


def not_automated(route: InstallRoute) -> str | None:
    """Why ``aisquare upgrade`` will not run this route's command itself, or ``None``.

    Only the uv tool route runs: it is what the one-line installer makes, its
    receipt says exactly how to reinstall it, and the result is checked in a new
    process afterwards. Everything else is told the exact command instead.
    """
    if route.kind == UVX:
        return _NOT_AUTOMATED[UVX]  # before Windows: there is no install to lock
    windows = _windows_blocker(route, "replace")
    if route.kind == EDITABLE:
        # First on every platform: the pull is the step the printed reinstall
        # cannot carry, and Windows only changes WHEN the reinstall can run.
        pull = command_line(["git", "-C", route.source or ".", "pull"])
        reason = (
            f"an editable install follows its checkout — pull it first ({pull}), then reinstall it"
        )
        if windows is not None:
            reason += " after aisquare exits (Windows locks the files of a running program)"
        return reason
    if windows is not None:
        return windows
    if route.kind != UV_TOOL:
        return _NOT_AUTOMATED.get(route.kind, "this install is not one aisquare manages")
    receipt = route.receipt or UvReceipt()
    if receipt.unreadable is not None:
        return f"its uv receipt could not be read ({receipt.unreadable})"
    if receipt.unrestatable:
        return (
            f"its uv receipt records {'; '.join(receipt.unrestatable)}, which a reinstall "
            "could not carry over"
        )
    return _uv_tool_blocker(route)


def _windows_blocker(route: InstallRoute, verb: str) -> str | None:
    """Why Windows rules out a self-``verb`` (``replace``, ``remove``), or ``None``."""
    if route.facts.platform != "win32":
        return None
    return (
        f"Windows locks the files of a running program, so aisquare cannot {verb} itself "
        "— run it after aisquare exits"
    )


def _uv_tool_blocker(route: InstallRoute) -> str | None:
    """Why uv cannot be pointed at THIS tool environment, or ``None``.

    Shared by upgrade and uninstall, so the two agree on which uv tools they touch:
    ``uv tool … aisquare-cli`` acts on ``tools/aisquare-cli``, and from an
    environment with any other name it would act on a different one.
    """
    if route.facts.prefix.name != DISTRIBUTION:
        return f"the tool environment is named {route.facts.prefix.name!r}, not {DISTRIBUTION!r}"
    if find_uv() is None:
        return "uv is not on PATH"
    return None


#: The receipt flag that holds ``@latest`` back by upload date: uv takes no release
#: uploaded after the cutoff (a date, or a span before now), so PyPI's newest may be
#: one this install would never get, and an unchanged version is no silent no-op.
_CUTOFF_FLAG = "--exclude-newer"


def cutoff(route: InstallRoute) -> str | None:
    """The upload-date cutoff this install resolves under (``--exclude-newer P7D``), or ``None``."""
    options = route.receipt.options if route.receipt is not None else ()
    if _CUTOFF_FLAG not in options:
        return None
    at = options.index(_CUTOFF_FLAG)
    return command_line(options[at : at + 2])


#: A cooldown as uv records it in the receipt: an ISO 8601 span of weeks, days, hours,
#: minutes and seconds (``P14D``, ``P1W``, ``PT36H``, ``P1DT12H``; uv refuses months and
#: years, measured with uv 0.12.19).
_SPAN = re.compile(
    r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?=\d)(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?"
)


def cutoff_time(route: InstallRoute, now: datetime) -> datetime | None:
    """When this install's uv cutoff falls: its timestamp, or its span back from ``now``.
    ``None`` when it has no cutoff, or one this cannot read (then nothing is compared)."""
    options = route.receipt.options if route.receipt is not None else ()
    if _CUTOFF_FLAG not in options[:-1]:
        return None
    value = options[options.index(_CUTOFF_FLAG) + 1]
    span = _SPAN.fullmatch(value)
    if span is not None and any(span.groups()):
        weeks, days, hours, minutes, seconds = (float(part or 0) for part in span.groups())
        try:
            return now - timedelta(
                weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds
            )
        except (OverflowError, ValueError):
            # A span no calendar holds (a hand-edited P999999D): it ended upgrade --check in
            # a traceback, with no --json object (#257). Unreadable, so nothing is compared.
            return None
    # A date or a timestamp, given any way, is recorded as an RFC 3339 timestamp (measured).
    return _instant(value)


def takes_prereleases(route: InstallRoute, current: str) -> bool:
    """Whether uv takes pre-releases when this install upgrades to the latest release, so
    PyPI's ``info.version``, its newest FINAL release, is not what it gets: when the release
    that runs, ``current``, is itself one, which a uv tool's ``>=`` names (measured, uv
    0.12.19). A recorded ``prerelease`` setting holds, so it is never compared."""
    return route.kind == UV_TOOL and is_prerelease(current)


def find_uv() -> str | None:
    """Where ``uv`` is on PATH (an indirection so a test decides the answer)."""
    return shutil.which("uv")


# --- what removes it --------------------------------------------------------------------


def remove_argv(route: InstallRoute) -> list[str]:
    """The command that removes this install's package — and nothing else of ours."""
    if route.receipt is not None:
        return ["uv", "tool", "uninstall", DISTRIBUTION]
    if route.kind == PIPX:
        return ["pipx", "uninstall", DISTRIBUTION]
    if route.kind == HOMEBREW:
        return ["brew", "uninstall", route.formula or DISTRIBUTION]
    if route.kind == UVX:
        return ["uv", "cache", "clean", DISTRIBUTION]
    return _pip_argv(route, "uninstall", DISTRIBUTION)


def not_removable(route: InstallRoute) -> str | None:
    """Why ``aisquare uninstall`` will not run the removal itself, or ``None``.

    Any uv tool is removed by uv — whatever it was installed from, ``uv tool
    uninstall`` deletes the environment and both shims (measured) and nothing
    else. Every other manager is told the command: a pip in a venv the user made
    may hold other things they installed, and pipx and Homebrew keep records of
    their own that only they should edit.
    """
    if route.kind == UVX:
        return (
            "it runs through uvx from uv's cache, so nothing is installed to remove; "
            "this frees the cache"
        )
    windows = _windows_blocker(route, "remove")
    if windows is not None:
        return windows
    if route.receipt is None:
        return {
            PIPX: "pipx installs are removed with pipx",
            HOMEBREW: "Homebrew installs are removed with brew",
        }.get(route.kind, f"it was installed with {route.manager}, which this CLI does not run")
    return _uv_tool_blocker(route)


def installer_env(route: InstallRoute) -> dict[str, str]:
    """The variables the uv run gets on top of this process's environment.

    ``UV_TOOL_DIR`` and ``UV_TOOL_BIN_DIR`` pin the reinstall to THIS tool
    environment and THESE executables: without them a shell whose uv points
    elsewhere would install a second copy beside the running one, and the
    version check afterwards would — correctly, but uselessly — report that
    nothing moved.
    """
    env = dict(INSTALLER_ENV)
    if route.receipt is not None:
        env["UV_TOOL_DIR"] = str(route.facts.prefix.parent)
        if route.receipt.bin_dir is not None:
            env["UV_TOOL_BIN_DIR"] = str(route.receipt.bin_dir)
    return env


#: An argument cmd.exe and PowerShell both pass on as it is, unquoted. Anything else — a
#: space, cmd's ``< > | & ^``, PowerShell's ``, ; ( ) { }`` or a leading ``@`` — is
#: double-quoted, which both read literally. ``%`` (cmd) and ``$`` (PowerShell) are read
#: inside double quotes too, so no quoting both shells share protects those.
_WINDOWS_BARE = re.compile(r"(?!@)[\w@%+=:./\\\[\]-]+", re.ASCII)


def command_line(argv: Sequence[str]) -> str:
    """``argv`` as one line a person can paste into their shell.

    On Windows that shell is cmd.exe or PowerShell. ``subprocess.list2cmdline`` quotes
    only for the C runtime, so ``--with tiktoken>=0.7`` came out bare, and cmd read
    ``>=0.7`` as a redirection: the constraint was dropped (sweep of #257).
    """
    if sys.platform == "win32":
        return " ".join(
            arg if _WINDOWS_BARE.fullmatch(arg) else _windows_quoted(arg) for arg in argv
        )
    return shlex.join(argv)


def _windows_quoted(arg: str) -> str:
    """``arg`` in double quotes, escaped by the C runtime's rules (``list2cmdline``'s): its
    own quotes, and the backslashes before them or before the closing quote, doubled."""
    escaped = re.sub(r'(\\*)"', r'\1\1\\"', arg)
    return '"' + re.sub(r"(\\+)\Z", r"\1\1", escaped) + '"'


# --- the seams --------------------------------------------------------------------------


def run_installer(argv: Sequence[str], *, env: Mapping[str, str], to_stderr: bool) -> int:
    """Run the package manager to completion and return its exit code.

    A registered spawn seam (``core.spawn.SEAMS``). Its output reaches the
    terminal: streamed as it happens, or — when stdout belongs to ``--json`` —
    collected and written to stderr, so the one JSON object stays the only
    thing on stdout. ``127`` when the program cannot be started at all, the
    shell's code for "command not found".
    """
    merged = {**os.environ, **env}
    try:
        if not to_stderr:
            return subprocess.run(
                list(argv), env=merged, stdin=subprocess.DEVNULL, check=False
            ).returncode
        completed = subprocess.run(
            list(argv),
            env=merged,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        print(f"{argv[0]}: {exc}", file=sys.stderr)
        return 127
    sys.stderr.write(completed.stdout or "")
    sys.stderr.flush()
    return completed.returncode


@dataclass(frozen=True)
class Captured:
    """One finished command whose output was kept: ``error`` when it could not run."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


def run_captured(argv: Sequence[str], *, timeout: float) -> Captured:
    """Run ``argv`` with its output captured. Never raises.

    A registered spawn seam (``core.spawn.SEAMS``): the NEW install asked its
    version after an upgrade, and asked to rewrite its own hooks. Neither is a
    model process.
    """
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Captured(None, error=str(exc))
    return Captured(completed.returncode, completed.stdout or "", completed.stderr or "")


def exec_replace(
    argv: Sequence[str], *, env: Mapping[str, str], stdout_to_stderr: bool
) -> NoReturn:
    """Replace this process with ``argv`` — the package removal, the LAST thing uninstall does.

    A registered spawn seam (``core.spawn.SEAMS``). ``os.execvp`` rather than a
    child: what is being removed is the environment this process runs from, so
    nothing of ours should still be running when it goes, and the exit status
    the caller sees is the package manager's own. Under ``--json`` stdout
    already holds the one report object, so the manager's stdout is pointed at
    stderr first. Returns only when the program could not be started, by
    raising ``OSError``.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    if stdout_to_stderr:
        # A stream with no descriptor cannot be redirected; the manager's few
        # stdout lines then follow the report rather than replacing the exec.
        with contextlib.suppress(AttributeError, OSError, ValueError):
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    os.execvpe(argv[0], list(argv), {**os.environ, **env})
