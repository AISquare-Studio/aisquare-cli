"""Launch specs the fleet replays, sealed against a write from inside an agent's sandbox.

A row's ``launch_spec`` (``models.LaunchSpec``) is replayed outside any sandbox: by
``fleet restart``, ``fleet switch`` and the automatic usage-limit hand-over, binary and
arguments both. It lives in the store, which an agent that sandboxes its tools must be
able to write for every board call (``AgentAdapter.store_access``), so the store cannot
keep it from such an agent: a prompt-injected Codex coder could point it at a program of
its own, or hand Codex a ``-c notify=…`` that runs one, and the next hand-over ran it with
the operator's full rights (review r2 of #113, finding 3).

What a sandbox cannot do is read :func:`key_path`: the store profile denies it
(:func:`~aisquare.core.agent_adapters.types.store_unreadable`). A spec is sealed with
that key when ``fleet spawn`` records it, and a replay takes only a spec whose seal
holds (``services.fleet``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from contextlib import suppress
from pathlib import Path

from aisquare.core import paths
from aisquare.core.atomic import write_replacing
from aisquare.models import LaunchSpec

KEY_NAME = "launch-key"


def key_path() -> Path:
    """The key that seals launch specs: owner-only, and unreadable from a sandbox."""
    return paths.aisquare_home() / KEY_NAME


def _stored() -> bytes | None:
    """The key on disk; ``None`` when there is none to use."""
    try:
        raw = key_path().read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    return key if len(key) >= 32 else None


def ensure_key() -> bytes | None:
    """The key, made when there is none; ``None`` when it can be neither read nor made.

    "None" includes the empty file a Codex sandbox leaves where a path its profile denies
    did not exist (measured on 0.162.1: an empty mode-000 file, which the sandbox cannot
    write either): it is replaced. A key that is there and cannot be read is not, since
    remaking it would void every seal made with it; inside a sandbox, that is every
    call, and the answer is ``None``.

    The spawn that makes it seals the specs already recorded (``seal_launch_specs``), as
    every spawn before it would have: the agents running across an upgrade keep their
    launch.
    """
    import secrets  # its import is the ssl stack's (tests/test_iam_single_reader.py)

    key = _stored()
    if key is not None:
        return key
    path = key_path()
    made = path.with_name(f".{KEY_NAME}.{secrets.token_hex(4)}")
    try:
        if path.exists() and path.stat().st_size > 0:
            return None
        paths.ensure_home()
        write_replacing(made, secrets.token_hex(32) + "\n", keep_mode=False, owner_only=True)
        if path.exists() and path.stat().st_size == 0:
            path.unlink()  # the sandbox's empty file; a key another spawn made stays
        # Linked, never renamed over: two first spawns racing keep ONE key, the other's
        # link failing, so neither seals with a key the other then replaces.
        os.link(made, path)
    except FileExistsError:
        return _stored()
    except OSError:
        return None
    finally:
        with suppress(OSError):
            made.unlink(missing_ok=True)
    key = _stored()
    if key is not None:
        _seal_recorded(key)
    return key


def _seal_recorded(key: bytes) -> None:
    """Seal the specs recorded before ``key`` was made; a store that cannot be opened keeps
    them unsealed, which a replay then says (never raises)."""
    from aisquare.core.store import store_session

    with suppress(Exception), store_session() as store:
        store.seal_launch_specs(lambda spec: spec.model_copy(update={"seal": _digest(key, spec)}))


def _digest(key: bytes, spec: LaunchSpec) -> str:
    body = json.dumps(
        spec.model_dump(mode="json", exclude={"seal"}), sort_keys=True, separators=(",", ":")
    )
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def sealed(spec: LaunchSpec) -> LaunchSpec:
    """``spec`` with its seal; unsealed when the key can be neither read nor made."""
    key = ensure_key()
    return spec.model_copy(update={"seal": _digest(key, spec) if key is not None else None})


def holds(spec: LaunchSpec) -> bool:
    """Whether ``spec`` is as ``fleet spawn`` sealed it: a spec with no seal, one sealed
    under another key, and one changed since, do not hold."""
    key = _stored()
    return (
        spec.seal is not None
        and key is not None
        and hmac.compare_digest(spec.seal, _digest(key, spec))
    )
