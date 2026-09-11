"""Bounded HTTP to the deployed platform. One primitive, and not a proxy.

This is the only module in Office that speaks to the Explainability gateway. It
answers exactly one question — "what did that endpoint say, within these
bounds?" — and refuses every other job: it decides no domain state, builds no
:class:`~aisquare.office.models.ServiceResult`, and returns nothing a browser
could be handed. P13 and P14 interpret; P12 fetches.

**What a result carries.** A :class:`~aisquare.office.models.TransportResult`
holds exactly one of a decoded :class:`~aisquare.office.models.TransportResponse`
or a sanitised :class:`~aisquare.office.models.ServiceError`. The split is not
"success or failure": *any* HTTP answer that arrives and decodes within the
bounds becomes a response, whatever its status, and only a failure to obtain
one at all becomes an error. That is deliberate and it is the single most
consequential decision in this module — see :func:`classify_status`.

**Bounds.** Separate connect and read deadlines plus a wall-clock total budget
enforced over the body read, a byte ceiling applied while streaming rather than
after buffering, and depth/length/size ceilings applied to the decoded JSON
before anything is cached. A response that breaches one is an
``upstream_invalid`` error, never a partially-applied body.

**The credential.** Resolved once per request through the CLI's own secure
facility, sent once in ``X-API-KEY``, and never stored, logged, cached, placed
in a URL, embedded in a cache key or formatted into an exception. Every string
that leaves this module has been through
:func:`~aisquare.office.platform_redaction.sanitize_detail` with the live key as
an argument, so even a transport library quoting a header value in its own
exception cannot carry it out.

**No retries.** Not for a GET either. A retry policy belongs where the caller
knows whether it is still worth having the answer, and a retry here would put a
non-idempotent request one bug away from running twice.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlencode

import httpx2

from aisquare.office.config import OfficeConfig
from aisquare.office.models import (
    PlatformBinding,
    PlatformQuery,
    ServiceError,
    ServiceErrorCode,
    ServiceStatus,
    TransportResponse,
    TransportResult,
)
from aisquare.office.platform_config import (
    CredentialSource,
    PlatformConfigError,
    PlatformProfile,
    normalize_base_url,
    revision_for,
    route_capability,
)
from aisquare.office.platform_redaction import (
    allowed_headers,
    body_shape,
    detail_of,
    request_id_from,
    sanitize_detail,
    scrub_credential,
)
from aisquare.office.ports import Clock

MAX_BODY_BYTES: Final = 2 * 1_048_576
"""Two mebibytes. A hundred run rows of forty fields is roughly 150 kB, so this
is an order of magnitude of headroom and still small enough that a gateway
answering with something unexpected cannot become Office's memory problem."""

READ_CHUNK_BYTES: Final = 64 * 1024
MAX_JSON_DEPTH: Final = 12
MAX_JSON_ARRAY: Final = 2_000
MAX_JSON_STRING: Final = 20_000
MAX_JSON_NODES: Final = 100_000

WRITE_TOTAL_S: Final = 10.0
"""The separately named budget for a write. Deliberately not the ordinary read
budget: a submission that takes four seconds has still submitted, and abandoning
it at three would leave an outcome nobody can classify."""

CONTEXT_TOTAL_S: Final = 2.0
"""The context-like budget. The gateway's own hop to the Praxis engine gives up
at 0.8 s and answers 502, so waiting the ordinary three seconds buys a caller
nothing it could not have had in two."""

NOT_FOUND_OR_MASKED: Final = "not_found_or_masked"
"""The prefix a classified 404 detail carries.

P01's ``ServiceErrorCode`` has no member for it, and inventing one would have
meant editing a module P12 does not own. The status code is preserved on the
response regardless, which is what actually keeps the four meanings of a 404 in
this API — absent workspace, masked object, not-built-yet, gone-for-good —
distinguishable to P13. Recorded in the handoff as a proposed amendment.
"""

JSON_CONTENT_TYPES: Final = ("application/json", "+json", "application/problem+json")

_PATH_OK = re.compile(r"^/v1/[A-Za-z0-9._~%/-]{1,480}$")
_WORKSPACE_PATH = re.compile(r"^/v1/workspaces/([^/]+)")
_STUDIO_PATH = re.compile(r"^/v1/studios/([^/]+)")


