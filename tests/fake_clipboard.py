"""A fake native clipboard tool on PATH, which records every write it is given.

The suite's ``no_native_clipboard`` fixture keeps every copy to OSC 52, to a terminal
it calls one that takes it; :func:`install` puts the real ``core.clipboard``
``local_writer`` and ``osc52_terminal`` back, with a fake ``pbcopy`` (or any other
tool's name) first on PATH and ``sys.platform`` reading ``darwin`` unless told
otherwise. The tool is a real process: ``/bin/sh`` handing
its stdin to this interpreter, which appends the bytes and its argv to
``calls.jsonl``, in the order the writes finished. What it does with each write is
:attr:`FakeClipboard.behaviour`, read per call, so a test can change it between
copies.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from aisquare.core import clipboard

REAL_LOCAL_WRITER = clipboard.local_writer
"""Read at import, before any test's ``no_native_clipboard`` replaced it — so this
module is imported at the top of a test module, never inside a test (:func:`install`
says so when it was)."""
REAL_OSC52_TERMINAL = clipboard.osc52_terminal
"""The same, for the classifier the fixture answers "accepts" for."""

NEEDS_POSIX = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake tool is a /bin/sh script on PATH"
)

TEXT = "héllo ✓ 日本"
"""Outside ASCII, outside Latin-1 and two cells wide: a tool reading the wrong encoding
mangles it."""

_TOOL = r"""
import json, os, sys, time
root = sys.argv[1]
with open(os.path.join(root, "started.log"), "a", encoding="utf-8") as started:
    started.write("started\n")
data = sys.stdin.buffer.read()
behaviour = open(os.path.join(root, "behaviour"), encoding="utf-8").read().split()
if "hang" in behaviour:
    time.sleep(30)
if "fail" in behaviour:
    sys.exit(3)
if "slow-first" in behaviour and data == "first".encode():
    time.sleep(0.6)
with open(os.path.join(root, "calls.jsonl"), "a", encoding="utf-8") as calls:
    calls.write(json.dumps({
        "argv": sys.argv[2:], "data": data.hex(), "lc_all": os.environ.get("LC_ALL"),
    }) + "\n")
"""


@dataclass
class FakeClipboard:
    root: Path
    name: str

    @property
    def behaviour(self) -> str:
        return (self.root / "behaviour").read_text(encoding="utf-8")

    @behaviour.setter
    def behaviour(self, words: str) -> None:
        (self.root / "behaviour").write_text(words, encoding="utf-8")

    def calls(self) -> list[dict[str, object]]:
        log = self.root / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def started(self) -> int:
        """How many times the tool has been run, finished or not."""
        log = self.root / "started.log"
        return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0

    def payloads(self) -> list[bytes]:
        return [bytes.fromhex(str(call["data"])) for call in self.calls()]

    def texts(self, encoding: str = "utf-8") -> list[str]:
        """Each write's text, in the order the writes finished."""
        return [payload.decode(encoding) for payload in self.payloads()]

    @property
    def clipboard(self) -> str | None:
        """What the clipboard holds: the last write's text, ``None`` before any."""
        texts = self.texts()
        return texts[-1] if texts else None


def install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str = "pbcopy",
    *,
    platform: str = "darwin",
    behaviour: str = "",
    only: bool = False,
) -> FakeClipboard:
    """A fake ``name`` on PATH, and the native route and the real terminal switched back on.

    ``only`` makes it the only directory on PATH, so a real tool of that name — a Mac's
    own ``pbcopy`` — cannot be found behind it. SSH's variables are cleared: the suite
    may run over SSH, and the native route stands down there.
    """
    assert REAL_LOCAL_WRITER.__module__ == REAL_OSC52_TERMINAL.__module__ == clipboard.__name__, (
        "tests.fake_clipboard was first imported inside a test, after the suite's stand-in"
        " replaced local_writer: import it at the top of the test module"
    )
    root = tmp_path / f"fake-{name}"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    (root / "tool.py").write_text(_TOOL, encoding="utf-8")
    tool = bin_dir / name
    tool.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} -I {shlex.quote(str(root / 'tool.py'))}"
        f' {shlex.quote(str(root))} "$@"\n',
        encoding="utf-8",
    )
    tool.chmod(0o755)
    fake = FakeClipboard(root, name)
    fake.behaviour = behaviour
    path = str(bin_dir) if only else str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", path)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(clipboard, "local_writer", REAL_LOCAL_WRITER)
    monkeypatch.setattr(clipboard, "osc52_terminal", REAL_OSC52_TERMINAL)
    return fake
