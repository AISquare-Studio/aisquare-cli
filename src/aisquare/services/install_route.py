"""How THIS aisquare was installed, and the command that upgrades it.

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

The outside world is reached through three functions here and nowhere else —
:func:`fetch_latest` (the network), :func:`run_installer` and
:func:`run_captured` (processes) — so a test replaces them and never starts uv
or touches PyPI; :func:`find_uv` is the one PATH lookup, for the same reason.
The process ones are registered spawn seams (``core.spawn.SEAMS``).

Everything is imported at module top, on purpose. ``uv tool install --force``
deletes the environment this process was loaded from while it is still
running, so an import made AFTER the install would load the new version's
module into the old process, or fail outright.
"""

from __future__ import annotations

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
from http.client import HTTPException
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from aisquare.core.version import DISTRIBUTION, __version__

UV_TOOL = "uv-tool"
EDITABLE = "editable"
LOCAL_SOURCE = "local-source"
PIPX = "pipx"
HOMEBREW = "homebrew"
VENV = "venv"
SYSTEM = "system"

ROUTES = (UV_TOOL, EDITABLE, LOCAL_SOURCE, PIPX, HOMEBREW, VENV, SYSTEM)
"""Every route :func:`classify` can answer, in the order it decides them."""

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

#: The first thing in ``aisquare --version`` output that reads as a version.
_VERSION_TOKEN = re.compile(r"\d+(?:\.\d+)+[0-9A-Za-z.+!-]*")


def version_key(text: str) -> tuple[Any, ...] | None:
    """A sort key for a PEP 440 version, or ``None`` when ``text`` is not one.

    Written here because a uv tool environment has no ``packaging`` to import,
    and a string comparison is wrong exactly where it matters: ``"0.10.0" <
    "0.9.0"``. Release segments compare as numbers with trailing zeros ignored
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
    leading ``v`` (``v0.9.1``, how tags are written) is dropped, and anything
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


def version_in(output: str) -> str | None:
    """The version ``aisquare --version`` printed, or ``None`` when it printed none."""
    match = _VERSION_TOKEN.search(output)
    return match.group(0) if match else None


# --- the latest release -----------------------------------------------------------------


@dataclass(frozen=True)
class LatestRelease:
    """What PyPI says is newest — ``version`` or, when it could not say, ``error``."""

    version: str | None
    error: str | None = None


def fetch_latest(timeout: float = LOOKUP_TIMEOUT_SECONDS) -> LatestRelease:
    """The newest ``aisquare-cli`` on PyPI. Never raises; an unreachable PyPI is an answer.

    Called only when ``aisquare upgrade`` runs — never by ``doctor``, which stays
    offline unless ``--live``. PyPI's number decides only whether there is
    anything to do; whether an upgrade WORKED is decided by asking the new
    install its version, because a mirror may serve a different "latest".
    """
    request = Request(
        PYPI_JSON_URL,
        headers={"Accept": "application/json", "User-Agent": f"{DISTRIBUTION}/{__version__}"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, HTTPException, OSError, TimeoutError, ValueError) as exc:
        # HTTPException is not an OSError: a truncated body raises IncompleteRead.
        return LatestRelease(None, f"could not reach PyPI ({exc})")
    info = payload.get("info") if isinstance(payload, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    if not isinstance(version, str) or version_key(version) is None:
        return LatestRelease(None, "PyPI's answer named no version")
    return LatestRelease(version)


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
    bin_dir: Path | None = None
    """Where uv put the ``aisquare`` executable — so a reinstall puts it there again."""
    unrestatable: tuple[str, ...] = ()


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
        return UvReceipt(unrestatable=(f"an unreadable {RECEIPT_NAME} ({exc})",))
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return UvReceipt(unrestatable=(f"a {RECEIPT_NAME} with no [tool] table",))
    refused: list[str] = []
    requirements = tool.get("requirements")
    if not isinstance(requirements, list):
        requirements = []
    ours: dict[str, Any] | None = None
    withs: list[str] = []
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
        else:
            withs.append(text)
    extras: tuple[str, ...] = ()
    source: tuple[str, str] | None = None
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
        unknown = set(ours) - _REQUIREMENT_KEYS - set(_SOURCE_KEYS) - {"subdirectory"}
        if unknown:
            refused.append(f"{DISTRIBUTION} requirement keys {', '.join(sorted(unknown))}")
    for key in _FILE_ONLY_LISTS:
        if tool.get(key):
            refused.append(f"{key} (uv takes those only as files)")
    options = tool.get("options")
    flags: list[str] = []
    if isinstance(options, dict):
        flags, refused_options = _option_flags(options)
        refused.extend(refused_options)
    python = tool.get("python")
    return UvReceipt(
        extras=extras,
        withs=tuple(withs),
        python=python if isinstance(python, str) and python else None,
        options=tuple(flags),
        source=source,
        bin_dir=_bin_dir(tool.get("entrypoints")),
        unrestatable=tuple(refused),
    )


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


def _direct_url(text: str | None) -> tuple[str, bool] | None:
    """``(url, editable)`` from ``direct_url.json``, or ``None`` for an index install."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("url"), str):
        return None
    dir_info = parsed.get("dir_info")
    editable = isinstance(dir_info, dict) and dir_info.get("editable") is True
    return parsed["url"], editable


