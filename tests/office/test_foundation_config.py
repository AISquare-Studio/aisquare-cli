"""``OfficeConfig``: the documented defaults, the bounds, and what it refuses.

The defaults are asserted by value rather than by "is not None" because they
are the numbers SHARED.md fixes — 500 ms observation, 15 s heartbeat, 2 s
acknowledgement, 1 s prompt-close observation, 3 s ordinary remote read. A
later packet that quietly retunes one of them changes behaviour nobody asked
for, so the numbers are pinned here where a change has to be deliberate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.office.config import (
    ALLOWED_HOSTS,
    DEFAULT_PORT,
    ENV_FIELDS,
    MAX_CAPTURE_FPS,
    TEAM_OS_DEFAULT_PORT,
    OfficeConfig,
    OfficeConfigError,
)

HOME = Path("/tmp/office-home")


def test_defaults_match_the_documented_starting_points() -> None:
    config = OfficeConfig(home=HOME)

    assert config.host == "127.0.0.1"
    assert config.port == DEFAULT_PORT == 7373
    assert config.poll_interval_ms == 500
    assert config.heartbeat_s == 15.0
    assert config.action_ack_s == 2.0
    assert config.prompt_close_s == 1.0
    assert config.remote_timeout_s == 3.0
    assert config.capture_fps == MAX_CAPTURE_FPS == 10
    assert config.web_root is None
    assert config.team_os_enabled is False
    assert config.team_os_port == TEAM_OS_DEFAULT_PORT == 4317
    assert config.platform_profile is None


def test_defaults_place_the_sidecar_under_the_resolved_home() -> None:
    """Never the CLI's ``context.db``, and never inside a repository."""
    config = OfficeConfig(home=HOME)

    assert config.sidecar_path == HOME / "office" / "observations.sqlite3"
    assert config.sidecar_path.is_relative_to(HOME)
    assert config.base_url == "http://127.0.0.1:7373"


def test_defaults_are_frozen_so_a_component_cannot_retune_its_own_budget() -> None:
    config = OfficeConfig(home=HOME)

    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError here
        config.port = 9999  # type: ignore[misc]


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::1", ""])
def test_reject_a_host_that_is_not_loopback(host: str) -> None:
    """Office is a local window on a local fleet; it never binds publicly."""
    with pytest.raises(OfficeConfigError) as caught:
        OfficeConfig.from_mapping({"host": host}, home=HOME)

    assert "loopback" in str(caught.value)


@pytest.mark.parametrize("host", sorted(ALLOWED_HOSTS))
def test_accept_every_allowed_loopback_host(host: str) -> None:
    assert OfficeConfig.from_mapping({"host": host}, home=HOME).host == host


@pytest.mark.parametrize("port", [0, -1, 65536, 200_000])
def test_reject_a_port_outside_the_usable_range(port: int) -> None:
    with pytest.raises(OfficeConfigError):
        OfficeConfig.from_mapping({"port": port}, home=HOME)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("poll_interval_ms", 0),
        ("poll_interval_ms", 10),
        ("poll_interval_ms", 600_000),
        ("heartbeat_s", 0),
        ("heartbeat_s", -1),
        ("action_ack_s", 0),
        ("prompt_close_s", 0),
        ("remote_timeout_s", 0),
    ],
)
def test_reject_an_interval_that_is_not_positive_and_finite(field: str, value: float) -> None:
    with pytest.raises(OfficeConfigError):
        OfficeConfig.from_mapping({field: value}, home=HOME)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capture_fps", 0),
        ("capture_fps", MAX_CAPTURE_FPS + 1),
        ("stream_queue_max", 0),
        ("stream_queue_max", 1_000_000),
        ("max_subscribers", 0),
        ("max_subscribers", 100_000),
    ],
)
def test_reject_queue_and_capture_limits_without_a_hard_ceiling(field: str, value: int) -> None:
    """An unbounded queue is the server's memory; an unbounded capture rate is
    the fleet's CPU. Both have ceilings a configuration cannot lift."""
    with pytest.raises(OfficeConfigError):
        OfficeConfig.from_mapping({field: value}, home=HOME)


