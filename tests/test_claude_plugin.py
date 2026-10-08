"""The Claude Code plugin route: the marketplace, the plugin, its hooks and its launcher.

``/plugin marketplace add AISquare-Studio/aisquare-cli``, then ``/plugin install
aisquare@aisquare-cli``, gives Claude Code the six lifecycle hooks ``aisquare agents
connect claude-code`` writes into settings.json, with no installer. Each one runs
``plugins/claude-code/scripts/aisquare-hook``, a POSIX launcher around ``aisquare hook
<event>``: the CLI on PATH, else the pinned release through uvx.

THE DEFECT A PLUGIN INVITES. Claude Code runs a plugin's hooks AND the settings.json
hooks, and never dedupes across the two. With both routes on one machine every session
would get its context twice and every prompt would be captured twice (measured in the
9.3 investigation: 2 prompt rows and 2 metric rows per prompt). So the launcher stands
down for any event settings.json already runs, and the tests below hand it the
settings.json ``agents connect`` really writes, then every shape one has been written in.

The static guards (the version, the manifests, hooks.json) each have negative controls
(CONTRIBUTING, "Writing a guard that still guards"). The launcher is run for real under
``sh`` -- dash on Ubuntu CI -- with fake ``aisquare`` and ``uvx`` on a PATH that holds
nothing else, so those tests skip on Windows, where Claude Code would need Git Bash to
run it at all.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import agents as agent_core
from tests.fsperms import can_deny_reads

REPO = Path(__file__).resolve().parents[1]
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
PLUGIN = REPO / "plugins" / "claude-code"
PLUGIN_JSON = PLUGIN / ".claude-plugin" / "plugin.json"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"
LAUNCHER = PLUGIN / "scripts" / "aisquare-hook"
PYPROJECT = REPO / "pyproject.toml"

_PIN = re.compile(r"^AISQUARE_PLUGIN_VERSION=(\S+)$", re.MULTILINE)
_COMMAND = 'sh "${{CLAUDE_PLUGIN_ROOT}}/scripts/aisquare-hook" {subcommand}'
_SUBCOMMANDS = [subcommand for _, subcommand in agent_core._HOOKS]
_SH = shutil.which("sh")

posix_only = pytest.mark.skipif(
    sys.platform == "win32" or _SH is None,
    reason="the launcher is a POSIX sh script; Claude Code on Windows runs it only under Git Bash",
)


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _release() -> str:
    version: str = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    return version


# --------------------------------------------------------------------------- the version


def version_disagreements(*, pyproject: str, plugin_json: str, launcher: str) -> list[str]:
    """What disagrees with pyproject's ``[project].version``, one line each; empty if none.

    Three numbers, one release: plugin.json is what ``/plugin update`` compares, and the
    launcher's pin is what uvx installs when the CLI is not there. The release commit
    bumps all three, and the GitHub release must reach PyPI before the pin can resolve.
    """
    release = tomllib.loads(pyproject)["project"]["version"]
    pin = _PIN.search(launcher)
    found = {
        "plugin.json's version": json.loads(plugin_json).get("version"),
        "the launcher's AISQUARE_PLUGIN_VERSION": pin.group(1) if pin else None,
    }
    return [
        f"{where} is {value!r}, pyproject.toml says {release!r}"
        for where, value in found.items()
        if value != release
    ]


def test_the_plugin_ships_the_release_it_pins() -> None:
    disagreements = version_disagreements(
        pyproject=PYPROJECT.read_text(encoding="utf-8"),
        plugin_json=PLUGIN_JSON.read_text(encoding="utf-8"),
        launcher=LAUNCHER.read_text(encoding="utf-8"),
    )
    assert disagreements == [], "the release commit bumps all three together"


def test_a_version_out_of_step_is_named() -> None:
    """Each of the three files can be the odd one out, and each is named when it is."""
    pyproject, plugin, launcher = (
        '[project]\nversion = "0.9.0"\n',
        '{"version": "0.9.0"}',
        "AISQUARE_PLUGIN_VERSION=0.9.0\n",
    )
    assert version_disagreements(pyproject=pyproject, plugin_json=plugin, launcher=launcher) == []
    bumped_alone = version_disagreements(
        pyproject='[project]\nversion = "0.10.0"\n', plugin_json=plugin, launcher=launcher
    )
    assert len(bumped_alone) == 2
    stale_manifest = version_disagreements(
        pyproject=pyproject, plugin_json='{"version": "0.8.0"}', launcher=launcher
    )
    assert stale_manifest == ["plugin.json's version is '0.8.0', pyproject.toml says '0.9.0'"]
    no_pin = version_disagreements(pyproject=pyproject, plugin_json=plugin, launcher="# none\n")
    assert no_pin == ["the launcher's AISQUARE_PLUGIN_VERSION is None, pyproject.toml says '0.9.0'"]


# --------------------------------------------------------------------------- the manifests


def test_the_marketplace_lists_this_plugin_under_the_names_doctor_reads() -> None:
    """``enabledPlugins`` keys the plugin as ``<plugin>@<marketplace>``; doctor reads that key."""
    market = _load(MARKETPLACE)
    plugin = _load(PLUGIN_JSON)
    [entry] = market["plugins"]
    source = Path(entry["source"])

    assert market["name"] == agent_core.CLAUDE_PLUGIN_MARKETPLACE
    assert market["owner"]["name"]
    assert entry["name"] == plugin["name"] == agent_core.CLAUDE_PLUGIN
    assert f"{plugin['name']}@{market['name']}" == agent_core.CLAUDE_PLUGIN_ID
    assert entry["source"].startswith("./") and ".." not in source.parts
    assert (REPO / source).resolve() == PLUGIN.resolve()
    assert "version" not in entry, "plugin.json holds the version: one fewer number to bump"


# --------------------------------------------------------------------------- hooks.json


def hook_problems(hooks_json: Mapping[str, Any]) -> list[str]:
    """How a hooks.json differs from the hooks ``agents connect`` installs; empty if it does not.

    The same six events as ``core.agents._HOOKS``, each one hook that runs the launcher
    with that event's ``aisquare hook`` subcommand, and the two context hooks allowed
    ``CONTEXT_HOOK_TIMEOUT_SECONDS``, as settings.json gives them.
    """
    events = hooks_json.get("hooks")
    if not isinstance(events, dict):
        return ["no top-level `hooks` object"]
    problems = [
        f"{event}: not an event agents connect installs"
        for event in sorted(set(events) - {event for event, _ in agent_core._HOOKS})
    ]
    for event, subcommand in agent_core._HOOKS:
        groups = events.get(event)
        if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
            problems.append(f"{event}: missing, or not exactly one group")
            continue
        items = groups[0].get("hooks")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            problems.append(f"{event}: not exactly one hook")
            continue
        item = items[0]
        if item.get("type") != "command" or item.get("command") != _COMMAND.format(
            subcommand=subcommand
        ):
            problems.append(f"{event}: does not run the launcher with {subcommand}")
        timeout = item.get("timeout")
        if event in agent_core._CONTEXT_HOOKS and not (
            isinstance(timeout, int) and timeout >= agent_core.CONTEXT_HOOK_TIMEOUT_SECONDS
        ):
            problems.append(
                f"{event}: timeout {timeout!r} is under {agent_core.CONTEXT_HOOK_TIMEOUT_SECONDS}"
            )
    return problems


def test_hooks_json_runs_every_lifecycle_event_through_the_launcher() -> None:
    assert hook_problems(_load(HOOKS_JSON)) == []


def _without(event: str) -> Callable[[dict[str, Any]], None]:
    return lambda hooks: hooks["hooks"].pop(event)


def _set(event: str, key: str, value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(hooks: dict[str, Any]) -> None:
        hooks["hooks"][event][0]["hooks"][0][key] = value

    return mutate


def _unwrapped(hooks: dict[str, Any]) -> None:
    events = hooks.pop("hooks")
    hooks.clear()
    hooks.update(events)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_without("StopFailure"), "StopFailure: missing, or not exactly one group"),
        (
            _set("Stop", "command", _COMMAND.format(subcommand="stop-failure")),
            "Stop: does not run the launcher with stop",
        ),
        (_set("SessionStart", "timeout", 60), "SessionStart: timeout 60 is under 120"),
        (
            lambda hooks: hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0].pop("timeout"),
            "UserPromptSubmit: timeout None is under 120",
        ),
        (
            lambda hooks: hooks["hooks"].update(PreToolUse=hooks["hooks"]["Stop"]),
            "PreToolUse: not an event agents connect installs",
        ),
        (_unwrapped, "no top-level `hooks` object"),
    ],
    ids=["missing-event", "wrong-subcommand", "short-timeout", "no-timeout", "extra", "unwrapped"],
)
def test_the_hooks_guard_names_each_way_hooks_json_can_drift(
    mutate: Callable[[dict[str, Any]], None], expected: str
) -> None:
    broken = copy.deepcopy(_load(HOOKS_JSON))
    mutate(broken)
    assert hook_problems(broken) == [expected]


# --------------------------------------------------------------------------- the launcher


@dataclass
class Machine:
    """A HOME, a PATH holding only fakes and ``grep``, and a log of what the fakes saw."""

    home: Path
    bin: Path
    tools: Path
    log: Path
    plugin_root: Path

    def fake(
        self,
        name: str,
        *,
        where: Path | None = None,
        exit_code: int = 0,
        no_python: int | None = None,
    ) -> Path:
        """An executable ``name`` that records its argv and stdin, prints a line, and exits.

        ``no_python`` is how a uvx that finds no interpreter for ``--python`` behaves:
        it exits with that code BEFORE anything reads stdin.
        """
        directory = self.bin if where is None else where
        directory.mkdir(parents=True, exist_ok=True)
        record = self.log / name
        refuse = (
            f'case " $* " in *" --python "*) exit {no_python} ;; esac\n'
            if no_python is not None
            else ""
        )
        script = directory / name
        script.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> '{record}.calls'\n"
            f": > '{record}.argv'\n"
            f"for arg in \"$@\"; do printf '%s\\n' \"$arg\" >> '{record}.argv'; done\n"
            + refuse
            + f"'{shutil.which('cat')}' > '{record}.stdin'\n"
            + ("printf 'context from the cli\\n'\n" if exit_code == 0 else "")
            + f"exit {exit_code}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def ran(self, name: str) -> list[str] | None:
        """The argv ``name`` was last run with, or ``None`` if it never ran."""
        record = self.log / f"{name}.argv"
        return record.read_text(encoding="utf-8").splitlines() if record.exists() else None

    def calls(self, name: str) -> list[str]:
        """Every run of ``name``, its arguments space-joined, oldest first."""
        record = self.log / f"{name}.calls"
        return record.read_text(encoding="utf-8").splitlines() if record.exists() else []

    def stdin_of(self, name: str) -> bytes:
        return (self.log / f"{name}.stdin").read_bytes()

    def settings(
        self, config_dir: Path, *commands: tuple[str, str], indent: int | None = 2
    ) -> None:
        """A settings.json whose hooks are ``(event, command)`` pairs."""
        hooks: dict[str, list[dict[str, Any]]] = {}
        for event, command in commands:
            hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": command}]})
        config_dir.mkdir(parents=True, exist_ok=True)
        separators = None if indent else (",", ":")
        (config_dir / "settings.json").write_text(
            json.dumps({"hooks": hooks}, indent=indent, separators=separators), encoding="utf-8"
        )

    def run(
        self, event: str, *, stdin: bytes = b"", env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        assert _SH is not None
        base = {
            "PATH": os.pathsep.join([str(self.bin), str(self.tools)]),
            "HOME": str(self.home),
            "CLAUDE_PLUGIN_ROOT": str(self.plugin_root),
        }
        return subprocess.run(
            [_SH, str(LAUNCHER), event],
            input=stdin,
            env={**base, **(env or {})},
            capture_output=True,
            timeout=60,
            check=False,
        )


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    home = tmp_path / "home"
    tools = tmp_path / "tools"
    tools.mkdir()
    grep = shutil.which("grep")
    assert grep is not None, "the launcher needs grep, and so does every machine it runs on"
    (tools / "grep").symlink_to(grep)
    log = tmp_path / "log"
    log.mkdir()
    root = home / ".claude" / "plugins" / "cache" / "aisquare-cli" / "aisquare" / _release()
    return Machine(home=home, bin=tmp_path / "bin", tools=tools, log=log, plugin_root=root)


@posix_only
@pytest.mark.parametrize("subcommand", _SUBCOMMANDS)
def test_it_runs_the_cli_with_the_event_and_the_payload_untouched(
    machine: Machine, subcommand: str
) -> None:
    machine.fake("aisquare")
    payload = '{"cwd": "/tmp/x", "prompt": "café \\u00e9 \\"quoted\\""}\n'.encode()

    result = machine.run(subcommand, stdin=payload)

    assert result.returncode == 0, result.stderr
    assert machine.ran("aisquare") == ["hook", subcommand]
    assert machine.stdin_of("aisquare") == payload, "the launcher must never read stdin"
    assert result.stdout == b"context from the cli\n"


@posix_only
def test_it_stands_down_where_agents_connect_installed_the_hooks(machine: Machine) -> None:
    """Both routes on one machine run each hook once; disconnect hands them to the plugin."""
    claude = machine.home / ".claude"
    machine.fake("aisquare")
    assert agent_core.install_hooks("claude-code", claude)

    quiet = {subcommand: machine.run(subcommand) for subcommand in _SUBCOMMANDS}

    assert {sub: (r.returncode, r.stdout) for sub, r in quiet.items()} == {
        sub: (0, b"") for sub in _SUBCOMMANDS
    }
    assert machine.ran("aisquare") is None, "a settings.json hook was run a second time"

    assert agent_core.remove_hooks("claude-code", claude)
    taken_over = machine.run("session-start")
    assert taken_over.stdout == b"context from the cli\n"
    assert machine.ran("aisquare") == ["hook", "session-start"]


def _programs(machine: Machine) -> Path:
    """Executables the settings.json hooks below name: they exist, so the launcher
    stands down beside them (it does not beside a program that is gone)."""
    programs = machine.home / "programs"
    for relative in ("aisquare", "asq", "python3", "My Tools/aisquare"):
        script = programs / relative
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    return programs


#: Every shape ``agents connect`` has written a hook in, and the hand-edited ones the
#: CLI's own matcher accepts, as (command, is it a POSIX shape ``core.agents``
#: recognises here). ``{bin}`` is :func:`_programs`. The JSON escaping of each is real:
#: the test writes them with ``json.dumps``, which turns a tab into ``\t``.
_OURS = [
    ("{bin}/aisquare hook stop", True),
    ("'{bin}/My Tools/aisquare' hook stop", True),
    ("{bin}/python3 -P -m aisquare hook stop", True),
    ("{bin}/python3 -m aisquare hook stop", True),
    ("aisquare hook stop", True),
    ("{bin}/asq hook stop", True),
    ("{bin}/aisquare hook stop ", True),
    ("{bin}/aisquare\thook\tstop", True),
    ("{bin}/aisquare  hook  stop", True),
    ("{bin}/aisquare --no-color hook stop", True),
    (r"C:\Users\u\.local\bin\aisquare.exe hook stop", False),
    (r'"C:\Program Files\aisquare\bin\aisquare.EXE" hook stop', False),
]


@posix_only
@pytest.mark.parametrize("indent", [2, None], ids=["indented", "minified"])
@pytest.mark.parametrize(("command", "posix_shape"), _OURS)
def test_it_recognises_every_shape_of_our_hook(
    machine: Machine, command: str, posix_shape: bool, indent: int | None
) -> None:
    machine.fake("aisquare")
    command = command.format(bin=_programs(machine))
    machine.settings(machine.home / ".claude", ("Stop", command), indent=indent)

    result = machine.run("stop")

    assert result.returncode == 0
    assert machine.ran("aisquare") is None, f"ran beside settings.json's {command!r}"
    if posix_shape:
        assert agent_core._is_aisquare_hook_command(command), "the CLI must agree it is ours"


@posix_only
@pytest.mark.parametrize(
    "command",
    [
        "webhook stop",
        "~/bin/my-hook stop",
        "/opt/aisquare-tools/notify hook stop",
        "/x/notaisquare hook stop",
        "echo aisquare hook stop",
        "{bin}/aisquare hook stop-failure",
        "{bin}/aisquare hook session-end",
    ],
)
def test_it_runs_beside_hooks_that_are_not_ours_for_this_event(
    machine: Machine, command: str
) -> None:
    """A user's own hook, or ours for ANOTHER event, must not silence this one."""
    machine.fake("aisquare")
    command = command.format(bin=_programs(machine))
    machine.settings(machine.home / ".claude", ("Stop", command))

    result = machine.run("stop")

    assert result.returncode == 0
    assert machine.ran("aisquare") == ["hook", "stop"]
    assert not (agent_core._is_aisquare_hook_command(command) and command.endswith(" stop"))


