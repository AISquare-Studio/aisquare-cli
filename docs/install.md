# Install

The [README](../README.md) has the two quickstarts. This page has the rest:
what the one-line installer does and how to read it before you run it,
installing by hand, Windows, and starting the UI.

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

Claude Code is the compatibility default. For Codex (CLI 0.162.1 or newer,
the release CI tests), add `--agent codex` (`--agent both` for the two
together):

```sh
curl -fsSL https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.sh | sh -s -- --agent codex
```

A missing Codex is installed with its own installer, the route its
[docs](https://learn.chatgpt.com/docs/codex/cli) give
(`curl -fsSL https://chatgpt.com/codex/install.sh | sh`); one already there is
kept, and `--upgrade-all` runs `codex update`. `aisquare init --agent codex`
merges aisquare's hooks into `$CODEX_HOME/hooks.json` (`~/.codex` by default),
and with Codex alone, `asq agents use codex` makes it the agent launches use.
One step is yours, and the summary names it: run `codex`, sign in if it asks,
open `/hooks` and trust aisquare's hooks. Codex runs no hook it has not been
told to trust, and nothing installs that trust for you. On an existing
installation, `asq agents connect codex` and `asq agents use codex` do the
same. Each further Codex login is a numbered account (`aisquare accounts add
--agent codex`, see [Codex accounts](coding-agents.md#codex-accounts)), and
Codex can take the hooks from [the Codex plugin](codex-plugin.md) instead.
[Coding-agent setup, mixed teams and compatibility](coding-agents.md).

On **Windows**, everything runs inside WSL2 — the UI gives each agent a real
tmux pane and Windows has no tmux. This does both steps for you, in PowerShell:

```text
irm https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.ps1 | iex
```

`irm … | iex` passes no options, so name the agent in an environment variable
first, which becomes `--agent` inside WSL (`claude-code`, `codex` or `both`):

```text
$env:AISQUARE_INSTALL_AGENT = 'codex'; irm https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.ps1 | iex
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
UV_PYTHON_DOWNLOADS=automatic uv tool install --python 3.13 --with tiktoken aisquare-cli   # or: pipx install aisquare-cli
aisquare init --local --yes --agent claude-code     # or --agent codex, or both: --agent claude-code --agent codex
```

Useful flags — note the `-s --`, since `sh` is reading the script on stdin:

```sh
curl -fsSL .../install.sh | sh -s -- --yes --no-agent
```

| Flag | Does |
| --- | --- |
| `--yes` | Never prompt, and do not open the UI at the end. For CI and Dockerfiles. |
| `--dry-run` | Print every command, run none. |
| `--agent NAME` | `claude-code` (default), `codex` or `both`. Repeat it to name both. |
| `--no-agent` | Skip coding agent installation and hooks. |
| `--no-system-deps` | Skip tmux, gh, git and Node. |
| `--project DIR` | Register `DIR` instead of the current directory. |
| `--no-project` | Set up the machine, register nothing. |
| `--offline` | Do not ask PyPI what the latest version is. |
| `--version V` | Pin `aisquare-cli` to `V`. |

It refuses to run as root outside a container, uses `sudo` only for the system
packages and one command at a time, and never edits your shell profile beyond
what uv and the Claude Code and Codex installers do themselves. Exit codes: `0` installed,
`1` a fatal step failed, `2` installed but a health check is unexpectedly amber,
or Node did not install.

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

Update in place with `aisquare upgrade`. It keeps your extras and re-connects
the hooks of Claude Code and Codex: in every Claude Code directory and Codex
home this machine connected, and in each Codex account home under
`~/.aisquare/codex-accounts`, so they name the new install. Codex treats
rewritten hooks as a new definition, and the report says when to review them
again in its `/hooks`. It runs for the uv tool install the one-liner makes; any
other install is shown the exact command that updates it. Releases before 0.8.0
have no `upgrade` yet: re-run the one-liner instead. It warns first, naming them,
while fleet agents run or while Remote is on in another shell or another asq.

Remove it with `aisquare uninstall`. It takes aisquare's hooks out of Claude
Code and Codex (`~/.codex`, `~/.codex*`, `$CODEX_HOME`, the Codex account
homes), leaving every other hook as it was, then removes the package of a uv
tool install, whatever it was installed from; any other install, and any install
on native Windows, is shown the command that removes it. MCP servers and
plugins that run aisquare are named with the command that removes each:
`claude mcp remove …` for `.claude.json` and `.mcp.json`, `codex mcp remove
aisquare` for a Codex home's `[mcp_servers.aisquare]`, and the plugin's
uninstall. It never rewrites those files, Codex's `config.toml` included. It
keeps `~/.aisquare` (your memory and boards) unless you add `--purge`, which
also deletes the Claude Code plugin's answers about uv in `~/.cache/aisquare`,
and it shows what it will do and asks first. It refuses, naming them, while
fleet agents run or while Remote is on in another shell or another asq
([Remote](remote.md)).

## Start the GUI

The installer offers this at the end; if you skipped it:

```sh
asq                                    # open the UI
```

Installed by hand? Wire the hooks the UI reads state from, once:

```sh
aisquare agents connect claude-code    # or: aisquare agents connect codex, then trust them in Codex's /hooks
asq
```

The README's [second quickstart](../README.md#quickstart-2-the-full-fleet)
walks the first run: add a project, start its manager, watch the agents
appear. The fleet guide's [first five minutes](fleet.md#the-first-five-minutes)
has every key and state chip, and the README lists the plain commands behind
the UI.

Scripts never meet a full-screen app: bare `aisquare` in a pipe, or under
`TERM=dumb`, prints usage and exits 2 exactly as before, and under `--json` it
prints one usage object so a `jq` pipeline gets JSON rather than a help page.

**[The fleet guide](fleet.md)** has the roles in full, the
`aisquare fleet …` command reference and every default you can change.
