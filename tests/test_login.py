"""``aisquare login`` and friends against a loopback identity provider.

The contract is ``docs/plans/aisquare-login.md`` section 4. Every test here
drives the real command through the real ``urllib`` path to a stub server;
only ``time.sleep`` is patched, so the poll loop runs in milliseconds.
"""

from __future__ import annotations

import json
import stat
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import auth as auth_cli
from aisquare.cli.app import app
from aisquare.core import credentials, paths
from aisquare.core.redaction import MARKER, redact
from aisquare.models import RedactionLevel
from aisquare.services import device_flow, iam
from tests.idp_stub import CLI, DEVICE_CODE, RLX, USER_CODE, IdentityProviderStub

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
    assert f"{idp.url}/rlx?code={USER_CODE}" in result.output
    assert "Signed in as anmol@example.com" in result.output
    assert "This session expires" in result.output

    stored = _stored()
    assert stored["iam_token"] == idp.issued[0]
    assert stored["iam_api_url"] == idp.url
    assert stored["iam_email"] == "anmol@example.com"
    assert stored["iam_sub"] == "uid-123"
    assert stored["iam_client_id"] == "rlx"
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
    assert start["form"] == {"client_id": "rlx", "scope": "openid profile email aisquare"}
    assert start["headers"]["user-agent"].startswith("aisquare-cli/")
    assert "(" in start["headers"]["user-agent"]
    assert start["headers"]["x-device-name"]
    assert start["headers"]["content-type"] == "application/x-www-form-urlencoded"
    poll = idp.requests[2]
    assert poll["form"]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert poll["form"]["device_code"] == DEVICE_CODE
    assert poll["form"]["client_id"] == "rlx"
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
    assert event["verification_uri_complete"].endswith(f"/rlx?code={USER_CODE}")
    assert "ask the user to visit the URL above" in result.stderr