@posix_only
@pytest.mark.parametrize(
    ("command", "runner_in_local_bin"),
    [
        ("{gone}/aisquare hook stop", False),
        ("'{gone}/My Tools/aisquare' hook stop", False),
        # A bare name the hook's shell would not find: the CLI is on no PATH, only in
        # ~/.local/bin, where the launcher still finds it.
        ("aisquare hook stop", True),
    ],
    ids=["gone", "gone-quoted", "bare-not-on-path"],
)
def test_it_runs_in_place_of_a_hook_whose_program_is_gone(
    machine: Machine, command: str, runner_in_local_bin: bool
) -> None:
    """Hooks from `agents connect`, then the CLI uninstalled: those hooks fail on every
    event, so standing down beside them would leave the session running no aisquare."""
    where = machine.home / ".local" / "bin" if runner_in_local_bin else None
    machine.fake("aisquare", where=where)
    command = command.format(gone=machine.home / "uninstalled")
    machine.settings(machine.home / ".claude", ("Stop", command))

    result = machine.run("stop")

    assert result.returncode == 0
    assert machine.ran("aisquare") == ["hook", "stop"]


@posix_only
@pytest.mark.parametrize("indent", [2, None], ids=["indented", "minified"])
@pytest.mark.parametrize(
    ("hooks", "runs"),
    [
        # A user's hook that cannot start, then a live one of ours: settings.json runs
        # aisquare, so the launcher must not run it a second time.
        (("{gone}/notify-send done", "{bin}/aisquare hook stop"), False),
        # A user's hook that can start, then ours naming an uninstalled CLI: no aisquare
        # runs from settings.json, so the launcher must.
        (("{bin}/python3 notify.py", "{gone}/aisquare hook stop"), True),
        # Ours twice, the first gone: the second still runs aisquare.
        (("{gone}/aisquare hook stop", "{bin}/aisquare hook stop"), False),
    ],
    ids=["dead-hook-then-ours", "live-hook-then-gone-ours", "gone-ours-then-live-ours"],
)
def test_it_grades_the_program_in_our_hook_not_the_first_on_its_line(
    machine: Machine, hooks: tuple[str, str], runs: bool, indent: int | None
) -> None:
    """A minified settings.json (``jq -c``, Nix, Ansible) has every hook on one line, and
    the program graded was the first ``"command"`` on it: the event fired twice beside a
    live hook of ours, or never beside a dead one (review of #257)."""
    machine.fake("aisquare")
    where = {"bin": _programs(machine), "gone": machine.home / "uninstalled"}
    machine.settings(
        machine.home / ".claude", *(("Stop", hook.format(**where)) for hook in hooks), indent=indent
    )

    result = machine.run("stop")

    assert result.returncode == 0
    assert machine.ran("aisquare") == (["hook", "stop"] if runs else None)


