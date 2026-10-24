"""The Codex plugin route: the marketplace, the plugin, its hooks and its launcher.

``codex plugin marketplace add AISquare-Studio/aisquare-cli``, then ``codex plugin add
aisquare@aisquare-cli``, gives Codex the hooks ``aisquare agents connect codex`` writes
into ``$CODEX_HOME/hooks.json``, with no installer. Each one runs
``plugins/codex/scripts/aisquare-hook codex <Event>``: the launcher the Claude Code plugin
ships, byte for byte, around ``aisquare hook codex``.

THE DEFECTS A PLUGIN INVITES. Codex runs a plugin's hooks AND hooks.json's, launching the
matching ones for an event at once (its hooks page), so with both routes every session
would get its context twice; the launcher stands down while hooks.json runs aisquare's.
And Codex 0.162.1 reads a repository's ``.claude-plugin/marketplace.json`` when it has no
``.agents/plugins/marketplace.json``: before this route, ``codex plugin add
aisquare@aisquare-cli`` installed the Claude Code plugin, whose hooks are Claude Code's
(measured). So the Codex marketplace has a file of its own, which Codex reads first.

Codex installs a plugin in ``$CODEX_HOME/plugins/cache/<marketplace>/<plugin>/<version>``,
enables it in ``config.toml`` (``[plugins."aisquare@aisquare-cli"] enabled = true``), and
runs none of its hooks until ``/hooks`` trusts them, which it records per hook in
``[hooks.state."aisquare@aisquare-cli:hooks/hooks.json:<event>:0:0"]`` (measured on
0.162.1; the fixtures below write the same).
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import agents as agent_core
from aisquare.core.agent_adapters import codex as codex_adapter
from aisquare.core.agent_adapters import get_adapter
from tests.test_claude_plugin import (
    LAUNCHER as CLAUDE_LAUNCHER,
)
from tests.test_claude_plugin import (
    Machine,
    posix_only,
    version_disagreements,
)

REPO = Path(__file__).resolve().parents[1]
MARKETPLACE = REPO / ".agents" / "plugins" / "marketplace.json"
CLAUDE_MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
PLUGIN = REPO / "plugins" / "codex"
PLUGIN_JSON = PLUGIN / ".codex-plugin" / "plugin.json"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"
LAUNCHER = PLUGIN / "scripts" / "aisquare-hook"
PYPROJECT = REPO / "pyproject.toml"
DOCS = REPO / "docs" / "codex-plugin.md"

_COMMAND = 'sh "${{PLUGIN_ROOT}}/scripts/aisquare-hook" codex {event}'
_SPECS = get_adapter("codex").capabilities.hooks
_EVENTS = [spec.event for spec in _SPECS]
_SH = shutil.which("sh")

#: The plugin's hooks run `sh`, so native Windows reads only the hooks.json route.
posix_route = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the plugin route runs sh: native Windows reads only the hooks.json route",
)


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _version() -> str:
    version: str = _load(PLUGIN_JSON)["version"]
    return version


# --------------------------------------------------------------------------- the files


def test_the_plugin_ships_the_release_it_pins() -> None:
    disagreements = version_disagreements(
        pyproject=PYPROJECT.read_text(encoding="utf-8"),
        plugin_json=PLUGIN_JSON.read_text(encoding="utf-8"),
        launcher=LAUNCHER.read_text(encoding="utf-8"),
    )
    assert disagreements == [], "the release commit bumps both plugins' versions with the rest"


def test_both_plugins_ship_one_launcher() -> None:
    """One script serves both agents, so the routes cannot drift apart; each plugin is
    copied into its agent's cache alone, so each carries its own copy."""
    assert LAUNCHER.read_bytes() == CLAUDE_LAUNCHER.read_bytes(), (
        "edit plugins/claude-code/scripts/aisquare-hook and copy it over this one"
    )
    if sys.platform != "win32":
        assert os.access(LAUNCHER, os.X_OK)