def test_discovery_is_cached_for_the_next_command(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    assert _login(runner, idp.url).exit_code == 0
    discovery_calls = idp.paths().count("/o/.well-known/openid-configuration")
    result = runner.invoke(app, ["auth", "status", "--live"])
    assert result.exit_code == 0, result.output
    assert idp.paths().count("/o/.well-known/openid-configuration") == discovery_calls


# ------------------------------------------ rlx's own page, and aisquare-cli's on an old server

#: The web app's origin, which is not the API's (prod: https://home.aisquare.studio). A link
#: built here, from the API's URL or from a host this code knows, could not be this one.
FRONTEND = "https://home.rlx-signin.example"


def _old_credentials_file(api_url: str, token: str, *, client_key: bool) -> None:
    """The credentials file as 0.7.0 to 0.10.0 wrote it, ``iam_client_id`` "aisquare-cli"
    included; without that key when ``client_key`` is False."""
    document = {
        "api_key": "keep-me",
        "iam_api_url": api_url,
        "iam_token": token,
        "iam_token_expires_at": (datetime.now(UTC) + timedelta(days=60)).isoformat(),
        "iam_scope": "openid profile email aisquare",
        "iam_sub": "uid-123",
        "iam_email": "anmol@example.com",
        "iam_name": "Anmol Majithia",
    }
    if client_key:
        document["iam_client_id"] = "aisquare-cli"
    path = paths.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_an_rlx_aware_server_signs_in_as_rlx_all_the_way(runner: CliRunner) -> None:
    idp = IdentityProviderStub(frontend=FRONTEND)
    try:
        result = _login(runner, idp.url)
        assert result.exit_code == 0, result.output
        assert f"{FRONTEND}/rlx?code={USER_CODE}" in result.output
        assert "/cli" not in result.output
        assert idp.starts() == [RLX]
        assert idp.polled_as() == [RLX, RLX]
        [token] = idp.issued
        assert idp.issued_to[token] == RLX
        assert _stored()["iam_client_id"] == RLX
        session = iam.stored_session()
        assert session is not None and session.client_id == RLX

        out = runner.invoke(app, ["logout"])
        assert out.exit_code == 0, out.output
        assert "revoked on the server" in out.output
        assert idp.revocations == [(token, RLX)]
        assert idp.revoked == [token]
    finally:
        idp.close()


def test_an_old_server_falls_back_to_aisquare_cli_and_says_nothing_of_it(
    runner: CliRunner,
) -> None:
    """Until the identity provider knows rlx it answers 401 invalid_client, and the sign-in
    starts again as aisquare-cli at the /cli page: the same sign-in, so the same words."""
    old = IdentityProviderStub(clients=(CLI,), frontend=FRONTEND)
    new = IdentityProviderStub(frontend=FRONTEND)
    try:
        fell_back = _login(runner, old.url)
        assert fell_back.exit_code == 0, fell_back.output
        assert f"{FRONTEND}/cli?code={USER_CODE}" in fell_back.output
        assert "/rlx" not in fell_back.output
        assert old.starts() == [RLX, CLI]  # rlx first, refused; then aisquare-cli
        assert old.polled_as() == [CLI, CLI]  # every poll as the client that started it
        [token] = old.issued
        assert old.issued_to[token] == CLI
        assert _stored()["iam_client_id"] == CLI

        out = runner.invoke(app, ["logout"])
        assert out.exit_code == 0, out.output
        assert "revoked on the server" in out.output
        assert old.revocations == [(token, CLI)]
        assert old.revoked == [token]

        # Nothing alarming: word for word what a sign-in on an rlx-aware server says.
        direct = _login(runner, new.url)
        assert direct.exit_code == 0, direct.output
        assert fell_back.output == direct.output.replace("/rlx?code=", "/cli?code=")
    finally:
        old.close()
        new.close()


@pytest.mark.parametrize(("clients", "page"), [((RLX, CLI), "/rlx"), ((CLI,), "/cli")])
def test_the_links_shown_are_the_ones_the_server_sent(
    runner: CliRunner, clients: tuple[str, ...], page: str
) -> None:
    idp = IdentityProviderStub(clients=clients, frontend=FRONTEND)
    try:
        result = _login(runner, idp.url, json_mode=True)
        assert result.exit_code == 0, result.output
        [event] = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
        assert event["verification_uri"] == f"{FRONTEND}{page}"
        assert event["verification_uri_complete"] == f"{FRONTEND}{page}?code={USER_CODE}"
        # `login --json` reports the fields it always did; the client is not one of them.
        assert set(json.loads(result.stdout)) == {"user", "api_url", "expires_at", "source"}
    finally:
        idp.close()


@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (401, "invalid_request", "unsupported_server"),  # a 401, but not invalid_client
        (400, "invalid_client", "unsupported_server"),  # invalid_client, but not a 401
        (403, "invalid_client", "unsupported_server"),
        (429, "rate_limited", "rate_limited"),
        (503, "temporarily_unavailable", "paused"),
        (500, "server_error", "unreachable"),
    ],
)
def test_no_other_answer_to_the_start_falls_back(
    runner: CliRunner, status: int, error: str, code: str
) -> None:
    idp = IdentityProviderStub(start_status=status, start_error=error)
    try:
        result = _login(runner, idp.url, json_mode=True)
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == code
        assert idp.starts() == [RLX], "only exactly HTTP 401 invalid_client is an old server"
        assert idp.polls() == 0
    finally:
        idp.close()


def test_a_server_that_knows_neither_client_is_asked_once_as_each(runner: CliRunner) -> None:
    idp = IdentityProviderStub(clients=())
    try:
        result = _login(runner, idp.url)
        assert result.exit_code == 1
        # aisquare-cli's refusal, said as it always was.
        assert "refused to start a device sign-in (invalid_client)" in result.output
        assert idp.starts() == [RLX, CLI]
    finally:
        idp.close()


def test_the_accounts_pages_wait_polls_and_stores_as_the_client_that_started() -> None:
    """The fleet UI's sign-in (``services.device_flow``) against an old server, for real."""
    old = IdentityProviderStub(clients=(CLI,))
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    try:
        endpoints = iam.discover(old.url)
        grant = iam.start_device_authorization(endpoints)
        assert grant.client_id == CLI
        assert grant.verification_uri_complete == f"{old.url}/cli?code={USER_CODE}"
        token = device_flow.wait_for_token(
            endpoints, grant, cancelled=lambda: False, sleep=sleep, monotonic=lambda: now[0]
        )
        session = device_flow.commit_sign_in(
            old.url, endpoints, token, client_id=grant.client_id, cancelled=lambda: False
        )
        assert old.polled_as() == [CLI, CLI]
        assert session.client_id == CLI and _stored()["iam_client_id"] == CLI
    finally:
        old.close()


