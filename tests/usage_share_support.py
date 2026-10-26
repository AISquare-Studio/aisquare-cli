"""A fake server and a checker of the wire contract, for the usage-sharing tests.

Not a test module. ``Server`` stands in for the AISquare API's ``/api/v1/cli/metrics/`` as the
shipper's transport: it records every call and answers as scripted, and no test reaches a real
one (the BE endpoint is a separate PR). ``check_contract`` holds a body to the contract
(``docs/usage-telemetry.md``): its keys, types, caps and enumerations.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from aisquare.services import iam


@dataclass
class Call:
    url: str
    body: dict[str, Any]
    token: str | None
    timeout: float


class Server:
    """A transport that answers as told, one reply per call (200 once the replies run out)."""

    def __init__(self, *replies: iam.HttpResult | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[Call] = []

    def __call__(
        self, url: str, body: dict[str, Any], token: str | None, timeout: float
    ) -> iam.HttpResult:
        self.calls.append(Call(url, copy.deepcopy(body), token, timeout))
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return ok(len(body["days"]), linked=token is not None)


def ok(days: int, *, linked: bool = False) -> iam.HttpResult:
    return iam.HttpResult(200, {"accepted_days": days, "linked": linked}, {})


def status(code: int, body: Any = None, **headers: str) -> iam.HttpResult:
    return iam.HttpResult(code, body, dict(headers))


def unreachable() -> iam.IamError:
    return iam.IamError("unreachable", "Could not reach the server: connection refused.")


_DAY_KEYS = {
    "date", "tz_offset_minutes", "active", "signed_in", "tokens", "cost_usd_list", "sessions",
    "turns", "models", "accounts", "repos_active", "repos", "features", "fleet",
}  # fmt: skip
_TOKEN_KEYS = {"input", "cache_write", "cache_read", "output"}
_HOSTS = {"github", "gitlab", "bitbucket", "other", "none"}
_SIZES = {"xs", "s", "m", "l", "xl"}
_METHODS = {"brew", "pipx", "uv", "pip", "script", "unknown"}


def check_contract(body: dict[str, Any]) -> None:
    """Assert ``body`` is a schema-version-1 body of the wire contract, within its caps."""
    assert set(body) == {"schema_version", "install_id", "sent_at", "opt_out", "client", "days"}
    assert body["schema_version"] == 1
    assert uuid.UUID(body["install_id"]).version == 4
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", body["sent_at"])
    assert isinstance(body["opt_out"], bool)
    client = body["client"]
    assert set(client) == {"cli_version", "os", "os_version", "arch", "python", "install_method"}
    assert all(isinstance(value, str) and value for value in client.values())
    assert client["install_method"] in _METHODS
    days = body["days"]
    assert isinstance(days, list) and len(days) <= 31
    assert [day["date"] for day in days] == sorted(day["date"] for day in days)
    for day in days:
        _check_day(day)
    assert len(json.dumps(body).encode()) <= 256 * 1024


def _check_day(day: dict[str, Any]) -> None:
    assert set(day) == _DAY_KEYS, set(day) ^ _DAY_KEYS
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day["date"])
    assert isinstance(day["tz_offset_minutes"], int) and day["active"] is True
    assert isinstance(day["signed_in"], bool)
    tokens = day["tokens"]
    assert set(tokens) == _TOKEN_KEYS | {"total"}
    assert tokens["total"] == sum(tokens[key] for key in _TOKEN_KEYS)
    assert all(isinstance(value, int) and value >= 0 for value in tokens.values())
    assert isinstance(day["cost_usd_list"], float) and day["cost_usd_list"] >= 0
    assert day["sessions"] >= 0 and day["turns"] >= 0
    for model, counts in day["models"].items():
        assert re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", model)
        assert set(counts) == _TOKEN_KEYS
    assert sum(sum(c.values()) for c in day["models"].values()) == tokens["total"]
    accounts = day["accounts"]
    assert set(accounts) == {"active", "registered", "by_type", "top_account_share"}
    assert 0 <= accounts["top_account_share"] <= 1
    assert all(re.fullmatch(r"[A-Za-z0-9._:-]{1,32}", kind) for kind in accounts["by_type"])
    assert len(day["repos"]) <= 25 and day["repos_active"] >= len(day["repos"])
    for repo in day["repos"]:
        assert set(repo) == {"repo_hash", "tokens_total", "sessions", "turns", "traits"}
        assert re.fullmatch(r"[0-9a-f]{16}", repo["repo_hash"])
        traits = repo["traits"]
        assert set(traits) == {"host", "language", "size_bucket", "monorepo"}
        assert traits["host"] in _HOSTS and traits["size_bucket"] in _SIZES
        assert isinstance(traits["monorepo"], bool) and isinstance(traits["language"], str)
    assert len(day["features"]) <= 200
    for name, count in day["features"].items():
        assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*){0,6}", name)
        assert isinstance(count, int) and count > 0
    fleet = day["fleet"]
    assert set(fleet) == {"agents_spawned", "tasks_done"} and fleet["tasks_done"] >= 0
