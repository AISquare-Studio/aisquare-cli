"""Remote works out of the box (#34): nothing to install, the ngrok token pasted once.

The owner installed main on a new machine and Remote failed for missing pieces: its Python
dependencies were the ``remote`` extra, which ``install.sh`` installs none of, and the ngrok
agent and its token were "the human's job". Now the four are core, ngrok is the bundled SDK
(``services.ngrok_sdk``), and the token is pasted in Settings → Remote.
"""

from __future__ import annotations

import inspect
import json
import ntpath
import os
import posixpath
import re
import stat
import subprocess
import sys
import textwrap
import threading
import time
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement

from aisquare.cli.ui import remote_control
from aisquare.cli.ui.remote_control import RemoteController
from aisquare.services import ngrok_sdk, ngrok_tunnel
from aisquare.services.ngrok_sdk import (
    BAD_TOKEN,
    RECONNECT_SECONDS,
    REFUSED_TOKEN,
    SDK_MISSING,
    SESSION_LOST,
    SdkTunnel,
    bundled_ngrok,
    check_token,
    find_authtoken,
    save_token,
    saved_token,
    token_path,
)
from aisquare.services.ngrok_tunnel import AUTHTOKEN_HINT, INSTALL_HINT, STOP_SECONDS, NgrokTunnel
from tests.fake_ngrok_sdk import Script, fake_sdk
from tests.test_remote_control import fake_server

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "2abcDEF_0123456789abcdefghijklmnopqrstuvwxyzAB"
NOT_ON_THE_NORMAL_PATH = ("pip install", "pipx", "uv ", "[remote]", "not installed", "ngrok config")
"""What Remote never tells the owner to do on the normal path (the owner, 2026-10-23)."""


def bundled(monkeypatch: pytest.MonkeyPatch, script: Script) -> Script:
    """The bundled ngrok, answering as ``script`` says."""
    monkeypatch.setitem(sys.modules, "ngrok", fake_sdk(script))
    return script


def hinted(*sentences: str | None) -> list[str]:
    return [hint for said in sentences if said for hint in NOT_ON_THE_NORMAL_PATH if hint in said]


# --- 1. the core dependencies alone -------------------------------------------------------


