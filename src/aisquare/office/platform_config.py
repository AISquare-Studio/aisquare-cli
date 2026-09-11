"""Which platform this Office talks to, as whom, and what that identity reaches.

Three things live here and nothing else does.

**Profile resolution.** A *profile* is not a new concept: it is the CLI's
existing explainability target, resolved through
:func:`aisquare.services.explainability_ops.resolve_target`, which already owns
the precedence rules, the key-file fallback and the guard that stops a staging
key satisfying a production target. This module borrows that function rather
than re-deriving any of it, because two readers of one credential is the defect
``tests/test_one_key_resolver.py`` exists to catch.

**Binding resolution.** :func:`resolve_binding` turns a profile plus an
explicit workspace into P01's :class:`PlatformBinding`, or refuses. It refuses
loudly and specifically: a missing workspace, a non-numeric workspace, an
unusable base URL and an absent credential are four different sentences, each
naming the setting to fix and none quoting its value. Nothing is guessed — not
from the repository basename, the local path, the display name, the model or
anything the browser sent.

**Capability facts.** The deployed gateway's studio-scoped Praxis routes reject
a workspace-scoped key, and the CLI's only credential *is* a workspace key.
That is a property of the deployment, not an outage, so it is recorded here as
data and enforced before transport. See :data:`WORKSPACE_KEY_CAPABILITIES`.

**No credential is ever a field of anything this module returns.** The key is
resolved at request time and used once; what persists is a fingerprint, folded
into the binding revision so a rotated or swapped key cannot share a cache
entry with the key it replaced. The fingerprint is internal: it appears in a
cache key and in the integer revision, never in a return value, a log line, an
error detail or an exception message.

Nothing here performs a network call, and importing it starts no client: the
office extra is not imported at all. ``platform_transport`` is the only module
that speaks HTTP.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urlparse

from aisquare.office.config import OfficeConfig
from aisquare.office.models import PlatformBinding, ServiceError
from aisquare.office.platform_redaction import sanitize_detail

WORKSPACE_ENV_VAR: Final = "AISQUARE_OFFICE_PLATFORM_WORKSPACE"
STUDIO_ENV_VAR: Final = "AISQUARE_OFFICE_PLATFORM_STUDIO"
AGENT_UID_ENV_VAR: Final = "AISQUARE_OFFICE_PLATFORM_AGENT_UID"
"""Where the scope comes from.

``OfficeConfig`` already carries ``platform_profile``; it carries no workspace,
studio or agent, and P01's module is not P12's to edit. These three variables
are read from an *injected* mapping in the same spirit as ``OfficeConfig``
itself — never from ``os.environ`` at import — and are listed in the handoff as
a proposed amendment to that config's allow-list.
"""

LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
"""The only hosts allowed to be reached over plain ``http``."""

MAX_BASE_URL_CHARS: Final = 512
MAX_SCOPE_ID_CHARS: Final = 64

_FINGERPRINT_PERSON: Final = b"asq-office-p12"
"""Domain separation for the credential fingerprint, so a digest computed here
is not comparable with one computed anywhere else from the same key."""

_REVISION_BYTES: Final = 7
"""56 bits of the digest as the revision. Wide enough that two distinct scopes
colliding is not a thing that happens, narrow enough to stay a friendly int."""


class PlatformConfigError(ValueError):
    """A platform setting is unusable.

    Like :class:`aisquare.office.config.OfficeConfigError`, the message names
    the setting and never the value: a bad value here is frequently a key
    pasted into the wrong variable, and an error message is the one place a
    secret reliably outlives the mistake.
    """


@dataclass(frozen=True, slots=True)
class PlatformProfile:
    """One resolved deployment: where to send, and *which variable* holds the key.

    The key itself is not here, on purpose. The base URL is here and not on
    :class:`PlatformBinding`, per SHARED.md: the endpoint belongs to the
    profile, and a change to it invalidates the binding revision through
    :func:`resolve_binding` rather than through a competing schema.
    """

    name: str
    base_url: str
    api_key_env: str
    studio_id: str | None = None
    key_source: str = "unset"
    """``"env"``, ``"file"`` or ``"unset"`` — where the key won, not its value.
    Shown in diagnostics so an operator rotates the source actually in play."""

    @property
    def has_credential(self) -> bool:
        return self.key_source in ("env", "file")


class CredentialSource(Protocol):
    """The seam between Office and the CLI's existing secure key handling.

    Two methods rather than one because they are called at different moments
    and only one of them touches a secret. :meth:`profile` is safe to call
    while building a binding or answering a capability question;
    :meth:`credential` is called inside request execution and its return value
    is used once and never stored.
    """

    def profile(self, name: str | None) -> PlatformProfile:
        """The named deployment's endpoint and key *source*, never the key."""

    def credential(self, profile: PlatformProfile) -> str | None:
        """The workspace key for ``profile``, resolved now. Never cached here."""