def test_the_marketplace_lists_this_plugin_under_the_names_doctor_reads() -> None:
    """``config.toml`` keys the plugin as ``<plugin>@<marketplace>``; doctor and uninstall
    read that key. Codex's marketplace manifest wants a policy and a category per entry."""
    market = _load(MARKETPLACE)
    plugin = _load(PLUGIN_JSON)
    [entry] = market["plugins"]
    source = entry["source"]
    path = Path(source["path"])

    assert market["name"] == agent_core.CLAUDE_PLUGIN_MARKETPLACE
    assert entry["name"] == plugin["name"] == agent_core.CLAUDE_PLUGIN == codex_adapter.PLUGIN
    assert f"{plugin['name']}@{market['name']}" == agent_core.CLAUDE_PLUGIN_ID
    assert source["source"] == "local"
    assert source["path"].startswith("./") and ".." not in path.parts
    assert (REPO / path).resolve() == PLUGIN.resolve()
    assert entry["policy"] == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}
    assert entry["category"]
    assert "version" not in entry, "plugin.json holds the version: one fewer number to bump"


def test_the_claude_code_marketplace_still_lists_the_claude_code_plugin() -> None:
    """The control: one repository, two marketplaces of one name, each its agent's plugin.
    Codex reads ``.agents/plugins/marketplace.json`` before ``.claude-plugin/``'s."""
    [entry] = _load(CLAUDE_MARKETPLACE)["plugins"]
    assert (REPO / entry["source"]).resolve() == (REPO / "plugins" / "claude-code").resolve()


def test_the_manifest_leaves_codex_to_find_hooks_json() -> None:
    """A ``hooks`` entry in the manifest replaces ``hooks/hooks.json``; there is none, so the
    file Codex keys its trust by (``<plugin>:hooks/hooks.json:...``) is the one it reads."""
    plugin = _load(PLUGIN_JSON)
    assert "hooks" not in plugin
    assert plugin["interface"]["displayName"] == "aisquare"
    assert HOOKS_JSON.is_file()


def hook_problems(hooks_json: Mapping[str, Any]) -> list[str]:
    """How a hooks.json differs from the hooks ``agents connect codex`` installs; empty if not.

    The Codex adapter's events, each one group (with the adapter's matcher) of one hook
    that runs the launcher with that event, and the adapter's timeout: the shape whose
    trust the adapter reads, one ``[hooks.state]`` key per event, ``:0:0``.
    """
    events = hooks_json.get("hooks")
    if not isinstance(events, dict):
        return ["no top-level `hooks` object"]
    problems = [
        f"{event}: not an event agents connect codex installs"
        for event in sorted(set(events) - set(_EVENTS))
    ]
    for spec in _SPECS:
        groups = events.get(spec.event)
        if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
            problems.append(f"{spec.event}: missing, or not exactly one group")
            continue
        if groups[0].get("matcher") != spec.matcher:
            problems.append(
                f"{spec.event}: matcher {groups[0].get('matcher')!r}, not {spec.matcher!r}"
            )
        items = groups[0].get("hooks")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            problems.append(f"{spec.event}: not exactly one hook")
            continue
        item = items[0]
        if item.get("type") != "command" or item.get("command") != _COMMAND.format(
            event=spec.event
        ):
            problems.append(f"{spec.event}: does not run the launcher with {spec.event}")
        if item.get("timeout") != spec.timeout:
            problems.append(f"{spec.event}: timeout {item.get('timeout')!r}, not {spec.timeout}")
    return problems


def test_hooks_json_runs_every_lifecycle_event_through_the_launcher() -> None:
    assert hook_problems(_load(HOOKS_JSON)) == []