def test_reject_an_unknown_configuration_key() -> None:
    """A typo is not forward compatibility. ``core.config`` keeps unknown keys on
    purpose so an older build cannot erase a newer one's file; a serving
    process's own settings have no such cross-build story to protect."""
    with pytest.raises(OfficeConfigError) as caught:
        OfficeConfig.from_mapping({"prot": 8080}, home=HOME)

    assert "prot" in str(caught.value)


def test_reject_a_relative_web_root() -> None:
    """A relative web root resolves against whatever directory the server was
    started in, which is not a property anyone should have to know."""
    with pytest.raises(OfficeConfigError):
        OfficeConfig.from_mapping({"web_root": Path("web/dist")}, home=HOME)


def test_the_web_root_is_a_path_object_not_a_string() -> None:
    config = OfficeConfig.from_mapping({"web_root": Path("/srv/office")}, home=HOME)

    assert isinstance(config.web_root, Path)
    assert config.web_root == Path("/srv/office")


def test_from_environment_reads_only_the_allow_list() -> None:
    env = {
        "AISQUARE_OFFICE_PORT": "7500",
        "AISQUARE_OFFICE_HEARTBEAT_S": "20",
        "TEAMOS_PORT": "4400",
        "TEAMOS_ENABLED": "true",
        # None of the following is ours to read.
        "AISQUARE_OFFICE_SECRET": "hunter2",
        "OFFICE_PORT": "1",
        "PATH": "/usr/bin",
    }

    config = OfficeConfig.from_environment(env, home=HOME)

    assert config.port == 7500
    assert config.heartbeat_s == 20.0
    assert config.team_os_port == 4400
    assert config.team_os_enabled is True
    assert config.host == "127.0.0.1"


def test_from_environment_is_pure_and_touches_no_real_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution is injected, so a test never depends on the process env."""
    monkeypatch.setenv("AISQUARE_OFFICE_PORT", "9999")
    calls: list[int] = []

    def resolver() -> Path:
        calls.append(1)
        return HOME

    config = OfficeConfig.from_environment({}, home_resolver=resolver)

    assert config.port == DEFAULT_PORT, "the ambient environment must not leak in"
    assert config.home == HOME
    assert calls == [1]


def test_from_environment_needs_a_home_it_was_given() -> None:
    with pytest.raises(OfficeConfigError):
        OfficeConfig.from_environment({})


def test_team_os_keeps_its_own_variable_names() -> None:
    """The peer is a separate local process. Renaming its settings into
    ``AISQUARE_*`` would claim it is ours to configure."""
    assert "TEAMOS_PORT" in ENV_FIELDS
    assert "TEAMOS_HOST" in ENV_FIELDS
    assert not any(name.startswith("AISQUARE_OFFICE_TEAMOS") for name in ENV_FIELDS)


def test_secret_no_field_can_hold_a_credential() -> None:
    """A binding names a profile; a token is named, never carried.

    This is asserted on the field names rather than on one instance, so a later
    packet adding ``team_os_token`` fails here instead of at the first log line
    that prints a configuration.
    """
    forbidden = ("token", "key", "secret", "password", "credential")
    allowed_named_sources = {"team_os_token_env"}

    for name in OfficeConfig.model_fields:
        if name in allowed_named_sources:
            assert name.endswith("_env"), "a credential field may only name its source variable"
            continue
        assert not any(word in name for word in forbidden), f"{name} looks like a credential"


def test_secret_a_bad_value_is_never_echoed_back() -> None:
    """The wrong value in a variable is sometimes a token, and an error message
    is where a token would outlive the mistake."""
    secret = "sk-live-do-not-log-this"

    with pytest.raises(OfficeConfigError) as caught:
        OfficeConfig.from_environment({"AISQUARE_OFFICE_PORT": secret}, home=HOME)

    message = str(caught.value)
    assert secret not in message
    assert "AISQUARE_OFFICE_PORT" in message


def test_secret_a_validation_failure_names_the_variable_not_the_value() -> None:
    with pytest.raises(OfficeConfigError) as caught:
        OfficeConfig.from_environment({"AISQUARE_OFFICE_PORT": "0"}, home=HOME)

    message = str(caught.value)
    assert "port" in message
    assert "AISQUARE_OFFICE_PORT" in message
