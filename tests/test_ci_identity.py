"""The signed-in developer's view: ``doctor``'s identity lines and ``ci bind-workspace``.

C2a and C3 of ``docs/ci-user-identity-handoff.md``. A developer who ran
``aisquare login`` has no ``AISQUARE_CI_KEY`` and no ``AISQUARE_CI_RUN``; the run
comes from ``GET /v1/me`` for the workspace the project is bound to. These tests
drive that path through the stub server with a token from the environment, so
no credentials file is read and nothing here depends on a real sign-in.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core import workspace as workspace_core
from aisquare.core.config import AppConfig, load_config, save_config
from aisquare.models import CheckStatus, ClientReason, DoctorCheck
from aisquare.services import ci_client
from aisquare.services.diagnostics import doctor
from tests.ci_schemas import fixture
from tests.stub_ci_server import StubCI, serve

TOKEN = "aisq_test-token-0000000000000000000000000000"
ME = fixture("me.v1.valid")
PRINCIPAL = ME["principal_id"]
TEAM = "ws_kernel01"
QUIET = "ws_9a8b7c6d5e4f30211203948576abcdef"
"""The fixture's second workspace: a membership with no run published."""


@pytest.fixture
def stub() -> Iterator[StubCI]:
    yield from serve()


def signed_in(monkeypatch: pytest.MonkeyPatch, stub: StubCI) -> None:
    """On, pointed at the stub, signed in through the environment, nothing exported."""
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(ci_client.RUN_ENV_VAR, raising=False)
    monkeypatch.setenv("AISQUARE_TOKEN", TOKEN)


def project_id() -> str:
    return workspace_core.current_project().id


def bind(workspace: str) -> None:
    config = AppConfig()
    config.experiment.enabled = True
    config.experiment.bindings[project_id()] = workspace
    save_config(config)
    ci_client.reset_cache()


def ci_checks() -> dict[str, DoctorCheck]:
    return {c.name: c for c in doctor() if c.name.startswith("ci ")}


def one_workspace_body() -> str:
    only = json.loads(json.dumps(ME))
    only["workspaces"] = only["workspaces"][:1]
    return json.dumps(only)


# --- doctor -------------------------------------------------------------------


