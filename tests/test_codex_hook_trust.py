"""aisquare's own Codex hooks are trusted where Codex records a human's yes (SMOKE12 O3 of #113).

Codex 0.164.0 and 0.166.0 run a ``hooks.json`` hook only once it is trusted, and ask at their
start, "Hooks need review · N hooks are new or changed", in a dialog that held a fleet window
until a human answered it: on the first manager's home, and again on every Codex slot a
``fleet switch --to`` moved an agent onto. "Trust all and continue" writes one
``[hooks.state."<hooks.json>:<event>:<group>:<handler>"]`` table per hook into the home's
``config.toml``, its ``trusted_hash`` Codex's hash of the hook as it normalizes it. Once
``install_hooks`` has written aisquare's hooks it records the same tables for them, and for
no other hook; the fixtures are what both Codex versions wrote at that dialog.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import agents
from aisquare.core.agent_adapters import codex_config, get_adapter
from aisquare.core.agent_adapters.types import HookTrustError

FIXTURES = Path(__file__).parent / "fixtures"
VERSIONS = ("0.164.0", "0.166.0")


def _fixture(version: str) -> dict[str, Any]:
    fixture: dict[str, Any] = json.loads(
        (FIXTURES / f"codex_hook_trust-{version}.json").read_text("utf-8")
    )
    return fixture


def _states(home: Path) -> dict[str, Any]:
    with (home / "config.toml").open("rb") as handle:
        states: dict[str, Any] = tomllib.load(handle).get("hooks", {}).get("state", {})
    return states


def _handlers(home: Path) -> list[tuple[str, int, int, dict[str, Any], dict[str, Any]]]:
    hooks = json.loads((home / "hooks.json").read_text("utf-8"))["hooks"]
    return [
        (event, at, place, group, handler)
        for event, groups in hooks.items()
        for at, group in enumerate(groups)
        for place, handler in enumerate(group["hooks"])
    ]


def _ours(handler: dict[str, Any]) -> bool:
    return agents._is_aisquare_hook_command(handler["command"])


@pytest.mark.parametrize("version", VERSIONS)
def test_each_hook_codex_trusted_hashes_and_keys_as_codex_recorded_it(version: str) -> None:
    """The fixture's eleven hooks span what Codex normalizes before it hashes: a timeout
    defaulted (600 s; 1 s for ``Interrupt``), clamped (``SessionEnd``'s 30 to 3, a 0 to 1),
    a matcher kept where the event reads one and dropped where it does not, a status
    message (not ASCII), an ``async``, a second group and a second handler."""
    fixture = _fixture(version)
    recorded = {
        key: state["trusted_hash"]
        for key, state in tomllib.loads(fixture["hooks_state"])["hooks"]["state"].items()
    }
    mine = {
        codex_config.hook_key(fixture["hooks_file"], event, at, place): codex_config.hook_hash(
            event, group, handler
        )
        for event, groups in fixture["hooks"]["hooks"].items()
        for at, group in enumerate(groups)
        for place, handler in enumerate(group["hooks"])
    }
    assert len(recorded) == 11
    assert mine == recorded


@pytest.mark.parametrize(
    "handler",
    [
        {"type": "prompt", "command": "x"},
        {"type": "command", "command": "x", "commandWindows": "y"},
        {"type": "command", "command": "x", "additionalContextLimit": 10},
        {"type": "command", "command": "  "},
        {"type": "command", "command": "x", "timeout": "3"},
        {"type": "command", "command": "x", "timeout": True},
        {"type": "command", "command": "x", "timeout": -1},
        {"type": "command", "command": "x", "async": "yes"},
    ],
)
def test_a_hook_whose_hash_is_not_known_here_is_left_to_codexs_question(
    handler: dict[str, Any],
) -> None:
    assert codex_config.hook_hash("Stop", {}, handler) is None
    assert codex_config.hook_hash("NoSuchEvent", {}, {"type": "command", "command": "x"}) is None


def _with_users_own(home: Path) -> bytes:
    """A home with a hook of the user's own and the user's own Codex settings and trust."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/true"}]}]}}
        ),
        encoding="utf-8",
    )
    theirs = (
        'model = "gpt-6.1-sol"\n\n[projects."/work/repo"]\ntrust_level = "trusted"\n\n'
        "[hooks.state]\n\n"
        '[hooks.state."/elsewhere/hooks.json:stop:0:0"]\n'
        'trusted_hash = "sha256:theirs"\n'
    )
    (home / "config.toml").write_text(theirs, encoding="utf-8")
    return theirs.encode("utf-8")


def test_install_trusts_aisquares_hooks_and_leaves_every_other_trust_as_it_was(
    tmp_path: Path,
) -> None:
    home = tmp_path / "codex"
    theirs = _with_users_own(home)

    assert agents.install_hooks("codex", home)

    after = (home / "config.toml").read_bytes()
    assert after.startswith(theirs), "every byte of the user's config.toml kept, in order"
    states = _states(home)
    assert states["/elsewhere/hooks.json:stop:0:0"] == {"trusted_hash": "sha256:theirs"}
    hooks_file = str(home / "hooks.json")
    ours = [entry for entry in _handlers(home) if _ours(entry[4])]
    assert len(ours) == len(get_adapter("codex").capabilities.hooks) == 8
    for event, at, place, group, handler in ours:
        key = codex_config.hook_key(hooks_file, event, at, place)
        assert states[key] == {"trusted_hash": codex_config.hook_hash(event, group, handler)}
    # The user's own Stop hook (group 0) is theirs to trust: no table of ours for it.
    assert codex_config.hook_key(hooks_file, "Stop", 0, 0) not in states
    assert len(states) == 1 + 8
    # A second install has nothing more to record, and writes nothing.
    assert agents.install_hooks("codex", home)
    assert (home / "config.toml").read_bytes() == after
    assert agents.trust_hooks("codex", home) == []


