"""Binding resolution: what it refuses, and what makes a revision change.

The refusals matter more than the successes here. A binding that resolves from
a guess — the repository basename, a display name, whatever tab the browser had
open — reads a real workspace under a real credential, and the operator finds
out from the data. So every test that asserts a refusal also asserts that the
message names the setting to fix, and several assert that no network-shaped
work was attempted at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from aisquare.office.config import OfficeConfig
from aisquare.office.platform_config import (
    AGENT_UID_ENV_VAR,
    STUDIO_ENV_VAR,
    WORKSPACE_ENV_VAR,
    BindingResolution,
    PlatformConfigError,
    PlatformProfile,
    credential_fingerprint,
    normalize_base_url,
    resolve_binding,
    revision_for,
    route_capability,
)

HOME = Path("/tmp/office-home")
KEY = "wk_live_synthetic0123456789abcdefghij"
OTHER_KEY = "wk_live_synthetic9876543210zyxwvutsr"
AGENT = "00000000-0000-4000-8000-000000000000"

PROFILE = PlatformProfile(
    name="stg",
    base_url="https://gateway.test/",
    api_key_env="EXPLAINABILITY_API_KEY",
    studio_id=None,
    key_source="env",
)


class FakeSource:
    """A credential source that records what it was asked for."""

    def __init__(self, profile: PlatformProfile = PROFILE, credential: str | None = KEY) -> None:
        self._profile = profile
        self._credential = credential
        self.profile_calls: list[str | None] = []
        self.credential_calls: list[str] = []

    def profile(self, name: str | None) -> PlatformProfile:
        self.profile_calls.append(name)
        return self._profile

    def credential(self, profile: PlatformProfile) -> str | None:
        self.credential_calls.append(profile.name)
        return self._credential


def env_with(**overrides: str) -> dict[str, str]:
    values = {WORKSPACE_ENV_VAR: "1042"}
    values.update(overrides)
    return values


def resolve(
    source: FakeSource | None = None,
    env: Mapping[str, str] | None = None,
    config: OfficeConfig | None = None,
) -> BindingResolution:
    return resolve_binding(
        project_id="prj_office",
        config=config if config is not None else OfficeConfig(home=HOME),
        source=source if source is not None else FakeSource(),
        env=env if env is not None else env_with(),
    )


def test_a_configured_profile_and_workspace_resolve_to_a_binding() -> None:
    resolution = resolve()

    assert resolution.error is None
    binding = resolution.binding
    assert binding is not None
    assert binding.workspace_id == "1042"
    assert binding.profile_name == "stg"
    assert binding.project_id == "prj_office"
    assert binding.binding_id.startswith("pb_")
    assert binding.revision > 0
    assert resolution.profile is not None
    assert resolution.profile.base_url == "https://gateway.test"


def test_the_credential_is_in_no_field_of_the_resolved_binding() -> None:
    """The key is a request-time lookup, never a stored attribute."""
    resolution = resolve()

    binding = resolution.binding
    profile = resolution.profile
    assert binding is not None
    assert profile is not None
    assert KEY not in repr(binding)
    assert KEY not in repr(profile)
    assert KEY not in profile.base_url + profile.api_key_env + profile.name + profile.key_source
    assert not any(field.name == "credential" for field in binding.__dataclass_fields__.values())


def test_a_missing_workspace_is_unconfigured_and_reaches_no_credential() -> None:
    source = FakeSource()

    resolution = resolve(source=source, env={})

    assert resolution.binding is None
    assert resolution.error is not None
    assert resolution.error.code == "service_unconfigured"
    assert WORKSPACE_ENV_VAR in resolution.error.detail
    assert resolution.error.retryable is False
    assert source.credential_calls == []


def test_a_non_numeric_workspace_is_refused_before_anything_is_sent() -> None:
    """``gateway/auth.py`` answers 400 for a non-numeric workspace; catching it
    here means the key is never offered to a request that cannot succeed."""
    source = FakeSource()

    resolution = resolve(source=source, env=env_with(**{WORKSPACE_ENV_VAR: "my-workspace"}))

    assert resolution.binding is None
    assert resolution.error is not None
    assert "numeric" in resolution.error.detail
    assert source.credential_calls == []


def test_an_absent_credential_is_unconfigured_and_names_the_variable() -> None:
    resolution = resolve(source=FakeSource(credential=None))

    assert resolution.binding is None
    assert resolution.error is not None
    assert resolution.error.code == "service_unconfigured"
    assert "EXPLAINABILITY_API_KEY" in resolution.error.detail


def test_a_plain_http_gateway_off_loopback_is_refused() -> None:
    source = FakeSource(
        profile=PlatformProfile(
            name="stg",
            base_url="http://gateway.test",
            api_key_env="EXPLAINABILITY_API_KEY",
            key_source="env",
        )
    )

    resolution = resolve(source=source)

    assert resolution.binding is None
    assert resolution.error is not None
    assert "https" in resolution.error.detail


def test_a_gateway_url_carrying_userinfo_is_refused() -> None:
    """A key pasted into a URL is the classic route into a log line."""
    source = FakeSource(
        profile=PlatformProfile(
            name="stg",
            base_url="https://user:secret@gateway.test",
            api_key_env="EXPLAINABILITY_API_KEY",
            key_source="env",
        )
    )

    resolution = resolve(source=source)

    assert resolution.binding is None
    assert resolution.error is not None
    assert "userinfo" in resolution.error.detail
    assert "secret" not in resolution.error.detail


def test_an_agent_that_is_not_a_uuid_is_refused_with_the_namespaces_named() -> None:
    resolution = resolve(env=env_with(**{AGENT_UID_ENV_VAR: "coder-1"}))

    assert resolution.binding is None
    assert resolution.error is not None
    assert AGENT_UID_ENV_VAR in resolution.error.detail
    assert "UUID" in resolution.error.detail


def test_a_studio_and_agent_are_carried_onto_the_binding_when_configured() -> None:
    resolution = resolve(env=env_with(**{STUDIO_ENV_VAR: "482", AGENT_UID_ENV_VAR: AGENT}))

    binding = resolution.binding
    assert binding is not None
    assert binding.studio_id == "482"
    assert binding.agent_uid == AGENT


def test_a_rotated_credential_changes_the_revision_but_not_the_binding_id() -> None:
    """The hard cache boundary. Same scope, different key, different revision —
    so nothing fetched under the old key is reachable under the new one."""
    first = resolve(source=FakeSource(credential=KEY)).binding
    second = resolve(source=FakeSource(credential=OTHER_KEY)).binding

    assert first is not None
    assert second is not None
    assert first.binding_id == second.binding_id
    assert first.revision != second.revision


def test_a_repointed_gateway_changes_the_revision() -> None:
    moved = PlatformProfile(
        name="stg",
        base_url="https://other-gateway.test",
        api_key_env="EXPLAINABILITY_API_KEY",
        key_source="env",
    )

    first = resolve().binding
    second = resolve(source=FakeSource(profile=moved)).binding

    assert first is not None
    assert second is not None
    assert first.revision != second.revision


def test_a_different_workspace_changes_the_binding_id() -> None:
    first = resolve().binding
    second = resolve(env=env_with(**{WORKSPACE_ENV_VAR: "2000"})).binding

    assert first is not None
    assert second is not None
    assert first.binding_id != second.binding_id


def test_resolution_is_deterministic_for_identical_inputs() -> None:
    """Derived rather than counted, so two processes agree without coordinating."""
    first = resolve().binding
    second = resolve().binding

    assert first is not None
    assert second is not None
    assert (first.binding_id, first.revision) == (second.binding_id, second.revision)


def test_the_fingerprint_is_not_the_key_and_is_stable() -> None:
    digest = credential_fingerprint(KEY)

    assert digest != KEY
    assert KEY not in digest
    assert digest == credential_fingerprint(KEY)
    assert digest != credential_fingerprint(OTHER_KEY)


def test_the_revision_helper_refuses_to_ignore_any_part_of_the_scope() -> None:
    reference = revision_for(
        PROFILE, workspace_id="1042", studio_id="482", agent_uid=AGENT, credential=KEY
    )
    variants = (
        revision_for(
            PROFILE, workspace_id="1043", studio_id="482", agent_uid=AGENT, credential=KEY
        ),
        revision_for(
            PROFILE, workspace_id="1042", studio_id="483", agent_uid=AGENT, credential=KEY
        ),
        revision_for(PROFILE, workspace_id="1042", studio_id="482", agent_uid=None, credential=KEY),
        revision_for(
            PROFILE, workspace_id="1042", studio_id="482", agent_uid=AGENT, credential=OTHER_KEY
        ),
    )

    assert reference not in variants
    assert len(set(variants)) == 4


def test_scope_fields_are_separated_so_two_scopes_cannot_collide() -> None:
    """Joining the parts without a separator would make ("48", "2") and
    ("4", "82") the same binding."""
    left = revision_for(PROFILE, workspace_id="48", studio_id="2", agent_uid=None, credential=KEY)
    right = revision_for(PROFILE, workspace_id="4", studio_id="82", agent_uid=None, credential=KEY)

    assert left != right


def test_normalize_base_url_strips_a_trailing_slash_and_keeps_the_host() -> None:
    assert normalize_base_url("https://gateway.test/") == "https://gateway.test"
    assert normalize_base_url("https://gateway.test/api/") == "https://gateway.test/api"
    assert normalize_base_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_normalize_base_url_refuses_a_query_or_a_foreign_scheme() -> None:
    with pytest.raises(PlatformConfigError):
        normalize_base_url("https://gateway.test/?key=abc")
    with pytest.raises(PlatformConfigError):
        normalize_base_url("ftp://gateway.test")
    with pytest.raises(PlatformConfigError):
        normalize_base_url("   ")


def test_the_studio_praxis_routes_are_recorded_as_unreachable_not_retried() -> None:
    """A workspace key gets 403 from every studio-scoped Praxis guard. That is a
    capability fact about the deployment, so it is data here rather than a
    failure a caller would sensibly retry."""
    for path in (
        "/v1/studios/482/praxis/insights",
        "/v1/studios/482/praxis/signals",
        "/v1/studios/482/praxis/runs/abc/injection",
        "/v1/studios/482/praxis/insights/abc/chain",
    ):
        capability = route_capability(path)
        assert capability is not None, path
        assert capability.reachable is False, path
        assert capability.reason


def test_the_context_route_is_recorded_as_the_one_reachable_studio_route() -> None:
    capability = route_capability("/v1/studios/482/praxis/context")

    assert capability is not None
    assert capability.reachable is True
    assert "require_policy_check_access" in capability.reason


def test_the_chain_route_is_matched_before_the_bare_insights_route() -> None:
    """Pattern order is load-bearing: a bare ``insights`` regex that matched
    first would describe the chain route with the wrong reason."""
    chain = route_capability("/v1/studios/482/praxis/insights/abc/chain")

    assert chain is not None
    assert "provenance" in chain.reason


def test_an_unlisted_route_is_unknown_rather_than_assumed_dead() -> None:
    """Declaring a route unreachable on no evidence is the same error as
    declaring one reachable on no evidence."""
    assert route_capability("/v1/workspaces/1042/runs") is None
    assert route_capability("/v1/workspaces/1042/praxis/agents/x/insights") is None


def test_a_resolution_cannot_carry_both_a_binding_and_an_error() -> None:
    with pytest.raises(ValueError):
        BindingResolution()
