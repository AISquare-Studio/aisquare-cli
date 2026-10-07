"""The installer's outside world, closed for the tests that import this fixture.

``aisquare upgrade`` reaches PyPI and runs uv, ``aisquare uninstall`` hands
the process to ``uv tool uninstall``, and ``services.install_route`` is the one
module that does any of it. A test that forgot to replace one of its seams would
reinstall or remove the CLI running the suite, or wait on the network — so every
module that drives either command imports :func:`no_real_installer`, which
pytest then applies to each of its tests (an autouse fixture imported into a
test module is that module's fixture).

The seams are closed by RECORDING, not only by raising. The CLI catches what a
command raises (``CliRunner`` turns it into ``result.exception``), so a raise
inside a command could be swallowed by a test that only checks the output; the
record is checked when the fixture tears down, where nothing can swallow it.

Kept out of ``conftest.py`` on purpose: this release train does not edit it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from aisquare.services import install_route

#: Everything in ``install_route`` that leaves this process or reads ambient
#: machine state. ``open_url`` is listed beside ``fetch_latest`` so a test of the
#: real lookup still cannot reach the network unless it supplies a stand-in.
SEAMS = ("fetch_latest", "open_url", "run_installer", "run_captured", "exec_replace", "find_uv")


@pytest.fixture(autouse=True)
def no_real_installer(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every installer seam closed; the test overrides the ones it means to drive."""
    reached: list[str] = []

    def closed(name: str) -> Callable[..., Any]:
        def refuse(*_args: object, **_kwargs: object) -> Any:
            reached.append(name)
            raise AssertionError(f"a test reached the real install_route.{name}")

        return refuse

    for name in SEAMS:
        monkeypatch.setattr(install_route, name, closed(name))
    yield reached
    assert not reached, (
        f"the real installer seam(s) {reached} were reached — replace them in the test "
        "with monkeypatch before driving the command"
    )