def test_a_new_definition_replaces_its_hash_in_place_and_keeps_the_humans_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upgrade writes each hook with a new ``--definition``, which Codex reads as changed
    ("hooks are new or changed") and asked about again: its ``trusted_hash`` is replaced
    where it is, and an ``enabled = false`` the human set on it stays."""
    home = tmp_path / "codex"
    home.mkdir()
    agents.install_hooks("codex", home)
    key = codex_config.hook_key(home / "hooks.json", "Stop", 0, 0)
    text = (home / "config.toml").read_text("utf-8")
    header = f"[hooks.state.{json.dumps(key)}]\n"
    assert header in text
    (home / "config.toml").write_text(
        text.replace(header, header + "enabled = false\n"), encoding="utf-8"
    )
    old = _states(home)
    monkeypatch.setattr(agents, "_aisquare_command", lambda *args, **kwargs: "/new/bin/aisquare")

    agents.install_hooks("codex", home)

    new = _states(home)
    assert set(new) == set(old) and new[key]["enabled"] is False
    for each, state in new.items():
        assert state["trusted_hash"] != old[each]["trusted_hash"]
    for event, at, place, group, handler in _handlers(home):
        state = new[codex_config.hook_key(home / "hooks.json", event, at, place)]
        assert state["trusted_hash"] == codex_config.hook_hash(event, group, handler)
    assert (home / "config.toml").read_text("utf-8").count("[hooks.state.") == 8


@pytest.mark.parametrize(
    "config",
    [
        pytest.param("model = \n", id="not-toml"),
        pytest.param('hooks = "off"\n', id="hooks-not-a-table"),
        pytest.param("[hooks]\nstate = 1\n", id="state-not-a-table"),
    ],
)
def test_a_config_toml_it_cannot_edit_is_left_as_it_was_and_the_hooks_still_written(
    tmp_path: Path, config: str
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text(config, encoding="utf-8")

    assert agents.install_hooks("codex", home)

    assert agents.hooks_installed("codex", home)
    assert (home / "config.toml").read_text("utf-8") == config
    with pytest.raises(HookTrustError):
        agents.trust_hooks("codex", home)


def test_a_hash_spelt_inline_is_refused_rather_than_rewritten(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    agents.install_hooks("codex", home)
    key = codex_config.hook_key(home / "hooks.json", "Stop", 0, 0)
    inline = f'[hooks.state]\n{json.dumps(key)} = {{ trusted_hash = "sha256:old" }}\n'
    (home / "config.toml").write_text(inline, encoding="utf-8")

    with pytest.raises(HookTrustError, match="not edited here"):
        agents.trust_hooks("codex", home)
    assert (home / "config.toml").read_text("utf-8") == inline


def test_a_config_toml_aisquare_makes_is_its_owners_alone(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    agents.install_hooks("codex", home)
    assert len(_states(home)) == 8
    if os.name == "posix":
        assert (home / "config.toml").stat().st_mode & 0o077 == 0


@pytest.mark.skipif(os.name != "posix", reason="a home through a symlink")
def test_the_trust_names_the_hooks_file_by_each_path_codex_may_find_it_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``CODEX_HOME`` Codex resolves to its real path; ``$HOME/.codex`` with none set it
    takes as it is spelt. The plain home reached through a link is trusted under both, and a
    slot's under its real path."""
    real = tmp_path / "real"
    real.mkdir()
    user = tmp_path / "user"
    user.mkdir()
    (user / ".codex").symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(agents, "_home", lambda: user)

    agents.install_hooks("codex", None)

    keys = set(_states(real))
    handlers = _handlers(real)
    assert len(handlers) == 8 and len(keys) == 16
    for event, at, place, _group, _handler in handlers:
        assert codex_config.hook_key(user / ".codex" / "hooks.json", event, at, place) in keys
        assert codex_config.hook_key(real.resolve() / "hooks.json", event, at, place) in keys
    assert (real / "config.toml").is_file() and not (user / ".codex" / "config.toml").is_symlink()

    slot = tmp_path / "slot"
    slot.mkdir()
    agents.install_hooks("codex", slot)
    assert {key.rpartition("/")[0] for key in _states(slot)} == {str(slot.resolve())}


def test_claude_code_records_no_trust(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    assert agents.install_hooks("claude-code", home)
    assert agents.trust_hooks("claude-code", home) == []
    assert not (home / "config.toml").exists()


def test_a_hooks_file_with_nothing_of_aisquares_records_nothing(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    _with_users_own(home)
    before = (home / "config.toml").read_bytes()
    assert agents.trust_hooks("codex", home) == []
    assert (home / "config.toml").read_bytes() == before
    assert agents.trust_hooks("codex", tmp_path / "nothing-here") == []
