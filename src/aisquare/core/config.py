"""Typed TOML configuration: schema, defaults, load and save.

Loading and saving are real; everything that *uses* the config is still
stubbed. Unknown keys in the file are ignored so old configs keep loading.
"""

from __future__ import annotations

import errno
import os
import tomllib
from pathlib import Path
from typing import Any
from uuid import uuid4

import tomli_w
from pydantic import BaseModel, Field

from aisquare.core import paths
from aisquare.models import Pool, RedactionLevel


class CaptureSettings(BaseModel):
    """Settings for the background capture pipeline."""

    enabled: bool = True


class RedactionSettings(BaseModel):
    """Settings controlling how captured data is scrubbed."""

    level: RedactionLevel = RedactionLevel.standard


class ExplainabilityTarget(BaseModel):
    """One explainability deployment (stg, prod, …) this machine can point at.

    Every field falls back to the top-level ``[explainability]`` default when
    unset, so a target is usually two lines. ``api_key_env`` is a *key source*,
    never a key: the CLI reads the named environment variable at the moment it
    needs it. No path to a secrets file is ever baked into config or source —
    the operator sources their own file into the shell (or exports the var by
    any other means) and the CLI just names what it needs.
    """

    gateway_url: str = ""
    api_key_env: str = "EXPLAINABILITY_API_KEY"
    studio_id: str = ""  # informational; the gateway routes by key + agent name
    proxy_url: str | None = None
    agent_name_template: str | None = None
    roles: list[str] | None = None


class ExplainabilitySettings(BaseModel):
    """Settings for tracing agent sessions through the explainability proxy.

    ``enabled`` is False until the stg pipeline is verified green for this
    team — flipping it on is the only opt-in, and every other safeguard
    (proxy health probe, mode check, pre-existing env detection) fails open:
    a session always launches, at worst untraced with a warning.

    ``targets`` carries one entry per deployment, so moving this machine from
    staging to production is a config edit (or ``aisquare explainability enable
    --target prod``) rather than a code change::

        [explainability]
        enabled = true
        target  = "stg"

        [explainability.targets.stg]
        gateway_url = "https://stg-explainability-api.example"

        [explainability.targets.prod]
        gateway_url = "https://explainability-api.example"
        api_key_env = "EXPLAINABILITY_PROD_API_KEY"

    The selector is deliberately NOT called "profile": ``aisquare --profile``
    already means the top-level config profile, and one word for two unrelated
    selectors is how an operator points production traffic at staging.
    Two independent lanes share this section, and either may run without the
    other. ``enabled`` governs the PROXY lane: model traffic from a launched
    agent, routed via ``ANTHROPIC_BASE_URL``. ``ship`` governs the CLIENT lane:
    the insights the CLI itself holds — human prompts and board events — which
    no proxy can see because they never touch the model API. They meet at the
    gateway, in one Run per session, because both key on the board session id.

    ``ship`` is the single predicate on the primary path, so it carries the
    whole "is this configured" question: it is only ever written True once a
    gateway URL and a usable key both exist. No key or no config therefore
    means nothing is captured at all — not captured-then-discarded.

    The key itself is NOT here. ``config.toml`` is a readable settings file
    people paste into issues; a workspace credential lives in
    ``~/.aisquare/explainability-key`` at mode 600, or in
    ``EXPLAINABILITY_API_KEY``.
    """

    enabled: bool = False
    proxy_url: str = "http://127.0.0.1:9090"
    agent_name_template: str = "aisquare-{role}"
    target: str = "stg"
    roles: list[str] = Field(default_factory=lambda: ["planner", "coder", "runner"])
    targets: dict[str, ExplainabilityTarget] = Field(default_factory=dict)
    ship: bool = False
    gateway_url: str = ""


class RoleLaunchProfile(BaseModel):
    """One role's launch spec, carried verbatim and never interpreted.

    ``bin`` is the executable, ``env`` the variables to set, ``args`` extra
    arguments appended to the command. Values in ``env`` get ``~`` and ``$VAR``
    expanded at launch so they read exactly like the shell line they replace::

        [team.profiles.coder1]
        bin = "claude"
        args = ["--model", "opus"]

        [team.profiles.coder1.env]
        CLAUDE_CONFIG_DIR  = "$HOME/.claude2"
        CLAUDE_CODE_TMPDIR = "$HOME/.cache/claude2"

    Nothing here knows what any of those variables MEAN, which is the point.
    An earlier cut understood "accounts" and expanded a bare name into a pair
    of directories — one operator's convention baked into a tool with no
    business knowing it, unusable by anyone laid out differently and liable to
    break for its author the day they reorganised. The operator states the
    spec; we carry it.
    """

    bin: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    args: list[str] = Field(default_factory=list)