@dataclass(frozen=True, slots=True)
class TimeoutBudget:
    """Connect, read and a wall-clock total.

    Three numbers rather than one because they fail differently. ``connect``
    bounds a gateway that is not there; ``read`` bounds one that accepted the
    connection and then went quiet; ``total`` bounds the case neither catches —
    a body that keeps arriving, slowly, forever, one chunk inside every read
    deadline.
    """

    connect: float
    read: float
    total: float


def budget_for(timeout_class: str, config: OfficeConfig) -> TimeoutBudget | None:
    """The named budget, or None when the name is not one of ours.

    ``ordinary`` follows ``OfficeConfig.remote_timeout_s`` — the documented 3 s
    starting point — so an operator who retunes it retunes this too.
    """
    ordinary = float(config.remote_timeout_s)
    if timeout_class == "ordinary":
        return TimeoutBudget(connect=min(1.0, ordinary), read=ordinary, total=ordinary)
    if timeout_class == "context":
        total = min(CONTEXT_TOTAL_S, ordinary)
        return TimeoutBudget(connect=min(1.0, total), read=total, total=total)
    if timeout_class == "write":
        return TimeoutBudget(connect=2.0, read=WRITE_TOTAL_S, total=WRITE_TOTAL_S)
    return None


@dataclass(frozen=True, slots=True)
class Probe:
    """One read-only preflight request, with the reason it is safe to make."""

    name: str
    method: str
    path: str
    query: PlatformQuery | None
    timeout_class: str
    why: str


class _BodyRejected(Exception):
    """The decoded body breached a bound. Carries the sentence to report."""


def _error(code: ServiceErrorCode, detail: str, *, retryable: bool) -> TransportResult:
    return TransportResult(
        error=ServiceError(code=code, detail=sanitize_detail(detail), retryable=retryable)
    )


def classify_status(
    status_code: int, *, detail: str | None = None
) -> tuple[ServiceStatus, ServiceError | None]:
    """What one HTTP status means for availability. Advisory, and pure.

    P13/P14 own the domain reading — a 200 whose body says ``processing`` is a
    successful read of a run that is not finished — so this answers only the
    availability question, and it answers it from the status plus the upstream's
    own sanitised ``detail`` where that detail is the only discriminator. Three
    places where it is:

    * **503** is two different things. ``praxis not configured`` is a permanent
      capability fact for the deployment and retrying it forever is wrong; an
      all-studios outage or an unreachable IAM is genuinely retryable.
    * **403** on the context route is ambiguous between a key lacking
      ``ingest:write`` and a workspace that does not own the studio, because the
      ingest scope check runs first. Both are ``forbidden``, and the detail says
      which so an operator fixes the right one.
    * **404** carries four meanings in this API and none of them is resolved
      here; the caller reads the preserved status and the prose.

    A 400 or a 422 is reported as ``unavailable`` with ``retryable`` false,
    which reads oddly and is the honest option available: the service is up and
    has permanently refused *this* request, and P01's ``ServiceStatus`` — which
    P12 does not own — has no member for that. The false ``retryable`` is the
    load-bearing half.
    """
    text = detail or ""
    lowered = text.lower()
    if 200 <= status_code < 300:
        return "ok", None
    if status_code == 401:
        return "unauthorized", ServiceError(
            code="unauthorized",
            detail=sanitize_detail(text or "the platform refused the request unauthenticated"),
            retryable=False,
        )
    if status_code == 403:
        if "scope" in lowered:
            hint = (
                "the workspace key is refused for lacking the required scope, "
                "not for the workspace owning the wrong studio"
            )
        elif "workspace" in lowered:
            hint = "the configured workspace key is not scoped to the requested workspace"
        elif "studio" in lowered:
            hint = "the configured workspace does not own the requested studio"
        else:
            hint = (
                "the platform refused this scope; it is ambiguous between a missing key "
                "scope and a workspace that does not own the studio"
            )
        return "forbidden", ServiceError(
            code="forbidden", detail=sanitize_detail(hint), retryable=False
        )
    if status_code == 404:
        return "unavailable", ServiceError(
            code="upstream_error",
            detail=sanitize_detail(f"{NOT_FOUND_OR_MASKED}: {text or 'the platform answered 404'}"),
            retryable=False,
        )
    if status_code == 429:
        return "unavailable", ServiceError(
            code="rate_limited",
            detail=sanitize_detail(text or "the platform is rate limiting this credential"),
            retryable=True,
        )
    if status_code == 501:
        return "unsupported", ServiceError(
            code="unsupported_capability",
            detail=sanitize_detail(text or "the platform does not implement this route"),
            retryable=False,
        )
    if status_code == 503 and "not configured" in lowered:
        return "unsupported", ServiceError(
            code="unsupported_capability",
            detail=sanitize_detail(
                "this deployment has no Praxis configured; that is a capability fact, not an outage"
            ),
            retryable=False,
        )
    if status_code >= 500:
        return "unavailable", ServiceError(
            code="service_unavailable",
            detail=sanitize_detail(text or f"the platform answered {status_code}"),
            retryable=True,
        )
    return "unavailable", ServiceError(
        code="upstream_invalid",
        detail=sanitize_detail(text or f"the platform refused the request with {status_code}"),
        retryable=False,
    )