def test_signed_in_and_bound_is_five_green_lines(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """The acceptance: no key, no run exported, and every line is green because
    the run came from GET /v1/me for the bound workspace."""
    signed_in(monkeypatch, stub)
    bind(TEAM)

    checks = ci_checks()

    assert {name: c.status for name, c in checks.items()} == {
        "ci test bed": CheckStatus.ok,
        "ci identity": CheckStatus.ok,
        "ci workspace": CheckStatus.ok,
        "ci endpoint": CheckStatus.ok,
        "ci descriptor": CheckStatus.ok,
    }
    assert "run_kernel0001 from GET /v1/me" in checks["ci test bed"].detail
    assert PRINCIPAL in checks["ci identity"].detail
    assert "2 workspaces" in checks["ci identity"].detail
    assert checks["ci workspace"].detail == f"{TEAM} (developer), run run_kernel0001"
    assert stub.me_fetches == 1


def test_the_identity_lines_never_carry_the_token(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    bind(TEAM)

    for check in doctor():
        assert TOKEN not in check.detail
        assert TOKEN not in (check.fix or "")


def test_several_workspaces_and_no_binding_names_the_choice(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """Guessing would bind the project to whichever the server listed first."""
    signed_in(monkeypatch, stub)

    checks = ci_checks()

    assert checks["ci identity"].status is CheckStatus.ok
    workspace = checks["ci workspace"]
    assert workspace.status is CheckStatus.warn
    assert TEAM in workspace.detail and QUIET in workspace.detail
    assert workspace.fix and "aisquare ci bind-workspace" in workspace.fix
    bed = checks["ci test bed"]
    assert bed.status is CheckStatus.warn and "no_run" in bed.detail
    assert "ci descriptor" not in checks, "no run, so nothing to fetch a descriptor for"


def test_a_single_workspace_needs_no_binding(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    stub.me_body = one_workspace_body()

    checks = ci_checks()

    assert checks["ci workspace"].status is CheckStatus.ok
    assert "1 workspace —" not in checks["ci identity"].detail
    assert "in 1 workspace" in checks["ci identity"].detail


def test_a_binding_to_a_workspace_the_user_is_not_in_warns(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    bind("ws_somebody_elses")

    workspace = ci_checks()["ci workspace"]

    assert workspace.status is CheckStatus.warn
    assert "not a member of ws_somebody_elses" in workspace.detail
    assert workspace.fix and "bind-workspace" in workspace.fix


def test_a_bound_workspace_with_no_run_says_so(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    bind(QUIET)

    workspace = ci_checks()["ci workspace"]

    assert workspace.status is CheckStatus.warn
    assert "no run published" in workspace.detail
    assert workspace.fix and "controller" in workspace.fix


def test_a_401_on_me_says_to_sign_in_again(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    stub.me_status = 401

    checks = ci_checks()

    identity = checks["ci identity"]
    assert identity.status is CheckStatus.warn
    assert "401" in identity.detail
    assert identity.fix and "aisquare login" in identity.fix
    assert "ci workspace" not in checks


def test_a_server_that_does_not_answer_me_is_named_not_blamed_on_the_session(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    stub.me_status = 503

    identity = ci_checks()["ci identity"]

    assert identity.status is CheckStatus.warn
    assert "503" in identity.detail
    assert identity.fix and "aisquare login" not in identity.fix


def test_a_server_that_predates_me_is_informational_when_a_run_is_exported(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """The harness and joint-smoke path: an exported run means the hooks never
    ask GET /v1/me, and the server they talk to today does not serve it. Round 3
    warned here with "turn the hooks off", to an operator whose delivery worked;
    the line is informational and names the export that is in use (round 4)."""
    signed_in(monkeypatch, stub)
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "k")
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, "run_kernel0001")
    stub.me_status = 404

    checks = ci_checks()

    assert checks["ci test bed"].status is CheckStatus.ok
    identity = checks["ci identity"]
    assert identity.status is CheckStatus.ok
    assert "404" in identity.detail and "run_kernel0001" in identity.detail
    assert "/ready answers" in identity.detail, "the version reading names its evidence"
    assert not identity.fix
    assert "AISQUARE_CI=0" not in identity.detail


def test_a_404_with_ready_down_is_a_warning_about_the_url_not_a_version_reading(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """A 404 carries no version: a stale host, a prefix that no longer routes or
    a proxy that 404s the unknown all produce it, and round 4 printed a green
    "this server predates the route" for every one of them on the exported-run
    branch (round 5). Only a /ready that answers earns that reading; otherwise
    the line is a warning about the URL, run exported or not."""
    signed_in(monkeypatch, stub)
    stub.me_status = 404
    stub.ready_status = 503

    monkeypatch.setenv(ci_client.RUN_ENV_VAR, "run_kernel0001")
    exported = ci_checks()["ci identity"]
    monkeypatch.delenv(ci_client.RUN_ENV_VAR)
    unexported = ci_checks()

    for identity in (exported, unexported["ci identity"]):
        assert identity.status is CheckStatus.warn
        assert "404" in identity.detail and "predates" not in identity.detail
        assert "not answering as a CI server" in identity.detail, "the conclusion, not a pointer"
        assert identity.fix and "AISQUARE_CI_URL" in identity.fix and stub.url in identity.fix
        assert "AISQUARE_CI=0" not in identity.fix
    assert "ci workspace" not in unexported
    assert unexported["ci test bed"].fix == "See the ci identity line", "names only printed lines"
    # One cause, one fix (round 6): the endpoint line keeps its fact about
    # /ready and offers no competing "turn the test bed off".
    endpoint = unexported["ci endpoint"]
    assert endpoint.status is CheckStatus.warn and "/ready did not answer" in endpoint.detail
    assert not endpoint.fix
    assert sum(1 for line in unexported.values() if line.fix) == 2, "pointer plus one fix"


def test_a_server_that_predates_me_warns_with_the_export_as_the_fix_when_no_run_is(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """Without an exported run the hooks WOULD ask GET /v1/me and get nothing,
    so this is a real warning - and the fix is the export, not turning the
    experiment off."""
    signed_in(monkeypatch, stub)
    stub.me_status = 404

    checks = ci_checks()

    identity = checks["ci identity"]
    assert identity.status is CheckStatus.warn
    assert "404" in identity.detail
    assert identity.fix and "AISQUARE_CI_RUN" in identity.fix
    assert "AISQUARE_CI=0" not in identity.fix
    assert checks["ci test bed"].status is CheckStatus.warn
    assert "no run resolved" in checks["ci test bed"].detail
    # No `ci workspace` line exists on this path, so the fix must not name one.
    assert "ci workspace" not in checks
    assert checks["ci test bed"].fix == "See the ci identity line"


def test_an_exported_run_still_wins_and_the_identity_is_still_shown(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, "run_exported0001")

    checks = ci_checks()

    assert "run run_exported0001," in checks["ci test bed"].detail
    assert "GET /v1/me" not in checks["ci test bed"].detail
    assert checks["ci identity"].status is CheckStatus.ok


def test_the_experiment_token_gets_an_identity_line_and_no_workspace_line(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """The server resolves harness bearers too, so "who does CI think I am" has
    an answer for an experiment token; the exported run wins, so the binding is
    not consulted and there is no workspace line to warn about."""
    signed_in(monkeypatch, stub)
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "k")
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, "run_kernel0001")

    checks = ci_checks()

    assert checks["ci identity"].status is CheckStatus.ok
    assert "experiment token from AISQUARE_CI_KEY" in checks["ci identity"].detail
    assert "ci workspace" not in checks
    assert stub.me_fetches == 1


def test_an_experiment_token_with_no_run_exported_resolves_one_like_the_hooks_do(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """Doctor used to say "no run id" here while _resolve_run asked GET /v1/me
    and found one: the gate is "has a bearer", not "is signed in"."""
    signed_in(monkeypatch, stub)
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "k")
    bind(TEAM)

    checks = ci_checks()

    assert checks["ci test bed"].status is CheckStatus.ok
    assert "run_kernel0001 from GET /v1/me" in checks["ci test bed"].detail
    assert checks["ci workspace"].status is CheckStatus.ok


def test_doctor_probes_me_without_caching_it(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    signed_in(monkeypatch, stub)
    bind(TEAM)

    doctor()

    assert not paths.ci_cache_dir().exists()


# --- ci bind-workspace ---------------------------------------------------------


def _run(runner: CliRunner, *args: str) -> Result:
    return runner.invoke(app, ["ci", "bind-workspace", *args], catch_exceptions=False)


def _text(result: Result) -> str:
    """stdout and stderr together; ``fail`` prints its message to stderr."""
    try:
        return str(result.output) + str(result.stderr)
    except ValueError:
        return str(result.output)


def test_binding_a_workspace_you_are_in_is_written_to_config(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)

    result = _run(runner, TEAM)

    assert result.exit_code == 0, _text(result)
    assert f"to {TEAM} (developer), run run_kernel0001" in result.output
    assert load_config().experiment.bindings == {project_id(): TEAM}
    assert stub.me_fetches == 1


def test_the_only_workspace_is_bound_without_being_named(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)
    stub.me_body = one_workspace_body()

    result = _run(runner)

    assert result.exit_code == 0, _text(result)
    assert load_config().experiment.bindings == {project_id(): TEAM}


def test_several_workspaces_and_no_argument_lists_them_and_refuses(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)

    result = _run(runner)

    assert result.exit_code == 1
    text = _text(result)
    assert TEAM in text and QUIET in text
    assert load_config().experiment.bindings == {}


def test_a_workspace_you_are_not_in_is_refused(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    """The binding is a selector and the server would refuse anyway; refusing
    here saves the developer a session of no_run rows."""
    signed_in(monkeypatch, stub)

    result = _run(runner, "ws_somebody_elses")

    assert result.exit_code == 1
    assert "not one of your workspaces" in _text(result)
    assert load_config().experiment.bindings == {}


def test_clear_forgets_the_binding(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)
    bind(TEAM)

    result = _run(runner, "--clear")

    assert result.exit_code == 0, _text(result)
    assert load_config().experiment.bindings == {}
    assert stub.me_fetches == 0, "clearing asks the server nothing"


def test_clear_forgets_only_this_projects_binding(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    """Another checkout's binding is not this command's to touch."""
    signed_in(monkeypatch, stub)
    bind(TEAM)
    config = load_config()
    config.experiment.bindings["prj_someone_else"] = "ws_other"
    save_config(config)

    _run(runner, "--clear")

    assert load_config().experiment.bindings == {"prj_someone_else": "ws_other"}


def test_a_rejected_token_points_at_login(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)
    stub.me_status = 401

    result = _run(runner, TEAM)

    assert result.exit_code == 1
    assert "aisquare login" in _text(result)


def test_off_and_no_bearer_each_refuse_by_name(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    monkeypatch.delenv(ci_client.ENABLED_ENV_VAR, raising=False)
    assert "AISQUARE_CI=1" in _text(_run(runner, TEAM))

    signed_in(monkeypatch, stub)
    monkeypatch.delenv("AISQUARE_TOKEN", raising=False)
    assert "aisquare login" in _text(_run(runner, TEAM))
    assert stub.me_fetches == 0


def test_the_command_leaves_no_me_cache_behind(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    signed_in(monkeypatch, stub)

    _run(runner, TEAM)

    assert not paths.ci_cache_dir().exists()


# --- the signed-in token only travels over https ------------------------------


def test_the_signed_in_token_is_withheld_from_a_plain_http_server(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """The fallback bearer is a 90-day OAuth token for the user's whole account,
    and AISQUARE_CI_URL accepts any URL: a stale http:// value must not put it
    on the wire in cleartext. The experiment token keeps its old latitude."""
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "http://ci.internal:8100")
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    monkeypatch.setenv("AISQUARE_TOKEN", TOKEN)

    assert ci_client.api_key_and_source() == ("", ci_client.SIGNED_IN_WITHHELD_SOURCE)
    problem, fix = ci_client.bearer_problem()
    assert "https://" in problem and "ci.internal" in problem and TOKEN not in problem
    assert "AISQUARE_CI_URL" in fix and "AISQUARE_CI_KEY" in fix

    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "experiment-token")
    assert ci_client.api_key_and_source() == (
        "experiment-token",
        ci_client.EXPERIMENT_TOKEN_SOURCE,
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://ci.aisquare.studio",
        "http://127.0.0.1:8100",
        "http://localhost:8100",
        "http://[::1]:8100",
    ],
)
def test_https_anywhere_and_http_to_this_machine_are_allowed(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path, url: str
) -> None:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, url)
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    monkeypatch.setenv("AISQUARE_TOKEN", TOKEN)

    assert ci_client.signed_in_allowed(url)
    assert ci_client.api_key_and_source() == (TOKEN, ci_client.SIGNED_IN_SOURCE)


def test_doctor_names_the_withheld_token_and_the_fix(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "http://ci.internal:8100")
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(ci_client.RUN_ENV_VAR, raising=False)
    monkeypatch.setenv("AISQUARE_TOKEN", TOKEN)
    # The public /ready probe may still run; a bearer-carrying request may not.
    sent: list[dict[str, str]] = []

    def exchange(url: str, **kwargs: Any) -> ci_client.Exchange:
        sent.append(kwargs.get("headers") or {})
        return ci_client._failed(time.monotonic(), ClientReason.transport_error, "stubbed")

    monkeypatch.setattr(ci_client, "exchange", exchange)

    checks = ci_checks()

    assert not any("Authorization" in headers for headers in sent), "the token left the machine"
    bed = checks["ci test bed"]
    assert bed.status is CheckStatus.warn
    assert "withheld" in bed.detail and "https://" in bed.detail
    assert bed.fix and "AISQUARE_CI_URL" in bed.fix
    assert TOKEN not in bed.detail and TOKEN not in bed.fix
    assert "ci identity" not in checks, "nothing is sent, so nothing is asked"


# --- signing out forgets who CI resolved you to --------------------------------


def test_sign_out_forgets_the_resolved_identity(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """The token goes at sign-out; the identity it resolved to must go with it —
    the cache file holds the principal id, the subject and every workspace, and
    the TTL only stops it being served."""
    from aisquare.services import auth as auth_service
    from aisquare.services import ci_me, iam

    ci_me.fetch(base=stub.url, key=TOKEN)
    assert ci_me._cache_path(TOKEN).exists()
    session = iam.Session(api_url="http://127.0.0.1:9", token=TOKEN, source="file")

    auth_service.sign_out(session)

    assert not ci_me._cache_path(TOKEN).exists()
    assert not ci_me._refusal_path(TOKEN).exists()


# --- review round 2 ----------------------------------------------------------


def test_an_expired_login_is_not_sent_and_doctor_says_to_sign_in_again(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """A lapsed 90-day token would only be refused; sending it costs a
    session-start round trip and then caches the refusal for a minute."""
    from datetime import UTC, datetime, timedelta

    from aisquare.core import credentials
    from aisquare.services import iam

    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(ci_client.RUN_ENV_VAR, raising=False)
    monkeypatch.delenv("AISQUARE_TOKEN", raising=False)
    credentials.store(
        **{
            iam.KEY_API_URL: "https://api.test",
            iam.KEY_TOKEN: TOKEN,
            iam.KEY_EXPIRES_AT: (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        }
    )
    ci_client.reset_cache()

    assert ci_client.api_key_and_source() == ("", ci_client.SIGNED_IN_EXPIRED_SOURCE)
    problem, fix = ci_client.bearer_problem()
    assert "expired" in problem and "aisquare login" in fix
    assert "[signed-in token]" in ci_client.scrub_secret(f"x {TOKEN} y"), (
        "the lapsed token is still scrubbed from details"
    )

    checks = ci_checks()

    assert checks["ci test bed"].status is CheckStatus.warn
    assert "expired" in checks["ci test bed"].detail
    assert checks["ci test bed"].fix and "aisquare login" in checks["ci test bed"].fix
    assert stub.me_fetches == 0


def test_the_credentials_file_is_read_once_per_process(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """Six callers per hook each asked iam to stat and parse the credentials
    file; the session is memoised, keyed by the environment token."""
    from aisquare.services import iam

    signed_in(monkeypatch, stub)
    reads: list[int] = []
    real = iam.current_session

    def counting(api_url: str | None = None) -> object:
        reads.append(1)
        return real(api_url)

    monkeypatch.setattr(iam, "current_session", counting)
    ci_client.reset_cache()

    from aisquare.services import diagnostics

    for _ in range(3):
        ci_client.api_key_and_source()
        ci_client.bearer_problem()
        ci_client.scrub_secret("nothing here")
        # The doctor note and the identity line read the same memo, so they
        # cannot describe a snapshot of the file other than the one that chose
        # the bearer (the review of #78).
        assert diagnostics._bearer_note(ci_client.SIGNED_IN_SOURCE).startswith("signed in")
        assert diagnostics._signed_in_as("aisquare-idp:x").startswith("signed in")
    assert len(reads) == 1

    monkeypatch.setenv("AISQUARE_TOKEN", "aisq_another-token-00000000000000000000000000")
    ci_client.api_key()
    assert len(reads) == 2, "a different environment token starts a fresh read"


def test_a_sign_in_from_another_process_is_seen_without_a_restart(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """The memo is keyed by a digest of the credentials file as well, so a
    long-lived `fleet ui` or `serve` sees a login done in another terminal on
    its next call - `None` included, which the first draft would have kept - and
    a same-length rewrite that leaves the timestamp alone (a refresh on a
    coarse filesystem) is seen too, where an mtime-and-size key needed a bumped
    mtime to notice it (round 4)."""
    import os

    from aisquare.core import credentials
    from aisquare.services import iam

    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    monkeypatch.delenv("AISQUARE_TOKEN", raising=False)
    ci_client.reset_cache()
    assert ci_client.api_key_and_source() == ("", "")

    # "Another process" signs in.
    credentials.store(**{iam.KEY_API_URL: "https://api.test", iam.KEY_TOKEN: TOKEN})
    assert ci_client.api_key_and_source() == (TOKEN, ci_client.SIGNED_IN_SOURCE)

    # A refresh rewrites the token at the same length inside one timestamp tick.
    before = os.stat(paths.credentials_path())
    replacement = TOKEN[:-1] + ("1" if TOKEN[-1] != "1" else "2")
    credentials.store(**{iam.KEY_TOKEN: replacement})
    os.utime(paths.credentials_path(), ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(paths.credentials_path())
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
    assert ci_client.api_key_and_source() == (replacement, ci_client.SIGNED_IN_SOURCE)

    credentials.drop(iam.KEY_TOKEN)
    assert ci_client.api_key_and_source() == ("", "")


def test_two_homes_without_a_credentials_file_are_not_one_memo_entry(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The memo key names the credentials PATH: two `AISQUARE_HOME`s that both
    lack the file used to hash alike, and the first home's answer - `None`
    included - was served for the second (round 4)."""
    from aisquare.core import credentials
    from aisquare.services import iam

    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    monkeypatch.delenv("AISQUARE_TOKEN", raising=False)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    monkeypatch.setenv("AISQUARE_HOME", str(first))
    ci_client.reset_cache()
    assert ci_client.api_key_and_source() == ("", "")

    monkeypatch.setenv("AISQUARE_HOME", str(second))
    credentials.store(**{iam.KEY_API_URL: "https://api.test", iam.KEY_TOKEN: TOKEN})
    assert ci_client.api_key_and_source() == (TOKEN, ci_client.SIGNED_IN_SOURCE)

    monkeypatch.setenv("AISQUARE_HOME", str(first))
    assert ci_client.api_key_and_source() == ("", ""), "not the second home's session"


def test_the_memo_key_is_the_resolved_credentials_path(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`AISQUARE_HOME` is taken verbatim, so two spellings of one home (a
    symlink) must share an entry and a relative home under a chdir must not
    carry the previous directory's answer - the round-4 key used the string as
    given and did neither (round 5)."""
    import os

    from aisquare.core import credentials
    from aisquare.services import iam

    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    monkeypatch.delenv("AISQUARE_TOKEN", raising=False)
    real_home = tmp_path / "real"
    real_home.mkdir()
    (tmp_path / "link").symlink_to(real_home, target_is_directory=True)
    reads: list[int] = []
    real_reader = iam.current_session

    def counting(api_url: str | None = None) -> object:
        reads.append(1)
        return real_reader(api_url)

    monkeypatch.setattr(iam, "current_session", counting)

    # One file, two spellings: one memo entry, one read.
    monkeypatch.setenv("AISQUARE_HOME", str(real_home))
    ci_client.reset_cache()
    credentials.store(**{iam.KEY_API_URL: "https://api.test", iam.KEY_TOKEN: TOKEN})
    assert ci_client.api_key_and_source() == (TOKEN, ci_client.SIGNED_IN_SOURCE)
    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path / "link"))
    assert ci_client.api_key_and_source() == (TOKEN, ci_client.SIGNED_IN_SOURCE)
    assert len(reads) == 1, "the symlink is the same file, not a second entry"

    # One relative spelling, two directories: two files, two answers.
    (tmp_path / "a" / "home").mkdir(parents=True)
    (tmp_path / "b" / "home").mkdir(parents=True)
    monkeypatch.setenv("AISQUARE_HOME", "home")
    monkeypatch.chdir(tmp_path / "a")
    ci_client.reset_cache()
    credentials.store(**{iam.KEY_API_URL: "https://api.test", iam.KEY_TOKEN: TOKEN})
    assert ci_client.api_key_and_source() == (TOKEN, ci_client.SIGNED_IN_SOURCE)
    monkeypatch.chdir(tmp_path / "b")
    assert ci_client.api_key_and_source() == ("", ""), "b/home has no session; a/home's is not it"
    assert os.path.exists(tmp_path / "a" / "home" / "credentials")


def test_an_email_shaped_subject_is_not_presented_as_the_users_email(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """`signed_in_display` keeps email, subject and source apart; the doctor
    lines branch on the email being known rather than on a substring test of
    whatever string came back (round 8)."""
    from aisquare.core import credentials
    from aisquare.services import diagnostics, iam

    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    monkeypatch.delenv("AISQUARE_TOKEN", raising=False)
    credentials.store(
        **{iam.KEY_API_URL: "https://api.test", iam.KEY_TOKEN: TOKEN, iam.KEY_SUB: "user@tenant"}
    )
    ci_client.reset_cache()

    assert ci_client.signed_in_display() == ("", "user@tenant", "file")
    assert diagnostics._signed_in_as("aisquare-idp:x") == "signed in (aisquare-idp:x)"
    note = diagnostics._bearer_note(ci_client.SIGNED_IN_SOURCE)
    assert note == "signed in (user@tenant) (aisquare login)"

    credentials.store(**{iam.KEY_EMAIL: "dev@example.com"})
    assert diagnostics._signed_in_as("aisquare-idp:x") == "signed in as dev@example.com"


def test_the_recall_predicate_resolves_the_project_from_the_servers_own_cwd(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, tmp_path: Path
) -> None:
    """available() and forward_recall take no cwd, because an MCP tool call
    carries none: the server process's working directory is the resolution for
    registration and for every pull, and a server started in another checkout
    binds to that checkout's project. Round 3 gave both a defaulted `cwd` that
    no production caller supplied; the honest shape is pinned here (round 4)."""
    import inspect

    from aisquare.services import ci_recall

    assert "cwd" not in inspect.signature(ci_recall.available).parameters
    assert "cwd" not in inspect.signature(ci_recall.forward_recall).parameters

    signed_in(monkeypatch, stub)
    here = tmp_path / "here"
    there = tmp_path / "there"
    here.mkdir()
    there.mkdir()
    config = AppConfig()
    config.experiment.enabled = True
    config.experiment.bindings[workspace_core.current_project(here).id] = TEAM
    save_config(config)
    ci_client.reset_cache()

    monkeypatch.chdir(here)
    assert ci_recall.available() is True
    monkeypatch.chdir(there)
    assert ci_recall.available() is False, "the other checkout has no binding"


def test_the_transport_rule_has_one_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """ci_client defers to iam.safe_transport rather than carrying a third copy
    of the https-or-loopback rule that also gates the sign-in itself."""
    from aisquare.services import iam

    calls: list[str] = []
    real = iam.safe_transport

    def spy(url: str) -> bool:
        calls.append(url)
        return real(url)

    monkeypatch.setattr(iam, "safe_transport", spy)

    assert ci_client.signed_in_allowed("https://ci.aisquare.studio") is True
    assert ci_client.signed_in_allowed("http://ci.internal") is False
    assert calls == ["https://ci.aisquare.studio", "http://ci.internal"]
    assert ci_client.signed_in_allowed("") is True


def test_bind_and_the_hooks_agree_on_the_project_even_under_a_stale_pin(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, runner: CliRunner
) -> None:
    """A pin left by `project switch` for a project since unregistered must not
    file the binding under an id no hook reads: both sides resolve through
    active_project, which honours the pin only while it still resolves."""
    from aisquare.core.store import store_session
    from aisquare.core.workspace import active_project

    signed_in(monkeypatch, stub)
    paths.ensure_home()
    workspace_core.pin_project("prj_stale_pin_never_registered")

    result = _run(runner, TEAM)
    assert result.exit_code == 0, _text(result)

    with store_session() as store:
        hooks_project = active_project(store).id
    assert load_config().experiment.bindings == {hooks_project: TEAM}
    assert ci_client.workspace_id(hooks_project) == TEAM
    assert "ci workspace" in ci_checks() and ci_checks()["ci workspace"].status is CheckStatus.ok


def test_the_recall_tool_sees_the_projects_binding(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, tmp_path: Path
) -> None:
    """The MCP recall path used to call gate() with no project, so a bound
    multi-workspace developer never got the tool registered while the hooks
    worked - the binding looked correct everywhere doctor looked."""
    from aisquare.services import ci_recall

    signed_in(monkeypatch, stub)
    monkeypatch.chdir(tmp_path)
    assert ci_recall.available() is False, "two workspaces, none bound"

    bind(TEAM)

    assert ci_recall.available() is True
    assert stub.me_fetches >= 1
