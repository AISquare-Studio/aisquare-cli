"""One reader and one writer for ``~/.aisquare/credentials``.

The file had two writers with two formats: ``init --api-key`` replaced the whole
file with a bare key string, and ``serve_token`` read JSON and fell back to
``{}`` on a decode error. Either order destroyed the other's value, silently —
the decode error read a bare key as "no data" rather than as "someone else owns
this file".

Two callers agreeing by careful editing is what produced that. A single
read-merge-write is what stops it recurring, which is why this module exists
rather than a matched pair of fixes.

JSON, because it is the format that can hold two names. A file already holding a
bare key is MIGRATED into ``api_key`` rather than discarded: every machine that
ran ``init --api-key`` before this change has one, and "unparseable therefore
empty" is the exact reading that lost data.
"""

from __future__ import annotations

import json
from typing import Any

from aisquare.core import paths

#: Where a legacy bare-string file is migrated to.
API_KEY = "api_key"


def load_all() -> dict[str, str]:
    """Everything stored, or ``{}``. Never raises — both callers are commands.

    A file that is not JSON is not assumed empty. If it holds a single
    non-blank line it is a pre-JSON API key and is reported as one; anything
    else genuinely carries nothing we can name.
    """
    path = paths.credentials_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        loaded: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        legacy = raw.strip()
        return {API_KEY: legacy} if legacy else {}
    if isinstance(loaded, dict):
        return {str(k): v for k, v in loaded.items() if isinstance(v, str)}
    return {}


def store(**values: str) -> tuple[dict[str, str], bool]:
    """Merge ``values`` into whatever is already there, owner-only.

    Returns the merged result and whether the file could actually be restricted
    to this account. The second half is not decoration: on NTFS
    ``chmod(0o600)`` returns cleanly and protects nothing, so a caller that
    assumed success would promise a guard it does not have. The one writer
    reports both facts so neither caller has to ask a second question.
    """
    data = load_all()
    data.update({k: v for k, v in values.items() if v})
    paths.ensure_home()
    path = paths.credentials_path()
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data, paths.restrict_to_owner(path)
