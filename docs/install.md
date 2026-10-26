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

That installs the latest release. Every release from 0.9.0 on carries its own
`install.sh` and `install.ps1`, which install exactly that version, so to pin
one, put its tag in the URL:

```sh
curl -fsSL https://github.com/AISquare-Studio/aisquare-cli/releases/download/v0.9.0/install.sh | sh
```

`--version V` (or `AISQUARE_INSTALL_VERSION=V`) still overrides the version a
script was released with, and `--version latest` drops it. The newest release's
copy is also at `https://github.com/AISquare-Studio/aisquare-cli/releases/latest/download/install.sh`; it can answer 404 for a few minutes right
after a release, before its installers are attached.

The copy on `main` runs that release's own installer for you. It downloads the
latest release's `install.sh`, or release V's with `--version V`, checks that it
is stamped with the version it should install, and runs it with your arguments.
So the steps and checks are always the ones that release shipped with, never
main's newer ones. When that copy cannot be downloaded or checked (no network,
a release from before 0.9.0, or the minutes before a new release's installers are
attached), main's script carries on by itself. Either way it says which in one
line: `using the v0.9.0 installer` or `using this installer (…)`. `--no-handoff`,
`--offline` and `AISQUARE_INSTALL_NO_HANDOFF=1` keep main's own copy, and a
release's own copy never hands off. With no `--version`, the release's script installs
the latest release unpinned, as main's would, so `uv tool upgrade aisquare-cli` moves
it later as well as asq's update row and `aisquare upgrade`. `--version V` pins, and a
release's own `install.sh` run by hand still installs exactly that version.

