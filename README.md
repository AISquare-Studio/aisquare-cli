# aisquare

[![PyPI](https://img.shields.io/pypi/v/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)
[![CI](https://github.com/AISquare-Studio/aisquare-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/AISquare-Studio/aisquare-cli/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/AISquare-Studio/aisquare-cli/blob/main/LICENSE)

**One terminal over your projects and your coding agents: task a manager in
plain words, and it runs the coders, testers and reviewers for you.**

![The Welcome walkthrough in asq: from an empty home to a project, Claude Code connected, then a manager and two coders (the agent is a stand-in)](https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/docs/demo.gif)

Type `asq` and you get a full-screen, mouse-driven view: your projects on the
left, and on the right a **manager** agent you task in prose — it plans, spawns
coders, testers and reviewers, and loops until the goal is met. Click any of
them and you are inside its *real* Claude Code session, typing at it directly.
Nothing is relayed or re-rendered as a chat.

Underneath, agents get a **memory** that persists across sessions — your
preferences, each project's conventions — so every session starts oriented
instead of cold. The memory also works on its own, without the UI.

## Quickstart 1: memory only

Needs [uv](https://docs.astral.sh/uv/) and
[Claude Code](https://claude.com/claude-code); no installer, no tmux. Node 22+
is optional and adds the codebase snapshot. Run `init` inside your repo:

```sh
uv tool install --python 3.13 aisquare-cli
cd path/to/your/repo
aisquare init --local --agent claude-code
aisquare remember --user "prefer pytest over unittest"
```

`init` registers the repo you run it in and wires Claude Code's hooks, so every
new session starts with what you told it to remember. `aisquare context list`
shows what is in scope;
[Memory](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/memory.md)
has the rest. To install from inside Claude Code instead, see
[the Claude Code plugin](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/claude-code-plugin.md).

## Quickstart 2: the full fleet

One line. It works out what your machine already has, installs only what is
missing, and ends by offering to open the UI:

```sh
curl -fsSL https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.sh | sh
```

macOS, Linux and WSL2. It installs uv, a Python 3.13 for the CLI alone,
`aisquare-cli`, tmux, gh, git, Node and Claude Code, then registers the git repo
you ran it from and wires Claude Code's hooks. Running it again is a no-op.
[Install](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/install.md)
has the PowerShell line that sets up WSL2 on Windows, how to read the script
before you run it, and how to install by hand.

Then `asq` opens on Welcome, three steps to a running fleet:

1. **Project:** use the folder you are in, or choose another.
2. **Claude Code:** connect it. If it is missing, Welcome names the install
   command; if it is signed out, it offers the sign-in.
3. **Fleet:** press *Start manager*, then *Open the manager* and answer Claude
   Code's one-time question about trusting the folder, then *Start the coders*:
   a manager and two coders, each a real Claude Code session. **Type your goal
   to the manager in prose.**

Click any agent to see and drive its session. **Press `F12`** to hand focus
back to the sidebar, where `w` returns to Welcome and `q` quits. **The agents
keep running**; reopen `asq` and it re-attaches to what it finds.

The manager never writes code and never merges — a human does that.

## Everything is also a command

Everything the UI does is also a plain command, and every one takes `--json`:

```sh
aisquare fleet ls                      # this project's agents and their live state
aisquare fleet attach                  # the same session in raw tmux, full fidelity
aisquare doctor                        # is everything wired? (and how to fix anything)
```

## What stays free

Everything that runs on your machine — memory, the fleet, the board — is MIT
licensed, needs no account, and stays free. It's a single CLI, local-first,
backed by one SQLite file: no daemon, no cloud dependency. Sign in with
`aisquare login` only when a command needs to act as you on AISquare.

## Requirements

- macOS, Linux, or Windows through WSL2.
- Python 3.11+ if you install by hand (the one-liner brings its own 3.13).
- [Claude Code](https://claude.com/claude-code) for the agents, and tmux 3.2+
  for the fleet.
- Node 22+, optional, for the codebase snapshot each session starts from.

The package is `aisquare-cli`; the command is `aisquare`, with `asq` as the
short alias.

## Documentation

| Page | What is in it |
| --- | --- |
| [Install](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/install.md) | The one-liner's flags and exit codes, reading it before you run it, installing by hand, Windows, starting the UI, updating and removing it |
| [Memory](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/memory.md) | Your agent remembers preferences and project conventions, and starts every session oriented. For everyone; nothing to run after setup |
| [The fleet](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/fleet.md) | The UI, the roles, `aisquare fleet …`, Claude accounts, and every default you can change |
| [Orchestration](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/orchestration.md) | Several agent sessions work one problem as a team, with a shared task board. Opt-in, per repo |
| [Reference](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/reference.md) | The hooks, the `~/.aisquare` layout, the main command tree and the global flags |
| [The AISquare platform](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/platform.md) | Optional: signing in, Explainability, the Collective Intelligence test bed |

## Community

Questions and ideas go to
[Discussions](https://github.com/AISquare-Studio/aisquare-cli/discussions), bugs
to [issues](https://github.com/AISquare-Studio/aisquare-cli/issues). Pull
requests are welcome:
[CONTRIBUTING](https://github.com/AISquare-Studio/aisquare-cli/blob/main/CONTRIBUTING.md)
has the setup and the one command CI runs. Releases are in the
[changelog](https://github.com/AISquare-Studio/aisquare-cli/blob/main/CHANGELOG.md);
to report a vulnerability, see
[SECURITY](https://github.com/AISquare-Studio/aisquare-cli/blob/main/SECURITY.md).

## License

[MIT](https://github.com/AISquare-Studio/aisquare-cli/blob/main/LICENSE)