class TeamSettings(BaseModel):
    """Per-role launch settings for ``aisquare team``.

    ``profiles`` maps a role to its full launch spec — see
    :class:`RoleLaunchProfile`. This is the general mechanism: it covers
    parallel agent installs, wrapper scripts, proxies, regions, or any other
    knob, without this file learning about any of them.

    ONE map on purpose. #52 landed a narrower ``bins`` (role → executable)
    beside it — a strict subset of ``profiles.<role>.bin``, so two homes for
    one concept and a precedence rule every reader had to carry. It was
    deleted rather than deprecated because #52 is unreleased: no config file
    anywhere holds a ``bins`` key, and a hand-written one still loads, because
    unknown keys are ignored (see the module docstring).

    Resolution order is flag > env > profile > default; see
    ``aisquare.core.harness.resolve_binary`` and ``resolve_profile``.
    """

    profiles: dict[str, RoleLaunchProfile] = Field(default_factory=dict)


class AppConfig(BaseModel):
    """Root configuration object persisted at ``~/.aisquare/config.toml``."""

    profile: str = "default"
    api_url: str = "https://api.aisquare.studio"
    default_pool: Pool = "project"
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)
    explainability: ExplainabilitySettings = Field(default_factory=ExplainabilitySettings)
    team: TeamSettings = Field(default_factory=TeamSettings)