def _core_closure() -> set[str]:
    """Every distribution a plain ``pip install aisquare-cli`` brings, here: pyproject's own
    ``[project].dependencies`` (not the installed metadata, which an edit does not change
    until a reinstall), and theirs, transitively, with no extra."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pending = [Requirement(line) for line in declared["project"]["dependencies"]]
    found: set[str] = set()
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = re.sub(r"[-_.]+", "-", requirement.name).lower()
        if name in found:
            continue
        found.add(name)
        pending += [Requirement(line) for line in metadata.requires(name) or []]
    return found


def _installed(origin: str, site_packages: list[str], stdlib: list[str], pathmod: Any) -> bool:
    """Whether ``origin`` is a module some distribution installed: under a site-packages
    directory that holds none of the stdlib (``stdlib``: sysconfig's ``stdlib`` and
    ``platstdlib``). On Windows, a Python that is no venv lists ``sys.prefix`` itself among
    ``site.getsitepackages()``, and its stdlib is ``<prefix>\\Lib``: taken as an installed
    directory, every stdlib module read as one no core distribution installed (#51).
    ``pathmod`` is ``os.path``; a test hands in ``ntpath`` for a Windows-shaped run.
    Embedded in the server test's subprocess, so it imports nothing."""

    def norm(path: str) -> str:
        return str(pathmod.normcase(pathmod.normpath(path)))

    def under(child: str, parent: str) -> bool:
        return child == parent or child.startswith(parent.rstrip(pathmod.sep) + pathmod.sep)

    libs = [norm(lib) for lib in stdlib]
    dirs = [norm(d) for d in site_packages]
    where = norm(origin) if origin else ""
    return any(under(where, d) for d in dirs if not any(under(lib, d) for lib in libs))


_ONLY_CORE = """
import json, os, site, sys, sysconfig, urllib.request
from typing import Any
from importlib.machinery import PathFinder
allowed = set(json.loads(sys.argv[1]))
site_packages = site.getsitepackages()
stdlib = [sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib")]
refused = []

{installed}

class OnlyCore:
    # Refuses a module installed in site-packages by no core distribution.

    def find_spec(self, name, path=None, target=None):
        top = name.partition(".")[0]
        if top in allowed or top == "aisquare" or "." in name:
            return None
        spec = PathFinder.find_spec(name, path)
        origin = (spec and spec.origin) or ""
        if not _installed(origin, site_packages, stdlib, os.path):
            return None  # the stdlib's, a builtin, or nobody's
        refused.append(top)
        raise ModuleNotFoundError(f"{{top}} is no core dependency", name=top)

sys.meta_path.insert(0, OnlyCore())
"""


def _run_on_core_alone(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    """``body`` in a process that can import no module outside a plain install's
    distributions (:data:`_ONLY_CORE`); ``sys.argv[2]`` is a free port."""
    from tests.test_remote_install_page import free_port

    allowed = sorted(
        top
        for top, distributions in metadata.packages_distributions().items()
        if {re.sub(r"[-_.]+", "-", d).lower() for d in distributions} & _core_closure()
    )
    script = _ONLY_CORE.format(installed=inspect.getsource(_installed)) + textwrap.dedent(body)
    env = {**os.environ, "AISQUARE_HOME": str(tmp_path / "home"), "NO_PROXY": "*"}
    return subprocess.run(
        [sys.executable, "-P", "-c", script, json.dumps(allowed), str(free_port())],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=tmp_path,
    )


def test_the_remote_server_imports_and_starts_on_the_core_dependencies_alone(
    tmp_path: Path,
) -> None:
    """In a process that can import no module outside a plain install's distributions (the
    dev extra's mcp brings starlette and uvicorn too, so this env alone proves nothing), the
    real server starts and serves the page, and the bundled ngrok imports."""
    done = _run_on_core_alone(
        tmp_path,
        """
        from aisquare.services import ngrok_sdk, remote_server

        assert ngrok_sdk.sdk() is not None, "the bundled ngrok"
        info = remote_server.start_remote_server(port=int(sys.argv[2]))
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(info.url_local, timeout=10) as response:
                print(json.dumps({"status": response.status, "refused": sorted(set(refused))}))
        finally:
            remote_server.stop_remote_server()
        """,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    said = json.loads(done.stdout.strip().splitlines()[-1])
    assert said["status"] == 200, said


def test_the_core_only_process_still_refuses_a_dev_only_package(tmp_path: Path) -> None:
    """#51 narrowed what counts as installed: the guard above still bites. ``pytest`` is the
    dev extra's, in this env's site-packages, and refused; the stdlib still imports."""
    assert "pytest" not in _core_closure()
    done = _run_on_core_alone(tmp_path, "import __future__, textwrap\nimport pytest\n")
    assert done.returncode != 0
    assert "pytest is no core dependency" in done.stderr, done.stderr[-3000:]


@pytest.mark.parametrize(
    ("origin", "installed"),
    [
        (r"C:\hostedtoolcache\windows\Python\3.12.10\x64\Lib\__future__.py", False),
        (r"C:\hostedtoolcache\windows\Python\3.12.10\x64\DLLs\_socket.pyd", False),
        (r"c:\HOSTEDTOOLCACHE\windows\python\3.12.10\x64\lib\json\__init__.py", False),
        (r"C:\hostedtoolcache\windows\Python\3.12.10\x64\Lib\site-packages\mcp\__init__.py", True),
        (r"C:\hostedtoolcache\windows\Python\3.12.10\x64\lib\SITE-PACKAGES\yaml\__init__.py", True),
        ("", False),
    ],
)
def test_a_windows_pythons_own_prefix_is_not_an_installed_directory(
    origin: str, installed: bool
) -> None:
    """#51: Windows CI's Python (no venv) lists ``sys.prefix`` among its site directories,
    and its stdlib lives below it: ``__future__`` was refused as no core dependency. The
    prefix holds the stdlib, so it is not one; its ``Lib\\site-packages`` is, in any case."""
    prefix = r"C:\hostedtoolcache\windows\Python\3.12.10\x64"
    site_packages = [prefix, prefix + r"\Lib\site-packages"]
    stdlib = [prefix + r"\Lib", prefix + r"\Lib"]
    assert _installed(origin, site_packages, stdlib, ntpath) is installed


def test_a_posix_venvs_site_packages_is_installed_and_its_stdlib_is_not() -> None:
    site_packages = ["/repo/.venv/lib/python3.13/site-packages"]
    stdlib = ["/usr/lib/python3.13", "/repo/.venv/lib/python3.13"]
    assert _installed(
        "/repo/.venv/lib/python3.13/site-packages/mcp/__init__.py", site_packages, stdlib, posixpath
    )
    assert not _installed("/usr/lib/python3.13/__future__.py", site_packages, stdlib, posixpath)


# --- the bundled ngrok --------------------------------------------------------------------


def test_the_bundled_tunnel_signs_in_with_the_token_and_forwards_to_remotes_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = bundled(monkeypatch, Script(url="https://abc.ngrok-free.app"))
    monkeypatch.setenv("NGROK_AUTHTOKEN", TOKEN)
    monkeypatch.setenv("AISQUARE_REMOTE_NGROK_URL", "https://mine.ngrok-free.app/")
    announced: list[str] = []
    tunnel = bundled_ngrok(8750)
    assert isinstance(tunnel, SdkTunnel)
    tunnel.on_announce = announced.append
    assert tunnel.start_tunnel() is None
    assert tunnel.wait_for_url(5) == "https://abc.ngrok-free.app"
    assert tunnel.running and announced == ["https://abc.ngrok-free.app"]
    assert script.calls.tokens == [TOKEN]
    assert script.calls.domains == ["mine.ngrok-free.app"]
    assert script.calls.forwards == ["http://127.0.0.1:8750"]
    assert tunnel.api_warning is None, "the SDK has no local agent API"
    tunnel.stop_tunnel()
    assert not tunnel.running and script.calls.closed == 1


def test_with_no_token_the_tunnel_says_to_paste_one_in_settings_and_signs_nothing_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = bundled(monkeypatch, Script())
    tunnel = SdkTunnel(8750)
    assert tunnel.start_tunnel() == AUTHTOKEN_HINT == tunnel.error
    assert "Settings → Remote" in AUTHTOKEN_HINT
    assert script.calls.tokens == [] and not tunnel.running


def test_a_token_ngrok_refuses_is_said_in_plain_words_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundled(monkeypatch, Script(refuse="ERR_NGROK_107"))
    tunnel = SdkTunnel(8750, token=lambda: TOKEN)
    assert tunnel.start_tunnel() is None
    assert tunnel.wait_for_url(5) is None
    deadline = time.monotonic() + 5
    while tunnel.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert tunnel.error == BAD_TOKEN and TOKEN not in BAD_TOKEN
    assert BAD_TOKEN in remote_control.LASTING_TUNNEL_ERRORS, "not retried every minute"


def ended(tunnel: SdkTunnel, within: float = 5.0) -> bool:
    """Whether the tunnel's thread ends within ``within`` seconds."""
    deadline = time.monotonic() + within
    while tunnel.running and time.monotonic() < deadline:
        time.sleep(0.01)
    return not tunnel.running


def test_a_session_that_cannot_sign_in_again_ends_for_the_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #274, m1: the SDK signs in again by itself after ngrok's service let the session
    go, for ever, with the old token, and its sentence for a sign-in that failed is only
    "reconnect error". A drop it rides out keeps the tunnel; one it cannot sign in again
    after for :data:`RECONNECT_SECONDS` ends it, so the watchdog starts a new one."""
    script = bundled(monkeypatch, Script(url="https://abc.ngrok-free.app"))
    clock = [100.0]
    tunnel = SdkTunnel(8750, token=lambda: TOKEN, ticks=lambda: clock[0])
    assert tunnel.start_tunnel() is None
    assert tunnel.wait_for_url(5) == "https://abc.ngrok-free.app"
    script.disconnect("transport error")  # the network dropped: the SDK signs in again
    clock[0] += 5
    script.disconnect("reconnect error")
    clock[0] += 60
    script.disconnect("transport error")  # signed in again meanwhile, and dropped anew
    clock[0] += RECONNECT_SECONDS - 1
    script.disconnect("reconnect error")
    assert tunnel.running and tunnel.error is None, "a drop the SDK rides out is its own"
    clock[0] += 1
    script.disconnect("reconnect error")
    assert ended(tunnel), "a session that cannot sign in again ends"
    assert tunnel.error == SESSION_LOST and script.calls.closed == 1


def test_a_session_ngrok_says_the_token_of_is_refused_ends_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = bundled(monkeypatch, Script(url="https://abc.ngrok-free.app"))
    tunnel = SdkTunnel(8750, token=lambda: TOKEN)
    assert tunnel.start_tunnel() is None
    assert tunnel.wait_for_url(5) is not None
    script.disconnect("listener closed: The authtoken you specified has been reset, ERR_NGROK_107")
    assert ended(tunnel)
    assert tunnel.error == BAD_TOKEN


def test_a_token_reset_mid_session_is_said_and_the_next_start_signs_in_with_the_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The controller's side of m1: the session the SDK cannot sign in again ends, the
    watchdog starts a new tunnel, whose sign-in says why (ngrok's code is in it), and once a
    new token is pasted in Settings (:meth:`RemoteController.token_saved`) the tunnel comes
    back signed in with that one, at once."""
    script = bundled(monkeypatch, Script(url="https://abc.ngrok-free.app"))
    clock = [100.0]
    token = [TOKEN]
    made: list[SdkTunnel] = []

    def factory(port: int) -> SdkTunnel:
        made.append(SdkTunnel(port, token=lambda: token[0], ticks=lambda: clock[0]))
        return made[-1]

    controller = RemoteController(server=fake_server(), tunnel_factory=factory, url_timeout=5)
    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(5)
    assert controller.public_origin == "https://abc.ngrok-free.app"
    script.refuse = "ERR_NGROK_107"  # the token is reset on ngrok's dashboard
    script.disconnect("transport error")
    clock[0] += RECONNECT_SECONDS
    script.disconnect("reconnect error")
    assert ended(made[0])
    assert controller.revive_tunnel_if_dead() is True, "the watchdog acts"
    controller._waiter.join(5)
    assert controller.status_line() == BAD_TOKEN, "the status line says why"
    assert controller.tunnel_error() == BAD_TOKEN, "Settings offers the token field"
    token[0], script.refuse = "2newTOKEN_9876543210", None
    assert controller.token_saved() is True, "a new token starts the tunnel at once"
    controller._waiter.join(5)
    assert script.calls.tokens[-1] == "2newTOKEN_9876543210"
    assert controller.public_origin == "https://abc.ngrok-free.app"
    assert controller.tunnel_error() is None
    assert controller.status_line() == "ngrok stopped — restarted it", "on the same link"
    controller.turn_off()


def test_a_stop_during_the_sign_in_cancels_it_and_binds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #274, m2: a stop during the sign-in (up to 20 s) was waited for only until
    :data:`STOP_SECONDS`; the sign-in went on, then bound the endpoint, so a quick off and on
    met it (ERR_NGROK_108, the domain in use). The sign-in is cancelled: the real SDK closes
    its socket then (a cancelled ``connect()`` drops its future)."""
    script = bundled(monkeypatch, Script(hold=threading.Event()))
    tunnel = SdkTunnel(8750, token=lambda: TOKEN)
    assert tunnel.start_tunnel() is None
    assert script.signing_in.wait(5)
    began = time.monotonic()
    tunnel.stop_tunnel()
    took = time.monotonic() - began
    assert not tunnel.running and took < STOP_SECONDS / 2, took
    assert script.calls.cancelled == 1
    assert script.hold is not None
    script.hold.set()  # a sign-in that went on would answer now
    time.sleep(0.2)
    assert script.calls.forwards == [] and tunnel.public_url is None
    assert tunnel.error is None, "a stop is no failure"


def test_any_other_failure_is_ngroks_own_words_with_the_token_cut_out() -> None:
    error = ValueError("failed to connect session", f"bad thing near {TOKEN}", "ERR_NGROK_334")
    said = ngrok_sdk.failure_sentence(error, TOKEN)
    assert TOKEN not in said and "ERR_NGROK_334" in said and said.startswith("ngrok: ")


def test_an_install_without_the_sdk_falls_back_to_an_ngrok_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "ngrok", None)  # `import ngrok` raises
    assert ngrok_sdk.sdk() is None
    tunnel = bundled_ngrok(8750)
    assert isinstance(tunnel, NgrokTunnel) and tunnel.api_off
    assert SdkTunnel(8750).start_tunnel() == SDK_MISSING
    assert not hinted(SDK_MISSING, INSTALL_HINT)


def test_the_ngrok_on_path_signs_in_with_the_token_pasted_in_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_token(TOKEN)
    seen: list[dict[str, str] | None] = []

    def popen(command: list[str], **options: object) -> subprocess.Popen[str]:
        seen.append(options.get("env"))  # type: ignore[arg-type]
        raise OSError("not started")

    tunnel = NgrokTunnel(8750, which=lambda _name: "/bin/ngrok", popen=popen)
    tunnel.start_tunnel()
    assert seen and seen[0] is not None and seen[0]["NGROK_AUTHTOKEN"] == TOKEN
    monkeypatch.setenv("NGROK_AUTHTOKEN", "theirs")
    NgrokTunnel(8750, which=lambda _name: "/bin/ngrok", popen=popen).start_tunnel()
    assert seen[1] is None, "an exported token is theirs to keep"


# --- the token ----------------------------------------------------------------------------


def test_a_saved_token_is_kept_owner_only_and_found_first_after_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    theirs = tmp_path / "ngrok.yml"
    theirs.write_text('version: "3"\nagent:\n  authtoken: their-own\n', encoding="utf-8")
    assert find_authtoken({}, [theirs]) == "their-own", "an ngrok signed in keeps working"
    save_token(f"  {TOKEN}\n")
    assert saved_token() == TOKEN
    assert find_authtoken({}, [theirs]) == TOKEN
    assert find_authtoken({"NGROK_AUTHTOKEN": "exported"}, [theirs]) == "exported"
    if sys.platform != "win32":
        assert stat.S_IMODE(token_path().stat().st_mode) == 0o600
    assert token_path().is_relative_to(Path(os.environ["AISQUARE_HOME"]))


@pytest.mark.parametrize(
    ("config", "token"),
    [
        pytest.param('version: "2"\nauthtoken: two\n', "two", id="v2"),
        pytest.param("﻿version: '3'\nagent:\n  authtoken: three\n", "three", id="v3, a BOM"),
        pytest.param("agent: [not, a, map]\n", None, id="no map"),
        pytest.param(": : :\n", None, id="no yaml"),
    ],
)
def test_their_own_ngrok_config_is_read_as_ngrok_writes_it(
    tmp_path: Path, config: str, token: str | None
) -> None:
    path = tmp_path / "ngrok.yml"
    path.write_text(config, encoding="utf-8")
    assert find_authtoken({}, [path]) == token


def test_the_suite_never_reads_the_developers_own_ngrok_config() -> None:
    """``conftest.no_real_ngrok``: the two configs a tunnel's start reads are the test's."""
    home = Path(os.path.expanduser("~"))
    for config in ngrok_sdk.their_configs():
        assert not config.is_relative_to(home) or "ngrok-home" in str(config), config


@pytest.mark.parametrize("code", sorted(ngrok_sdk.TOKEN_CODES))
def test_check_token_refuses_what_ngrok_refuses(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    bundled(monkeypatch, Script(refuse=code))
    assert check_token(TOKEN) == REFUSED_TOKEN and TOKEN not in REFUSED_TOKEN


def test_check_token_takes_what_it_cannot_tell(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network, or the account's one session in use elsewhere: kept, and Remote's start
    says what ngrok says then."""
    script = bundled(monkeypatch, Script(refuse="ERR_NGROK_108"))
    assert check_token(TOKEN) is None and script.calls.tokens == [TOKEN]
    bundled(monkeypatch, Script())
    assert check_token(TOKEN) is None


# --- 4. the normal path says nothing to install -------------------------------------------


def test_the_normal_path_never_says_to_install_anything_or_run_ngrok_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh install: Turn on with no token, a bad token, a good one. Every sentence the
    controller says on the way, and the server's own refusals, name no install."""
    said: list[str | None] = []
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=bundled_ngrok)
    controller.on_news = lambda news, trouble: said.append(news)

    bundled(monkeypatch, Script())
    controller.turn_on()
    said += [controller.message, controller.status_line()]
    assert controller.message == AUTHTOKEN_HINT
    controller.turn_off()

    bundled(monkeypatch, Script(refuse="ERR_NGROK_105"))
    save_token("2bad")
    controller.turn_on()
    assert controller.tunnel is not None
    controller.tunnel.wait_for_url(5)
    deadline = time.monotonic() + 5
    while controller.message != BAD_TOKEN and time.monotonic() < deadline:
        time.sleep(0.01)
    said += [controller.message, controller.status_line()]
    assert controller.message == BAD_TOKEN
    controller.turn_off()

    script = bundled(monkeypatch, Script(url="https://abc.ngrok-free.app"))
    save_token(TOKEN)
    controller.turn_on()
    deadline = time.monotonic() + 5
    while controller.public_origin is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert controller.public_origin == "https://abc.ngrok-free.app"
    assert script.calls.tokens == [TOKEN]
    said += [controller.message, controller.status_line()]
    controller.turn_off()

    sentences = [*said, AUTHTOKEN_HINT, BAD_TOKEN, REFUSED_TOKEN]
    assert not hinted(*sentences), [s for s in sentences if hinted(s)]
    assert not [s for s in said if (s and "2bad" in s) or (s and TOKEN in s)], "the token is said"


def test_the_bundled_ngrok_is_the_controllers_default() -> None:
    import inspect

    default = inspect.signature(RemoteController).parameters["tunnel_factory"].default
    assert default is bundled_ngrok
    assert ngrok_tunnel.AUTHTOKEN_FROM in AUTHTOKEN_HINT


# --- delta review of #274 (#40), m4: a token saved while ngrok signs in -------------------------


def test_a_token_saved_while_ngrok_signs_in_replaces_that_sign_in_with_the_new_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delta review of #274, m4: Save during a sign-in (up to 20 s) found the tunnel running
    and left it be. It signed in with the old token, ngrok refused it, and it ended as a first
    tunnel with a lasting error: nothing retried it, and the slot asked again for the token
    Save had just checked. The sign-in is cancelled and the new token's starts at once."""
    script = bundled(monkeypatch, Script(url="https://abc.ngrok-free.app", hold=threading.Event()))
    token = [TOKEN]
    made: list[SdkTunnel] = []

    def factory(port: int) -> SdkTunnel:
        made.append(SdkTunnel(port, token=lambda: token[0]))
        return made[-1]

    controller = RemoteController(server=fake_server(), tunnel_factory=factory, url_timeout=5)
    controller.turn_on()
    assert script.signing_in.wait(5), "the premise: the first tunnel is signing in"
    token[0] = "2newTOKEN_9876543210"
    assert controller.token_saved() is True, "the sign-in with the old token is replaced"
    assert ended(made[0]), "the old sign-in is cancelled"
    assert script.calls.cancelled == 1
    assert script.hold is not None
    script.hold.set()
    assert controller._waiter is not None
    controller._waiter.join(5)
    assert script.calls.tokens == [TOKEN, "2newTOKEN_9876543210"]
    assert controller.tunnel is made[1] and controller.public_origin == "https://abc.ngrok-free.app"
    assert controller.tunnel_error() is None, "the slot does not ask again"
    controller.turn_off()


def test_a_token_saved_under_a_tunnel_that_is_up_says_it_is_for_the_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """m4's second half: *Change ngrok token* under a tunnel that is up keeps its session
    (a restart would move the link of one with no static domain), and the status line says
    the new token waits for ngrok's next start, where it said nothing."""
    script = bundled(monkeypatch, Script(url="https://abc.ngrok-free.app"))
    made: list[SdkTunnel] = []

    def factory(port: int) -> SdkTunnel:
        made.append(SdkTunnel(port, token=lambda: TOKEN))
        return made[-1]

    controller = RemoteController(server=fake_server(), tunnel_factory=factory, url_timeout=5)
    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(5)
    assert controller.public_origin == "https://abc.ngrok-free.app"
    assert controller.token_saved() is False, "the session that is up is kept"
    assert len(made) == 1 and made[0].running and script.calls.closed == 0
    assert remote_control.TOKEN_AT_NEXT_START in controller.status_line()
    controller.turn_off()