class CliCredentialSource:
    """The real source: the CLI's explainability targets and key file.

    Both methods import ``aisquare.services.explainability_ops`` lazily. That
    module pulls in the CLI's config loader and a good deal else, and P12's
    checklist requires that ordinary CLI import costs nothing here — a
    module-level import would make merely *having* Office installed drag the
    operator surface into every hook process.
    """

    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        self._env = env

    def _resolve(self, name: str | None) -> PlatformProfile:
        from aisquare.core.config import load_config
        from aisquare.services.explainability_ops import resolve_target

        settings = load_config().explainability
        target = resolve_target(settings, name, env=self._env)
        return PlatformProfile(
            name=target.name,
            base_url=target.gateway_url,
            api_key_env=target.api_key_env,
            studio_id=target.studio_id or None,
            key_source=target.key_source,
        )

    def profile(self, name: str | None) -> PlatformProfile:
        return self._resolve(name)

    def credential(self, profile: PlatformProfile) -> str | None:
        from aisquare.core.config import load_config
        from aisquare.services.explainability_ops import resolve_target

        settings = load_config().explainability
        return resolve_target(settings, profile.name, env=self._env).api_key


# --------------------------------------------------------------------------
# Capability facts
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteCapability:
    """One deployed route family and whether this credential kind reaches it."""

    pattern: re.Pattern[str]
    reachable: bool
    reason: str
    """Why, in one sentence an operator can act on. Stated as a property of the
    deployment, so a caller is never tempted to read it as a transient fault."""


WORKSPACE_KEY_CAPABILITIES: Final[tuple[RouteCapability, ...]] = (
    RouteCapability(
        pattern=re.compile(r"^/v1/studios/[^/]+/praxis/context/?$"),
        reachable=True,
        reason=(
            "the only studio-scoped Praxis route moved onto require_policy_check_access, "
            "which accepts a workspace key that owns the path studio"
        ),
    ),
    RouteCapability(
        pattern=re.compile(r"^/v1/studios/[^/]+/praxis/insights/[^/]+/chain/?$"),
        reachable=False,
        reason=(
            "studio-scoped Praxis provenance is guarded by require_studio_access, which "
            "rejects a workspace-scoped key with 403 for every studio"
        ),
    ),
    RouteCapability(
        pattern=re.compile(r"^/v1/studios/[^/]+/praxis/insights/?$"),
        reachable=False,
        reason=(
            "studio-scoped Praxis lessons are guarded by require_studio_access, which "
            "rejects a workspace-scoped key with 403 for every studio"
        ),
    ),
    RouteCapability(
        pattern=re.compile(r"^/v1/studios/[^/]+/praxis/signals/?$"),
        reachable=False,
        reason=(
            "platform signal submission is studio-scoped and rejects a workspace-scoped "
            "key, so teaching is capability-unavailable under this profile"
        ),
    ),
    RouteCapability(
        pattern=re.compile(r"^/v1/studios/[^/]+/praxis/runs/[^/]+/injection/?$"),
        reachable=False,
        reason=("recorded injections are studio-scoped and reject a workspace-scoped key with 403"),
    ),
)
"""What a workspace-scoped key reaches on the deployed gateway.

Recorded as a **capability fact**, never retried. The reasoning, from the
coordinator's resolved question: ``validate_api_key`` is an alias for
``require_studio_access`` and ``validate_ingest_api_key`` returns ``None`` for
a workspace key by design, so every studio Praxis handler comparing
``auth_studio_id != studio_id`` rejects unconditionally. One route —
``praxis/context`` — was deliberately moved onto ``require_policy_check_access``
and is reachable when the workspace owns the studio.

Order matters: the ``chain`` pattern precedes the bare ``insights`` pattern so
the more specific family is matched first.

Everything not listed is *unknown*, not reachable. :func:`route_capability`
returns ``None`` for an unmatched path and the transport sends it, because
declaring a route dead on no evidence is the same error as declaring one alive
on no evidence.
"""