def normalized_query(query: PlatformQuery | None) -> tuple[tuple[str, str], ...]:
    """Allow-listed pairs in one deterministic order.

    Sorted by key and then value so two callers spelling the same query
    differently share a cache entry, and so two different queries never do.
    """
    if query is None:
        return ()
    pairs: list[tuple[str, str]] = []
    if query.limit is not None:
        pairs.append(("limit", str(query.limit)))
    if query.cursor is not None:
        pairs.append(("cursor", query.cursor))
    pairs.extend((str(key), str(value)) for key, value in query.filters.items())
    return tuple(sorted(pairs))


def _bounded(value: object, *, depth: int = 0, budget: list[int] | None = None) -> object:
    """``value`` if it is within every JSON bound, else :class:`_BodyRejected`.

    Applied after ``json.loads`` and before anything is cached or returned.
    Checking rather than truncating: a silently shortened array would be handed
    to P13 as if it were the whole page, and a page that is quietly wrong is
    worse than a read that failed.
    """
    remaining = [MAX_JSON_NODES] if budget is None else budget
    remaining[0] -= 1
    if remaining[0] < 0:
        raise _BodyRejected("the platform response has more elements than Office will decode")
    if depth > MAX_JSON_DEPTH:
        raise _BodyRejected("the platform response nests deeper than Office will decode")
    if isinstance(value, dict):
        for key, item in value.items():
            if len(str(key)) > MAX_JSON_STRING:
                raise _BodyRejected("the platform response has an implausibly long key")
            _bounded(item, depth=depth + 1, budget=remaining)
        return value
    if isinstance(value, list):
        if len(value) > MAX_JSON_ARRAY:
            raise _BodyRejected("the platform response has a longer array than Office will decode")
        for item in value:
            _bounded(item, depth=depth + 1, budget=remaining)
        return value
    if isinstance(value, str) and len(value) > MAX_JSON_STRING:
        raise _BodyRejected("the platform response has a longer text field than Office will decode")
    return value


@dataclass
class _CacheEntry:
    binding_id: str
    revision: int
    result: TransportResult
    stored_monotonic: float


def default_client() -> httpx2.Client:
    """The client this transport uses unless one is injected.

    ``trust_env`` is off, which is the security decision in this function: it
    stops an ambient ``HTTPS_PROXY`` from routing a workspace key through
    whatever an operator's shell happens to name, and stops ``.netrc`` from
    attaching a second credential. Redirects are not followed, because a
    redirect is a host change and this transport sends a key.
    """
    return httpx2.Client(
        trust_env=False,
        follow_redirects=False,
        http2=False,
        limits=httpx2.Limits(max_connections=4, max_keepalive_connections=2),
    )


