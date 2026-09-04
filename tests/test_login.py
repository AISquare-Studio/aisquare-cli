"""``aisquare login`` and friends against a loopback identity provider.

The contract is ``docs/plans/aisquare-login.md`` section 4. Every test here
drives the real command through the real ``urllib`` path to a stub server;
only ``time.sleep`` is patched, so the poll loop runs in milliseconds.
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import auth as auth_cli
from aisquare.cli.app import app
from aisquare.core import credentials, paths
from aisquare.core.redaction import MARKER, redact
from aisquare.models import RedactionLevel
from aisquare.services import iam
from tests.idp_stub import DEVICE_CODE, USER_CODE, IdentityProviderStub

DEAD_PORT_URL = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_cli, "_sleep", lambda _seconds: None)


@pytest.fixture
def idp() -> Iterator[IdentityProviderStub]:
    stub = IdentityProviderStub()
    yield stub
    stub.close()


def _login(runner: CliRunner, url: str, *extra: str, json_mode: bool = False) -> Any:
    argv = ["login", "--no-browser", "--api-url", url, *extra]
    if json_mode:
        argv = ["--json", *argv]
    return runner.invoke(app, argv)


def _stored() -> dict[str, str]:
    return credentials.load_all()


# --------------------------------------------------------------------------- the happy path


def test_login_walks_the_device_flow_and_stores_the_session(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    result = _login(runner, idp.url)

    assert result.exit_code == 0, result.output
    assert USER_CODE in result.output
    assert f"{idp.url}/cli?code={USER_CODE}" in result.output
    assert "Signed in as anmol@example.com" in result.output
    assert "This session expires" in result.output

    stored = _stored()
    assert stored["iam_token"] == idp.issued[0]
    assert stored["iam_api_url"] == idp.url
    assert stored["iam_email"] == "anmol@example.com"
    assert stored["iam_sub"] == "uid-123"
    assert stored["iam_client_id"] == "aisquare-cli"
    assert stored["iam_scope"] == "openid profile email aisquare"
    assert stored["iam_token_expires_at"]

    # discovery, start, two polls, userinfo: the standard shape, nothing custom.
    assert idp.paths() == [
        "/o/.well-known/openid-configuration",
        "/o/device-authorization/",
        "/o/token/",
        "/o/token/",
        "/o/userinfo/",
    ]
    start = idp.requests[1]
    assert start["form"] == {"client_id": "aisquare-cli", "scope": "openid profile email aisquare"}
    assert start["headers"]["user-agent"].startswith("aisquare-cli/")
    assert "(" in start["headers"]["user-agent"]
    assert start["headers"]["x-device-name"]
    assert start["headers"]["content-type"] == "application/x-www-form-urlencoded"
    poll = idp.requests[2]
    assert poll["form"]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert poll["form"]["device_code"] == DEVICE_CODE
    assert idp.requests[4]["headers"]["authorization"] == f"Bearer {idp.issued[0]}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_credentials_file_is_private(runner: CliRunner, idp: IdentityProviderStub) -> None:
    assert _login(runner, idp.url).exit_code == 0
    mode = stat.S_IMODE(paths.credentials_path().stat().st_mode)
    assert mode == 0o600


def test_json_login_puts_one_object_on_stdout_and_the_link_on_stderr(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    result = _login(runner, idp.url, json_mode=True)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["user"] == {
        "sub": "uid-123",
        "email": "anmol@example.com",
        "name": "Anmol Majithia",
    }
    assert payload["api_url"] == idp.url
    assert payload["source"] == "file"
    assert payload["expires_at"]
    event_lines = [line for line in result.stderr.splitlines() if line.startswith("{")]
    event = json.loads(event_lines[0])
    assert event["event"] == "verification"
    assert event["user_code"] == USER_CODE
    assert event["verification_uri_complete"].endswith(f"/cli?code={USER_CODE}")
    assert "ask the user to visit the URL above" in result.stderr


def test_discovery_is_cached_for_the_next_command(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    assert _login(runner, idp.url).exit_code == 0
    discovery_calls = idp.paths().count("/o/.well-known/openid-configuration")
    result = runner.invoke(app, ["auth", "status", "--live"])
    assert result.exit_code == 0, result.output
    assert idp.paths().count("/o/.well-known/openid-configuration") == discovery_calls


# --------------------------------------------------------------------------- the poll loop


def test_slow_down_and_rate_limits_keep_polling(runner: CliRunner) -> None:
    stub = IdentityProviderStub(["slow_down", "rate_limited", "pending", "token"])
    try:
        result = _login(runner, stub.url)
        assert result.exit_code == 0, result.output
        assert stub.polls() == 4
    finally:
        stub.close()


def test_a_denied_request_stores_nothing(runner: CliRunner) -> None:
    stub = IdentityProviderStub(["pending", "denied"])
    try:
        result = _login(runner, stub.url)
        assert result.exit_code == 1
        assert "denied in the browser" in result.output
        assert "iam_token" not in _stored()
    finally:
        stub.close()


def test_an_expired_code_says_so(runner: CliRunner) -> None:
    stub = IdentityProviderStub(["expired"])
    try:
        result = _login(runner, stub.url, json_mode=True)
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "expired"
    finally:
        stub.close()


def test_a_paused_provider_is_reported(runner: CliRunner) -> None:
    stub = IdentityProviderStub(["paused"])
    try:
        result = _login(runner, stub.url)
        assert result.exit_code == 1
        assert "temporarily paused" in result.output
    finally:
        stub.close()


def test_ctrl_c_stores_nothing_and_exits_130(
    runner: CliRunner, idp: IdentityProviderStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(auth_cli, "_sleep", interrupt)
    result = _login(runner, idp.url)
    assert result.exit_code == 130
    assert "Sign-in cancelled. Nothing was stored." in result.output
    assert "iam_token" not in _stored()


def test_the_local_deadline_ends_an_endless_pending(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = IdentityProviderStub(["pending"], expires_in=1)
    clock = iter([0.0, 0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr(auth_cli, "_monotonic", lambda: next(clock, 1000.0))
    try:
        result = _login(runner, stub.url)
        assert result.exit_code == 1
        assert "expired before it was approved" in result.output
    finally:
        stub.close()


# --------------------------------------------------------------------------- refusals


def test_a_server_without_discovery_is_unsupported(runner: CliRunner) -> None:
    stub = IdentityProviderStub(discovery=False)
    try:
        result = _login(runner, stub.url, json_mode=True)
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "unsupported_server"
    finally:
        stub.close()


def test_an_unreachable_server_is_reported(runner: CliRunner) -> None:
    result = _login(runner, DEAD_PORT_URL, json_mode=True)
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "unreachable"
    assert "127.0.0.1:9" in payload["detail"] or "Connection refused" in payload["detail"]


def test_plain_http_to_a_remote_host_is_refused(runner: CliRunner) -> None:
    result = _login(runner, "http://api.example.com", json_mode=True)
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "api_url_not_https"


def test_a_rate_limited_start_is_reported(runner: CliRunner) -> None:
    stub = IdentityProviderStub(start_status=429)
    try:
        result = _login(runner, stub.url, json_mode=True)
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "rate_limited"
    finally:
        stub.close()


def test_an_environment_token_blocks_the_browser_flow(
    runner: CliRunner, idp: IdentityProviderStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_TOKEN", "aisq_" + "e" * 43)
    result = _login(runner, idp.url)
    assert result.exit_code == 1
    assert "AISQUARE_TOKEN is set" in result.output
    assert idp.requests == []


# ------------------------------------------------ with-token, whoami, status, token, logout


def test_login_with_token_checks_it_against_userinfo(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    idp.issued.append("aisq_" + "v" * 43)
    result = runner.invoke(
        app, ["login", "--with-token", "--api-url", idp.url], input="aisq_" + "v" * 43 + "\n"
    )
    assert result.exit_code == 0, result.output
    assert _stored()["iam_token"] == "aisq_" + "v" * 43
    assert _stored()["iam_email"] == "anmol@example.com"

    bad = runner.invoke(
        app, ["--json", "login", "--with-token", "--api-url", idp.url], input="aisq_nope\n"
    )
    assert bad.exit_code == 1
    assert json.loads(bad.stdout)["error"] == "invalid_token"


def test_whoami_reads_the_file_offline(runner: CliRunner, idp: IdentityProviderStub) -> None:
    assert _login(runner, idp.url).exit_code == 0
    seen = len(idp.requests)
    result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 0, result.output
    assert "anmol@example.com" in result.output
    assert idp.url in result.output
    assert "expires in" in result.output
    assert len(idp.requests) == seen

    as_json = runner.invoke(app, ["--json", "whoami"])
    assert json.loads(as_json.stdout)["user"]["email"] == "anmol@example.com"


def test_whoami_and_token_when_not_signed_in(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "whoami"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "not_authenticated"
    result = runner.invoke(app, ["auth", "token"])
    assert result.exit_code == 1
    assert "Not signed in" in result.output


def test_whoami_reports_an_environment_token(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_TOKEN", "aisq_" + "e" * 43)
    monkeypatch.setenv("AISQUARE_API_URL", "https://api.example.com")
    result = runner.invoke(app, ["--json", "whoami"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["source"] == "env"
    assert payload["api_url"] == "https://api.example.com"


def test_auth_status_shapes(runner: CliRunner, idp: IdentityProviderStub) -> None:
    before = runner.invoke(app, ["--json", "auth", "status"])
    assert before.exit_code == 1
    assert json.loads(before.stdout) == {
        "signed_in": False,
        "source": None,
        "api_url": None,
        "user": None,
        "expires_at": None,
        "live": None,
    }

    assert _login(runner, idp.url).exit_code == 0
    after = runner.invoke(app, ["--json", "auth", "status", "--live"])
    assert after.exit_code == 0, after.output
    payload = json.loads(after.stdout)
    assert payload["signed_in"] is True
    assert payload["source"] == "file"
    assert payload["user"]["email"] == "anmol@example.com"
    assert payload["live"]["ok"] is True

    idp.revoked.append(idp.issued[0])
    dead = json.loads(runner.invoke(app, ["--json", "auth", "status", "--live"]).stdout)
    assert dead["live"] == {
        "ok": False,
        "error": "session_expired",
        "message": "Your AISquare session has expired or was revoked. Run aisquare login.",
    }


def test_auth_token_prints_only_the_token(runner: CliRunner, idp: IdentityProviderStub) -> None:
    assert _login(runner, idp.url).exit_code == 0
    result = runner.invoke(app, ["auth", "token"])
    assert result.exit_code == 0
    assert result.stdout.strip() == idp.issued[0]
    assert json.loads(runner.invoke(app, ["--json", "auth", "token"]).stdout) == {
        "token": idp.issued[0]
    }


def test_logout_revokes_and_forgets_but_keeps_other_credentials(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    credentials.store(api_key="keep-me")
    assert _login(runner, idp.url).exit_code == 0
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0, result.output
    assert "revoked on the server" in result.output
    assert idp.revoked == [idp.issued[0]]
    stored = _stored()
    assert "iam_token" not in stored
    assert stored["api_key"] == "keep-me"

    again = runner.invoke(app, ["--json", "logout"])
    assert json.loads(again.stdout) == {
        "signed_out": False,
        "server_revoked": False,
        "env_token_still_set": False,
    }


def test_logout_offline_forgets_locally_and_says_so(runner: CliRunner) -> None:
    stub = IdentityProviderStub()
    assert _login(runner, stub.url).exit_code == 0
    stub.close()
    # discovery is cached, so the revoke call is what fails
    result = runner.invoke(app, ["--json", "logout"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "signed_out": True,
        "server_revoked": False,
        "env_token_still_set": False,
    }
    assert "iam_token" not in _stored()


def test_a_second_login_replaces_and_revokes_the_first(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    assert _login(runner, idp.url).exit_code == 0
    first = idp.issued[0]
    result = _login(runner, idp.url)
    assert result.exit_code == 0, result.output
    assert "Already signed in as anmol@example.com" in result.output
    assert _stored()["iam_token"] == idp.issued[1]
    assert idp.revoked == [first]


# --------------------------------------------------------------------------- the API helper


def test_request_sends_the_bearer_and_workspace_and_maps_401(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    assert _login(runner, idp.url).exit_code == 0
    result = iam.request("api/v1/ping/", workspace="ws-7", api_url=idp.url)
    assert result.status == 200
    assert result.body == {"pong": True, "workspace": "ws-7"}

    idp.revoked.append(idp.issued[0])
    with pytest.raises(iam.IamError) as caught:
        iam.request("api/v1/ping/", api_url=idp.url)
    assert caught.value.code == "session_expired"


def test_request_refuses_a_token_for_a_different_host(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    assert _login(runner, idp.url).exit_code == 0
    with pytest.raises(iam.IamError) as caught:
        iam.request("api/v1/ping/", api_url="https://stg-api.example.com")
    assert caught.value.code == "api_url_mismatch"
    assert "stg-api.example.com" in caught.value.message


def test_request_without_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(iam.IamError) as caught:
        iam.request("api/v1/ping/", api_url="https://api.example.com")
    assert caught.value.code == "not_authenticated"


# ------------------------------------------------------ the token never leaves in the clear


def test_the_redaction_rules_know_our_token_shape() -> None:
    token = "aisq_" + "Q" * 43
    assert token not in redact(f"my token is {token} ok", RedactionLevel.standard)
    assert MARKER in redact(f"my token is {token} ok", RedactionLevel.standard)
    assert "aisqr_" + "R" * 43 not in redact("refresh aisqr_" + "R" * 43, RedactionLevel.standard)


def test_drop_removes_only_the_named_keys(isolated_home: Path) -> None:
    credentials.store(api_key="k", iam_token="t", iam_api_url="u")
    remaining = credentials.drop("iam_token", "iam_api_url", "never_there")
    assert remaining == {"api_key": "k"}
    assert credentials.load_all() == {"api_key": "k"}
    assert credentials.drop("api_key") == {}
