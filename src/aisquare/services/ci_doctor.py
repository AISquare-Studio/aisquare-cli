"""The CI test bed's ``doctor`` rows, parked: dormant, and wired to nothing.

``aisquare doctor`` no longer shows the test bed (``ci test bed``, ``ci endpoint``,
``ci descriptor``, ``ci descriptor cache``, ``ci delivery override``): the experiment is
unmaintained and off for everyone, and its rows were noise in every report. They live on
here, with their tests, for the day it is built out. To bring them back, add
``*ci_doctor.checks()`` to the list ``diagnostics.doctor`` returns, where the row for the
experiment sat beside the watcher's; nothing else needs to change.

Nothing imports this module. Every ``ci_*`` module, the ``AISQUARE_CI`` gate, the hooks
and the ``ci_recall`` tool are as they were.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import ci_client, ci_descriptor, ci_override
from aisquare.services.ci_contract import DeliveryDescriptor


def _ok(name: str, detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=CheckStatus.ok, detail=detail)


def _warn(name: str, detail: str, fix: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=CheckStatus.warn, detail=detail, fix=fix)


def checks() -> list[DoctorCheck]:
    """The CI test bed's state, one line per question a developer would ask.

    Off is reported as ``ok`` rather than as a warning: off is the intended
    state for everyone who has not been asked to run the experiment, and a
    permanent warning trains people to ignore the one line that matters.

    Switched on, the questions are asked in the order the hooks would hit
    them: is the URL usable, is there a token, is there a run; can the server
    be reached at all (``GET /ready``, public); and does the descriptor come
    back — which is the real question, because it is where a bad token, an
    unknown run, an expired run or a contract skew each show up with their own
    answer. The descriptor is fetched without caching it: a diagnostic must not
    create state. Every probe is bounded by the transport's own deadline.
    """
    name = "ci test bed"
    if not ci_client.enabled():
        return [_ok(name, "off — no requests, no added latency (AISQUARE_CI=1 enables)")]
    raw = ci_client.raw_endpoint()
    if not raw:
        return [
            _warn(
                name,
                "enabled but no endpoint configured — every prompt records not_configured",
                "Point it at the server: export AISQUARE_CI_URL=https://…",
            )
        ]
    base = ci_client.endpoint()
    if not base:
        return [
            _warn(
                name,
                f"enabled, but {_display_url(raw)} is not a usable URL — it needs an "
                "http(s):// scheme; every prompt records not_configured",
                "Give it a scheme: export AISQUARE_CI_URL=https://…",
            )
        ]
    shown = _display_url(base)
    key = ci_client.api_key()
    raw_run = ci_client.raw_run_id()
    run = ci_client.run_id()
    checks: list[DoctorCheck] = []
    if not key:
        problem = ci_client.api_key_problem()
        checks.append(
            _warn(
                name,
                f"enabled for {shown}, but {problem} — not usable; the server will reject "
                "every request"
                if problem
                else f"enabled for {shown}, but no bearer token — the server will reject every "
                "request",
                "Re-export the token on one line: export AISQUARE_CI_KEY=…"
                if problem
                else "Set the experiment token: export AISQUARE_CI_KEY=…",
            )
        )
    elif not raw_run:
        checks.append(
            _warn(
                name,
                f"enabled for {shown}, but no run id — every prompt records no_run",
                "Export the run the controller published: export AISQUARE_CI_RUN=run_…",
            )
        )
    elif not run:
        checks.append(
            _warn(
                name,
                f"enabled for {shown}, but {raw_run!r} is not a run id (run_…) — "
                "every prompt records no_run",
                "Export the run the controller published: export AISQUARE_CI_RUN=run_…",
            )
        )
    else:
        checks.append(_ok(name, f"enabled for {shown}, run {run}"))
    checks.append(_check_ci_endpoint(base, shown))
    descriptor: DeliveryDescriptor | None = None
    if key and run:
        descriptor_check, descriptor = _check_ci_descriptor(base, key, run)
        checks.append(descriptor_check)
        stale = _check_ci_cache(run, descriptor)
        if stale is not None:
            checks.append(stale)
    # Outside the gate above: a set override is named whenever it is set, even
    # when no descriptor could be fetched for it to apply to.
    override = _check_ci_override(descriptor)
    if override is not None:
        checks.append(override)
    return checks


_CI_PROBE_MS = 3_000
"""Doctor must stay fast; an unreachable endpoint is the common case here, and
the transport's wall-clock deadline is what bounds each probe."""


def _check_ci_endpoint(base: str, shown: str) -> DoctorCheck:
    """``GET /ready`` — public, cheap, and proof of a live server rather than a
    listener. It follows the same proxies the hook does, because it is the
    same transport."""
    result = ci_client.exchange(
        f"{base}/ready", method="GET", deadline_ms=_CI_PROBE_MS, max_body=4096
    )
    if result.reason is None and result.status == 200:
        return _ok("ci endpoint", f"{shown}/ready answered 200 in {result.elapsed_ms} ms")
    why = result.detail if result.reason is not None else f"http {result.status}"
    return _warn(
        "ci endpoint",
        f"{shown}/ready did not answer ({why}) — prompts still work; whether the hooks can "
        "deliver is what the descriptor line says, not this one",
        "Check the server is up, or turn the test bed off: export AISQUARE_CI=0",
    )