class HttpPlatformTransport:
    """The :class:`~aisquare.office.ports.PlatformTransport` implementation."""

    def __init__(
        self,
        *,
        config: OfficeConfig,
        clock: Clock,
        source: CredentialSource,
        client: httpx2.Client | None = None,
        cache_ttl_s: float = 15.0,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        self._config = config
        self._clock = clock
        self._source = source
        self._client = client if client is not None else default_client()
        self._cache_ttl_s = cache_ttl_s
        self._max_body_bytes = max_body_bytes
        self._cache: dict[tuple[str, ...], _CacheEntry] = {}
        self._last_success: dict[tuple[str, int], datetime] = {}

    # -- public surface ----------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        query: PlatformQuery | None = None,
        body: Mapping[str, object] | None = None,
        binding: PlatformBinding,
        timeout_class: str = "ordinary",
    ) -> TransportResult:
        """One bounded request. Never raises, never retries, never proxies."""
        verb = method.upper()
        if verb not in ("GET", "POST"):
            return _error("internal", f"Office does not issue {verb} upstream", retryable=False)

        budget = budget_for(timeout_class, self._config)
        if budget is None:
            return _error("internal", f"unknown timeout class {timeout_class!r}", retryable=False)

        path_problem = _path_problem(path)
        if path_problem is not None:
            return _error("internal", path_problem, retryable=False)

        scope_problem = _scope_problem(path, binding)
        if scope_problem is not None:
            return _error("binding_required", scope_problem, retryable=False)

        capability = route_capability(path)
        if capability is not None and not capability.reachable:
            return _error("unsupported_capability", capability.reason, retryable=False)

        try:
            profile = self._source.profile(binding.profile_name)
            base_url = normalize_base_url(profile.base_url)
        except PlatformConfigError as exc:
            return _error("service_unconfigured", str(exc), retryable=False)
        except Exception as exc:
            return _error(
                "service_unconfigured",
                f"the platform profile could not be resolved: {type(exc).__name__}",
                retryable=False,
            )

        credential = self._source.credential(profile)
        if not credential:
            self.invalidate(binding_id=binding.binding_id)
            return _error(
                "service_unconfigured",
                f"no workspace key is available; export ${profile.api_key_env}",
                retryable=False,
            )

        stale = self._stale_binding(profile, base_url, binding, credential)
        if stale is not None:
            return stale

        pairs = normalized_query(query)
        key = self._cache_key(binding, base_url, verb, path, pairs)
        if verb == "GET":
            cached = self._cached(key)
            if cached is not None:
                return cached

        result = self._send(
            verb,
            base_url + path,
            pairs=pairs,
            body=body,
            credential=credential,
            budget=budget,
        )

        response = result.response
        if response is not None:
            if response.status_code in (401, 403):
                # A revoked or rescoped credential is a hard cache boundary:
                # nothing already fetched under it may keep being displayed.
                self.invalidate(binding_id=binding.binding_id)
            elif 200 <= response.status_code < 300:
                self._last_success[(binding.binding_id, binding.revision)] = response.received_at
                if verb == "GET":
                    self._cache[key] = _CacheEntry(
                        binding_id=binding.binding_id,
                        revision=binding.revision,
                        result=result,
                        stored_monotonic=self._clock.monotonic(),
                    )
        return result

    def invalidate(self, *, binding_id: str | None = None, revision: int | None = None) -> int:
        """Drop cached results for a binding, a revision, or everything.

        Returns how many entries went, so a caller that must prove a revocation
        took effect can assert on it rather than on the absence of a later read.
        """
        doomed = [
            key
            for key, entry in self._cache.items()
            if (binding_id is None or entry.binding_id == binding_id)
            and (revision is None or entry.revision == revision)
        ]
        for key in doomed:
            del self._cache[key]
        if binding_id is not None:
            for success_key in [
                candidate
                for candidate in self._last_success
                if candidate[0] == binding_id and (revision is None or candidate[1] == revision)
            ]:
                del self._last_success[success_key]
        return len(doomed)

    def last_success_at(self, binding: PlatformBinding) -> datetime | None:
        """When this exact binding revision last read something successfully.

        P12 supplies the freshness fact; P13/P14 decide whether authorised stale
        data is worth showing. An unauthorised binding has none, because
        :meth:`invalidate` removed it at the moment of the refusal.
        """
        return self._last_success.get((binding.binding_id, binding.revision))

    def capture_fixture(self, result: TransportResult, name: str) -> dict[str, object]:
        """Redacted fixture metadata for ``result``: status, shape, no values.

        Behind an explicit call, per the packet checklist — an ordinary request
        writes nothing to disk, and this returns a dictionary rather than
        writing one, so the decision about where a fixture lands stays with the
        preflight or the test that asked for it.

        Header *names* are recorded but not their values: ``Date`` and
        ``X-Request-Id`` are values about one real exchange, and a fixture is a
        statement about a shape.
        """
        response = result.response
        if response is None:
            error = result.error
            return {
                "name": name,
                "captured": False,
                "error": (
                    {"code": error.code, "retryable": error.retryable}
                    if error is not None
                    else None
                ),
            }
        status, _ = classify_status(response.status_code, detail=detail_of(response.json_body))
        return {
            "name": name,
            "captured": True,
            "status_code": response.status_code,
            "service_status": status,
            "headers_present": sorted(response.allowed_headers),
            "content_type": response.allowed_headers.get("content-type", ""),
            "has_request_id": response.request_id is not None,
            "shape": body_shape(response.json_body),
        }

    # -- preflight ---------------------------------------------------------

    def preflight_plan(self, binding: PlatformBinding) -> tuple[Probe, ...]:
        """The read-only probes this binding supports, in order, as data.

        Returning the plan rather than performing it is what lets a reviewer
        check the two things that matter before anything is sent: that no probe
        carries ``run_id`` — the context assembler durably records an injection
        when it sees one, which would fabricate evidence that an agent consumed
        a packet it never saw — and that no probe writes.

        There is no health probe first, because this gateway documents none;
        inventing one would be calling a route on no evidence, which is the
        habit the packet brief is most insistent about.
        """
        probes: list[Probe] = [
            Probe(
                name="workspace-runs-list",
                method="GET",
                path=f"/v1/workspaces/{binding.workspace_id}/runs",
                query=PlatformQuery(limit=1),
                timeout_class="ordinary",
                why="the narrowest scoped read that proves the credential and shape at once",
            )
        ]
        if binding.agent_uid is not None:
            probes.append(
                Probe(
                    name="praxis-agent-insights",
                    method="GET",
                    path=(
                        f"/v1/workspaces/{binding.workspace_id}"
                        f"/praxis/agents/{binding.agent_uid}/insights"
                    ),
                    query=PlatformQuery(limit=1),
                    timeout_class="ordinary",
                    why="the only Praxis read a workspace-scoped key is known to reach",
                )
            )
        if binding.studio_id is not None:
            probes.append(
                Probe(
                    name="praxis-context",
                    method="GET",
                    path=f"/v1/studios/{binding.studio_id}/praxis/context",
                    query=None,
                    timeout_class="context",
                    why="assembles nothing durable while no run_id is sent",
                )
            )
        return tuple(probes)

    def run_preflight(self, binding: PlatformBinding) -> tuple[tuple[Probe, TransportResult], ...]:
        """Execute the plan, plus one run detail if the list supplied an id.

        The detail probe exists only in this method and never in the plan,
        because its path is not knowable until the list has answered — which is
        exactly the order the brief requires: a known permitted run, only after
        the list supplies its run ID.
        """
        observed: list[tuple[Probe, TransportResult]] = []
        for probe in self.preflight_plan(binding):
            result = self.request(
                probe.method,
                probe.path,
                query=probe.query,
                binding=binding,
                timeout_class=probe.timeout_class,
            )
            observed.append((probe, result))
            if probe.name == "workspace-runs-list":
                run_id = _first_run_id(result)
                if run_id is not None:
                    detail = Probe(
                        name="run-detail",
                        method="GET",
                        path=f"/v1/workspaces/{binding.workspace_id}/runs/{run_id}",
                        query=None,
                        timeout_class="ordinary",
                        why="a run the list already returned, so no object is probed for",
                    )
                    observed.append(
                        (
                            detail,
                            self.request(
                                detail.method,
                                detail.path,
                                binding=binding,
                                timeout_class=detail.timeout_class,
                            ),
                        )
                    )
        return tuple(observed)

    # -- internals ---------------------------------------------------------

    def _stale_binding(
        self,
        profile: PlatformProfile,
        base_url: str,
        binding: PlatformBinding,
        credential: str,
    ) -> TransportResult | None:
        """Refuse a binding whose profile, endpoint or credential has moved.

        Recomputing the revision is cheap and it is the only check that catches
        a key rotated *under* a long-lived binding: the scope is unchanged, the
        caller believes nothing happened, and the cached body belongs to a
        credential that no longer exists.
        """
        current = revision_for(
            PlatformProfile(
                name=profile.name,
                base_url=base_url,
                api_key_env=profile.api_key_env,
                studio_id=binding.studio_id,
                key_source=profile.key_source,
            ),
            workspace_id=binding.workspace_id,
            studio_id=binding.studio_id,
            agent_uid=binding.agent_uid,
            credential=credential,
        )
        if current == binding.revision:
            return None
        self.invalidate(binding_id=binding.binding_id)
        return _error(
            "binding_required",
            "the platform profile, endpoint or credential changed since this binding "
            "was resolved; resolve it again before reading",
            retryable=False,
        )

    def _cache_key(
        self,
        binding: PlatformBinding,
        base_url: str,
        method: str,
        path: str,
        pairs: Sequence[tuple[str, str]],
    ) -> tuple[str, ...]:
        """Everything the answer depends on, and nothing that is a secret.

        The credential is present only through ``binding.revision``, which is
        derived from its fingerprint — so two keys never share an entry, and no
        cache key ever contains key text.
        """
        return (
            binding.profile_name,
            str(binding.revision),
            base_url,
            binding.workspace_id,
            binding.studio_id or "",
            binding.agent_uid or "",
            method,
            path,
            urlencode(list(pairs)),
        )

    def _cached(self, key: tuple[str, ...]) -> TransportResult | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if self._clock.monotonic() - entry.stored_monotonic > self._cache_ttl_s:
            del self._cache[key]
            return None
        return entry.result

    def _send(
        self,
        method: str,
        url: str,
        *,
        pairs: Sequence[tuple[str, str]],
        body: Mapping[str, object] | None,
        credential: str,
        budget: TimeoutBudget,
    ) -> TransportResult:
        headers = {
            "X-API-KEY": credential,
            "Accept": "application/json",
            "User-Agent": "aisquare-office",
        }
        started = self._clock.monotonic()
        response: httpx2.Response | None = None
        try:
            request = self._client.build_request(
                method,
                url,
                params=list(pairs),
                headers=headers,
                json=dict(body) if body is not None else None,
                timeout=httpx2.Timeout(
                    connect=budget.connect,
                    read=budget.read,
                    write=budget.read,
                    pool=budget.connect,
                ),
            )
            # follow_redirects belongs to the REQUEST, not to whichever client
            # was injected. A client configured to follow would re-send
            # X-API-KEY to whatever host the Location header named, which would
            # turn the seam that lets a test supply a client into a credential
            # leak. Stated here, it cannot be configured away.
            response = self._client.send(request, stream=True, follow_redirects=False)
            raw = self._read_bounded(response, started=started, budget=budget)
            status_code = response.status_code
            header_map = dict(response.headers)
        except httpx2.TimeoutException as exc:
            return _error(
                "timeout",
                f"the platform did not answer within {budget.total:g}s: {type(exc).__name__}",
                retryable=True,
            )
        except _BodyRejected as exc:
            return _error("upstream_invalid", str(exc), retryable=False)
        except httpx2.TooManyRedirects:
            return _error(
                "upstream_invalid",
                "the platform redirected the request; Office does not follow redirects "
                "with a credential attached",
                retryable=False,
            )
        except httpx2.RequestError as exc:
            return _error(
                "service_unavailable",
                sanitize_detail(
                    f"the platform could not be reached: {type(exc).__name__}",
                    credential=credential,
                ),
                retryable=True,
            )
        except Exception as exc:  # a remote read must have no uncaught failure mode
            return _error(
                "internal",
                sanitize_detail(
                    f"the platform read failed unexpectedly: {type(exc).__name__}",
                    credential=credential,
                ),
                retryable=False,
            )
        finally:
            if response is not None:
                response.close()

        content_type = str(header_map.get("content-type", "")).lower()
        if not any(marker in content_type for marker in JSON_CONTENT_TYPES):
            return _error(
                "upstream_invalid",
                f"the platform answered {status_code} with a non-JSON content type",
                retryable=False,
            )
        try:
            # Scrubbed BEFORE decoding, not after. An upstream is free to quote
            # the credential back — this gateway's own denial prose does — and a
            # decoded body carrying it would put the key in a return value, in
            # whatever P13 logs, and in any fixture captured from it. Replacing
            # in the raw text leaves the JSON valid, because the marker contains
            # no quote and no backslash.
            decoded: Any = json.loads(scrub_credential(raw.decode("utf-8"), credential))
        except (UnicodeDecodeError, ValueError):
            return _error(
                "upstream_invalid",
                f"the platform answered {status_code} with a body Office could not decode",
                retryable=False,
            )
        try:
            _bounded(decoded)
        except _BodyRejected as exc:
            return _error("upstream_invalid", str(exc), retryable=False)

        return TransportResult(
            response=TransportResponse(
                status_code=status_code,
                allowed_headers=allowed_headers(header_map),
                json_body=decoded,
                received_at=self._clock.now(),
                request_id=request_id_from(header_map),
            )
        )

    def _read_bounded(
        self,
        response: httpx2.Response,
        *,
        started: float,
        budget: TimeoutBudget,
    ) -> bytes:
        """Stream the body under the byte ceiling and the total budget.

        Streamed rather than buffered so the ceiling stops the read instead of
        describing it afterwards: a ``read()`` that has already allocated a
        gigabyte has already done the damage the ceiling exists to prevent.
        """
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes(READ_CHUNK_BYTES):
            total += len(chunk)
            if total > self._max_body_bytes:
                raise _BodyRejected(f"the platform response exceeds {self._max_body_bytes} bytes")
            chunks.append(chunk)
            if self._clock.monotonic() - started >= budget.total:
                raise httpx2.ReadTimeout("body still arriving at the total budget", request=None)
        return b"".join(chunks)