macOS, Linux and WSL2. It installs [uv](https://docs.astral.sh/uv/), a Python
3.13 for the CLI alone, `aisquare-cli`, tmux, gh, git and
[Claude Code](https://claude.com/claude-code), then registers the git repo you
ran it from and wires Claude Code's hooks. It offers the Chrome DevTools MCP too,
defaulting to no: Node 22.12+, then the MCP registered with Claude Code at user scope
(`claude mcp add chrome-devtools --scope user npx chrome-devtools-mcp@latest`; undo with
`claude mcp remove -s user chrome-devtools`). aisquare does not need it; browser tools
do. The server sends Google usage statistics unless
`CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS` is set, as its README says. Running it again is
a no-op: it reports what is current and installs nothing.

On **Windows**, everything runs inside WSL2 — the UI gives each agent a real
tmux pane and Windows has no tmux. This does both steps for you, in PowerShell:

```text
irm https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.ps1 | iex
```

It runs main's `install.sh` inside WSL, so the hand-off above happens there,
with the same checks and the same one line. A release's own `install.ps1`
pins the same way:
`irm https://github.com/AISquare-Studio/aisquare-cli/releases/download/v0.9.0/install.ps1 | iex`.

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
| `--no-system-deps` | Skip tmux, gh and git, and ask about no optional extra. An extra named with `--with` still installs, with the package manager and `sudo`. |
| `--with chrome-devtools` | Install the optional Chrome DevTools MCP without asking: Node 22.12+, then the MCP registered with Claude Code (not under `--no-agent`, nor when it is already registered). `--yes` alone asks nothing and installs no extra. |
| `--project DIR` | Register `DIR` instead of the current directory. |
| `--no-project` | Set up the machine, register nothing. |
| `--offline` | Do not ask PyPI what the latest version is, and keep this copy of the script. |
| `--no-handoff` | Run this copy of the script, not the latest release's own (see above). |
| `--version V` | Pin `aisquare-cli` to `V`, or `latest` to undo a release's own pin. |

It refuses to run as root outside a container, uses `sudo` only for the system
packages and one command at a time, and never edits your shell profile beyond
what uv and the Claude Code installer do themselves. Exit codes: `0` installed,
`1` a fatal step failed, `2` installed but a health check is unexpectedly amber,
or an optional extra you chose did not install: Node or npx for
`--with chrome-devtools`. Its Claude Code registration only warns.

</details>

Requires **Python 3.11+** if you install by hand (the one-liner brings its own
3.13). The package is `aisquare-cli`; the command is `aisquare`, with `asq` as
the short alias.

Then check the machine at any time:

```sh
aisquare doctor
```

It reports every dependency and gives the exact command for anything missing.

Update in place with `aisquare upgrade`. It keeps your extras and re-connects
Claude Code's hooks. It runs for the uv tool install the one-liner makes; any
other install is shown the exact command that updates it. Releases before 0.8.0
have no `upgrade` yet: re-run the one-liner instead. It warns first, naming them,
while fleet agents run or while Remote is on in another shell or another asq.

### Updating from asq

From 0.9.0 on, asq checks for a new release itself, at most once a day: at
start when its last answer is a day old, and again each day while it runs. It
asks PyPI what `aisquare upgrade --check` asks, and nothing else; a lookup that
fails (offline) is silent and tried again within the hour. An 0.8.x install has
no check yet: update it once with `aisquare upgrade`. When a newer release is
out, a row shows under *⚙ Settings*, and a toast says so once per release:

```text
 ⚙ Settings
 ⬆ 0.9.1 · restart to update
```

Click the row, or move to it with ↓ and press Enter. The dialog says what
happens and how many agents are running, and asks PyPI again as it opens.
*Restart & update* stays off until PyPI answers, for 5 seconds at most, and the
dialog then names the release PyPI answered. If PyPI does not answer by then,
the button comes on all the same and the dialog says it installs the newest
release, with the last check's number given as that; an answer that comes later
is ignored, as is one for a dialog already closed. If PyPI says you already run
the newest, the dialog closes and a toast says so.

*Restart & update* closes asq, runs `aisquare upgrade` without asking again,
and reopens asq on the new version in the view you had open. It installs
exactly as `aisquare upgrade` does, unpinned: the newest release at the moment
you press, so one that comes out while the dialog stays open is the one
installed, and a later `uv tool upgrade aisquare-cli` still moves the install.
Cancel or Esc changes nothing.

Your agents keep running through it: they live on the fleet's own tmux server,
not inside asq, and a reinstall does not touch them. asq writes down the agents
running when you press the button; after the reopen, any whose pane is gone is
restarted on its session, as `aisquare fleet restart` does, and the toast says
what it found: `✓ aisquare 0.9.1 — 2 agents reattached`, plus each restart and
its reason. As *Restart all* does, it leaves an agent stopped on purpose since
the press (`aisquare fleet stop`, or Stop in its view) and one that ended by
itself with exit 0, and names each: `1 left as it was (coder-1: stopped on
purpose)`. That record is for this reopen only: if asq cannot open again, or
you stay in the shell, it is dropped, and one the reopen does not take within
ten minutes of the update handing back to asq is never acted on. If the install
fails, its output stays on screen until you press Enter, and the version you
had reopens.

An install pinned to its release, by that release's own `install.sh` run from its
release URL or by `--version`, is checked too: *Restart & update* and `aisquare upgrade` move it to
the newest release and drop the pin, which `uv tool upgrade aisquare-cli` alone
does not.

On Homebrew, pip or pipx the row still shows, and the dialog gives the command
that updates that install instead of a restart button. A source checkout or an
editable install is never checked. To turn the check off:

```sh
aisquare config set updates.check false
```

or switch off *Check for a new release daily* under App settings ▸ User preferences
▸ Updates.

Remove it with `aisquare uninstall`. It takes aisquare's hooks out of Claude
Code, then removes the package of a uv tool install, whatever it was installed
from; any other install, and any install on native Windows, is shown the
command that removes it. It keeps `~/.aisquare` (your memory and boards) unless
you add `--purge`, which also deletes the Claude Code plugin's answers about uv
in `~/.cache/aisquare`, and it shows what it will do and asks first. It refuses,
naming them, while fleet agents run or while Remote is on in another shell or
another asq ([Remote](remote.md)).

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