def _check_ci_descriptor(
    base: str, key: str, run: str
) -> tuple[DoctorCheck, DeliveryDescriptor | None]:
    """The question that matters: will the hooks be told how to deliver?

    Returns the descriptor too, so the override line can be judged against it."""
    result = ci_descriptor.fetch(run, base=base, key=key, cache=False, deadline_ms=_CI_PROBE_MS)
    descriptor = result.descriptor
    if descriptor is None:
        detail = result.detail
        # The fix follows the status the server sent, never a word in its
        # message: an error.v1 sentence is quoted into the detail now, and a
        # word inside it ("expired", the server's own contract_version_mismatch
        # code, a TLS certificate that "has expired") must not pick a fix. The
        # two phrase-based branches match the client's OWN parse details at
        # their start, and nothing else.
        if result.status in (401, 403):
            fix = "Check AISQUARE_CI_KEY is the experiment token for this server"
        elif result.status == 404:
            fix = "Check AISQUARE_CI_RUN names a run this server has published"
        elif detail.startswith("descriptor speaks contract_version"):
            fix = "Upgrade aisquare-cli, or ask for a run published for this contract"
        elif detail.startswith("descriptor expired at"):
            fix = "Ask the experiment controller for a fresh run"
        else:
            fix = "Check the server, or turn the test bed off: export AISQUARE_CI=0"
        return (
            _warn(
                "ci descriptor",
                f"run {run}: {detail} — every turn records descriptor_unavailable",
                fix,
            ),
            None,
        )
    # The server's ruling is always shown as the server's. But "the hooks will
    # not call" is a promise the line beneath would break while the override is
    # active, so that clause defers to it instead.
    note = ci_override.DIRECT_API_NOTE
    if ci_override.apply(descriptor).active:
        note = "direct_api only — overridden, see ci delivery override below"
    return (
        _ok(
            "ci descriptor",
            f"run {run}: {ci_override.describe(descriptor, direct_api_note=note)}; "
            f"ceiling {descriptor.client_safety_ms} ms; expires {descriptor.expires_at}",
        ),
        descriptor,
    )


def _check_ci_cache(run: str, fresh: DeliveryDescriptor | None) -> DoctorCheck | None:
    """The hooks read the cached descriptor; this probe fetched a fresh one.

    When the two disagree on delivery, every line above describes a descriptor
    the hooks are not using until the cache expires — so say so, once.
    """
    cached = ci_descriptor.cached(run)
    if cached is None or fresh is None or cached.delivery == fresh.delivery:
        return None
    return _warn(
        "ci descriptor cache",
        f"the hooks use a cached descriptor until {cached.expires_at} that lists "
        f"{ci_override.describe(cached)}; the server now says "
        f"{ci_override.describe(fresh)}",
        "Wait for the cache to expire, or start a new session after it does",
    )


def _check_ci_override(descriptor: DeliveryDescriptor | None) -> DoctorCheck | None:
    """The staging override, on its own line whenever the variable is set.

    Silent when it is unset — the common case, and the one every row should
    come from. Set, it is always a warning: active means every row this
    machine writes says ``delivery_source override`` and measures nothing;
    ignored means someone exported it and it is doing nothing, which is worth
    a line before they wonder why.
    """
    if not ci_override.requested():
        return None
    if descriptor is None:
        return _warn(
            "ci delivery override",
            f"{ci_override.ENV_VAR} is set, but there is no fetched descriptor to apply it to — "
            "nothing is delivered",
            "Fix the descriptor line first, or unset it",
        )
    ruling = ci_override.apply(descriptor)
    if ruling.active:
        return _warn(
            "ci delivery override",
            f"{ruling.detail}; every row records delivery_source override and measures nothing",
            f"Unset {ci_override.ENV_VAR} once the server publishes real delivery modes",
        )
    return _warn("ci delivery override", ruling.detail, f"Fix or unset {ci_override.ENV_VAR}")


def _display_url(url: str) -> str:
    """A URL as ``doctor`` may print it: scheme, host and path, never ``user:secret@``.

    ``doctor`` output is the most pasteable artefact there is, and a credential
    in the URL would leak by being ordinary. The path stays, so a base URL with
    one prints the route that was actually probed.
    """
    try:
        parts = urlsplit(url if "://" in url else f"//{url}", scheme="")
    except ValueError:
        return re.sub(r"[^@/]*@", "", url)
    host = parts.netloc.rsplit("@", 1)[-1]
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{host}{path}" if parts.scheme else f"{host}{path}"