def _cannot_start(path: Path) -> Path:
    """An executable script whose ``#!`` interpreter is gone: a console script after its
    environment lost its Python (``uv python uninstall``, a removed ``python@3.x``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{path.parent / 'gone' / 'python'}\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@posix_only
def test_it_runs_in_place_of_a_hook_whose_script_cannot_start(machine: Machine) -> None:
    """Executable is not runnable: a script whose interpreter is gone fails every event
    with exit 126, and the launcher stood down beside it (review of #257)."""
    machine.fake("aisquare")
    dead = _cannot_start(machine.home / "venv" / "bin" / "aisquare")
    machine.settings(machine.home / ".claude", ("Stop", f"{dead} hook stop"))
    with pytest.raises(OSError):  # the premise: exec refuses it (a bad interpreter)
        subprocess.run([str(dead)], capture_output=True, check=False)

    result = machine.run("stop")

    assert result.returncode == 0
    assert machine.ran("aisquare") == ["hook", "stop"]


@posix_only
@pytest.mark.parametrize("good_in_local_bin", [False, True], ids=["to-uvx", "to-local-bin"])
def test_an_aisquare_that_cannot_start_is_passed_over(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, good_in_local_bin: bool
) -> None:
    """The first aisquare on PATH can be a script that cannot start; picked, it failed
    every event instead of reaching ~/.local/bin or the uvx fallback. The doctor's search
    (``agent_core.launcher_finds``) passes it over the same way, or the row names a
    program the plugin never runs (review of #257)."""
    dead = _cannot_start(machine.bin / "aisquare")
    local = machine.home / ".local" / "bin"
    good = machine.fake("aisquare", where=local) if good_in_local_bin else None
    machine.fake("uvx")
    monkeypatch.setenv("PATH", os.pathsep.join([str(machine.bin), str(machine.tools)]))
    monkeypatch.setattr(agent_core, "_home", lambda: machine.home)

    result = machine.run("stop")
    doctor_sees = agent_core.plugin_runner()

    assert result.returncode == 0
    expected_uvx = ["--python", ">=3.11,<3.14", "--from", f"aisquare-cli=={_release()}"]
    expected_uvx += ["aisquare", "hook", "stop"]
    ran = (machine.ran("aisquare"), machine.ran("uvx"))
    assert ran == ((["hook", "stop"], None) if good else (None, expected_uvx)), ran
    assert doctor_sees == good and doctor_sees != dead, doctor_sees


@posix_only
def test_a_script_whose_interpreter_this_user_cannot_reach_is_passed_over(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doctor's search asked Path.is_file of a #! path in a directory this user cannot
    enter, which raises on 3.11/3.12; the launcher's `[ -f ]` answers no, and so does the
    doctor now (review of #257)."""
    if not can_deny_reads():
        pytest.skip("needs a directory this user cannot enter")
    locked = machine.home / "someone-else"
    interpreter = locked / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o755)
    script = machine.bin / "aisquare"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!{interpreter}\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    machine.fake("uvx")
    monkeypatch.setenv("PATH", os.pathsep.join([str(machine.bin), str(machine.tools)]))
    monkeypatch.setattr(agent_core, "_home", lambda: machine.home)
    locked.chmod(0)
    try:
        doctor_sees = agent_core.plugin_runner()
        result = machine.run("stop")
    finally:
        locked.chmod(0o700)

    assert result.returncode == 0
    assert doctor_sees is None, doctor_sees
    assert machine.ran("uvx") is not None, "the launcher passed it over the same way"


@posix_only
def test_it_reads_the_settings_json_the_session_reads(machine: Machine) -> None:
    """CLAUDE_CONFIG_DIR, else ~/.claude -- and the config dir the plugin is installed in."""
    alt = machine.home / ".claude-c2"
    in_alt = alt / "plugins" / "cache" / "aisquare-cli" / "aisquare" / _release()
    machine.fake("aisquare")
    ours = ("Stop", f"{_programs(machine)}/aisquare hook stop")

    machine.settings(alt, ours)
    machine.settings(machine.home / ".claude")
    pointed = machine.run("stop", env={"CLAUDE_CONFIG_DIR": str(alt), "CLAUDE_PLUGIN_ROOT": ""})
    from_the_root = machine.run("stop", env={"CLAUDE_PLUGIN_ROOT": str(in_alt)})
    assert machine.ran("aisquare") is None, "missed the hooks in the session's config dir"

    machine.settings(alt)
    machine.settings(machine.home / ".claude", ours)
    elsewhere = machine.run(
        "stop", env={"CLAUDE_CONFIG_DIR": str(alt), "CLAUDE_PLUGIN_ROOT": str(in_alt)}
    )

    assert (pointed.returncode, from_the_root.returncode, elsewhere.returncode) == (0, 0, 0)
    assert machine.ran("aisquare") == ["hook", "stop"], (
        "~/.claude's hooks do not run in a session that reads ~/.claude-c2"
    )


@posix_only
def test_without_the_cli_on_path_it_finds_local_bin(machine: Machine) -> None:
    """Where ``uv tool install`` puts it: a session from a desktop app may lack it on PATH."""
    machine.fake("aisquare", where=machine.home / ".local" / "bin")
    machine.fake("uvx")

    result = machine.run("notification")

    assert result.returncode == 0
    assert machine.ran("aisquare") == ["hook", "notification"]
    assert machine.ran("uvx") is None


@posix_only
@pytest.mark.parametrize("uvx_in_local_bin", [False, True], ids=["on-path", "local-bin"])
def test_with_only_uv_it_runs_the_pinned_release(machine: Machine, uvx_in_local_bin: bool) -> None:
    machine.fake("uvx", where=machine.home / ".local" / "bin" if uvx_in_local_bin else None)

    result = machine.run("user-prompt-submit", stdin=b'{"prompt": "hi"}')

    assert result.returncode == 0
    assert machine.ran("uvx") == [
        "--python",
        ">=3.11,<3.14",
        "--from",
        f"aisquare-cli=={_release()}",
        "aisquare",
        "hook",
        "user-prompt-submit",
    ]
    assert machine.stdin_of("uvx") == b'{"prompt": "hi"}'


@posix_only
def test_where_uv_has_no_tested_python_it_runs_on_what_uv_has(machine: Machine) -> None:
    """Fedora's packaged uv will not fetch an interpreter, and its only Python may be newer.

    Measured on Fedora 44: Python 3.14 only, ``python-downloads = "manual"`` in
    /etc/uv/uv.toml, and ``uvx --python '>=3.11,<3.14'`` exits 2 -- every session
    started with no memory. Retrying without the range runs it on 3.14.
    """
    machine.fake("uvx", no_python=2)
    pin = f"aisquare-cli=={_release()}"

    result = machine.run("session-start", stdin=b'{"cwd": "/tmp/x"}')

    assert result.returncode == 0
    assert machine.calls("uvx") == [
        f"--python >=3.11,<3.14 --from {pin} aisquare hook session-start",
        f"--from {pin} aisquare hook session-start",
    ]
    assert machine.stdin_of("uvx") == b'{"cwd": "/tmp/x"}', "the retry gets the payload"
    assert result.stdout == b"context from the cli\n", "no failure message when the retry ran"


@posix_only
def test_when_uvx_cannot_run_it_at_all_session_start_says_so(machine: Machine) -> None:
    """The pinned release not on PyPI yet, or no network on a cold cache: one message."""
    machine.fake("uvx", exit_code=1)

    result = machine.run("session-start")

    assert result.returncode == 0
    assert len(machine.calls("uvx")) == 2
    assert "exit 1" in json.loads(result.stdout)["systemMessage"]


@posix_only
def test_a_build_that_is_not_on_pypi_can_stand_in_for_the_pin(machine: Machine) -> None:
    machine.fake("uvx")
    wheel = "/wheels/aisquare_cli-9.9.9-py3-none-any.whl"

    machine.run("stop", env={"AISQUARE_PLUGIN_FROM": wheel})

    argv = machine.ran("uvx") or []
    assert argv[argv.index("--from") + 1] == wheel


@posix_only
def test_with_neither_cli_nor_uv_session_start_says_so_once(machine: Machine) -> None:
    results = {subcommand: machine.run(subcommand) for subcommand in _SUBCOMMANDS}

    message = json.loads(results.pop("session-start").stdout)
    assert list(message) == ["systemMessage"]
    assert "uv" in message["systemMessage"]
    assert {sub: (r.returncode, r.stdout) for sub, r in results.items()} == {
        sub: (0, b"") for sub in results
    }


@posix_only
@pytest.mark.parametrize("exit_code", [1, 2, 127])
def test_a_failing_cli_never_fails_the_hook(machine: Machine, exit_code: int) -> None:
    """Exit 2 from a UserPromptSubmit or Stop hook would block the prompt or the stop."""
    machine.fake("aisquare", exit_code=exit_code)

    prompt = machine.run("user-prompt-submit")
    start = machine.run("session-start")

    assert (prompt.returncode, prompt.stdout) == (0, b"")
    assert start.returncode == 0
    assert f"exit {exit_code}" in json.loads(start.stdout)["systemMessage"]


@posix_only
@pytest.mark.parametrize("event", ["pre-tool-use", "", "stop; touch pwned"])
def test_an_event_it_does_not_know_runs_nothing(machine: Machine, event: str) -> None:
    machine.fake("aisquare")

    result = machine.run(event)

    assert (result.returncode, result.stdout) == (0, b"")
    assert machine.ran("aisquare") is None