def _path_problem(path: str) -> str | None:
    """Why ``path`` may not be sent, or None.

    The browser never supplies one of these — P13/P14 build them from validated
    identifiers — so this is a guard against a future caller, not against a
    hostile input. It is still absolute: a path carrying its own query would
    bypass the normalisation the cache key depends on, and ``..`` would let a
    route escape the ``/v1`` prefix that every capability fact is written
    against.
    """
    if not path.startswith("/"):
        return "a platform path must be absolute"
    if "?" in path or "#" in path:
        return "a platform path must not carry its own query or fragment"
    if ".." in path or "//" in path:
        return "a platform path must not contain traversal or empty segments"
    if not _PATH_OK.match(path):
        return "a platform path must be a bounded /v1 route"
    return None


def _scope_problem(path: str, binding: PlatformBinding) -> str | None:
    """Why ``path`` does not belong to ``binding``, or None.

    Checked before transport rather than left to the gateway's 403, so a
    mismatch never sends the credential anywhere at all. The gateway would
    refuse it too — this is not the security boundary — but a request that was
    never made cannot be logged upstream against the wrong workspace.
    """
    workspace = _WORKSPACE_PATH.match(path)
    if workspace is not None and workspace.group(1) != binding.workspace_id:
        return "the requested workspace is not the one this binding resolved"
    studio = _STUDIO_PATH.match(path)
    if studio is not None:
        if binding.studio_id is None:
            return (
                "this binding has no studio, so a studio-scoped route cannot be read; "
                "most Praxis reads are unreachable without one"
            )
        if studio.group(1) != binding.studio_id:
            return "the requested studio is not the one this binding resolved"
    return None


def _first_run_id(result: TransportResult) -> str | None:
    """The first run id in a runs-list response, if it looks like one."""
    response = result.response
    if response is None or not 200 <= response.status_code < 300:
        return None
    body = response.json_body
    if not isinstance(body, dict):
        return None
    runs = body.get("runs")
    if not isinstance(runs, list) or not runs:
        return None
    first = runs[0]
    if not isinstance(first, dict):
        return None
    run_id = first.get("run_id")
    if isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", run_id):
        return run_id
    return None


__all__ = [
    "CONTEXT_TOTAL_S",
    "MAX_BODY_BYTES",
    "NOT_FOUND_OR_MASKED",
    "WRITE_TOTAL_S",
    "HttpPlatformTransport",
    "Probe",
    "TimeoutBudget",
    "budget_for",
    "classify_status",
    "default_client",
    "normalized_query",
]
