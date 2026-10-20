"""Fake executables a test can put on PATH, findable on either platform.

The PATHEXT lesson, in one place. Three fixtures learned it separately and the
third learned it after the first had already been fixed:

* ``test_brain.py::fake_gbrain`` — the original, and the one that recorded why;
* ``test_tmux.py::fake_bin`` — ``TmuxServer.binary()`` is ``shutil.which(...)``,
  so an extensionless file reported "tmux is not installed" about a file sitting
  right where the caller named it;
* ``test_fleet_service.py::claude_on_path`` — ``fleet.spawn`` gates on
  ``shutil.which(resolution.binary)``, so 81 tests failed with "'claude' is not
  on your PATH" against a file that was there.

WHAT THE PLATFORMS DISAGREE ABOUT, since it is the whole reason this exists:

* POSIX resolves a bare name on PATH and runs it through its shebang, so an
  extensionless file with the execute bits set is a program.
* Windows resolves through **PATHEXT**. A file with no extension is not a
  program there, so ``shutil.which("claude")`` does not find it, and
  ``CreateProcess`` would not run it if it did — which is why a ``.cmd`` is the
  shape pip and npm use for interpreted scripts.

AND WHAT THEY DISAGREE ABOUT MORE QUIETLY: the exit status. ``read line``
returns non-zero at EOF and ``exit 0`` is what masks it; the ``.cmd`` equivalent
``set /p`` sets errorlevel 1 on empty stdin, and a batch file with no explicit
``exit /b`` exits with the errorlevel of its last command. A fake that exits 0
on POSIX and 1 on Windows for the same stdin is a difference nobody writes down
and everybody debugs. :func:`executable_fake` always ends the Windows body with
``exit /b <code>``.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def executable_fake(
    directory: Path,
    name: str,
    *,
    posix: str,
    windows: str,
    exit_code: int | None = 0,
) -> Path:
    """Write a fake ``name`` into ``directory`` that ``shutil.which`` can find.

    ``posix`` and ``windows`` are the BODIES — no shebang, no ``@echo off``, no
    trailing exit; this adds each platform's wrapper so a caller cannot forget
    the half that only bites on the platform they are not using.

    ``exit_code=None`` appends no exit line, so the fake exits with whatever its
    last command did. That is what a LAUNCHER wants — one that runs a real
    interpreter and must report the interpreter's status — and forcing ``exit
    /b 0`` on it would mask every failure the caller is trying to observe.

    Returns the path actually written, which carries ``.cmd`` on Windows. Every
    caller must use the RETURN VALUE rather than the name it passed in, or the
    command it goes on to build will name a file that does not exist.

    That includes callers deriving a SIBLING from it — ``path.with_name("x")``
    silently drops the extension this function added, and the result is not a
    program on Windows. Use ``with_name("x" + path.suffix)``. A real test made
    exactly that mistake and only CI caught it, because the case skips on a
    machine without tmux.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path = directory / f"{name}.cmd"
        body = windows.replace("\n", "\r\n").rstrip("\r\n")
        tail = "" if exit_code is None else f"exit /b {exit_code}\r\n"
        path.write_text(f"@echo off\r\n{body}\r\n{tail}", encoding="utf-8")
        return path
    path = directory / name
    tail = "" if exit_code is None else f"exit {exit_code}\n"
    path.write_text(f"#!/bin/sh\n{posix.rstrip()}\n{tail}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def prepend_to_path(directory: Path, monkeypatch: object) -> None:
    """Put ``directory`` first on PATH, keeping the inherited entries.

    Prepend rather than replace: the fake must win, but ``git`` and friends have
    to stay reachable, and a hardcoded ``/usr/bin:/bin`` is not portable — on
    Windows it names nothing at all.
    """
    setenv = monkeypatch.setenv  # type: ignore[attr-defined]
    setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
