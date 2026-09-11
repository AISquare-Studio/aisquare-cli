"""STUB of the Remote local server — the module API of PLAN §4-F, and nothing behind it.

The real server (static dist + read-only JSON API + WS stream + password gate on
127.0.0.1:8748) is written on ``feat/remote-server``. The TUI modal on
``feat/remote-modal`` codes against THIS signature so its branch runs and tests
standalone; when the server branch merges, this file is replaced wholesale and
the modal needs no change — the six names and ``RemoteInfo`` are the contract:

    start(dist_dir, port=8748) -> RemoteInfo{token, password, url_local}
    stop()
    status() -> {"running": bool, "sessions": [{sid, ua, first_seen, last_seen}]}
    revoke(sid)             # drops the cookie session (and, for real, its websockets)
    set_allow_write(bool)
    regenerate_password() -> str

What the stub DOES do is keep ``~/.aisquare/remote.json`` (0600) in the PLAN §1
shape ``{token, password, allow_write, auto_off_at, sessions:[...]}``, because
the modal's Devices list reads sessions from that file and its password /
allow-write controls write through these functions. It binds no port.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aisquare.core import paths

DEFAULT_PORT = 8748

_WORDS = (
    "amber", "birch", "cedar", "delta", "ember", "fjord", "glade", "harbor",
    "indigo", "juniper", "kestrel", "lagoon", "meadow", "nectar", "orchid", "pebble",
    "quartz", "river", "saffron", "tundra", "umber", "velvet", "willow", "yarrow",
    "zenith", "anchor", "beacon", "canyon", "dune", "falcon", "garnet", "heron",
)  # fmt: skip
"""Small, unambiguous, easy to type on a phone: 32 words → 4 of them ≈ 20 bits + the URL token."""

_running = False


@dataclass(frozen=True)
class RemoteInfo:
    """What ``start`` hands the TUI: the URL-path token, the unlock password, the local URL."""

    token: str
    password: str
    url_local: str


def remote_state_path() -> Path:
    """``~/.aisquare/remote.json`` — the server's own state, separate from ``state.json``."""
    return paths.aisquare_home() / "remote.json"


def new_password() -> str:
    """A 4-word passphrase, hyphen-joined, four DISTINCT words from :data:`_WORDS`."""
    import secrets  # inside the function: tests/test_iam_single_reader.py's import ratchet

    return "-".join(secrets.SystemRandom().sample(_WORDS, 4))


def _read() -> dict[str, Any]:
    path = remote_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    paths.ensure_home()
    path = remote_state_path()
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def _ensure_state() -> dict[str, Any]:
    """The state file with every PLAN §1 key present; a missing token/password is minted."""
    import secrets  # inside the function: the hook path must not pay for it

    data = _read()
    changed = False
    if not isinstance(data.get("token"), str) or not data["token"]:
        data["token"] = secrets.token_urlsafe(16)
        changed = True
    if not isinstance(data.get("password"), str) or not data["password"]:
        data["password"] = new_password()
        changed = True
    if not isinstance(data.get("allow_write"), bool):
        data["allow_write"] = False  # never on by default — PLAN §4-B/E
        changed = True
    data.setdefault("auto_off_at", None)
    if not isinstance(data.get("sessions"), list):
        data["sessions"] = []
        changed = True
    if changed:
        _write(data)
    return data


def start(dist_dir: Path | None = None, port: int = DEFAULT_PORT) -> RemoteInfo:
    """Mint (or reuse) the token and password and report the local URL. Binds nothing (stub)."""
    global _running
    data = _ensure_state()
    _running = True
    return RemoteInfo(
        token=data["token"],
        password=data["password"],
        url_local=f"http://127.0.0.1:{port}/r/{data['token']}",
    )


def stop() -> None:
    global _running
    _running = False


def status() -> dict[str, Any]:
    """``{running, sessions}`` — sessions come from ``remote.json`` so the modal shows real rows."""
    data = _read()
    sessions = [s for s in data.get("sessions", []) if isinstance(s, dict)]
    return {"running": _running, "sessions": sessions}


def revoke(sid: str) -> None:
    """Drop the session ``sid``; the real server also closes its websockets (code 4401)."""
    data = _ensure_state()
    data["sessions"] = [s for s in data["sessions"] if s.get("sid") != sid]
    _write(data)


def set_allow_write(enabled: bool) -> None:
    data = _ensure_state()
    data["allow_write"] = bool(enabled)
    _write(data)


def regenerate_password() -> str:
    """Mint a new passphrase; existing sessions stay (they unlocked with the old one)."""
    data = _ensure_state()
    data["password"] = new_password()
    _write(data)
    return str(data["password"])
