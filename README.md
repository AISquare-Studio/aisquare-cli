# rlx

[![PyPI](https://img.shields.io/pypi/v/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)
[![CI](https://github.com/AISquare-Studio/aisquare-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/AISquare-Studio/aisquare-cli/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/AISquare-Studio/aisquare-cli/blob/main/LICENSE)

**One terminal over your projects and your coding agents: task a manager in
plain words, and it runs the coders, testers and reviewers for you.**

rlx is short for relax: a tool made by a developer, for developers. Type `rlx`
and you get one full-screen, mouse-driven view of your projects and their agents.

- **One simple place for your coding agents.** Claude Code and Codex (beta),
  across all your projects. Click any agent and you are inside its *real*
  session, typing at it directly; nothing is relayed or re-rendered as a chat.
- **Talk to the manager, and it takes care of the rest.** Tell it the goal in
  plain words. It plans the work, starts the coders, testers and reviewers, and
  keeps going until the goal is met. It never writes code and never pushes, and
  it merges a PR only when a person approved it on GitHub and every CI check on
  its head is green.
- **Account usage, managed for you.** Sign in several accounts per agent. Each
  new agent starts on an account with room left, and an agent whose account
  hits its usage limit is moved to another one and carries on. Plans with only
  a weekly limit count too.
- **Memory that carries across sessions.** Your preferences and each project's
  conventions reach every new session, so it starts oriented instead of cold.
- **Everything is also a command**, for your scripts and for the agents.
- **Local-first.** Your memory, boards and fleet live on your machine, in a
  SQLite store under `~/.aisquare`. rlx runs no daemon (the agents run in a tmux
  server) and needs no cloud service of its own.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.sh | sh
```

On macOS, Linux and WSL2, that one line works out what your machine already
has and installs only what is missing: uv, a Python 3.13 of its own, rlx, tmux,
gh, git and Claude Code, and Node for browser tools if you want them. It
registers the git repo you ran it from, wires your agent's hooks so every
session gets its memory, and ends by offering to open rlx. Running it again is a
no-op. For Codex too, end the line with `| sh -s -- --agent both` instead of
`| sh` (`| sh -s -- --agent codex` for Codex alone). To read it before you run
it, it is `install.sh` at the root of this repository.

That installs the latest release. To pin one, every release from 0.9.0 on
carries its own copy: `curl -fsSL https://github.com/AISquare-Studio/aisquare-cli/releases/download/v0.10.0/install.sh | sh`.

On Windows, run `irm https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.ps1 | iex`
in PowerShell: it runs that installer inside your WSL2 distribution, or prints the
one command that installs WSL2 if there is none.

The package is `aisquare-cli`. Its command is `rlx`; `relax`, `asq` and
`aisquare` start it too.

## Get started

`rlx` opens on Welcome, three steps to a running fleet:

1. **Project:** use the folder you are in, or choose another.
2. **Coding agent:** Claude Code, or Codex (beta). Connect it: if it is missing,
   Welcome names the install command, and if it is signed out, it offers the
   sign-in.
3. **Fleet:** press *Start manager*, then *Open the manager* and answer the
   agent's one-time question about trusting the folder, then *Start the
   coders*: a manager and two coders, each a real session. **Type your goal to
   the manager in plain words.**

Click any agent to see and drive its session. **Press `F12`** to hand focus
back to the sidebar, where `w` returns to Welcome and `q` quits. **The agents
keep running**; open `rlx` again and it re-attaches to what it finds. To stop
them spending tokens without losing them, *⚙ Settings ▸ Fleet ▸ Pause all…*
exits each one and keeps its task, worktree and session; *Resume all…* brings
each back where it was.

## Accounts

Each account signs in through its agent's own login, and *⚙ Settings ▸
Accounts* shows them all with their usage:

```sh
rlx accounts add                  # a Claude Code account
rlx accounts add --agent codex    # a Codex account, signed in with a device code
rlx accounts order 2 1            # the order a new agent's account is picked in
rlx accounts usage                # each account's five-hour and weekly use
```

A new agent starts on the first account in that order that is under 85% used,
or on the one with the most room when none is. An agent that hits a limit moves
to the first such account and resumes its session there, unless the limit
lifts within 15 minutes. When all of its own agent's accounts are full, it can
start fresh on the other agent's, with a brief of the work.

## Over SSH

You can open `rlx` over SSH into the same Mac while the Mac's own is open: both
show the same fleet, and a line in the sidebar says where each one runs. If your
fleet runs Claude Code, start its first agent at the Mac: on macOS Claude Code's
login is in the Keychain, which no agent in a tmux server started over SSH can
read. Codex's login is a file, so a fleet of Codex agents can start over SSH.
A Codex account signs in with a device code, so `rlx accounts add --agent codex`
works over SSH too: open the link it prints on any device and enter the code
(`--browser` signs in with this machine's browser instead).
[Running more than one rlx](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/multi-instance.md)
has the rest, including what happens when the two run different releases.

## Memory on its own

Memory works without the fleet. It needs [uv](https://docs.astral.sh/uv/) and
[Claude Code](https://claude.com/claude-code); no installer, no tmux. Run `init`
inside your repo:

```sh
UV_PYTHON_DOWNLOADS=automatic uv tool install --python 3.13 aisquare-cli
cd path/to/your/repo
rlx init --local --agent claude-code
rlx remember --user "prefer pytest over unittest"
```

`init` registers the repo you run it in and wires Claude Code's hooks, so every
new session starts with what you told it to remember. `rlx context list` shows
what is in scope. To install from inside Codex instead, see
[the Codex plugin](https://github.com/AISquare-Studio/aisquare-cli/blob/main/plugins/codex/README.md).

## Everything is also a command

Everything rlx shows you is also a plain command, and every one takes `--json`;
`rlx --help` lists them:

```sh
rlx fleet ls                         # this project's agents and their live state
rlx fleet top                        # what each live agent consumes: CPU, memory, tokens
rlx fleet relabel coder-1 login fix  # give an agent a name you chose: login-fix
rlx fleet pause --all                # every project's agents stop, kept for resume
rlx fleet resume --all               # ...and come back where they were
rlx fleet attach                     # the same session in raw tmux, full fidelity
rlx instances                        # every rlx open on this home: at the Mac, over SSH
rlx outcomes                         # what the agents got done this week, and who steered
rlx doctor                           # is everything wired? (and how to fix anything)
```

## Requirements

- macOS, Linux, or Windows through WSL2.
- Python 3.11+ if you install by hand (the one-liner brings its own 3.13).
- [Claude Code](https://claude.com/claude-code) or
  [Codex](https://learn.chatgpt.com/docs/codex/cli) (beta; 0.164.0 or newer,
  tested on 0.164.0, 0.166.0 and 0.167.0) for the agents, either or both, and
  tmux 3.2+ for the fleet.
- Node 22.12+, optional, for browser tools like the Chrome DevTools MCP; the one-liner
  offers both, Node and the MCP registered with Claude Code (`--with chrome-devtools`
  installs them without asking). rlx itself never runs Node.
- The symbol index (`rlx code find|refs|similar`) needs nothing extra for Python.

## Documentation

| Page | What is in it |
| --- | --- |
| [Usage sharing](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/usage-telemetry.md) | Opt-in, anonymous daily counts of how rlx is used: what is collected, what never is, how to see exactly what would be sent, and how to turn it off |

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
