"""The suite's private tmux folders under ``/tmp``, and the servers a run leaves in them.

Every test gets a ``TMUX_TMPDIR`` of its own (``tests/conftest.py``'s ``no_real_fleet``),
and a few tests make another one beside it. Teardown ends every server in them, but a
run that is killed (a timeout, ``kill``, an xdist worker that dies) runs no teardown:
its servers outlive it, each with a pane still running, and its folders stay in
``/tmp``. 55 such servers were found live on one box, some of them days old.

So each folder is named for the process that made it, ``asqtx<pid>-…``, and the next
run ends whatever a dead process left (:func:`sweep_orphans`). A folder whose process is
alive belongs to a run still going (another worktree's, another xdist worker's) and is
never touched.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

#: Short on purpose: an AF_UNIX path is capped near 100 bytes.
ORPHAN_PREFIX = "asqtx"

_OWNED = re.compile(rf"^{ORPHAN_PREFIX}(\d+)-")


def uid() -> int:
    """This user's id — the ``tmux-<uid>`` folder. Windows has neither, nor tmux; a test
    that removes ``os.getuid`` (to stand for Windows) reads as uid 0 here, never a crash."""
    if sys.platform == "win32":
        return 0
    getuid = getattr(os, "getuid", None)
    return getuid() if callable(getuid) else 0


def private_folder() -> Path:
    """A new folder under ``/tmp``, named for this process."""
    import tempfile

    return Path(tempfile.mkdtemp(prefix=f"{ORPHAN_PREFIX}{os.getpid()}-", dir="/tmp"))


def end_private_servers(private: Path, owner: int | None = None, tmux: str | None = None) -> None:
    """End every tmux server under ``private`` used as a ``TMUX_TMPDIR``, with its panes."""
    folder = private / f"tmux-{uid() if owner is None else owner}"
    tmux = tmux or shutil.which("tmux")
    if tmux is None or not folder.is_dir():
        return
    for socket in folder.iterdir():
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                [tmux, "-S", str(socket), "kill-server"],
                capture_output=True,
                timeout=10,
                check=False,
            )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: alive, and someone else's
        return True
    return True


def sweep_orphans(base: Path = Path("/tmp")) -> list[Path]:
    """End the servers in, and remove, every private folder whose process is gone.

    Returns the folders removed. A folder that is not this user's is left alone: tmux
    keeps its sockets in ``tmux-<uid>``, so another user's servers are not ours to end.
    """
    if sys.platform == "win32" or not base.is_dir():
        return []
    removed: list[Path] = []
    for folder in base.iterdir():
        owned = _OWNED.match(folder.name)
        if owned is None or _alive(int(owned.group(1))):
            continue
        try:
            st = folder.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid():
            continue
        end_private_servers(folder)
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            removed.append(folder)
    return removed
