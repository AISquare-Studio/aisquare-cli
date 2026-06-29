"""Launch the user's text editor on a piece of text.

Used by ``context edit``. Kept as a single function in ``core`` so the service
layer can offer "edit in $EDITOR" without depending on the CLI framework, and so
tests can stub it with one ``monkeypatch``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path


def _editor_command() -> list[str]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    return shlex.split(editor)


def edit_text(text: str, *, suffix: str = ".md") -> str | None:
    """Open ``text`` in the user's editor and return the edited result.

    Returns ``None`` if the editor exits non-zero (treated as "leave
    unchanged"). The text round-trips through a temporary file.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(text)
        tmp_path = Path(handle.name)
    try:
        completed = subprocess.run([*_editor_command(), str(tmp_path)], check=False)
        if completed.returncode != 0:
            return None
        return tmp_path.read_text(encoding="utf-8")
    finally:
        tmp_path.unlink(missing_ok=True)
