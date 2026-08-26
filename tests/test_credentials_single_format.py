"""Two writers, one file, and until now two formats that erased each other.

``~/.aisquare/credentials`` had exactly two users and they disagreed:
``init --api-key`` did ``write_text(api_key)`` — a whole-file replace with a
bare string — and ``serve_token()`` did ``json.loads`` with a ``{}`` fallback on
``JSONDecodeError``. Measured in temp homes, both directions destroy:

* serve then ``init --api-key`` → the bearer token is gone.
* ``init --api-key`` then serve → the API key is gone, because the decode error
  is read as "no data" rather than as "someone else owns this file".

Reachability, established before fixing: ``serve_token`` is reached by
``aisquare serve --show-token`` and by ``run_http``, and with the mcp extra
present that command exits 0 and writes the file. Live, not latent.

JSON is the format both now use, because it is the one that can hold two names.
A file already containing a bare key is MIGRATED into ``api_key`` rather than
discarded — the old failure was precisely "unparseable therefore empty", and a
fix that kept that reading would have preserved the bug for existing files.

Both go through one helper. Two writers agreeing today by careful editing is
what produced this; a single read-merge-write is what stops it recurring.

No fixture here is credential-shaped: values are assembled at import time from
obviously synthetic parts.
"""

from __future__ import annotations

import json
import stat

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import credentials, paths

# Assembled rather than written as a literal, so nothing here can be mistaken
# for a real key by a scanner or a reader.
_FAKE_KEY = "-".join(["not", "a", "real", "key", "fixture"])
_LEGACY_KEY = "-".join(["legacy", "bare", "value"])


def test_serve_then_init_keeps_both(runner: CliRunner) -> None:
    """Direction one: the bearer token used to be erased by a later init."""
    token = credentials.load_all()  # touch nothing yet
    assert token == {}

    from aisquare.services import mcp_server

    issued = mcp_server.serve_token()
    runner.invoke(app, ["init", "--yes", "--api-key", _FAKE_KEY], catch_exceptions=False)

    stored = credentials.load_all()
    assert stored.get("serve_token") == issued, "init destroyed the bearer token"
    assert stored.get("api_key") == _FAKE_KEY


def test_init_then_serve_keeps_both(runner: CliRunner) -> None:
    """Direction two, and the worse one: a typed API key erased silently."""
    from aisquare.services import mcp_server

    runner.invoke(app, ["init", "--yes", "--api-key", _FAKE_KEY], catch_exceptions=False)
    issued = mcp_server.serve_token()

    stored = credentials.load_all()
    assert stored.get("api_key") == _FAKE_KEY, "serve destroyed the API key"
    assert stored.get("serve_token") == issued


def test_a_legacy_bare_file_is_migrated_not_discarded(runner: CliRunner) -> None:
    """The half a naive fix would miss.

    Every machine that ran `init --api-key` before this change has a bare file.
    Treating it as unparseable — which is what the old code did — would throw
    the key away on the first serve, which is the very bug being fixed.
    """
    paths.ensure_home()
    paths.credentials_path().write_text(_LEGACY_KEY, encoding="utf-8")

    from aisquare.services import mcp_server

    issued = mcp_server.serve_token()

    stored = credentials.load_all()
    assert stored.get("api_key") == _LEGACY_KEY, "the legacy bare key was discarded"
    assert stored.get("serve_token") == issued


def test_the_file_stays_owner_only(runner: CliRunner) -> None:
    """0600 was never the defect and must not become one."""
    runner.invoke(app, ["init", "--yes", "--api-key", _FAKE_KEY], catch_exceptions=False)

    mode = stat.S_IMODE(paths.credentials_path().stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_the_token_is_stable_across_calls(runner: CliRunner) -> None:
    """A merge that rewrote the token every time would be a new defect."""
    from aisquare.services import mcp_server

    first = mcp_server.serve_token()
    second = mcp_server.serve_token()

    assert first == second


@pytest.mark.parametrize("junk", ["", "   ", "{not json", "[]"])
def test_unusable_content_never_crashes_a_caller(runner: CliRunner, junk: str) -> None:
    """Reading credentials must not raise, whatever is in the file.

    A file this shared home may hold anything; the callers are `init` and
    `serve`, and neither should die on it. Empty and whitespace carry no key to
    migrate, so they are simply absent rather than stored as one.
    """
    paths.ensure_home()
    paths.credentials_path().write_text(junk, encoding="utf-8")

    stored = credentials.load_all()

    assert isinstance(stored, dict)
    assert "api_key" not in stored or stored["api_key"].strip()


def test_written_content_is_json(runner: CliRunner) -> None:
    """One format, stated as a property rather than left to the two writers."""
    runner.invoke(app, ["init", "--yes", "--api-key", _FAKE_KEY], catch_exceptions=False)

    parsed = json.loads(paths.credentials_path().read_text(encoding="utf-8"))
    assert parsed["api_key"] == _FAKE_KEY