@pytest.mark.parametrize("client_key", [True, False], ids=["0.10.0", "no-client-key"])
def test_a_session_stored_by_0_10_0_still_works_and_signs_out_as_aisquare_cli(
    runner: CliRunner, idp: IdentityProviderStub, client_key: bool
) -> None:
    token = idp.issue(CLI)
    _old_credentials_file(idp.url, token, client_key=client_key)
    session = iam.stored_session()
    assert session is not None and session.client_id == CLI

    whoami = runner.invoke(app, ["whoami"])
    assert whoami.exit_code == 0, whoami.output
    assert "anmol@example.com" in whoami.output and "expires in" in whoami.output
    assert iam.request("api/v1/ping/", api_url=idp.url).status == 200
    status = runner.invoke(app, ["--json", "auth", "status", "--live"])
    assert json.loads(status.stdout)["live"]["ok"] is True

    out = runner.invoke(app, ["logout"])
    assert out.exit_code == 0, out.output
    assert "revoked on the server" in out.output
    assert idp.revocations == [(token, CLI)]
    assert idp.revoked == [token]
    assert "iam_token" not in _stored() and _stored()["api_key"] == "keep-me"


def test_an_rlx_sign_in_retires_an_aisquare_cli_session_as_aisquare_cli(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    old_token = idp.issue(CLI)
    _old_credentials_file(idp.url, old_token, client_key=True)
    result = _login(runner, idp.url)
    assert result.exit_code == 0, result.output
    assert "Already signed in as anmol@example.com" in result.output
    new_token = idp.issued[-1]
    assert idp.issued_to[new_token] == RLX
    assert idp.revocations == [(old_token, CLI)]
    assert idp.revoked == [old_token]
    assert _stored()["iam_client_id"] == RLX


def test_a_link_to_the_web_apps_rlx_page_is_shown_as_it_came() -> None:
    """``_safe_link`` takes the /rlx page on the web app's host as it takes /cli, and still
    refuses what it refused: plain http off the loopback, and anything that is not a URL."""
    issuer = "https://api.aisquare.studio/o"
    for url in (
        "https://home.aisquare.studio/rlx",
        "https://home.aisquare.studio/rlx?code=WDJB-MJHT",
        "https://home.aisquare.studio/cli?code=WDJB-MJHT",
        "http://127.0.0.1:3000/rlx?code=WDJB-MJHT",
    ):
        assert iam._safe_link(url, issuer) == url
    for url in (
        "http://home.aisquare.studio/rlx?code=WDJB-MJHT",
        "javascript:alert(1)",
        "/rlx?code=WDJB-MJHT",
        "file:///rlx",
    ):
        with pytest.raises(iam.IamError) as caught:
            iam._safe_link(url, issuer)
        assert caught.value.code == "unsupported_server"


def test_the_stub_binds_codes_and_tokens_to_their_client(idp: IdentityProviderStub) -> None:
    """The contract the tests above lean on: another client's poll for a device code is
    ``invalid_grant``, and another client's revocation of a token leaves it live."""
    endpoints = iam.discover(idp.url)
    for starter, other in ((RLX, CLI), (CLI, RLX)):
        started = iam._http(
            "POST", endpoints.device_authorization, form={"client_id": starter, "scope": iam.SCOPE}
        )
        assert started.status == 200
        crossed = iam._http(
            "POST",
            endpoints.token,
            form={"grant_type": iam.DEVICE_GRANT, "device_code": DEVICE_CODE, "client_id": other},
        )
        assert (crossed.status, crossed.error) == (400, "invalid_grant")
        token = idp.issue(starter)
        refused = iam._http("POST", endpoints.revocation, form={"token": token, "client_id": other})
        assert refused.status == 400 and token not in idp.revoked
        assert (
            iam._http(
                "POST", endpoints.revocation, form={"token": token, "client_id": starter}
            ).status
            == 200
        )
        assert token in idp.revoked


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


def test_the_escape_watcher_stop_waits_for_its_thread() -> None:
    # The thread's ``finally`` restores the terminal; stop() must not return
    # while it could still be running, or a cancelled login leaves the shell
    # in cbreak mode. (Off a TTY the thread ends at once; the join is what is
    # under test.)
    watcher = auth_cli._EscapeWatcher(threading.Event())
    watcher.start()
    watcher.stop()
    assert not watcher._thread.is_alive()


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
    # The reason has to name the address or say it was refused. `Connection
    # refused` is the POSIX wording; Windows says `[WinError 10061] No
    # connection could be made because the target machine actively refused it`
    # and does not repeat the address — so matching either spelling exactly
    # asserted one platform's strerror rather than the property.
    detail = payload["detail"]
    assert "127.0.0.1:9" in detail or "refused" in detail.lower(), detail


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
    # Whose token it is cannot be known here; stored as aisquare-cli's, as before 0.10.1.
    assert _stored()["iam_client_id"] == "aisquare-cli"

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


@pytest.mark.parametrize("restricted", [False, True])
def test_a_stored_session_carries_whether_its_file_could_be_restricted(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    restricted: bool,
) -> None:
    """The warning on stderr reaches a terminal and nothing else. The session carries it, so
    the fleet UI, whose stderr Textual captures, can say it too (review of #65, R7)."""
    monkeypatch.setattr(paths, "restrict_to_owner", lambda _path: restricted)
    session = iam.store_session(
        api_url="https://api.example",
        token="t",
        expires_in=None,
        scope="s",
        claims={},
        client_id=iam.CLIENT_ID,
    )
    assert session.unrestricted is not restricted
    warned = "may be able to read your session token" in capsys.readouterr().err
    assert warned is not restricted


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
        "minted_keys_cleared": 0,  # the signed-in branch's shape (#142)
        "minted_keys_still_live": [],
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
        "minted_keys_cleared": 0,  # #142: no project had a CLI-minted ingest key
        "minted_keys_still_live": [],
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


def test_a_login_to_another_host_never_hands_it_the_first_hosts_token(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    assert _login(runner, idp.url).exit_code == 0
    other = IdentityProviderStub()
    try:
        result = _login(runner, other.url)
        assert result.exit_code == 0, result.output
        # The old token is neither sent to the new server nor revoked on the old
        # one (the CLI is no longer talking to it); it expires on its own.
        assert "/o/revoke_token/" not in other.paths()
        assert other.revoked == []
        assert _stored()["iam_token"] == other.issued[0]
    finally:
        other.close()
    assert idp.revoked == []
    assert "/o/revoke_token/" not in idp.paths()
    assert _stored()["iam_api_url"] == other.url


def test_login_with_token_retires_the_previous_session_on_the_same_host(
    runner: CliRunner, idp: IdentityProviderStub
) -> None:
    assert _login(runner, idp.url).exit_code == 0
    first = idp.issued[0]
    idp.issued.append("aisq_" + "v" * 43)
    result = runner.invoke(
        app, ["login", "--with-token", "--api-url", idp.url], input="aisq_" + "v" * 43 + "\n"
    )
    assert result.exit_code == 0, result.output
    assert idp.revoked == [first]
    assert _stored()["iam_token"] == "aisq_" + "v" * 43


@pytest.mark.parametrize("claims", [{"sub": "uid-456"}, {}])
def test_login_with_token_clears_the_previous_sessions_optional_fields(
    runner: CliRunner, idp: IdentityProviderStub, claims: dict[str, str]
) -> None:
    credentials.store(api_key="keep-me", serve_token="keep-this-too")
    assert _login(runner, idp.url).exit_code == 0
    previous = iam.stored_session()
    assert previous is not None
    assert previous.expires_at is not None
    assert all((previous.scope, previous.sub, previous.email, previous.name))

    token = "aisq_" + "v" * 43
    idp.issued.append(token)
    idp.claims = dict(claims)
    result = runner.invoke(
        app, ["--json", "login", "--with-token", "--api-url", idp.url], input=token + "\n"
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["expires_at"] is None
    assert payload["user"] == {"sub": claims.get("sub", ""), "email": "", "name": ""}

    reloaded = iam.stored_session()
    assert reloaded is not None
    assert reloaded.as_json() == payload
    assert reloaded.token == token
    assert reloaded.scope == ""
    whoami = runner.invoke(app, ["--json", "whoami"])
    assert whoami.exit_code == 0, whoami.output
    # whoami carries one field login does not: where the active project's traces
    # land (#142) — none chosen here.
    assert json.loads(whoami.stdout) == {**payload, "destination": None, "credits": None}
    assert _stored()["api_key"] == "keep-me"
    assert _stored()["serve_token"] == "keep-this-too"


def test_discovered_endpoints_must_share_the_servers_origin(runner: CliRunner) -> None:
    idp = IdentityProviderStub(
        discovery_overrides={"token_endpoint": "https://elsewhere.example/o/token/"}
    )
    try:
        result = _login(runner, idp.url, json_mode=True)
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "unsupported_server"
        # Refused before any code was requested, so nothing could be redirected.
        assert "/o/device-authorization/" not in idp.paths()
    finally:
        idp.close()
    assert "iam_token" not in _stored()


@pytest.mark.parametrize("page", ["/cli", "/rlx"])
def test_a_sign_in_link_over_plain_http_to_a_remote_host_is_refused(
    runner: CliRunner, page: str
) -> None:
    idp = IdentityProviderStub(verification_uri=f"http://elsewhere.example{page}")
    try:
        result = _login(runner, idp.url, json_mode=True)
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "unsupported_server"
        assert idp.polls() == 0
    finally:
        idp.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_discovery_cache_is_private(runner: CliRunner, idp: IdentityProviderStub) -> None:
    assert _login(runner, idp.url).exit_code == 0
    cached = sorted((paths.cache_dir() / "oidc").glob("*.json"))
    assert cached, "discovery was not cached"
    assert stat.S_IMODE(cached[0].stat().st_mode) == 0o600


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


def test_an_answer_http_client_cannot_read_is_unreachable_and_an_error_keeps_its_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``http.client`` raises its own exceptions, not ``OSError``s — ``LineTooLong`` for a
    header past its limit, ``IncompleteRead`` for a body cut short — and they escaped
    ``_http`` past every caller that tolerates an unreachable server by catching
    ``IamError`` (review of the accounts stack's fold, round 1, F2). An error status
    whose body is cut short is read inside the ``except HTTPError`` branch, where no
    sibling clause catches anything: it keeps its status, with what arrived."""
    import urllib.request
    from email.message import Message
    from http.client import IncompleteRead, LineTooLong
    from urllib.error import HTTPError

    def too_long(request: urllib.request.Request, timeout: float, **_: object) -> object:
        raise LineTooLong("header line")

    monkeypatch.setattr(urllib.request, "urlopen", too_long)
    with pytest.raises(iam.IamError) as caught:
        iam._http("GET", "https://api.example.com/api/v1/ping/")
    assert caught.value.code == "unreachable"

    class CutShort:
        def read(self, *args: object) -> bytes:
            raise IncompleteRead(b'{"detail": "unava', expected=40)

        def close(self) -> None:  # HTTPError closes its body when collected
            return None

    def unavailable(request: urllib.request.Request, timeout: float, **_: object) -> object:
        raise HTTPError(request.full_url, 503, "unavailable", Message(), CutShort())  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    result = iam._http("GET", "https://api.example.com/api/v1/ping/")
    assert result.status == 503 and result.body == '{"detail": "unava'


def test_a_success_whose_answer_is_cut_short_says_the_server_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2xx whose body will not read is ``unreachable`` still — every caller that
    tolerates one stays as it was — but it was SAID as "Could not reach", for a server
    that answered and may have done what was asked: a mint's key exists with its uid
    never read, and the message invited a retry that mints another (review of the
    accounts stack's fold, round 2, F3). A read that times out mid-body is the same."""
    import urllib.request
    from http.client import IncompleteRead

    failure: BaseException = IncompleteRead(b'{"uid": "k-', expected=30)

    class Answered:
        status = 201

        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def __enter__(self) -> Answered:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, *args: object) -> bytes:
            raise failure

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout, **_: Answered())
    url = "https://api.example.com/api/v2/iam/workspace-api-key/"
    with pytest.raises(iam.IamError) as caught:
        iam._http("POST", url, json_body={"workspace_id": 7})
    assert caught.value.code == "unreachable"
    assert caught.value.message == (
        f"{url} answered HTTP 201, but the answer was cut short "
        "(IncompleteRead(11 bytes read, 30 more expected)); the request may have been "
        "carried out."
    )
    failure = TimeoutError("timed out")
    with pytest.raises(iam.IamError) as caught:
        iam._http("GET", url)
    assert caught.value.code == "unreachable"
    assert caught.value.message == (
        f"{url} answered HTTP 201, but the answer was cut short (TimeoutError('timed out'))."
    )


# ------------------------------------------------------ the token never leaves in the clear


def test_the_redaction_rules_know_our_token_shape() -> None:
    token = "aisq_" + "Q" * 43
    assert token not in redact(f"my token is {token} ok", RedactionLevel.standard)
    assert MARKER in redact(f"my token is {token} ok", RedactionLevel.standard)
    assert "aisqr_" + "R" * 43 not in redact("refresh aisqr_" + "R" * 43, RedactionLevel.standard)


def test_drop_removes_only_the_named_keys(isolated_home: Path) -> None:
    credentials.store(api_key="k", iam_token="t", iam_api_url="u")
    remaining, _ = credentials.drop("iam_token", "iam_api_url", "never_there")
    assert remaining == {"api_key": "k"}
    assert credentials.load_all() == {"api_key": "k"}
    assert credentials.drop("api_key") == ({}, True)
