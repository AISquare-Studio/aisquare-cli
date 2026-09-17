"""``~/.aisquare/state.json`` — the small runtime-state file, read and written in one place.

Three preferences share it: the project ``project switch`` pinned
(``active_project_id``, :mod:`aisquare.core.workspace`), the board's theme
(``board_theme``, ``cli.watch``) and the fleet UI's navigator width
(``sidebar_width``, ``cli.ui``). Each surface used to carry its own
read-modify-write of the file, and the copies had drifted: one caught only
``JSONDecodeError``, two raised ``AttributeError`` on a file whose top level
was not an object (``.get`` on a list), one wrote in place with no rename, and
two shared one fixed temp name — so one corrupt file produced a different
failure per surface, and two processes autosaving at once could truncate each
other's write. This is the one home; the surfaces keep their keys and call
here.

- :func:`read_state` never raises. A missing, unreadable or non-object file
  reads as ``{}``: every key is a preference, and a preference that cannot be
  read is one that is not set.
- :func:`update_state` sets or removes ONE key and keeps every other. It writes
  a sibling temp file named for this process and ``os.replace``\\ s it over the
  target — the recipe ``services.ci_descriptor._replace`` uses — so ``asq`` and
  ``board -w`` cannot clobber each other's temp, and a crash mid-write cannot
  leave the file torn. A file that exists but is not a JSON object is left
  exactly as it is and the update is refused: the keys in it are a user's, and
  destroying them to record a width is worse than not recording it.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from aisquare.core import paths


def read_state() -> dict[str, object]:
    """The file's contents; ``{}`` when it is missing, unreadable or not a JSON object."""
    try:
        data = json.loads(paths.state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(data) if isinstance(data, dict) else {}


def update_state(key: str, value: object) -> bool:
    """Set ``key`` to ``value`` — or, with ``None``, drop it — keeping every other key.

    ``True`` when the file now says so. ``False`` when it was left as it was:
    it exists but is not a JSON object, or it could not be read or written.
    Never raises.
    """
    path = paths.state_path()
    try:
        paths.ensure_home()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    return _replace(path, json.dumps(data, indent=2) + "\n")


def _replace(target: Path, body: str) -> bool:
    """Write ``body`` to ``target`` in one step: this process's own temp file, then a rename."""
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        return False
    return True