def route_capability(path: str) -> RouteCapability | None:
    """The recorded fact about ``path``, or None when nothing is recorded."""
    for capability in WORKSPACE_KEY_CAPABILITIES:
        if capability.pattern.match(path):
            return capability
    return None


# --------------------------------------------------------------------------
# Binding resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BindingResolution:
    """Exactly one of ``binding`` or ``error``, the shape ``TransportResult`` uses.

    A resolution failure is a :class:`ServiceError` rather than an exception
    because "this Office has no platform configured" is an ordinary, displayable
    state that must not interrupt local fleet observation, Team OS reads or the
    SSE heartbeat.
    """

    binding: PlatformBinding | None = None
    profile: PlatformProfile | None = None
    error: ServiceError | None = None

    def __post_init__(self) -> None:
        if (self.binding is None) == (self.error is None):
            raise ValueError("BindingResolution carries exactly one of binding or error")
        if self.binding is not None and self.profile is None:
            raise ValueError("a resolved binding always carries the profile it resolved from")


def _unconfigured(detail: str) -> BindingResolution:
    return BindingResolution(
        error=ServiceError(
            code="service_unconfigured",
            detail=sanitize_detail(detail),
            retryable=False,
        )
    )


def normalize_base_url(value: str) -> str:
    """The profile's endpoint, normalised once, or :class:`PlatformConfigError`.

    Stricter than :func:`aisquare.services.explainability._usable_base_url`,
    which only has to keep an unusable value out of an agent's environment.
    This one guards a credential: ``https`` is required off loopback, userinfo
    is refused outright (a key pasted into a URL is the classic way one reaches
    a log), and a base URL carrying its own query or fragment is refused
    because the transport appends a path to it.
    """
    text = value.strip()
    if not text:
        raise PlatformConfigError("the platform profile has no gateway URL")
    if len(text) > MAX_BASE_URL_CHARS:
        raise PlatformConfigError("the platform profile's gateway URL is implausibly long")
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise PlatformConfigError("the platform profile's gateway URL is not a URL") from exc
    if parsed.scheme not in ("http", "https"):
        raise PlatformConfigError("the platform gateway URL must be http or https")
    if not parsed.netloc:
        raise PlatformConfigError("the platform gateway URL names no host")
    if "@" in parsed.netloc:
        raise PlatformConfigError(
            "the platform gateway URL carries userinfo; credentials belong in the key source"
        )
    if parsed.query or parsed.fragment:
        raise PlatformConfigError(
            "the platform gateway URL must be a bare base URL, without a query or fragment"
        )
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise PlatformConfigError("the platform gateway URL must use https unless it is loopback")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def credential_fingerprint(credential: str) -> str:
    """A stable, domain-separated digest of a key — never the key.

    Its only purposes are to change the binding revision when the credential
    changes and to keep two credentials' cached results apart. It is not
    returned by any public transport call and must not be logged: a digest is
    not a secret, but a digest an attacker can compare against their own guess
    is one more thing than they had.
    """
    return hashlib.blake2b(
        credential.encode("utf-8"), digest_size=16, person=_FINGERPRINT_PERSON
    ).hexdigest()


def _binding_id(
    profile_name: str, workspace_id: str, studio_id: str | None, agent_uid: str | None
) -> str:
    material = " | ".join((profile_name, workspace_id, studio_id or "", agent_uid or ""))
    digest = hashlib.blake2b(
        material.encode("utf-8"), digest_size=8, person=_FINGERPRINT_PERSON
    ).hexdigest()
    return f"pb_{digest}"