def _set(event: str, key: str, value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(hooks: dict[str, Any]) -> None:
        hooks["hooks"][event][0]["hooks"][0][key] = value

    return mutate


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda h: h["hooks"].pop("Interrupt"), "Interrupt: missing, or not exactly one group"),
        (
            _set("Stop", "command", _COMMAND.format(event="SessionEnd")),
            "Stop: does not run the launcher with Stop",
        ),
        (_set("SessionStart", "timeout", 60), "SessionStart: timeout 60, not 120"),
        (
            lambda h: h["hooks"]["PreToolUse"][0].pop("matcher"),
            "PreToolUse: matcher None, not 'request_user_input'",
        ),
        (
            lambda h: h["hooks"].update(Notification=h["hooks"]["Stop"]),
            "Notification: not an event agents connect codex installs",
        ),
        (
            lambda h: h["hooks"]["Stop"].append(h["hooks"]["Stop"][0]),
            "Stop: missing, or not exactly one group",
        ),
        (lambda h: h.update(hooks=[]), "no top-level `hooks` object"),
    ],
    ids=["missing", "wrong-event", "short-timeout", "no-matcher", "extra", "two-groups", "list"],
)
def test_the_hooks_guard_names_each_way_hooks_json_can_drift(
    mutate: Callable[[dict[str, Any]], None], expected: str
) -> None:
    broken = copy.deepcopy(_load(HOOKS_JSON))
    mutate(broken)
    assert hook_problems(broken) == [expected]


# --------------------------------------------------------------------------- the launcher


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    """A HOME whose ``~/.codex`` installed the plugin (where ``PLUGIN_ROOT`` points), a PATH
    of fakes, and a log of what they saw: tests/test_claude_plugin.py's."""
    home = tmp_path / "home"
    tools = tmp_path / "tools"
    tools.mkdir()
    for needed in ("grep", "mkdir", "date", "mv", "rm"):
        found = shutil.which(needed)
        assert found is not None, f"the launcher needs {needed}"
        (tools / needed).symlink_to(found)
    log = tmp_path / "log"
    log.mkdir()
    root = home / ".codex" / "plugins" / "cache" / "aisquare-cli" / "aisquare" / _version()
    return Machine(home=home, bin=tmp_path / "bin", tools=tools, log=log, plugin_root=root)