def _keep_unknown(existing: Any, dumped: Any, model: Any) -> Any:
    """``dumped``, plus any key in ``existing`` that ``model`` has no field for.

    A build whose model lacks a field discards it on load and cannot write it
    back, so a config written by a NEWER build and then saved by an OLDER one
    loses everything the older one never heard of. Measured on this machine: a
    build with a three-field ``ExplainabilitySettings`` erased ``target``,
    ``roles``, ``ship``, ``gateway_url`` and ``[explainability.targets]`` —
    exit 0, no warning, and because the tracing seam is fail-open the result is
    a green-looking machine with no tracing.

    The model stays AUTHORITATIVE for everything it knows, including absence:
    unsetting a role binding has to actually remove it, so a key the model has
    a field for is taken from ``dumped`` or not at all. Only keys with no
    corresponding field survive from the file. Recursion follows the model, so a
    field added inside an existing section is preserved too — which is the shape
    the harm actually took, all five lost keys being sub-keys of a section both
    builds knew about.

    A field whose value is a plain container (``targets: dict[str, Target]``)
    has no sub-model to recurse into, so the model owns that subtree entirely
    and it is replaced wholesale. That is correct: its keys are data, and a
    stale entry there is a stale deployment, not an unknown field.
    """
    if not isinstance(existing, dict) or not isinstance(dumped, dict):
        return dumped
    fields = getattr(type(model), "model_fields", None)
    if not fields:
        return dumped
    merged = dict(dumped)
    for key, value in existing.items():
        if key not in fields:
            merged.setdefault(key, value)
        elif key in dumped:
            merged[key] = _keep_unknown(value, dumped[key], getattr(model, key, None))
    return merged


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from ``path`` (default: the standard location).

    A missing file yields the built-in defaults.
    """
    target = path or paths.config_path()
    if not target.exists():
        return AppConfig()
    with target.open("rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)
    return AppConfig.model_validate(data)


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """Write ``config`` as TOML to ``path`` (default: the standard location).

    Parent directories are created on demand. Returns the written path.

    ``exclude_none`` because **TOML has no null**: ``tomli_w`` raises
    ``TypeError`` on ``None`` rather than writing anything, so an optional
    field left unset would make the whole file unwritable. Omitting the key is
    also the correct round-trip — it reloads as the model's default, which is
    the ``None`` we dropped.

    **The write is atomic and follows symlinks.** Both are properties callers
    depend on, and both were previously discovered rather than read, which is why
    they are stated here rather than only commented at the code:

    - The new content goes to a temp file beside the destination and arrives by
      ``os.replace``, so a concurrent reader sees the whole old file or the whole
      new one, never a partial document. The parent directory is flushed after
      the rename so the change survives a hard kill.
    - If ``path`` is a SYMLINK, the file it points at is written and the link is
      preserved. ``os.replace`` swaps the NAME it is given, so without resolving
      it would replace the link with a regular file — and severing a dotfiles
      link is silent in the way that costs most: the tracked file keeps its old
      contents, ``git status`` shows nothing, and the next machine sync restores
      settings that stopped being live.

    Following the link means a write needs permission on the REAL file's
    directory. When it does not have it, this raises and the error names the
    resolved path — not the link the caller passed — because otherwise a
    permission error points at a directory that is perfectly writable. Nothing
    is modified on that path: the link and the original file both survive.

    **A BROKEN link is followed too, and its missing directories are created.**
    Measured: a link at a path four levels deep that does not exist yet produces
    all four directories plus the file, exit 0, and the link then resolves. That
    is what makes a dangling link work rather than fail — but FOLLOWING A LINK
    AND CREATING WHAT IT POINTS AT ARE TWO DECISIONS, and only the first is ours
    to take from a link. Following honours intent the user stated; materialising
    a tree they never created invents it, at a location no command named and
    possibly on another filesystem — a broken link into a mounted Windows drive
    would have this function create directories there, silently.

    So a link whose target DIRECTORY is missing raises instead, naming that
    directory and the remedies. A link into a directory that EXISTS is written
    normally: after a fresh clone only the file is missing, nothing is invented,
    and that case keeps working. ``aisquare doctor`` flags a missing target, so
    the state is visible before a write meets it.

    This restriction applies ONLY when a link was followed. An ordinary first
    write still creates ``~/.aisquare`` — that path never involved a pointer to
    somewhere else.

    Returns the path the CALLER asked for, not the resolved one: commands echo it
    back to an operator, and where the bytes physically land is not their concern.
    """
    target = path or paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    written = Path(os.path.realpath(target))
    if written != target and not written.parent.exists():
        # A link was followed and its destination directory is not there. Create
        # it and we would be inventing a tree at a path no command named; the
        # only honest thing left is to say so. The state is already broken from
        # the user's point of view — their link does not resolve — so nothing
        # that works today stops working, which is what makes this refusal
        # affordable on a seam that must not gain failures.
        raise FileNotFoundError(
            errno.ENOENT,
            f"{target} is a symlink to {written}, but its directory "
            f"{written.parent} does not exist. Following the link is deliberate; "
            f"creating a directory tree there is not. Clone or create "
            f"{written.parent}, or repoint the link.",
            str(written.parent),
        )
    written.parent.mkdir(parents=True, exist_ok=True)

    dumped = config.model_dump(mode="json", exclude_none=True)
    if written.exists():
        # Keys this build has never heard of belong to whoever wrote them; see
        # _keep_unknown. Reading fails open on purpose — a config we cannot parse
        # is exactly the state a write is most likely trying to repair, and
        # refusing to write would strand the operator with the broken file.
        try:
            with written.open("rb") as handle:
                dumped = _keep_unknown(tomllib.load(handle), dumped, config)
        except (OSError, tomllib.TOMLDecodeError):
            pass
    payload = tomli_w.dumps(dumped)

    # Written BESIDE the target and renamed over it, never into the target
    # itself. ``os.replace`` is atomic within a filesystem, so a concurrent
    # reader sees either the whole old file or the whole new one; writing in
    # place truncates first, and anyone reading in that window gets a partial
    # TOML document. Not theoretical on a multi-seat machine — several sessions
    # reach this function, and the caller that suffers most is the QUIETEST one:
    # ``cli/launch.py`` treats an unreadable config as "launching untraced" by
    # design, so a torn write costs tracing silently instead of raising.
    #
    # The temp file is a SIBLING because ``os.replace`` is only atomic within
    # one filesystem — a name under /tmp would reintroduce a copy step. It
    # carries pid plus a random suffix so two writers cannot collide on it, and
    # it is removed on any failure rather than left next to the file an operator
    # reads. ``fsync`` before the rename so a crash cannot publish a file whose
    # contents never reached the disk.
    temp = written.parent / f".{written.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp"
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, written)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        if written == target:
            raise
        # A symlink was followed, so the path that failed is NOT the one the
        # caller passed. Unqualified, the operator reads "permission denied" and
        # goes looking at a directory that is perfectly writable — we would have
        # swapped a silent sever for a confusing error and lost either way. The
        # class and errno are preserved so callers matching on either still work.
        detail = (
            f"{exc.strerror or exc} — {target} is a symlink, so the config is "
            f"written through it and this needs write permission on "
            f"{written.parent}, not on the link"
        )
        try:
            raise type(exc)(exc.errno, detail, str(written)) from exc
        except TypeError:  # an OSError subclass with an unusual signature
            raise OSError(exc.errno, detail, str(written)) from exc
    except BaseException:
        temp.unlink(missing_ok=True)
        raise

    # The rename is atomic the instant it returns, but not yet DURABLE: the new
    # directory entry can still be in cache, so a hard kill or power loss here
    # reverts the file to its previous contents. That is a different property
    # from the one above — a reader never sees a partial file either way — and
    # the cost of skipping it is "your last `explainability enable` did not
    # stick", which `explainability status` reports immediately. It is the last
    # step of the standard durable-replace recipe, and it was missing.
    #
    # MEASURED before adding it rather than assumed cheap: +2.15 ms median per
    # write on this box (2.695 -> 4.845 ms, 200 samples interleaved, ext4 on a
    # native WSL2 disk). Affordable because all ten call sites are explicit
    # operator commands — enable/disable, config set, bind/clear, init — and
    # none is on the launch, session or heartbeat path, so this is paid once per
    # typed command and never in a loop. If that ever stops being true, this is
    # the line to reconsider, and the number above is what to compare against.
    #
    # FAIL-OPEN, deliberately: the write has already succeeded and the caller's
    # change is on disk. A parent we cannot open or sync (read-only mount, an
    # exotic filesystem) must cost durability, never the write itself.
    #
    # Worth knowing: POSIX rename semantics hold here because ~/.aisquare is a
    # native ext4 disk. On a DrvFs//mnt/c or \\wsl.localhost path the guarantee
    # softens, and nothing in this code can tell which kind of path it is on.
    try:
        directory = os.open(written.parent, os.O_RDONLY)
    except OSError:
        return target
    try:
        os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)
    return target