def _path_of(url: str) -> str:
    """A ``file://`` URL as the path a person types; any other URL unchanged."""
    if not url.startswith("file://"):
        return url
    parsed = urlparse(url)
    path = unquote(parsed.path)
    if re.match(r"^/[A-Za-z]:", path):  # file:///C:/x on Windows
        path = path[1:]
    return path


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
    if (found.prefix / PIPX_METADATA_NAME).is_file():
        return InstallRoute(PIPX, found)
    formula = _homebrew_formula(found.prefix)
    if formula is not None:
        return InstallRoute(HOMEBREW, found, formula=formula)
    direct = _direct_url(found.direct_url)
    if direct is not None:
        url, editable = direct
        return InstallRoute(EDITABLE if editable else LOCAL_SOURCE, found, source=_path_of(url))
    if found.prefix != found.base_prefix:
        return InstallRoute(VENV, found)
    return InstallRoute(SYSTEM, found)


def detect() -> InstallRoute:
    """The route of THIS process."""
    return classify(facts())


# --- what upgrades it -------------------------------------------------------------------


def _uv_spec(route: InstallRoute, target: str | None) -> list[str]:
    """The package argument(s) for ``uv tool install``: ``name[extras]@latest`` or a source."""
    receipt = route.receipt or UvReceipt()
    extras = f"[{','.join(receipt.extras)}]" if receipt.extras else ""
    if receipt.source is None:
        return [f"{DISTRIBUTION}{extras}@{target or 'latest'}"]
    kind, where = receipt.source
    if kind == "editable":
        return ["-e", f"{where}{extras}"]
    if kind in ("directory", "path"):
        return [f"{where}{extras}"]
    if kind == "git":
        # The recorded revision is left off: reinstalling the commit that is
        # already installed would not be an upgrade, the branch's head is.
        url = where.split("?", 1)[0]
        return [f"{DISTRIBUTION}{extras} @ {url if url.startswith('git+') else 'git+' + url}"]
    return [f"{DISTRIBUTION}{extras} @ {where}"]


def _uv_install_argv(route: InstallRoute, target: str | None) -> list[str]:
    receipt = route.receipt or UvReceipt()
    argv = ["uv", "tool", "install", "--force", "--python"]
    argv.append(receipt.python or route.facts.python_version)
    for requirement in receipt.withs:
        argv.extend(["--with", requirement])
    argv.extend(receipt.options)
    argv.extend(_uv_spec(route, target))
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


def upgrade_argv(route: InstallRoute, target: str | None = None) -> list[str]:
    """The command that moves this install to ``target`` (``None`` = the latest release)."""
    pinned = f"{DISTRIBUTION}=={target}" if target else DISTRIBUTION
    if route.receipt is not None and route.kind != EDITABLE:
        return _uv_install_argv(route, target)
    if route.kind == EDITABLE:
        return ["git", "-C", route.source or ".", "pull"]
    if route.kind == LOCAL_SOURCE:
        return _pip_argv(route, "install", "--upgrade", "--force-reinstall", route.source or ".")
    if route.kind == PIPX:
        if target:
            return ["pipx", "install", "--force", pinned]
        return ["pipx", "upgrade", DISTRIBUTION]
    if route.kind == HOMEBREW:
        return ["brew", "upgrade", route.formula or DISTRIBUTION]
    user = ["--user"] if route.kind == SYSTEM and route.facts.user_install else []
    if target:
        return _pip_argv(route, "install", *user, pinned)
    return _pip_argv(route, "install", "--upgrade", *user, DISTRIBUTION)


#: Why each route is reported rather than run. Short, because it is printed
#: beside the command that does the job.
_NOT_AUTOMATED = {
    EDITABLE: "an editable install follows its checkout — update the checkout instead",
    LOCAL_SOURCE: "it was installed from a local or VCS source, not from PyPI",
    PIPX: "pipx installs are upgraded with pipx",
    HOMEBREW: "Homebrew installs are upgraded with brew",
    VENV: "it lives in a virtual environment this CLI does not manage",
    SYSTEM: "it was installed with pip outside a virtual environment",
}


def not_automated(route: InstallRoute) -> str | None:
    """Why ``aisquare upgrade`` will not run this route's command itself, or ``None``.

    Only the uv tool route runs: it is what the one-line installer makes, its
    receipt says exactly how to reinstall it, and the result is checked in a new
    process afterwards. Everything else is told the exact command instead.
    """
    if route.facts.platform == "win32":
        return (
            "Windows locks the files of a running program, so aisquare cannot replace "
            "itself — quit it first"
        )
    if route.kind != UV_TOOL:
        return _NOT_AUTOMATED.get(route.kind, "this install is not one aisquare manages")
    receipt = route.receipt or UvReceipt()
    if receipt.unrestatable:
        return (
            f"its uv receipt records {'; '.join(receipt.unrestatable)}, which a reinstall "
            "could not carry over"
        )
    if route.facts.prefix.name != DISTRIBUTION:
        return f"the tool environment is named {route.facts.prefix.name!r}, not {DISTRIBUTION!r}"
    if find_uv() is None:
        return "uv is not on PATH"
    return None


def find_uv() -> str | None:
    """Where ``uv`` is on PATH (an indirection so a test decides the answer)."""
    return shutil.which("uv")


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


def command_line(argv: Sequence[str]) -> str:
    """``argv`` as one line a person can paste into their shell."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(list(argv))
    return shlex.join(argv)


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
