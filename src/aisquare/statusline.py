"""``asq-statusline``: Claude Code's status line, and a record of each session (#287, 2c).

Claude Code runs the ``statusLine`` command after each assistant message, its session's
state as JSON on stdin (https://code.claude.com/docs/en/statusline): the session id,
cwd, model, context window, cost and, for a Pro or Max login after its first answer, the
account's ``rate_limits``. This keeps what aisquare reads of it, one file per session
under ``<aisquare home>/cache/statusline/<session_id>.json``, written atomically, and
prints one line no wider than ``COLUMNS``::

    acct 3 · 5h 42% · wk 61% · fleet 58% left (3x) · ctx 34%

(the x a multiplication sign, as on asq's sidebar).

The fleet's figure is ``fleet.json`` beside the records, which asq's headroom read and
``aisquare accounts usage`` write. The records feed ``aisquare accounts sessions`` and,
as ``claude_usage`` samples, the usage cache (``services.claude_accounts.ingest_statusline``):
a reading Claude Code already had, so the usage endpoint is asked less often.

LEAN ON PURPOSE. It runs on every assistant message of every session that carries it, so
it imports the standard library only: ``json``, ``os``, ``sys``, ``time``, ``pathlib`` (and
``subprocess`` only under ``--wrap``). No ``typer``, ``rich``, ``textual``, ``pydantic`` or
``sqlite3``, pinned by ``tests/test_statusline.py``. It never fails the status line: a
payload it cannot read prints what it can, and nothing is raised to Claude Code.

``--wrap COMMAND`` runs the user's own status line first, with the same stdin, and prints
its output before ours (``aisquare accounts statusline --install --wrap``).

Records hold the session id, its cwd, its model and the figures above: never a prompt.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

RECORD_DIR = ("cache", "statusline")
"""Where the records are, under the aisquare home."""
FLEET_FILE = "fleet.json"
"""The fleet's summary beside the records: ``percent_left``, ``accounts``, ``labels``."""
WRAP_FLAG = "--wrap"
"""Runs the user's own status line command first (its output, then ours)."""
WRAP_TIMEOUT = 5.0
"""How long a wrapped command may take, in seconds, before its output is dropped."""
SEPARATOR = " · "
TIMES = "\N{MULTIPLICATION SIGN}"


def home() -> Path:
    """The aisquare home, as ``core.paths.aisquare_home`` finds it, without importing it."""
    override = os.environ.get("AISQUARE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".aisquare"


def record_dir() -> Path:
    return home().joinpath(*RECORD_DIR)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _epoch(value: Any) -> float | None:
    """A ``resets_at``: epoch seconds as given, or an ISO time read as one; else ``None``."""
    if (number := _number(value)) is not None:
        return number
    if isinstance(value, str):
        from datetime import datetime  # only for a payload that sends text

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _window(limits: Any, name: str) -> dict[str, float | None] | None:
    window = limits.get(name) if isinstance(limits, dict) else None
    if not isinstance(window, dict):
        return None
    used = _number(window.get("used_percentage"))
    if used is None:
        return None
    return {"used_percentage": used, "resets_at": _epoch(window.get("resets_at"))}


def _get(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def record_of(
    payload: Any, env: dict[str, str] | os._Environ[str], now: float
) -> dict[str, Any] | None:
    """What is kept of one payload, or ``None`` without a session id to file it under."""
    if not isinstance(payload, dict):
        return None
    session = payload.get("session_id")
    if not isinstance(session, str) or not session or "/" in session or "\\" in session:
        return None
    model = _get(payload, "model", "id")
    cwd = payload.get("cwd") or _get(payload, "workspace", "current_dir")
    limits = payload.get("rate_limits")
    return {
        "session_id": session,
        "cwd": cwd if isinstance(cwd, str) else None,
        "model": model if isinstance(model, str) else None,
        "config_dir": env.get("CLAUDE_CONFIG_DIR") or None,
        "fleet_agent": env.get("AISQUARE_FLEET_AGENT") or None,
        "five_hour": _window(limits, "five_hour"),
        "seven_day": _window(limits, "seven_day"),
        "context_percent": _number(_get(payload, "context_window", "used_percentage")),
        "cost_usd": _number(_get(payload, "cost", "total_cost_usd")),
        "updated_at": now,
    }


def write_record(directory: Path, record: dict[str, Any]) -> Path:
    """``record`` to ``<directory>/<session_id>.json``: a temp beside it, renamed over it."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{record['session_id']}.json"
    temporary = directory / f".{record['session_id']}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.replace(temporary, target)
    return target


def read_fleet(directory: Path) -> dict[str, Any]:
    """``fleet.json``, or ``{}`` when there is none or it cannot be read."""
    try:
        data = json.loads((directory / FLEET_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _account(record: dict[str, Any], fleet: dict[str, Any]) -> str:
    """The account's name: its label from ``fleet.json``, else ``acct N`` for a managed
    slot's directory, else ``claude`` (the plain claude, or a directory nobody named)."""
    config_dir = record.get("config_dir")
    labels = fleet.get("labels")
    if config_dir and isinstance(labels, dict):
        named = labels.get(os.path.realpath(config_dir)) or labels.get(config_dir)
        if isinstance(named, str) and named:
            return named
    if config_dir:
        path = Path(config_dir)
        if path.name.isdigit() and path.parent.name == "claude-accounts":
            return f"acct {int(path.name)}"
    if isinstance(labels, dict) and isinstance(labels.get(""), str):
        return str(labels[""])
    return "claude"


def line(record: dict[str, Any], fleet: dict[str, Any], columns: int) -> str:
    """The status line: account · 5h · wk · fleet · ctx, each part only when known,
    dropped from the end until it fits ``columns``, the account cut last."""
    parts = [_account(record, fleet)]
    for name, key in (("5h", "five_hour"), ("wk", "seven_day")):
        window = record.get(key)
        if isinstance(window, dict) and window.get("used_percentage") is not None:
            parts.append(f"{name} {round(window['used_percentage'])}%")
    left = _number(fleet.get("percent_left"))
    count = fleet.get("accounts")
    if left is not None:
        many = f" ({count}{TIMES})" if isinstance(count, int) and count > 1 else ""
        parts.append(f"fleet {round(left)}% left{many}")
    if (context := record.get("context_percent")) is not None:
        parts.append(f"ctx {round(context)}%")
    while len(parts) > 1 and len(SEPARATOR.join(parts)) > columns:
        parts.pop()
    return SEPARATOR.join(parts)[: max(columns, 1)]


def _columns(env: dict[str, str] | os._Environ[str]) -> int:
    try:
        return max(int(env.get("COLUMNS", "")), 1)
    except ValueError:
        return 80


def _wrapped(command: str, stdin: str) -> str:
    """The user's own status line, run as Claude Code would run it; nothing if it fails."""
    import subprocess  # only under --wrap

    try:
        done = subprocess.run(
            command,
            shell=True,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=WRAP_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.rstrip("\n")


def main(argv: list[str] | None = None) -> int:
    """Read the payload, keep its record, print the line. Always exits 0."""
    args = sys.argv[1:] if argv is None else argv
    raw = sys.stdin.read()
    out: list[str] = []
    if len(args) >= 2 and args[0] == WRAP_FLAG and (own := _wrapped(args[1], raw)):
        out.append(own)
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    record = record_of(payload, os.environ, time.time())
    directory = record_dir()
    if record is not None:
        with contextlib.suppress(OSError):  # the line still prints; the record is a bonus
            write_record(directory, record)
        out.append(line(record, read_fleet(directory), _columns(os.environ)))
    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