def _run(
    machine: Machine,
    event: str,
    *,
    stdin: bytes = b"",
    env: Mapping[str, str] | None = None,
    argv: tuple[str, ...] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """The launcher as Codex runs it: ``codex <Event>``, with PLUGIN_ROOT and its
    CLAUDE_PLUGIN_ROOT alias both set to the installed copy (measured on 0.162.1)."""
    assert _SH is not None
    base = {
        "PATH": os.pathsep.join([str(machine.bin), str(machine.tools)]),
        "HOME": str(machine.home),
        "PLUGIN_ROOT": str(machine.plugin_root),
        "CLAUDE_PLUGIN_ROOT": str(machine.plugin_root),
    }
    return subprocess.run(
        [_SH, str(LAUNCHER), *(argv if argv is not None else ("codex", event))],
        input=stdin,
        env={**base, **(env or {})},
        capture_output=True,
        timeout=60,
        check=False,
    )


def _hooks_json(home: Path, *commands: str, indent: int | None = 2) -> None:
    """A hooks.json whose every hook is one of ``commands``, on SessionStart and Stop."""
    hooks = {
        event: [{"hooks": [{"type": "command", "command": command} for command in commands]}]
        for event in ("SessionStart", "Stop")
    }
    home.mkdir(parents=True, exist_ok=True)
    separators = None if indent else (",", ":")
    (home / "hooks.json").write_text(
        json.dumps({"hooks": hooks}, indent=indent, separators=separators), encoding="utf-8"
    )


def _program(machine: Machine, relative: str = "programs/aisquare") -> Path:
    script = machine.home / relative
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    return script


@posix_only
@pytest.mark.parametrize("event", _EVENTS)
def test_it_runs_the_cli_with_the_home_and_the_payload_untouched(
    machine: Machine, event: str
) -> None:
    """``aisquare hook codex`` reads the event from the payload, and keys the session by
    the home this copy was installed in, as hooks.json's ``--config-dir`` does."""
    machine.fake("aisquare")
    payload = f'{{"hook_event_name": "{event}", "prompt": "café \\"q\\""}}\n'.encode()

    result = _run(machine, event, stdin=payload)

    assert result.returncode == 0, result.stderr
    assert machine.ran("aisquare") == [
        "hook",
        "codex",
        "--config-dir",
        str(machine.home / ".codex"),
    ]
    assert machine.stdin_of("aisquare") == payload, "the launcher must never read stdin"
    assert result.stdout == b"context from the cli\n"


@posix_only
def test_a_copy_outside_a_codex_cache_names_no_home(machine: Machine) -> None:
    """``aisquare hook codex`` then reads CODEX_HOME, as a session's own hooks do."""
    machine.fake("aisquare")

    _run(machine, "Stop", env={"PLUGIN_ROOT": str(machine.home / "src" / "plugins" / "codex")})

    assert machine.ran("aisquare") == ["hook", "codex"]


@posix_only
@pytest.mark.parametrize("form", ["console-script", "python-m"])
def test_it_stands_down_where_agents_connect_installed_the_hooks(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    """Both routes in one Codex home run each hook once; disconnect hands them to the
    plugin. The hooks are the ones `agents connect codex` writes, `--config-dir` and
    `--definition` and all, naming a program this test chooses."""
    codex = machine.home / ".codex"
    codex.mkdir(parents=True)
    machine.fake("aisquare")
    if form == "console-script":
        program = _program(machine, "My Tools/bin/aisquare")
        monkeypatch.setattr("aisquare.core.agents.sys.argv", [str(program)])
    else:
        monkeypatch.setattr("aisquare.core.agents.sys.argv", [str(machine.home / "pytest")])
        monkeypatch.setattr(agent_core, "current_install", lambda: machine.home / "no-venv")
        monkeypatch.setenv("PATH", str(machine.tools))
    assert agent_core.install_hooks("codex", codex)
    written = agent_core.hook_commands("codex", codex)
    assert all(" --definition " in c and " --config-dir " in c for c in written), written
    # Codex's hooks run outside its sandbox in the agent's environment: whatever the
    # running install, `-E` keeps its PYTHONPATH from them (review r14 of #113, 1).
    assert all(" -E -P -m aisquare hook codex" in c for c in written), written

    quiet = {event: _run(machine, event) for event in _EVENTS}

    assert {e: (r.returncode, r.stdout) for e, r in quiet.items()} == {e: (0, b"") for e in _EVENTS}
    assert machine.ran("aisquare") is None, "a hooks.json hook was run a second time"

    assert agent_core.remove_hooks("codex", codex)
    taken_over = _run(machine, "SessionStart")
    assert taken_over.stdout == b"context from the cli\n"
    assert machine.ran("aisquare") == ["hook", "codex", "--config-dir", str(codex)]


#: Shapes of our Codex hook the launcher must recognise, as hooks.json text holds them.
_OURS = [
    "{bin} hook codex",
    "{bin} hook codex --definition 4f2a",
    "{bin} hook codex --config-dir {codex} --definition 4f2a",
    "{bin} hook codex --definition 4f2a --config-dir {codex}",
    "{bin} hook codex --config-dir '{codex}/with space' --definition 4f2a",
    "{bin} hook codex --config-dir '{codex}/Jane'\"'\"'s' --definition 4f2a",
    "'{bin}' hook codex --config-dir {codex}",
    '"{bin}" hook codex',
    "{bin}\thook\tcodex",
]


@posix_only
@pytest.mark.parametrize("indent", [2, None], ids=["indented", "minified"])
@pytest.mark.parametrize("command", _OURS)
def test_it_recognises_every_shape_of_our_hook(
    machine: Machine, command: str, indent: int | None
) -> None:
    machine.fake("aisquare")
    codex = machine.home / ".codex"
    _hooks_json(codex, command.format(bin=_program(machine), codex=codex), indent=indent)

    result = _run(machine, "Stop")

    assert (result.returncode, result.stdout) == (0, b"")
    assert machine.ran("aisquare") is None


@posix_only
@pytest.mark.parametrize(
    "command",
    [
        "{bin} hook stop",  # Claude Code's, not Codex's
        "{bin} hook codexx",
        "echo {bin} hook codex",
        "{bin} webhook codex",
        "{home}/bin/my-hook codex",
        "{bin} hook codex; rm -rf /tmp/x",
    ],
)
def test_it_runs_beside_hooks_that_are_not_ours(machine: Machine, command: str) -> None:
    machine.fake("aisquare")
    codex = machine.home / ".codex"
    _hooks_json(codex, command.format(bin=_program(machine), home=machine.home))

    _run(machine, "Stop")

    assert machine.ran("aisquare") == ["hook", "codex", "--config-dir", str(codex)]


@posix_only
def test_it_runs_in_place_of_a_hook_whose_program_is_gone(machine: Machine) -> None:
    """Beside a hooks.json naming an aisquare that was uninstalled, which fails on every
    event, standing down would leave the session with none."""
    machine.fake("aisquare")
    codex = machine.home / ".codex"
    _hooks_json(codex, f"{machine.home}/gone/aisquare hook codex --definition 4f2a")

    _run(machine, "UserPromptSubmit")

    assert machine.ran("aisquare") == ["hook", "codex", "--config-dir", str(codex)]


@posix_only
def test_it_reads_the_hooks_json_of_codex_home_where_it_was_not_installed(
    machine: Machine,
) -> None:
    """A copy outside a Codex cache reads CODEX_HOME's hooks.json, else ~/.codex's."""
    machine.fake("aisquare")
    elsewhere = machine.home / "codex-2"
    _hooks_json(elsewhere, f"{_program(machine)} hook codex")
    outside = {"PLUGIN_ROOT": str(machine.home / "src")}

    pointed = _run(machine, "Stop", env={**outside, "CODEX_HOME": str(elsewhere)})
    default = _run(machine, "Stop", env=outside)

    assert (pointed.returncode, default.returncode) == (0, 0)
    assert machine.calls("aisquare") == ["hook codex"], "only ~/.codex's session runs it"


@posix_only
def test_codex_stands_down_beside_hooks_json_alone(machine: Machine) -> None:
    """Claude Code's settings.json hooks say nothing of a Codex session's."""
    machine.fake("aisquare")
    machine.settings(machine.home / ".claude", ("Stop", f"{_program(machine)} hook stop"))

    _run(machine, "Stop")

    assert machine.ran("aisquare") == [
        "hook",
        "codex",
        "--config-dir",
        str(machine.home / ".codex"),
    ]


@posix_only
def test_claude_code_stands_down_beside_settings_json_alone(machine: Machine) -> None:
    """And Codex's hooks.json says nothing of a Claude Code session's: the same script,
    run as Claude Code's plugin runs it."""
    machine.fake("aisquare")
    _hooks_json(machine.home / ".codex", f"{_program(machine)} hook codex")
    claude = machine.home / ".claude" / "plugins" / "cache" / "aisquare-cli" / "aisquare" / "1"

    _run(machine, "", argv=("stop",), env={"CLAUDE_PLUGIN_ROOT": str(claude)})

    assert machine.ran("aisquare") == ["hook", "stop"]


@posix_only
def test_with_only_uv_it_runs_the_pinned_release(machine: Machine) -> None:
    machine.fake("uvx")

    result = _run(machine, "UserPromptSubmit", stdin=b'{"prompt": "hi"}')

    assert result.returncode == 0
    assert machine.ran("uvx") == [
        "--python",
        ">=3.11,<3.14",
        "--from",
        f"aisquare-cli=={_version()}",
        "aisquare",
        "hook",
        "codex",
        "--config-dir",
        str(machine.home / ".codex"),
    ]
    assert machine.stdin_of("uvx") == b'{"prompt": "hi"}'


@posix_only
def test_uv_is_asked_at_session_start_alone(machine: Machine) -> None:
    """The answer about uv's Pythons is asked at SessionStart, Codex's session start, and
    read back on the events after it."""
    machine.fake("uvx")
    machine.fake_uv(installed=True)

    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionStart"):
        _run(machine, event)

    assert [call.split()[0] for call in machine.calls("uv")] == ["python", "python"]


@posix_only
def test_with_neither_cli_nor_uv_session_start_says_so_once(machine: Machine) -> None:
    results = {event: _run(machine, event) for event in _EVENTS}

    message = json.loads(results.pop("SessionStart").stdout)
    assert list(message) == ["systemMessage"] and "uv" in message["systemMessage"]
    assert {e: (r.returncode, r.stdout) for e, r in results.items()} == {
        e: (0, b"") for e in results
    }, "Stop wants JSON or nothing on stdout"


@posix_only
@pytest.mark.parametrize("exit_code", [1, 2, 127])
def test_a_failing_cli_never_fails_the_hook(machine: Machine, exit_code: int) -> None:
    """Exit 2 from a UserPromptSubmit, PreToolUse or Stop hook would block it."""
    machine.fake("aisquare", exit_code=exit_code)

    prompt = _run(machine, "UserPromptSubmit")
    start = _run(machine, "SessionStart")

    assert (prompt.returncode, prompt.stdout) == (0, b"")
    assert start.returncode == 0
    assert f"exit {exit_code}" in json.loads(start.stdout)["systemMessage"]


@posix_only
@pytest.mark.parametrize(
    "argv",
    [
        ("codex",),
        ("codex", ""),
        ("codex", "session-start"),
        ("codex", "Notification"),
        ("codex", "SubagentStart"),
        ("codex", "Stop; touch pwned"),
        ("SessionStart",),
        ("Codex", "Stop"),
    ],
)
def test_an_event_it_does_not_know_runs_nothing(machine: Machine, argv: tuple[str, ...]) -> None:
    machine.fake("aisquare")

    result = _run(machine, "", argv=argv)

    assert (result.returncode, result.stdout) == (0, b"")
    assert machine.ran("aisquare") is None


# --------------------------------------------------------------------------- the docs


def test_the_page_gives_the_two_commands_and_the_trust_step() -> None:
    text = " ".join(DOCS.read_text(encoding="utf-8").split())
    market = _load(MARKETPLACE)["name"]

    assert "codex plugin marketplace add AISquare-Studio/aisquare-cli" in text
    assert f"codex plugin add {agent_core.CLAUDE_PLUGIN_ID}" in text
    assert f"codex plugin remove {agent_core.CLAUDE_PLUGIN_ID}" in text
    assert f"codex plugin marketplace remove {market}" in text
    assert "/hooks" in text


def test_the_readme_offers_both_plugins() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    assert "docs/claude-code-plugin.md" in readme and "docs/codex-plugin.md" in readme


@posix_only
def test_the_launchers_aisquare_runs_without_the_sessions_python_paths(machine: Machine) -> None:
    """Review r14 of #113, finding 1: the agent's session keeps ``PYTHONPATH`` for its own
    tools, and a sandboxed agent's sandbox may write what it names (direnv's
    ``$PWD/src``); the aisquare the plugin's hooks run, outside that sandbox, gets neither
    it nor ``PYTHONHOME``."""
    seen = machine.log / "python-paths"
    machine.bin.mkdir(parents=True, exist_ok=True)
    fake = machine.bin / "aisquare"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s|%s' \"${{PYTHONPATH-unset}}\" \"${{PYTHONHOME-unset}}\" > '{seen}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    _run(machine, "SessionStart", env={"PYTHONPATH": "/tree/src", "PYTHONHOME": "/tree/home"})
    assert seen.read_text(encoding="utf-8") == "unset|unset"