def revision_for(
    profile: PlatformProfile,
    *,
    workspace_id: str,
    studio_id: str | None,
    agent_uid: str | None,
    credential: str,
) -> int:
    """An integer that changes when anything the binding depends on changes.

    Derived rather than counted, so it needs no storage and two processes
    resolving the same configuration agree without coordinating. It includes
    the endpoint and the credential fingerprint as well as the scope, which is
    what makes it usable as the hard cache boundary SHARED.md asks for: a
    rotated key, a repointed gateway or a changed studio all produce a
    different revision, and every cache entry and operation record pinned to
    the old one is unreachable from the new.

    Not monotonic, and nothing requires it to be — ``revision`` answers "is this
    the same binding as before", never "is this newer".
    """
    material = " | ".join(
        (
            profile.name,
            profile.base_url,
            profile.api_key_env,
            workspace_id,
            studio_id or "",
            agent_uid or "",
            credential_fingerprint(credential),
        )
    )
    digest = hashlib.blake2b(
        material.encode("utf-8"), digest_size=16, person=_FINGERPRINT_PERSON
    ).digest()
    return int.from_bytes(digest[:_REVISION_BYTES], "big")


def _scope_value(env: Mapping[str, str], variable: str) -> str | None:
    value = env.get(variable, "").strip()
    return value or None


def resolve_binding(
    *,
    project_id: str,
    config: OfficeConfig,
    source: CredentialSource,
    env: Mapping[str, str],
) -> BindingResolution:
    """The platform scope for one project, or a displayable reason there is none.

    Pure with respect to the process: the environment and the credential source
    are arguments, so a test resolves a binding by constructing one rather than
    by monkeypatching, and no ambient operator shell can make this answer
    differently inside the suite.
    """
    if not project_id.strip():
        return _unconfigured("Office resolved no project, so no platform binding applies")

    try:
        profile = source.profile(config.platform_profile)
    except Exception as exc:  # a missing config file must not take the server down
        return _unconfigured(f"the platform profile could not be resolved: {type(exc).__name__}")

    try:
        base_url = normalize_base_url(profile.base_url)
    except PlatformConfigError as exc:
        return _unconfigured(str(exc))

    workspace_id = _scope_value(env, WORKSPACE_ENV_VAR)
    if workspace_id is None:
        return _unconfigured(
            f"no platform workspace is configured; set {WORKSPACE_ENV_VAR} to the "
            "workspace this Office reads. It is never inferred from the project name."
        )
    if len(workspace_id) > MAX_SCOPE_ID_CHARS or not workspace_id.isdigit():
        return _unconfigured(
            f"{WORKSPACE_ENV_VAR} must be the numeric workspace id the gateway uses"
        )

    studio_id = _scope_value(env, STUDIO_ENV_VAR) or profile.studio_id
    if studio_id is not None and len(studio_id) > MAX_SCOPE_ID_CHARS:
        return _unconfigured(f"{STUDIO_ENV_VAR} is implausibly long for a studio id")

    agent_uid = _scope_value(env, AGENT_UID_ENV_VAR)
    if agent_uid is not None:
        try:
            uuid.UUID(agent_uid)
        except ValueError:
            return _unconfigured(
                f"{AGENT_UID_ENV_VAR} must be the agent's UUID. A local agent id, a board "
                "session id and a gateway run id are different namespaces."
            )

    credential = source.credential(profile)
    if not credential:
        return _unconfigured(
            f"no workspace key is available; export ${profile.api_key_env} or store the "
            "key with the CLI's own key file. Office never reads a key from config.toml."
        )

    resolved = PlatformProfile(
        name=profile.name,
        base_url=base_url,
        api_key_env=profile.api_key_env,
        studio_id=studio_id,
        key_source=profile.key_source,
    )
    return BindingResolution(
        binding=PlatformBinding(
            binding_id=_binding_id(resolved.name, workspace_id, studio_id, agent_uid),
            revision=revision_for(
                resolved,
                workspace_id=workspace_id,
                studio_id=studio_id,
                agent_uid=agent_uid,
                credential=credential,
            ),
            project_id=project_id,
            profile_name=resolved.name,
            workspace_id=workspace_id,
            studio_id=studio_id,
            agent_uid=agent_uid,
        ),
        profile=resolved,
    )


__all__ = [
    "AGENT_UID_ENV_VAR",
    "STUDIO_ENV_VAR",
    "WORKSPACE_ENV_VAR",
    "WORKSPACE_KEY_CAPABILITIES",
    "BindingResolution",
    "CliCredentialSource",
    "CredentialSource",
    "PlatformConfigError",
    "PlatformProfile",
    "RouteCapability",
    "credential_fingerprint",
    "normalize_base_url",
    "resolve_binding",
    "revision_for",
    "route_capability",
]
