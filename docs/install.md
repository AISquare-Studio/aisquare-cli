# Install

The [README](../README.md) has the two quickstarts. This page has the rest:
what the one-line installer does and how to read it before you run it,
installing by hand, Windows, and the first run in the UI.

## The one-line installer

One line. It works out what your machine already has, installs only what is
missing, and ends by offering to open the UI:

```sh
curl -fsSL https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.sh | sh
```

macOS, Linux and WSL2. It installs [uv](https://docs.astral.sh/uv/), a Python
3.13 for the CLI alone, `aisquare-cli`, tmux, gh, git, Node and
[Claude Code](https://claude.com/claude-code), then registers the git repo you
ran it from and wires Claude Code's hooks. Running it again is a no-op: it
reports what is current and installs nothing.

On **Windows**, everything runs inside WSL2 — the UI gives each agent a real
tmux pane and Windows has no tmux. This does both steps for you, in PowerShell:

```text
irm https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.ps1 | iex
```

<details>
<summary><b>Piping a script into a shell, and how not to</b></summary>

Fair. Read it first, or skip it entirely — nothing here needs it.

```sh
# See exactly what it would do, and run none of it:
curl -fsSL https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.sh -o install.sh
less install.sh
sh install.sh --dry-run
```

Or install by hand, which stays fully supported:

```sh
uv tool install --python 3.13 --with tiktoken aisquare-cli   # or: pipx install aisquare-cli
aisquare init --local --yes --agent claude-code
```

Useful flags — note the `-s --`, since `sh` is reading the script on stdin:

```sh
curl -fsSL .../install.sh | sh -s -- --yes --no-agent
```

| Flag | Does |
| --- | --- |
| `--yes` | Never prompt, and do not open the UI at the end. For CI and Dockerfiles. |
| `--dry-run` | Print every command, run none. |
| `--no-agent` | Skip Claude Code. |
| `--no-system-deps` | Skip tmux, gh, git and Node. |
| `--project DIR` | Register `DIR` instead of the current directory. |
| `--no-project` | Set up the machine, register nothing. |
| `--offline` | Do not ask PyPI what the latest version is. |
| `--version V` | Pin `aisquare-cli` to `V`. |

It refuses to run as root outside a container, uses `sudo` only for the system
packages and one command at a time, and never edits your shell profile beyond
what uv and the Claude Code installer do themselves. Exit codes: `0` installed,
`1` a fatal step failed, `2` installed but a health check is unexpectedly amber.

</details>

Requires **Python 3.11+** if you install by hand (the one-liner brings its own
3.13). The package is `aisquare-cli`; the command is `aisquare`, with `asq` as
the short alias.

Then check the machine at any time:

```sh
aisquare doctor
```

It reports every dependency and gives the exact command for anything missing.
`gbrain` staying amber is expected — long-term memory is optional
([Orchestration](orchestration.md#long-term-memory-optional-via-gbrain)).

## Start the GUI

The installer offers this at the end; if you skipped it:

```sh
asq                                    # open the UI
```

Installed by hand? Wire the hooks the UI reads state from, once:

```sh
aisquare agents connect claude-code
asq
```

That's the whole setup. From inside the UI:

1. **Click `+` beside Fleet** and point it at a directory. It registers the
   project and runs a health check in the background, streaming the log — you
   never leave the UI. The project appears in the navigator on the left.
2. **Click the project**, then press *Start manager*. Its live Claude Code
   session fills the pane. **Type your goal in prose**, exactly as you would to
   any Claude session.
3. **Watch the agents appear** under the project, each with a role icon
   (🧭 manager · 🔨 coder · 🧪 tester · 🌐 ui-tester · 👀 reviewer · 🛡 validator) and a live
   state chip — **▶ working**, **⏸ waiting**, **🔔 NEEDS YOU**, **💤 exited**.
   Click one to see and drive its session.
4. **Press `F12`** to hand focus back to the sidebar — the pane swallows only it
   and the scroll keys (shift/alt+PgUp/PgDn, shift+Home/End). There, `t` picks a theme and `q` quits. **The agents keep
   running**; reopen `asq` and it re-attaches to what it finds.

The manager never writes code and never merges — a human does that.

Everything the UI does is also a plain command, and every one takes `--json`:

```sh
aisquare fleet ls                      # this project's agents and their live state
aisquare fleet attach                  # the same session in raw tmux, full fidelity
aisquare doctor                        # is everything wired? (and how to fix anything)
```

Scripts never meet a full-screen app: bare `aisquare` in a pipe, or under
`TERM=dumb`, prints usage and exits 2 exactly as before, and under `--json` it
prints one usage object so a `jq` pipeline gets JSON rather than a help page.

**[The fleet guide](fleet.md)** has the roles in full, the
`aisquare fleet …` command reference and every default you can change.
